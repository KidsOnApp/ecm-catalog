#!/usr/bin/env python3
"""
update_eans.py
--------------
Raccoglie i codici a barre (EAN/UPC) delle pubblicazioni ECM da MusicBrainz e li collega
agli album del catalogo tramite il numero di catalogo ECM.

Colonne scritte in `albums` (con --apply):
  ean         text[]  codici a barre noti (13 cifre, con zero iniziale per gli UPC)
  dist_catno  text[]  numeri di distribuzione PolyGram/Universal (es. "533 680-2")

Come funziona:
  1. scarica da MusicBrainz tutte le release dell'etichetta ECM Records (paginato);
  2. per ogni release legge i numeri di catalogo: "ECM 1623" e/o "533 680-2";
  3. se il numero ECM manca ma c'e' il numero di distribuzione, lo ricava dalle altre release
     (tabella di corrispondenza); i codici 78118-2XXXX-2 contengono il numero ECM per intero;
  4. controlla la cifra di controllo EAN e il "decodificatore" (EAN -> numero di distribuzione).

Per default e' una simulazione. Uso:  python update_eans.py [--apply]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import argparse, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import defaultdict

UA = 'NordicgrundCatalog/1.0 (+https://nordicgrund.com)'
LABELS = re.compile(r'\bECM\b|rarum|JAPO', re.I)


def get(url, retries=4):
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < retries:
                time.sleep(6 * (i + 1)); continue
            return None
        except Exception:
            if i < retries:
                time.sleep(4); continue
            return None


def sb(method, path, body=None, extra=None):
    h = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json', 'User-Agent': UA}
    h.update(extra or {})
    data = json.dumps(body).encode() if body is not None else None
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(URL + path, data=data, method=method, headers=h), timeout=60) as r:
                b = r.read()
                return json.loads(b) if b else True
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < 3:
                time.sleep(5 * (i + 1)); continue
            print(f'    [Supabase HTTP {e.code}] {e.read()[:200]!r}')
            return None
        except Exception as e:
            if i < 3:
                time.sleep(3); continue
            print(f'    [errore] {e}')
            return None


def ean13(code):
    c = re.sub(r'\D', '', code or '')
    if len(c) == 12:
        c = '0' + c
    if len(c) != 13:
        return None
    s = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(c[:12]))
    return c if (10 - s % 10) % 10 == int(c[12]) else None


def norm_cat(c):
    return re.sub(r'\s+', ' ', (c or '').strip().upper())


def dist_key(c):
    """'533 680-2' / '533680-2' / '533 680 2' -> '5336802'; None se non e' un numero di distribuzione."""
    m = re.fullmatch(r'\s*(\d{3})\s?(\d{3})\s?-?\s?(\d)\s*', c or '')
    return ''.join(m.groups()) if m else None


def ecm_from_catno(c):
    c = norm_cat(c)
    m = re.fullmatch(r'78118-2(\d{4})-2', c)
    if m:
        return 'ECM ' + m.group(1)
    if re.match(r'^(ECM|RARUM|JAPO)\s*\d', c):
        return re.sub(r'^(ECM|RARUM|JAPO)\s*', lambda x: x.group(1).replace('RARUM', 'rarum') + ' ', c)
    return None


def ean_to_dist(ean):
    """Decodifica i codici PolyGram: prefisso 73145 / 04228 + ultime 5 cifre + '2' + controllo."""
    e = ean13(ean)
    if not e:
        return None
    u = e[1:]  # UPC-A 12 cifre
    if u[:5] in ('73145', '04228') and u[10] == '2':
        five = u[5:10]
        return five
    return None


def fetch_releases():
    rel, off = [], 0
    q = urllib.parse.quote('label:"ECM Records" OR label:"ECM"')
    while True:
        d = get(f'https://musicbrainz.org/ws/2/release/?query={q}&fmt=json&limit=100&offset={off}')
        time.sleep(1.1)
        if not d or not d.get('releases'):
            break
        rel += d['releases']
        off += 100
        print(f'  scaricate {len(rel)} / {d.get("count")}')
        if off >= (d.get('count') or 0):
            break
    return rel


def main():
    global URL, KEY
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
    KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
    if not URL or not KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')

    albums, off = [], 0
    while True:
        rows = sb('GET', '/rest/v1/albums?select=id,catalog&order=catalog&limit=1000&offset=%d' % off)
        if not isinstance(rows, list) or not rows:
            break
        albums += rows
        if len(rows) < 1000:
            break
        off += 1000
    by_cat = {norm_cat(r['catalog']): r for r in albums}
    print(f'{len(albums)} album nel catalogo. Modalita: {"APPLICA" if a.apply else "SIMULAZIONE"}\n')

    print('Scarico le release da MusicBrainz...')
    rels = fetch_releases()

    # 1) per ogni release: numeri ECM, numeri di distribuzione, barcode valido
    parsed, d2e = [], defaultdict(set)
    bad = 0
    for r in rels:
        ecm, dist = set(), set()
        for li in r.get('label-info') or []:
            c = li.get('catalog-number') or ''
            e, dk = ecm_from_catno(c), dist_key(c)
            if e: ecm.add(e)
            if dk: dist.add(dk)
        bc = ean13(r.get('barcode'))
        if r.get('barcode') and not bc:
            bad += 1
        parsed.append((ecm, dist, bc))
        for dk in dist:
            for e in ecm:
                d2e[dk].add(e)

    # 2) assegna i codici agli album
    ean, dcat = defaultdict(set), defaultdict(set)
    for ecm, dist, bc in parsed:
        keys = set(ecm)
        if not keys:
            for dk in dist:
                keys |= d2e.get(dk, set())
        for k in keys:
            if bc: ean[k].add(bc)
            for dk in dist:
                dcat[k].add(f'{dk[:3]} {dk[3:6]}-{dk[6]}')

    have = [c for c in by_cat if ean.get(c)]
    print(f'\nRelease lette: {len(rels)} (barcode non validi scartati: {bad})')
    print(f'Album con almeno un EAN: {len(have)} / {len(by_cat)}')
    print(f'Album con numero di distribuzione: {len([c for c in by_cat if dcat.get(c)])}')
    print(f'Numeri di distribuzione noti: {len(d2e)}')

    # 3) prova del decodificatore: EAN -> cinque cifre -> numero di distribuzione noto -> album giusto?
    ok = tot = 0
    for c in have:
        known = {dk[1:6] for dk in {x.replace(' ', '').replace('-', '') for x in dcat.get(c, ())}}
        for e in ean[c]:
            f = ean_to_dist(e)
            if f is not None and known:
                tot += 1
                ok += 1 if f in known else 0
    print(f'Decodificatore EAN -> numero di distribuzione: {ok}/{tot} coincidono con MusicBrainz')

    missing = sorted(c for c in by_cat if not ean.get(c))
    print(f'\nAlbum senza EAN ({len(missing)}), primi 40: {missing[:40]}')

    if a.apply:
        n = 0
        for c in have:
            row = by_cat[c]
            body = {'ean': sorted(ean[c]), 'dist_catno': sorted(dcat.get(c, []))}
            r = sb('PATCH', '/rest/v1/albums?id=eq.' + urllib.parse.quote(str(row['id'])), body,
                   {'Prefer': 'return=minimal'})
            if r is None:
                sys.exit('ERRORE: scrittura fallita (le colonne ean e dist_catno esistono?)')
            n += 1
        print(f'\nAggiornati {n} album.')


if __name__ == '__main__':
    main()
