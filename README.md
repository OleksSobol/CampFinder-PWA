# 🌲 CampFinder PWA

A lightweight, mobile-first Progressive Web App (PWA) to discover all open campgrounds in the **Okanogan-Wenatchee National Forest** (extensible to any USFS region), with live pricing badges, GPS-based "Near Me" distance sorting, and an interactive Leaflet map.

Built with **Python (FastAPI) + HTMX + Jinja2 + Leaflet.js + Tailwind CSS** — zero complex build steps, no TypeScript compiler, and packaged cleanly for one-click deployment in **Coolify** and access via **Cloudflare** (`camp.lab.osobol.com`).

---

## ✨ Features

- **Automated USFS Scraper**: Scrapes official Forest Service pages (`fs.usda.gov`), extracts exact latitude/longitude coordinates, pricing/fees ($0 vs paid), district, elevation, and recreation links.
- **"Find Near Me" Geolocation**: Uses your browser's GPS (`navigator.geolocation`) to compute real-time distances to each campsite (Haversine formula in miles) and sort the nearest ones first.
- **Interactive Leaflet Map**:
  - Color-coded pins: Emerald green for Open, price badges (`FREE`, `$10/night`, etc.).
  - Blue pulsating marker for user's live position.
  - One-tap "Navigate" button opening turn-by-turn directions in Google Maps or Apple Maps.
- **HTMX Dynamic Filtering**:
  - Instant search by campground name or district.
  - Filter pills: **All**, **Free Only**, **≤ $15**, and **Paid Sites**.
  - Server-rendered HTML swaps without full page reloads.
- **Offline Backcountry PWA**:
  - Service Worker (`sw.js`) caches the app shell and campground database so you can view maps and coordinates even when you lose cell reception in the mountains.
  - Installable to iOS / Android home screen ("Add to Home Screen").

---

## 📁 Project Structure

```
.
├── app.py                  # FastAPI web server, HTMX routes & distance calculation
├── scraper.py              # USFS recreation page scraper & parser
├── requirements.txt        # Minimal Python dependencies (FastAPI, uvicorn, jinja2)
├── Dockerfile              # Production container for Coolify
├── docker-compose.yml      # Docker Compose setup with persistent data volume
├── data/
│   └── campgrounds.json    # Cached campground database (with coordinates & fees)
├── templates/
│   ├── index.html          # Main PWA layout, Leaflet map & HTMX controls
│   └── cards.html          # HTMX partial for campground list cards
└── static/
    ├── sw.js               # Service Worker for offline mountain usage
    ├── manifest.json       # PWA Web App Manifest
    └── icons/
        └── icon.svg        # Crisp vector camping tent icon
```

---

## 🚀 Running Locally

1. **Clone or navigate to the directory**:
   ```bash
   cd campfinder
   ```

2. **Create a virtual environment & install requirements**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Start the server**:
   ```bash
   python3 app.py
   # Or using uvicorn directly:
   uvicorn app:app --host 0.0.0.0 --port 8000 --reload
   ```

4. Open your browser at `http://localhost:8000`.

---

## 🐳 Deploying to Coolify on Your Home Server

Coolify makes running this app on your home server effortless:

### Method 1: Deploy from Git (Recommended)
1. Push this folder to your GitHub / GitLab repository.
2. In your **Coolify Dashboard**:
   - Click **+ New Resource** -> **Application** -> **Public/Private Repository**.
   - Select your repository and branch.
   - Coolify will detect the `Dockerfile` automatically.
3. Under **Persistent Storage / Volumes**, add:
   ```
   ./data -> /app/data
   ```
   *(This ensures your scraped data persists across container restarts and updates).*
4. Click **Deploy**.

### Method 2: Docker Compose in Coolify
If you prefer Docker Compose:
1. In Coolify, click **+ New Resource** -> **Docker Compose**.
2. Paste the contents of `docker-compose.yml`.
3. Click **Deploy**.

---

## 🌐 Connecting Your Cloudflare Domain (`camp.lab.osobol.com`)

You have two easy ways to expose this through Cloudflare:

### Option A: Cloudflare Tunnel (`cloudflared`) — *Recommended (No Open Router Ports)*
If your home server runs behind a dynamic IP or NAT:
1. In your **Cloudflare Zero Trust Dashboard** -> **Networks** -> **Tunnels**:
   - Create a new tunnel (or use an existing one running on your home server).
2. Add a **Public Hostname**:
   - **Subdomain**: `camp`
   - **Domain**: `lab.osobol.com`
   - **Service**: `HTTP` -> `localhost:8000` (or the internal Coolify container IP/hostname `campfinder:8000`).
3. Save. Cloudflare will automatically route `https://camp.lab.osobol.com` with full HTTPS encryption!

### Option B: Coolify Domain Mapping + Cloudflare DNS Proxy
1. In your **Cloudflare DNS Dashboard** for `osobol.com`:
   - Add a `CNAME` or `A` record for `camp.lab` pointing to your home server's public IP / DDNS hostname.
   - Ensure the orange cloud (Proxy) is enabled.
2. In **Coolify Application Settings**:
   - Set **Domains** to: `https://camp.lab.osobol.com`
   - Coolify's built-in reverse proxy (Traefik/Caddy) will handle SSL certificates and route traffic to the container.

---

## 🔄 Updating Data

- **Web UI**: Click the **Sync** button in the header bar of the app. It triggers a background re-scrape without blocking users.
- **CLI**: Run `python3 scraper.py` anytime to refresh `data/campgrounds.json`.
