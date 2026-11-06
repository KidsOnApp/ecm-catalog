#!/usr/bin/env python3
"""
find_alt_covers.py
------------------
Cerca una copertina per album specifici (elenco di numeri di catalogo) da fonti
alternative, accettando solo candidati verificati:
  1. Discogs: ricerca per numero di catalogo; accettato se il catno coincide
     (es. 'ECM 1909/10' = 'ECM 1909/10') e l'etichetta e' ECM.
  2. iTunes: ricerca per artista e titolo; accettato se il campo copyright
     contiene 'ECM' e il titolo e' simile.
Per default e' una simulazione. Con --apply carica l'immagine in Storage
(<nome>_alt.jpg), imposta cover_url e cover_ok (mbid resta vuoto).

Uso:  python find_alt_covers.py --catalogs "ECM 1313;ECM 2001/02" [--apply]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCOGS_TOKEN (opzionale)
"""
import argparse, difflib, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

UA = 'NordicgrundCatalog/1.0 (+https://nordicgrund.com)'
WAITS = [5, 15, 30]


def req(url, headers=None, method='GET', body=None, raw=False):
    h = {'User-Agent': UA}
    h.update(headers or {})
    data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
    for i in range(len(WAITS) + 1):
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                b = resp.read()
                return b if raw else (json.loads(b) if b else True)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'    [HTTP {e.code}] {url[:80]}')
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'    [errore] {e}')
            return None


def sb(method, path, body=None, extra=None, raw_body=None, ctype='application/json'):
    h = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': ctype}
    h.update(extra or {})
    return req(URL + path, h, method, raw_body if raw_body is not None else body)


def canon(c):
    c = (c or '').upper()
    prefix = 'JAPO' if c.startswith('JAPO') else 'ECM' if 'ECM' in c else None
    nums = re.findall(r'\d+', c)
    if not prefix or not nums:
        return None
    a = int(nums[0]); b = None
    if len(nums) > 1:
        s, f = nums[1], nums[0]
        b = int(f[:len(f) - len(s)] + s) if len(s) < len(f) else int(s)
    return (prefix, a, b)


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def discogs(album):
    tok = (os.environ.get('DISCOGS_TOKEN') or '').strip()
    if not tok:
        return None
    want = canon(album['catalog'])
    tries = [album['catalog']]
    if want:
        tries += [f'{want[0]} {want[1]}/{str(want[2])[-2:]}' if want[2] else f'{want[0]} {want[1]}']
    if album['catalog'].lower().startswith('rarum'):
        tries.append(re.sub(r'(?i)^rarum\s*', 'ECM ', album['catalog']))
    for catno in dict.fromkeys(tries):
        time.sleep(1.1)
        q = urllib.parse.urlencode({'catno': catno, 'type': 'release', 'per_page': 10, 'token': tok})
        d = req('https://api.discogs.com/database/search?' + q)
        for r in (d or {}).get('results') or []:
            cn = r.get('catno') or ''
            labels = ' '.join(r.get('label') or []).lower()
            ok_cat = canon(cn) == want if want else norm(cn) == norm(album['catalog'])
            if ok_cat and ('ecm' in labels or 'rarum' in cn.lower()) and r.get('cover_image'):
                return {'src': 'discogs', 'url': r['cover_image'], 'info': f'{r.get("title")} [{cn}]'}
    return None


def itunes(album):
    term = f'{album.get("artist","")} {album.get("title","")}'
    q = urllib.parse.urlencode({'term': term, 'entity': 'album', 'limit': 15})
    time.sleep(1.0)
    d = req('https://itunes.apple.com/search?' + q)
    for r in (d or {}).get('results') or []:
        if 'ecm' not in (r.get('copyright') or '').lower():
            continue
        if sim(r.get('collectionName'), album.get('title')) < 0.75:
            continue
        art = (r.get('artworkUrl100') or '').replace('100x100bb', '1200x1200bb')
        if art:
            return {'src': 'itunes', 'url': art, 'info': f'{r.get("artistName")} - {r.get("collectionName")} | {r.get("copyright")}'}
    return None


def main():
    global URL, KEY
    ap = argparse.ArgumentParser()
    ap.add_argument('--catalogs', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
    KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
    if not URL or not KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    cats = [c.strip() for c in a.catalogs.split(';') if c.strip()]
    print(f'{len(cats)} album. Modalita: {"APPLICA" if a.apply else "SIMULAZIONE"}\n')
    found = 0
    for c in cats:
        rows = sb('GET', f'/rest/v1/albums?select=id,catalog,artist,title&catalog=eq.{urllib.parse.quote(c)}')
        if not isinstance(rows, list) or not rows:
            print(f'NON TROVATO  {c}'); continue
        al = rows[0]
        hit = discogs(al) or itunes(al)
        if not hit:
            print(f'NESSUNO      {c} | {al["title"][:50]}'); continue
        img = req(hit['url'], raw=True)
        if not img or len(img) < 3000:
            print(f'IMG KO       {c} | {hit["src"]} {hit["url"][:70]}'); continue
        found += 1
        print(f'TROVATO      {c} | {al["title"][:40]} <- {hit["src"]}: {hit["info"][:90]} ({len(img)//1024} KB)')
        if a.apply:
            name = re.sub(r'\W+', '_', c).strip('_') + '_alt.jpg'
            r = sb('POST', f'/storage/v1/object/covers/{urllib.parse.quote(name)}', None,
                   {'x-upsert': 'true'}, raw_body=img, ctype='image/jpeg')
            if r is None:
                print('    [errore] Storage'); continue
            sb('PATCH', f'/rest/v1/albums?id=eq.{urllib.parse.quote(al["id"])}',
               {'cover_url': f'{URL}/storage/v1/object/public/covers/{urllib.parse.quote(name)}', 'cover_ok': True},
               {'Prefer': 'return=minimal'})
    print(f'\nRIEPILOGO: {found}/{len(cats)} copertine trovate')


if __name__ == '__main__':
    main()
