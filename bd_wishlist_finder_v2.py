#!/usr/bin/env python3
"""
BD Wishlist vs Bedetheque Sales Matcher  (v3 - exact match)
============================================================
Scrapes jcarreno's BDGest wishlist, finds sales listings for each
wishlist series, then fetches each individual sale page to get the
exact album ID — ensuring only albums actually on the wishlist are
reported. Results are grouped by seller.

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
REQUEST_DELAY = 1

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

def get_soup(url, session, params=None, retries=3):
    """GET a URL and return a BeautifulSoup, or None on error."""
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
                print(f"    WARNING  {url}: {e}")
    return None

# ── URL/ID parsers ─────────────────────────────────────────────────────────────

def parse_series_from_href(href):
    """'/serie-17744-BD-hack-GU.html'  ->  ('17744', 'hack-GU')"""
    m = re.search(r"/serie-(\d+)-BD-(.+?)(?:\.html)?$", href)
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_album_id_from_href(href):
    """
    '/BD-Asterix-Tome-1-Asterix-le-Gaulois-22940.html'  ->  '22940'
    Works for any bedetheque album page URL — the album ID is always
    the last number before .html.
    """
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
        wishlist_by_album_id  { album_id: wishlist_item_dict }
        series_map            { series_id: {name, slug, url} }
    """
    print("\nScraping wishlist...")
    wishlist_by_album_id = {}   # album_id (str) -> item dict
    series_map           = {}   # series_id (str) -> {name, slug, url}
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
            # ── Series link (/serie-...)
            s_tag = li.select_one('a[href*="/serie-"]')
            if not s_tag:
                continue
            s_href = s_tag.get("href", "")
            series_id, series_slug = parse_series_from_href(s_href)
            if not series_id:
                continue
            series_name = s_tag.get_text(strip=True)
            series_url  = urljoin(BEDETHEQUE_BASE, s_href)

            # ── Album page link (/BD-...)
            # The wishlist has two <a href="/BD-..."> per item:
            #   1. The cover thumbnail link
            #   2. The "Acheter" button
            # Both lead to the same album URL, so either works.
            a_tags = li.select('a[href*="/BD-"]')
            album_href = ""
            for a in a_tags:
                href = a.get("href", "")
                if re.search(r"-(\d+)\.html$", href):
                    album_href = href
                    break

            album_id  = parse_album_id_from_href(album_href)
            album_url = urljoin(BEDETHEQUE_BASE, album_href) if album_href else ""

            # ── Album title from the link text (skip "Acheter")
            album_title = ""
            for a in a_tags:
                txt = a.get_text(strip=True)
                if txt and txt.lower() != "acheter":
                    album_title = txt
                    break
            if not album_title:
                # Fallback: scrape text content of the li, strip metadata
                raw = li.get_text(" ", strip=True)
                raw = raw.replace(series_name, "").strip()
                raw = re.sub(
                    r"\s*(Editeur|DL|Etat|Achat le|Acheter)\s*:.*",
                    "", raw, flags=re.IGNORECASE
                ).strip()
                album_title = raw or series_name

            item = {
                "series_id":   series_id,
                "series_name": series_name,
                "series_slug": series_slug,
                "series_url":  series_url,
                "album_id":    album_id or "",
                "album_title": album_title,
                "album_url":   album_url,
            }

            # Index by album_id for fast exact lookup later
            if album_id:
                wishlist_by_album_id[album_id] = item
            # Also keep track of which series are on the list
            if series_id not in series_map:
                series_map[series_id] = {
                    "name": series_name,
                    "slug": series_slug,
                    "url":  series_url,
                }

            items_this_page += 1
            total += 1

        print(f"  Page {page}: {items_this_page} albums  "
              f"(running total: {total})")

        if not items_this_page:
            break

        # Advance to next page if its link exists
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

# ── Step 2 — Get sale listing IDs per series ──────────────────────────────────

