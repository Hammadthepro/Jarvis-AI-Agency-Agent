"""Agent 4: Website verification. Evidence-based, deterministic.

Statuses: VERIFIED_HAS_WEBSITE (site_health working|broken|outdated), VERIFIED_NO_WEBSITE,
WEBSITE_UNCERTAIN, CONFLICTING_EVIDENCE, VERIFICATION_FAILED.

A missing listed URL is only a hint. VERIFIED_NO_WEBSITE requires every required independent check
to complete: >=1 SERP query (2 when a phone is known), fetchable profile pages checked for website
links, candidate-domain DNS checks, and confidence >= NO_WEBSITE_MIN_CONFIDENCE.
A listed site counts as broken only after a second failing probe at least N minutes later."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from jarvis.agents.deps import Deps
from jarvis.core.errors import IntegrationState, ProviderError
from jarvis.integrations.base import CallContext
from jarvis.security.robots import registrable_domain, terms_blocked
from jarvis.security.ssrf import USER_AGENT, DnsFailure, FetchResult, UnsafeDestination

DIRECTORY_DOMAINS = {
    "yelp.com", "facebook.com", "instagram.com", "linkedin.com", "bbb.org", "angi.com", "angieslist.com", "homeadvisor.com",
    "thumbtack.com", "yellowpages.com", "mapquest.com", "manta.com", "nextdoor.com", "porch.com", "houzz.com",
    "google.com", "apple.com", "bing.com", "tripadvisor.com", "foursquare.com", "chamberofcommerce.com", "superpages.com",
    "x.com", "twitter.com", "tiktok.com", "youtube.com", "pinterest.com", "bizapedia.com", "opencorporates.com",
    "zoominfo.com", "dnb.com", "buzzfile.com", "birdeye.com", "nextdoor.co", "groupon.com", "doordash.com", "ubereats.com",
    "grubhub.com", "zocdoc.com", "healthgrades.com", "vitals.com", "avvo.com", "justia.com", "findlaw.com", "expertise.com",
    "wikipedia.org", "reddit.com", "craigslist.org", "indeed.com", "glassdoor.com"}
SOCIAL_DOMAINS = {"facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "pinterest.com"}
GENERIC_NAME_TOKENS = {"llc", "inc", "co", "company", "corp", "corporation", "ltd", "the", "and", "&", "services", "service",
                       "of", "pllc", "pc", "group"}
BLOCKING_STATES = {IntegrationState.NOT_CONFIGURED, IntegrationState.INVALID_CREDENTIALS, IntegrationState.PERMISSION_DENIED,
                   IntegrationState.QUOTA_EXCEEDED, IntegrationState.DISABLED, IntegrationState.BLOCKED_PENDING_APPROVAL}
PARKED_MARKERS = ("domain is for sale", "buy this domain", "this domain may be for sale", "domain parking",
                  "parked free", "hugedomains", "sedo.com", "dan.com", "afternic", "this domain has expired",
                  "domain has expired", "is parked")


@dataclass(frozen=True, repr=False)
class Candidate:
    """Identity used for verification. When sourced from Google Places it is in-memory only."""
    name: str | None
    phone: str | None
    city: str | None
    state: str | None
    listed_url: str | None
    source: str                    # 'google_places' | 'manual_import' | 'licensed_business_data'

    def __repr__(self) -> str:
        return f"Candidate(source={self.source}, <redacted>)"


@dataclass
class Check:
    check: str
    status: str                    # completed | failed | skipped
    result: str
    detail: dict = field(default_factory=dict)


@dataclass
class Outcome:
    status: str
    site_health: str | None = None
    reason: str | None = None
    url: str | None = None
    final_url: str | None = None
    listing_sources: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)   # {source_type, source_ref, excerpt}
    confidence: float = 0.0
    identity_facts: list[dict] = field(default_factory=list)  # {fact_key, value, quote, source_ref, source_type}
    website_check: str = "WEBSITE_UNKNOWN"                     # precise outcome (WEBSITE_CHECKS)
    listing_website: str = "not_listed"                        # did the discovery source list a website?
    http: list[dict] = field(default_factory=list)             # every probe: status, redirects, final URL, verdict


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower().replace("'", "")) if t not in GENERIC_NAME_TOKENS}


def identity_score(c: Candidate, page_text: str, title: str = "") -> float:
    text = (title + " " + page_text).lower()
    score = 0.0
    if c.name:
        toks = name_tokens(c.name)
        if toks:
            present = sum(1 for t in toks if t in text) / len(toks)
            ratio = SequenceMatcher(None, c.name.lower(), title.lower()).ratio() if title else 0
            score += 0.5 * max(present, ratio)
    ph = digits(c.phone)
    if len(ph) == 10 and ph in re.sub(r"\D", "", text):
        score += 0.35
    if c.city and c.city.lower() in text:
        score += 0.15
    return round(min(score, 1.0), 2)


def extract_identity(c: Candidate, page_text: str, source_ref: str, source_type: str) -> list[dict]:
    """Quote-backed identity facts from an INDEPENDENT page (never from Places data itself)."""
    facts: list[dict] = []
    if c.name:
        m = re.search(re.escape(c.name), page_text, re.I)
        if m:
            facts.append({"fact_key": "business_name", "value": m.group(0), "quote": m.group(0),
                          "source_ref": source_ref, "source_type": source_type})
    ph = digits(c.phone)
    if len(ph) == 10:
        pat = r"\(?" + ph[:3] + r"\)?[\s.-]*" + ph[3:6] + r"[\s.-]*" + ph[6:]
        m = re.search(pat, page_text)
        if m:
            facts.append({"fact_key": "phone", "value": f"+1{ph}", "quote": m.group(0),
                          "source_ref": source_ref, "source_type": source_type})
    return facts


def is_parked(body: str) -> bool:
    low = body.lower()
    return any(m in low for m in PARKED_MARKERS)


def quality_signals(res: FetchResult) -> tuple[str, dict]:
    body = res.body.lower()
    sig = {"https": res.final_url.startswith("https://"),
           "viewport_meta": 'name="viewport"' in body or "name='viewport'" in body,
           "flash": ".swf" in body or "<embed" in body and "flash" in body,
           "table_layout": body.count("<table") >= 4 and "<div" not in body[:5000]}
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", body)]
    sig["copyright_year"] = max(years) if years else None
    now = datetime.now(UTC).year
    outdated = (not sig["viewport_meta"]) or (not sig["https"]) or sig["flash"] or sig["table_layout"] \
        or (sig["copyright_year"] is not None and sig["copyright_year"] < now - 5)
    return ("outdated" if outdated else "working"), sig


def _title(body: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""


def _visible(body: str) -> str:
    b = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b))[:100_000]


def candidate_domains(name: str, city: str | None, n: int) -> list[str]:
    toks = [t for t in re.findall(r"[a-z0-9]+", name.lower().replace("'", "")) if t not in {"llc", "inc", "the", "co"}]
    if not toks:
        return []
    base = "".join(toks)
    opts = [f"{base}.com"]
    if city:
        opts.append(f"{base}{re.sub(r'[^a-z]', '', city.lower())}.com")
    opts.append(f"{base}.net")
    opts.append(f"{'-'.join(toks)}.com")
    seen: list[str] = []
    for o in opts:
        if o not in seen and len(o) <= 70:
            seen.append(o)
    return seen[:n]


def query_hash(q: str) -> str:
    return hashlib.sha256(q.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# HTTP classification. A probe's verdict says what the server actually answered; only some verdicts can confirm that
# a website is broken, and only after a second probe minutes later. Others say "a site is there but we could not check
# it" and must never become "broken" or "no website".
# ---------------------------------------------------------------------------------------------------------------------
CONFIRMS_BROKEN = {"nxdomain", "connect_error", "tls_error", "http_5xx", "not_found", "parked", "redirect_loop"}
SITE_PRESENT_UNVERIFIABLE = {"blocked", "auth_required", "rate_limited", "robots_disallowed", "http_5xx", "timeout",
                             "http_4xx_other", "unconfirmed_identity"}
WEBSITE_CHECK = {"blocked": "WEBSITE_BLOCKED", "robots_disallowed": "WEBSITE_BLOCKED", "auth_required": "WEBSITE_AUTH_REQUIRED",
                 "rate_limited": "WEBSITE_RATE_LIMITED", "unconfirmed_identity": "WEBSITE_FOUND"}
WEBSITE_CHECKS = ("NO_WEBSITE_LISTED", "NO_WEBSITE_FOUND", "WEBSITE_FOUND", "WEBSITE_WORKING", "WEBSITE_BROKEN_CONFIRMED",
                  "WEBSITE_BLOCKED", "WEBSITE_RATE_LIMITED", "WEBSITE_AUTH_REQUIRED", "WEBSITE_REDIRECTED", "WEBSITE_UNKNOWN")
ANTI_BOT_MARKERS = ("captcha", "attention required", "just a moment", "access denied", "are you a robot", "cf-chl",
                    "request blocked", "bot detection")
_ROBOTS: dict[str, tuple[float, str]] = {}      # origin -> (expires, verdict); per worker process
ROBOTS_TTL_S = 6 * 3600


def classify(res: FetchResult) -> str:
    """Verdict of one fetch: ok | parked | not_found | blocked | auth_required | rate_limited | http_5xx | http_4xx_other
    | nxdomain | connect_error | tls_error | timeout | redirect_loop | unsafe."""
    if res.error:
        e = res.error.split(":")[0]
        return {"too_many_redirects": "redirect_loop"}.get(e, e)
    s = res.status
    if s is None:
        return "connect_error"
    if s in (401, 407):
        return "auth_required"
    if s == 403:
        return "blocked"
    if s == 429:
        return "rate_limited"
    if s in (404, 410):
        return "not_found"
    if s >= 500:
        return "http_5xx"
    if s >= 400:
        return "http_4xx_other"
    return "parked" if is_parked(res.body) else "ok"


def anti_bot(res: FetchResult) -> bool:
    h = res.headers or {}
    body = (res.body or "")[:20_000].lower()
    return bool("cf-ray" in h or "cf-mitigated" in h or any(m in body for m in ANTI_BOT_MARKERS))


def retry_after_s(res: FetchResult) -> float | None:
    ra = (res.headers or {}).get("retry-after", "")
    return float(ra) if ra.replace(".", "", 1).isdigit() else None


def probe_record(label: str, res: FetchResult, verdict: str) -> dict:
    """The evidence behind a verdict: what was requested, what answered, where it redirected."""
    return {"check": label, "url": res.url, "final_url": res.final_url, "status": res.status, "verdict": verdict,
            "redirects": list(res.redirects or [])[:10], "content_type": (res.headers or {}).get("content-type", "")[:80],
            "error": res.error, "anti_bot": anti_bot(res) if verdict in ("blocked", "rate_limited") else None,
            "retry_after_s": retry_after_s(res) if verdict == "rate_limited" else None,
            "at": datetime.now(UTC).isoformat(timespec="seconds")}


def name_in_domain(name: str | None, host: str) -> bool:
    """A domain plausibly belongs to the business when its label contains the business's distinctive name words."""
    label = (host or "").lower().split(".")[0].replace("-", "")
    toks = [t for t in name_tokens(name or "") if len(t) >= 3]
    if not toks or not label:
        return False
    hit = sum(1 for t in toks if t in label)
    return hit == len(toks) or hit >= 2


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class WebsiteVerifier:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def _robots(self, url: str) -> tuple[str, FetchResult | None]:
        """robots.txt decision for the URL's origin: allowed | robots_disallowed | blocked | http_5xx | unreachable.
        Network failures are never cached (a later probe must be live)."""
        origin = _origin(url)
        hit = _ROBOTS.get(origin)
        if hit and hit[0] > time.monotonic():
            return hit[1], None
        res = await self.deps.fetch_url(origin + "/robots.txt")
        v = classify(res)
        if v in ("nxdomain", "connect_error", "tls_error", "timeout", "unsafe", "redirect_loop"):
            return "unreachable", res
        if res.status in (401, 403):
            verdict = "blocked"
        elif res.status is not None and res.status >= 500:
            return "http_5xx", res
        elif res.status is not None and res.status >= 400:
            verdict = "allowed"                      # no robots.txt: no restrictions (RFC 9309)
        else:
            rp = RobotFileParser()
            rp.parse(res.body.splitlines())
            verdict = "allowed" if (rp.can_fetch(USER_AGENT.split("/")[0], url) and rp.can_fetch("*", url)) else "robots_disallowed"
        _ROBOTS[origin] = (time.monotonic() + ROBOTS_TTL_S, verdict)
        return verdict, res

    async def _probe(self, url: str, out: Outcome | None = None, label: str = "probe") -> tuple[FetchResult, str]:
        """One SSRF-safe check of a site: robots.txt first (respected), then the page. Records the evidence."""
        if not re.match(r"^https?://", url):
            url = "https://" + url
        rv, rres = await self._robots(url)
        if rv == "unreachable":
            res, verdict = rres, classify(rres)
        elif rv in ("blocked", "robots_disallowed", "http_5xx"):
            res = rres or FetchResult(url, url, None, {}, "", error=None)
            verdict = rv
        else:
            res = await self.deps.fetch_url(url)
            verdict = classify(res)
            ra = retry_after_s(res) if verdict == "rate_limited" else None
            if ra is not None and ra <= 15:          # honour a short Retry-After once; longer waits stay "rate limited"
                await asyncio.sleep(ra)
                res = await self.deps.fetch_url(url)
                verdict = classify(res)
            if verdict == "not_found" and urlsplit(res.final_url).path not in ("", "/"):
                root = await self.deps.fetch_url(_origin(res.final_url) + "/")   # a missing deep link is not a missing site
                if classify(root) == "ok":
                    res, verdict = root, "ok"
        if out is not None:
            out.http.append(probe_record(label, res, verdict))
        return res, verdict

    def _site_found(self, out: Outcome, c: Candidate, res: FetchResult, score: float, reason: str, confidence: float) -> Outcome:
        health, sig = quality_signals(res)
        out.status, out.site_health, out.url, out.final_url = "VERIFIED_HAS_WEBSITE", health, res.final_url, res.final_url
        start = urlsplit(res.url).hostname or ""
        moved = bool(res.redirects) and registrable_domain(start) != registrable_domain(urlsplit(res.final_url).hostname or "")
        out.website_check = "WEBSITE_REDIRECTED" if moved else "WEBSITE_WORKING"
        out.evidence.append({"source_type": "http_probe", "source_ref": res.final_url, "excerpt": _title(res.body) or None,
                             "signals": sig, "redirects": list(res.redirects or [])[:10]})
        out.identity_facts += extract_identity(c, _visible(res.body), res.final_url, "official_website")
        out.confidence, out.reason = confidence, reason
        return out

    async def verify(self, ctx: CallContext, c: Candidate) -> Outcome:
        out = await self._verify(ctx, c)
        if c.source == "google_places":
            # The listed URL is Google Places content: keep what the server answered, never the URL itself.
            for rec in out.http:
                if rec["check"].startswith("listed_site"):
                    rec.update(url="listed-url (google-derived, not stored)", final_url=None, redirects=[])
        return out

    async def _verify(self, ctx: CallContext, c: Candidate) -> Outcome:
        out = Outcome(status="WEBSITE_UNCERTAIN", website_check="WEBSITE_UNKNOWN",
                      listing_website="listed" if c.listed_url else "not_listed")
        delay = (self.deps.second_probe_delay_s if self.deps.second_probe_delay_s is not None
                 else self.deps.settings.broken_second_probe_minutes * 60)

        # 1) Website listed by the discovery source: a hint to check, never proof either way.
        if c.listed_url:
            out.listing_sources.append("maps_hint" if c.source == "google_places" else c.source)
            res, verdict = await self._probe(c.listed_url, out, "listed_site")
            if verdict not in ("ok",) and (verdict in CONFIRMS_BROKEN or verdict == "timeout"):
                out.checks.append(Check("listed_site_probe", "completed", verdict, {"status": res.status}))
                await asyncio.sleep(delay)
                res2, verdict2 = await self._probe(c.listed_url, out, "listed_site_second_probe")
                out.checks.append(Check("listed_site_second_probe", "completed", verdict2, {"delay_s": delay, "status": res2.status}))
                if verdict in CONFIRMS_BROKEN and verdict2 in CONFIRMS_BROKEN:
                    out.status, out.site_health, out.reason = "VERIFIED_HAS_WEBSITE", "broken", f"{verdict}, then {verdict2}"
                    out.website_check = "WEBSITE_BROKEN_CONFIRMED"
                    out.url = c.listed_url if c.source != "google_places" else None
                    out.evidence.append({"source_type": "http_probe", "source_ref": out.url or "listed-url (google-derived, not stored)",
                                         "excerpt": f"two failing probes {delay:.0f}s apart: {verdict} (HTTP {res.status}), "
                                                    f"{verdict2} (HTTP {res2.status})"})
                    out.confidence = 0.85
                    return out
                res, verdict = res2, verdict2
            if verdict == "ok":
                score = identity_score(c, _visible(res.body), _title(res.body))
                out.checks.append(Check("listed_site_probe", "completed", "responds", {"identity": score, "status": res.status}))
                if score >= 0.6:
                    return self._site_found(out, c, res, score, "identity_confirmed_on_site", min(1.0, 0.6 + score * 0.4))
                out.checks.append(Check("listed_site_identity", "completed", "mismatch", {"identity": score}))
            elif verdict in SITE_PRESENT_UNVERIFIABLE or verdict == "unsafe":
                # The listed site answered, but not with a page we may read: blocked, login, rate limit, robots, a server
                # error that did not persist, or a timeout. Inconclusive by design: not broken, not "no website".
                out.checks.append(Check("listed_site_probe", "completed", verdict, {"status": res.status}))
                out.website_check = WEBSITE_CHECK.get(verdict, "WEBSITE_UNKNOWN")
                out.url = c.listed_url if c.source != "google_places" else None
                out.reason, out.confidence = f"listed_site_{verdict}", 0.5
                return out

        # 2) Independent search (name + location, and the phone number), in parallel.
        if not c.name:
            out.status, out.reason = "VERIFICATION_FAILED", "no_identity_to_search"
            return out
        queries = [(f'"{c.name}" {c.city or ""} {c.state or ""}'.strip(), "serp_name_location")]
        if len(digits(c.phone)) == 10:
            p = digits(c.phone)
            queries.append((f'"({p[:3]}) {p[3:6]}-{p[6:]}"', "serp_phone"))
        loc = ", ".join(x for x in (c.city, c.state) if x) or None

        async def search(q: str):
            try:
                return await self.deps.serp.search(ctx, q, location=loc), None
            except ProviderError as exc:
                if exc.state in BLOCKING_STATES:
                    raise  # missing credentials/quota must surface as a blocked task, not a silent "failed check"
                return None, exc
        found = await asyncio.gather(*(search(q) for q, _ in queries))
        candidates: dict[str, dict] = {}
        profiles: list[str] = []
        for (q, label), (resp, exc) in zip(queries, found, strict=True):
            if exc is not None:
                out.checks.append(Check(label, "failed", exc.state.value, {"error": exc.message[:200]}))
                continue
            out.checks.append(Check(label, "completed", f"{len(resp.results)} results",
                                    {"query_hash": query_hash(q) if c.source == "google_places" else None,
                                     "query": None if c.source == "google_places" else q, "search_id": resp.search_id}))
            urls = [(r.link, r.title, r.snippet) for r in resp.results[:10]]
            if resp.local_website:
                urls.insert(0, (resp.local_website, "knowledge graph website", None))
            for link, title, snippet in urls:
                host = registrable_domain(urlsplit(link).hostname or "")
                if not host:
                    continue
                out.evidence.append({"source_type": "serp_result", "source_ref": link, "excerpt": (snippet or title or "")[:400]})
                if host in DIRECTORY_DOMAINS:
                    if len(profiles) < self.deps.settings.max_profile_checks and link not in profiles:
                        profiles.append(link)
                    continue
                candidates.setdefault(host, {"url": link, "title": title})

        serp_done = [ch for ch in out.checks if ch.check.startswith("serp_") and ch.status == "completed"]
        serp_failed = [ch for ch in out.checks if ch.check.startswith("serp_") and ch.status == "failed"]
        matches: list[tuple[str, float, FetchResult]] = []
        unverifiable: list[tuple[str, str, str]] = []   # (host, verdict, url): a likely site we could not read

        def weigh(host: str, url: str, res: FetchResult, verdict: str, check: str) -> None:
            if verdict == "ok":
                score = identity_score(c, _visible(res.body), _title(res.body))
                out.checks.append(Check(check, "completed", f"identity {score}", {"host": host}))
                if score >= 0.6:
                    matches.append((host, score, res))
                elif score >= 0.3 and name_in_domain(c.name, host):
                    unverifiable.append((host, "unconfirmed_identity", url))
                return
            out.checks.append(Check(check, "completed", verdict, {"host": host, "status": res.status}))
            if verdict in SITE_PRESENT_UNVERIFIABLE and name_in_domain(c.name, host):
                unverifiable.append((host, verdict, url))

        # 3) Probe search candidates (parallel)
        cand = list(candidates.items())[:5]
        for (host, info), (res, verdict) in zip(cand, await asyncio.gather(
                *(self._probe(i["url"], out, "candidate") for _h, i in cand)), strict=True):
            weigh(host, info["url"], res, verdict, "candidate_probe")

        # 4) Profiles: look for a website link (skip sites whose terms forbid automated access)
        profile_links: set[str] = set()
        allowed_profiles = []
        for link in profiles:
            if terms_blocked(link):
                out.checks.append(Check("profile_check", "skipped", "terms_blocked", {"host": urlsplit(link).hostname}))
            else:
                allowed_profiles.append(link)
        for link, (res, verdict) in zip(allowed_profiles, await asyncio.gather(
                *(self._probe(link, out, "profile") for link in allowed_profiles)), strict=True):
            if verdict != "ok":
                out.checks.append(Check("profile_check", "failed", verdict, {"host": urlsplit(link).hostname}))
                continue
            links = {registrable_domain(urlsplit(h).hostname or "") for h in re.findall(r'href="(https?://[^"]+)"', res.body)}
            links = {h for h in links if h and h not in DIRECTORY_DOMAINS and h != registrable_domain(urlsplit(link).hostname or "")}
            profile_links |= links
            if identity_score(c, _visible(res.body), _title(res.body)) >= 0.6:
                out.identity_facts += extract_identity(c, _visible(res.body), res.final_url,
                                                       "social_page" if registrable_domain(urlsplit(link).hostname or "") in SOCIAL_DOMAINS else "directory_page")
                out.evidence.append({"source_type": "directory_page", "source_ref": res.final_url, "excerpt": _title(res.body)})
            out.checks.append(Check("profile_check", "completed", f"{len(links)} external links", {"host": urlsplit(link).hostname}))
        extra = [h for h in list(profile_links)[:3] if h not in {m[0] for m in matches}]
        for host, (res, verdict) in zip(extra, await asyncio.gather(
                *(self._probe(f"https://{h}", out, "profile_link") for h in extra)), strict=True):
            weigh(host, f"https://{host}", res, verdict, "profile_link_probe")

        # 5) Candidate domains derived from the name (DNS, then a probe where they resolve), in parallel.
        doms = candidate_domains(c.name, c.city, self.deps.settings.max_candidate_domains)

        async def resolve(dom: str) -> str:
            try:
                await self.deps.resolver(dom, 443)
                return "resolves"
            except DnsFailure:
                return "nxdomain"
            except UnsafeDestination:
                return "non_public"
        dns = await asyncio.gather(*(resolve(d) for d in doms))
        live = [d for d, r in zip(doms, dns, strict=True) if r == "resolves"]
        probes = dict(zip(live, await asyncio.gather(*(self._probe(f"https://{d}", out, "candidate_domain") for d in live)),
                          strict=True))
        dns_done = len(doms)
        for dom, r in zip(doms, dns, strict=True):
            if r != "resolves":
                out.checks.append(Check("candidate_domain_dns", "completed", r, {"domain": dom}))
                continue
            res, verdict = probes[dom]
            score = identity_score(c, _visible(res.body), _title(res.body)) if verdict == "ok" else 0
            out.checks.append(Check("candidate_domain_dns", "completed", f"resolves; {verdict}; identity {score}", {"domain": dom}))
            if score >= 0.6 and dom not in {m[0] for m in matches}:
                matches.append((dom, score, res))
            elif verdict in SITE_PRESENT_UNVERIFIABLE or (verdict == "ok" and score >= 0.3):
                unverifiable.append((dom, verdict if verdict != "ok" else "unconfirmed_identity", f"https://{dom}"))

        # 6) Decide
        distinct = {m[0] for m in matches}
        if len(distinct) > 1:
            out.status, out.reason, out.confidence = "CONFLICTING_EVIDENCE", "multiple_sites_match_identity", 0.4
            out.evidence.extend({"source_type": "http_probe", "source_ref": m[2].final_url, "excerpt": f"identity {m[1]}"} for m in matches)
            return out
        if matches:
            host, score, res = matches[0]
            self._site_found(out, c, res, score, "found_via_independent_search", round(min(1.0, 0.5 + score * 0.5), 2))
            if c.listed_url and any(ch.check == "listed_site_identity" for ch in out.checks):
                out.reason = "listed_url_mismatch_official_site_found"
            return out
        if unverifiable:
            # A site that is probably theirs exists but could not be read (blocked, login, rate limit, robots, server
            # error) or could not be tied to them: never conclude "no website" from that.
            host, verdict, url = unverifiable[0]
            out.status, out.website_check, out.url = "WEBSITE_UNCERTAIN", WEBSITE_CHECK.get(verdict, "WEBSITE_UNKNOWN"), url
            out.reason, out.confidence = f"likely_site_unverifiable:{verdict}", 0.4
            return out
        if serp_failed or not serp_done:
            out.status, out.reason, out.confidence = "VERIFICATION_FAILED", "verification_incomplete", 0.0
            return out
        if any(ch.check == "listed_site_identity" for ch in out.checks):
            out.status, out.reason, out.confidence = "CONFLICTING_EVIDENCE", "listed_site_belongs_to_other_business", 0.4
            return out
        conf = 0.45 + 0.2 * len(serp_done) + (0.1 if dns_done else 0) + \
            (0.1 if any(ch.check == "profile_check" and ch.status == "completed" for ch in out.checks) else 0)
        if len(digits(c.phone)) != 10:
            conf -= 0.1
        conf = round(min(conf, 1.0), 2)
        out.confidence = conf
        if conf >= self.deps.settings.no_website_min_confidence:
            out.status, out.reason, out.website_check = "VERIFIED_NO_WEBSITE", "all_required_checks_completed_no_site_found", \
                "NO_WEBSITE_FOUND"
        else:
            out.status, out.reason = "WEBSITE_UNCERTAIN", "insufficient_evidence"
            out.website_check = "NO_WEBSITE_LISTED" if not c.listed_url else "WEBSITE_UNKNOWN"
        return out


