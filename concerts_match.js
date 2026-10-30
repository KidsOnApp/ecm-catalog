// ═══════════════════════════════════════════════════════════
//  CONCERTI: abbinamento ai musicisti del catalogo
//  Stesso codice incluso in index.html; questo file serve ai test.
// ═══════════════════════════════════════════════════════════

// Nome normalizzato: minuscole, senza accenti né punteggiatura.
function normName(s) {
  return String(s || '').toLowerCase()
    .replace(/ø/g, 'o').replace(/æ/g, 'ae').replace(/œ/g, 'oe').replace(/ß/g, 'ss')
    .replace(/ł/g, 'l').replace(/đ/g, 'd').replace(/ı/g, 'i')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, ' ').trim();
}

// Righe che descrivono il repertorio o il titolo del programma, non chi suona.
const CONCERT_SKIP_LINE = /^\s*(["“”«].*["“”»]\s*$|works by|music by|programme?\b|komponist|nachtmusik)/i;
const CONCERT_TRIBUTE   = /^\s*(remembering|tribute to|homage to|hommage|omaggio a|in memoriam)\b/i;
// Separatori tra nomi nello stesso testo ("A & B", "A / B", "Trio w/ C, D", "feat. E", "A- Gruppo").
const CONCERT_SPLIT = /\s*(?:\/|,|&|\+|;|\bw\/|\bwith\b|\bfeat\.?|\bfeaturing\b|\band\b|\bund\b|\s-\s|-\s)\s*/i;
// Parole di formazione da togliere in coda ("Marcin Wasilewski Trio" -> "Marcin Wasilewski").
const CONCERT_LINEUP_SUFFIX = /\s+(duo|trio|quartet|quartett|quintet|quintett|sextet|septet|octet|group|band|ensemble)$/i;
// Nomi del catalogo che uniscono più persone ("Trygve Seim, Frode Haltli").
const COMPOSITE_NAME = /,|&|\/|\bw\/|\bfeat\.?\s/i;

// Chiavi di un nome del catalogo: il nome intero e, se composto, ogni sua parte.
function nameKeys(n) {
  const keys = new Set();
  const k = normName(n);
  if (k) keys.add(k);
  if (COMPOSITE_NAME.test(n)) {
    n.split(/\s*(?:,|&|\/|\bw\/|\bfeat\.?\s)\s*/i).forEach(p => {
      const pk = normName(p.replace(/^[^:]*:\s*/, ''));  // "Veljo Tormis: Choir" -> "Choir"
      if (pk) keys.add(pk);
    });
  }
  return keys;
}

// Indice dei nomi del catalogo: chiave normalizzata -> nome come appare nel sito.
// Usa solo nomi singoli; i nomi composti vengono scomposti in nameKeys().
// people(a) è la funzione del sito che unisce artist_list e musician_list.
function buildNameIndex(albums, peopleFn) {
  const idx = new Map();
  for (const a of albums) {
    for (const n of peopleFn(a)) {
      if (COMPOSITE_NAME.test(n)) continue;
      const k = normName(n);
      if (k.length < 3 || idx.has(k)) continue;
      idx.set(k, n);
    }
  }
  const multi = [...idx.keys()].filter(k => k.includes(' '));
  return { idx, multi };
}

// Trova i musicisti del catalogo citati in un concerto.
// Restituisce [{ key, name, tribute }] senza duplicati.
function matchConcert(c, index) {
  const head = String(c.artist || '');
  const lines = [head, ...String(c.info || '').split('\n')]
    .map(l => l.replace(/["“”«»][^"“”«»]*["“”«»]/g, ' ')     // titoli tra virgolette
               .replace(/\b(works|music|pieces|compositions) by\b.*$/i, ' '))  // repertorio a metà riga
    .filter(l => l.trim() && !CONCERT_SKIP_LINE.test(l));
  const found = new Map();
  const add = (key, inHead) => {
    if (!found.has(key)) found.set(key, { key, name: index.idx.get(key), tribute: inHead && CONCERT_TRIBUTE.test(head) });
  };
  // 1. nomi di due o più parole contenuti nel testo
  lines.forEach((l, i) => {
    const hay = ' ' + normName(l) + ' ';
    for (const k of index.multi) {
      if (hay.includes(' ' + k + ' ')) add(k, i === 0);
    }
  });
  // 2. nomi di una parola sola (es. "Arcanum"): solo se un segmento coincide per intero
  lines.forEach((l, i) => {
    l.split(CONCERT_SPLIT).forEach(seg => {
      const k = normName(seg.replace(CONCERT_TRIBUTE, '').replace(CONCERT_LINEUP_SUFFIX, ''));
      if (k && !k.includes(' ') && index.idx.has(k)) add(k, i === 0);
    });
  });
  return [...found.values()];
}

// Collega i concerti ai nomi. Restituisce Map chiave -> [concerto, ...] ordinati per data.
function linkConcerts(concerts, index) {
  const byKey = new Map();
  for (const c of concerts) {
    c.matches = matchConcert(c, index);
    for (const m of c.matches) {
      if (!byKey.has(m.key)) byKey.set(m.key, []);
      byKey.get(m.key).push(c);
    }
  }
  for (const list of byKey.values()) list.sort((x, y) => x.date.localeCompare(y.date));
  return byKey;
}

// Concerti di un nome del catalogo (anche composto), senza duplicati, per data.
function concertsForName(name, liveByKey) {
  const out = new Map();
  for (const k of nameKeys(name)) {
    for (const c of (liveByKey.get(k) || [])) out.set(c.id, c);
  }
  return [...out.values()].sort((x, y) => x.date.localeCompare(y.date));
}
