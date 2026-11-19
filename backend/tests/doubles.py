"""TEST DOUBLES for external providers. Used only by the automated test suite (JARVIS_ENV=test).
They emulate provider HTTP contracts (Google Places API (New), SerpApi, Firecrawl v2, Cloudflare,
Resend) and the Claude SDK surface, so the real adapters, gateway, budget engine and workflow run
unmodified. Nothing here is importable by production code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx

from jarvis.security.ssrf import DnsFailure, FetchResult


@dataclass
class Site:
    status: int = 200
    body: str = ""
    headers: dict = field(default_factory=lambda: {"content-type": "text/html"})
    error: str | None = None


class FakeWeb:
    def __init__(self) -> None:
        self.sites: dict[str, Site] = {}          # url (no trailing slash) -> Site
        self.dns: set[str] = set()               # hostnames that resolve
        self.places_pages: list[dict] = []       # Text Search responses served in order
        self.serp: dict[str, dict] = {}          # query substring -> serpapi json
        self.serp_error: str | None = None
        self.firecrawl_pages: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.places_requests: list[dict] = []
        self.deploy_runs: list[tuple[str, str]] = []
        self.resend_mode = "ok"                   # ok | timeout
        self.sent: list[dict] = []

    # --- direct-fetch double (replaces safe_fetch; the SSRF guard has its own tests) ---
    async def fetch(self, url: str, **_kw) -> FetchResult:
        key = url.rstrip("/")
        host = httpx.URL(url).host
        self.calls.append(("fetch", url))
        if key in self.sites:
            s = self.sites[key]
            if s.error:
                return FetchResult(url, url, None, {}, "", error=s.error)
            return FetchResult(url, url, s.status, s.headers, s.body)
        if host not in self.dns:
            return FetchResult(url, url, None, {}, "", error="nxdomain")
        return FetchResult(url, url, 404, {}, "not found")

    async def resolve(self, host: str, port: int) -> str:
        if host in self.dns:
            return "93.184.216.34"
        raise DnsFailure(host, "NXDOMAIN")

    async def wrangler_runner(self, directory, branch):
        self.deploy_runs.append((str(directory), branch))
        url = f"https://{branch}.jarvis-previews.pages.dev"
        body = (directory / "index.html").read_text()
        self.sites[url] = Site(200, body, {"content-type": "text/html", "x-robots-tag": "noindex, nofollow"})
        return 0, f"✨ Deployment complete! Take a peek over at {url}", ""

    # --- provider HTTP endpoints ---
    def transport(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            u = req.url
            self.calls.append((req.method, str(u)))
            if u.host == "places.googleapis.com":
                body = json.loads(req.content or b"{}")
                self.places_requests.append({"mask": req.headers.get("x-goog-fieldmask"), "body": body, "path": u.path})
                if u.path.endswith(":searchText"):
                    page = self.places_pages.pop(0) if self.places_pages else {"places": []}
                    return httpx.Response(200, json=page)
                return httpx.Response(200, json={"id": u.path.rsplit("/", 1)[-1], "displayName": {"text": "Listing"},
                                                 "rating": 4.4, "userRatingCount": 31, "googleMapsUri": "https://maps.google.com/?cid=1"})
            if u.host == "serpapi.com":
                if u.path.startswith("/account"):
                    return httpx.Response(200, json={"plan_name": "Free", "this_month_usage": 3, "searches_per_month": 250})
                if self.serp_error:
                    return httpx.Response(429 if "run out" in self.serp_error else 200, json={"error": self.serp_error})
                q = u.params.get("q", "")
                for k, v in self.serp.items():
                    if k in q:
                        return httpx.Response(200, json={"search_metadata": {"id": "s1", "status": "Success"}, **v})
                return httpx.Response(200, json={"search_metadata": {"id": "s0", "status": "Success"},
                                                 "error": "Google hasn't returned any results for this query."})
            if u.host == "api.firecrawl.dev":
                if u.path.endswith("/team/credit-usage"):
                    return httpx.Response(200, json={"success": True, "data": {"remaining_credits": 940, "plan_credits": 1000}})
                target = json.loads(req.content)["url"]
                md = self.firecrawl_pages.get(target.rstrip("/"))
                if md is None:
                    return httpx.Response(200, json={"success": False, "error": "not found"})
                return httpx.Response(200, json={"success": True, "data": {"markdown": md, "metadata": {"sourceURL": target, "statusCode": 200}}})
            if u.host == "api.cloudflare.com":
                if u.path.endswith("/deployments") and req.method == "GET":
                    runs = [{"id": f"dep-{i}", "url": f"https://{b}.jarvis-previews.pages.dev", "deployment_trigger": {"metadata": {"branch": b}}}
                            for i, (_d, b) in enumerate(reversed(self.deploy_runs))]
                    return httpx.Response(200, json={"result": runs})
                return httpx.Response(200, json={"result": {}, "success": True})
            if u.host == "api.resend.com":
                if u.path == "/domains":
                    return httpx.Response(200, json={"data": [{"name": "agency.example", "status": "verified"}]})
                if self.resend_mode == "timeout":
                    raise httpx.ReadTimeout("timed out", request=req)
                self.sent.append(json.loads(req.content))
                return httpx.Response(200, json={"id": f"msg-{len(self.sent)}"})
            return httpx.Response(404, json={"error": "unknown host in test double"})
        return httpx.MockTransport(handler)


class FakeAnthropic:
    """Implements the subset of the AsyncAnthropic surface used by ClaudeClient."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responders: list = []
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    async def _retrieve(self, model):
        return SimpleNamespace(id=model, display_name=model)

    async def _create(self, **kw):
        self.calls.append(kw)
        data = None
        for r in self.responders:
            data = r(kw)
            if data is not None:
                break
        if data is None:
            data = default_response(kw)
        if isinstance(data, SimpleNamespace):          # a prebuilt message (e.g. tool use) from a responder
            return data
        text = json.dumps(data)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", model=kw["model"],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=400))