def get_sale_listing_ids_for_series(series_id, series_info, session):
    """
    Fetch the series sales page and return a list of raw sale dicts,
    each containing only the sale_id, album_title, vendeur, prix, eo.
    The album_id is NOT available from this page — we get it in Step 3.
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
            continue  # header row

        # Col 0 — sale listing link (NOT the album page link)
        title_link = cells[0].find("a")
        if not title_link:
            continue

        sale_href   = title_link.get("href", "")
        sale_id     = parse_sale_id_from_href(sale_href)
        if not sale_id:
            continue

        sale_url    = urljoin(BEDETHEQUE_BASE, sale_href)
        album_title = title_link.get_text(strip=True)

        # Col 1 — EO
        eo = "Oui" if cells[1].get_text(strip=True) else ""

        # Col 2 — seller
        seller_link = cells[2].find("a")
        vendeur = (seller_link.get_text(strip=True) if seller_link
                   else cells[2].get_text(strip=True))

        # Col 3 — price
        prix = re.sub(r"[^\d.,]", "", cells[3].get_text(strip=True))

        # Col 5 — condition (img title attr)
        etat = ""
        if len(cells) >= 6:
            img = cells[5].find("img")
            if img:
                etat = img.get("title", "")

        sales.append({
            "sale_id":     sale_id,
            "sale_url":    sale_url,
            "album_title": album_title,   # display title from table
            "vendeur":     vendeur,
            "prix":        prix,
            "eo":          eo,
            "etat":        etat,
            "series_id":   series_id,
            "album_id":    None,          # filled in Step 3
        })

    return sales

# ── Step 3 — Resolve each sale's album ID ─────────────────────────────────────

def resolve_album_id(sale, session):
    """
    Fetch the individual sale page (e.g. /ventes-BD-1416903.html) and
    extract the album ID from the album page link (e.g. /BD-hack-GU-Tome-1-74021.html).

    The sale detail page always has a link like:
        <a href="/BD-SeriesSlug-Tome-N-Title-ALBUMID.html">Tome N</a>
    inside the main content area.
    """
    soup = get_soup(sale["sale_url"], session)
    if soup is None:
        return None

    # The album link is in the main <li> block — it links to /BD-...-ALBUMID.html
    # and is distinct from the series ventes link (/ventes_serie-...).
    # Look specifically in the content section (not the nav or side table).
    content = soup.select_one("div#content, div.content, ul.ventes_detail, div.main")
    search_area = content if content else soup

    for a in search_area.find_all("a", href=True):
        href = a["href"]
        # Must be an album page (starts with /BD-), not a ventes link
        if re.match(r"^/BD-", href) and re.search(r"-(\d+)\.html$", href):
            album_id = parse_album_id_from_href(href)
            if album_id:
                return album_id

    # Broader fallback: any /BD-...-NNNNN.html link on the whole page
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.match(r"^/?BD-", href) and re.search(r"-(\d+)\.html$", href):
            album_id = parse_album_id_from_href(href)
            if album_id:
                return album_id

    return None

# ── Step 4 — Match sales to wishlist ──────────────────────────────────────────

def find_matches(wishlist_by_album_id, series_map, session):
    """
    Main loop:
      1. For each series on the wishlist, fetch its sales page.
      2. For each listing found, fetch the sale detail page to get the album ID.
      3. If that album ID is in the wishlist, it's a match — keep it.
      4. If album ID resolution fails, fall back to title-based matching.

    Returns a list of result dicts.
    """
    # Build a set of wishlist album IDs per series for quick pre-filtering
    wishlist_by_series = defaultdict(set)
    for album_id, item in wishlist_by_album_id.items():
        wishlist_by_series[item["series_id"]].add(album_id)

    results = []
    total_series = len(series_map)

    for idx, (series_id, info) in enumerate(series_map.items(), 1):
        print(f"  [{idx:3d}/{total_series}]  {info['name']}", end="", flush=True)

        # Get all sale listings for this series
        sales = get_sale_listing_ids_for_series(series_id, info, session)
        if not sales:
            print("  ->  no listings")
            time.sleep(REQUEST_DELAY)
            continue

        print(f"  ->  {len(sales)} listing(s), resolving album IDs...", end="", flush=True)

        matched_count = 0
        wanted_ids    = wishlist_by_series[series_id]  # album IDs we want

        for sale in sales:
            time.sleep(REQUEST_DELAY)
            album_id = resolve_album_id(sale, session)
            sale["album_id"] = album_id or ""

            matched_wish = None

            if album_id and album_id in wanted_ids:
                # ── Exact match: the specific album on sale is on the wishlist
                matched_wish = wishlist_by_album_id[album_id]

            elif not album_id:
                # ── Could not resolve the album ID from the sale page.
                #    Fall back: title-based matching against wishlist albums
                #    in this series.
                sale_norm = normalise(sale["album_title"])
                for wid in wanted_ids:
                    wish = wishlist_by_album_id[wid]
                    if wish["series_id"] == series_id:
                        if titles_match(sale_norm, normalise(wish["album_title"])):
                            matched_wish = wish
                            break

            if matched_wish:
                results.append({
                    **sale,
                    "series_name": info["name"],
                    "wish_title":  matched_wish["album_title"],
                    "wish_url":    matched_wish["album_url"],
                })
                matched_count += 1

        print(f"  {matched_count} match(es)")

    return results


def normalise(s):
    """Lowercase, strip accents, collapse punctuation."""
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def titles_match(sale_norm, wish_norm):
    """True if the two normalised titles likely refer to the same album."""
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
        v_url  = f"https://www.bedetheque.com/ventes/search?RechVendeur={seller}"
        body  += f"""
    <tr class="seller-row">
      <td colspan="5">
        &#128100;
        <a href="{html.escape(v_url)}" target="_blank" class="seller-link">{esc}</a>
        <span class="badge">{len(items)} annonce(s)</span>
      </td>
    </tr>"""
        for m in items:
            wish_esc = html.escape(m["wish_title"])
            body += f"""
    <tr class="item">
      <td>
        <a href="{html.escape(m['sale_url'])}" target="_blank" class="sale-link"
           title="Annonce {html.escape(m.get('sale_id',''))}">{html.escape(m['album_title'])}</a>
      </td>
      <td class="series-cell">
        <a href="{html.escape(m.get('wish_url',''))}" target="_blank" class="wish-link"
           title="Voir l'album sur Bedetheque">{html.escape(m['series_name'])}</a>
      </td>
      <td class="price">{html.escape(m['prix'] or '?')} &#8364;</td>
      <td>{html.escape(m['etat'] or '&mdash;')}</td>
      <td class="eo">{html.escape(m['eo'] or '&mdash;')}</td>
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
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
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
    background: #263238; color: #eceff1; padding: .7rem 1rem; font-size: .95rem;
  }}
  .seller-link {{ color: #80cbc4; text-decoration: none; font-weight: bold; }}
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
  .sale-link {{ color: #b71c1c; text-decoration: none; font-weight: 500; }}
  .sale-link:hover {{ text-decoration: underline; }}
  .wish-link {{ color: #555; text-decoration: none; font-size: .82rem; }}
  .wish-link:hover {{ text-decoration: underline; color: #b71c1c; }}
  .series-cell {{ color: #666; }}
  .price {{ font-weight: bold; color: #b71c1c; white-space: nowrap; }}
  .eo {{ color: #555; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>&#128218; Wishlist BD &mdash; Annonces en vente</h1>
    <p>Utilisateur&nbsp;: <strong>jcarreno</strong> &nbsp;&middot;&nbsp;
       <strong>{len(results)}</strong> annonce(s) correspondant exactement
       &agrave; la wishlist, chez <strong>{len(by_seller)}</strong> vendeur(s)</p>
  </header>
  <table>
    <thead>
      <tr>
        <th>Album en vente</th>
        <th>Serie</th>
        <th>Prix</th>
        <th>Etat</th>
        <th>EO</th>
      </tr>
    </thead>
    <tbody>{body}
    </tbody>
  </table>
  <p style="font-size:.82rem;color:#888">
    Seuls les albums figurant exactement dans la wishlist sont affich&eacute;s.
    Cliquez sur le titre pour acc&eacute;der &agrave; l'annonce.
  </p>
</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"HTML report saved: {path}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("BD Wishlist vs Bedetheque Sales Matcher  (v3 - exact match)")
    print("=" * 60)

    session = requests.Session()

    # 1. Scrape the full wishlist
    wishlist_by_album_id, series_map = scrape_wishlist(session)
    if not wishlist_by_album_id:
        print("ERROR: Could not load the wishlist.")
        sys.exit(1)

    # 2 & 3. For each series, get sale listings, resolve album IDs, match
    print(f"\nSearching sales and matching against wishlist "
          f"({len(series_map)} series)...")
    results = find_matches(wishlist_by_album_id, series_map, session)

    print(f"\n  Total exact matches: {len(results)}")

    # 4. Output
    print_console(results)
    save_csv(results, "results.csv")
    save_html(results, "results.html")

    print("\nDone!")


if __name__ == "__main__":
    main()