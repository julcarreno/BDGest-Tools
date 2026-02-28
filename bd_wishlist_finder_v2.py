#!/usr/bin/env python3
"""
BD Wishlist vs Bedetheque Sales Matcher  (v5 - two-phase matching)
================================================================
Scrapes jcarreno's BDGest wishlist, then searches for sale listings
ONLY for the series listed in a priority file.

Matching is done in two phases to minimise HTTP requests:

  Phase 1 (fast): For each priority series, fetch all pages of the
  Bedetheque sales search (/ventes/search?RechSerie=...). Each row
  already contains the album title embedded in the listing label
  (e.g. "Astérix -2b1966- La serpe d'or"). This is fuzzy-matched
  against the wishlist with no extra HTTP requests.

  Phase 2 (precise): Only for rows that passed the fuzzy filter,
  fetch the individual sale page (/ventes-BD-NNNN.html) to extract
  the exact album ID and confirm it appears in the wishlist. This
  eliminates false positives while keeping the total number of
  detail-page fetches small.

Results are grouped by seller.

Usage:
    1. Create a plain text file (default: priority_series.txt) with
       one series name per line, e.g.:

           Asterix
           Aama
           Akira

       Names are matched case-insensitively and accent-insensitively
       against your wishlist series. Partial matches are supported
       (e.g. "Akira" matches "Akira - Glenat cartonnes en couleur").

    2. Run:
           pip install requests beautifulsoup4 --break-system-packages
           python bd_wishlist_matcher.py
       or specify a custom priority file:
           python bd_wishlist_matcher.py my_priorities.txt

Outputs:
    results.html  — clickable HTML report grouped by seller
    results.csv   — flat CSV for spreadsheet use
    (+ console summary)
"""

import re
import csv
import time
import html
import sys
import unicodedata
from collections import defaultdict
from urllib.parse import urljoin
from pathlib import Path

# ── Auto-install dependencies ──────────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing required libraries...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "requests", "beautifulsoup4",
                           "--break-system-packages", "-q"])
    import requests
    from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────

WISHLIST_USER_ID     = "71812"
WISHLIST_COLLECTION  = "1"
WISHLIST_BASE_URL    = "https://www.bdgest.com/online/wishlist"
BEDETHEQUE_BASE      = "https://www.bedetheque.com"
DEFAULT_PRIORITY_FILE = "priority_series.txt"

# Polite delay between HTTP requests (seconds).
REQUEST_DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.bedetheque.com/",
}

# ── Text helpers ───────────────────────────────────────────────────────────────

def normalise(s):
    """Lowercase, strip accents, collapse punctuation to spaces."""
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def titles_match(sale_norm, wish_norm):
    """True if two normalised album titles likely refer to the same album."""
    if wish_norm in sale_norm or sale_norm in wish_norm:
        return True
    t_s = re.search(r"\b(\d+)\b", sale_norm)
    t_w = re.search(r"\b(\d+)\b", wish_norm)
    if t_s and t_w and t_s.group(1) == t_w.group(1):
        stopwords = {"le","la","les","de","du","un","une","et","a","en","l"}
        ws = set(sale_norm.split()) - stopwords
        ww = set(wish_norm.split()) - stopwords
        if len(ws & ww) >= 2:
            return True
    return False

# ── HTTP helper ────────────────────────────────────────────────────────────────

def get_soup(url, session, params=None, retries=3):
    """GET a URL and return BeautifulSoup, or None on persistent error."""
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"\n    WARNING  {url}: {e}")
    return None

# ── URL/ID parsers ─────────────────────────────────────────────────────────────

def parse_series_from_href(href):
    """'/serie-17744-BD-hack-GU.html'  ->  ('17744', 'hack-GU')"""
    m = re.search(r"/serie-(\d+)-BD-(.+?)(?:\.html)?$", href)
    return (m.group(1), m.group(2)) if m else (None, None)


def parse_album_id_from_href(href):
    """'/BD-Asterix-Tome-1-...-22940.html'  ->  '22940'"""
    m = re.search(r"-(\d+)\.html$", href)
    return m.group(1) if m else None


def parse_sale_id_from_href(href):
    """'/ventes-BD-1278004.html'  ->  '1278004'"""
    m = re.search(r"/ventes-BD-(\d+)\.html", href)
    return m.group(1) if m else None

# ── Priority file ──────────────────────────────────────────────────────────────

