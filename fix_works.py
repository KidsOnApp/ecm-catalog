#!/usr/bin/env python3
"""
fix_works.py
------------
Removes the manually-added "Works" placeholder albums from index.html
and replaces them with correct data fetched from MusicBrainz.

Run once from the repo root:
  python fix_works.py [--dry-run]

Requires only the Python standard library.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

DRY_RUN    = '--dry-run' in sys.argv
INDEX_FILE = 'index.html'
MB_BASE    = 'https://musicbrainz.org/ws/2'
USER_AGENT = 'ECMCatalogUpdater/1.0 (ale.michetti@gmail.com)'
RATE_LIMIT = 1.2   # seconds between MB requests


# ── HTTP ───────────────────────────────────────────────────────────────────
def mb_get(path, params=None):
    if params:
        path += '?' + urllib.parse.urlencode(params)
    url = MB_BASE + path
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f'  [warn] {url} — {e}')
        return None


# ── Parse / write index.html ───────────────────────────────────────────────
def load_albums(html):
    m = re.search(r'const ALBUMS = (\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError('ALBUMS array not found in index.html')
    return json.loads(m.group(1)), html[:m.start(1)], html[m.end(1):]


def save_albums(albums, prefix, suffix, path):
    new_json = json.dumps(albums, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(prefix + new_json + suffix)


# ── MusicBrainz search ─────────────────────────────────────────────────────
def search_works_for_artist(artist_name):
    """
    Search MusicBrainz for 'Works' compilation by this artist on ECM.
    Returns a list of candidate releases (may be empty).
    """
    # Try exact title match first, then broader
    queries = [
        f'artist:"{artist_name}" AND title:"Works" AND label:ECM',
        f'artist:"{artist_name}" AND title:Works AND label:ECM',
    ]
    for q in queries:
        time.sleep(RATE_LIMIT)
        data = mb_get('/release', {
            'query': q,
            'fmt':   'json',
            'limit': 5,
            'inc':   'labels+artist-credits',
        })
        if not data:
            continue
        hits = data.get('releases', [])
        # Keep only releases whose title is exactly "Works"
        exact = [r for r in hits if r.get('title', '').strip().lower() == 'works']
        if exact:
            return exact
        if hits:
            return hits[:3]
    return []


def fetch_release_detail(mbid):
    """Fetch a release with label-info and artist-credits."""
    time.sleep(RATE_LIMIT)
    return mb_get(f'/release/{mbid}', {'fmt': 'json', 'inc': 'labels+artist-credits'})


def extract_catalog(release):
    """Return best ECM catalog number from a release, or None."""
    for li in release.get('label-info', []):
        cat = (li.get('catalog-number') or '').strip()
        label_name = ((li.get('label') or {}).get('name') or '')
        if cat and 'ECM' in label_name.upper():
            return cat
    for li in release.get('label-info', []):
        cat = (li.get('catalog-number') or '').strip()
        if cat.upper().startswith('ECM') or cat.upper().startswith('JAPO'):
            return cat
    return None


def guess_series(catalog):
    cat = (catalog or '').upper()
    if cat.startswith('JAPO'):
        return 'JAPO'
    m = re.match(r'ECM\s*(\d+)', cat)
    if m:
        num = int(m.group(1))
        return 'ECM 2' if num >= 2000 else 'ECM 1' if num >= 1000 else 'ECM New Series'
    if 'NEW SERIES' in cat:
        return 'ECM New Series'
    return ''


def build_album(release, catalog, original):
    """Build an album dict from a MB release, preserving owned/wishlist status."""
    date = release.get('date', '') or ''
    year = date[:4] if len(date) >= 4 else original.get('year', '')

    ac_list = release.get('artist-credit', [])
    artist_parts, artist_list = [], []
    for ac in ac_list:
        if isinstance(ac, str):
            artist_parts.append(ac)
        else:
            name = ((ac.get('artist') or {}).get('name') or ac.get('name', ''))
            if name:
                artist_list.append(name)
                artist_parts.append(name + (ac.get('joinphrase') or ''))
    artist_str = ''.join(artist_parts).strip(' ,/')
    if not artist_str:
        artist_str = original.get('artist', '')
        artist_list = original.get('artistList', [])

    return {
        'id':           catalog,
        'catalog':      catalog,
        'year':         year,
        'artist':       artist_str,
        'artistList':   artist_list,
        'title':        release.get('title', 'Works').strip(),
        'notes':        original.get('notes', ''),
        'series':       guess_series(catalog),
        'musicians':    original.get('musicians', ''),
        'musicianList': original.get('musicianList', []),
        'owned':        original.get('owned', None),
        'sheet':        '',
    }


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    with open(INDEX_FILE, encoding='utf-8') as f:
        html = f.read()

    albums, prefix, suffix = load_albums(html)

    # Separate Works from the rest
    works_albums  = [a for a in albums if a.get('title', '').strip().lower() == 'works']
    other_albums  = [a for a in albums if a.get('title', '').strip().lower() != 'works']

    print(f'Found {len(works_albums)} "Works" placeholder albums to replace.\n')
    for a in works_albums:
        print(f'  - {a["catalog"]} | {a["artist"]}')
    print()

    if not works_albums:
        print('Nothing to do.')
        return

    # Group by artist to avoid duplicate searches
    # (there may be multiple Works entries per artist, e.g. Keith Jarrett has several)
    by_artist = {}
    for a in works_albums:
        by_artist.setdefault(a['artist'], []).append(a)

    replaced = []
    not_found = []

    for artist, originals in by_artist.items():
        print(f'Searching MusicBrainz for: {artist} — Works…')
        candidates = search_works_for_artist(artist)

        if not candidates:
            print(f'  [not found] No MB match for "{artist} Works". Keeping placeholder(s).')
            not_found.extend(originals)
            continue

        # If multiple candidates, fetch detail for each to get catalog number
        matched = []
        for cand in candidates:
            detail = fetch_release_detail(cand['id'])
            if not detail:
                continue
            cat = extract_catalog(detail)
            if not cat:
                print(f'  [skip] {cand["title"]} (no ECM catalog number)')
                continue
            # Check we're not duplicating an album already in other_albums
            existing_cats = {a['catalog'].strip().upper() for a in other_albums}
            if cat.strip().upper() in existing_cats:
                print(f'  [skip] {cat} already in catalog')
                continue
            album = build_album(detail, cat, originals[0])
            matched.append(album)
            print(f'  ✓ {cat} — {album["artist"]} — {album["title"]} ({album["year"]})')

        if matched:
            replaced.extend(matched)
        else:
            print(f'  [not found] No valid ECM catalog number found. Keeping placeholder(s).')
            not_found.extend(originals)

    print(f'\nSummary:')
    print(f'  Replaced: {len(replaced)} album(s)')
    print(f'  Not found / kept as-is: {len(not_found)} album(s)')

    if DRY_RUN:
        print('\n[dry-run] index.html NOT modified.')
        return

    # Rebuild album list: other_albums + replaced + not_found
    # Sort by a rough catalog-number key
    def sort_key(a):
        m = re.search(r'(\d+)', a.get('catalog', ''))
        return int(m.group(1)) if m else 0

    final = other_albums + replaced + not_found
    final.sort(key=sort_key)

    save_albums(final, prefix, suffix, INDEX_FILE)
    print(f'\nindex.html updated: {len(final)} albums total.')
    print('Run this once — the Works placeholders are now gone.')


if __name__ == '__main__':
    main()
