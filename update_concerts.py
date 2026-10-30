#!/usr/bin/env python3
"""
update_concerts.py
------------------
Legge la pagina concerti di ECM Records (https://ecmrecords.com/concerts/)
e aggiorna la tabella `concerts` di Supabase.

- Ogni concerto ha un id stabile (fonte + hash di data, artista, sala, città),
  quindi le esecuzioni ripetute aggiornano le righe invece di duplicarle.
- I concerti passati e quelli spariti dalla pagina ECM vengono rimossi.
- Se la pagina non restituisce nessun concerto (struttura cambiata, errore di
  rete) lo script si ferma con errore e non cancella nulla.

L'abbinamento concerto -> musicisti del catalogo lo fa il sito (index.html),
con gli stessi nomi che usa per musicisti e album collegati.

Uso:  python update_concerts.py [--dry-run] [--file pagina.html]
Env:  SUPABASE_URL, SUPABASE_SERVICE_KEY (non servono con --dry-run)
"""
import hashlib, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser

SOURCE = 'ecm'
PAGE_URL = 'https://ecmrecords.com/concerts/'
UA = 'ECMCatalogConcerts/1.0 (ale.michetti@gmail.com)'
WAITS = [5, 15, 30, 60]

MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july',
     'august', 'september', 'october', 'november', 'december'], start=1)}


