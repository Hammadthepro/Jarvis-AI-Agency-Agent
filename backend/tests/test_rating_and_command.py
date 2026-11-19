"""T1-T7: rating parsing/validation; command parsing fixtures from docs/API_CONTRACTS.md §2."""
from decimal import Decimal

import pytest

from jarvis.agents.command import CampaignParameters, apply_answers, deterministic_parse
from jarvis.core.errors import ValidationFailed
from jarvis.integrations.google_places import (
    DISCOVERY_ENTERPRISE,
    DISCOVERY_ENTERPRISE_WITH_RATING,
    DISCOVERY_PRO,
    IDS_ONLY,
    LISTING_DISPLAY,
    mask_sku,
    validate_min_rating,
)


def fields(r):
    return [q["field"] for q in r.questions]


def test_t1_min_rating_35():
    r = deterministic_parse("Find 100 plumbers in Houston with at least a 3.5 Google rating and no website.")
    assert r.draft["minimum_google_rating"] == "3.5" and r.draft["website_filter"] == ["NO_WEBSITE"]
    assert r.draft["location"] == {"city": "Houston", "state": "TX"} and r.draft["niche"] == "plumbing"
    assert fields(r) == ["missing_rating_policy"]          # policy must be chosen explicitly
    done = apply_answers(r.draft, {"missing_rating_policy": "exclude"})
    assert done.parameters and done.parameters.minimum_google_rating == Decimal("3.5")


def test_t2_min_rating_40_and_missing_count():
    r = deterministic_parse("Find dentists in Dallas with a minimum Google rating of 4.0.")
    assert r.draft["minimum_google_rating"] == "4.0"
    assert "target_count" in fields(r)


def test_t2b_rated_45_missing_niche_location():
    r = deterministic_parse("Find 50 businesses rated 4.5 or higher.")
    assert r.draft["minimum_google_rating"] == "4.5" and {"niche", "location"} <= set(fields(r))


@pytest.mark.parametrize("text", ["Find 20 roofers in Denver rated at least 3.7", "Find 20 roofers in Denver with a rating of 4.25"])
def test_t6_non_half_steps_ask_never_round(text):
    r = deterministic_parse(text)
    assert "minimum_google_rating" not in r.draft and "minimum_google_rating" in fields(r) and r.parameters is None


@pytest.mark.parametrize("text", ["Find 10 bakeries in Atlanta with a rating of 6", "Find 10 bakeries in Atlanta with a rating of 0",
                                  "Find 10 bakeries in Atlanta with a rating of -1", "Find 10 bakeries in Atlanta rated four-ish good"])
def test_t6_invalid_thresholds(text):
    r = deterministic_parse(text)
    assert "minimum_google_rating" not in r.draft and r.parameters is None


def test_t6_above_is_ambiguous():
    r = deterministic_parse("Find 10 bakeries in Atlanta with a rating above 4")
    q = next(q for q in r.questions if q["field"] == "minimum_google_rating")
    assert q["options"] == [4.0, 4.5]


@pytest.mark.parametrize("v", ["3.7", 4.25, 0, "0.0", -1, 5.5, 6, "abc"])
def test_t6_validator_rejects(v):
    with pytest.raises(ValidationFailed):
        validate_min_rating(v)


def test_t7_no_default_rating():
    r = deterministic_parse("Find 30 electricians in Austin")
    assert r.parameters and r.parameters.minimum_google_rating is None and r.parameters.missing_rating_policy is None


def test_t5_policy_required_and_only_with_rating():
    p = CampaignParameters(niche="dentist", location={"city": "Dallas", "state": "TX"}, target_count=5, minimum_google_rating="4.0")
    with pytest.raises(ValidationFailed):
        p.require_complete()
    with pytest.raises(ValueError):
        CampaignParameters(niche="dentist", location={"city": "Dallas"}, target_count=5, missing_rating_policy="exclude")
    with pytest.raises(ValueError):
        CampaignParameters(niche="dentist", location={"city": "Dallas"}, target_count=5, minimum_google_rating="4.0",
                           missing_rating_policy="exclude", discovery_provider="manual_import")


def test_t16_mask_skus():
    assert mask_sku(DISCOVERY_ENTERPRISE) == "text_search_enterprise"
    assert mask_sku(DISCOVERY_ENTERPRISE_WITH_RATING) == "text_search_enterprise"
    assert mask_sku(DISCOVERY_PRO) == "text_search_pro"
    assert mask_sku(IDS_ONLY) == "text_search_ids_only"
    assert mask_sku(LISTING_DISPLAY, "details") == "place_details_enterprise"
    assert "places.attributions" in DISCOVERY_ENTERPRISE and "attributions" in LISTING_DISPLAY
    assert "places.rating" not in DISCOVERY_ENTERPRISE


def test_preview_count_phrasings_enable_build_stages():
    from jarvis.agents.command import ALL_STAGES, deterministic_parse
    for text, n in (("Find 10 independent roofing companies in Houston with outdated websites and prepare two redesign previews.", 2),
                    ("find 5 dentists in Austin and build 3 preview websites", 3),
                    ("Find 10 roofers in Houston, TX with outdated websites and build sites for the top 2", 2)):
        d = deterministic_parse(text).draft
        assert d["sites_to_build"] == n and d["stages"] == ALL_STAGES, text
    assert "sites_to_build" not in deterministic_parse("Find 10 roofers in Houston").draft
