"""robots.txt evaluation before any crawl/scrape. robots.txt is a technical signal, not a substitute
for legal/terms review (see docs/SECURITY_AND_COMPLIANCE.md). Unreachable robots.txt (5xx/timeouts)
fails closed for crawling; a 404 means no restrictions (RFC 9309)."""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from jarvis.security.ssrf import USER_AGENT, safe_fetch

# Sites whose terms forbid automated access for our purposes (operator-reviewable list).
TERMS_BLOCKLIST = {"facebook.com", "instagram.com", "linkedin.com", "yelp.com", "nextdoor.com", "google.com",
                   "maps.google.com", "x.com", "twitter.com", "tiktok.com"}


def registrable_domain(host: str) -> str:
    parts = host.lower().rstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def terms_blocked(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return registrable_domain(host) in TERMS_BLOCKLIST


async def robots_allows(url: str, fetch=safe_fetch) -> tuple[bool, str]:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    res = await fetch(robots_url)
    if res.error or res.status is None:
        return False, f"robots.txt unreachable ({res.error}); failing closed"
    if res.status in (401, 403):
        return False, f"robots.txt returned {res.status}; failing closed"
    if res.status >= 500:
        return False, f"robots.txt returned {res.status}; failing closed"
    if res.status >= 400:
        return True, "no robots.txt (4xx): no restrictions"
    rp = RobotFileParser()
    rp.parse(res.body.splitlines())
    ok = rp.can_fetch(USER_AGENT.split("/")[0], url) and rp.can_fetch("*", url)
    return ok, "allowed by robots.txt" if ok else "disallowed by robots.txt"
