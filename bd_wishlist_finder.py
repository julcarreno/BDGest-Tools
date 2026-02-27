#!/usr/bin/env python3
"""
BD Wishlist vs Bedetheque Sales Matcher
========================================
Scrapes the wishlist from BDGest and finds matching items for sale
on Bedetheque, grouped by seller.

Usage:
    pip install requests beautifulsoup4 --break-system-packages
    python bd_wishlist_matcher.py

Output:
    - Console output grouped by seller
    - results.html  — a nicely formatted HTML report
    - results.csv   — a CSV for further processing
"""

import re
import csv
import time
import html
import sys
from collections import defaultdict
from urllib.parse import urlencode, urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing required libraries...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "requests", "beautifulsoup4", "--break-system-packages",
                           "-q"])
    import requests
    from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────

WISHLIST_USER_ID   = "71812"
WISHLIST_COLLECTION = "1"
WISHLIST_BASE_URL  = "https://www.bdgest.com/online/wishlist"
SALES_SEARCH_URL   = "https://www.bedetheque.com/ventes/search"

# Polite delay between requests (seconds). Increase if you get rate-limited.
REQUEST_DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_page(url, params=None, session=None):
    """Fetch a page and return a BeautifulSoup object, or None on error."""
    requester = session or requests
    try:
        resp = requester.get(url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        print(f"  ⚠  Request error for {url}: {e}")
        return None


def extract_series_id(href):
    """Extract numeric series ID from a bedetheque series URL.

    Example: /serie-59-BD-Asterix.html → 59
    """
    m = re.search(r"/serie-(\d+)-", href)
    return m.group(1) if m else None


def extract_album_id(href):
    """Extract numeric album ID from a bedetheque album URL.

    Example: /BD-Asterix-Tome-1-...-22940.html → 22940
    """
    m = re.search(r"-(\d+)\.html$", href)
    return m.group(1) if m else None


# ── Step 1 – Scrape the Wishlist ───────────────────────────────────────────────

def scrape_wishlist(session):
    """Return a list of wishlist items and a dict of unique series.

    Each wishlist item:
        {
            "series_id":   "59",
            "series_name": "Asterix",
            "series_url":  "https://www.bedetheque.com/serie-59-BD-Asterix.html",
            "album_id":    "22940",
            "album_title": "1. Astérix le Gaulois",
            "album_url":   "https://www.bedetheque.com/BD-Asterix-Tome-1-...-22940.html",
        }
    """
    print("\n📚 Scraping wishlist…")
    wishlist_items = []
    series_map = {}   # series_id → {name, url}
    page = 0

    while True:
        params = {
            "IdUser":       WISHLIST_USER_ID,
            "IdCollection": WISHLIST_COLLECTION,
            "Lettre":       "",
            "Page":         page,
        }
        soup = get_page(WISHLIST_BASE_URL, params=params, session=session)
        if soup is None:
            break

        # Each album is in a <li> inside the main <ul> (no special class needed)
        # The series link looks like: <a href="/serie-59-BD-Asterix.html">
        # The album  link looks like: <a href="/BD-Asterix-Tome-1-...-22940.html">

        items_on_page = 0
        for li in soup.select("ul > li"):
            # Series link
            series_link = li.select_one('a[href*="/serie-"]')
            # Album link (the "Acheter" link or the title link)
            album_link  = li.select_one('a[href*="/BD-"]')

            if not series_link or not album_link:
                continue

            series_href = series_link.get("href", "")
            album_href  = album_link.get("href", "")

            series_id = extract_series_id(series_href)
            if not series_id:
                continue

            album_id = extract_album_id(album_href)

            # Album title: look for the text node that is NOT inside an <a>
            # It's usually the text between <img> tags and the <ul> metadata
            title_tag = li.select_one("div:not([class])")
            if title_tag:
                album_title = title_tag.get_text(" ", strip=True)
            else:
                # Fallback: use the album link text
                album_title = album_link.get_text(strip=True)

            series_name = series_link.get_text(strip=True)
            series_url  = urljoin("https://www.bedetheque.com", series_href)
            album_url   = urljoin("https://www.bedetheque.com", album_href)

            wishlist_items.append({
                "series_id":   series_id,
                "series_name": series_name,
                "series_url":  series_url,
                "album_id":    album_id or "",
                "album_title": album_title,
                "album_url":   album_url,
            })

            if series_id not in series_map:
                series_map[series_id] = {
                    "name": series_name,
                    "url":  series_url,
                }
            items_on_page += 1

        print(f"  Page {page}: found {items_on_page} items "
              f"(total so far: {len(wishlist_items)})")

        # Check for next page link
        next_links = soup.select('a[href*="Page="]')
        next_page = page + 1
        has_next = any(
            str(next_page) in (lnk.get("href", "") + lnk.get_text())
            for lnk in next_links
        )
        # Also stop if this page had no items
        if not items_on_page or not has_next:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    print(f"  ✅ Wishlist: {len(wishlist_items)} albums across "
          f"{len(series_map)} unique series.")
    return wishlist_items, series_map


# ── Step 2 – Scrape Sales for each Series ─────────────────────────────────────

def scrape_sales_for_series(series_id, series_name, session):
    """Return list of sale listings for a given series ID.

    Each listing:
        {
            "series_id":   "59",
            "series_name": "Asterix",
            "album_title": "Astérix et les Goths",
            "tome":        "3",
            "album_id":    "22944",
            "prix":        "5.00",
            "etat":        "Très bon",
            "vendeur":     "michdupont",
            "annonce_url": "https://www.bedetheque.com/ventes/...",
        }
    """
    listings = []
    page = 1

    while True:
        params = {
            "RechIdSerie": series_id,
            "RechSerie":   "",
            "RechAuteur":  "",
            "RechEditeur": "",
            "RechPrixMin": "",
            "RechPrixMax": "",
            "RechVendeur": "",
            "RechPays":    "",
            "RechEtat":    "",
            "Page":        page,
        }
        soup = get_page(SALES_SEARCH_URL, params=params, session=session)
        if soup is None:
            break

        # Each listing row — the sales table uses <tr class="ligne*">
        rows = soup.select("tr.ligne1, tr.ligne2, tr[class*='ligne']")

        # Fallback: find any table rows with sale data
        if not rows:
            rows = soup.select("table.ventes tr, table tr")

        items_on_page = 0
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            # Try to extract data from table cells
            # Typical column order on bedetheque:
            # Couverture | Titre / Série | État | Prix | Vendeur | (actions)
            listing = parse_sale_row(row, series_id, series_name)
            if listing:
                listings.append(listing)
                items_on_page += 1

        if items_on_page == 0:
            break

        # Check for next page
        next_link = soup.select_one('a[href*="Page="]')
        if not next_link or f"Page={page+1}" not in next_link.get("href", ""):
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return listings


def parse_sale_row(row, series_id, series_name):
    """Parse a single table row from the sales results page."""
    cells = row.find_all("td")
    if not cells:
        return None

    text = " ".join(c.get_text(" ", strip=True) for c in cells)

    # Skip header rows or rows without pricing
    if not re.search(r"\d+[.,]\d{2}\s*€?|\d+\s*€", text):
        return None

    # Extract the sale detail link (annonce)
    link = row.select_one('a[href*="/ventes/"]')
    if not link:
        link = row.select_one("a[href]")
    annonce_url = urljoin("https://www.bedetheque.com",
                          link.get("href", "")) if link else ""

    # Extract seller — usually in a cell with a link to /profil/ or plain text
    vendeur = ""
    vendeur_link = row.select_one('a[href*="/profil/"], a[href*="vendeur"]')
    if vendeur_link:
        vendeur = vendeur_link.get_text(strip=True)
    else:
        # Look in the last few cells
        for cell in reversed(cells):
            t = cell.get_text(strip=True)
            if t and not re.match(r"^\d+[.,]\d{2}", t) and len(t) > 1:
                vendeur = t
                break

    # Extract price
    prix_match = re.search(r"(\d+[.,]\d{2})\s*€?", text)
    prix = prix_match.group(1).replace(",", ".") if prix_match else ""

    # Extract état (condition)
    etats = ["Neuf", "Très bon", "Bon", "Moyen", "Mauvais"]
    etat = ""
    for e in etats:
        if e.lower() in text.lower():
            etat = e
            break

    # Extract album title — usually in the 2nd cell
    album_title = ""
    if len(cells) >= 2:
        title_cell = cells[1]
        title_link = title_cell.select_one("a")
        album_title = (title_link.get_text(strip=True) if title_link
                       else title_cell.get_text(" ", strip=True))

    # Extract album ID from annonce URL or title link
    album_id = extract_album_id(annonce_url)

    if not album_title and not prix:
        return None

    return {
        "series_id":   series_id,
        "series_name": series_name,
        "album_title": album_title,
        "album_id":    album_id or "",
        "prix":        prix,
        "etat":        etat,
        "vendeur":     vendeur,
        "annonce_url": annonce_url,
    }


# ── Step 3 – Match Sales to Wishlist Items ─────────────────────────────────────

def match_sales_to_wishlist(wishlist_items, all_sales):
    """
    Match sales to wishlist by series_id (exact) and optionally album_id.
    Returns matched sale listings with wishlist info attached.
    """
    # Build wishlist index: series_id → list of album_ids wanted
    wishlist_by_series = defaultdict(list)
    for item in wishlist_items:
        wishlist_by_series[item["series_id"]].append(item)

    matched = []
    for sale in all_sales:
        sid = sale["series_id"]
        if sid not in wishlist_by_series:
            continue

        # The series is on the wishlist — check if this specific album is wanted
        wanted_albums = wishlist_by_series[sid]

        if sale["album_id"]:
            # Exact album match
            exact = [w for w in wanted_albums
                     if w["album_id"] == sale["album_id"]]
            if exact:
                for wish in exact:
                    matched.append({**sale, "wishlist_item": wish,
                                    "match_type": "exact"})
            else:
                # Album is from the right series but not specifically this volume
                # — still useful to show (user asked for "different editions")
                matched.append({**sale, "wishlist_item": wanted_albums[0],
                                "match_type": "series_match"})
        else:
            # No album ID on the sale, match by series
            matched.append({**sale, "wishlist_item": wanted_albums[0],
                            "match_type": "series_match"})

    return matched


# ── Step 4 – Output ────────────────────────────────────────────────────────────

def print_results(matched):
    """Print results grouped by seller."""
    if not matched:
        print("\n❌ Aucune correspondance trouvée.")
        return

    by_seller = defaultdict(list)
    for m in matched:
        by_seller[m["vendeur"] or "(vendeur inconnu)"].append(m)

    print(f"\n{'='*70}")
    print(f"  🎉 {len(matched)} annonce(s) trouvée(s) chez "
          f"{len(by_seller)} vendeur(s)")
    print(f"{'='*70}")

    for seller in sorted(by_seller.keys()):
        items = by_seller[seller]
        print(f"\n👤 Vendeur : {seller}  ({len(items)} annonce(s))")
        print(f"{'─'*60}")
        for m in sorted(items, key=lambda x: x["series_name"]):
            tag = "✓" if m["match_type"] == "exact" else "~"
            print(f"  {tag} {m['series_name']} — {m['album_title']}")
            print(f"      État : {m['etat'] or '?'}   "
                  f"Prix : {m['prix'] or '?'} €")
            if m["annonce_url"]:
                print(f"      URL  : {m['annonce_url']}")
    print()


def save_csv(matched, filename="results.csv"):
    """Save results to a CSV file."""
    if not matched:
        return
    fieldnames = ["vendeur", "series_name", "album_title", "etat",
                  "prix", "match_type", "annonce_url", "series_id"]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for m in sorted(matched, key=lambda x: (x["vendeur"], x["series_name"])):
            writer.writerow(m)
    print(f"📄 CSV enregistré : {filename}")


def save_html(matched, filename="results.html"):
    """Save a nicely formatted HTML report grouped by seller."""
    by_seller = defaultdict(list)
    for m in matched:
        by_seller[m["vendeur"] or "(vendeur inconnu)"].append(m)

    rows_html = ""
    for seller in sorted(by_seller.keys()):
        items = sorted(by_seller[seller], key=lambda x: x["series_name"])
        esc_seller = html.escape(seller)
        rows_html += f"""
        <tr class="seller-header">
            <td colspan="5">👤 {esc_seller} &nbsp;
                <span class="badge">{len(items)} annonce(s)</span>
            </td>
        </tr>"""
        for m in items:
            match_class = "exact" if m["match_type"] == "exact" else "series"
            annonce = (f'<a href="{html.escape(m["annonce_url"])}" '
                       f'target="_blank">Voir l\'annonce ↗</a>'
                       if m["annonce_url"] else "—")
            rows_html += f"""
        <tr class="item {match_class}">
            <td>{html.escape(m['series_name'])}</td>
            <td>{html.escape(m['album_title'])}</td>
            <td>{html.escape(m['etat'] or '?')}</td>
            <td>{html.escape(m['prix'] or '?')} €</td>
            <td>{annonce}</td>
        </tr>"""

    report = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Wishlist vs Petites Annonces BD</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 1100px; margin: 2rem auto;
          background: #f5f5f5; color: #222; }}
  h1 {{ color: #c0392b; }}
  h2 {{ color: #555; font-size: 1rem; font-weight: normal; margin-top: 0; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           box-shadow: 0 1px 4px rgba(0,0,0,.15); border-radius: 6px;
           overflow: hidden; margin-bottom: 1rem; }}
  th {{ background: #c0392b; color: white; padding: .6rem 1rem;
        text-align: left; }}
  td {{ padding: .5rem 1rem; border-bottom: 1px solid #eee; }}
  tr.seller-header td {{ background: #2c3e50; color: white; font-weight: bold;
                          font-size: 1.05rem; padding: .7rem 1rem; }}
  tr.item:hover td {{ background: #fef9f9; }}
  tr.exact td:first-child::before {{ content: "✓ "; color: #27ae60; }}
  tr.series td:first-child::before {{ content: "~ "; color: #e67e22; }}
  .badge {{ background: #e74c3c; color: white; border-radius: 12px;
            padding: 2px 8px; font-size: .85rem; }}
  a {{ color: #c0392b; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .legend {{ font-size: .85rem; color: #666; margin: .5rem 0 1.5rem; }}
  .legend span {{ margin-right: 1.5rem; }}
</style>
</head>
<body>
<h1>📚 Wishlist vs Petites Annonces Bedetheque</h1>
<h2>Utilisateur : jcarreno · {len(matched)} annonce(s) trouvée(s) chez
    {len(by_seller)} vendeur(s)</h2>
<p class="legend">
  <span>✓ = correspondance exacte (même album)</span>
  <span>~ = même série (édition différente possible)</span>
</p>
<table>
  <thead>
    <tr>
      <th>Série</th>
      <th>Titre / Tome</th>
      <th>État</th>
      <th>Prix</th>
      <th>Annonce</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
</body>
</html>"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"🌐 Rapport HTML enregistré : {filename}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("🔍 BD Wishlist ↔ Bedetheque Sales Matcher")
    print("=" * 50)

    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Scrape the full wishlist
    wishlist_items, series_map = scrape_wishlist(session)

    if not wishlist_items:
        print("❌ Impossible de lire la wishlist. Vérifiez l'URL ou votre connexion.")
        return

    # 2. Search for sales for each unique series
    print(f"\n🛒 Recherche des annonces pour {len(series_map)} séries…")
    all_sales = []

    for i, (series_id, series_info) in enumerate(series_map.items(), 1):
        name = series_info["name"]
        print(f"  [{i}/{len(series_map)}] {name} (id={series_id})", end="", flush=True)
        sales = scrape_sales_for_series(series_id, name, session)
        if sales:
            print(f" → {len(sales)} annonce(s) trouvée(s)")
            all_sales.extend(sales)
        else:
            print(" → aucune annonce")
        time.sleep(REQUEST_DELAY)

    print(f"\n  Total : {len(all_sales)} annonce(s) sur {len(series_map)} séries.")

    # 3. Match sales to wishlist
    print("\n🔗 Correspondances en cours…")
    matched = match_sales_to_wishlist(wishlist_items, all_sales)

    # 4. Output results
    print_results(matched)
    save_csv(matched, "results.csv")
    save_html(matched, "results.html")

    print("\n✅ Terminé !")


if __name__ == "__main__":
    main()