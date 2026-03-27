#!/usr/bin/env python3
"""
Vinyl Wishlist Price Tracker
─────────────────────────────
Scrapes Discogs marketplace for the cheapest M/NM/M- offers (media & sleeve)
shipping from Europe, with real prices including shipping.

Usage:
    python3 sync.py                    # reads token from .env or DISCOGS_TOKEN env var
    python3 sync.py YOUR_TOKEN         # pass token as argument
    DISCOGS_TOKEN=xxx python3 sync.py  # env var

Setup:
    1. pip install cloudscraper
    2. Put your Discogs token in .env:  DISCOGS_TOKEN=xxxxx
    3. Edit wishlist.txt (one album per line: Artist - Album)
    4. Run:  python3 sync.py
    5. Open index.html in your browser

Cron (daily at 8am):
    0 8 * * * cd /path/to/vinyl-tracker && python3 sync.py >> sync.log 2>&1
"""

import sys
import json
import time
import os
import re
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from html import escape

import cloudscraper

# ── Config ────────────────────────────────────────────────────────────────────
WISHLIST_FILE = Path("wishlist.txt")
PRICES_FILE = Path("prices.json")
COVERS_DIR = Path("covers")
HTML_FILE = Path("index.html")
USER_AGENT = "VinylWishlistTracker/1.0"
REQUEST_DELAY = 1.1       # seconds between API calls (Discogs limit: 60/min)
SCRAPE_DELAY = 2.0        # seconds between sell-list page fetches

EUROPEAN_COUNTRIES = {
    "Albania", "Andorra", "Austria", "Belarus", "Belgium",
    "Bosnia And Herzegovina", "Bulgaria", "Croatia", "Cyprus",
    "Czech Republic", "Czechia", "Denmark", "Estonia", "Finland", "France",
    "Georgia", "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy",
    "Kosovo", "Latvia", "Liechtenstein", "Lithuania", "Luxembourg", "Malta",
    "Moldova", "Monaco", "Montenegro", "Netherlands", "North Macedonia",
    "Norway", "Poland", "Portugal", "Romania", "San Marino", "Serbia",
    "Slovakia", "Slovenia", "Spain", "Sweden", "Switzerland",
    "Turkey", "UK", "United Kingdom", "Ukraine", "Vatican City",
}

ACCEPTED_CONDITIONS = {"Mint (M)", "Near Mint (NM or M-)"}


# ── .env loader ───────────────────────────────────────────────────────────────

def load_dotenv():
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


# ── API helpers ───────────────────────────────────────────────────────────────

def fetch_json(url, token):
    """Fetch JSON from Discogs API with rate limiting and retry on 429."""
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Discogs token={token}",
    }
    req = urllib.request.Request(url, headers=headers)
    time.sleep(REQUEST_DELAY)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining")
            if remaining and int(remaining) < 5:
                print("    ... rate limit low, pausing 5s")
                time.sleep(5)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("    ... rate limited, waiting 60s")
            time.sleep(60)
            return fetch_json(url, token)
        raise


def download_cover(url, item_id, token):
    """Download cover image, skip if already cached."""
    COVERS_DIR.mkdir(exist_ok=True)
    dest = COVERS_DIR / f"{item_id}.jpg"
    if dest.exists():
        return f"covers/{item_id}.jpg"
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Discogs token={token}",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest.write_bytes(resp.read())
        time.sleep(0.3)
        return f"covers/{item_id}.jpg"
    except Exception as e:
        print(f"    ! cover download failed: {e}")
        return None


# ── Wishlist parsing ──────────────────────────────────────────────────────────

