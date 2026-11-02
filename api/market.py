"""WynnFlip V9: on-demand market API + static dashboard on Vercel."""
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib.parse import quote
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from scanner import build, extract_listing_page, load_profiles, number

API = "https://wynnventory.com/api/v2"
REFERENCE_URL = "https://raw.githubusercontent.com/realxuan24/WynnFlip/main/site/market.json"

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def reference_from_snapshot(payload):
    """Convert old published observations to COMPARISON-ONLY rows.

    They are NOT active listings, NOT verified sales, and are never displayed
    as fresh opportunities. Missing or malformed entries are ignored.
    """
    if not isinstance(payload, dict):
        return []
    result = []
    for item in payload.get("listings", [])[:6000]:
        if not isinstance(item, dict):
            continue
        price = number(item.get("price_e"))
        if not item.get("name") or price is None or price <= 0:
            continue
        rolls = item.get("stat_rolls")
        if not isinstance(rolls, dict):
            rolls = {}
        clean_rolls = {str(k): v for k, v in rolls.items()
                       if number(v) is not None}
        result.append({
            "name": item["name"],
            "item_type": item.get("item_type", ""),
            "subtype": item.get("subtype", ""),
            "tier": item.get("rarity", ""),
            "listing_price": round(price), "amount": 1,
            "hash_code": "reference:" + str(item.get("id") or len(result)),
            "stat_rolls": clean_rolls,
            "overall_roll": item.get("overall_roll"),
            "timestamp": item.get("listing_timestamp"),
            "unidentified": item.get("unidentified"),
            "shiny_stat": item.get("shiny_stat"),
        })
    return result


def load_reference(session):
    """Best-effort historical asking-price pool, never a live source."""
    try:
        response = session.get(REFERENCE_URL, timeout=6)
        response.raise_for_status()
        return reference_from_snapshot(response.json())
    except (requests.RequestException, ValueError, TypeError):
        return []