def provenance(source_type: str, source_ref: str) -> tuple[str, str]:
    """(provider, independence key) of a piece of evidence. Pages are grouped by website: two facts from one website
    (or from one provider listing) are ONE source, not two independent confirmations."""
    host = registrable_domain(urlsplit(source_ref).hostname or "") if "://" in (source_ref or "") else ""
    provider = {"serp_result": "serp_api", "firecrawl_page": "firecrawl", "manual_import": "manual_import"}.get(source_type, "web")
    if source_type == "serp_result" and host:
        return "serp_api", f"web:{host}"          # a search result points at another website: that website is the source
    if host:
        return provider, f"web:{host}"
    return provider, f"{provider}:{(source_ref or '')[:80]}"


async def persist_outcome(db, *, workspace_id, prospect_id, task_id, out: Outcome) -> list:
    """Writes evidence + verification record. Never writes Google-derived values."""
    async with db.transaction() as conn:
        ev_ids = []
        for e in out.evidence[:40]:
            provider, ind = provenance(e["source_type"], e["source_ref"])
            ev_ids.append(await conn.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt,
                                        source_provider, independence_key)
                   values ($1,$2,$3,$4,$5,$6,$7,$8) returning id""",
                workspace_id, prospect_id, e["source_type"], e["source_ref"][:2000],
                "days_180" if e["source_type"] == "serp_result" else "indefinite", (e.get("excerpt") or None), provider, ind))
        seen: set[tuple] = set()
        for f in out.identity_facts:
            key = (f["fact_key"], f["value"])
            if key in seen:
                continue
            seen.add(key)
            provider, ind = provenance(f["source_type"], f["source_ref"])
            eid = await conn.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt,
                                        source_provider, independence_key)
                   values ($1,$2,$3,$4,'indefinite',$5,$6,$7) returning id""",
                workspace_id, prospect_id, f["source_type"], f["source_ref"][:2000], f["quote"], provider, ind)
            await conn.execute(
                """insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                   values ($1,$2,$3,$4,'verified',$5,$6,0.9)""",
                workspace_id, prospect_id, f["fact_key"], f["value"], eid, f["quote"])
        await conn.execute(
            """insert into website_verifications(workspace_id, prospect_id, task_id, status, site_health, reason, url,
                   final_url, listing_sources, checks, evidence_ids, confidence, website_check, listing_website, http_evidence)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
            workspace_id, prospect_id, task_id, out.status, out.site_health, out.reason, out.url, out.final_url,
            out.listing_sources, [c.__dict__ for c in out.checks], ev_ids, out.confidence, out.website_check,
            out.listing_website, out.http[:40])
        return ev_ids


def satisfies_filter(status: str, health: str | None, website_filter: list[str]) -> bool:
    if "ANY" in website_filter:
        # An inconclusive check (blocked, rate limited, login, unreadable) keeps the business, marked for research,
        # instead of silently dropping it; qualification will not treat its website as an opportunity.
        return status in ("VERIFIED_HAS_WEBSITE", "VERIFIED_NO_WEBSITE", "WEBSITE_UNCERTAIN")
    ok = False
    if "NO_WEBSITE" in website_filter:
        ok |= status == "VERIFIED_NO_WEBSITE"
    if "WEBSITE_BROKEN" in website_filter:
        ok |= status == "VERIFIED_HAS_WEBSITE" and health == "broken"
    if "WEBSITE_WORKING_OUTDATED" in website_filter:
        ok |= status == "VERIFIED_HAS_WEBSITE" and health == "outdated"
    return ok

