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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from jarvis.agents.deps import Deps
from jarvis.core.errors import IntegrationState, ProviderError
from jarvis.integrations.base import CallContext
from jarvis.security.robots import registrable_domain, terms_blocked
from jarvis.security.ssrf import DnsFailure, FetchResult, UnsafeDestination

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


class WebsiteVerifier:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def _probe(self, url: str) -> tuple[FetchResult, str]:
        """Returns (result, verdict) verdict in ok | parked | http_4xx | http_5xx | nxdomain | connect_error | tls_error | timeout | unsafe"""
        if not re.match(r"^https?://", url):
            url = "https://" + url
        res = await self.deps.fetch_url(url)
        if res.error:
            return res, res.error.split(":")[0]
        if res.status is None:
            return res, "connect_error"
        if res.status >= 500:
            return res, "http_5xx"
        if res.status >= 400:
            return res, "http_4xx"
        if is_parked(res.body):
            return res, "parked"
        return res, "ok"

    async def verify(self, ctx: CallContext, c: Candidate) -> Outcome:
        out = Outcome(status="WEBSITE_UNCERTAIN")
        delay = (self.deps.second_probe_delay_s if self.deps.second_probe_delay_s is not None
                 else self.deps.settings.broken_second_probe_minutes * 60)

        # 1) Listed website (hint from the discovery source)
        if c.listed_url:
            out.listing_sources.append("maps_hint" if c.source == "google_places" else c.source)
            res, verdict = await self._probe(c.listed_url)
            if verdict == "ok":
                score = identity_score(c, _visible(res.body), _title(res.body))
                out.checks.append(Check("listed_site_probe", "completed", "responds", {"identity": score, "status": res.status}))
                if score >= 0.6:
                    health, sig = quality_signals(res)
                    out.status, out.site_health, out.final_url = "VERIFIED_HAS_WEBSITE", health, res.final_url
                    out.url = res.final_url  # identity confirmed by our own probe => independent evidence
                    out.evidence.append({"source_type": "http_probe", "source_ref": res.final_url,
                                         "excerpt": _title(res.body) or None, "signals": sig})
                    out.identity_facts += extract_identity(c, _visible(res.body), res.final_url, "official_website")
                    out.confidence = min(1.0, 0.6 + score * 0.4)
                    out.reason = "identity_confirmed_on_site"
                    return out
                out.checks.append(Check("listed_site_identity", "completed", "mismatch", {"identity": score}))
            else:
                out.checks.append(Check("listed_site_probe", "completed", verdict))
                await asyncio.sleep(delay)
                res2, verdict2 = await self._probe(c.listed_url)
                out.checks.append(Check("listed_site_second_probe", "completed", verdict2, {"delay_s": delay}))
                if verdict2 != "ok":
                    out.status, out.site_health, out.reason = "VERIFIED_HAS_WEBSITE", "broken", verdict2
                    out.url = c.listed_url if c.source != "google_places" else None
                    out.evidence.append({"source_type": "http_probe", "source_ref": out.url or "listed-url (google-derived, not stored)",
                                         "excerpt": f"two failing probes {delay:.0f}s apart: {verdict}, {verdict2}"})
                    out.confidence = 0.85
                    return out
                score = identity_score(c, _visible(res2.body), _title(res2.body))
                if score >= 0.6:
                    health, sig = quality_signals(res2)
                    out.status, out.site_health, out.url, out.final_url = "VERIFIED_HAS_WEBSITE", health, res2.final_url, res2.final_url
                    out.evidence.append({"source_type": "http_probe", "source_ref": res2.final_url, "excerpt": _title(res2.body), "signals": sig})
                    out.confidence, out.reason = 0.75, "recovered_on_second_probe"
                    return out

        # 2) Independent search
        if not c.name:
            out.status, out.reason = "VERIFICATION_FAILED", "no_identity_to_search"
            return out
        queries = [(f'"{c.name}" {c.city or ""} {c.state or ""}'.strip(), "serp_name_location")]
        if len(digits(c.phone)) == 10:
            p = digits(c.phone)
            queries.append((f'"({p[:3]}) {p[3:6]}-{p[6:]}"', "serp_phone"))
        candidates: dict[str, dict] = {}
        profiles: list[str] = []
        for q, label in queries:
            try:
                resp = await self.deps.serp.search(ctx, q, location=", ".join(x for x in (c.city, c.state) if x) or None)
            except ProviderError as exc:
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

        # 3) Probe search candidates for identity
        matches: list[tuple[str, float, FetchResult]] = []
        for host, info in list(candidates.items())[:5]:
            res, verdict = await self._probe(info["url"])
            if verdict != "ok":
                out.checks.append(Check("candidate_probe", "completed", verdict, {"host": host}))
                continue
            score = identity_score(c, _visible(res.body), _title(res.body))
            out.checks.append(Check("candidate_probe", "completed", f"identity {score}", {"host": host}))
            if score >= 0.6:
                matches.append((host, score, res))

        # 4) Profiles: look for a website link (skip sites whose terms forbid automated access)
        profile_links: set[str] = set()
        for link in profiles:
            if terms_blocked(link):
                out.checks.append(Check("profile_check", "skipped", "terms_blocked", {"host": urlsplit(link).hostname}))
                continue
            res, verdict = await self._probe(link)
            if verdict != "ok":
                out.checks.append(Check("profile_check", "failed", verdict, {"host": urlsplit(link).hostname}))
                continue
            found = {registrable_domain(urlsplit(h).hostname or "") for h in re.findall(r'href="(https?://[^"]+)"', res.body)}
            found = {h for h in found if h and h not in DIRECTORY_DOMAINS and h != registrable_domain(urlsplit(link).hostname or "")}
            profile_links |= found
            if identity_score(c, _visible(res.body), _title(res.body)) >= 0.6:
                out.identity_facts += extract_identity(c, _visible(res.body), res.final_url,
                                                       "social_page" if registrable_domain(urlsplit(link).hostname or "") in SOCIAL_DOMAINS else "directory_page")
                out.evidence.append({"source_type": "directory_page", "source_ref": res.final_url, "excerpt": _title(res.body)})
            out.checks.append(Check("profile_check", "completed", f"{len(found)} external links", {"host": urlsplit(link).hostname}))
        for host in list(profile_links)[:3]:
            if host in {m[0] for m in matches}:
                continue
            res, verdict = await self._probe(f"https://{host}")
            if verdict == "ok":
                score = identity_score(c, _visible(res.body), _title(res.body))
                if score >= 0.6:
                    matches.append((host, score, res))

        # 5) Candidate domains via DNS
        dns_done = 0
        for dom in candidate_domains(c.name, c.city, self.deps.settings.max_candidate_domains):
            try:
                await self.deps.resolver(dom, 443)
            except DnsFailure:
                dns_done += 1
                out.checks.append(Check("candidate_domain_dns", "completed", "nxdomain", {"domain": dom}))
                continue
            except UnsafeDestination:
                dns_done += 1
                out.checks.append(Check("candidate_domain_dns", "completed", "non_public", {"domain": dom}))
                continue
            dns_done += 1
            res, verdict = await self._probe(f"https://{dom}")
            score = identity_score(c, _visible(res.body), _title(res.body)) if verdict == "ok" else 0
            out.checks.append(Check("candidate_domain_dns", "completed", f"resolves; {verdict}; identity {score}", {"domain": dom}))
            if score >= 0.6 and dom not in {m[0] for m in matches}:
                matches.append((dom, score, res))

        # 6) Decide
        distinct = {m[0] for m in matches}
        if len(distinct) > 1:
            out.status, out.reason, out.confidence = "CONFLICTING_EVIDENCE", "multiple_sites_match_identity", 0.4
            out.evidence.extend({"source_type": "http_probe", "source_ref": m[2].final_url, "excerpt": f"identity {m[1]}"} for m in matches)
            return out
        if matches:
            host, score, res = matches[0]
            health, sig = quality_signals(res)
            out.status, out.site_health, out.url, out.final_url = "VERIFIED_HAS_WEBSITE", health, res.final_url, res.final_url
            out.evidence.append({"source_type": "http_probe", "source_ref": res.final_url, "excerpt": _title(res.body), "signals": sig})
            out.identity_facts += extract_identity(c, _visible(res.body), res.final_url, "official_website")
            out.confidence, out.reason = round(min(1.0, 0.5 + score * 0.5), 2), "found_via_independent_search"
            if c.listed_url and any(ch.check == "listed_site_identity" for ch in out.checks):
                out.reason = "listed_url_mismatch_official_site_found"
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
            out.status, out.reason = "VERIFIED_NO_WEBSITE", "all_required_checks_completed_no_site_found"
        else:
            out.status, out.reason = "WEBSITE_UNCERTAIN", "insufficient_evidence"
        return out


