#!/usr/bin/env python3
"""
ECM Catalog Updater
-------------------
Queries MusicBrainz for new ECM Records releases and adds them to index.html.

Usage:
  python update_catalog.py [--dry-run]

Requires only the Python standard library (no pip install needed).
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

DRY_RUN    = '--dry-run' in sys.argv
INDEX_FILE = 'index.html'
MB_BASE    = 'https://musicbrainz.org/ws/2'
USER_AGENT = 'ECMCatalogUpdater/1.0 (ale.michetti@gmail.com)'
RATE_LIMIT  = 1.1   # seconds between requests (MB allows 1/sec)


# ── HTTP helper ────────────────────────────────────────────────────────────
def mb_get(path, params=None):
    """GET from MusicBrainz API, returns parsed JSON."""
    if params:
        path += '?' + urllib.parse.urlencode(params)
    url = MB_BASE + path
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f'  [warn] MB request failed: {url} — {e}')
        return None


# ── Parse existing catalog from index.html ─────────────────────────────────
def load_albums(html):
    """Extract the ALBUMS array from index.html. Returns (albums_list, prefix, suffix)."""
    m = re.search(r'const ALBUMS = (\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError('Could not find ALBUMS array in index.html')
    prefix = html[:m.start(1)]
    suffix = html[m.end(1):]
    albums = json.loads(m.group(1))
    return albums, prefix, suffix


def known_catalogs(albums):
    """Return set of catalog numbers already in the list, normalised."""
    return {a['catalog'].strip().upper() for a in albums}


def latest_year(albums):
    """Return the most recent year present in the catalog."""
    years = [int(a['year']) for a in albums if a.get('year') and str(a['year']).isdigit()]
    return max(years) if years else 2020


# ── MusicBrainz helpers ────────────────────────────────────────────────────
def get_ecm_label_mbid():
    """Search for ECM Records label and return its MBID."""
    print('Searching for ECM Records label on MusicBrainz…')
    time.sleep(RATE_LIMIT)
    data = mb_get('/label', {'query': 'label:"ECM Records" AND country:DE', 'fmt': 'json', 'limit': 5})
    if not data:
        return None
    for label in data.get('labels', []):
        name = label.get('name', '')
        if 'ECM' in name.upper():
            print(f'  Found: {name} — MBID: {label["id"]}')
            return label['id']
    return None


def fetch_label_releases(label_mbid, from_year):
    """
    Paginate through all releases for the ECM label,
    returning only those from from_year onwards.
    """
    print(f'Fetching releases from MusicBrainz (from {from_year})…')
    releases = []
    offset   = 0
    limit    = 100

    while True:
        time.sleep(RATE_LIMIT)
        data = mb_get('/release', {
            'label':  label_mbid,
            'fmt':    'json',
            'limit':  limit,
            'offset': offset,
            'inc':    'labels+artist-credits',
        })
        if not data:
            break

        batch = data.get('releases', [])
        if not batch:
            break

        for rel in batch:
            date = rel.get('date', '') or ''
            year_str = date[:4] if date else ''
            if year_str.isdigit() and int(year_str) >= from_year:
                releases.append(rel)

        total = data.get('release-count', 0)
        offset += limit
        print(f'  Fetched {min(offset, total)}/{total}…')
        if offset >= total:
            break

    return releases


# ── Catalog number helpers ─────────────────────────────────────────────────
def extract_catalog_number(release):
    """Return the best ECM catalog number for a release, or None."""
    for li in release.get('label-info', []):
        cat = (li.get('catalog-number') or '').strip()
        label_name = (li.get('label', {}) or {}).get('name', '') or ''
        if cat and 'ECM' in label_name.upper():
            return cat
    # Fallback: any catalog number that starts with ECM
    for li in release.get('label-info', []):
        cat = (li.get('catalog-number') or '').strip()
        if cat.upper().startswith('ECM') or cat.upper().startswith('JAPO'):
            return cat
    return None


def guess_series(catalog):
    """Derive series name from catalog number (e.g. ECM 2700 → 'ECM 2')."""
    cat = catalog.upper()
    if cat.startswith('JAPO'):
        return 'JAPO'
    m = re.match(r'ECM\s*(\d+)', cat)
    if m:
        num = int(m.group(1))
        if num >= 2000:
            return 'ECM 2'
        elif num >= 1000:
            return 'ECM 1'
        else:
            return 'ECM New Series'
    if 'NEW SERIES' in cat:
        return 'ECM New Series'
    return ''


def build_album(release, catalog):
    """Convert a MusicBrainz release to our album object format."""
    date     = release.get('date', '') or ''
    year_str = date[:4] if len(date) >= 4 else ''

    # Artist name
    artist_credits = release.get('artist-credit', [])
    artist_parts   = []
    artist_list    = []
    for ac in artist_credits:
        if isinstance(ac, str):
            artist_parts.append(ac)
        else:
            name = (ac.get('artist') or {}).get('name') or ac.get('name', '')
            if name:
                artist_list.append(name)
                artist_parts.append(name + (ac.get('joinphrase') or ''))
    artist_str = ''.join(artist_parts).strip(' ,/')
    if not artist_str and artist_list:
        artist_str = ' / '.join(artist_list)

    return {
        'id':          catalog,
        'catalog':     catalog,
        'year':        year_str,
        'artist':      artist_str,
        'artistList':  artist_list,
        'title':       release.get('title', '').strip(),
        'notes':       '',
        'series':      guess_series(catalog),
        'musicians':   '',
        'musicianList': [],
        'owned':       None,
        'sheet':       '',
    }


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    # Load current index.html
    try:
        with open(INDEX_FILE, encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        print(f'ERROR: {INDEX_FILE} not found. Run this script from the repo root.')
        sys.exit(1)

    albums, prefix, suffix = load_albums(html)
    existing = known_catalogs(albums)
    from_year = latest_year(albums)

    print(f'Current catalog: {len(albums)} albums, latest year: {from_year}')
    print(f'Looking for new releases from {from_year} onwards…\n')

    # Find ECM label MBID
    label_mbid = get_ecm_label_mbid()
    if not label_mbid:
        print('ERROR: Could not find ECM Records label on MusicBrainz.')
        sys.exit(1)

    # Fetch releases
    releases = fetch_label_releases(label_mbid, from_year)
    print(f'\nFound {len(releases)} MB releases from {from_year} onwards.\n')

    # Filter to genuinely new albums
    new_albums = []
    for rel in releases:
        catalog = extract_catalog_number(rel)
        if not catalog:
            continue
        if catalog.strip().upper() in existing:
            continue
        album = build_album(rel, catalog)
        if not album['title']:
            continue
        new_albums.append(album)
        print(f'  + {catalog} — {album["artist"]} — {album["title"]} ({album["year"]})')

    if not new_albums:
        print('No new albums to add. Catalog is up to date.')
        return

    print(f'\n{len(new_albums)} new album(s) to add.')

    if DRY_RUN:
        print('[dry-run] index.html NOT modified.')
        return

    # Sort new albums by catalog number and append
    def sort_key(a):
        m = re.search(r'(\d+)', a['catalog'])
        return int(m.group(1)) if m else 0

    new_albums.sort(key=sort_key)
    albums.extend(new_albums)

    # Write back to index.html
    new_json = json.dumps(albums, ensure_ascii=False, separators=(',', ':'))
    new_html = prefix + new_json + suffix
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print(f'index.html updated: {len(albums)} albums total (+{len(new_albums)} new).')


if __name__ == '__main__':
    main()
