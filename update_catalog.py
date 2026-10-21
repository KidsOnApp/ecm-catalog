#!/usr/bin/env python3
"""
update_catalog.py (versione Supabase)
-------------------------------------
Cerca su MusicBrainz le nuove uscite ECM e le inserisce nella tabella `albums`
di Supabase. Gli album già presenti non vengono toccati.
Copertine e musicisti vengono poi completati da download_covers.py e
enrich_musicians.py (eseguiti dallo stesso workflow).

Uso:  python update_catalog.py [--dry-run]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

DRY_RUN = '--dry-run' in sys.argv
SB_URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
SB_KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
MB = 'https://musicbrainz.org/ws/2'
UA = 'ECMCatalogUpdater/2.0 (ale.michetti@gmail.com)'
RATE = 1.1
WAITS = [5, 15, 30, 60]
ECM_LABEL_MBID = '0538c84c-a5ab-4dbe-a47e-2f60bdc8d7cb'


def http_json(url, headers, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    for i in range(len(WAITS) + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else True
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and i < len(WAITS):
                print(f'  [retry] HTTP {e.code}, attendo {WAITS[i]}s')
                time.sleep(WAITS[i]); continue
            print(f'  [warn] HTTP {e.code} {url[:90]} {e.read()[:150]!r}')
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'  [warn] {e}')
            return None


def mb_get(path, params):
    time.sleep(RATE)
    return http_json(f'{MB}{path}?{urllib.parse.urlencode(dict(params, fmt="json"))}', {'User-Agent': UA})


def sb(method, path, body=None, extra=None):
    h = {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}', 'Content-Type': 'application/json'}
    h.update(extra or {})
    return http_json(SB_URL + path, h, method, body)


def load_existing():
    rows, off = [], 0
    while True:
        b = sb('GET', f'/rest/v1/albums?select=catalog,year&order=catalog&limit=1000&offset={off}')
        if not isinstance(b, list) or not b:
            break
        rows += b
        if len(b) < 1000:
            break
        off += 1000
    return rows


def guess_series(catalog):
    cat = catalog.upper()
    if cat.startswith('JAPO'):
        return 'JAPO'
    m = re.match(r'ECM\s*(\d+)', cat)
    if m:
        n = int(m.group(1))
        return 'ECM 2' if n >= 2000 else 'ECM 1' if n >= 1000 else 'ECM New Series'
    return 'ECM New Series' if 'NEW SERIES' in cat else ''


def catalog_number(rel):
    for li in rel.get('label-info', []):
        cat = (li.get('catalog-number') or '').strip()
        if cat and 'ECM' in ((li.get('label') or {}).get('name', '') or '').upper():
            return cat
    for li in rel.get('label-info', []):
        cat = (li.get('catalog-number') or '').strip()
        if cat.upper().startswith(('ECM', 'JAPO')):
            return cat
    return None


def build_row(rel, catalog):
    year = (rel.get('date') or '')[:4]
    names, parts = [], []
    for ac in rel.get('artist-credit', []):
        if isinstance(ac, dict):
            n = (ac.get('artist') or {}).get('name') or ac.get('name', '')
            if n:
                names.append(n)
                parts.append(n + (ac.get('joinphrase') or ''))
    return {
        'id': catalog, 'catalog': catalog, 'year': year or None,
        'artist': ''.join(parts).strip(' ,/') or ' / '.join(names),
        'artist_list': names, 'title': (rel.get('title') or '').strip(),
        'notes': '', 'series': guess_series(catalog),
        'musicians': '', 'musician_list': [],
        'cover_url': None, 'cover_ok': False,
        'mbid': rel.get('id'), 'rg_mbid': None,
    }


def main():
    if not SB_URL or not SB_KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    existing_rows = load_existing()
    if not existing_rows:
        sys.exit('ERRORE: impossibile leggere gli album da Supabase')
    existing = {r['catalog'].strip().upper() for r in existing_rows}
    years = [int(r['year']) for r in existing_rows if str(r.get('year') or '').isdigit()]
    from_year = max(years) if years else 2020
    print(f'Catalogo: {len(existing_rows)} album, ultimo anno {from_year}')

    new, offset = {}, 0
    while True:
        d = mb_get('/release', {'label': ECM_LABEL_MBID, 'limit': 100, 'offset': offset,
                                'inc': 'labels+artist-credits'})
        if not d or not d.get('releases'):
            break
        for rel in d['releases']:
            y = (rel.get('date') or '')[:4]
            if not (y.isdigit() and int(y) >= from_year):
                continue
            cat = catalog_number(rel)
            if not cat or cat.strip().upper() in existing or cat in new:
                continue
            row = build_row(rel, cat)
            if row['title']:
                new[cat] = row
                print(f'  + {cat} — {row["artist"]} — {row["title"]} ({row["year"]})')
        offset += 100
        print(f'  letti {min(offset, d.get("release-count", 0))}/{d.get("release-count", 0)}')
        if offset >= d.get('release-count', 0):
            break

    if not new:
        print('Nessun nuovo album. Catalogo aggiornato.')
        return
    print(f'\n{len(new)} nuovi album.')
    if DRY_RUN:
        print('[dry-run] nulla scritto.')
        return
    r = sb('POST', '/rest/v1/albums', list(new.values()),
           {'Prefer': 'resolution=ignore-duplicates,return=minimal'})
    if r is None:
        sys.exit('ERRORE: inserimento in Supabase fallito')
    print('Inseriti in Supabase.')


if __name__ == '__main__':
    main()