async def persist_outcome(db, *, workspace_id, prospect_id, task_id, out: Outcome) -> list:
    """Writes evidence + verification record. Never writes Google-derived values."""
    async with db.transaction() as conn:
        ev_ids = []
        for e in out.evidence[:40]:
            ev_ids.append(await conn.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt)
                   values ($1,$2,$3,$4,$5,$6) returning id""",
                workspace_id, prospect_id, e["source_type"], e["source_ref"][:2000],
                "days_180" if e["source_type"] == "serp_result" else "indefinite", (e.get("excerpt") or None)))
        seen: set[tuple] = set()
        for f in out.identity_facts:
            key = (f["fact_key"], f["value"])
            if key in seen:
                continue
            seen.add(key)
            eid = await conn.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt)
                   values ($1,$2,$3,$4,'indefinite',$5) returning id""",
                workspace_id, prospect_id, f["source_type"], f["source_ref"][:2000], f["quote"])
            await conn.execute(
                """insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                   values ($1,$2,$3,$4,'verified',$5,$6,0.9)""",
                workspace_id, prospect_id, f["fact_key"], f["value"], eid, f["quote"])
        await conn.execute(
            """insert into website_verifications(workspace_id, prospect_id, task_id, status, site_health, reason, url,
                   final_url, listing_sources, checks, evidence_ids, confidence)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
            workspace_id, prospect_id, task_id, out.status, out.site_health, out.reason, out.url, out.final_url,
            out.listing_sources, [c.__dict__ for c in out.checks], ev_ids, out.confidence)
        return ev_ids


def satisfies_filter(status: str, health: str | None, website_filter: list[str]) -> bool:
    if "ANY" in website_filter:
        return status in ("VERIFIED_HAS_WEBSITE", "VERIFIED_NO_WEBSITE")
    ok = False
    if "NO_WEBSITE" in website_filter:
        ok |= status == "VERIFIED_NO_WEBSITE"
    if "WEBSITE_BROKEN" in website_filter:
        ok |= status == "VERIFIED_HAS_WEBSITE" and health == "broken"
    if "WEBSITE_WORKING_OUTDATED" in website_filter:
        ok |= status == "VERIFIED_HAS_WEBSITE" and health == "outdated"
    return ok


_ = IntegrationState
