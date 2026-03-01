#!/usr/bin/env python3
"""
BD Wishlist Matcher – Web UI
============================
Run:
    pip install flask requests beautifulsoup4
    python app.py
Then open http://localhost:5000

Architecture: background thread + polling.
The search runs in a daemon thread and appends events to a job queue.
The frontend polls /api/search/poll?job_id=X&since=N every second to pick up
new events. This is fully reliable with Flask's dev server — no SSE buffering.
"""

import re
import time
import uuid
import json
import threading
import unicodedata
from collections import defaultdict
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, jsonify

# ── Config ─────────────────────────────────────────────────────────────────────

BEDETHEQUE_BASE  = "https://www.bedetheque.com"
BDGEST_BASE      = "https://www.bdgest.com"
REQUEST_DELAY    = 1.0
DEFAULT_WISHLIST = "https://www.bdgest.com/online/wishlist?IdUser=71812&IdCollection=1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": "https://www.bedetheque.com/",
}

app = Flask(__name__)

# ── In-memory stores ───────────────────────────────────────────────────────────

_wishlist_cache = {}          # cache_key -> (wishlist_by_album_id, series_map)
_jobs           = {}          # job_id    -> {"events": [...], "done": bool}
_jobs_lock      = threading.Lock()

# ── Text helpers ───────────────────────────────────────────────────────────────

def normalise(s):
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def titles_match(a, b):
    if a in b or b in a:
        return True
    stopwords = {"le","la","les","de","du","un","une","et","a","en","l"}
    wa = set(a.split()) - stopwords
    wb = set(b.split()) - stopwords
    return len(wa & wb) >= 2

# ── HTTP helper ────────────────────────────────────────────────────────────────

