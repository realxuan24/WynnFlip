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

    # V17.2 unified board: "promising" is a lower-evidence RESEARCH lead,
    # not a weakening of the qualified/high-confidence requirements.
    promising_reasons = []
    if item.get("low_value_filtered"):
        promising_reasons.append("Low-value/bulk item")
    if age is None or not math.isfinite(age) or age < 0 or age > 15:
        promising_reasons.append("Observation older than 15 minutes or unverifiable")
    if source not in ("roll_comparables", "unid_comparables") or buy is None:
        promising_reasons.append("No comparable-based valuation")
    if comparable_count < 3:
        promising_reasons.append("Fewer than three comparable offers")
    if sellability.get("label") in ("Unknown / thin market", "High resale risk") or risk == "Higher resale risk":
        promising_reasons.append("Insufficient market activity or elevated risk")
    if int(_positive(sellability.get("cheaper_asks")) or 0) > 2:
        promising_reasons.append("Too many cheaper competing asks")

    promising_reference = reference
    if source == "roll_comparables":
        intel = item.get("v17_roll_intel") or {}
        matched = intel.get("matched_asking_range_e") or {}
        low = _positive(matched.get("low"))
        if int(_positive(intel.get("matched_offer_count")) or 0) < 3 or low is None:
            promising_reasons.append("Fewer than three similar-roll offers")
        if reference and low:
            promising_reference = min(reference, low)
            if reference > low * 2:
                promising_reasons.append("Model resale ask differs too much from roll-matched market")
    elif source == "unid_comparables":
        if item.get("identification_status") != "unidentified":
            promising_reasons.append("UNID status mismatch")
        if reference and buy and reference * qty / buy > 4:
            promising_reasons.append("Extreme UNID price gap")

    if buy and promising_reference:
        buyer_inclusive = item.get("price_assumption") == "buyer_inclusive"
        promising_take_home = round(promising_reference * qty / 1.05) if buyer_inclusive else round(promising_reference * qty)
        promising_net = promising_take_home - buy
        promising_roi = 100.0 * promising_net / buy
    else:
        promising_net, promising_roi = None, None
    if promising_net is None or promising_net < 4 * LE or promising_roi is None or promising_roi < 20:
        promising_reasons.append("Conservative profit below 4 LE or ROI below 20%")

    tier = "high_confidence" if not reasons else ("promising" if not promising_reasons else "research")
    displayed_reference = caution_reference if tier == "high_confidence" else promising_reference if tier == "promising" else None
    displayed_net = net if tier == "high_confidence" else promising_net if tier == "promising" else None
    displayed_roi = roi if tier == "high_confidence" else promising_roi if tier == "promising" else None
    item["v172_qualification"] = {
        "status": "qualified" if tier == "high_confidence" else "promising" if tier == "promising" else "research_only",
        "tier": tier,
        "reasons": reasons if tier == "research" else [] if tier == "high_confidence" else promising_reasons,
        "comparable_count": comparable_count,
        "conservative_reference_e": round(displayed_reference) if displayed_reference else None,
        "conservative_net_profit_e": round(displayed_net) if displayed_net is not None else None,
        "conservative_roi_pct": round(displayed_roi, 1) if displayed_roi is not None else None,
        "note": "Observed asking-price comparison, not a confirmed sale, guaranteed profit, or confirmed availability.",
    }
    return item


def qualify_items(items):
    for item in items:
        qualify_item(item)
    return sum(x["v172_qualification"]["status"] == "qualified" for x in items)
