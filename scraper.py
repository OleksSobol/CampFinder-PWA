import os
import re
import json
import time
import logging
from datetime import datetime, timezone
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://www.fs.usda.gov"
RECREATION_URL = (
    "https://www.fs.usda.gov/r06/okanogan-wenatchee/recreation"
    "?rec_site_type_attach%5BCAMPGROUND%5D=CAMPGROUND&items_per_page=50"
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "campgrounds.json")


def fetch_url(url: str, retries: int = 3, timeout: int = 15) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 CampFinder/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Fetch failed (attempt {attempt+1}/{retries}) for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2)
            else:
                raise e


def parse_price(fee_text: str) -> tuple[bool, str]:
    """Determine if a campsite is free or paid, and extract readable price."""
    lower = fee_text.lower()
    if "no fee" in lower or "no fees" in lower or "free" in lower:
        return True, "Free"
    
    # Search for dollar amounts like $15, $20 per night, etc.
    prices = re.findall(r"\$(\d+(?:\.\d{2})?)", fee_text)
    if prices:
        # Take the standard single site price if present
        return False, f"${prices[0]}/night"
    
    if "day use fee" in lower or "pass" in lower:
        return False, "Pass/Fee"
    
    return False, "Fee Required"


def scrape_campground_details(rel_url: str) -> dict:
    url = f"{BASE_URL}{rel_url}" if rel_url.startswith("/") else rel_url
    try:
        html = fetch_url(url)
    except Exception as e:
        logger.error(f"Error fetching details from {url}: {e}")
        return {}

    # Extract coordinates
    lat_match = re.search(r"Latitude:\s*</b>\s*([0-9.-]+)", html)
    lng_match = re.search(r"Longitude:\s*</b>\s*([0-9.-]+)", html)
    lat = float(lat_match.group(1)) if lat_match else None
    lng = float(lng_match.group(1)) if lng_match else None

    # Status check from detail page
    status = "Open"
    if "Closed" in html and "field_rec_status" in html:
        if "Temporarily Closed" in html:
            status = "Temporarily Closed"
        elif "Partially Closed" in html:
            status = "Partially Closed"
        else:
            status = "Closed"

    # Fees extraction
    fee_text = "No fee information available"
    fee_match = re.search(r'id=["\']rec_acc_fees["\']>(.*?)</div>\s*</div>', html, re.DOTALL)
    if fee_match:
        raw_fee = fee_match.group(1)
        cleaned = re.sub(r"<[^>]+>", " ", raw_fee)
        cleaned = re.sub(r"&nbsp;", " ", cleaned)
        cleaned = re.sub(r"&amp;", "&", cleaned)
        fee_text = " ".join(cleaned.split())

    is_free, price_badge = parse_price(fee_text)

    # Ranger District / Area
    district = "Okanogan-Wenatchee"
    dist_match = re.search(r"Ranger District:\s*</b>\s*([^<]+)", html)
    if dist_match:
        district = dist_match.group(1).strip()
    else:
        for d in [
            "Chelan Ranger District",
            "Cle Elum Ranger District",
            "Entiat Ranger District",
            "Methow Valley Ranger District",
            "Naches Ranger District",
            "Wenatchee River Ranger District",
        ]:
            if d.lower() in html.lower():
                district = d
                break

    # Stay limit & reservations
    stay_limit = None
    stay_match = re.search(r"Stay Limit:\s*</b>\s*([^<]+)", html)
    if stay_match:
        stay_limit = stay_match.group(1).strip()

    reservation_url = None
    rec_gov_match = re.search(r'href=["\'](https://www\.recreation\.gov/[^"\']+)["\']', html)
    if rec_gov_match:
        reservation_url = rec_gov_match.group(1)

    # Elevation
    elevation = None
    elev_match = re.search(r"Elevation:\s*</b>\s*([^<]+)", html)
    if elev_match:
        elevation = elev_match.group(1).strip()

    return {
        "lat": lat,
        "lng": lng,
        "status": status,
        "fee_text": fee_text,
        "is_free": is_free,
        "price_badge": price_badge,
        "district": district,
        "stay_limit": stay_limit,
        "elevation": elevation,
        "reservation_url": reservation_url,
    }


def scrape_all_campgrounds(max_pages: int = 3) -> list[dict]:
    """Scrapes all campground listings from Okanogan-Wenatchee."""
    campgrounds = []
    seen_urls = set()

    # Step 1: Scrape Open ones first (they have highest priority)
    pages = [
        f"{RECREATION_URL}&field_rec_status=1&page=0",  # Open campgrounds
        f"{RECREATION_URL}&page=0",                     # General page 0
        f"{RECREATION_URL}&page=1",                     # General page 1
        f"{RECREATION_URL}&page=2",                     # General page 2
    ]

    for list_url in pages[:max_pages]:
        logger.info(f"Scanning directory: {list_url}")
        try:
            html = fetch_url(list_url)
        except Exception as e:
            logger.error(f"Failed to load listing page {list_url}: {e}")
            continue

        # Extract articles
        items = re.findall(
            r'<article class=["\']wfs-rec__teaser["\']>\s*<a href=["\']([^"\']+)["\']>\s*<h3[^>]*>([^<]+)</h3>',
            html,
        )

        for rel_url, name in items:
            rel_url = rel_url.strip()
            name = name.strip()
            if rel_url in seen_urls:
                continue
            seen_urls.add(rel_url)

            # Check teaser status & snippet
            teaser_pattern = rf'<a href=["\']{re.escape(rel_url)}["\'].*?</article>'
            teaser_match = re.search(teaser_pattern, html, re.DOTALL)
            teaser_text = teaser_match.group(0) if teaser_match else ""

            is_open = True
            if "Closed" in teaser_text and "field_rec_status" in teaser_text:
                is_open = False

            campgrounds.append({
                "id": rel_url.split("/")[-1],
                "name": name,
                "url": f"{BASE_URL}{rel_url}",
                "rel_url": rel_url,
                "is_open": is_open,
                "status": "Open" if is_open else "Closed",
            })

    logger.info(f"Found {len(campgrounds)} unique campgrounds. Fetching coordinates and fees...")

    # Step 2: Fetch details for campgrounds
    results = []
    for idx, camp in enumerate(campgrounds):
        logger.info(f"[{idx+1}/{len(campgrounds)}] Scraping details: {camp['name']}")
        details = scrape_campground_details(camp["rel_url"])
        if details:
            camp.update(details)
        # Only keep entries that have valid GPS coordinates so Leaflet can display them
        if camp.get("lat") and camp.get("lng"):
            results.append(camp)
        time.sleep(0.3)  # Gentle delay to respect USDA server

    # Save to file
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_count": len(results),
        "open_count": sum(1 for c in results if c.get("is_open")),
        "campgrounds": results,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    logger.info(f"Successfully saved {len(results)} campgrounds with coordinates to {DATA_FILE}")
    return results


def get_cached_data() -> dict:
    """Read cached campground data from disk."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading {DATA_FILE}: {e}")
    return {"updated_at": None, "total_count": 0, "open_count": 0, "campgrounds": []}


if __name__ == "__main__":
    scrape_all_campgrounds(max_pages=2)