def get_soup(url, session, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep(2)
    return None

# ── Parsers ────────────────────────────────────────────────────────────────────

def parse_series_from_href(href):
    m = re.search(r"/serie-(\d+)-BD-(.+?)(?:\.html)?$", href)
    return (m.group(1), m.group(2)) if m else (None, None)

def parse_album_id_from_href(href):
    m = re.search(r"-(\d+)\.html$", href)
    return m.group(1) if m else None

def parse_sale_id_from_href(href):
    m = re.search(r"/ventes-BD-(\d+)\.html", href)
    return m.group(1) if m else None

def parse_sale_title(raw):
    m = re.search(r" -(\d+)[^-]*- (.+)$", raw)
    if m:
        return m.group(1), m.group(2).strip()
    m2 = re.search(r" -([A-Z0-9'][^-]*)-\s+(.+)$", raw)
    if m2:
        return m2.group(1), m2.group(2).strip()
    return None, raw

def parse_wishlist_url(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    user_id = qs.get("IdUser", [None])[0]
    coll_id = qs.get("IdCollection", ["1"])[0]
    return user_id, coll_id

# ── Wishlist scraping ──────────────────────────────────────────────────────────

def scrape_wishlist(user_id, coll_id, session):
    wishlist_by_album_id = {}
    series_map = {}
    page = 0

    while True:
        params = {"IdUser": user_id, "IdCollection": coll_id,
                  "Lettre": "", "Page": page}
        soup = get_soup(f"{BDGEST_BASE}/online/wishlist", session, params)
        if soup is None:
            break

        items_this_page = 0
        for li in soup.select("ul > li"):
            s_tag = li.select_one('a[href*="/serie-"]')
            if not s_tag:
                continue
            s_href = s_tag.get("href", "")
            series_id, series_slug = parse_series_from_href(s_href)
            if not series_id:
                continue
            series_name = s_tag.get_text(strip=True)
            series_url  = urljoin(BEDETHEQUE_BASE, s_href)

            album_href = ""
            for a in li.select('a[href*="/BD-"]'):
                href = a.get("href", "")
                if re.search(r"-(\d+)\.html$", href):
                    album_href = href
                    break

            album_id  = parse_album_id_from_href(album_href)
            album_url = urljoin(BEDETHEQUE_BASE, album_href) if album_href else ""

            album_title = ""
            for a in li.select('a[href*="/BD-"]'):
                txt = a.get_text(strip=True)
                if txt and txt.lower() != "acheter":
                    album_title = txt
                    break
            if not album_title:
                raw = li.get_text(" ", strip=True).replace(series_name, "").strip()
                raw = re.sub(r"\s*(Editeur|DL|Etat|Achat le|Acheter)\s*:.*",
                             "", raw, flags=re.IGNORECASE).strip()
                album_title = raw or series_name

            if album_id:
                wishlist_by_album_id[album_id] = {
                    "series_id":   series_id,
                    "series_name": series_name,
                    "series_slug": series_slug,
                    "series_url":  series_url,
                    "album_id":    album_id,
                    "album_title": album_title,
                    "album_url":   album_url,
                }
            if series_id not in series_map:
                series_map[series_id] = {"name": series_name,
                                          "slug": series_slug,
                                          "url":  series_url}
            items_this_page += 1

        if not items_this_page:
            break
        next_page  = page + 1
        next_links = [a for a in soup.select('a[href*="Page="]')
                      if f"Page={next_page}" in a.get("href", "")]
        if not next_links:
            break
        page = next_page
        time.sleep(REQUEST_DELAY)

    return wishlist_by_album_id, series_map

# ── Sales scraping ─────────────────────────────────────────────────────────────

def get_sale_rows_from_search(series_id, series_name, session):
    sales = []
    deb = 0
    search_name = series_name.split(" - ")[0].strip()

    while True:
        params = {
            "RechIdSerie": "", "RechIdAuteur": "",
            "RechSerie": search_name,
            "RechAuteur": "", "RechEditeur": "",
            "RechPrixMin": "", "RechPrixMax": "",
            "RechVendeur": "", "RechPays": "", "RechEtat": "", "RechRecent": "0",
        }
        if deb:
            params["DEB"] = f"__{deb}"

        soup = get_soup(f"{BEDETHEQUE_BASE}/ventes/search", session, params)
        if soup is None:
            break
        table = soup.select_one("table")
        if not table:
            break

        rows_this_page = 0
        for tr in table.select("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            title_link = cells[0].find("a")
            if not title_link:
                continue
            sale_href = title_link.get("href", "")
            sale_id   = parse_sale_id_from_href(sale_href)
            if not sale_id:
                continue

            raw_title = title_link.get_text(strip=True)
            tome_num, album_title_part = parse_sale_title(raw_title)
            eo          = "Oui" if cells[1].get_text(strip=True) else ""
            seller_link = cells[2].find("a")
            vendeur     = (seller_link.get_text(strip=True) if seller_link
                           else cells[2].get_text(strip=True))
            prix  = re.sub(r"[^\d.,]", "", cells[3].get_text(strip=True))
            etat  = ""
            if len(cells) >= 6:
                img = cells[5].find("img")
                if img:
                    etat = img.get("title", "")

            sales.append({
                "sale_id": sale_id,
                "sale_url": urljoin(BEDETHEQUE_BASE, sale_href),
                "raw_title": raw_title,
                "album_title_part": album_title_part,
                "tome_num": tome_num,
                "vendeur": vendeur,
                "prix": prix, "eo": eo, "etat": etat,
                "series_id": series_id, "album_id": None,
            })
            rows_this_page += 1

        if not rows_this_page:
            break
        if not soup.select_one(f'a[href*="DEB=__{deb+1}"]'):
            break
        deb += 1
        time.sleep(REQUEST_DELAY)

    return sales

def resolve_album_id(sale_url, session):
    """Fetch the sale detail page and extract the album ID from the link to the album page.
    Album page hrefs look like: /BD-Asterix-1-...-22941.html or full https://... URLs.
    """
    soup = get_soup(sale_url, session)
    if soup is None:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Match relative paths (/BD-…) or full URLs (https://…/BD-…)
        if re.search(r"[/]BD-", href) and re.search(r"-(\d+)\.html$", href):
            aid = parse_album_id_from_href(href)
            if aid:
                return aid
    return None

def fuzzy_candidate(sale, wanted_items):
    norm = normalise(sale["album_title_part"])
    for wish in wanted_items:
        if titles_match(norm, normalise(wish["album_title"])):
            return wish
    return None

def get_all_sales_for_vendor(vendeur, session):
    """Fetch every listing by a vendor (RechVendeur only). Returns raw sale rows."""
    sales = []
    deb = 0
    while True:
        params = {
            "RechIdSerie": "", "RechIdAuteur": "",
            "RechSerie": "", "RechAuteur": "", "RechEditeur": "",
            "RechPrixMin": "", "RechPrixMax": "",
            "RechVendeur": vendeur,
            "RechPays": "", "RechEtat": "", "RechRecent": "0",
        }
        if deb:
            params["DEB"] = f"__{deb}"

        soup = get_soup(f"{BEDETHEQUE_BASE}/ventes/search", session, params)
        if soup is None:
            break
        table = soup.select_one("table")
        if not table:
            break

        rows_this_page = 0
        for tr in table.select("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            title_link = cells[0].find("a")
            if not title_link:
                continue
            sale_href = title_link.get("href", "")
            sale_id   = parse_sale_id_from_href(sale_href)
            if not sale_id:
                continue

            raw_title = title_link.get_text(strip=True)
            tome_num, album_title_part = parse_sale_title(raw_title)
            eo   = "Oui" if cells[1].get_text(strip=True) else ""
            prix = re.sub(r"[^\d.,]", "", cells[3].get_text(strip=True))
            etat = ""
            if len(cells) >= 6:
                img = cells[5].find("img")
                if img:
                    etat = img.get("title", "")

            sales.append({
                "sale_id":          sale_id,
                "sale_url":         urljoin(BEDETHEQUE_BASE, sale_href),
                "raw_title":        raw_title,
                "album_title_part": album_title_part,
                "tome_num":         tome_num,
                "vendeur":          vendeur,
                "prix": prix, "eo": eo, "etat": etat,
                "album_id": None,
            })
            rows_this_page += 1

        if not rows_this_page:
            break
        if not soup.select_one(f'a[href*="DEB=__{deb+1}"]'):
            break
        deb += 1
        time.sleep(REQUEST_DELAY)

    return sales

# ── Job helpers ────────────────────────────────────────────────────────────────

def job_emit(job_id, event_type, data):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["events"].append({"type": event_type, "data": data})

def job_finish(job_id):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["done"] = True

# ── Background search worker ───────────────────────────────────────────────────

def run_search(job_id, cache_key, priority_ids, wishlist_url_str, do_bonus):
    try:
        session = requests.Session()

        if cache_key in _wishlist_cache:
            wishlist_by_album_id, series_map = _wishlist_cache[cache_key]
        else:
            user_id, coll_id = parse_wishlist_url(wishlist_url_str)
            if not user_id:
                job_emit(job_id, "error", {"msg": "Invalid wishlist URL"})
                return
            job_emit(job_id, "status", {"msg": "Scraping wishlist…"})
            wishlist_by_album_id, series_map = scrape_wishlist(user_id, coll_id, session)
            _wishlist_cache[f"{user_id}:{coll_id}"] = (wishlist_by_album_id, series_map)

        if not wishlist_by_album_id:
            job_emit(job_id, "error", {"msg": "Wishlist is empty or could not be loaded."})
            return

        priority_series = {sid: info for sid, info in series_map.items()
                           if sid in priority_ids}
        bonus_series    = {sid: info for sid, info in series_map.items()
                           if sid not in priority_ids} if do_bonus else {}

        wishlist_by_series = defaultdict(list)
        for aid, item in wishlist_by_album_id.items():
            wishlist_by_series[item["series_id"]].append(item)

        total = len(priority_series)
        done  = 0

        sellers_found           = set()
        seller_priority_matches = defaultdict(list)
        seller_bonus_matches    = defaultdict(list)

        job_emit(job_id, "progress", {
            "done": 0, "total": total,
            "msg": f"Starting search across {total} priority series…"
        })

        # ── Phase A: Priority series (same as before) ──────────────────────────
        for series_id, info in priority_series.items():
            wanted = wishlist_by_series.get(series_id, [])
            if not wanted:
                done += 1
                job_emit(job_id, "progress", {
                    "done": done, "total": total,
                    "msg": f"Skipping {info['name']} (no wishlist albums)"
                })
                continue

            job_emit(job_id, "progress", {
                "done": done, "total": total,
                "msg": f"Searching: {info['name']}…"
            })

            sales      = get_sale_rows_from_search(series_id, info["name"], session)
            candidates = []
            for s in sales:
                m = fuzzy_candidate(s, wanted)
                if m:
                    candidates.append((s, m))

            job_emit(job_id, "progress", {
                "done": done, "total": total,
                "msg": f"{info['name']}: {len(sales)} listings, {len(candidates)} candidates"
            })

            for sale, _ in candidates:
                time.sleep(REQUEST_DELAY)
                job_emit(job_id, "status", {"msg": f"Resolving: {sale['raw_title'][:60]}…"})
                album_id = resolve_album_id(sale["sale_url"], session)
                if not album_id:
                    job_emit(job_id, "status", {"msg": f"  → no album ID found on page: {sale['sale_url']}"})
                    continue   # could not resolve — skip rather than guess

                confirmed = wishlist_by_album_id.get(album_id)
                if not confirmed:
                    job_emit(job_id, "status", {"msg": f"  → album {album_id} not on wishlist"})
                    continue   # album exists but is not on the wishlist

                job_emit(job_id, "status", {"msg": f"  → MATCH: album {album_id} = {confirmed['album_title']}"})
                sale["album_id"] = album_id
                vendeur = sale["vendeur"]
                sellers_found.add(vendeur)
                match = {
                    "sale_url":    sale["sale_url"],
                    "album_title": sale["raw_title"],
                    "series_name": info["name"],
                    "wish_title":  confirmed["album_title"],
                    "wish_url":    confirmed.get("album_url", ""),
                    "prix":        sale["prix"],
                    "etat":        sale["etat"],
                    "eo":          sale["eo"],
                    "album_id":    sale["album_id"],
                    "vendeur":     vendeur,
                    "priority":    True,
                }
                seller_priority_matches[vendeur].append(match)
                job_emit(job_id, "match", {
                    "vendeur":  vendeur,
                    "match":    match,
                    "priority": True,
                    "seller_priority_count": len(seller_priority_matches[vendeur]),
                    "seller_bonus_count":    len(seller_bonus_matches[vendeur]),
                })

            done += 1
            job_emit(job_id, "progress", {
                "done": done, "total": total,
                "msg": f"Done: {info['name']}"
            })

        # ── Phase B: Bonus search ──────────────────────────────────────────────
        # For each vendor found in Phase A, fetch all their listings once, then
        # filter in Python to rows that belong to a non-priority wishlist series,
        # then resolve the exact album ID and confirm against the wishlist.
        if do_bonus and sellers_found and bonus_series:

            # album_ids already confirmed as priority — don't double-report
            confirmed_album_ids = {
                m["album_id"]
                for matches in seller_priority_matches.values()
                for m in matches
                if m["album_id"]
            }

            # Build a normalised lookup: series_norm -> (sid, info, [wanted_items])
            # so we can quickly check whether a vendor listing belongs to a bonus series
            bonus_series_norm = {}
            for sid2, info2 in bonus_series.items():
                wanted2 = wishlist_by_series.get(sid2, [])
                if wanted2:
                    key = normalise(info2["name"].split(" - ")[0])
                    bonus_series_norm[key] = (sid2, info2, wanted2)

            if bonus_series_norm:
                vendor_list = sorted(sellers_found)
                job_emit(job_id, "progress", {
                    "done": 0, "total": len(vendor_list),
                    "msg": f"Bonus: scanning {len(vendor_list)} seller(s) for non-priority wishlist series…"
                })

                for v_idx, vendeur in enumerate(vendor_list):
                    job_emit(job_id, "progress", {
                        "done": v_idx, "total": len(vendor_list),
                        "msg": f"Bonus: fetching all listings for {vendeur}…"
                    })

                    # One fetch of all vendor listings
                    vendor_sales = get_all_sales_for_vendor(vendeur, session)

                    # Group vendor listings by normalised series name
                    vendor_by_series = defaultdict(list)
                    for vsale in vendor_sales:
                        # series name is everything before the " -tome- " token
                        series_part = re.split(r"\s+-[^-]+-\s+", vsale["raw_title"])[0].strip()
                        vendor_by_series[normalise(series_part)].append(vsale)

                    # For each bonus wishlist series, check if vendor has any of it
                    for series_norm_key, (sid2, info2, wanted2) in bonus_series_norm.items():
                        vendor_listings = vendor_by_series.get(series_norm_key, [])
                        if not vendor_listings:
                            continue

                        # Fuzzy-filter to candidates that match a wishlist album title
                        candidates2 = []
                        for vsale in vendor_listings:
                            m = fuzzy_candidate(vsale, wanted2)
                            if m:
                                candidates2.append(vsale)

                        # Phase 2: resolve exact album ID, confirm against wishlist
                        for sale2 in candidates2:
                            time.sleep(REQUEST_DELAY)
                            album_id2 = resolve_album_id(sale2["sale_url"], session)
                            if not album_id2 or album_id2 in confirmed_album_ids:
                                continue

                            confirmed2 = wishlist_by_album_id.get(album_id2)
                            if confirmed2:
                                confirmed_album_ids.add(album_id2)
                                bonus = {
                                    "sale_url":    sale2["sale_url"],
                                    "album_title": sale2["raw_title"],
                                    "series_name": info2["name"],
                                    "wish_title":  confirmed2["album_title"],
                                    "wish_url":    confirmed2.get("album_url", ""),
                                    "prix":        sale2["prix"],
                                    "etat":        sale2["etat"],
                                    "eo":          sale2["eo"],
                                    "album_id":    album_id2,
                                    "vendeur":     vendeur,
                                    "priority":    False,
                                }
                                seller_bonus_matches[vendeur].append(bonus)
                                job_emit(job_id, "match", {
                                    "vendeur":  vendeur,
                                    "match":    bonus,
                                    "priority": False,
                                    "seller_priority_count": len(seller_priority_matches[vendeur]),
                                    "seller_bonus_count":    len(seller_bonus_matches[vendeur]),
                                })

                    job_emit(job_id, "progress", {
                        "done": v_idx + 1, "total": len(vendor_list),
                        "msg": f"Bonus done: {vendeur}"
                    })

        total_priority = sum(len(v) for v in seller_priority_matches.values())
        total_bonus    = sum(len(v) for v in seller_bonus_matches.values())
        job_emit(job_id, "done", {
            "total_priority": total_priority,
            "total_bonus":    total_bonus,
            "sellers":        len(seller_priority_matches),
        })

    except Exception as e:
        job_emit(job_id, "error", {"msg": f"Server error: {e}"})
    finally:
        job_finish(job_id)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_PAGE, default_wishlist=DEFAULT_WISHLIST)

@app.route("/api/wishlist")
def api_wishlist():
    url = request.args.get("url", DEFAULT_WISHLIST)
    user_id, coll_id = parse_wishlist_url(url)
    if not user_id:
        return jsonify({"error": "Could not parse user ID from URL"}), 400

    session = requests.Session()
    wishlist_by_album_id, series_map = scrape_wishlist(user_id, coll_id, session)

    counts = defaultdict(int)
    for item in wishlist_by_album_id.values():
        counts[item["series_id"]] += 1

    series_list = sorted(
        [{"id": sid, "name": info["name"], "count": counts[sid]}
         for sid, info in series_map.items()],
        key=lambda x: x["name"].lower()
    )

    cache_key = f"{user_id}:{coll_id}"
    _wishlist_cache[cache_key] = (wishlist_by_album_id, series_map)

    return jsonify({
        "series": series_list,
        "total_albums": len(wishlist_by_album_id),
        "cache_key": cache_key,
    })

@app.route("/api/search/start", methods=["POST"])
def api_search_start():
    """Spawn a background search thread, return job_id immediately."""
    body         = request.get_json(force=True)
    cache_key    = body.get("cache_key", "")
    priority_ids = set(body.get("priority", []))
    wishlist_url = body.get("url", DEFAULT_WISHLIST)
    do_bonus     = bool(body.get("do_bonus", False))

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"events": [], "done": False}

    t = threading.Thread(
        target=run_search,
        args=(job_id, cache_key, priority_ids, wishlist_url, do_bonus),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})

@app.route("/api/search/poll")
def api_search_poll():
    """Return all new events since index `since`."""
    job_id = request.args.get("job_id", "")
    since  = int(request.args.get("since", 0))

    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Unknown job"}), 404

    new_events = job["events"][since:]
    return jsonify({
        "events": new_events,
        "next":   since + len(new_events),
        "done":   job["done"],
    })

# ── HTML + JS ──────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BD Wishlist Matcher</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f14;--surface:#151820;--surface2:#1d2130;--border:#2a2f45;
  --accent:#e84040;--accent2:#f5a623;--bonus:#4a9eff;
  --text:#e8eaf2;--muted:#6b7094;--success:#3ddc84;
  --font-head:'Syne',sans-serif;--font-mono:'DM Mono',monospace;
  --radius:10px;--glow:0 0 0 1px var(--accent),0 0 24px rgba(232,64,64,.15);
}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--font-mono);
  font-size:14px;line-height:1.6;min-height:100vh;}
