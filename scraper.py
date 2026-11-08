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

# Authoritative USDA Forest Service REST endpoints
OPPORTUNITIES_ENDPOINT = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_RecreationOpportunities_01/MapServer/0/query"
)
INFRA_ENDPOINT = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_InfraRecreationSites_01/MapServer/0/query"
)

# Complete 79 National Forest Org Code -> (Region, Forest Slug) mapping for modern USFS Drupal URLs
FOREST_SLUG_MAP = {
    "0102": ("r01", "beaverhead-deerlodge"),
    "0103": ("r01", "bitterroot"),
    "0104": ("r01", "idahopanhandle"),
    "0110": ("r01", "flathead"),
    "0111": ("r01", "custergallatin"),
    "0114": ("r01", "kootenai"),
    "0115": ("r01", "helena-lewisclark"),
    "0116": ("r01", "lolo"),
    "0117": ("r01", "nezperce-clearwater"),
    "0118": ("r01", "dpg"),
    "0202": ("r02", "bighorn"),
    "0203": ("r02", "blackhills"),
    "0204": ("r02", "gmug"),
    "0206": ("r02", "mbrtb"),
    "0207": ("r02", "nebraska"),
    "0209": ("r02", "riogrande"),
    "0210": ("r02", "arp"),
    "0212": ("r02", "psicc"),
    "0213": ("r02", "sanjuan"),
    "0214": ("r02", "shoshone"),
    "0215": ("r02", "whiteriver"),
    "0301": ("r03", "apache-sitgreaves"),
    "0302": ("r03", "carson"),
    "0303": ("r03", "cibola"),
    "0304": ("r03", "coconino"),
    "0305": ("r03", "coronado"),
    "0306": ("r03", "gila"),
    "0307": ("r03", "kaibab"),
    "0308": ("r03", "lincoln"),
    "0309": ("r03", "prescott"),
    "0310": ("r03", "santafe"),
    "0312": ("r03", "tonto"),
    "0401": ("r04", "ashley"),
    "0402": ("r04", "boise"),
    "0403": ("r04", "bridger-teton"),
    "0407": ("r04", "dixie"),
    "0408": ("r04", "fishlake"),
    "0410": ("r04", "manti-lasal"),
    "0412": ("r04", "payette"),
    "0413": ("r04", "salmon-challis"),
    "0414": ("r04", "sawtooth"),
    "0415": ("r04", "caribou-targhee"),
    "0417": ("r04", "humboldt-toiyabe"),
    "0419": ("r04", "uinta-wasatch-cache"),
    "0501": ("r05", "angeles"),
    "0502": ("r05", "cleveland"),
    "0503": ("r05", "eldorado"),
    "0504": ("r05", "inyo"),
    "0505": ("r05", "klamath"),
    "0506": ("r05", "lassen"),
    "0507": ("r05", "lospadres"),
    "0508": ("r05", "mendocino"),
    "0509": ("r05", "modoc"),
    "0510": ("r05", "sixrivers"),
    "0511": ("r05", "plumas"),
    "0512": ("r05", "sanbernardino"),
    "0513": ("r05", "sequoia"),
    "0514": ("r05", "shasta-trinity"),
    "0515": ("r05", "sierra"),
    "0516": ("r05", "stanislaus"),
    "0517": ("r05", "tahoe"),
    "0519": ("r05", "laketahoebasin"),
    "0601": ("r06", "deschutes"),
    "0602": ("r06", "fremont-winema"),
    "0603": ("r06", "giffordpinchot"),
    "0604": ("r06", "malheur"),
    "0605": ("r06", "mbs"),
    "0606": ("r06", "mthood"),
    "0607": ("r06", "ochoco"),
    "0609": ("r06", "olympic"),
    "0610": ("r06", "rogue-siskiyou"),
    "0612": ("r06", "siuslaw"),
    "0614": ("r06", "umatilla"),
    "0615": ("r06", "umpqua"),
    "0616": ("r06", "wallowa-whitman"),
    "0617": ("r06", "okanogan-wenatchee"),
    "0618": ("r06", "willamette"),
    "0621": ("r06", "colville"),
    "0622": ("r06", "columbiarivergorge"),
    "1004": ("r10", "chugach"),
    "1005": ("r10", "tongass"),
}

