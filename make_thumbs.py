"""
Crea le miniature delle copertine per la griglia del sito.

Per ogni copertina in Storage (bucket "covers") salva una versione ridotta in
covers/thumbs/<stesso nome>, lato massimo 300 px, JPEG ~qualita 72 (circa 10-15 KB
invece di 50-100 KB). Il sito usa la miniatura nella griglia e, se manca, la copertina
originale. Le miniature gia presenti vengono saltate (usa --force per rifarle).

Uso:  python make_thumbs.py [--force] [--limit N]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import argparse, io, os, sys, time, urllib.parse, urllib.request, urllib.error, json
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

URL = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
KEY = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
PUB = '/storage/v1/object/public/covers/'
SIZE = 300
QUALITY = 72


def http(method, url, data=None, headers=None, timeout=60):
    rq = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return r.status, r.read()


def albums():
    out, off = [], 0
    h = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
    while True:
        _, b = http('GET', f'{URL}/rest/v1/albums?select=id,cover_url&cover_url=not.is.null&order=id&limit=1000&offset={off}', headers=h)
        rows = json.loads(b)
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


def exists(name):
    try:
        st, _ = http('HEAD', URL + PUB + 'thumbs/' + name, timeout=30)
        return st == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def make(name, force):
    if not force and exists(name):
        return 'skip'
    for attempt in range(3):
        try:
            _, raw = http('GET', URL + PUB + name)
            im = Image.open(io.BytesIO(raw))
            im = im.convert('RGB')
            im.thumbnail((SIZE, SIZE), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=QUALITY, optimize=True, progressive=True)
            data = buf.getvalue()
            http('POST', f'{URL}/storage/v1/object/covers/thumbs/{name}', data=data, headers={
                'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'image/jpeg',
                'x-upsert': 'true', 'cache-control': 'max-age=2592000'})
            return f'ok {len(raw)//1024}KB -> {len(data)//1024}KB'
        except Exception as e:
            err = e
            time.sleep(2 * (attempt + 1))
    return f'ERR {err}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    if not URL or not KEY:
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')
    names = []
    for r in albums():
        u = r['cover_url'] or ''
        i = u.find(PUB)
        if i < 0:
            continue
        n = u[i + len(PUB):].split('?')[0]
        if n and not n.startswith('thumbs/'):
            names.append(n)
    names = sorted(set(names))
    if a.limit:
        names = names[:a.limit]
    print(f'{len(names)} copertine', flush=True)
    stats = {'ok': 0, 'skip': 0, 'ERR': 0}
    with ThreadPoolExecutor(8) as ex:
        for i, (n, res) in enumerate(zip(names, ex.map(lambda n: make(n, a.force), names)), 1):
            k = res.split()[0]
            stats[k] = stats.get(k, 0) + 1
            if k != 'skip':
                print(f'{i:5d} {urllib.parse.unquote(n)}: {res}', flush=True)
    print(f'\nRIEPILOGO: {stats}', flush=True)


if __name__ == '__main__':
    main()
