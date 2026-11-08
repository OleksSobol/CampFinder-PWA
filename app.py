import os
import math
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def haversine_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great circle distance between two points in miles."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def filter_and_sort_campgrounds(
    campgrounds: list[dict],
    q: Optional[str] = None,
    filter_type: Optional[str] = "open",
    user_lat: Optional[float] = None,
    user_lng: Optional[float] = None,
) -> tuple[list[dict], int]:
    results = []

    for camp in campgrounds:
        # 1. Text Search Filter across Name, District, Region, Town, Fee Info
        if q and q.strip():
            query_lower = q.lower().strip()
            search_str = (
                f"{camp.get('name', '')} {camp.get('district', '')} {camp.get('town', '')} "
                f"{camp.get('region', '')} {camp.get('fee_text', '')}"
            ).lower()
            if query_lower not in search_str:
                continue

        # 2. Type / Status / Price Filter
        is_free = camp.get("is_free", False)
        is_open = camp.get("is_open", True)
        price_badge = camp.get("price_badge", "")

        if filter_type == "open" and not is_open:
            continue
        elif filter_type == "free" and not is_free:
            continue
        elif filter_type == "paid" and is_free:
            continue
        elif filter_type == "under15":
            if not is_free:
                import re
                prices = re.findall(r"\$(\d+)", price_badge)
                if prices and int(prices[0]) > 15:
                    continue

        # 3. Calculate Distance if user coordinates provided
        camp_copy = dict(camp)
        if user_lat is not None and user_lng is not None and camp.get("lat") and camp.get("lng"):
            dist = haversine_distance_miles(user_lat, user_lng, camp["lat"], camp["lng"])
            camp_copy["distance_miles"] = round(dist, 1)
        else:
            camp_copy["distance_miles"] = None

        results.append(camp_copy)

    total_matches = len(results)

    # 4. Sort by Distance if available, else by name
    if user_lat is not None and user_lng is not None:
        results.sort(key=lambda x: (x["distance_miles"] is None, x["distance_miles"] or 999999))
    else:
        results.sort(key=lambda x: (x.get("name") or ""))

    return results, total_matches


@asynccontextmanager
async def lifespan(app: FastAPI):
    data = scraper.get_cached_data()
    if not data.get("campgrounds") or data.get("total_count", 0) < 1000:
        logger.info("Fewer than 1,000 campgrounds found on startup. Seeding US West live dataset...")
        try:
            scraper.scrape_all_campgrounds()
        except Exception as e:
            logger.error(f"Startup scan failed: {e}")
    yield


app = FastAPI(title="CampFinder PWA", lifespan=lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# PWA root-level endpoints (required for service worker scope)
@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        os.path.join(STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse(
        os.path.join(STATIC_DIR, "manifest.json"),
        media_type="application/manifest+json",
    )


# Health check for Coolify & container orchestrators
@app.get("/health")
async def health_check():
    data = scraper.get_cached_data()
    return {
        "status": "healthy",
        "total_campgrounds": data.get("total_count", 0),
        "open_count": data.get("open_count", 0),
        "updated_at": data.get("updated_at"),
    }


# JSON API for Leaflet map & offline service worker caching
@app.get("/api/campgrounds")
async def api_campgrounds():
    data = scraper.get_cached_data()
    return JSONResponse(data)


# Main Application Page (HTML)
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    data = scraper.get_cached_data()
    all_campgrounds = data.get("campgrounds", [])

    # Light map payload for instant Leaflet loading
    map_campgrounds = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "lat": c.get("lat"),
            "lng": c.get("lng"),
            "status": c.get("status", "Open"),
            "is_open": c.get("is_open", True),
            "is_free": c.get("is_free", False),
            "price_badge": c.get("price_badge", "Free"),
            "district": c.get("district", ""),
            "town": c.get("town", ""),
            "region": c.get("region", ""),
            "fee_text": c.get("fee_text", ""),
            "url": c.get("url", ""),
            "reservation_url": c.get("reservation_url"),
        }
        for c in all_campgrounds
    ]

    # Initial top 100 open cards for fast DOM rendering
    filtered_initial, total_initial = filter_and_sort_campgrounds(
        campgrounds=all_campgrounds,
        filter_type="open",
    )

    top_cards = []
    for c in filtered_initial[:100]:
        c_copy = dict(c)
        c_copy["distance_miles"] = None
        top_cards.append(c_copy)

    total_count = data.get("total_count", len(all_campgrounds))
    open_count = data.get("open_count", sum(1 for c in all_campgrounds if c.get("is_open")))

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "campgrounds": top_cards,
            "total_matches": total_initial,
            "total_count": total_count,
            "open_count": open_count,
            "map_campgrounds": map_campgrounds,
            "updated_at": data.get("updated_at"),
            "user_loc": False,
        },
    )


# HTMX Partial: Filtered & Sorted Campground Cards
@app.get("/campgrounds", response_class=HTMLResponse)
async def list_campgrounds(
    request: Request,
    q: Optional[str] = Query(None),
    filter: Optional[str] = Query("open"),
    lat: Optional[str] = Query(None),
    lng: Optional[str] = Query(None),
):
    data = scraper.get_cached_data()
    all_campgrounds = data.get("campgrounds", [])

    user_lat = float(lat) if lat and lat.strip() else None
    user_lng = float(lng) if lng and lng.strip() else None

    filtered, total_matches = filter_and_sort_campgrounds(
        campgrounds=all_campgrounds,
        q=q,
        filter_type=filter,
        user_lat=user_lat,
        user_lng=user_lng,
    )

    visible_cards = filtered[:100]

    return templates.TemplateResponse(
        request=request,
        name="cards.html",
        context={
            "campgrounds": visible_cards,
            "total_matches": total_matches,
            "user_loc": user_lat is not None,
        },
    )


# HTMX Trigger: On-Demand Refresh Scrape
@app.post("/refresh", response_class=HTMLResponse)
async def refresh_campgrounds():
    logger.info("Executing on-demand scan of live US West campgrounds...")
    try:
        campgrounds = scraper.scrape_all_campgrounds()
        count = len(campgrounds)
        return HTMLResponse(
            f'<span class="text-emerald-200">Synced {count}!</span>'
            f'<script>setTimeout(() => window.location.reload(), 400);</script>',
            headers={"HX-Refresh": "true"},
        )
    except Exception as e:
        logger.error(f"Refresh failed: {e}")
        return HTMLResponse('<span class="text-rose-200">Sync failed</span>')


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
