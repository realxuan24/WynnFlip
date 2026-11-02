
"""WynnFlip V9: on-demand market API + static dashboard on Vercel."""
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from scanner import build, extract_listing_page, load_profiles

API = "https://wynnventory.com/api/v2"
ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def fetch_market(key, session=None, limit_pages=2, page_size=100):
    session = session or requests.Session()
    rows = []
    total_pages = None

    for page in range(1, limit_pages + 1):
        response = session.get(
            API + "/market/listings",
            headers={"X-API-Key": key},
            params={
                "page": page,
                "page_size": page_size,
                "sort": "timestamp_desc"
            },
            timeout=12,
        )
        response.raise_for_status()
        listing, pages = extract_listing_page(response.json())
        rows.extend(listing)
        total_pages = pages

        if len(listing) < page_size or (
            pages is not None and page >= int(pages)
        ):
            break

    if not rows:
        raise ValueError("No listings returned")

    now = datetime.now(timezone.utc)

    profiles = load_profiles(
        ROOT / "profiles/item_weights.json"
    )

    evaluated, _, _ = build(
        rows,
        {"listings": {}},
        {},
        now,
        profiles
    )

    evaluated.sort(
        key=lambda x: (
            x.get("listing_age_minutes")
            if x.get("listing_age_minutes") is not None
            else 10**9
        )
    )

    return {
        "status": {
            "version": "9",
            "updated_at": now.isoformat(),
            "scanned": len(rows),
            "exported": len(evaluated),
            "pages": page,
            "total_pages": total_pages,
            "complete": bool(
                (total_pages and page >= int(total_pages))
                or len(listing) < page_size
            ),
            "matched_profiles": sum(
                bool(x.get("wynnpool")) for x in evaluated
            ),
            "note": (
                "Crowdsourced observations; availability and "
                "sales unverified. CDN cached up to 30 seconds."
            )
        },
        "listings": evaluated,
        "lootpool": {
            "state": "unavailable",
            "note": "Live mode focuses on market listings."
        },
    }


@app.get("/api/market")
@app.get("/api/market.py")
def market():
    key = os.environ.get(
        "WYNNVENTORY_API_KEY", ""
    ).strip()

    if not key:
        return JSONResponse(
            {
                "error": (
                    "Server API key missing. Configure "
                    "WYNNVENTORY_API_KEY in Vercel."
                )
            },
            status_code=503,
            headers={"Cache-Control": "no-store"}
        )

    try:
        data = fetch_market(key)

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None else None
        )

        message = (
            "WynnVentory rate limited requests"
            if status == 429
            else "WynnVentory request failed"
        )

        return JSONResponse(
            {"error": message},
            status_code=503 if status == 429 else 502,
            headers={"Cache-Control": "no-store"}
        )

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError
    ):
        return JSONResponse(
            {
                "error": (
                    "Market feed unavailable or "
                    "unexpectedly formatted"
                )
            },
            status_code=502,
            headers={"Cache-Control": "no-store"}
        )

    return JSONResponse(
        data,
        headers={
            "Cache-Control": (
                "public, s-maxage=30, "
                "stale-while-revalidate=30"
            )
        }
    )


@app.get("/")
def home():
    path = SITE / "index.html"

    if not path.is_file():
        raise HTTPException(
            status_code=500,
            detail="Dashboard file missing from deployment"
        )

    return FileResponse(
        path,
        media_type="text/html"
    )


@app.get("/{resource:path}")
def static_file(resource: str):
    # Only serve files under site/.
    # Never expose repository source or secrets.
    path = (SITE / resource).resolve()

    if (
        not path.is_relative_to(SITE.resolve())
        or not path.is_file()
    ):
        raise HTTPException(
            status_code=404,
            detail="Not found"
        )

    return FileResponse(path)