def _facts_from(user: str) -> list[tuple[str, str, str]]:
    return re.findall(r"fact_id=(\S+) \[(\w+)\] (.+)", user)


def default_response(kw: dict) -> dict:
    system, user = kw["system"], kw["messages"][0]["content"]
    if "research-extract" in system:
        text = user.split("<page_text>")[1].split("</page_text>")[0]
        facts = []
        for line in text.splitlines():
            m = re.match(r"\s*[-*]\s*(.+)", line)
            if m and len(m.group(1)) < 80:
                facts.append({"fact_key": "service", "value": m.group(1).strip(), "quote": m.group(1).strip()})
        m = re.search(r"Serving ([A-Z][^.\n]+)", text)
        if m:
            facts.append({"fact_key": "service_area", "value": m.group(1), "quote": m.group(0)})
        facts.append({"fact_key": "service", "value": "Invented service", "quote": "this quote is not on the page"})
        return {"facts": facts}
    if "sitespec" in system:
        facts = _facts_from(user)
        name = re.search(r"Business name \(verified\): (.+)", user).group(1)
        niche = re.search(r"Business niche: (.+)", user).group(1)
        services = [{"name": v[:40], "description": f"{v} for local customers.", "fact_id": fid} for fid, k, v in facts if k == "service"]
        area = next((v for _f, k, v in facts if k == "service_area"), None)
        tone = ["plain-spoken", "warm", "precise", "confident"][len(name) % 4]
        return {"strategy": {"audience": f"Local {niche} customers", "positioning": f"{name}, a {tone} local {niche} business",
                             "conversion_plan": "Click-to-call first", "tone": tone},
                "meta_title": f"{name} | {niche.title()}"[:60], "meta_description": f"{name}: {niche} services."[:150],
                "eyebrow": niche.title(), "headline": f"{niche.title()} you can reach today"[:60],
                "subheadline": f"{name} offers " + ", ".join(s["name"].lower() for s in services[:3]) + ".",
                "primary_cta": "Call now", "services_heading": "What we do", "services": services, "about_heading": f"About {name}",
                "about": [f"{name} is a local {niche} business."], "service_area": area, "service_area_short": area,
                "faq": [{"q": "Where do you work?", "a": area}] if area else [], "cta_heading": "Talk to us", "cta_body": "Call to discuss your job."}
    if "outreach" in system:
        ids = re.findall(r"id=(\S+):", user)
        link = re.search(r"Preview link: (\S+)", user).group(1)
        return {"subject": "A website concept for your business", "body": f"Hi, we noticed something about your business. Preview: {link}",
                "observation_ids": ids[:1]}
    if "command-v1" in system:
        return {"understood": True, "niche": None, "city": None, "state": None, "target_count": None, "minimum_google_rating": None,
                "rating_phrase": None, "website_filter": [], "sites_to_build": None}
    raise AssertionError("unexpected prompt")