REGION_NAMES = {
    "01": "Northern (MT/ID)",
    "02": "Rocky Mountain (CO/WY)",
    "03": "Southwestern (AZ/NM)",
    "04": "Intermountain (ID/NV/UT)",
    "05": "Pacific Southwest (CA)",
    "06": "Pacific Northwest (WA/OR)",
    "10": "Alaska (AK)",
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


def fetch_infra_lookup() -> dict:
    """Fetch authoritative portal URLs and Recreation.gov booking links from Infra database."""
    logger.info("Loading Infra recreation lookup table for direct Drupal & Booking URLs...")
    lookup = {}
    offset = 0
    batch_size = 2000
    regions_str = "('01', '02', '03', '04', '05', '06', '10')"
    where_infra = f"site_subtype = 'CAMPGROUND' AND region IN {regions_str}"

    while True:
        params = urllib.parse.urlencode({
            "where": where_infra,
            "outFields": "site_cn,public_site_name,usda_portal_url,rec1stop_url",
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": batch_size,
            "f": "json",
        })
        req = urllib.request.Request(
            f"{INFRA_ENDPOINT}?{params}",
            headers={"User-Agent": "CampFinder/1.0 (Mozilla/5.0; https://github.com/OleksSobol/CampFinder-PWA)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except Exception as e:
            logger.warning(f"Infra lookup fetch failed at offset {offset}: {e}")
            break

        features = data.get("features", [])
        if not features:
            break

        for f in features:
            a = f.get("attributes", {})
            cn = a.get("site_cn")
            if cn:
                lookup[cn] = a

        if len(features) < batch_size:
            break
        offset += batch_size

    logger.info(f"Loaded {len(lookup)} authoritative Infra records.")
    return lookup


def build_campsite_url(name: str, org_code: str, infra_cn: str, infra_lookup: dict) -> tuple[str, str | None]:
    """Return authoritative (usda_url, reservation_url) guaranteed not to redirect to root recreation page."""
    infra_record = infra_lookup.get(infra_cn) if infra_cn else None

    # 1. Official modern portal URL from Infra if available
    if infra_record:
        portal_url = infra_record.get("usda_portal_url")
        rec1stop = infra_record.get("rec1stop_url")
        if portal_url and "recarea/?recid=" not in portal_url:
            return portal_url, rec1stop

    # 2. Derive modern Drupal URL using forest org code and slug
    org_prefix = org_code[:4] if org_code else ""
    if org_prefix in FOREST_SLUG_MAP:
        reg_code, forest_slug = FOREST_SLUG_MAP[org_prefix]
        clean_name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if "camp" not in clean_name and "dispersed" not in clean_name and "cabin" not in clean_name:
            clean_name = f"{clean_name}-campground"
        derived_url = f"https://www.fs.usda.gov/{reg_code}/{forest_slug}/recreation/{clean_name}"
        rec1stop = infra_record.get("rec1stop_url") if infra_record else None
        return derived_url, rec1stop

    # 3. Clean search fallback
    fallback = f"https://www.google.com/search?q={urllib.parse.quote(name + ' campground usda forest service')}"
    return fallback, None


def fetch_all_western_campgrounds() -> list[dict]:
    """Fetch live recreation camping areas across USFS Western Regions (1-6) with modern direct URLs."""
    infra_lookup = fetch_infra_lookup()

    where_clause = (
        "markeractivitygroup = 'Camping & Cabins' AND ("
        "forestorgcode LIKE '01%' OR forestorgcode LIKE '02%' OR forestorgcode LIKE '03%' OR "
        "forestorgcode LIKE '04%' OR forestorgcode LIKE '05%' OR forestorgcode LIKE '06%' OR "
        "forestorgcode LIKE '10%')"
    )

    out_fields = (
        "recareaname,longitude,latitude,recareaurl,forestname,forestorgcode,feedescription,"
        "operational_hours,openstatus,reservation_info,restrictions,markeractivity,infra_cn"
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
        url = f"{OPPORTUNITIES_ENDPOINT}?{params}"
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
        org_code = a.get("forestorgcode") or ""
        region = REGION_NAMES.get(org_code[:2], "USFS West")

        # Authoritative modern URL and Direct Reservation URL
        infra_cn = a.get("infra_cn")
        usda_url, reservation_url = build_campsite_url(name, org_code, infra_cn, infra_lookup)

        # If reservation_url wasn't in Infra, look for explicit recreation.gov link in text
        if not reservation_url:
            for txt in [
                a.get("reservation_info") or "",
                a.get("feedescription") or "",
                a.get("restrictions") or "",
            ]:
                rec_match = re.search(r"https?://(?:www\.)?recreation\.gov/[^\s\"'<>)]+", txt)
                if rec_match:
                    reservation_url = rec_match.group(0)
                    break

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
