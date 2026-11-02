"""WynnFlip V9 — read-only, on-demand WynnVentory proxy/analysis.
Public endpoint; CDN cache reduces requests but is not a distributed lock.
"""
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import requests
from scanner import build, extract_listing_page, load_profiles

API = "https://wynnventory.com/api/v2"


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
        raise ValueError("No market listings returned")
    now = datetime.now(timezone.utc)
    profiles = load_profiles(Path('profiles/item_weights.json'))
    evaluated, _, _ = build(rows, {"listings": {}}, {}, now, profiles)
    evaluated.sort(key=lambda x: x.get('listing_age_minutes') if x.get('listing_age_minutes') is not None else 10**9)
    return {
        "status": {
            "version": "9", "updated_at": now.isoformat(),
            "scanned": len(rows), "exported": len(evaluated),
            "pages": page, "total_pages": total_pages,
            "complete": bool((total_pages and page >= int(total_pages)) or len(listing) < page_size),
            "matched_profiles": sum(bool(x.get('wynnpool')) for x in evaluated),
            "note": "Fresh API observation; availability and sales unverified. Cached up to 30 seconds."
        },
        "listings": evaluated,
        "lootpool": {"state": "unavailable", "note": "On-demand mode focuses on market listings; lootpool lookup skipped."},
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split('?', 1)[0] not in ('/api/market', '/api/market.py'):
            self.reply(404, {"error": "Not found"}, cache=False)
            return
        key = os.environ.get('WYNNVENTORY_API_KEY', '').strip()
        if not key:
            self.reply(503, {"error": "Server API key missing. Set WYNNVENTORY_API_KEY in Vercel settings."}, cache=False)
            return
        try:
            data = fetch_market(key)
        except requests.HTTPError as err:
            code = err.response.status_code if err.response is not None else 502
            label = 'WynnVentory rate limited requests' if code == 429 else 'WynnVentory request failed'
            self.reply(503 if code == 429 else 502, {"error": label}, cache=False)
            return
        except (requests.RequestException, ValueError, TypeError, KeyError):
            self.reply(502, {"error": "Market feed unavailable or unexpectedly formatted"}, cache=False)
            return
        self.reply(200, data, cache=True)

    def reply(self, code, body, cache):
        payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'public, s-maxage=30, stale-while-revalidate=30' if cache else 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