def parse_wishlist():
    """Parse wishlist.txt into structured items."""
    if not WISHLIST_FILE.exists():
        print(f"Error: {WISHLIST_FILE} not found. Create it first.")
        sys.exit(1)

    items = []
    for num, line in enumerate(WISHLIST_FILE.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Discogs master URL
        m = re.match(r"https?://.*discogs\.com/master/(\d+)", line)
        if m:
            items.append({"type": "master_id", "id": int(m.group(1)), "raw": line})
            continue

        # Discogs release URL
        m = re.match(r"https?://.*discogs\.com/release/(\d+)", line)
        if m:
            items.append({"type": "release_id", "id": int(m.group(1)), "raw": line})
            continue

        # Artist - Album
        if " - " in line:
            artist, album = line.split(" - ", 1)
            items.append({
                "type": "search",
                "artist": artist.strip(),
                "album": album.strip(),
                "raw": line,
            })
        else:
            print(f"  line {num}: skipping unrecognized format: {line}")

    return items


# ── Discogs API lookups ───────────────────────────────────────────────────────

def search_master(artist, album, token):
    """Search for a master release. Falls back to release search."""
    query = urllib.parse.quote(f"{artist} {album}")
    url = f"https://api.discogs.com/database/search?q={query}&type=master&per_page=5"
    data = fetch_json(url, token)
    results = data.get("results", [])
    if results:
        return results[0], "master"

    url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=5"
    data = fetch_json(url, token)
    results = data.get("results", [])
    if results:
        return results[0], "release"

    return None, None


def get_master(master_id, token):
    return fetch_json(f"https://api.discogs.com/masters/{master_id}", token)


def get_release(release_id, token):
    return fetch_json(f"https://api.discogs.com/releases/{release_id}", token)


def marketplace_url_master(master_id):
    """Discogs marketplace URL for all versions of a master, filtered to M/NM vinyl."""
    return (f"https://www.discogs.com/sell/list?master_id={master_id}"
            f"&ev=mb&condition=Mint+%28M%29&condition=Near+Mint+%28NM+or+M-%29"
            f"&format=Vinyl&sort=price%2Casc")


def marketplace_url_release(release_id):
    """Discogs marketplace URL for a specific release, filtered to M/NM."""
    return (f"https://www.discogs.com/sell/release/{release_id}"
            f"?ev=rb&condition=Mint+%28M%29&condition=Near+Mint+%28NM+or+M-%29"
            f"&sort=price%2Casc")


# ── Sell list scraping ────────────────────────────────────────────────────────

def _strip_html(s):
    """Remove HTML tags and trim whitespace."""
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_sell_list_html(html):
    """Parse individual listings from a Discogs sell list page."""
    listings = []
    rows = re.split(r'<tr class="shortcut_navigable', html)

    for row_html in rows[1:]:
        row = row_html.split("</tr>")[0]
        listing = {}

        # Listing URL
        m = re.search(r'href="(/sell/item/\d+)"', row)
        listing["url"] = "https://www.discogs.com" + m.group(1) if m else ""

        # Release title/format from link text
        m = re.search(r'item_description_title[^>]*>([^<]+)', row)
        listing["title"] = _strip_html(m.group(1)) if m else ""

        # Label
        m = re.search(r'mplabel">Label:</span>\s*<a[^>]*>([^<]+)', row)
        listing["label"] = _strip_html(m.group(1)) if m else ""

        # Cat#
        m = re.search(r'item_catno">([^<]+)', row)
        listing["catno"] = _strip_html(m.group(1)) if m else ""

        # Media condition — text after "Media Condition:" label
        m = re.search(
            r'condition-label-desktop">\s*Media Condition:\s*</span>'
            r'.*?<span[^>]*>\s*([A-Z][^<]+)',
            row, re.DOTALL,
        )
        listing["media_condition"] = _strip_html(m.group(1)) if m else ""

        # Sleeve condition
        m = re.search(r'item_sleeve_condition">([^<]+)', row)
        listing["sleeve_condition"] = _strip_html(m.group(1)) if m else ""

        # Ships from
        m = re.search(r'Ships From:</span>\s*([^<]+)', row)
        listing["ships_from"] = _strip_html(m.group(1)) if m else ""

        # ── Price parsing ────────────────────────────────────────────────
        # Shipping text (from desktop section)
        m = re.search(
            r'class="item_price hide_mobile".*?item_shipping">\s*\+\s*([^<]+)',
            row, re.DOTALL,
        )
        listing["shipping_text"] = _strip_html(m.group(1)) if m else ""

        # Total EUR price including shipping (converted_price always contains price+shipping in EUR)
        m = re.search(
            r'class="item_price hide_mobile".*?converted_price">\s*(?:about)?\s*€([\d,.\s]+)',
            row, re.DOTALL,
        )
        if not m:
            continue
        listing["total_eur"] = float(m.group(1).replace(",", "").replace(" ", ""))

        listings.append(listing)

    return listings


def scrape_cheapest_offers(scraper, master_id=None, release_id=None, max_pages=3):
    """Scrape Discogs sell list for cheapest M/NM offers from Europe."""
    if master_id:
        base_url = (
            f"https://www.discogs.com/sell/list?master_id={master_id}"
            f"&ev=mb"
            f"&condition=Near+Mint+%28NM+or+M-%29"
            f"&sort=price%2Casc&limit=50"
        )
    elif release_id:
        base_url = (
            f"https://www.discogs.com/sell/release/{release_id}"
            f"?condition=Near+Mint+%28NM+or+M-%29"
            f"&sort=price%2Casc&limit=50"
        )
    else:
        return []

    offers = []
    for page in range(1, max_pages + 1):
        url = base_url + (f"&page={page}" if page > 1 else "")
        time.sleep(SCRAPE_DELAY)

        try:
            resp = scraper.get(url, timeout=25)
            if resp.status_code != 200:
                print(f"    ! sell list returned {resp.status_code}")
                break
        except Exception as e:
            print(f"    ! sell list fetch failed: {e}")
            break

        listings = parse_sell_list_html(resp.text)
        if not listings:
            break

        for lst in listings:
            # Sleeve must be M / NM / M-
            if lst["sleeve_condition"] not in ACCEPTED_CONDITIONS:
                continue
            # Must ship from Europe
            if lst["ships_from"] not in EUROPEAN_COUNTRIES:
                continue
            offers.append(lst)

        if len(offers) >= 3:
            break

        # Stop if no more pages
        if "pagination_next" not in resp.text:
            break

    offers.sort(key=lambda o: o["total_eur"])
    return offers[:3]


# ── Sync logic ────────────────────────────────────────────────────────────────

def sync_item(item, token, scraper):
    """Process one wishlist entry, return price data dict."""
    result = {
        "query": item["raw"],
        "artist": "",
        "album": "",
        "year": None,
        "master_id": None,
        "release_id": None,
        "master_url": None,
        "cover": None,
        "marketplace_url": None,
        "offers": [],
        "lowest_price": None,
        "currency": "EUR",
        "error": None,
    }

    try:
        master_data = None

        # ── Resolve to master release ─────────────────────────────────────
        if item["type"] == "search":
            print(f"  searching: {item['artist']} - {item['album']}")
            hit, hit_type = search_master(item["artist"], item["album"], token)
            if not hit:
                result["error"] = "Not found on Discogs"
                result["artist"] = item.get("artist", "")
                result["album"] = item.get("album", "")
                return result

            if hit_type == "master":
                result["master_id"] = hit["id"]
                master_data = get_master(hit["id"], token)
            else:
                release = get_release(hit["id"], token)
                mid = release.get("master_id")
                if mid:
                    result["master_id"] = mid
                    master_data = get_master(mid, token)
                else:
                    return _fill_from_release(result, release, hit["id"], token, scraper)

        elif item["type"] == "master_id":
            result["master_id"] = item["id"]
            master_data = get_master(item["id"], token)

        elif item["type"] == "release_id":
            release = get_release(item["id"], token)
            mid = release.get("master_id")
            if mid:
                result["master_id"] = mid
                master_data = get_master(mid, token)
            else:
                return _fill_from_release(result, release, item["id"], token, scraper)

        if not master_data:
            result["error"] = "Could not load master release"
            return result

        # ── Fill master info ──────────────────────────────────────────────
        artists = master_data.get("artists", [])
        result["artist"] = ", ".join(a.get("name", "") for a in artists)
        result["album"] = master_data.get("title", "")
        result["year"] = master_data.get("year")
        result["master_url"] = f"https://www.discogs.com/master/{result['master_id']}"
        result["marketplace_url"] = marketplace_url_master(result["master_id"])

        images = master_data.get("images", [])
        if images:
            result["cover"] = download_cover(
                images[0].get("uri", images[0].get("resource_url", "")),
                result["master_id"], token
            )

        # ── Scrape cheapest M/NM offers from Europe ──────────────────────
        print(f"  scraping offers: {result['artist']} - {result['album']}")
        result["offers"] = scrape_cheapest_offers(scraper, master_id=result["master_id"])
        result["lowest_price"] = result["offers"][0]["total_eur"] if result["offers"] else None

        if result["offers"]:
            prices = [f"€{o['total_eur']:.2f}" for o in result["offers"]]
            print(f"    found {len(result['offers'])} offers from Europe: {', '.join(prices)}")
        else:
            print(f"    no M/NM offers from Europe found")

    except Exception as e:
        result["error"] = str(e)
        print(f"    ERROR: {e}")

    return result


def _fill_from_release(result, release, release_id, token, scraper):
    """Fill result from a single release (no master available)."""
    artists = release.get("artists", [])
    result["artist"] = ", ".join(a.get("name", "") for a in artists)
    result["album"] = release.get("title", "")
    result["year"] = release.get("year")
    result["release_id"] = release_id
    result["marketplace_url"] = marketplace_url_release(release_id)

    images = release.get("images", [])
    if images:
        result["cover"] = download_cover(
            images[0].get("uri", images[0].get("resource_url", "")),
            release_id, token
        )

    print(f"  scraping offers: {result['artist']} - {result['album']}")
    result["offers"] = scrape_cheapest_offers(scraper, release_id=release_id)
    result["lowest_price"] = result["offers"][0]["total_eur"] if result["offers"] else None
    return result


# ── HTML generation ───────────────────────────────────────────────────────────

def generate_html(data):
    """Generate index.html with embedded price data."""
    items_json = json.dumps(data["items"], ensure_ascii=False)
    synced = data["synced_at"]
    count = len(data["items"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vinyl Wishlist</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Ovo&family=Mulish:wght@300;400;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0e0e0e;
    --surface: #181818;
    --border: #2a2a2a;
    --text: #e8e4dc;
    --muted: #999;
    --accent: #c8a96e;
    --accent2: #e8c88e;
    --green: #4a9;
    --red: #c0392b;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ scroll-behavior: smooth; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Mulish', sans-serif;
    min-height: 100vh;
  }}

  /* ── Header ── */
  header {{
    padding: 4rem 4rem 2rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
  }}

  .header-left h1 {{
    font-family: 'Ovo', serif;
    font-size: clamp(2.5rem, 6vw, 5rem);
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.02em;
    color: var(--text);
  }}

  .header-left h1 em {{
    color: var(--accent);
    font-style: italic;
  }}

  .h1-sub {{
    font-size: 0.38em;
    font-weight: 400;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--muted);
    font-family: 'Mulish', sans-serif;
  }}

  .header-left .subtitle {{
    margin-top: 0.75rem;
    font-size: 0.8rem;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}

  .header-right {{
    text-align: right;
    font-size: 0.8rem;
    color: var(--muted);
    line-height: 1.8;
  }}

  .header-right .count {{
    font-family: 'Ovo', serif;
    font-size: 2rem;
    color: var(--accent);
    display: block;
    line-height: 1;
  }}

  /* ── Controls bar ── */
  .controls {{
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 0.75rem 4rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }}

  .sort-btn {{
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: 'Mulish', sans-serif;
    font-size: 0.8rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s, background 0.15s;
  }}
  .sort-btn.active {{
    color: var(--accent);
    border-color: var(--accent);
    background: rgba(200,169,110,0.07);
  }}
  .sort-btn:hover:not(.active) {{
    color: var(--text);
    border-color: #444;
  }}

  .search-wrap {{
    margin-left: auto;
    position: relative;
  }}
  #search {{
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: 'Mulish', sans-serif;
    font-size: 0.8rem;
    padding: 0.35rem 0.6rem;
    width: 200px;
    border-radius: 2px;
    outline: none;
    transition: border-color 0.15s;
  }}
  #search:focus {{ border-color: var(--accent); }}
  #search::placeholder {{ color: var(--muted); opacity: 0.6; }}

  /* ── Content ── */
  .content {{
    padding: 2rem 4rem;
  }}

  .album-list {{
    display: flex;
    flex-direction: column;
  }}

  /* ── Album row ── */
  @keyframes rowIn {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}

  .album-row {{
    display: grid;
    grid-template-columns: 80px 1fr repeat(3, minmax(140px, 180px));
    gap: 1.5rem;
    align-items: center;
    padding: 1rem 0;
    border-bottom: 1px solid var(--border);
    animation: rowIn 0.3s ease both;
    transition: background 0.15s;
  }}
  .album-row:hover {{
    background: rgba(255,255,255,0.015);
  }}
  .album-row.has-error {{
    opacity: 0.5;
  }}

  .cover-wrap {{
    width: 80px;
    height: 80px;
    flex-shrink: 0;
    overflow: hidden;
    background: #111;
  }}
  .cover-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    filter: grayscale(15%);
    transition: filter 0.3s;
  }}
  .album-row:hover .cover-wrap img {{
    filter: grayscale(0%);
  }}
  .cover-placeholder {{
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Ovo', serif;
    font-size: 2rem;
    color: var(--muted);
    background: repeating-linear-gradient(
      45deg, var(--surface), var(--surface) 8px, #1a1a1a 8px, #1a1a1a 16px
    );
  }}

  .row-info {{
    min-width: 0;
  }}
  .row-artist {{
    font-size: 0.78rem;
    color: var(--accent);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .row-title {{
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--text);
    margin-top: 0.15rem;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .row-meta {{
    font-size: 0.75rem;
    color: var(--muted);
    margin-top: 0.25rem;
    display: flex;
    gap: 0.75rem;
    align-items: center;
  }}
  .row-meta a {{
    color: var(--accent);
    text-decoration: none;
    font-size: 0.72rem;
  }}
  .row-meta a:hover {{ text-decoration: underline; }}

  /* ── Offer column ── */
  .offer {{
    padding: 0.6rem 0.75rem;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border);
    border-radius: 2px;
    transition: border-color 0.15s;
  }}
  .offer:hover {{
    border-color: #444;
  }}
  .offer-price {{
    font-family: 'Ovo', serif;
    font-size: 1.15rem;
    color: var(--green);
    line-height: 1;
  }}
  .offer-shipping {{
    font-size: 0.68rem;
    color: var(--muted);
    margin-top: 0.2rem;
  }}
  .offer-label {{
    font-size: 0.72rem;
    color: var(--text);
    margin-top: 0.3rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .offer-detail {{
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 0.1rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .offer-link {{
    display: inline-block;
    margin-top: 0.35rem;
    font-size: 0.7rem;
    color: var(--accent);
    text-decoration: none;
  }}
  .offer-link:hover {{ text-decoration: underline; }}

  .offer.empty {{
    opacity: 0.25;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .offer.empty .offer-price {{
    color: var(--muted);
    font-size: 0.85rem;
  }}

  .row-error {{
    font-size: 0.78rem;
    color: var(--red);
    grid-column: 3 / -1;
  }}

  /* ── Footer ── */
  footer {{
    border-top: 1px solid var(--border);
    padding: 2rem 4rem;
    font-size: 0.8rem;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
  }}

  /* ── Responsive ── */
  @media (max-width: 900px) {{
    .album-row {{
      grid-template-columns: 60px 1fr;
      gap: 0.75rem 1rem;
    }}
    .offer {{ grid-column: 1 / -1; }}
    header {{ padding: 2rem 1.5rem 1.5rem; }}
    .controls {{ padding: 0.75rem 1.5rem; }}
    .content {{ padding: 1.5rem; }}
    footer {{ padding: 1.5rem; }}
  }}
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>Vinyl <em>Wishlist</em> <span class="h1-sub">Price Tracker</span></h1>
    <div class="subtitle">Discogs Marketplace &middot; M / NM &middot; ships from Europe</div>
  </div>
  <div class="header-right">
    <span class="count" id="count">{count}</span>
    albums tracked<br>
    <span style="font-size:0.75rem">synced {escape(synced[:16].replace('T', ' '))} UTC</span>
  </div>
</header>

<div class="controls">
  <button class="sort-btn active" data-sort="artist">Artist</button>
  <button class="sort-btn" data-sort="price-asc">Price &uarr;</button>
  <button class="sort-btn" data-sort="price-desc">Price &darr;</button>
  <div class="search-wrap">
    <input type="text" id="search" placeholder="Search...">
  </div>
</div>

<div class="content">
  <div class="album-list" id="grid"></div>
</div>

<footer>
  <div>Top 3 cheapest M / NM offers &middot; media &amp; sleeve &middot; ships from Europe &middot; price includes shipping</div>
  <div>Built with Discogs</div>
</footer>

<script>
const DATA = {items_json};

function renderRow(item, index) {{
  const cover = item.cover
    ? `<img src="${{item.cover}}" alt="" loading="lazy">`
    : `<div class="cover-placeholder">&#9834;</div>`;

  const offers = (item.offers || []);
  let offerCols = '';

  for (let i = 0; i < 3; i++) {{
    if (i < offers.length) {{
      const o = offers[i];
      offerCols += `<div class="offer">
        <div class="offer-price">&euro;${{o.total_eur.toFixed(2)}}</div>
        <div class="offer-shipping">${{esc(o.shipping_text || '')}}</div>
        <div class="offer-label">${{esc(o.label || '')}}</div>
        <div class="offer-detail">${{esc(o.ships_from || '')}} &middot; ${{esc(o.sleeve_condition || '')}}</div>
        <a class="offer-link" href="${{o.url}}" target="_blank" rel="noopener">view listing &rarr;</a>
      </div>`;
    }} else {{
      offerCols += `<div class="offer empty"><div class="offer-price">&mdash;</div></div>`;
    }}
  }}

  const errorHtml = item.error
    ? `<div class="row-error">${{esc(item.error)}}</div>`
    : '';

  const metaParts = [];
  if (item.year) metaParts.push(item.year);

  const links = [];
  if (item.marketplace_url) links.push(`<a href="${{item.marketplace_url}}" target="_blank" rel="noopener">all M/NM</a>`);
  if (item.master_url) links.push(`<a href="${{item.master_url}}" target="_blank" rel="noopener">discogs</a>`);

  return `<div class="album-row${{item.error ? ' has-error' : ''}}" data-index="${{index}}">
    <div class="cover-wrap">${{cover}}</div>
    <div class="row-info">
      <div class="row-artist">${{esc(item.artist)}}</div>
      <div class="row-title">${{esc(item.album)}}</div>
      <div class="row-meta">
        <span>${{metaParts.join(' &middot; ')}}</span>
        ${{links.join(' ')}}
      </div>
    </div>
    ${{errorHtml || offerCols}}
  </div>`;
}}

function esc(s) {{
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}}

// ── Sort & filter ──
let currentSort = 'artist';
let currentFilter = '';

function sortAndRender() {{
  let items = DATA.map((item, i) => ({{ ...item, _origIdx: i }}));

  if (currentFilter) {{
    const q = currentFilter.toLowerCase();
    items = items.filter(it =>
      (it.artist + ' ' + it.album + ' ' + it.query).toLowerCase().includes(q)
    );
  }}

  if (currentSort === 'artist') {{
    items.sort((a, b) => (a.artist || '').localeCompare(b.artist || ''));
  }} else if (currentSort === 'price-asc') {{
    items.sort((a, b) => (a.lowest_price ?? Infinity) - (b.lowest_price ?? Infinity));
  }} else if (currentSort === 'price-desc') {{
    items.sort((a, b) => (b.lowest_price ?? -1) - (a.lowest_price ?? -1));
  }}

  document.getElementById('grid').innerHTML = items.map(it => renderRow(it, it._origIdx)).join('');
}}

document.querySelectorAll('.sort-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentSort = btn.dataset.sort;
    sortAndRender();
  }});
}});

document.getElementById('search').addEventListener('input', e => {{
  currentFilter = e.target.value;
  sortAndRender();
}});

sortAndRender();
</script>
</body>
</html>"""
    HTML_FILE.write_text(html)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    load_dotenv()
    token = os.environ.get("DISCOGS_TOKEN", "")
    if len(sys.argv) > 1:
        token = sys.argv[1]
    if not token:
        print("Usage: DISCOGS_TOKEN=xxx python3 sync.py")
        print("   or: python3 sync.py YOUR_TOKEN")
        print("   or: put DISCOGS_TOKEN=xxx in .env file")
        sys.exit(1)

    print("=" * 50)
    print("  Vinyl Wishlist Price Tracker")
    print("  M/NM condition · ships from Europe")
    print("=" * 50)

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True}
    )

    items = parse_wishlist()
    print(f"\n  {len(items)} items in wishlist\n")

    results = []
    for i, item in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {item['raw']}")
        result = sync_item(item, token, scraper)
        results.append(result)
        print()

    data = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "items": results,
    }

    PRICES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Saved {PRICES_FILE}")

    generate_html(data)
    print(f"Generated {HTML_FILE}")
    print(f"\nDone! Open {HTML_FILE} in your browser.")


if __name__ == "__main__":
    main()