def load_priority_series(filepath):
    """
    Read the priority file and return a list of normalised series name strings.
    Lines starting with # are treated as comments and ignored.
    Empty lines are ignored.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"\nERROR: Priority file '{filepath}' not found.")
        print("Please create it with one series name per line, e.g.:\n")
        print("    Asterix")
        print("    Aama")
        print("    Akira\n")
        sys.exit(1)

    priorities = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                priorities.append(normalise(line))

    if not priorities:
        print(f"\nERROR: Priority file '{filepath}' is empty.")
        sys.exit(1)

    return priorities


def match_priority_to_wishlist(priority_norms, series_map, priority_file):
    """
    For each priority name, find all matching series IDs in the wishlist.
    Matching is: the priority string appears anywhere in the normalised
    series name (partial/substring match).

    Returns a filtered series_map containing only matched series, and
    prints a report of what was (and wasn't) matched.
    """
    print("\nMatching priority series against wishlist...")

    matched_map  = {}   # series_id -> info dict
    unmatched    = []

    for prio in priority_norms:
        found = []
        for sid, info in series_map.items():
            if prio in normalise(info["name"]):
                found.append((sid, info))
        if found:
            for sid, info in found:
                matched_map[sid] = info
                print(f"  ✓  '{prio}'  ->  {info['name']}  (id={sid})")
        else:
            unmatched.append(prio)

    if unmatched:
        print(f"\n  ERROR: The following priority entries had NO match in the wishlist:")
        for u in unmatched:
            print(f"    ✗  '{u}'")
        print(f"\n  Please fix or remove the unmatched entries in '{priority_file}'"
              f" and run again.")
        sys.exit(1)

    print(f"\n  {len(matched_map)} wishlist series selected from "
          f"{len(priority_norms)} priority entries.")
    return matched_map

# ── Step 1 — Scrape the wishlist ──────────────────────────────────────────────

def scrape_wishlist(session):
    """
    Walk all wishlist pages and return:
        wishlist_by_album_id  { album_id: item_dict }
        series_map            { series_id: {name, slug, url} }
    """
    print("\nScraping wishlist...")
    wishlist_by_album_id = {}
    series_map           = {}
    page                 = 0
    total                = 0

    while True:
        params = {
            "IdUser":       WISHLIST_USER_ID,
            "IdCollection": WISHLIST_COLLECTION,
            "Lettre":       "",
            "Page":         page,
        }
        soup = get_soup(WISHLIST_BASE_URL, session, params)
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

            # Album page link — find the /BD-...-NNNN.html href
            album_href = ""
            for a in li.select('a[href*="/BD-"]'):
                href = a.get("href", "")
                if re.search(r"-(\d+)\.html$", href):
                    album_href = href
                    break

            album_id  = parse_album_id_from_href(album_href)
            album_url = urljoin(BEDETHEQUE_BASE, album_href) if album_href else ""

            # Album title: prefer the non-"Acheter" link text
            album_title = ""
            for a in li.select('a[href*="/BD-"]'):
                txt = a.get_text(strip=True)
                if txt and txt.lower() != "acheter":
                    album_title = txt
                    break
            if not album_title:
                raw = li.get_text(" ", strip=True).replace(series_name, "").strip()
                raw = re.sub(
                    r"\s*(Editeur|DL|Etat|Achat le|Acheter)\s*:.*",
                    "", raw, flags=re.IGNORECASE
                ).strip()
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
                series_map[series_id] = {
                    "name": series_name,
                    "slug": series_slug,
                    "url":  series_url,
                }

            items_this_page += 1
            total += 1

        print(f"  Page {page}: {items_this_page} albums  (running total: {total})")

        if not items_this_page:
            break

        next_page  = page + 1
        next_links = [
            a for a in soup.select('a[href*="Page="]')
            if f"Page={next_page}" in a.get("href", "")
        ]
        if not next_links:
            break
        page = next_page
        time.sleep(REQUEST_DELAY)

    print(f"\n  Done: {len(wishlist_by_album_id)} albums (with IDs) across "
          f"{len(series_map)} unique series.")
    return wishlist_by_album_id, series_map

# ── Step 2 — Fetch all sale rows from the search page ─────────────────────────

def get_sale_rows_from_search(series_id, series_name, session):
    """
    Fetch all pages of /ventes/search?RechSerie=<name> and return every
    sale row for this series.

    The title string in the results table has the format:
        "Astérix -1a1965a- Astérix le Gaulois"
         ^series  ^edition  ^album title

    We extract:
        tome_num         — the leading integer of the edition token, e.g. "1"
        album_title_part — the portion after the edition token, e.g. "Astérix le Gaulois"

    Returns a list of dicts, one per listing row.
    """
    sales = []
    deb   = 0

    # Use only the base series name for the search query (strip any " - edition"
    # qualifier so the search remains broad enough to find results)
    search_name = series_name.split(" - ")[0].strip()

    while True:
        params = {
            "RechIdSerie":  "",
            "RechIdAuteur": "",
            "RechSerie":    search_name,
            "RechAuteur":   "",
            "RechEditeur":  "",
            "RechPrixMin":  "",
            "RechPrixMax":  "",
            "RechVendeur":  "",
            "RechPays":     "",
            "RechEtat":     "",
            "RechRecent":   "0",
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
            prix        = re.sub(r"[^\d.,]", "", cells[3].get_text(strip=True))
            etat        = ""
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
                "prix":             prix,
                "eo":               eo,
                "etat":             etat,
                "series_id":        series_id,
                "album_id":         None,
            })
            rows_this_page += 1

        if not rows_this_page:
            break

        # Follow pagination if a next-page link exists
        next_deb = deb + 1
        if not soup.select_one(f'a[href*="DEB=__{next_deb}"]'):
            break
        deb = next_deb
        time.sleep(REQUEST_DELAY)

    return sales


def parse_sale_title(raw):
    """
    Split a sale title string into (tome_num, album_title_part).

    Examples:
        "Astérix -3b1966- Astérix et les Goths"  ->  ("3",   "Astérix et les Goths")
        "Akira -1- Akira"                         ->  ("1",   "Akira")
        "Aama -INT- Intégrale"                    ->  ("INT", "Intégrale")
        "Série sans tiret"                        ->  (None,  "Série sans tiret")

    The edition token sits between the first pair of dashes and can look like:
        -1-   -2b1966-   -12a1969-   -INT-   -8'Lbd-
    tome_num is the leading integer, or the full token if non-numeric.
    """
    # Numeric tome: " -3b1966- Title" or " -1- Title"
    m = re.search(r" -(\d+)[^-]*- (.+)$", raw)
    if m:
        return m.group(1), m.group(2).strip()

    # Non-numeric token: " -INT- Title" or " -HS- Title"
    m2 = re.search(r" -([A-Z0-9'][^-]*)-\s+(.+)$", raw)
    if m2:
        return m2.group(1), m2.group(2).strip()

    return None, raw


# ── Step 3 — Resolve album ID from a sale detail page ─────────────────────────

def resolve_album_id(sale_url, session):
    """
    Fetch the individual sale listing page and extract the album ID
    from the album page link (e.g. /BD-Asterix-Tome-1-...-22940.html).
    Returns the album ID string, or None if not found.
    """
    soup = get_soup(sale_url, session)
    if soup is None:
        return None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.match(r"^/?BD-", href) and re.search(r"-(\d+)\.html$", href):
            album_id = parse_album_id_from_href(href)
            if album_id:
                return album_id
    return None


# ── Step 4 — Two-phase match: fuzzy pre-filter then ID confirmation ────────────

def fuzzy_candidate(sale, wanted_items):
    """
    Phase 1: quick in-memory check whether a sale row is a plausible match
    for any wishlist item in this series.

    The sale title is parsed into:
        tome_num         e.g. "3"                  (from "-3b1966-")
        album_title_part e.g. "Astérix et les Goths"

    The wishlist item has:
        album_title      e.g. "Astérix et les Goths"  (plain title, no tome prefix)

    Matching strategy:
        - Normalise both title strings.
        - Run titles_match() which checks substring containment and word overlap.
        - No tome-number comparison is attempted here because wishlist titles
          don't carry a tome number — that confirmation is left to Phase 2
          (exact album-ID check on the sale detail page).

    Returns the first wishlist item that matches, or None.
    """
    sale_part_norm = normalise(sale["album_title_part"])

    for wish in wanted_items:
        wish_norm = normalise(wish["album_title"])
        if titles_match(sale_part_norm, wish_norm):
            return wish

    return None


def find_matches(wishlist_by_album_id, priority_series_map, session):
    """
    For each priority series:
      1. Fetch ALL sale rows from the search page (fast, no per-row requests).
      2. Phase 1: fuzzy-filter rows to candidates that look like a wishlist match.
      3. Phase 2: for each candidate only, fetch the sale detail page to get the
         exact album ID and confirm it is in the wishlist.
    """
    wishlist_by_series = defaultdict(list)
    for album_id, item in wishlist_by_album_id.items():
        wishlist_by_series[item["series_id"]].append(item)

    results      = []
    total_series = len(priority_series_map)

    for idx, (series_id, info) in enumerate(priority_series_map.items(), 1):
        print(f"\n  [{idx}/{total_series}]  {info['name']}")

        wanted = wishlist_by_series.get(series_id, [])
        if not wanted:
            print(f"    -> No wishlist albums for this series (skipping).")
            continue

        # Phase 1: fetch search results and fuzzy-filter
        sales = get_sale_rows_from_search(series_id, info["name"], session)
        if not sales:
            print(f"    -> No listings found on sale.")
            time.sleep(REQUEST_DELAY)
            continue

        candidates = []
        for sale in sales:
            match = fuzzy_candidate(sale, wanted)
            if match:
                candidates.append((sale, match))

        print(f"    -> {len(sales)} listing(s) found, "
              f"{len(candidates)} candidate(s) after fuzzy filter.")

        if not candidates:
            continue

        # Phase 2: confirm each candidate with an individual page fetch
        for sale, tentative_wish in candidates:
            time.sleep(REQUEST_DELAY)
            album_id = resolve_album_id(sale["sale_url"], session)
            sale["album_id"] = album_id or ""

            confirmed_wish = None

            if album_id:
                # Check the resolved album ID against the wishlist
                confirmed_wish = wishlist_by_album_id.get(album_id)
                if confirmed_wish:
                    print(f"    ✓  CONFIRMED: {sale['raw_title']}")
                else:
                    print(f"    ✗  False positive (album {album_id} not on wishlist): "
                          f"{sale['raw_title']}")
            else:
                # Could not resolve ID — accept the fuzzy match with a warning
                confirmed_wish = tentative_wish
                print(f"    ~  ID unresolved, keeping fuzzy match: {sale['raw_title']}")

            if confirmed_wish:
                results.append({
                    "sale_id":     sale["sale_id"],
                    "sale_url":    sale["sale_url"],
                    "album_title": sale["raw_title"],
                    "vendeur":     sale["vendeur"],
                    "prix":        sale["prix"],
                    "eo":          sale["eo"],
                    "etat":        sale["etat"],
                    "series_id":   series_id,
                    "album_id":    sale["album_id"],
                    "series_name": info["name"],
                    "wish_title":  confirmed_wish["album_title"],
                    "wish_url":    confirmed_wish["album_url"],
                })

    return results

# ── Step 5 — Output ────────────────────────────────────────────────────────────

def print_console(results):
    if not results:
        print("\nNo matches found.")
        return

    by_seller = defaultdict(list)
    for m in results:
        by_seller[m["vendeur"]].append(m)

    print(f"\n{'='*72}")
    print(f"  {len(results)} listing(s) found across {len(by_seller)} seller(s)")
    print(f"{'='*72}")

    for seller in sorted(by_seller):
        items = sorted(by_seller[seller], key=lambda x: x["series_name"])
        print(f"\n  Seller: {seller}  ({len(items)} listing(s))")
        print(f"  {'-'*68}")
        for m in items:
            print(f"    {m['album_title']}")
            print(f"       Price: {m['prix'] or '?'} EUR"
                  f"   Condition: {m['etat'] or '?'}"
                  f"   EO: {m['eo'] or 'No'}")
            print(f"       Sale URL: {m['sale_url']}")
    print()


def save_csv(results, path="results.csv"):
    if not results:
        return
    fields = ["vendeur", "series_name", "album_title", "wish_title",
              "prix", "etat", "eo", "sale_url", "wish_url", "album_id"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for m in sorted(results, key=lambda x: (x["vendeur"], x["series_name"])):
            w.writerow(m)
    print(f"CSV saved: {path}")


def save_html(results, path="results.html"):
    by_seller = defaultdict(list)
    for m in results:
        by_seller[m["vendeur"]].append(m)

    body = ""
    for seller in sorted(by_seller):
        items  = sorted(by_seller[seller], key=lambda x: x["series_name"])
        esc    = html.escape(seller)
        v_url  = (f"https://www.bedetheque.com/ventes/search"
                  f"?RechVendeur={seller}")
        body  += f"""
    <tr class="seller-row">
      <td colspan="5">
        &#128100;
        <a href="{html.escape(v_url)}" target="_blank"
           class="seller-link">{esc}</a>
        <span class="badge">{len(items)} annonce(s)</span>
      </td>
    </tr>"""
        for m in items:
            body += f"""
    <tr class="item">
      <td>
        <a href="{html.escape(m['sale_url'])}" target="_blank"
           class="sale-link">{html.escape(m['album_title'])}</a>
      </td>
      <td class="series-cell">
        <a href="{html.escape(m.get('wish_url',''))}" target="_blank"
           class="wish-link">{html.escape(m['series_name'])}</a>
      </td>
      <td class="price">{html.escape(m['prix'] or '?')} &#8364;</td>
      <td>{html.escape(m['etat'] or '\u2014')}</td>
      <td class="eo">{html.escape(m['eo'] or '\u2014')}</td>
    </tr>"""

    page = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wishlist BD - Annonces trouvees</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Arial, sans-serif;
    background: #f0f2f5; color: #1a1a1a; padding: 2rem 1rem;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  header {{ margin-bottom: 1.5rem; }}
  header h1 {{ font-size: 1.7rem; color: #b71c1c; }}
  header p  {{ color: #555; margin-top: .4rem; font-size: .95rem; }}
  header strong {{ color: #b71c1c; }}
  table {{
    width: 100%; border-collapse: collapse; background: #fff;
    border-radius: 10px; overflow: hidden;
    box-shadow: 0 2px 12px rgba(0,0,0,.1); margin-bottom: 1.5rem;
  }}
  thead th {{
    background: #b71c1c; color: #fff; padding: .65rem 1rem;
    text-align: left; font-size: .8rem;
    text-transform: uppercase; letter-spacing: .07em;
  }}
  tr.seller-row td {{
    background: #263238; color: #eceff1;
    padding: .7rem 1rem; font-size: .95rem;
  }}
  .seller-link {{
    color: #80cbc4; text-decoration: none; font-weight: bold;
  }}
  .seller-link:hover {{ text-decoration: underline; }}
  .badge {{
    display: inline-block; background: #ef9a9a; color: #7f0000;
    border-radius: 999px; padding: 1px 9px;
    font-size: .75rem; font-weight: bold; margin-left: .7rem;
  }}
  tr.item td {{
    padding: .55rem 1rem; border-bottom: 1px solid #f0f0f0;
    font-size: .88rem; vertical-align: middle;
  }}
  tr.item:hover td {{ background: #fff8f8; }}
  .sale-link {{
    color: #b71c1c; text-decoration: none; font-weight: 500;
  }}
  .sale-link:hover {{ text-decoration: underline; }}
  .wish-link {{
    color: #555; text-decoration: none; font-size: .82rem;
  }}
  .wish-link:hover {{ text-decoration: underline; color: #b71c1c; }}
  .series-cell {{ color: #666; }}
  .price {{ font-weight: bold; color: #b71c1c; white-space: nowrap; }}
  .eo {{ color: #555; text-align: center; }}
  footer {{
    font-size: .82rem; color: #888; margin-top: 1rem;
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>&#128218; Wishlist BD &mdash; Annonces en vente</h1>
    <p>Utilisateur&nbsp;: <strong>jcarreno</strong>
       &nbsp;&middot;&nbsp;
       <strong>{len(results)}</strong> annonce(s) correspondant
       exactement &agrave; la wishlist, chez
       <strong>{len(by_seller)}</strong> vendeur(s)</p>
  </header>
  <table>
    <thead>
      <tr>
        <th>Album en vente</th>
        <th>S&eacute;rie</th>
        <th>Prix</th>
        <th>&Eacute;tat</th>
        <th>EO</th>
      </tr>
    </thead>
    <tbody>{body}
    </tbody>
  </table>
  <footer>
    Seuls les albums figurant exactement dans la wishlist sont
    affich&eacute;s. Cliquez sur le titre pour acc&eacute;der
    &agrave; l&rsquo;annonce de vente.
  </footer>
</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"HTML report saved: {path}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    priority_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PRIORITY_FILE

    print("BD Wishlist vs Bedetheque Sales Matcher  (v5 - two-phase matching)")
    print("=" * 64)
    print(f"Priority file: {priority_file}")

    # Load the priority list
    priority_norms = load_priority_series(priority_file)
    print(f"Loaded {len(priority_norms)} priority series entries.")

    session = requests.Session()

    # Scrape the full wishlist (needed to build the series map and album IDs)
    wishlist_by_album_id, series_map = scrape_wishlist(session)
    if not wishlist_by_album_id:
        print("ERROR: Could not load the wishlist.")
        sys.exit(1)

    # Filter series_map down to only the priority series
    priority_series_map = match_priority_to_wishlist(priority_norms, series_map, priority_file)
    if not priority_series_map:
        print(f"\nNo priority series matched the wishlist. "
              f"Check '{priority_file}'.")
        sys.exit(1)

    # Search sales and match to wishlist
    print(f"\nSearching sales for {len(priority_series_map)} priority series...")
    results = find_matches(wishlist_by_album_id, priority_series_map, session)

    print(f"\n  Total exact matches: {len(results)}")

    # Output
    print_console(results)
    save_csv(results, "results.csv")
    save_html(results, "results.html")

    print("Done!")


if __name__ == "__main__":
    main()