# ════════════════════════════════════════════════════════════
#  PARSER DELLA PAGINA
# ════════════════════════════════════════════════════════════
class ConcertTableParser(HTMLParser):
    """
    Struttura della tabella (ottobre 2026):
      <tr class="header ..."><td><span class="year">2026</span><span class="month">OCTOBER</span></td></tr>
      <tr class="concert october D dk">
        <td class="date"><span>October 30</span></td>
        <td class="artist"> <span><span>Nome</span></span>  oppure
                            <a class="c_info"><span>Nome</span></a><p class="info">w/ ...</p> </td>
        <td class="venue">Sala</td>
        <td class="city-country">Città, Paese</td>
        <td class="ticket-shop"><a href="...">Event</a></td>
      </tr>
    L'anno compare solo nell'intestazione del primo mese di ogni anno.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []          # concerti grezzi
        self.headers = []       # (anno o None, mese) nell'ordine della pagina
        self._tr = None         # 'header' | 'concert' | None
        self._tr_classes = []
        self._td = None         # classe della cella corrente
        self._span = None       # 'year' | 'month' dentro un header
        self._in_info = False
        self._cur = None
        self._hdr = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get('class') or '').split()
        if tag == 'tr':
            if 'header' in cls:
                self._tr, self._hdr = 'header', {'year': None, 'month': ''}
            elif 'concert' in cls:
                self._tr = 'concert'
                self._tr_classes = cls
                self._cur = {'date_text': '', 'artist': '', 'info': '', 'venue': '',
                             'city_country': '', 'url': None,
                             'country_code': next((c for c in cls if len(c) == 2 and c.islower()), None)}
            else:
                self._tr = None
        elif tag == 'td' and self._tr == 'concert':
            self._td = next((c for c in cls if c in ('date', 'artist', 'venue', 'city-country', 'ticket-shop')), None)
        elif tag == 'span' and self._tr == 'header':
            self._span = 'year' if 'year' in cls else 'month' if 'month' in cls else None
        elif tag == 'p' and self._td == 'artist' and 'info' in cls:
            self._in_info = True
        elif tag == 'a' and self._td == 'ticket-shop' and a.get('href'):
            self._cur['url'] = a['href'].strip()
        elif tag == 'br' and self._in_info:
            self._cur['info'] += '\n'

    def handle_endtag(self, tag):
        if tag == 'tr':
            if self._tr == 'header' and self._hdr:
                self.headers.append((self._hdr['year'], self._hdr['month']))
                self.rows.append({'_header': self._hdr})
            elif self._tr == 'concert' and self._cur:
                self.rows.append(self._cur)
            self._tr, self._cur, self._hdr = None, None, None
        elif tag == 'td':
            self._td = None
        elif tag == 'span':
            self._span = None
        elif tag == 'p':
            self._in_info = False

    def handle_data(self, data):
        if self._tr == 'header' and self._span and self._hdr is not None:
            t = data.strip()
            if self._span == 'year' and t.isdigit():
                self._hdr['year'] = int(t)
            elif self._span == 'month' and t:
                self._hdr['month'] = t.lower()
        elif self._tr == 'concert' and self._td and self._cur is not None:
            key = {'date': 'date_text', 'venue': 'venue', 'city-country': 'city_country'}.get(self._td)
            if self._td == 'artist':
                key = 'info' if self._in_info else 'artist'
            if key:
                self._cur[key] += data


def clean(s):
    s = (s or '').replace('\xa0', ' ')
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in s.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def parse_concerts(html, today=None):
    """Restituisce la lista dei concerti dalla pagina, con data completa."""
    today = today or date.today()
    p = ConcertTableParser()
    p.feed(html)
    year, last_month, out = None, None, []
    for r in p.rows:
        if '_header' in r:
            h = r['_header']
            if h['year']:
                year = h['year']
                last_month = None
            continue
        m = re.match(r'\s*([A-Za-z]+)\s+(\d{1,2})', r['date_text'])
        if not m or m.group(1).lower() not in MONTHS:
            print(f'  [salto] data non riconosciuta: {r["date_text"]!r} ({r["artist"].strip()[:40]})')
            continue
        month, day = MONTHS[m.group(1).lower()], int(m.group(2))
        if year is None:                       # nessuna intestazione con l'anno prima della riga
            year = today.year - 1 if month - today.month > 6 else today.year
        elif last_month and last_month - month > 6:
            # mese che torna indietro senza intestazione dell'anno (dicembre -> gennaio)
            year += 1
        last_month = month
        try:
            d = date(year, month, day)
        except ValueError:
            print(f'  [salto] data impossibile: {year}-{month}-{day}')
            continue
        artist = clean(r['artist']).replace('\n', ' ')
        if not artist:
            continue
        city, country = '', ''
        cc = clean(r['city_country']).replace('\n', ' ')
        if ',' in cc:
            city, country = [x.strip() for x in cc.rsplit(',', 1)]
        else:
            city = cc
        venue = clean(r['venue']).replace('\n', ' ')
        key = f'{d.isoformat()}|{artist.lower()}|{venue.lower()}|{city.lower()}'
        out.append({
            'id': f'{SOURCE}-' + hashlib.sha1(key.encode()).hexdigest()[:16],
            'source': SOURCE,
            'date': d.isoformat(),
            'artist': artist,
            'info': clean(r['info']) or None,
            'venue': venue or None,
            'city': city or None,
            'country': country or None,
            'country_code': r['country_code'],
            'url': r['url'],
        })
    # stesso concerto ripetuto nella pagina: tiene il primo
    seen, uniq = set(), []
    for c in out:
        if c['id'] not in seen:
            seen.add(c['id'])
            uniq.append(c)
    return uniq


# ════════════════════════════════════════════════════════════
#  RETE E SUPABASE
# ════════════════════════════════════════════════════════════
def http(url, headers, method='GET', body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    for i in range(len(WAITS) + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
                if raw:
                    return b.decode('utf-8', 'replace')
                return json.loads(b) if b else True
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < len(WAITS):
                print(f'  [retry] HTTP {e.code}, attendo {WAITS[i]}s')
                time.sleep(WAITS[i]); continue
            print(f'  [errore] HTTP {e.code} {url[:90]} {e.read()[:200]!r}')
            return None
        except Exception as e:
            if i < len(WAITS):
                time.sleep(WAITS[i]); continue
            print(f'  [errore] {e}')
            return None


def sb(method, path, body=None, extra=None):
    url = (os.environ.get('SUPABASE_URL') or '').strip().rstrip('/')
    key = (os.environ.get('SUPABASE_SERVICE_KEY') or '').strip()
    h = {'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    h.update(extra or {})
    return http(url + path, h, method, body)


def main():
    args = sys.argv[1:]
    dry = '--dry-run' in args
    if '--file' in args:
        with open(args[args.index('--file') + 1], encoding='utf-8') as f:
            html = f.read()
    else:
        print(f'Scarico {PAGE_URL}')
        html = http(PAGE_URL, {'User-Agent': UA, 'Accept': 'text/html'}, raw=True)
        if not html:
            sys.exit('ERRORE: pagina concerti ECM non raggiungibile')

    today = date.today()
    concerts = [c for c in parse_concerts(html, today) if c['date'] >= today.isoformat()]
    print(f'Concerti futuri letti: {len(concerts)}')
    if not concerts:
        sys.exit('ERRORE: nessun concerto letto. La struttura della pagina potrebbe essere cambiata; nulla è stato modificato.')

    artists = sorted({c['artist'] for c in concerts})
    print(f'Artisti/formazioni: {len(artists)}  |  Paesi: {len({c["country"] for c in concerts})}  |  '
          f'Dal {concerts[0]["date"]} al {max(c["date"] for c in concerts)}')

    if dry:
        for c in concerts[:15]:
            print(f'  {c["date"]}  {c["artist"][:45]:45}  {c["city"] or ""}, {c["country"] or ""}')
        print('[dry-run] nulla scritto.')
        return

    if not os.environ.get('SUPABASE_URL') or not os.environ.get('SUPABASE_SERVICE_KEY'):
        sys.exit('ERRORE: servono SUPABASE_URL e SUPABASE_SERVICE_KEY')

    run_start = datetime.now(timezone.utc).isoformat()
    for c in concerts:
        c['seen_at'] = run_start
    for i in range(0, len(concerts), 200):
        r = sb('POST', '/rest/v1/concerts?on_conflict=id', concerts[i:i + 200],
               {'Prefer': 'resolution=merge-duplicates,return=minimal'})
        if r is None:
            sys.exit('ERRORE: scrittura dei concerti in Supabase fallita (la tabella `concerts` esiste?)')
    print(f'Aggiornati {len(concerts)} concerti.')

    # pulizia: concerti passati e concerti ECM non più presenti nella pagina
    r1 = sb('DELETE', f'/rest/v1/concerts?source=eq.{SOURCE}&date=lt.{today.isoformat()}',
            extra={'Prefer': 'return=representation'})
    r2 = sb('DELETE', f'/rest/v1/concerts?source=eq.{SOURCE}&seen_at=lt.{urllib.parse.quote(run_start)}',
            extra={'Prefer': 'return=representation'})
    print(f'Rimossi: {len(r1) if isinstance(r1, list) else 0} passati, '
          f'{len(r2) if isinstance(r2, list) else 0} non più in programma.')


if __name__ == '__main__':
    main()
