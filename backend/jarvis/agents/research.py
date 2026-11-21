"""Agent 5: Research. Real search (SerpApi) + extraction (Firecrawl) + Haiku fact extraction.

Every fact must carry an EXACT quote that appears in the fetched source text; the deterministic
quote verifier drops anything that doesn't. Conflicting values for single-valued keys are marked
`conflicting`. Contacts from pages that prohibit automated address collection are flagged and
never used for outreach."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from jarvis.agents.deps import Deps
from jarvis.core.errors import ProviderError
from jarvis.integrations.base import CallContext
from jarvis.security.robots import robots_allows, terms_blocked

EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["fact_key", "value", "quote"],
        "properties": {
            "fact_key": {"enum": ["business_name", "service", "service_area", "description", "hours", "phone",
                                  "email", "street_address", "licence_number", "year_established", "brand_color",
                                  "tagline", "specialty"]},
            "value": {"type": "string"},
            "quote": {"type": "string", "description": "verbatim substring of the page text supporting the value"}}}}},
}
EXTRACT_SYSTEM = (
    "You extract facts about ONE local business from the text of a web page about it. Return only facts "
    "explicitly stated in the text, each with a verbatim quote copied exactly from the text. Never infer, "
    "embellish, or add reviews, ratings, awards or claims that are not in the text. If the page is not about "
    "the business, return an empty list. Prompt version: research-extract-v1.")
SINGLE_VALUED = {"business_name", "phone", "street_address", "year_established", "licence_number"}
HARVEST_MARKERS = ("do not harvest", "no unsolicited", "not be used for unsolicited", "harvesting of email",
                   "unsolicited commercial email", "do not send unsolicited")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quotes(facts: list[dict], source_text: str) -> list[dict]:
    hay = _norm(source_text)
    kept = []
    for f in facts:
        q = _norm(f.get("quote", ""))
        if len(q) >= 3 and q in hay and _norm(f.get("value", ""))[:200]:
            kept.append(f)
    return kept


def candidate_subpages(base_url: str, markdown: str, limit: int) -> list[str]:
    links = re.findall(r"\]\((https?://[^)\s]+|/[^)\s]*)\)", markdown)
    host = urlsplit(base_url).hostname
    out: list[str] = []
    for href in links:
        u = urljoin(base_url, href).split("#")[0]
        if urlsplit(u).hostname != host or u.rstrip("/") == base_url.rstrip("/"):
            continue
        if re.search(r"(about|service|contact|area|what-we-do|our-work|team)", u, re.I) and u not in out:
            out.append(u)
    return out[:limit]


class Researcher:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def _pages_for(self, prospect_id: UUID) -> list[tuple[str, str]]:
        rows = await self.deps.db.fetch(
            """select status, site_health, final_url from website_verifications where prospect_id = $1
               order by checked_at desc limit 1""", prospect_id)
        urls: list[tuple[str, str]] = []
        if rows and rows[0]["status"] == "VERIFIED_HAS_WEBSITE" and rows[0]["site_health"] != "broken" and rows[0]["final_url"]:
            urls.append((rows[0]["final_url"], "official_website"))
        prof = await self.deps.db.fetch(
            """select distinct source_ref from evidence where prospect_id = $1 and source_type in ('directory_page','social_page')
               limit 3""", prospect_id)
        urls += [(r["source_ref"], "directory_page") for r in prof]
        return urls

    async def research(self, ctx: CallContext, prospect_id: UUID) -> dict:
        s = self.deps.settings
        pages = await self._pages_for(prospect_id)
        summary = {"pages_scraped": 0, "facts_kept": 0, "facts_dropped": 0, "skipped": []}
        budget_pages = s.firecrawl_max_pages_per_prospect
        queue = list(pages)
        seen: set[str] = set()
        while queue and summary["pages_scraped"] < budget_pages:
            url, kind = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if terms_blocked(url):
                summary["skipped"].append({"url": url, "why": "terms_blocked"})
                continue
            allowed, why = await robots_allows(url, fetch=self.deps.fetch_url)
            if not allowed:
                summary["skipped"].append({"url": url, "why": why})
                continue
            try:
                page = await self.deps.firecrawl.scrape(ctx, url)
            except ProviderError as exc:
                summary["skipped"].append({"url": url, "why": exc.state.value})
                if exc.state.value in ("NOT_CONFIGURED", "QUOTA_EXCEEDED", "INVALID_CREDENTIALS", "DISABLED"):
                    raise
                continue
            summary["pages_scraped"] += 1
            text = page.markdown
            ev_id = await self.deps.db.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt)
                   values ($1,$2,'firecrawl_page',$3,'days_90',$4) returning id""",
                ctx.workspace_id, prospect_id, page.url, text[:20000])
            if kind == "official_website":
                for sub in candidate_subpages(page.url, text, budget_pages):
                    queue.append((sub, "official_website"))
            await self._extract(ctx, prospect_id, ev_id, page.url, text, summary)
        await self._mark_conflicts(prospect_id)
        return summary

    async def _extract(self, ctx: CallContext, prospect_id: UUID, ev_id: UUID, url: str, text: str, summary: dict) -> None:
        res = await self.deps.claude.structured(
            ctx, model=self.deps.settings.claude_model_fast, system=EXTRACT_SYSTEM,
            user=f"Page URL: {url}\n\n<page_text>\n{text[:24000]}\n</page_text>", schema=EXTRACT_SCHEMA,
            max_tokens=2000, operation="research.extract")
        facts = res.data.get("facts", [])
        kept = verify_quotes(facts, text)
        summary["facts_kept"] += len(kept)
        summary["facts_dropped"] += len(facts) - len(kept)
        prohibited = any(m in text.lower() for m in HARVEST_MARKERS)
        async with self.deps.db.transaction() as conn:
            for f in kept:
                await conn.execute(
                    """insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                       values ($1,$2,$3,$4,'verified',$5,$6,0.85)""",
                    ctx.workspace_id, prospect_id, f["fact_key"], f["value"][:500], ev_id, f["quote"][:1000])
            for email in sorted(set(EMAIL_RE.findall(text)))[:5]:
                await conn.execute(
                    """insert into contacts(workspace_id, prospect_id, contact_type, value, evidence_id, is_public_business_contact,
                                            harvesting_prohibited, verification)
                       values ($1,$2,'email',$3,$4,$5,$6,'verified') on conflict do nothing""",
                    ctx.workspace_id, prospect_id, email.lower(), ev_id, not prohibited, prohibited)

    async def _mark_conflicts(self, prospect_id: UUID) -> None:
        rows = await self.deps.db.fetch(
            """select fact_key, array_agg(distinct lower(value #>> '{}')) vals from research_facts
               where prospect_id = $1 and status = 'verified' and fact_key = any($2) group by fact_key""",
            prospect_id, list(SINGLE_VALUED))
        for r in rows:
            vals = {same_value_key(r["fact_key"], v) for v in r["vals"]}
            if len(vals) > 1:
                await self.deps.db.execute(
                    "update research_facts set status = 'conflicting' where prospect_id = $1 and fact_key = $2",
                    prospect_id, r["fact_key"])


def same_value_key(fact_key: str, value: str) -> str:
    """Comparison key for a single-valued fact. Phone numbers compare as the 10-digit US number, so "(615) 555-0121"
    and "+16155550121" are the same fact, not a conflict (a false conflict would drop the phone from the preview)."""
    if fact_key == "phone":
        digits = re.sub(r"\D", "", value)
        return digits[-10:] if len(digits) >= 10 else digits
    return re.sub(r"\W", "", value)
