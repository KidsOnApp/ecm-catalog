#!/usr/bin/env python3
"""
download_covers.py
------------------
Scarica le copertine di tutti gli album ECM da MusicBrainz / iTunes
e le carica nel bucket "covers" di Supabase Storage.
Aggiorna il DB: cover_url + cover_ok per ogni album.

Uso:
  python download_covers.py --url https://xxx.supabase.co --key SERVICE_ROLE_KEY [--limit 50]

GitHub Actions: legge SUPABASE_URL e SUPABASE_SERVICE_KEY dall'ambiente.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ── Configurazione ──────────────────────────────────────────────────────────
MB_BASE    = 'https://musicbrainz.org/ws/2'
MB_AGENT   = 'ECMCatalogApp/2.0 (ale.michetti@gmail.com)'
MB_RATE    = 1.2   # secondi tra richieste MusicBrainz
MAX_RETRY  = 3
RETRY_WAIT = [5, 15, 30]


# ── HTTP helpers ────────────────────────────────────────────────────────────
def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < MAX_RETRY - 1:
                w = RETRY_WAIT[attempt]
                print(f'    [retry {attempt+1}] HTTP {e.code} — attendo {w}s…')
                time.sleep(w)
                continue
            return None, None
        except Exception as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(RETRY_WAIT[attempt])
                continue
            return None, None
    return None, None


def mb_get(path, params=None):
    url = MB_BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    time.sleep(MB_RATE)
    data, _ = http_get(url, headers={'User-Agent': MB_AGENT})
    if data:
        try:
            return json.loads(data)
        except Exception:
            return None
    return None


# ── Supabase REST ───────────────────────────────────────────────────────────
def sb_request(method, path, sb_url, sb_key, body=None, extra_headers=None):
    url = sb_url.rstrip('/') + path
    headers = {
        'apikey':        sb_key,
        'Authorization': f'Bearer {sb_key}',
    }
    if extra_headers:
        headers.update(extra_headers)
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body else None)
    if data and 'Content-Type' not in headers:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else True
    except urllib.error.HTTPError as e:
        msg = e.read().decode('utf-8', errors='replace')
        print(f'    [SB error {e.code}] {msg[:200]}')
        return None


def sb_get_albums(sb_url, sb_key, only_missing=True):
    """Legge album dal DB. Se only_missing=True, solo quelli senza copertina."""
    filter_q = '&cover_ok=eq.false' if only_missing else ''
    path = f'/rest/v1/albums?select=id,catalog,artist,title,mbid,rg_mbid{filter_q}&order=catalog&limit=2000'
    result = sb_request('GET', path, sb_url, sb_key, extra_headers={'Prefer': 'return=representation'})
    return result if isinstance(result, list) else []


def sb_update_album(sb_url, sb_key, album_id, cover_url, cover_ok):
    path = f'/rest/v1/albums?id=eq.{urllib.parse.quote(album_id)}'
    body = {'cover_url': cover_url, 'cover_ok': cover_ok}
    sb_request('PATCH', path, sb_url, sb_key, body=body,
               extra_headers={'Prefer': 'return=minimal'})


def sb_upload_cover(sb_url, sb_key, filename, image_bytes, content_type='image/jpeg'):
    """Carica un'immagine nel bucket 'covers' di Supabase Storage."""
    path = f'/storage/v1/object/covers/{filename}'
    headers = {'Content-Type': content_type, 'x-upsert': 'true'}
    result = sb_request('POST', path, sb_url, sb_key, body=image_bytes, extra_headers=headers)
    if result is not None:
        public_url = f'{sb_url.rstrip("/")}/storage/v1/object/public/covers/{filename}'
        return public_url
    return None


# ── Ricerca copertine ───────────────────────────────────────────────────────
def search_mbid(catalog):
    """Cerca MBID su MusicBrainz tramite numero catalogo."""
    m = re.search(r'((?:ECM|JAPO)\s*[\dA-Z\-]+)', catalog, re.IGNORECASE)
    if not m:
        return None, None
    catno = m.group(1).strip()
    data = mb_get('/release', {
        'query': f'catno:"{catno}"',
        'limit': 5,
        'fmt':   'json',
        'inc':   'release-groups',
    })
    if not data or not data.get('releases'):
        return None, None
    best = data['releases'][0]
    mbid    = best.get('id')
    rg      = best.get('release-group') or {}
    rg_mbid = rg.get('id')
    return mbid, rg_mbid


def fetch_caa_cover(mbid, kind='release'):
    """Scarica copertina da Cover Art Archive (release o release-group)."""
    if not mbid:
        return None, None
    base = 'https://coverartarchive.org'
    url  = f'{base}/{kind}/{mbid}/front-500'
    data, ct = http_get(url, timeout=15)
    if data and ct and 'image' in ct:
        return data, ct
    return None, None