.wrap{max-width:1100px;margin:0 auto;padding:2rem 1.5rem 5rem}
header{display:flex;align-items:baseline;gap:1rem;
  border-bottom:1px solid var(--border);padding-bottom:1.5rem;margin-bottom:2.5rem;}
header h1{font-family:var(--font-head);font-size:clamp(1.6rem,4vw,2.4rem);
  font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
header .sub{font-size:.75rem;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;}
.step{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.75rem 2rem;margin-bottom:1.5rem;transition:border-color .25s;}
.step.active{border-color:var(--accent);box-shadow:var(--glow);}
.step-header{display:flex;align-items:center;gap:.85rem;margin-bottom:1.25rem;}
.step-num{width:28px;height:28px;border-radius:50%;background:var(--accent);color:#fff;
  font-family:var(--font-head);font-weight:700;font-size:.85rem;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.step-num.done{background:var(--success);}
.step-title{font-family:var(--font-head);font-weight:700;font-size:1.05rem;letter-spacing:-.01em;}
.input-row{display:flex;gap:.75rem;flex-wrap:wrap;}
input[type=text]{flex:1;min-width:260px;background:var(--surface2);
  border:1px solid var(--border);border-radius:6px;color:var(--text);
  font-family:var(--font-mono);font-size:.85rem;padding:.6rem 1rem;outline:none;
  transition:border-color .2s,box-shadow .2s;}
input[type=text]:focus{border-color:var(--accent);box-shadow:var(--glow);}
button{background:var(--accent);color:#fff;border:none;border-radius:6px;
  font-family:var(--font-head);font-weight:700;font-size:.85rem;
  letter-spacing:.05em;text-transform:uppercase;padding:.6rem 1.4rem;
  cursor:pointer;transition:opacity .2s,transform .1s;white-space:nowrap;}
button:hover{opacity:.85;}button:active{transform:scale(.97);}
button:disabled{opacity:.4;cursor:not-allowed;}
button.secondary{background:var(--surface2);border:1px solid var(--border);color:var(--text);}
button.secondary:hover{border-color:var(--accent);color:var(--accent);}
.series-toolbar{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-bottom:1rem;}
.series-toolbar small{color:var(--muted);margin-left:auto;font-size:.78rem;}
.series-filter{flex:1;min-width:180px;background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;color:var(--text);font-family:var(--font-mono);font-size:.8rem;
  padding:.45rem .8rem;outline:none;}
.series-filter:focus{border-color:var(--accent);}
.series-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
  gap:.45rem;max-height:340px;overflow-y:auto;padding-right:.25rem;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;}
.series-item{display:flex;align-items:center;gap:.6rem;background:var(--surface2);
  border:1px solid var(--border);border-radius:6px;padding:.5rem .7rem;
  cursor:pointer;user-select:none;transition:border-color .15s,background .15s;}
.series-item:hover{border-color:var(--accent);background:#1f2438;}
.series-item input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px;
  cursor:pointer;flex-shrink:0;}
.series-item label{cursor:pointer;font-size:.82rem;flex:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.series-item .badge{font-size:.7rem;color:var(--muted);flex-shrink:0;}
.progress-bar-wrap{background:var(--surface2);border-radius:999px;
  height:8px;overflow:hidden;margin:1rem 0 .5rem;}
.progress-bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));
  border-radius:999px;width:0%;transition:width .3s ease;}
