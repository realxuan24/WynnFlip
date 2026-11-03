"""Regression tests for conservative V17.2 flip qualification."""
from copy import deepcopy
from qualification import qualify_item, qualify_items

def candidate():
    return {
        "name": "Test Mythic", "quantity": 1, "price_e": 40960,
        "purchase_total_e": 40960, "listing_age_minutes": 2,
        "low_value_filtered": False, "valuation_source": "roll_comparables",
        "identification_status": "identified", "comparables": 7,
        "resale_estimate_e": 120000, "price_assumption": "buyer_inclusive",
        "sellability": {"label": "Uncertain demand", "cheaper_asks": 0},
        "v161_liquidity": {"label": "Some liquidity evidence"},
        "v17_roll_intel": {
            "status": "matched_asking_evidence",
            "method": "community-weighted roll similarity",
            "community_profile": "test-profile", "matched_offer_count": 7,
            "matched_asking_range_e": {"low": 110000, "median": 120000, "high": 125000},
        },
    }

def test_strong_candidate_qualifies():
    x = qualify_item(candidate())
    q = x["v172_qualification"]
    assert q["status"] == "qualified"
    assert q["conservative_reference_e"] == 110000
    assert q["conservative_net_profit_e"] == round(110000 / 1.05) - 40960

def test_baseline_only_never_qualifies():
    x = candidate()
    x["valuation_source"] = "item_baseline_not_roll_adjusted"
    assert qualify_item(x)["v172_qualification"]["status"] == "research_only"

def test_unweighted_rolls_never_qualify():
    x = candidate()
    x["v17_roll_intel"]["method"] = "unweighted roll similarity"
    assert qualify_item(x)["v172_qualification"]["status"] == "promising"

def test_risky_or_stale_listings_never_qualify():
    x = candidate()
    x["listing_age_minutes"] = 16
    x["sellability"]["cheaper_asks"] = 3
    x["v161_liquidity"]["label"] = "Higher resale risk"
    assert qualify_item(x)["v172_qualification"]["status"] == "research_only"

def test_insufficient_evidence_never_qualifies():
    x = candidate()
    x["comparables"] = 2
    x["v17_roll_intel"]["matched_offer_count"] = 2
    assert qualify_item(x)["v172_qualification"]["status"] == "research_only"

def test_minimum_net_profit_and_roi():
    x = candidate()
    x["purchase_total_e"] = 82000
    assert qualify_item(x)["v172_qualification"]["status"] == "promising"
    x = candidate()
    x["resale_estimate_e"] = 55000
    assert qualify_item(x)["v172_qualification"]["status"] == "research_only"

def test_unid_status_match_and_profitability():
    x = candidate()
    x["identification_status"] = "unidentified"
    x["valuation_source"] = "unid_comparables"
    x["comparables"] = 6
    assert qualify_item(x)["v172_qualification"]["status"] == "qualified"
    x["identification_status"] = "identified"
    assert qualify_item(x)["v172_qualification"]["status"] == "research_only"

def test_qualification_count():
    strong = candidate()
    weak = deepcopy(strong)
    weak["listing_age_minutes"] = 70
    assert qualify_items([strong, weak]) == 1

def test_three_similar_roll_offers_are_promising():
    x = candidate()
    x["comparables"] = 3
    x["listing_age_minutes"] = 3
    x["v17_roll_intel"].update(status="thin_matching_evidence", matched_offer_count=3)
    q = qualify_item(x)["v172_qualification"]
    assert q["tier"] == "promising"
    assert q["conservative_reference_e"] == 110000

def test_unmatched_rolls_remain_research_only():
    x = candidate()
    x["v17_roll_intel"].update(
        status="insufficient_roll_evidence", matched_offer_count=0,
        matched_asking_range_e=None,
    )
    assert qualify_item(x)["v172_qualification"]["tier"] == "research"

def test_three_level_classification():
    high = candidate()
    promising = candidate()
    promising["comparables"] = 3
    promising["v17_roll_intel"].update(status="thin_matching_evidence", matched_offer_count=3)
    research = candidate()
    research["valuation_source"] = "item_baseline_not_roll_adjusted"
    qualify_items([high, promising, research])
    assert [x["v172_qualification"]["tier"] for x in (high, promising, research)] == [
        "high_confidence", "promising", "research",
    ]

def test_stale_unid_shattershard_not_promising():
    x = candidate()
    x.update(name="Shattershard", identification_status="unidentified",
             valuation_source="unid_comparables", comparables=4,
             listing_age_minutes=12, purchase_total_e=275251,
             resale_estimate_e=370800)
    assert qualify_item(x)["v172_qualification"]["tier"] == "research"

def test_negative_cautious_scenario_blocks_promising():
    x = candidate()
    x.update(identification_status="unidentified",
             valuation_source="unid_comparables", comparables=4)
    x["v16_scenarios"]={"supported":True,"net_profit_low_e":-10394}
    assert qualify_item(x)["v172_qualification"]["tier"] == "research"

def test_positive_cautious_scenario_can_be_promising():
    x = candidate()
    x.update(identification_status="unidentified",
             valuation_source="unid_comparables", comparables=4)
    x["v16_scenarios"]={"supported":True,"net_profit_low_e":5000}
    assert qualify_item(x)["v172_qualification"]["tier"] == "promising"