def fetch_itunes_cover(artist, title):
    """Scarica copertina da iTunes Search API come fallback."""
    q    = urllib.parse.quote(f'{artist} {title}'.strip())
    url  = f'https://itunes.apple.com/search?term={q}&entity=album&media=music&limit=10'
    data, _ = http_get(url, timeout=15)
    if not data:
        return None, None
    try:
        results = json.loads(data).get('results', [])
    except Exception:
        return None, None
    for r in results:
        art = r.get('artworkUrl100', '')
        if not art:
            continue
        # Converti a 600x600
        img_url  = art.replace('100x100bb', '600x600bb')
        img_data, ct = http_get(img_url, timeout=15)
        if img_data and ct and 'image' in ct:
            return img_data, ct
    return None, None


def safe_filename(catalog):
    """Converte il numero catalogo in un filename sicuro."""
    return re.sub(r'[^\w\-]', '_', catalog.strip()) + '.jpg'


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default=os.environ.get('SUPABASE_URL'),
                        help='Supabase Project URL')
    parser.add_argument('--key', default=os.environ.get('SUPABASE_SERVICE_KEY'),
                        help='Supabase Service Role Key')
    parser.add_argument('--limit', type=int, default=0,
                        help='Max album da processare (0 = tutti)')
    parser.add_argument('--all', action='store_true',
                        help='Riprocessa anche album che hanno già una copertina')
    args = parser.parse_args()

    if not args.url or not args.key:
        print('ERRORE: --url e --key sono obbligatori (o variabili SUPABASE_URL / SUPABASE_SERVICE_KEY)')
        sys.exit(1)

    # Debug: verifica URL (stampa prefisso/suffisso senza esporre il key)
    url_repr = repr(args.url)
    print(f'DEBUG SUPABASE_URL: lunghezza={len(args.url)}, valore={args.url[:12]}...{args.url[-8:]}')
    print(f'DEBUG repr: {url_repr[:60]}')
    if not args.url.startswith('https://'):
        print('ATTENZIONE: SUPABASE_URL non inizia con https:// !')

    only_missing = not args.all
    print(f'Leggo album dal database{"" if only_missing else " (tutti)"}…')
    albums = sb_get_albums(args.url, args.key, only_missing=only_missing)
    if args.limit:
        albums = albums[:args.limit]
    print(f'  {len(albums)} album da processare.\n')

    ok = 0
    missing = 0

    for i, album in enumerate(albums, 1):
        aid     = album['id']
        catalog = album.get('catalog', aid)
        artist  = album.get('artist', '')
        title   = album.get('title', '')
        mbid    = album.get('mbid')
        rg_mbid = album.get('rg_mbid')

        print(f'[{i}/{len(albums)}] {catalog} — {artist} — {title}')

        image_data, content_type = None, 'image/jpeg'

        # 1) Cover Art Archive via Release Group MBID (migliore copertura)
        if rg_mbid:
            image_data, ct = fetch_caa_cover(rg_mbid, kind='release-group')
            if image_data:
                content_type = ct
                print(f'    ✓ CAA release-group')

        # 2) Cover Art Archive via Release MBID
        if not image_data and mbid:
            image_data, ct = fetch_caa_cover(mbid, kind='release')
            if image_data:
                content_type = ct
                print(f'    ✓ CAA release')

        # 3) Cerca MBID su MusicBrainz se non ce l'abbiamo
        if not image_data and not mbid and not rg_mbid:
            mbid_new, rg_mbid_new = search_mbid(catalog)
            if mbid_new or rg_mbid_new:
                if rg_mbid_new:
                    image_data, ct = fetch_caa_cover(rg_mbid_new, kind='release-group')
                    if image_data:
                        content_type = ct
                        print(f'    ✓ CAA release-group (MBID cercato)')
                if not image_data and mbid_new:
                    image_data, ct = fetch_caa_cover(mbid_new, kind='release')
                    if image_data:
                        content_type = ct
                        print(f'    ✓ CAA release (MBID cercato)')

        # 4) iTunes come ultimo fallback
        if not image_data:
            image_data, ct = fetch_itunes_cover(artist, title)
            if image_data:
                content_type = ct
                print(f'    ✓ iTunes')

        # Carica su Supabase Storage e aggiorna DB
        if image_data:
            filename  = safe_filename(catalog)
            cover_url = sb_upload_cover(args.url, args.key, filename, image_data, content_type)
            if cover_url:
                sb_update_album(args.url, args.key, aid, cover_url, True)
                ok += 1
                print(f'    → Caricata: {filename}')
            else:
                sb_update_album(args.url, args.key, aid, None, False)
                missing += 1
                print(f'    ✗ Errore upload Supabase')
        else:
            sb_update_album(args.url, args.key, aid, None, False)
            missing += 1
            print(f'    ✗ Nessuna copertina trovata')

    print(f'\n── Completato ────────────────────────────')
    print(f'  Copertine caricate:  {ok}')
    print(f'  Mancanti:            {missing}')
    print(f'  Totale processati:   {ok + missing}')


if __name__ == '__main__':
    main()