.progress-label{font-size:.78rem;color:var(--muted);}
.status-log{font-size:.78rem;color:var(--muted);font-style:italic;
  min-height:1.4em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  margin-top:.35rem;}
.error-box{margin-top:.75rem;padding:.6rem 1rem;
  background:rgba(232,64,64,.12);border:1px solid var(--accent);
  border-radius:6px;font-size:.82rem;color:var(--accent);}
.results-header{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:.5rem;margin-bottom:1.25rem;margin-top:1.5rem;}
.results-count{font-family:var(--font-head);font-size:1rem;font-weight:600;}
.results-count em{color:var(--accent);}
.legend{display:flex;gap:1rem;font-size:.75rem;color:var(--muted);}
.legend span{display:flex;align-items:center;gap:.35rem;}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.dot.priority{background:var(--accent);}
.dot.bonus{background:var(--bonus);}
.seller-block{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;margin-bottom:1rem;
  animation:slideIn .3s ease;}
@keyframes slideIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.seller-head{display:flex;align-items:center;gap:.75rem;background:var(--surface2);
  padding:.75rem 1.1rem;border-bottom:1px solid var(--border);}
.seller-name{font-family:var(--font-head);font-weight:700;font-size:.95rem;flex:1;}
.seller-name a{color:var(--text);text-decoration:none;}
.seller-name a:hover{color:var(--accent);}
.count-chip{font-size:.72rem;font-family:var(--font-mono);
  padding:2px 9px;border-radius:999px;font-weight:500;}
