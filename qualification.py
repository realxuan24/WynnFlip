"""V17.2: conservative opportunity qualification.

All references are independent observed ASKING prices, never completed sales.
Do not promote historical baselines, weak roll matches, or unknown demand into
a guaranteed flip. This module does not mutate market history.
"""
import math

LE = 4096
MIN_NET_PROFIT_E = 8 * LE
MIN_ROI_PCT = 25.0
MIN_COMPARABLES = 5
MAX_AGE_MINUTES = 5.0


def _positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def qualify_item(item):
    """Annotate an already-priced item; return it for compatibility."""
    reasons = []
    def reject(condition, reason):
        if condition:
            reasons.append(reason)

    buy = _positive(item.get("purchase_total_e"))
    qty = int(_positive(item.get("quantity")) or 1)
    age = item.get("listing_age_minutes")
    try:
        age = float(age) if age is not None else None
    except (TypeError, ValueError):
        age = None

    reject(item.get("low_value_filtered"), "Low-value or bulk item")
    reject(age is None or not math.isfinite(age) or age < 0 or age > MAX_AGE_MINUTES,
           "Listing observation is older than five minutes or unverified")
    reject(buy is None, "Missing purchase cost")
    source = item.get("valuation_source")
    reject(source not in ("roll_comparables", "unid_comparables"),
           "No roll/UNID-matched asking-price valuation")

    comparable_count = int(_positive(item.get("comparables")) or 0)
    reject(comparable_count < MIN_COMPARABLES,
           "Fewer than five independently comparable asking offers")

    sellability = item.get("sellability") or {}
    risk = (item.get("v161_liquidity") or {}).get("label")
    reject(sellability.get("label") in ("Unknown / thin market", "High resale risk"),
           "Thin market or elevated resale risk")
    reject(risk == "Higher resale risk", "Lifecycle evidence signals higher resale risk")
    reject(int(_positive(sellability.get("cheaper_asks")) or 0) > 1,
           "Too many cheaper competing asks")

    reference = _positive(item.get("resale_estimate_e"))
    caution_reference = reference
    if source == "roll_comparables":
        intel = item.get("v17_roll_intel") or {}
        matching = intel.get("matched_asking_range_e") or {}
        low = _positive(matching.get("low"))
        reject(int(_positive(intel.get("matched_offer_count")) or 0) < MIN_COMPARABLES
               or intel.get("status") != "matched_asking_evidence" or low is None,
               "Fewer than five recent comparable-roll asking offers")
        reject(intel.get("method") != "community-weighted roll similarity"
               or not intel.get("community_profile"),
               "No community-weighted important-ID evidence")
        if reference and low:
            # Avoid a model reference far above observed comparable asks.
            reject(reference > low * 1.2,
                   "Resale reference exceeds conservative matched-roll evidence")
            caution_reference = min(reference, low)
    elif source == "unid_comparables":
        # UNIDs do not have roll desirability; require enough same-status
        # independent offers and refuse extreme apparent mispricings.
        reject(item.get("identification_status") != "unidentified",
               "Identification status does not match UNID reference")
        if reference and buy:
            reject(reference * qty / buy > 4,
                   "Extraordinary UNID price gap needs direct verification")

    if buy and caution_reference:
        buyer_inclusive = item.get("price_assumption") == "buyer_inclusive"
        take_home = round(caution_reference * qty / 1.05) if buyer_inclusive else round(caution_reference * qty)
        net = take_home - buy
        roi = 100.0 * net / buy
    else:
        net, roi = None, None
    reject(net is None or net < MIN_NET_PROFIT_E, "Conservative spread below 8 LE")
    reject(roi is None or roi < MIN_ROI_PCT, "Conservative return below 25%")

    item["v172_qualification"] = {
        "status": "qualified" if not reasons else "research_only",
        "reasons": reasons,
        "comparable_count": comparable_count,
        "conservative_reference_e": round(caution_reference) if caution_reference else None,
        "conservative_net_profit_e": round(net) if net is not None else None,
        "conservative_roi_pct": round(roi, 1) if roi is not None else None,
        "note": "Evidence-screened opportunity, not a verified sale, guaranteed profit, or confirmed availability.",
    }
    return item


def qualify_items(items):
    for item in items:
        qualify_item(item)
    return sum(x["v172_qualification"]["status"] == "qualified" for x in items)
