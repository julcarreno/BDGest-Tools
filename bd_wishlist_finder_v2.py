#!/usr/bin/env python3
"""
BD Wishlist vs Bedetheque Sales Matcher  (v2)
==============================================
Scrapes jcarreno's BDGest wishlist, then checks each series for active
sale listings on Bedetheque. Matches are grouped by seller and include
the exact album title and a direct link to every sale listing.

Usage:
    pip install requests beautifulsoup4 --break-system-packages
    python bd_wishlist_matcher.py

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

WISHLIST_USER_ID    = "71812"
WISHLIST_COLLECTION = "1"
WISHLIST_BASE_URL   = "https://www.bdgest.com/online/wishlist"
BEDETHEQUE_BASE     = "https://www.bedetheque.com"

# Polite delay between HTTP requests (seconds).
REQUEST_DELAY = 0.75

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

# ── HTTP helper ────────────────────────────────────────────────────────────────

def get_soup(url, session, params=None):
    """GET a URL and return a BeautifulSoup, or None on error."""
    try:
        r = session.get(url, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException as e:
        print(f"    WARNING  {url}: {e}")
        return None

# ── URL parsers ────────────────────────────────────────────────────────────────

def parse_series_from_href(href):
    """
    '/serie-17744-BD-hack-GU.html'  ->  ('17744', 'hack-GU')
    """
    m = re.search(r"/serie-(\d+)-BD-(.+?)(?:\.html)?$", href)
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_album_id_from_href(href):
    """'/BD-Asterix-Tome-1-...-22940.html'  ->  '22940'"""
    m = re.search(r"-(\d+)\.html$", href)
    return m.group(1) if m else None


def parse_sale_id_from_href(href):
    """'/ventes-BD-1278004.html'  ->  '1278004'"""
    m = re.search(r"/ventes-BD-(\d+)\.html", href)
    return m.group(1) if m else None

# ── Step 1 — Scrape the wishlist ──────────────────────────────────────────────

def scrape_wishlist(session):
    """
    Walk all wishlist pages and collect every album.

    Returns:
        wishlist_items  list of dicts, one per album
        series_map      { series_id: {name, slug, url} }
    """
    print("\nScraping wishlist...")
    wishlist_items = []
    series_map     = {}
    page           = 0

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
            # Series link  (/serie-...)
            s_tag = li.select_one('a[href*="/serie-"]')
            if not s_tag:
                continue
            s_href = s_tag.get("href", "")
            series_id, series_slug = parse_series_from_href(s_href)
            if not series_id:
                continue
            series_name = s_tag.get_text(strip=True)
            series_url  = urljoin(BEDETHEQUE_BASE, s_href)

            # Album page link  (/BD-...)
            a_tag = li.select_one('a[href*="/BD-"]')
            album_href  = a_tag.get("href", "") if a_tag else ""
            album_id    = parse_album_id_from_href(album_href)
            album_url   = urljoin(BEDETHEQUE_BASE, album_href) if album_href else ""

            # Album title: prefer the link text of the album page link.
            # Fall back to cleaning the full li text.
            album_title = ""
            if a_tag:
                link_text = a_tag.get_text(strip=True)
                if link_text and link_text.lower() not in ("acheter", ""):
                    album_title = link_text
            if not album_title:
                raw = li.get_text(" ", strip=True)
                raw = raw.replace(series_name, "").strip()
                raw = re.sub(
                    r"\s*(Editeur|DL|Etat|Achat le|Acheter)\s*:.*",
                    "", raw, flags=re.IGNORECASE
                ).strip()
                album_title = raw or series_name

            wishlist_items.append({
                "series_id":   series_id,
                "series_name": series_name,
                "series_slug": series_slug,
                "series_url":  series_url,
                "album_id":    album_id or "",
                "album_title": album_title,
                "album_url":   album_url,
            })

            if series_id not in series_map:
                series_map[series_id] = {
                    "name": series_name,
                    "slug": series_slug,
                    "url":  series_url,
                }
            items_this_page += 1

        print(f"  Page {page}: {items_this_page} albums  "
              f"(running total: {len(wishlist_items)})")

        if not items_this_page:
            break

        # Go to next page if its link exists
        next_page  = page + 1
        next_links = [
            a for a in soup.select('a[href*="Page="]')
            if f"Page={next_page}" in a.get("href", "")
        ]
        if not next_links:
            break
        page = next_page
        time.sleep(REQUEST_DELAY)

    print(f"\n  Done: {len(wishlist_items)} albums across "
          f"{len(series_map)} unique series.")
    return wishlist_items, series_map

# ── Step 2 — Scrape sales per series ──────────────────────────────────────────

def scrape_sales_for_series(series_id, series_info, session):
    """
    Fetch the series sales page:
        https://www.bedetheque.com/ventes_serie-{id}-BD-{slug}.html

    Returns a list of sale dicts, each containing:
        album_title   exact title shown in the listing
        sale_url      https://www.bedetheque.com/ventes-BD-{id}.html
        sale_id       numeric listing ID
        vendeur       seller username
        prix          price string ("4.00")
        eo            "Oui" or ""
        etat          condition text (from img title attr)
    """
    slug = series_info["slug"]
    url  = f"{BEDETHEQUE_BASE}/ventes_serie-{series_id}-BD-{slug}.html"
    soup = get_soup(url, session)
    if soup is None:
        return []

    table = soup.select_one("table")
    if not table:
        return []

    sales = []
    for tr in table.select("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue  # skip header rows

        # Col 0 — album title + link to the sale listing
        title_link = cells[0].find("a")
        if not title_link:
            continue
        album_title = title_link.get_text(strip=True)
        sale_href   = title_link.get("href", "")
        sale_url    = urljoin(BEDETHEQUE_BASE, sale_href)
        sale_id     = parse_sale_id_from_href(sale_href) or ""

        # Col 1 — EO (edition originale flag)
        eo = "Oui" if cells[1].get_text(strip=True) else ""

        # Col 2 — seller
        seller_link = cells[2].find("a")
        vendeur = (seller_link.get_text(strip=True) if seller_link
                   else cells[2].get_text(strip=True))

        # Col 3 — price
        prix = re.sub(r"[^\d.,]", "", cells[3].get_text(strip=True))

        # Col 5 (optional) — condition from img title attribute
        etat = ""
        if len(cells) >= 6:
            img = cells[5].find("img")
            if img:
                etat = img.get("title", "")

        if not album_title or not vendeur:
            continue

        sales.append({
            "album_title": album_title,
            "sale_url":    sale_url,
            "sale_id":     sale_id,
            "vendeur":     vendeur,
            "prix":        prix,
            "eo":          eo,
            "etat":        etat,
        })

    return sales

# ── Step 3 — Match sales to wishlist ──────────────────────────────────────────

def normalise(s):
    """Lowercase, strip accents, collapse punctuation for fuzzy comparison."""
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def titles_match(sale_norm, wish_norm):
    """
    Return True if the two normalised titles likely refer to the same album.
    Checks direct containment, then shared tome number + word overlap.
    """
    if wish_norm in sale_norm or sale_norm in wish_norm:
        return True
    t_s = re.search(r"\b(\d+)\b", sale_norm)
    t_w = re.search(r"\b(\d+)\b", wish_norm)
    if t_s and t_w and t_s.group(1) == t_w.group(1):
        stopwords = {"le", "la", "les", "de", "du", "un", "une", "et", "a", "en"}
        words_s   = set(sale_norm.split()) - stopwords
        words_w   = set(wish_norm.split()) - stopwords
        if len(words_s & words_w) >= 2:
            return True
    return False


def match_all(wishlist_items, all_sales_by_series):
    """
    Cross-reference every sale against the wishlist.
    Returns a flat list of enriched result dicts.
    """
    wishlist_by_series = defaultdict(list)
    for item in wishlist_items:
        wishlist_by_series[item["series_id"]].append(item)

    results = []
    for series_id, sales in all_sales_by_series.items():
        if series_id not in wishlist_by_series:
            continue
        wl_items    = wishlist_by_series[series_id]
        series_name = wl_items[0]["series_name"]

        for sale in sales:
            # Try to match the specific album
            match_type   = "series"
            matched_wish = wl_items[0]
            sale_norm    = normalise(sale["album_title"])

            for wish in wl_items:
                if titles_match(sale_norm, normalise(wish["album_title"])):
                    matched_wish = wish
                    match_type   = "exact"
                    break

            results.append({
                **sale,
                "series_id":   series_id,
                "series_name": series_name,
                "match_type":  match_type,
                "wish_title":  matched_wish["album_title"],
                "wish_url":    matched_wish["album_url"],
            })

    return results

# ── Step 4 — Output ────────────────────────────────────────────────────────────

def print_console(matched):
    if not matched:
        print("\nNo matches found.")
        return

    by_seller = defaultdict(list)
    for m in matched:
        by_seller[m["vendeur"]].append(m)

    print(f"\n{'='*72}")
    print(f"  {len(matched)} listing(s) found across {len(by_seller)} seller(s)")
    print(f"{'='*72}")

    for seller in sorted(by_seller):
        items = sorted(by_seller[seller], key=lambda x: x["series_name"])
        print(f"\n  Seller: {seller}  ({len(items)} listing(s))")
        print(f"  {'-'*68}")
        for m in items:
            tag = "[exact]" if m["match_type"] == "exact" else "[series]"
            print(f"    {tag}  {m['album_title']}")
            print(f"           Price: {m['prix'] or '?'} EUR"
                  f"   Condition: {m['etat'] or '?'}"
                  f"   EO: {m['eo'] or 'No'}")
            print(f"           Sale URL: {m['sale_url']}")
    print()


def save_csv(matched, path="results.csv"):
    if not matched:
        return
    fields = ["vendeur", "series_name", "album_title", "wish_title",
              "prix", "etat", "eo", "match_type", "sale_url", "wish_url"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for m in sorted(matched, key=lambda x: (x["vendeur"], x["series_name"])):
            w.writerow(m)
    print(f"CSV saved: {path}")


def save_html(matched, path="results.html"):
    by_seller = defaultdict(list)
    for m in matched:
        by_seller[m["vendeur"]].append(m)

    body = ""
    for seller in sorted(by_seller):
        items = sorted(by_seller[seller], key=lambda x: x["series_name"])
        esc   = html.escape(seller)
        vendor_url = f"https://www.bedetheque.com/ventes/search?RechVendeur={seller}"
        body += f"""
    <tr class="seller-row">
      <td colspan="6">
        <span>&#128100;</span>
        <a href="{html.escape(vendor_url)}" target="_blank" class="seller-link">{esc}</a>
        <span class="badge">{len(items)} annonce(s)</span>
      </td>
    </tr>"""
        for m in items:
            cls  = "exact" if m["match_type"] == "exact" else "series"
            icon = "&#10003;" if cls == "exact" else "&#8776;"
            tooltip = "Correspondance exacte" if cls == "exact" else "Meme serie"
            # Wish title for tooltip on the sale title cell
            wish_escaped = html.escape(m["wish_title"])
            body += f"""
    <tr class="item {cls}">
      <td class="match-icon" title="{tooltip}">{icon}</td>
      <td>
        <a href="{html.escape(m['sale_url'])}" target="_blank" class="sale-link">{html.escape(m['album_title'])}</a>
        <span class="wish-label" title="Dans votre wishlist : {wish_escaped}">&#127197;</span>
      </td>
      <td class="series-cell">{html.escape(m['series_name'])}</td>
      <td class="price">{html.escape(m['prix'] or '?')} &#8364;</td>
      <td>{html.escape(m['etat'] or '&mdash;')}</td>
      <td class="eo">{html.escape(m['eo'] or '&mdash;')}</td>
    </tr>"""

    total_sellers = len(by_seller)
    total_matches = len(matched)

    page = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wishlist BD - Annonces trouvees</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #f0f2f5;
    color: #1a1a1a;
    padding: 2rem 1rem;
  }}
  .container {{ max-width: 1150px; margin: 0 auto; }}
  .header {{ margin-bottom: 1.5rem; }}
  .header h1 {{ font-size: 1.7rem; color: #b71c1c; }}
  .header .subtitle {{ color: #555; margin-top: .4rem; font-size: .95rem; }}
  .header .subtitle strong {{ color: #b71c1c; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 2px 12px rgba(0,0,0,.1);
    margin-bottom: 1.5rem;
  }}
  thead th {{
    background: #b71c1c;
    color: #fff;
    padding: .65rem 1rem;
    text-align: left;
    font-size: .8rem;
    text-transform: uppercase;
    letter-spacing: .07em;
  }}
  tr.seller-row td {{
    background: #263238;
    color: #eceff1;
    padding: .7rem 1rem;
    font-size: .95rem;
  }}
  .seller-link {{
    color: #80cbc4;
    text-decoration: none;
    font-weight: bold;
  }}
  .seller-link:hover {{ text-decoration: underline; }}
  .badge {{
    display: inline-block;
    background: #ef9a9a;
    color: #7f0000;
    border-radius: 999px;
    padding: 1px 9px;
    font-size: .75rem;
    font-weight: bold;
    margin-left: .7rem;
  }}
  tr.item td {{
    padding: .55rem 1rem;
    border-bottom: 1px solid #f0f0f0;
    font-size: .88rem;
    vertical-align: middle;
  }}
  tr.item:last-of-type td {{ border-bottom: none; }}
  tr.item:hover td {{ background: #fff8f8; }}

  .match-icon {{
    text-align: center;
    width: 2rem;
    font-size: 1.05rem;
  }}
  tr.exact .match-icon  {{ color: #2e7d32; }}
  tr.series .match-icon {{ color: #e65100; }}

  .sale-link {{ color: #b71c1c; text-decoration: none; font-weight: 500; }}
  .sale-link:hover {{ text-decoration: underline; }}
  .wish-label {{ cursor: help; margin-left: .3rem; opacity: .6; font-size: .85rem; }}

  .series-cell {{ color: #555; font-size: .83rem; }}
  .price {{ font-weight: bold; color: #b71c1c; white-space: nowrap; }}
  .eo {{ color: #555; }}

  .legend {{
    font-size: .82rem;
    color: #666;
    margin-top: .5rem;
  }}
  .legend span {{ margin-right: 1.5rem; }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>&#128218; Wishlist BD &mdash; Annonces en vente</h1>
    <p class="subtitle">
      Utilisateur : <strong>jcarreno</strong> &nbsp;&middot;&nbsp;
      <strong>{total_matches}</strong> annonce(s) trouv&eacute;e(s) chez
      <strong>{total_sellers}</strong> vendeur(s)
    </p>
  </div>

  <table>
    <thead>
      <tr>
        <th></th>
        <th>Album en vente</th>
        <th>Serie</th>
        <th>Prix</th>
        <th>Etat</th>
        <th>EO</th>
      </tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>

  <p class="legend">
    <span>&#10003; = correspondance exacte (meme album que la wishlist)</span>
    <span>&#8776; = meme serie (edition ou tome different possible)</span>
    <span>&#127197; = survolez pour voir le titre dans la wishlist</span>
  </p>

</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"HTML report saved: {path}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("BD Wishlist vs Bedetheque Sales Matcher  (v2)")
    print("=" * 50)

    session = requests.Session()

    # 1. Scrape the full wishlist
    wishlist_items, series_map = scrape_wishlist(session)
    if not wishlist_items:
        print("ERROR: Could not load the wishlist.")
        sys.exit(1)

    # 2. Scrape each series' sales page
    print(f"\nSearching for listings across {len(series_map)} series...")
    all_sales_by_series = {}

    for idx, (series_id, info) in enumerate(series_map.items(), 1):
        print(f"  [{idx:3d}/{len(series_map)}]  {info['name']}", end="", flush=True)
        sales = scrape_sales_for_series(series_id, info, session)
        if sales:
            print(f"  ->  {len(sales)} listing(s)")
            all_sales_by_series[series_id] = sales
        else:
            print("  ->  no listings")
        time.sleep(REQUEST_DELAY)

    total_raw = sum(len(v) for v in all_sales_by_series.values())
    print(f"\n  Raw total: {total_raw} listing(s) across "
          f"{len(all_sales_by_series)} series.")

    # 3. Match listings to wishlist
    print("\nMatching listings to wishlist...")
    matched = match_all(wishlist_items, all_sales_by_series)
    print(f"  {len(matched)} match(es) found.")

    # 4. Output
    print_console(matched)
    save_csv(matched, "results.csv")
    save_html(matched, "results.html")

    print("\nDone!")


if __name__ == "__main__":
    main()