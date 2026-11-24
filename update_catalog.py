#!/usr/bin/env python3
"""
update_catalog.py (versione Supabase)
-------------------------------------
Cerca su MusicBrainz le nuove uscite ECM e le inserisce nella tabella `albums`
di Supabase. Gli album già presenti non vengono toccati.
Copertine e musicisti vengono poi completati da download_covers.py e
enrich_musicians.py (eseguiti dallo stesso workflow).

Uso:  python update_catalog.py [--dry-run] [--from-year AAAA]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY, MB_CONTACT (opzionale: contatto per MusicBrainz)
"""
import json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

DRY_RUN = '--dry-run' in sys.argv
SB_URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
SB_KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
MB = 'https://musicbrainz.org/ws/2'
UA = f"NordicgrundCatalog/2.1 ({os.environ.get('MB_CONTACT') or 'https://nordicgrund.com'})"
RATE = 1.1
WAITS = [5, 15, 30, 60]
# "ECM Records" (Edition of Contemporary Music) su MusicBrainz
ECM_LABEL_MBID = '1edf842b-d727-4d27-8db6-f31882443f89'


def arg_value(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


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
                print(f'  [retry] HTTP {e.code}, attendo {WAITS[i]}s', flush=True)
                time.sleep(WAITS[i]); continue
            print(f'  [warn] HTTP {e.code} {e.read()[:150]!r}', flush=True)
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'  [warn] {e}', flush=True)
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


def norm_cat(cat):
    """'ECM 2880 5780292' -> 'ECM 2880'; 'ECM New Series 2751' -> 'ECM 2751'; '2857' -> 'ECM 2857'."""
    c = re.sub(r'\s+', ' ', (cat or '').strip())
    c = re.sub(r'(?i)^ECM\s*New\s*Series\s*', 'ECM ', c)
    m = re.match(r'(?i)^(ECM|JAPO)\s*(\d{3,5}(?:\s*/\s*\d{1,4})?)', c)
    if m:
        num = re.sub(r'\s+', '', m.group(2))
        return m.group(1).upper() + ' ' + num
    if re.fullmatch(r'\d{4}', c):
        return f'ECM {c}'
    return None


def catalog_number(rel):
    infos = rel.get('label-info', []) or []
    ecm_label = lambda li: 'ECM' in (((li.get('label') or {}).get('name')) or '').upper()
    # prima i numeri che iniziano con ECM/JAPO, poi i 4 cifre "nudi" su etichetta ECM
    for li in infos:
        cat = (li.get('catalog-number') or '').strip()
        if re.match(r'(?i)^(ECM|JAPO)\s*(New\s*Series\s*)?\d', cat):
            n = norm_cat(cat)
            if n:
                return n
    for li in infos:
        cat = (li.get('catalog-number') or '').strip()
        if ecm_label(li) and re.fullmatch(r'\d{4}', cat):
            return norm_cat(cat)
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
        'mbid': rel.get('id'), 'rg_mbid': (rel.get('release-group') or {}).get('id'),
    }


def main():
    if not SB_URL or not SB_KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    existing_rows = load_existing()
    if not existing_rows:
        sys.exit('ERRORE: impossibile leggere gli album da Supabase')
    existing = set()
    for r in existing_rows:
        c = (r.get('catalog') or '').strip()
        existing.add(c.upper())
        n = norm_cat(c)
        if n:
            existing.add(n.upper())
    years = [int(r['year']) for r in existing_rows if str(r.get('year') or '').isdigit()]
    from_year = int(arg_value('--from-year') or 0) or ((max(years) - 1) if years else 2020)
    print(f'Catalogo: {len(existing_rows)} album, ultimo anno {max(years) if years else "?"}; cerco uscite dal {from_year}', flush=True)

    new, offset, total = {}, 0, None
    while True:
        d = mb_get('/release', {'label': ECM_LABEL_MBID, 'limit': 100, 'offset': offset,
                                'inc': 'labels+artist-credits+release-groups'})
        if not d or 'releases' not in d:
            sys.exit('ERRORE: MusicBrainz non risponde come previsto, nessun dato letto')
        total = d.get('release-count', 0)
        for rel in d['releases']:
            y = (rel.get('date') or '')[:4]
            if not (y.isdigit() and int(y) >= from_year):
                continue
            cat = catalog_number(rel)
            # JAPO e' stata tolta dal catalogo: le sue ristampe non vanno riaggiunte
            if not cat or cat.upper().startswith('JAPO') or cat.upper() in existing:
                continue
            row = build_row(rel, cat)
            if not row['title']:
                continue
            old = new.get(cat)
            # se lo stesso numero compare piu' volte (CD, LP, digitale) tiene la data piu' vecchia
            if old is None or (row['year'] or '9999') < (old['year'] or '9999'):
                new[cat] = row
        offset += 100
        print(f'  letti {min(offset, total)}/{total}', flush=True)
        if offset >= total or not d['releases']:
            break

    if not new:
        print('Nessun nuovo album. Catalogo aggiornato.')
        return
    for cat in sorted(new):
        r = new[cat]
        print(f'  + {cat} — {r["artist"]} — {r["title"]} ({r["year"]})')
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
