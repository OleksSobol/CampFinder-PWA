import os
import re
import json
import time
import logging
from datetime import datetime, timezone
import urllib.request
import urllib.parse
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "campgrounds.json")
SEED_FILE = os.path.join(os.path.dirname(__file__), "campgrounds_seed.json")

# Authoritative USDA Forest Service Enterprise Data Warehouse (EDW) REST endpoint
ARCGIS_ENDPOINT = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_InfraRecreationSites_01/MapServer/0/query"
)

# USFS Regions covering the entire US West:
# 01: Northern (Montana, northern Idaho)
# 02: Rocky Mountain (Colorado, Wyoming, South Dakota, Nebraska)
# 03: Southwestern (Arizona, New Mexico)
# 04: Intermountain (Idaho, Nevada, Utah, western Wyoming)
# 05: Pacific Southwest (California, Hawaii)
# 06: Pacific Northwest (Washington, Oregon)
WESTERN_REGIONS = ("01", "02", "03", "04", "05", "06")

REGION_NAMES = {
    "01": "Northern (MT/ID)",
    "02": "Rocky Mountain (CO/WY)",
    "03": "Southwestern (AZ/NM)",
    "04": "Intermountain (ID/NV/UT)",
    "05": "Pacific Southwest (CA)",
    "06": "Pacific Northwest (WA/OR)",
}


def parse_price(fee_text: str, fee_charged: str = "N") -> tuple[bool, str]:
    """Determine if a campsite is free or paid, and extract readable price."""
    lower = fee_text.lower()
    if fee_charged == "N" or "no fee" in lower or "no fees" in lower or "free" in lower:
        return True, "Free"

    prices = re.findall(r"\$(\d+(?:\.\d{2})?)", fee_text)
    if prices:
        val = float(prices[0])
        val_str = f"${int(val)}" if val.is_integer() else f"${val:.2f}"
        return False, f"{val_str}/night"

    if "day use fee" in lower or "pass" in lower:
        return False, "Pass/Fee"

    return False, "Fee Req."


def format_camp_name(raw_name: str) -> str:
    """Format ALL-CAPS names to clean Title Case while preserving standard abbreviations."""
    if not raw_name:
        return "Campground"
    name = raw_name.strip()
    if name.isupper():
        name = name.title()
    name = re.sub(r"\bCg\b", "Campground", name)
    name = re.sub(r"\bCp\b", "Camp", name)
    return name


