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

# Authoritative USDA Forest Service Recreation Opportunities REST endpoint (powers live fs.usda.gov recreation pages)
ARCGIS_ENDPOINT = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_RecreationOpportunities_01/MapServer/0/query"
)

REGION_NAMES = {
    "01": "Northern (MT/ID)",
    "02": "Rocky Mountain (CO/WY)",
    "03": "Southwestern (AZ/NM)",
    "04": "Intermountain (ID/NV/UT)",
    "05": "Pacific Southwest (CA)",
    "06": "Pacific Northwest (WA/OR)",
}


def parse_price(fee_text: str, name: str = "") -> tuple[bool, str]:
    """Accurately extract price, preventing false 'Free' flags from 'download free app' text."""
    lower_name = name.lower()
    if not fee_text or not fee_text.strip():
        if "dispersed" in lower_name:
            return True, "Free"
        if any(w in lower_name for w in ["cabin", "lookout", "group", "resort"]):
            return False, "Reservable"
        return False, "See USFS"

    lower = fee_text.lower()

    # 1. Search for explicit site/nightly price patterns
    site_match = re.search(
        r"\$(\d+(?:\.\d{2})?)\s*(?:for\s*(?:campsite|site|camping)|per\s*night|/\s*night|nightly)",
        lower,
    )
    if site_match:
        val = int(float(site_match.group(1)))
        return False, f"${val}/night"

    label_match = re.search(
        r"(?:single site|overnight|standard|campsite|site)[:\s]*\$(\d+(?:\.\d{2})?)",
        lower,
    )
    if label_match:
        val = int(float(label_match.group(1)))
        return False, f"${val}/night"

    # Any other explicit dollar amount in fee text
    dollar_match = re.search(r"\$(\d+(?:\.\d{2})?)", fee_text)
    if dollar_match:
        val = int(float(dollar_match.group(1)))
        return False, f"${val}/night"

    # 2. Check for explicit free phrases (stripping promotional app mentions)
    clean = re.sub(r"free\s+(?:recreation\.gov|mobile)?\s*app", "", lower)
    clean = re.sub(r"toll[-\s]?free", "", clean)

    if any(
        phrase in clean
        for phrase in [
            "no fee",
            "no fees",
            "no charge",
            "free camping",
            "camping is free",
            "free of charge",
            "free site",
        ]
    ):
        return True, "Free"

    if re.search(r"\bfree\b", clean):
        return True, "Free"

    if "pass" in clean:
        return False, "Pass/Fee"

    return False, "Fee Req."


def format_camp_name(raw_name: str) -> str:
    """Format ALL-CAPS names to clean Title Case."""
    if not raw_name:
        return "Campground"
    name = raw_name.strip()
    if name.isupper():
        name = name.title()
    name = re.sub(r"\bCg\b", "Campground", name)
    name = re.sub(r"\bCp\b", "Camp", name)
    return name


