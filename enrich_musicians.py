#!/usr/bin/env python3
"""
enrich_musicians.py  (v3: MusicBrainz + Discogs)
------------------------------------------------
Per ogni album senza musicisti (e non ancora controllato):
  1. trova la release su MusicBrainz (numero di catalogo, poi titolo+artista)
  2. legge i crediti della release su MusicBrainz
  3. se mancano, prova fino a 3 altre edizioni dello stesso release-group
  4. se mancano ancora e c'è DISCOGS_TOKEN, cerca su Discogs
Salva: mbid, rg_mbid, musicians, musician_list, credits_source, credits_checked_at.
Gli album con musicisti già presenti non vengono sovrascritti.

Uso:  python enrich_musicians.py [--limit 50] [--recheck]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCOGS_TOKEN (opzionale)
Richiede le colonne credits_source (text) e credits_checked_at (timestamptz).
"""
import argparse, difflib, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

MB = 'https://musicbrainz.org/ws/2'
DG = 'https://api.discogs.com'
UA = 'ECMCatalogApp/3.0 (ale.michetti@gmail.com)'
WAITS = [5, 15, 30, 60]
PERF = {'instrument', 'vocal', 'performer', 'conductor', 'performing orchestra', 'orchestra', 'chorus master'}
# Ruoli Discogs che NON sono esecutori
NOT_PERF = re.compile(
    r'producer|engineer|master|design|photo|liner|cover|artwork|record(ed|ing)|mix(ed|ing)|executive|'
    r'technician|coordinat|manage|edit|written|compos|arrang|lyric|translat|illustrat|direction|'
    r'concept|text|supervis|assist|art direction|sleeve|typograph|painting|drawing|layout|graphic|tape|curat|restor|a&r|other', re.I)


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
            if e.code != 404:
                print(f'    [HTTP {e.code}] {url[:90]}')
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'    [err] {e}')
            return None


def mb(path, params):
    time.sleep(1.2)
    return http_json(f'{MB}{path}?{urllib.parse.urlencode(dict(params, fmt="json"))}', {'User-Agent': UA})


def dg(path, params=None):
    time.sleep(1.1)
    q = dict(params or {}, token=DISCOGS_TOKEN)
    return http_json(f'{DG}{path}?{urllib.parse.urlencode(q)}', {'User-Agent': UA})


def sb(method, path, body=None, extra=None):
    h = {'apikey': SB_KEY, 'Authorization': f'Bearer {SB_KEY}', 'Content-Type': 'application/json'}
    h.update(extra or {})
    return http_json(SB_URL.rstrip('/') + path, h, method, body)


def get_albums(limit, recheck):
    # nuovi album + album compilati dalla vecchia versione (non validata) di Discogs
    flt = '' if recheck else '&or=(credits_checked_at.is.null,credits_source.eq.discogs)'
    out, off = [], 0
    while True:
        b = sb('GET', '/rest/v1/albums?select=id,catalog,artist,title,musician_list,mbid,rg_mbid,credits_source'
                      f'{flt}&order=catalog&limit=1000&offset={off}')
        if b is None:
            sys.exit('ERRORE lettura album: hai creato le colonne credits_source / credits_checked_at?')
        if not isinstance(b, list) or not b:
            break
        out += [x for x in b if not x.get('musician_list') or x.get('credits_source') == 'discogs']
        if len(b) < 1000:
            break
        off += 1000
    return out[:limit] if limit else out


# ── MusicBrainz ──────────────────────────────────────────────────────────────
def pick(rels):
    rels.sort(key=lambda r: (r.get('status') != 'Official', r.get('date') or '9999'))
    r = rels[0]
    return r['id'], (r.get('release-group') or {}).get('id')


def esc(s):
    return re.sub(r'([\\"])', r'\\\1', s or '')


def find_release(catalog, artist='', title=''):
    m = re.search(r'((?:ECM|JAPO)\s*[\dA-Z\-/ ]+)', catalog, re.I)
    catno = (m.group(1) if m else catalog).strip()
    d = mb('/release', {'query': f'catno:"{catno}"', 'limit': 5})
    rels = (d or {}).get('releases') or []
    if rels:
        return pick(rels)
    if not title:
        return None, None
    clean = re.sub(r'\s*[\[\(].*?[\]\)]\s*', ' ', title).strip() or title
    queries = []
    if artist:
        queries.append(f'release:"{esc(clean)}" AND artist:"{esc(artist)}" AND label:"ECM"')
        queries.append(f'release:"{esc(clean)}" AND artist:"{esc(artist)}"')
    queries.append(f'release:"{esc(clean)}" AND label:"ECM"')
    for q in queries:
        d = mb('/release', {'query': q, 'limit': 5})
        rels = [r for r in ((d or {}).get('releases') or []) if int(r.get('score', 0)) >= 90]
        if rels:
            return pick(rels)
    return None, None


def mb_credits(mbid):
    d = mb(f'/release/{mbid}', {'inc': 'artist-rels+recording-rels+recording-level-rels'})
    if not d:
        return []
    found = {}

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


