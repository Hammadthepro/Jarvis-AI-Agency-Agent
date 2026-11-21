"""Qualification v2: separate dimensions, independent evidence, gates (pure scoring, no I/O)."""
from jarvis.agents.qualification import Inputs, score

STRONG = dict(website_check="NO_WEBSITE_FOUND", site_health=None, website_confidence=0.95, name_sources=2, phone_sources=2,
              independent_sources=3, has_public_email=False, has_verified_phone=True, category_matches=True,
              service_count=3, has_service_area=True, conflicting_facts=0)


def dims(res):
    return {c["rule"]: c["awarded"] for c in res["components"]}


def test_well_evidenced_prospect_qualifies_with_every_dimension_shown():
    r = score(Inputs(**STRONG))
    assert r["status"] == "qualified" and r["qualified"] and r["total"] >= 60
    assert set(dims(r)) == {"website_opportunity", "identity", "commercial_potential", "relevance", "contactability",
                            "evidence_confidence", "readiness"}


def test_the_76_point_case_is_no_longer_sales_ready():
    """The reported case: no verified services, no contact person, one source, an uncertain website failure."""
    r = score(Inputs(**{**STRONG, "website_check": "WEBSITE_BLOCKED", "website_confidence": 0.5, "name_sources": 1,
                        "phone_sources": 1, "independent_sources": 1, "service_count": 0, "has_service_area": False}))
    assert r["status"] == "needs_research" and not r["qualified"] and r["total"] <= 59
    assert dims(r)["website_opportunity"] == 0                      # an inconclusive check earns nothing
    assert any("verified services" in m for m in r["missing"]) and any("independent source" in m for m in r["missing"])


def test_high_website_opportunity_cannot_compensate_for_identity():
    r = score(Inputs(**{**STRONG, "name_sources": 0, "phone_sources": 0, "has_verified_phone": False}))
    assert dims(r)["website_opportunity"] == 100 and r["status"] == "needs_research" and r["total"] <= 59


def test_single_source_is_not_independent_confirmation():
    r = score(Inputs(**{**STRONG, "name_sources": 1, "phone_sources": 1, "independent_sources": 1}))
    assert dims(r)["identity"] == 50 and dims(r)["evidence_confidence"] < 50 and r["status"] == "needs_research"


def test_no_services_and_no_contact_are_missing_information():
    r = score(Inputs(**{**STRONG, "service_count": 0, "has_service_area": False, "has_verified_phone": False}))
    assert r["status"] == "needs_research"
    assert dims(r)["commercial_potential"] == 0 and dims(r)["contactability"] == 0
    assert any("phone number or public business email" in m for m in r["missing"])


def test_working_modern_website_is_not_a_fit_but_outdated_is_an_opportunity():
    assert score(Inputs(**{**STRONG, "website_check": "WEBSITE_WORKING", "site_health": "working"}))["status"] == "not_a_fit"
    r = score(Inputs(**{**STRONG, "website_check": "WEBSITE_WORKING", "site_health": "outdated"}))
    assert dims(r)["website_opportunity"] == 60 and r["status"] == "qualified"


def test_category_match_alone_is_not_relevance_and_wrong_category_is_not_a_fit():
    r = score(Inputs(**{**STRONG, "service_count": 0, "has_service_area": False}))
    assert dims(r)["relevance"] == 50
    assert score(Inputs(**{**STRONG, "category_matches": False}))["status"] == "not_a_fit"


def test_conflicting_information_lowers_identity():
    r = score(Inputs(**{**STRONG, "conflicting_facts": 1}))
    assert dims(r)["identity"] == 60 and any("conflicting" in m for m in r["missing"])


def test_ratings_are_not_inputs():
    assert "rating" not in " ".join(Inputs.__dataclass_fields__) and "review" not in " ".join(Inputs.__dataclass_fields__)