def _price_records(payload):
    """Normalize ranking response shapes without assuming endpoint availability."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ('items', 'rankings', 'results', 'data'):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _price_records(value)
            if nested:
                return nested
    return []


def market_price_baselines(session, key):
    """One optional request for historical item-level market asking-price averages.

    No reliance on this endpoint for scanner availability.  Seven-day market
    rankings are NOT comparable rolled sales and not verified transactions.
    """
    try:
        response = session.get(
            API + '/market/rankings',
            headers={'X-API-Key': key},
            timeout=5,
        )
        response.raise_for_status()
        rows = _price_records(response.json())
    except (requests.RequestException, ValueError, TypeError):
        return {}
    prices = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('name'):
            continue
        # Prefer trimmed mean. It should not be treated as roll-specific price.
        amount = next((number(row.get(field)) for field in
             ('average_mid_80_percent_price', 'p50_price', 'average_p50_ema_price')
             if number(row.get(field)) is not None), None)
        count = number(row.get('total_count'))
        if amount is None or amount <= 0 or (count is not None and count < 3):
            continue
        # Tier and shiny variants must not be conflated with regular items.
        if row.get('shiny') or row.get('shiny_stat'):
            continue
        prices[(str(row['name']).casefold().strip(), str(row.get('tier') or ''))] = {
            'price': amount, 'observations': count,
        }
    return prices


def add_baseline_estimates(items, baselines):
    """Fill missing roll-comparable valuations with CLEARLY LABELED item baselines.

    Deliberately do not mark these as qualified flips: listing price averages
    provide neither rolled-item fair value nor actual buyer demand.
    """
    filled = 0
    for item in items:
        if item.get('fair_e') is not None:
            item['valuation_source'] = 'roll_comparables'
            continue
        if item.get('wynnpool') or item.get('rarity') is None:
            continue
        # Skip unidentified/shiny without verifiable matching ranking variants.
        if item.get('unidentified') or item.get('shiny_stat'):
            continue
        name = str(item.get('name') or '').casefold().strip()
        tier = str(item.get('item_tier') or '')
        base = baselines.get((name, tier))
        if base is None and not tier:
            base = baselines.get((name, ''))
        if not base:
            continue
        price = round(base['price'])
        buy = number(item.get('price_e'))
        if price <= 0 or not buy or buy <= 0:
            continue
        item['fair_e'] = price
        item['profit_e'] = price - buy
        item['roi'] = round(100*(price-buy)/buy,1)
        item['confidence'] = None   # intentionally not fabricated
        item['valuation_source'] = 'item_baseline_not_roll_adjusted'
        item['category'] = 'unrated'
        item['warnings'] = list(item.get('warnings') or []) + [
            'Baseline is aggregated item asking prices, NOT a roll-adjusted resale estimate.',
            'Do not treat its displayed price difference as realizable flip profit.',
        ]
        filled += 1
    return filled

def fetch_market(key, session=None, limit_pages=2, page_size=100):
    session = session or requests.Session()
    rows = []
    total_pages = None
    for page in range(1, limit_pages + 1):
        response = session.get(
            API + "/market/listings",
            headers={"X-API-Key": key},
            params={"page": page, "page_size": page_size, "sort": "timestamp_desc"},
            timeout=12,
        )
        response.raise_for_status()
        listing, pages = extract_listing_page(response.json())
        rows.extend(listing)
        total_pages = pages
        if len(listing) < page_size or (pages is not None and page >= int(pages)):
            break
    if not rows:
        raise ValueError("No listings returned")
    now = datetime.now(timezone.utc)
    profiles = load_profiles(ROOT / 'profiles/item_weights.json')
    reference = load_reference(session)
    evaluated, _, _ = build(rows, {"listings": {}}, {}, now, profiles, reference_rows=reference)
    baselines = market_price_baselines(session, key)
    baseline_estimates = add_baseline_estimates(evaluated, baselines)
    evaluated.sort(key=lambda x: x.get('listing_age_minutes') if x.get('listing_age_minutes') is not None else 10**9)
    return {
        "status": {
            "version": "11", "baseline_items": len(baselines), "baseline_estimates": baseline_estimates, "reference_offers": len(reference), "reference_source": "GitHub snapshot of past asking prices (not live or sold)", "updated_at": now.isoformat(),
            "scanned": len(rows), "exported": len(evaluated),
            "pages": page, "total_pages": total_pages,
            "complete": bool((total_pages and page >= int(total_pages)) or len(listing) < page_size),
            "matched_profiles": sum(bool(x.get('wynnpool')) for x in evaluated),
            "note": "Fresh WynnVentory observations; historical GitHub offers are comparison-only, NOT active or confirmed sales."
        },
        "listings": evaluated,
        "lootpool": {"state": "unavailable", "note": "Live mode focuses on market listings."},
    }


@app.get('/api/market')
@app.get('/api/market.py')
def market():
    key = os.environ.get('WYNNVENTORY_API_KEY', '').strip()
    if not key:
        return JSONResponse({"error": "Server API key missing. Configure WYNNVENTORY_API_KEY in Vercel."}, status_code=503, headers={"Cache-Control": "no-store"})
    try:
        data = fetch_market(key)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        message = "WynnVentory rate limited requests" if status == 429 else "WynnVentory request failed"
        return JSONResponse({"error": message}, status_code=503 if status == 429 else 502, headers={"Cache-Control": "no-store"})
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return JSONResponse({"error": "Market feed unavailable or unexpectedly formatted"}, status_code=502, headers={"Cache-Control": "no-store"})
    return JSONResponse(data, headers={"Cache-Control": "public, s-maxage=30, stale-while-revalidate=30"})


@app.get('/')
def home():
    path = SITE / 'index.html'
    if not path.is_file():
        raise HTTPException(status_code=500, detail="Dashboard file missing from deployment")
    return FileResponse(path, media_type='text/html')


@app.get('/{resource:path}')
def static_file(resource: str):
    # Only allow assets under site; never expose repository source or secrets.
    path = (SITE / resource).resolve()
    if not path.is_relative_to(SITE.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)
