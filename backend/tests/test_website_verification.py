"""Website verification: every HTTP outcome keeps its meaning. Uses the real verifier, identity scoring and robots
handling with scripted HTTP answers (SIMULATED websites; no network)."""
import pytest

from jarvis.agents import verification as v
from jarvis.agents.verification import Candidate, WebsiteVerifier, classify
from jarvis.integrations.base import CallContext
from jarvis.security.ssrf import FetchResult

PAGE = ('<html><head><meta name="viewport" content="width=device-width"><title>Lakeside Dental</title></head>'
        "<body>Lakeside Dental (714) 555-0100 Anaheim © 2026</body></html>")


class Web:
    """Scripted answers: url -> list of FetchResult (consumed in order; the last one repeats)."""

    def __init__(self):
        self.answers: dict[str, list[FetchResult]] = {}
        self.calls: list[str] = []

    def set(self, url, *results):
        self.answers[url.rstrip("/")] = list(results)

    async def fetch(self, url, **_kw):
        self.calls.append(url)
        key = url.rstrip("/")
        if key.endswith("/robots.txt") and key not in self.answers:
            return FetchResult(url, url, 404, {}, "")
        seq = self.answers.get(key)
        if not seq:
            return FetchResult(url, url, None, {}, "", error="nxdomain")
        return seq.pop(0) if len(seq) > 1 else seq[0]


def r(url, status, body="", headers=None, redirects=None, final=None, error=None):
    return FetchResult(url, final or url, status, headers or {}, body, redirects or [], error=error)


@pytest.fixture
def verifier(deps, web):
    v._ROBOTS.clear()
    w = Web()
    deps.fetch = w.fetch
    deps.second_probe_delay_s = 0
    web.serp['"Lakeside Dental"'] = {"organic_results": []}
    web.serp['"(714) 555-0100"'] = {"organic_results": []}
    return WebsiteVerifier(deps), w


def cand(url=None):
    return Candidate("Lakeside Dental", "(714) 555-0100", "Anaheim", "CA", url, "serp_api")


async def run(verifier, deps, db, url=None):
    from tests.test_core_db import new_ws
    ws, _ = await new_ws(db)
    return await verifier.verify(CallContext(db, ws), cand(url))


def test_classification_keeps_each_http_meaning():
    u = "https://x.example"
    assert classify(r(u, 200, PAGE)) == "ok"
    assert classify(r(u, 403)) == "blocked" and classify(r(u, 401)) == "auth_required" and classify(r(u, 429)) == "rate_limited"
    assert classify(r(u, 404)) == "not_found" and classify(r(u, 503)) == "http_5xx" and classify(r(u, 418)) == "http_4xx_other"
    assert classify(r(u, None, error="nxdomain")) == "nxdomain" and classify(r(u, None, error="too_many_redirects")) == "redirect_loop"
    assert classify(r(u, 200, "This domain is for sale! Buy this domain")) == "parked"


async def test_working_listed_site(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 200, PAGE))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.status == "VERIFIED_HAS_WEBSITE" and out.website_check == "WEBSITE_WORKING" and out.listing_website == "listed"
    assert out.http[0]["status"] == 200 and out.http[0]["verdict"] == "ok"


async def test_403_anti_bot_is_blocked_not_broken(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 403, "Attention Required! | Cloudflare",
                                              {"cf-ray": "1"}))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.status == "WEBSITE_UNCERTAIN" and out.website_check == "WEBSITE_BLOCKED" and out.site_health is None
    assert out.http[0]["anti_bot"] is True and out.http[0]["status"] == 403


async def test_401_is_auth_required(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 401))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.status == "WEBSITE_UNCERTAIN" and out.website_check == "WEBSITE_AUTH_REQUIRED"


async def test_429_is_rate_limited_and_a_short_retry_after_is_honoured(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 429, headers={"retry-after": "0"}),
          r("https://lakesidedental.example", 429, headers={"retry-after": "120"}))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.website_check == "WEBSITE_RATE_LIMITED" and out.site_health is None
    assert w.calls.count("https://lakesidedental.example") == 2                     # retried once, then reported
    w2 = verifier[1]
    w2.set("https://ok.example", r("https://ok.example", 429, headers={"retry-after": "0"}), r("https://ok.example", 200, PAGE))
    out2 = await run(vf, deps, db, "https://ok.example")
    assert out2.website_check == "WEBSITE_WORKING"                                  # recovered after Retry-After


async def test_two_generic_4xx_never_make_a_site_broken(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 418), r("https://lakesidedental.example", 451))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.site_health != "broken" and out.website_check != "WEBSITE_BROKEN_CONFIRMED"