def fetch_all_western_campgrounds() -> list[dict]:
    """Fetch all campgrounds across USFS Regions 1-6 using the official EDW ArcGIS REST API."""
    regions_str = ",".join(f"'{r}'" for r in WESTERN_REGIONS)
    where_clause = f"site_subtype = 'CAMPGROUND' AND region IN ({regions_str})"

    out_fields = (
        "site_name,public_site_name,recarea_name,parent_recarea,closest_towns,region,managing_org,fee_charged,"
        "fee_description,recarea_status,restrictions,minimum_elevation,usda_portal_url,"
        "rec1stop_url,latitude,longitude"
    )

    all_features = []
    offset = 0
    batch_size = 2000

    logger.info(f"Initiating full US West USFS campground scan (Regions: {', '.join(WESTERN_REGIONS)})...")

    while True:
        params = urllib.parse.urlencode({
            "where": where_clause,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": batch_size,
            "f": "geojson",
        })
        url = f"{ARCGIS_ENDPOINT}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CampFinder/1.0 (Mozilla/5.0; https://github.com/OleksSobol/CampFinder-PWA)"
            },
        )

        logger.info(f"Querying ArcGIS EDW batch: offset={offset}, limit={batch_size}...")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                payload = json.loads(res.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to query USFS API at offset {offset}: {e}")
            break

        features = payload.get("features", [])
        if not features:
            break

        all_features.extend(features)
        logger.info(f"Retrieved {len(features)} records (Total so far: {len(all_features)})")

        if len(features) < batch_size:
            break
        offset += batch_size
        time.sleep(0.3)

    logger.info(f"Transforming {len(all_features)} USFS campgrounds into CampFinder format...")

    results = []
    seen_coords = set()

    for f in all_features:
        props = f.get("properties", {})
        geom = f.get("geometry", {})
        coords = geom.get("coordinates") if geom else None

        lat = coords[1] if (coords and len(coords) >= 2) else props.get("latitude")
        lng = coords[0] if (coords and len(coords) >= 2) else props.get("longitude")

        if not lat or not lng:
            continue

        # Prevent exact duplicate coordinate stacking
        coord_key = (round(lat, 5), round(lng, 5))
        if coord_key in seen_coords:
            continue
        seen_coords.add(coord_key)

        raw_name = props.get("public_site_name") or props.get("site_name") or "Campground"
        name = format_camp_name(raw_name)

        fee_text = props.get("fee_description") or ""
        fee_charged = props.get("fee_charged") or ("Y" if fee_text else "N")
        is_free, price_badge = parse_price(fee_text, fee_charged=fee_charged)

        rec_status = (props.get("recarea_status") or "").upper()
        is_open = rec_status not in ("CLOSED", "TEMPORARILY CLOSED")
        status = "Closed" if not is_open else "Open"

        recarea = props.get("parent_recarea") or props.get("recarea_name")
        town = props.get("closest_towns") or ""
        reg_id = props.get("region")

        district_parts = []
        if recarea:
            clean_rec = recarea.title() if recarea.isupper() else recarea
            district_parts.append(clean_rec)
        if town:
            clean_town = re.sub(r"\s+", " ", town).strip("., ")
            district_parts.append(clean_town)

        district = " • ".join(district_parts) if district_parts else REGION_NAMES.get(reg_id, "USFS West")

        usda_url = props.get("usda_portal_url")
        if not usda_url:
            usda_url = f"https://www.google.com/search?q={urllib.parse.quote(name + ' campground usda forest service')}"

        cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not cid:
            cid = f"cg-{round(lat, 4)}-{round(lng, 4)}"

        results.append({
            "id": cid,
            "name": name,
            "url": usda_url,
            "rel_url": None,
            "is_open": is_open,
            "status": status,
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "fee_text": fee_text.strip() if fee_text else ("No fees required" if is_free else "Fee information on site"),
            "is_free": is_free,
            "price_badge": price_badge,
            "district": district,
            "town": town,
            "region": REGION_NAMES.get(reg_id, "USFS West"),
            "stay_limit": props.get("restrictions"),
            "elevation": props.get("minimum_elevation"),
            "reservation_url": props.get("rec1stop_url"),
        })

    logger.info(f"Processed {len(results)} valid Western US campgrounds.")
    return results


def save_campgrounds(campgrounds: list[dict]):
    """Save campground dataset to json cache and seed file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_count": len(campgrounds),
        "open_count": sum(1 for c in campgrounds if c.get("is_open")),
        "campgrounds": campgrounds,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    # Also save to seed file so container builds always have current seed
    try:
        with open(SEED_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to update seed file: {e}")
    logger.info(f"Saved {len(campgrounds)} campgrounds to {DATA_FILE}")


def scrape_all_campgrounds(max_pages: int = 3) -> list[dict]:
    """Backwards-compatible wrapper that triggers full Western US scan."""
    campgrounds = fetch_all_western_campgrounds()
    if campgrounds:
        save_campgrounds(campgrounds)
    return campgrounds


def get_cached_data() -> dict:
    """Read cached campground data from disk, auto-seeding if volume data is stale/incomplete."""
    data = None
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error reading {DATA_FILE}: {e}")

    # If data is missing or holds fewer than 1,000 campgrounds (e.g. stale initial volume)
    if not data or data.get("total_count", 0) < 1000:
        if os.path.exists(SEED_FILE):
            try:
                with open(SEED_FILE, "r", encoding="utf-8") as f:
                    seed_data = json.load(f)
                if seed_data.get("total_count", 0) >= 1000:
                    logger.info(f"Overwriting stale volume cache with {seed_data.get('total_count')} seed campgrounds...")
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(DATA_FILE, "w", encoding="utf-8") as f:
                        json.dump(seed_data, f, indent=2)
                    return seed_data
            except Exception as e:
                logger.error(f"Failed to read seed file {SEED_FILE}: {e}")

    return data or {"updated_at": None, "total_count": 0, "open_count": 0, "campgrounds": []}


if __name__ == "__main__":
    scrape_all_campgrounds()