.count-chip.p{background:rgba(232,64,64,.18);color:var(--accent);}
.count-chip.b{background:rgba(74,158,255,.15);color:var(--bonus);}
.match-list{list-style:none;}
.match-item{display:grid;grid-template-columns:1fr auto auto auto;
  align-items:center;gap:.5rem 1rem;padding:.6rem 1.1rem;
  border-bottom:1px solid rgba(42,47,69,.6);transition:background .15s;
  animation:fadeIn .25s ease;}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.match-item:last-child{border-bottom:none;}
.match-item:hover{background:var(--surface2);}
.match-title a{font-size:.83rem;text-decoration:none;font-weight:500;}
.match-title a.priority-link{color:var(--accent);}
.match-title a.bonus-link{color:var(--bonus);}
.match-title a:hover{text-decoration:underline;}
.match-series{font-size:.73rem;color:var(--muted);margin-top:1px;}
.match-price{font-family:var(--font-head);font-weight:700;
  font-size:.85rem;color:var(--accent2);white-space:nowrap;}
.match-etat{font-size:.72rem;color:var(--muted);text-align:right;}
.match-eo{font-size:.68rem;font-weight:700;letter-spacing:.08em;
  color:var(--accent2);text-align:center;visibility:hidden;}
.match-eo.show{visibility:visible;}
.match-eo-label{background:rgba(245,166,35,.15);border:1px solid rgba(245,166,35,.3);
  border-radius:4px;padding:1px 5px;}