def sibling_releases(rg, exclude, n=3):
    if not rg:
        return []
    d = mb('/release', {'release-group': rg, 'limit': 25})
    rels = [r for r in (d or {}).get('releases') or [] if r['id'] != exclude]
    rels.sort(key=lambda r: (r.get('status') != 'Official', r.get('date') or '9999'))
    return [r['id'] for r in rels[:n]]


# ── Discogs ──────────────────────────────────────────────────────────────────
def clean_name(n):
    return re.sub(r'\s*\(\d+\)\s*$', '', n or '').strip()


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def title_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
        return True
    return min(len(a), len(b)) >= 8 and (a in b or b in a)


def discogs_credits(catalog, artist, title):
    if not DISCOGS_TOKEN:
        return []
    m = re.search(r'((?:ECM|JAPO)\s*[\dA-Z\-/ ]+)', catalog, re.I)
    catno = re.sub(r'\s+', ' ', (m.group(1) if m else catalog)).strip()
    base = re.match(r'(ECM|JAPO)\s*(\d+)', catno, re.I)
    tries = [{'catno': catno, 'label': 'ECM Records', 'type': 'release'}]
    if base:
        tries.append({'catno': f'{base.group(1).upper()} {base.group(2)}', 'type': 'release'})
    tries.append({'release_title': title, 'artist': artist, 'label': 'ECM Records', 'type': 'release'})
    for p in tries:
        d = dg('/database/search', dict(p, per_page=5))
        for res in ((d or {}).get('results') or [])[:5]:
            rt = (res.get('title') or '').split(' - ', 1)[-1]
            if not title_match(rt, title):
                continue
            rel = dg(f'/releases/{res["id"]}')
            found = {}
            for ea in (rel or {}).get('extraartists') or []:
                role = ea.get('role') or ''
                name = clean_name(ea.get('anv') or ea.get('name'))
                if not name or not role or NOT_PERF.search(role):
                    continue
                ins = found.setdefault(name, set())
                for r in re.split(r',\s*(?![^\[]*\])', role):
                    r = re.sub(r'\s*\[.*?\]', '', r).strip().lower()
                    if r:
                        ins.add(r)
            if found:
                return sorted(found.items())
    return []


def fmt(cr):
    return ', '.join(f'{n} ({", ".join(sorted(s))})' if s else n for n, s in cr)


def main():
    global SB_URL, SB_KEY, DISCOGS_TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--recheck', action='store_true', help='ricontrolla anche album già controllati')
    ap.add_argument('--url', default=os.environ.get('SUPABASE_URL'))
    ap.add_argument('--key', default=os.environ.get('SUPABASE_SERVICE_KEY'))
    a = ap.parse_args()
    if not a.url or not a.key:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    SB_URL, SB_KEY = a.url.strip(), a.key.strip()
    DISCOGS_TOKEN = (os.environ.get('DISCOGS_TOKEN') or '').strip()
    print('Discogs:', 'attivo' if DISCOGS_TOKEN else 'NON attivo (manca DISCOGS_TOKEN)')

    albums = get_albums(a.limit, a.recheck)
    print(f'{len(albums)} album da controllare\n')
    stats = {'musicbrainz': 0, 'musicbrainz-altra-edizione': 0, 'discogs-v2': 0, 'nessuna': 0}
    for i, al in enumerate(albums, 1):
        print(f'[{i}/{len(albums)}] {al["catalog"]} — {al.get("artist","")}')
        mbid, rg = al.get('mbid'), al.get('rg_mbid')
        if not mbid:
            mbid, rg = find_release(al['catalog'], al.get('artist', ''), al.get('title', ''))
        patch, cr, src = {}, [], None
        if mbid and al.get('credits_source') != 'discogs':
            patch.update({'mbid': mbid, 'rg_mbid': rg})
            cr = mb_credits(mbid)
            src = 'musicbrainz' if cr else None
            if not cr:
                for other in sibling_releases(rg, mbid):
                    cr = mb_credits(other)
                    if cr:
                        src = 'musicbrainz-altra-edizione'
                        break
        if not cr:
            cr = discogs_credits(al['catalog'], al.get('artist', ''), al.get('title', ''))
            src = 'discogs-v2' if cr else None
        if cr:
            patch['musician_list'] = [n for n, _ in cr]
            patch['musicians'] = fmt(cr)
            print(f'    ✓ {len(cr)} musicisti ({src})')
            stats[src] += 1
        else:
            print('    · nessun credito trovato')
            stats['nessuna'] += 1
            if al.get('credits_source') == 'discogs':  # svuota i dati vecchi non validati
                patch['musician_list'] = []
                patch['musicians'] = ''
        patch['credits_source'] = src
        patch['credits_checked_at'] = datetime.now(timezone.utc).isoformat()
        sb('PATCH', f'/rest/v1/albums?id=eq.{urllib.parse.quote(al["id"])}', patch, {'Prefer': 'return=minimal'})
    print('\nRiepilogo:', stats)


if __name__ == '__main__':
    main()
