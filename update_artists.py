#!/usr/bin/env python3
"""
update_artists.py
-----------------
Per gli artisti piu' presenti nel catalogo cerca la pagina Wikipedia e salva nella
tabella `artists` di Supabase:
  - la biografia (introduzione di Wikipedia) in inglese, italiano, francese,
    tedesco e spagnolo, con il link alla pagina;
  - una foto, SOLO se ha una licenza libera su Wikimedia Commons (CC BY, CC BY-SA,
    CC0, pubblico dominio), con autore, licenza e pagina del file per l'attribuzione.

Controlli: la pagina deve esistere, non essere di disambiguazione e parlare di musica.
Per default e' una simulazione; con --apply scrive su Supabase.

Uso:  python update_artists.py [--top 150] [--min-albums 3] [--names "A;B"] [--apply]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import argparse, html, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timezone

UA = 'NordicgrundCatalog/1.0 (+https://nordicgrund.com)'
LANGS = ['en', 'it', 'fr', 'de', 'es']
MUSIC = re.compile(r'jazz|musician|pianist|saxophon|guitarist|bassist|double bass|drummer|percussion|trumpet|'
                   r'trombon|clarinet|flautist|flutist|composer|singer|vocalist|soprano|tenor|baritone|conductor|'
                   r'violin|viola|cellist|cello|organist|harpsichord|accordion|oud|band|quartet|trio|ensemble|'
                   r'orchestra|choir|vocal group|bandleader|improvis|music|pianista|compositor|compositeur|'
                   r'komponist|musicien|musiker|músico|musicista|chitarrista|sassofon', re.I)
FREE_LIC = re.compile(r'^(CC[ -]BY|CC0|CC[ -]BY[ -]SA|Public domain|PD|Attribution)', re.I)
SUFFIXES = ['', ' (musician)', ' (jazz musician)', ' (pianist)', ' (saxophonist)', ' (guitarist)', ' (bassist)',
            ' (drummer)', ' (trumpeter)', ' (composer)', ' (singer)', ' (violinist)', ' (cellist)',
            ' (percussionist)', ' (trombonist)', ' (vocalist)', ' (conductor)', ' (band)', ' (ensemble)']
GENERIC = re.compile(r'^(various|unissued|anonymous|traditional|unknown|n/a)\b|\d', re.I)
COMPOSITE = re.compile(r',|/|&|\bw/|\bfeat\.?\b|\band\b', re.I)


def get(url, headers=None, retries=3):
    h = {'User-Agent': UA}
    h.update(headers or {})
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < retries:
                time.sleep(5 * (i + 1)); continue
            return None
        except Exception:
            if i < retries:
                time.sleep(3); continue
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


def wiki(lang, **params):
    q = urllib.parse.urlencode(dict(action='query', format='json', redirects=1, **params))
    time.sleep(0.25)
    return get(f'https://{lang}.wikipedia.org/w/api.php?{q}')


def top_artists(top, min_albums):
    names, off = Counter(), 0
    while True:
        rows = sb('GET', '/rest/v1/albums?select=artist_list,musician_list&order=catalog&limit=1000&offset=%d' % off)
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            for n in set((r.get('artist_list') or []) + (r.get('musician_list') or [])):
                n = (n or '').strip()
                if n and not GENERIC.search(n) and not COMPOSITE.search(n) and len(n) > 2:
                    names[n] += 1
        if len(rows) < 1000:
            break
        off += 1000
    return [(n, c) for n, c in names.most_common(top) if c >= min_albums]


def find_en_page(name):
    titles = [name + s for s in SUFFIXES]
    for i in range(0, len(titles), 50):
        d = wiki('en', prop='pageprops|extracts|langlinks', ppprop='wikibase_item|disambiguation',
                 exintro=1, explaintext=1, lllimit=50, titles='|'.join(titles[i:i + 50]))
        pages = ((d or {}).get('query') or {}).get('pages') or {}
        norm = {x['to']: x['from'] for x in ((d or {}).get('query') or {}).get('normalized') or []}
        redir = {x['to']: x['from'] for x in ((d or {}).get('query') or {}).get('redirects') or []}
        found = []
        for p in pages.values():
            if 'missing' in p or 'disambiguation' in (p.get('pageprops') or {}):
                continue
            ex = p.get('extract') or ''
            if 'may refer to' in ex[:200] or not MUSIC.search(ex[:600]):
                continue
            asked = redir.get(p['title'], p['title'])
            asked = norm.get(asked, asked)
            order = titles.index(asked) if asked in titles else 99
            found.append((order, p))
        if found:
            return sorted(found, key=lambda x: x[0])[0][1]
    return None


def bio_in(lang, title):
    d = wiki(lang, prop='extracts', exintro=1, explaintext=1, titles=title)
    pages = ((d or {}).get('query') or {}).get('pages') or {}
    for p in pages.values():
        ex = (p.get('extract') or '').strip()
        if ex and 'missing' not in p:
            return {'text': ex, 'url': f'https://{lang}.wikipedia.org/wiki/' + urllib.parse.quote(p['title'].replace(' ', '_'))}
    return None


def photo_for(qid):
    time.sleep(0.25)
    e = get('https://www.wikidata.org/w/api.php?' + urllib.parse.urlencode(
        dict(action='wbgetclaims', format='json', entity=qid, property='P18')))
    claims = ((e or {}).get('claims') or {}).get('P18') or []
    if not claims:
        return None
    fname = (claims[0].get('mainsnak') or {}).get('datavalue', {}).get('value')
    if not fname:
        return None
    time.sleep(0.25)
    c = get('https://commons.wikimedia.org/w/api.php?' + urllib.parse.urlencode(
        dict(action='query', format='json', prop='imageinfo', iiprop='url|extmetadata', iiurlwidth=500, titles='File:' + fname)))
    pages = ((c or {}).get('query') or {}).get('pages') or {}
    for p in pages.values():
        ii = (p.get('imageinfo') or [None])[0]
        if not ii:
            continue
        md = ii.get('extmetadata') or {}
        lic = (md.get('LicenseShortName') or {}).get('value', '')
        if not FREE_LIC.search(lic):
            return {'rejected': lic or 'licenza non indicata', 'file': fname}
        author = re.sub(r'<[^>]+>', '', (md.get('Artist') or {}).get('value', '')).strip()
        return {'url': ii.get('thumburl') or ii.get('url'), 'credit': html.unescape(author)[:200] or 'Wikimedia Commons',
                'license': lic, 'page': ii.get('descriptionurl'), 'file': fname}
    return None


def main():
    global URL, KEY
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=150)
    ap.add_argument('--min-albums', type=int, default=3)
    ap.add_argument('--names', default='')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
    KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
    if not URL or not KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    if a.names.strip():
        todo = [(n.strip(), 0) for n in a.names.split(';') if n.strip()]
    else:
        todo = top_artists(a.top, a.min_albums)
    print(f'{len(todo)} artisti da cercare. Modalita: {"APPLICA" if a.apply else "SIMULAZIONE"}\n')
    stats = Counter()
    for i, (name, cnt) in enumerate(todo, 1):
        p = find_en_page(name)
        if not p:
            print(f'[{i}] NO PAGINA  {name} ({cnt} album)'); stats['nopage'] += 1; continue
        qid = (p.get('pageprops') or {}).get('wikibase_item')
        bios = {'en': {'text': p['extract'].strip(),
                       'url': 'https://en.wikipedia.org/wiki/' + urllib.parse.quote(p['title'].replace(' ', '_'))}}
        for ll in p.get('langlinks') or []:
            if ll['lang'] in LANGS and ll['lang'] != 'en':
                b = bio_in(ll['lang'], ll['*'])
                if b:
                    bios[ll['lang']] = b
        ph = photo_for(qid) if qid else None
        photo = ph if ph and 'url' in ph else None
        note = ('foto ' + ph['license']) if photo else ('foto scartata: ' + ph['rejected']) if ph else 'senza foto'
        print(f'[{i}] OK         {name} -> {p["title"]} | lingue: {",".join(sorted(bios))} | {note}')
        stats['ok'] += 1
        stats['photo'] += 1 if photo else 0
        if a.apply:
            row = {'name': name, 'wikidata': qid, 'bio': bios,
                   'photo_url': photo and photo['url'], 'photo_credit': photo and photo['credit'],
                   'photo_license': photo and photo['license'], 'photo_page': photo and photo['page'],
                   'updated_at': datetime.now(timezone.utc).isoformat()}
            r = sb('POST', '/rest/v1/artists?on_conflict=name', row,
                   {'Prefer': 'resolution=merge-duplicates,return=minimal'})
            if r is None:
                sys.exit('ERRORE: scrittura in Supabase fallita (la tabella `artists` esiste?)')
    print('\nRIEPILOGO:', dict(stats))


if __name__ == '__main__':
    main()