.hidden{display:none!important;}
.bonus-toggle{display:flex;flex-direction:column;cursor:pointer;gap:0;color:var(--text);font-size:.82rem;}
.bonus-toggle input[type=checkbox]{accent-color:var(--bonus);width:14px;height:14px;margin-right:.4rem;vertical-align:middle;}
.bonus-toggle span:first-of-type{font-weight:600;}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;
  animation:spin .7s linear infinite;vertical-align:middle;margin-right:.4rem;}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>BD Wishlist Matcher</h1>
    <span class="sub">bedetheque · annonces</span>
  </header>

  <!-- Step 1 -->
  <div class="step active" id="step1">
    <div class="step-header">
      <div class="step-num" id="s1num">1</div>
      <div class="step-title">Wishlist URL</div>
    </div>
    <div class="input-row">
      <input type="text" id="wishlist-url" value="{{ default_wishlist }}"
             placeholder="https://www.bdgest.com/online/wishlist?IdUser=…">
      <button id="btn-load">Load Wishlist</button>
    </div>
    <div id="load-status" style="margin-top:.75rem;font-size:.78rem;color:var(--muted);"></div>
  </div>

  <!-- Step 2 -->
  <div class="step" id="step2">
    <div class="step-header">
      <div class="step-num" id="s2num">2</div>
      <div class="step-title">Select Priority Series</div>
    </div>
    <div id="series-panel" class="hidden">
      <div class="series-toolbar">
        <input class="series-filter" type="text" id="series-search" placeholder="Filter series…">
        <button class="secondary" id="btn-select-all">Select All</button>
        <button class="secondary" id="btn-select-none">Select None</button>
        <small id="series-sel-count"></small>
      </div>
      <div class="series-grid" id="series-grid"></div>
      <div style="margin-top:1.25rem;display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap;">
        <button id="btn-search">Search Sales</button>
        <label class="bonus-toggle" title="For each seller found in priority results, also search all their other non-priority wishlist series using the same exact-match criteria">
          <input type="checkbox" id="chk-bonus">
          <span>Also search bonus series</span>
          <span style="font-size:.72rem;color:var(--muted);display:block;margin-top:1px;">
            Checks non-priority wishlist series from discovered sellers (slower)
          </span>
        </label>
      </div>
    </div>
    <div id="step2-placeholder" style="color:var(--muted);font-size:.85rem;">
      Load a wishlist first.
    </div>
  </div>

  <!-- Step 3 -->
  <div class="step" id="step3">
    <div class="step-header">
      <div class="step-num" id="s3num">3</div>
      <div class="step-title">Search Results</div>
    </div>
    <div id="progress-panel" class="hidden">
      <div class="progress-bar-wrap">
        <div class="progress-bar" id="progress-bar"></div>
      </div>
      <div class="progress-label"><span id="progress-text"></span></div>
      <div class="status-log" id="status-log"></div>
      <div id="error-box" class="error-box hidden"></div>
    </div>
    <div id="results-panel" class="hidden">
      <div class="results-header">
        <div class="results-count">
          <em id="total-priority-count">0</em> priority &nbsp;·&nbsp;
          <em id="total-bonus-count">0</em> bonus matches
        </div>
        <div class="legend">
          <span><span class="dot priority"></span>Priority series</span>
          <span><span class="dot bonus"></span>Other wishlist</span>
        </div>
      </div>
      <div id="sellers-container"></div>
    </div>
    <div id="step3-placeholder" style="color:var(--muted);font-size:.85rem;">
      Configure your search above.
    </div>
  </div>
  <!-- Debug panel -->
  <div class="step" style="margin-top:1rem;">
    <div class="step-header" style="margin-bottom:.5rem;">
      <div class="step-title" style="font-size:.85rem;color:var(--muted);">Debug Log</div>
      <button class="secondary" style="font-size:.7rem;padding:.3rem .7rem;margin-left:auto;" onclick="document.getElementById('debug-log').innerHTML=''">Clear</button>
    </div>
    <div id="debug-log" style="font-family:var(--font-mono);font-size:.72rem;color:var(--muted);max-height:200px;overflow-y:auto;line-height:1.8;"></div>
  </div>

</div>

<script>
// ── Debug log (visible in page) ──────────────────────────────────────────
function dbg(msg) {
  console.log('[BD]', msg);
  const el = document.getElementById('debug-log');
  if (el) {
    const line = document.createElement('div');
    line.textContent = new Date().toISOString().slice(11,19) + ' ' + msg;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }
}

const COOKIE_KEY = 'bd_priority_series';

function show(id){ document.getElementById(id).classList.remove('hidden'); }
function hide(id){ document.getElementById(id).classList.add('hidden'); }
function setActive(n){
  [1,2,3].forEach(i=>document.getElementById('step'+i).classList.toggle('active',i===n));
}
function saveCookie(ids){
  document.cookie=`${COOKIE_KEY}=${encodeURIComponent(JSON.stringify([...ids]))};path=/;max-age=${60*60*24*180}`;
}
function loadCookie(){
  const m=document.cookie.split(';').find(c=>c.trim().startsWith(COOKIE_KEY+'='));
  if(!m)return null;
  try{return new Set(JSON.parse(decodeURIComponent(m.split('=').slice(1).join('='))));}
  catch{return null;}
}

// ── State ─────────────────────────────────────────────────────────────────
let cacheKey='', jobId=null, pollSince=0, pollTimer=null;
let resultsMap={}, totalPriority=0, totalBonus=0;

