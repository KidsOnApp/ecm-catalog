#!/usr/bin/env python3
"""
enrich_musicians.py
-------------------
Per ogni album senza MBID: cerca la release su MusicBrainz tramite numero di
catalogo, poi ne legge i crediti (strumenti / voci / direttori) e aggiorna
Supabase: mbid, rg_mbid, musicians (testo), musician_list (array JSON).
Gli album che hanno già musicisti inseriti a mano NON vengono sovrascritti.

Uso:  python enrich_musicians.py [--limit 50]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import argparse, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

MB = 'https://musicbrainz.org/ws/2'
UA = 'ECMCatalogApp/2.0 (ale.michetti@gmail.com)'
RATE = 1.2
WAITS = [5, 15, 30, 60]
PERF = {'instrument', 'vocal', 'performer', 'conductor', 'performing orchestra', 'orchestra', 'chorus master'}


def http_json(url, headers, method='GET', body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    for i in range(len(WAITS) + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else True
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and i < len(WAITS):
                time.sleep(WAITS[i]); continue
            if e.code == 404:
                return None
            print(f'    [HTTP {e.code}] {url[:90]}')
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'    [err] {e}')
            return None


def mb(path, params):
    time.sleep(RATE)
    params = dict(params, fmt='json')
    return http_json(f'{MB}{path}?{urllib.parse.urlencode(params)}', {'User-Agent': UA})


def sb(method, path, body=None, extra=None):
    url = SB_URL.rstrip('/') + path
    h = {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}', 'Content-Type': 'application/json'}
    if extra:
        h.update(extra)
    return http_json(url, h, method, body)


def get_albums(limit):
    out, off = [], 0
    while True:
        b = sb('GET', f'/rest/v1/albums?select=id,catalog,musician_list,mbid&mbid=is.null&order=catalog&limit=1000&offset={off}')
        if not isinstance(b, list) or not b:
            break
        out += b
        if len(b) < 1000:
            break
        off += 1000
    return out[:limit] if limit else out


def find_release(catalog):
    m = re.search(r'((?:ECM|JAPO)\s*[\dA-Z\-/ ]+)', catalog, re.I)
    catno = (m.group(1) if m else catalog).strip()
    d = mb('/release', {'query': f'catno:"{catno}"', 'limit': 5})
    rels = (d or {}).get('releases') or []
    if not rels:
        return None, None
    rels.sort(key=lambda r: (r.get('status') != 'Official', r.get('date') or '9999'))
    r = rels[0]
    return r['id'], (r.get('release-group') or {}).get('id')


def credits(mbid):
    d = mb(f'/release/{mbid}', {'inc': 'artist-rels+recording-rels+recording-level-rels'})
    if not d:
        return []
    found = {}  # name -> set(instruments)

    def add(rels):
        for rel in rels or []:
            if rel.get('type') not in PERF or not rel.get('artist'):
                continue
            name = rel['artist'].get('name')
            if not name:
                continue
            ins = found.setdefault(name, set())
            for a in rel.get('attributes') or []:
                if a not in ('additional', 'guest', 'solo', 'co', 'assistant'):
                    ins.add(a)
            if rel.get('type') == 'vocal' and not rel.get('attributes'):
                ins.add('vocals')

    add(d.get('relations'))
    for med in d.get('media') or []:
        for t in med.get('tracks') or []:
            add((t.get('recording') or {}).get('relations'))
    return sorted(found.items())


def main():
    global SB_URL, SB_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--url', default=os.environ.get('SUPABASE_URL'))
    ap.add_argument('--key', default=os.environ.get('SUPABASE_SERVICE_KEY'))
    a = ap.parse_args()
    if not a.url or not a.key:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    SB_URL, SB_KEY = a.url.strip(), a.key.strip()

    albums = get_albums(a.limit)
    print(f'{len(albums)} album da arricchire\n')
    ok = nf = 0
    for i, al in enumerate(albums, 1):
        print(f'[{i}/{len(albums)}] {al["catalog"]}')
        mbid, rg = find_release(al['catalog'])
        if not mbid:
            nf += 1
            print('    ✗ release non trovata')
            continue
        patch = {'mbid': mbid, 'rg_mbid': rg}
        if not al.get('musician_list'):
            cr = credits(mbid)
            if cr:
                patch['musician_list'] = [n for n, _ in cr]
                patch['musicians'] = ', '.join(f'{n} ({", ".join(sorted(s))})' if s else n for n, s in cr)
                print(f'    ✓ {len(cr)} musicisti')
            else:
                print('    · nessun credito su MusicBrainz')
        sb('PATCH', f'/rest/v1/albums?id=eq.{urllib.parse.quote(al["id"])}', patch, {'Prefer': 'return=minimal'})
        ok += 1
    print(f'\nAggiornati: {ok}  Non trovati: {nf}')


if __name__ == '__main__':
    main()
