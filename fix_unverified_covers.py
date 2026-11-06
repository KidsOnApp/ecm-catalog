#!/usr/bin/env python3
"""
fix_unverified_covers.py
------------------------
Controlla le copertine degli album che NON hanno un MBID (cioe' trovate per
titolo/artista e quindi non verificate) e cerca l'edizione ECM giusta su
MusicBrainz usando il numero di catalogo esatto.

- Un'edizione e' accettata solo se il suo numero di catalogo coincide con quello
  dell'album (es. 'ECM 1330-32' = 'ECM 1330/32').
- La copertina viene presa da Cover Art Archive (release, poi release-group).
- Per default e' una simulazione: stampa cosa farebbe e non scrive nulla.
  Con --apply sostituisce le copertine trovate (nuovo file in Storage, il vecchio
  resta) e salva mbid e rg_mbid.
- Con --clear-unmatched (solo insieme a --apply) toglie la copertina agli album
  per cui non si trova nessuna edizione verificata.

Uso:  python fix_unverified_covers.py [--apply] [--clear-unmatched] [--limit N]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import argparse, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

MB = 'https://musicbrainz.org/ws/2'
CAA = 'https://coverartarchive.org'
UA = 'NordicgrundCatalog/1.0 (+https://nordicgrund.com)'
WAITS = [5, 15, 30, 60]


def req(url, headers=None, method='GET', body=None, raw=False, timeout=60):
    h = {'User-Agent': UA}
    h.update(headers or {})
    data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
    for i in range(len(WAITS) + 1):
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                b = resp.read()
                if raw:
                    return b
                return json.loads(b) if b else True
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < len(WAITS):
                time.sleep(WAITS[i]); continue
            if e.code != 404:
                print(f'    [HTTP {e.code}] {url[:90]}')
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'    [errore] {e}')
            return None


def mb(path, params):
    time.sleep(1.2)
    return req(f'{MB}{path}?{urllib.parse.urlencode(dict(params, fmt="json"))}')


def sb(method, path, body=None, extra=None, raw_body=None, ctype='application/json'):
    h = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': ctype}
    h.update(extra or {})
    return req(URL + path, h, method, raw_body if raw_body is not None else body)


def canon(catalog):
    """('ECM', 1330, 1332) a partire da 'ECM 1330-32', 'ECM 1330/32', 'ECM 1330'."""
    c = (catalog or '').upper()
    prefix = 'JAPO' if c.startswith('JAPO') else 'ECM' if 'ECM' in c else None
    nums = re.findall(r'\d+', c)
    if not prefix or not nums:
        return None
    a = int(nums[0])
    b = None
    if len(nums) > 1:
        s = nums[1]
        first = nums[0]
        b = int(first[:len(first) - len(s)] + s) if len(s) < len(first) else int(s)
    return (prefix, a, b)


def find_candidates(catalog):
    """Release di MusicBrainz con lo stesso numero di catalogo, ordinate per preferenza."""
    want = canon(catalog)
    if not want:
        return None, []
    prefix, a, b = want
    tries = []
    if b:
        tries += [f'{prefix} {a}/{str(b)[-2:]}', f'{prefix} {a}-{str(b)[-2:]}']
    tries += [f'{prefix} {a}']
    found = {}
    for catno in tries:
        d = mb('/release', {'query': f'catno:"{catno}"', 'limit': 25})
        for r in (d or {}).get('releases') or []:
            for li in r.get('label-info') or []:
                if canon(li.get('catalog-number') or '') == want:
                    found[r['id']] = r
        if found:
            break
    rels = sorted(found.values(), key=lambda r: (r.get('status') != 'Official', r.get('date') or '9999'))
    return want, rels


def fetch_cover(rel):
    """Prima la copertina della release, poi quella del release-group."""
    img = req(f'{CAA}/release/{rel["id"]}/front-500', raw=True)
    if img and len(img) > 2000:
        return img, 'release'
    rg = (rel.get('release-group') or {}).get('id')
    if rg:
        img = req(f'{CAA}/release-group/{rg}/front-500', raw=True)
        if img and len(img) > 2000:
            return img, 'release-group'
    return None, None


def load_albums():
    out, off = [], 0
    while True:
        b = sb('GET', '/rest/v1/albums?select=id,catalog,title,artist,cover_url,mbid'
                      '&cover_url=not.is.null&mbid=is.null&order=catalog&limit=1000&offset=%d' % off)
        if not isinstance(b, list) or not b:
            break
        out += b
        if len(b) < 1000:
            break
        off += 1000
    return out


def main():
    global URL, KEY
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--clear-unmatched', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
    KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
    if not URL or not KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')

    albums = load_albums()
    if a.limit:
        albums = albums[:a.limit]
    print(f'{len(albums)} album con copertina e senza MBID. Modalita: {"APPLICA" if a.apply else "SIMULAZIONE"}\n')
    res = {'ok': [], 'nomatch': [], 'noart': [], 'skip': []}
    for i, al in enumerate(albums, 1):
        cat, title = al['catalog'], al.get('title') or ''
        want, rels = find_candidates(cat)
        if want is None:
            print(f'[{i}] SKIP     {cat} | {title[:40]} (numero di catalogo non riconosciuto)')
            res['skip'].append(cat); continue
        if not rels:
            print(f'[{i}] NOMATCH  {cat} | {title[:40]} (nessuna release con questo numero)')
            res['nomatch'].append(cat)
            if a.apply and a.clear_unmatched:
                sb('PATCH', f'/rest/v1/albums?id=eq.{urllib.parse.quote(al["id"])}',
                   {'cover_url': None, 'cover_ok': False}, {'Prefer': 'return=minimal'})
            continue
        img, kind, used = None, None, None
        for rel in rels[:4]:
            img, kind = fetch_cover(rel)
            if img:
                used = rel; break
        if not img:
            print(f'[{i}] NOART    {cat} | {title[:40]} ({len(rels)} release, nessuna copertina)')
            res['noart'].append(cat)
            if a.apply and a.clear_unmatched:
                sb('PATCH', f'/rest/v1/albums?id=eq.{urllib.parse.quote(al["id"])}',
                   {'cover_url': None, 'cover_ok': False}, {'Prefer': 'return=minimal'})
            continue
        rgid = (used.get('release-group') or {}).get('id')
        print(f'[{i}] OK       {cat} | {title[:40]} <- {used.get("title","")[:40]} [{used["id"]}] ({kind})')
        res['ok'].append(cat)
        if a.apply:
            base = os.path.splitext(os.path.basename(urllib.parse.urlparse(al['cover_url']).path))[0] or re.sub(r'\W+', '_', cat)
            name = f'{base}_mb.jpg'
            r = sb('POST', f'/storage/v1/object/covers/{urllib.parse.quote(name)}', None,
                   {'x-upsert': 'true'}, raw_body=img, ctype='image/jpeg')
            if r is None:
                print('    [errore] caricamento in Storage fallito'); continue
            new_url = f'{URL}/storage/v1/object/public/covers/{urllib.parse.quote(name)}'
            sb('PATCH', f'/rest/v1/albums?id=eq.{urllib.parse.quote(al["id"])}',
               {'cover_url': new_url, 'cover_ok': True, 'mbid': used['id'], 'rg_mbid': rgid},
               {'Prefer': 'return=minimal'})

    print('\nRIEPILOGO:', {k: len(v) for k, v in res.items()})
    for k in ('nomatch', 'noart', 'skip'):
        if res[k]:
            print(f'{k.upper()}:', ', '.join(res[k]))


if __name__ == '__main__':
    main()