// ── Step 1 ────────────────────────────────────────────────────────────────
document.getElementById('btn-load').addEventListener('click', async ()=>{
  const btn=document.getElementById('btn-load');
  const st=document.getElementById('load-status');
  btn.disabled=true;
  st.innerHTML='<span class="spinner"></span>Scraping wishlist…';
  hide('series-panel'); show('step2-placeholder');

  try{
    const url=document.getElementById('wishlist-url').value.trim();
    const res=await fetch('/api/wishlist?url='+encodeURIComponent(url));
    const data=await res.json();
    if(data.error)throw new Error(data.error);
    cacheKey=data.cache_key;
    dbg('Wishlist loaded. cache_key=' + cacheKey + ' series=' + data.series.length);
    st.textContent=`✓  ${data.total_albums} albums across ${data.series.length} series loaded.`;
    renderGrid(data.series);
    show('series-panel'); hide('step2-placeholder');
    setActive(2);
    document.getElementById('s1num').classList.add('done');
  }catch(e){
    st.textContent='✗ Error: '+e.message;
  }
  btn.disabled=false;
});

// ── Step 2 ────────────────────────────────────────────────────────────────
function renderGrid(list){
  const grid=document.getElementById('series-grid');
  const saved=loadCookie();
  grid.innerHTML='';
  list.forEach(s=>{
    const checked=saved?saved.has(s.id):true;
    const row=document.createElement('div');
    row.className='series-item';
    row.dataset.name=s.name.toLowerCase();
    row.dataset.id=s.id;

    const cb=document.createElement('input');
    cb.type='checkbox'; cb.id='cb_'+s.id;
    cb.dataset.id=s.id; cb.checked=checked;
    cb.addEventListener('change',updateCount);

    const lbl=document.createElement('label');
    lbl.htmlFor='cb_'+s.id; lbl.title=s.name; lbl.textContent=s.name;

    const badge=document.createElement('span');
    badge.className='badge'; badge.textContent=s.count;

    row.appendChild(cb); row.appendChild(lbl); row.appendChild(badge);
    row.addEventListener('click',e=>{
      if(e.target===cb||e.target===lbl)return;
      cb.checked=!cb.checked; updateCount();
    });
    grid.appendChild(row);
  });
  updateCount();
}

function updateCount(){
  const grid=document.getElementById('series-grid');
  const all=grid.querySelectorAll('input[type=checkbox]');
  const chk=grid.querySelectorAll('input[type=checkbox]:checked');
  document.getElementById('series-sel-count').textContent=`${chk.length} / ${all.length} selected`;
}

document.getElementById('series-search').addEventListener('input',function(){
  const q=this.value.toLowerCase();
  document.getElementById('series-grid').querySelectorAll('.series-item').forEach(el=>{
    el.style.display=el.dataset.name.includes(q)?'':'none';
  });
});
document.getElementById('btn-select-all').addEventListener('click',()=>{
  document.getElementById('series-grid').querySelectorAll('input[type=checkbox]').forEach(cb=>cb.checked=true);
  updateCount();
});
document.getElementById('btn-select-none').addEventListener('click',()=>{
  document.getElementById('series-grid').querySelectorAll('input[type=checkbox]').forEach(cb=>cb.checked=false);
  updateCount();
});

// ── Step 3: start + poll ──────────────────────────────────────────────────
document.getElementById('btn-search').addEventListener('click', async ()=>{
  const grid=document.getElementById('series-grid');
  const ids=[...grid.querySelectorAll('input[type=checkbox]:checked')].map(cb=>cb.dataset.id);
  if(!ids.length){alert('Select at least one series.');return;}
  saveCookie(ids);

  if(pollTimer){clearInterval(pollTimer);pollTimer=null;}
  resultsMap={}; totalPriority=0; totalBonus=0; jobId=null; pollSince=0;
  document.getElementById('sellers-container').innerHTML='';
  document.getElementById('total-priority-count').textContent='0';
  document.getElementById('total-bonus-count').textContent='0';
  document.getElementById('progress-bar').style.width='0%';
  document.getElementById('progress-text').textContent='';
  document.getElementById('status-log').textContent='';
  hide('error-box');

  show('progress-panel'); show('results-panel'); hide('step3-placeholder');
  setActive(3);
  document.getElementById('s2num').classList.add('done');

  const wishlistUrl=document.getElementById('wishlist-url').value.trim();
  const doBonus=document.getElementById('chk-bonus').checked;
  dbg('Starting search. cacheKey=' + cacheKey + ' ids=' + ids.length + ' bonus=' + doBonus);
  try{
    const res=await fetch('/api/search/start',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cache_key:cacheKey,priority:ids,url:wishlistUrl,do_bonus:doBonus})
    });
    const data=await res.json();
    dbg('search/start response: ' + JSON.stringify(data));
    if(data.error)throw new Error(data.error);
    jobId=data.job_id; pollSince=0;
    dbg('Poll timer starting for job ' + jobId);
    pollTimer=setInterval(poll,1000);
  }catch(e){
    dbg('ERROR starting: ' + e.message);
    showErr('Failed to start search: '+e.message);
  }
});

async function poll(){
  if(!jobId)return;
  try{
    const res=await fetch(`/api/search/poll?job_id=${jobId}&since=${pollSince}`);
    const data=await res.json();
    if(data.error){dbg('poll error: '+data.error);showErr(data.error);stopPoll();return;}
    dbg(`poll: ${data.events.length} new events, next=${data.next}, done=${data.done}`);
    data.events.forEach(handleEvent);
    pollSince=data.next;
    if(data.done)stopPoll();
  }catch(e){
    dbg('poll exception: '+e.message);
    showErr('Poll error: '+e.message); stopPoll();
  }
}