async def test_404_on_a_deep_link_checks_the_homepage(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example/old-page", r("https://lakesidedental.example/old-page", 404))
    w.set("https://lakesidedental.example", r("https://lakesidedental.example/", 200, PAGE))
    out = await run(vf, deps, db, "https://lakesidedental.example/old-page")
    assert out.website_check == "WEBSITE_WORKING"


async def test_404_homepage_twice_is_confirmed_broken(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 404))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.website_check == "WEBSITE_BROKEN_CONFIRMED" and out.site_health == "broken" and "not_found, then not_found" in out.reason


async def test_5xx_that_recovers_is_not_broken_but_persistent_5xx_is(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 503), r("https://lakesidedental.example", 200, PAGE))
    assert (await run(vf, deps, db, "https://lakesidedental.example")).website_check == "WEBSITE_WORKING"
    w.set("https://down.example", r("https://down.example", 502), r("https://down.example", 503))
    out = await run(vf, deps, db, "https://down.example")
    assert out.website_check == "WEBSITE_BROKEN_CONFIRMED" and [h["status"] for h in out.http if h["check"].startswith("listed")] == [502, 503]


async def test_dns_failure_twice_is_broken_with_evidence(verifier, deps, db):
    vf, w = verifier
    out = await run(vf, deps, db, "https://gone.example")                          # unknown host: nxdomain
    assert out.website_check == "WEBSITE_BROKEN_CONFIRMED" and "nxdomain, then nxdomain" in out.reason
    assert all(h["error"] == "nxdomain" for h in out.http if h["check"].startswith("listed"))


async def test_timeouts_are_unknown_not_broken(verifier, deps, db):
    vf, w = verifier
    w.set("https://slow.example", r("https://slow.example", None, error="timeout"))
    out = await run(vf, deps, db, "https://slow.example")
    assert out.website_check == "WEBSITE_UNKNOWN" and out.site_health is None


async def test_redirect_to_another_domain_is_recorded(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 200, PAGE,
                                              redirects=["https://lakesidedental.example"], final="https://lakeside-smiles.example/"))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.website_check == "WEBSITE_REDIRECTED" and out.final_url == "https://lakeside-smiles.example/"
    assert out.http[0]["redirects"] == ["https://lakesidedental.example"]


async def test_robots_disallow_is_respected(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example/robots.txt", r("https://lakesidedental.example/robots.txt", 200, "User-agent: *\nDisallow: /"))
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 200, PAGE))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.website_check == "WEBSITE_BLOCKED" and "https://lakesidedental.example" not in w.calls    # page never fetched


async def test_no_listed_website_but_an_independent_site_is_found(verifier, deps, db, web):
    vf, w = verifier
    web.serp['"Lakeside Dental"'] = {"organic_results": [{"position": 1, "title": "Lakeside Dental", "link": "https://lakesidedental.example/"}]}
    w.set("https://lakesidedental.example", r("https://lakesidedental.example/", 200, PAGE))
    out = await run(vf, deps, db)
    assert out.listing_website == "not_listed" and out.status == "VERIFIED_HAS_WEBSITE" and out.reason == "found_via_independent_search"


async def test_a_blocked_name_matching_site_prevents_a_no_website_conclusion(verifier, deps, db, web):
    vf, w = verifier
    web.serp['"Lakeside Dental"'] = {"organic_results": [{"position": 1, "title": "Lakeside Dental", "link": "https://lakesidedental.example/"}]}
    w.set("https://lakesidedental.example", r("https://lakesidedental.example/", 403, "Access denied"))
    out = await run(vf, deps, db)
    assert out.status == "WEBSITE_UNCERTAIN" and out.website_check == "WEBSITE_BLOCKED" and out.url == "https://lakesidedental.example/"


async def test_missing_website_field_alone_is_not_no_website(verifier, deps, db, web):
    """No listed website and the independent search could not run: never 'no website'."""
    vf, w = verifier
    web.serp_error = "Internal search error"
    out = await run(vf, deps, db)
    assert out.listing_website == "not_listed" and out.status == "VERIFICATION_FAILED" and out.website_check == "WEBSITE_UNKNOWN"


async def test_incorrect_business_match_is_not_their_website(verifier, deps, db):
    vf, w = verifier
    w.set("https://lakesidedental.example", r("https://lakesidedental.example", 200,
                                              "<html><title>Riverside Auto Repair</title><body>(951) 555-0999 Riverside</body></html>"))
    out = await run(vf, deps, db, "https://lakesidedental.example")
    assert out.status != "VERIFIED_HAS_WEBSITE" and any(ch.check == "listed_site_identity" for ch in out.checks)