def fetch_all_western_campgrounds() -> list[dict]:
    """Fetch live recreation camping areas across USFS Western Regions (1-6)."""
    where_clause = (
        "markeractivitygroup = 'Camping & Cabins' AND ("
        "forestorgcode LIKE '01%' OR forestorgcode LIKE '02%' OR forestorgcode LIKE '03%' OR "
        "forestorgcode LIKE '04%' OR forestorgcode LIKE '05%' OR forestorgcode LIKE '06%')"
    )

    out_fields = (
        "recareaname,longitude,latitude,recareaurl,forestname,forestorgcode,feedescription,"
        "operational_hours,openstatus,reservation_info,restrictions,markeractivity"
    )

    all_features = []
    offset = 0
    batch_size = 2000

    logger.info("Initiating full US West USFS live recreation scan (Regions 1-6)...")

    while True:
        params = urllib.parse.urlencode({
            "where": where_clause,
            "outFields": out_fields,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": batch_size,
            "f": "json",
        })
        url = f"{ARCGIS_ENDPOINT}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CampFinder/1.0 (Mozilla/5.0; https://github.com/OleksSobol/CampFinder-PWA)"
            },
        )

        logger.info(f"Querying live recreation batch: offset={offset}, limit={batch_size}...")
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

    logger.info(f"Transforming {len(all_features)} live USFS camping sites into CampFinder format...")

    results = []
    seen_coords = set()

    for f in all_features:
        a = f.get("attributes", {})
        try:
            lat = float(a.get("latitude"))
            lng = float(a.get("longitude"))
        except (TypeError, ValueError):
            continue

        if not lat or not lng:
            continue

        coord_key = (round(lat, 5), round(lng, 5))
        if coord_key in seen_coords:
            continue
        seen_coords.add(coord_key)

        raw_name = a.get("recareaname") or "Campground"
        name = format_camp_name(raw_name)

        # Real live status from USFS web portal
        status_raw = (a.get("openstatus") or "").lower().strip()
        hours_raw = (a.get("operational_hours") or "").lower().strip()

        if status_raw in ("closed", "unreachable") or "closed" in hours_raw:
            is_open = False
            status = "Closed"
        elif status_raw == "temporarily closed":
            is_open = False
            status = "Temporarily Closed"
        elif status_raw == "open":
            is_open = True
            status = "Open"
        else:
            if "closed" in hours_raw:
                is_open = False
                status = "Closed"
            else:
                is_open = True
                status = "Open"

        # Fee text parsing
        fee_raw = a.get("feedescription") or ""
        fee_clean = re.sub(r"<[^>]+>", " ", fee_raw)
        fee_clean = re.sub(r"&nbsp;", " ", fee_clean)
        fee_clean = re.sub(r"&amp;", "&", fee_clean)
        fee_clean = " ".join(fee_clean.split())

        is_free, price_badge = parse_price(fee_clean, name=name)

        forest = a.get("forestname") or "National Forest"
        org_code = (a.get("forestorgcode") or "")[:2]
        region = REGION_NAMES.get(org_code, "USFS West")

        # Reservation Link
        reservation_url = None
        for txt in [
            a.get("reservation_info") or "",
            a.get("feedescription") or "",
            a.get("restrictions") or "",
        ]:
            rec_match = re.search(r"https?://(?:www\.)?recreation\.gov/[^\s\"'<>)]+", txt)
            if rec_match:
                reservation_url = rec_match.group(0)
                break

        usda_url = a.get("recareaurl")
        if not usda_url:
            usda_url = f"https://www.google.com/search?q={urllib.parse.quote(name + ' campground usda forest service')}"

        cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not cid:
            cid = f"cg-{round(lat, 4)}-{round(lng, 4)}"

        display_fee = fee_clean if fee_clean else ("No fees required" if is_free else "Check USFS portal for rates")

        results.append({
            "id": cid,
            "name": name,
            "url": usda_url,
            "rel_url": None,
            "is_open": is_open,
            "status": status,
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "fee_text": display_fee[:250],
            "is_free": is_free,
            "price_badge": price_badge,
            "district": forest,
            "town": forest,
            "region": region,
            "stay_limit": a.get("restrictions"),
            "elevation": None,
            "reservation_url": reservation_url,
            "activity": a.get("markeractivity") or "Camping",
        })

    logger.info(f"Processed {len(results)} valid live Western US campgrounds.")
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
                    logger.info(
                        f"Overwriting stale volume cache with {seed_data.get('total_count')} seed campgrounds..."
                    )
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(DATA_FILE, "w", encoding="utf-8") as f:
                        json.dump(seed_data, f, indent=2)
                    return seed_data
            except Exception as e:
                logger.error(f"Failed to read seed file {SEED_FILE}: {e}")

    return data or {"updated_at": None, "total_count": 0, "open_count": 0, "campgrounds": []}


if __name__ == "__main__":
    scrape_all_campgrounds()