function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}
function showErr(msg){
  const b=document.getElementById('error-box');
  b.textContent=msg; show('error-box');
  document.getElementById('status-log').textContent='⚠ '+msg;
}

function handleEvent(ev){
  dbg('event: ' + ev.type + ' ' + JSON.stringify(ev.data).slice(0,60));
  const d=ev.data;
  switch(ev.type){
    case 'progress':
      if(d.total>0){
        document.getElementById('progress-bar').style.width=(d.done/d.total*100)+'%';
        document.getElementById('progress-text').textContent=`${d.done} / ${d.total} series`;
      }
      if(d.msg)document.getElementById('status-log').textContent=d.msg;
      break;
    case 'status':
      document.getElementById('status-log').textContent=d.msg;
      break;
    case 'match':
      addMatch(d.vendeur,d.match,d.priority,d.seller_priority_count,d.seller_bonus_count);
      break;
    case 'done':
      document.getElementById('progress-bar').style.width='100%';
      document.getElementById('status-log').textContent=
        `Search complete — ${d.total_priority} priority, ${d.total_bonus} bonus matches across ${d.sellers} seller(s).`;
      document.getElementById('s3num').classList.add('done');
      break;
    case 'error':
      showErr(d.msg);
      break;
  }
}

// ── Result rendering ──────────────────────────────────────────────────────
function addMatch(vendeur,match,isPriority,priCount,bonusCount){
  if(!resultsMap[vendeur])resultsMap[vendeur]={priority:[],bonus:[],el:null};
  if(isPriority){resultsMap[vendeur].priority.push(match);totalPriority++;}
  else{resultsMap[vendeur].bonus.push(match);totalBonus++;}
  document.getElementById('total-priority-count').textContent=totalPriority;
  document.getElementById('total-bonus-count').textContent=totalBonus;

  let block=resultsMap[vendeur].el;
  if(!block){
    block=makeBlock(vendeur);
    resultsMap[vendeur].el=block;
    insertSorted(block,vendeur);
  } else {
    block.querySelector('[data-chip=p]').textContent=`${priCount} priority`;
    block.querySelector('[data-chip=b]').textContent=`${bonusCount} bonus`;
    block.dataset.total=priCount+bonusCount;
    reSort(block,vendeur);
  }
  appendRow(block,match,isPriority);
}

function makeBlock(vendeur){
  const vUrl=`https://www.bedetheque.com/ventes/search?RechVendeur=${encodeURIComponent(vendeur)}`;
  const block=document.createElement('div');
  block.className='seller-block'; block.dataset.total=1;

  const head=document.createElement('div'); head.className='seller-head';

  const nm=document.createElement('div'); nm.className='seller-name';
  const a=document.createElement('a'); a.href=vUrl; a.target='_blank'; a.textContent=vendeur;
  nm.append('👤 ',a);

  const cp=document.createElement('span'); cp.className='count-chip p'; cp.dataset.chip='p'; cp.textContent='0 priority';
  const cb=document.createElement('span'); cb.className='count-chip b'; cb.dataset.chip='b'; cb.textContent='0 bonus';

  head.appendChild(nm); head.appendChild(cp); head.appendChild(cb);
  const list=document.createElement('ul'); list.className='match-list';
  block.appendChild(head); block.appendChild(list);
  return block;
}

function insertSorted(block,vendeur){
  const total=resultsMap[vendeur].priority.length+resultsMap[vendeur].bonus.length;
  block.dataset.total=total;
  const c=document.getElementById('sellers-container');
  const others=[...c.querySelectorAll('.seller-block')];
  let placed=false;
  for(const b of others){
    if(b===block)continue;
    if(parseInt(b.dataset.total||0)<total){c.insertBefore(block,b);placed=true;break;}
  }
  if(!placed)c.appendChild(block);
}

function reSort(block,vendeur){
  const total=resultsMap[vendeur].priority.length+resultsMap[vendeur].bonus.length;
  block.dataset.total=total;
  const c=document.getElementById('sellers-container');
  const others=[...c.querySelectorAll('.seller-block')];
  let placed=false;
  for(const b of others){
    if(b===block)continue;
    if(parseInt(b.dataset.total||0)<total){c.insertBefore(block,b);placed=true;break;}
  }
  if(!placed)c.appendChild(block);
}

function appendRow(block,match,isPriority){
  const li=document.createElement('li'); li.className='match-item';

  const td=document.createElement('div'); td.className='match-title';
  const a=document.createElement('a');
  a.href=match.sale_url; a.target='_blank';
  a.className=isPriority?'priority-link':'bonus-link';
  a.textContent=match.album_title;
  const sd=document.createElement('div'); sd.className='match-series'; sd.textContent=match.series_name;
  td.appendChild(a); td.appendChild(sd);

  const pr=document.createElement('div'); pr.className='match-price';
  pr.textContent=match.prix?match.prix+' €':'—';

  const et=document.createElement('div'); et.className='match-etat';
  et.textContent=match.etat||'—';

  const eo=document.createElement('div');
  eo.className='match-eo'+(match.eo==='Oui'?' show':'');
  const es=document.createElement('span'); es.className='match-eo-label'; es.textContent='EO';
  eo.appendChild(es);

  li.appendChild(td); li.appendChild(pr); li.appendChild(et); li.appendChild(eo);
  block.querySelector('.match-list').appendChild(li);
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    print("\n  BD Wishlist Matcher UI")
    print("  ─────────────────────────────")
    print("  Open: http://localhost:5000\n")
    app.run(debug=False, threaded=True, port=5000, use_reloader=False)
