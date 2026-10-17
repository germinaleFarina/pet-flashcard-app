"""
vocab_search.py - ricerca nel vocabolario (nessuna dipendenza da Kivy).

Contiene:
- normalize(): il modo in cui il testo viene "appiattito" per la ricerca.
  E' usato sia da import_vocab.py (colonne word_norm / translation_norm di
  vocab.db) sia dall'app per il testo digitato: deve essere identico nei due
  casi, per questo vive in un solo file. Se lo modifichi, rigenera vocab.db.
- VocabSearcher: la ricerca vera e propria, con
    * ricerca per prefisso e per parole (lo spazio chiude una parola);
    * tolleranza ai refusi (sostituzioni, lettere mancanti/in piu',
      lettere scambiate, anche sulla prima lettera).

Come vengono trattati gli spazi
-------------------------------
- "haus"      -> parole che INIZIANO con "haus" (Haus, Hausarbeit, ...)
- "haus "     -> la parola "haus" e' finita: Haus, "Haus und Hof", ma non
                 Hausarbeit
- "haus u"    -> voci che iniziano con la parola "haus" e contengono una
                 parola che inizia con "u" (Haus und Hof)
- "haus tuer" -> se nessuna voce contiene entrambe le parole, ogni parola
                 viene cercata da sola e i risultati sono raggruppati
"""

import bisect
import re
import time
import unicodedata

# ---------------------------------------------------------------------------
# Normalizzazione
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[\W_]+")

# Lunghezza massima della chiave normalizzata della traduzione: basta per la
# ricerca per prefisso e per i refusi, e tiene piccolo il database.
TRANSLATION_KEY_MAX = 80


def normalize(text):
    """Minuscolo (anche sulle lettere accentate), ä/ö/ü -> ae/oe/ue,
    ß -> ss, altri accenti tolti, punteggiatura e spazi multipli -> un solo
    spazio. Es.: 'Übung' -> 'uebung', 'Straße' -> 'strasse', 'Café' -> 'cafe'."""
    if not text:
        return ""
    s = unicodedata.normalize("NFC", str(text)).casefold()  # casefold: ß -> ss
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", s).strip()


def normalize_translation(text):
    return normalize(text)[:TRANSLATION_KEY_MAX].rstrip()


# ---------------------------------------------------------------------------
# Parametri
# ---------------------------------------------------------------------------

MAX_RESULTS = 15           # risultati mostrati in lista
MAX_QUERY_WORDS = 6        # parole considerate in una ricerca
TYPO_MIN_LENGTH = 3        # sotto questa lunghezza non si correggono refusi
FUZZY_KEY_LENGTH = 40      # caratteri di ogni voce tenuti in memoria
FUZZY_TIME_BUDGET_SECONDS = 0.25   # tetto di tempo per la correzione refusi
EDGE_CANDIDATE_CAP = 300   # candidati per ogni controllo sulla prima lettera

_HI = "\uffff"  # per gli intervalli "inizia con": [prefisso, prefisso + _HI)


# ---------------------------------------------------------------------------
# Distanza di edit (Damerau/OSA) con taglio anticipato
# ---------------------------------------------------------------------------

def _typo_distance(q, c, max_d, whole_word):
    """Numero minimo di errori di battitura (sostituzione, lettera mancante o
    in piu', due lettere adiacenti scambiate) tra q e c.
    - whole_word=False: q viene confrontato con l'INIZIO di c (ricerca per
      prefisso): le lettere in piu' di c non contano.
    - whole_word=True: q deve corrispondere all'intera parola c.
    Restituisce max_d + 1 se la distanza supera max_d."""
    n = len(q)
    if whole_word:
        if abs(len(c) - n) > max_d:
            return max_d + 1
    else:
        if len(c) < n - max_d:
            return max_d + 1
        c = c[: n + max_d]
    m = len(c)

    prev2 = None
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        qi = q[i - 1]
        cur = [i] + [0] * m
        row_min = i
        for j in range(1, m + 1):
            v = prev[j - 1] + (0 if qi == c[j - 1] else 1)
            t = prev[j] + 1
            if t < v:
                v = t
            t = cur[j - 1] + 1
            if t < v:
                v = t
            if prev2 is not None and j > 1 and qi == c[j - 2] and q[i - 2] == c[j - 1]:
                t = prev2[j - 2] + 1
                if t < v:
                    v = t
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > max_d:
            return max_d + 1
        prev2, prev = prev, cur

    if whole_word:
        return prev[m]
    return min(prev[max(0, n - max_d): min(m, n + max_d) + 1])


class _FuzzyIndex:
    """Elenco ordinato in memoria delle voci normalizzate di una colonna,
    usato SOLO quando la ricerca esatta non trova nulla. Si costruisce una
    volta (durante la schermata iniziale) e permette di limitare il lavoro
    ai soli candidati plausibili invece di scorrere tutto il dizionario."""

    def __init__(self, con, column):
        rows = con.execute(
            f"SELECT substr({column}, 1, ?), rowid FROM vocab WHERE {column} != ''",
            (FUZZY_KEY_LENGTH,),
        ).fetchall()
        rows.sort()
        self.keys = [r[0] for r in rows]
        self.rids = [r[1] for r in rows]
        # Stesso elenco ordinato "senza la prima lettera": serve a trovare
        # i refusi sulla prima lettera con una semplice ricerca binaria.
        self.order = sorted(range(len(self.keys)), key=lambda i: self.keys[i][1:])
        self.skeys = [self.keys[i][1:] for i in self.order]

    @staticmethod
    def _span(sorted_list, prefix):
        return (bisect.bisect_left(sorted_list, prefix),
                bisect.bisect_left(sorted_list, prefix + _HI))

    def search(self, q, whole_word, limit):
        """Restituisce i rowid delle voci piu' vicine a q (entro 1-2 refusi),
        ordinate per numero di errori, poi per lunghezza."""
        n = len(q)
        if n < TYPO_MIN_LENGTH:
            return []
        max_d = 1 if n <= 5 else 2
        found = {}
        deadline = time.perf_counter() + FUZZY_TIME_BUDGET_SECONDS

        def consider(idx):
            rid = self.rids[idx]
            if rid in found:
                return
            key = self.keys[idx]
            target = key.split(" ", 1)[0] if whole_word else key
            d = _typo_distance(q, target, max_d, whole_word)
            if d <= max_d:
                found[rid] = (d, len(key), key)

        # 1) Casi in cui le prime lettere sono sbagliate: il resto e' giusto,
        #    quindi bastano poche ricerche binarie.
        #    a) due lettere adiacenti scambiate, in qualunque punto
        for i in range(n - 1):
            if q[i] != q[i + 1]:
                swapped = q[:i] + q[i + 1] + q[i] + q[i + 2:]
                lo, hi = self._span(self.keys, swapped)
                for idx in range(lo, min(hi, lo + EDGE_CANDIDATE_CAP)):
                    consider(idx)
        #    b) prima lettera sbagliata, mancante o in piu'
        lo, hi = self._span(self.skeys, q[1:])
        for k in range(lo, min(hi, lo + EDGE_CANDIDATE_CAP)):
            consider(self.order[k])
        lo, hi = self._span(self.keys, q[1:])
        for idx in range(lo, min(hi, lo + EDGE_CANDIDATE_CAP)):
            consider(idx)
        lo, hi = self._span(self.skeys, q)
        for k in range(lo, min(hi, lo + EDGE_CANDIDATE_CAP)):
            consider(self.order[k])

        # 2) Refuso piu' avanti: si controllano le voci che condividono le
        #    prime 3, poi 2, poi 1 lettere, allargando solo se serve.
        prefix_lengths = [p for p in (3, 2, 1) if (p <= n if whole_word else p < n)]
        timed_out = False
        for plen in prefix_lengths:
            lo, hi = self._span(self.keys, q[:plen])
            for count, idx in enumerate(range(lo, hi)):
                if count % 128 == 0 and time.perf_counter() > deadline:
                    timed_out = True
                    break
                consider(idx)
            if timed_out:
                break
            # Allarga ai gruppi piu' grandi solo finche' non ci sono abbastanza
            # candidati a un solo errore da riempire la lista.
            if sum(1 for v in found.values() if v[0] <= 1) >= limit:
                break

        ranked = sorted(found.items(), key=lambda kv: kv[1])
        return [rid for rid, _ in ranked[:limit]]


# ---------------------------------------------------------------------------
# Ricerca
# ---------------------------------------------------------------------------

class VocabSearcher:
    """Cerca nel vocab.db (tabella vocab con le colonne word, translation,
    word_type, gender, word_norm, translation_norm).

    search() restituisce una lista di gruppi (intestazione, righe): di norma
    un solo gruppo senza intestazione; se la ricerca ha piu' parole che non
    compaiono insieme in nessuna voce, un gruppo per ogni parola.
    Ogni riga e' (rowid, word, translation, word_type, gender)."""

    _SELECT = "SELECT rowid, word, translation, word_type, gender FROM vocab"
    REQUIRED_COLUMNS = {"word", "translation", "word_type", "gender",
                        "word_norm", "translation_norm"}

    def __init__(self, con):
        self.con = con
        self._fuzzy = {}

    @classmethod
    def check_schema(cls, con):
        cols = {row[1] for row in con.execute("PRAGMA table_info(vocab)")}
        missing = cls.REQUIRED_COLUMNS - cols
        if missing:
            raise RuntimeError(
                "vocab.db e' di una versione vecchia (mancano le colonne "
                f"{sorted(missing)}). Rigeneralo con: "
                "python import_vocab.py --csv vocabolario_finale.csv"
            )

    @staticmethod
    def column(reversed_search):
        return "translation_norm" if reversed_search else "word_norm"

    def reset(self):
        """Da chiamare quando il contenuto di vocab cambia (es. voce
        personale aggiunta): l'elenco per i refusi verra' ricostruito."""
        self._fuzzy.clear()

    def warm_up(self, reversed_search=False):
        """Costruisce in anticipo l'indice per i refusi (pensato per essere
        chiamato mentre l'utente guarda la schermata iniziale)."""
        self._fuzzy_index(self.column(reversed_search))

    def _fuzzy_index(self, col):
        if col not in self._fuzzy:
            self._fuzzy[col] = _FuzzyIndex(self.con, col)
        return self._fuzzy[col]

    # ----- interpretazione del testo digitato -----
    @staticmethod
    def parse(text):
        """-> (parole normalizzate, ultima_parola_conclusa). L'ultima parola
        e' conclusa se il testo finisce con uno spazio o una punteggiatura."""
        tokens = normalize(text).split()[:MAX_QUERY_WORDS]
        last_complete = bool(text) and not text[-1].isalnum()
        return tokens, last_complete

    def search(self, text, reversed_search=False):
        col = self.column(reversed_search)
        tokens, last_complete = self.parse(text)
        if not tokens:
            return []

        if len(tokens) == 1:
            rows = self._lookup(col, tokens[0], last_complete, MAX_RESULTS)
            return [(None, rows)] if rows else []

        rows = self._phrase(col, tokens, last_complete, MAX_RESULTS)
        if rows:
            return [(None, rows)]

        # Nessuna voce contiene tutte le parole: ognuna viene cercata da sola.
        raw = re.findall(r"[^\W_]+", text)
        labels = raw[:MAX_QUERY_WORDS] if len(raw) >= len(tokens) else tokens
        per_word = max(3, MAX_RESULTS // len(tokens))
        groups, seen = [], set()
        for i, tok in enumerate(tokens):
            if tok in seen:
                continue
            seen.add(tok)
            complete = i < len(tokens) - 1 or last_complete
            word_rows = self._lookup(col, tok, complete, per_word)
            if word_rows:
                groups.append((labels[i], word_rows))
        return groups

    # ----- singola parola: esatta, poi con correzione refusi -----
    def _lookup(self, col, token, complete, limit):
        rows = self._exact_single(col, token, complete, limit)
        if rows or len(token) < TYPO_MIN_LENGTH:
            return rows
        rids = self._fuzzy_index(col).search(token, complete, limit)
        return self._fetch(rids)

    def _exact_single(self, col, t, complete, limit):
        if not complete:
            return self.con.execute(
                f"{self._SELECT} WHERE {col} >= ? AND {col} < ? "
                f"ORDER BY {col}, word LIMIT ?",
                (t, t + _HI, limit),
            ).fetchall()
        # Parola conclusa: prima la voce che e' esattamente quella parola,
        # poi le voci che iniziano con quella parola seguita da uno spazio.
        rows = self.con.execute(
            f"{self._SELECT} WHERE {col} = ? ORDER BY word LIMIT ?", (t, limit)
        ).fetchall()
        if len(rows) < limit:
            rows += self.con.execute(
                f"{self._SELECT} WHERE {col} >= ? AND {col} < ? "
                f"ORDER BY {col}, word LIMIT ?",
                (t + " ", t + "!", limit - len(rows)),
            ).fetchall()
        return rows

    # ----- piu' parole nella stessa voce -----
    def _phrase(self, col, tokens, last_complete, limit):
        """Voci che iniziano con la prima parola (conclusa, perche' dopo c'e'
        uno spazio) e contengono, come parole intere o inizi di parola,
        tutte le altre."""
        first = tokens[0]
        where = [f"{col} >= ?", f"{col} < ?"]
        params = [first + " ", first + "!"]
        for i, tok in enumerate(tokens[1:], start=1):
            if i < len(tokens) - 1 or last_complete:
                where.append(f"({col} LIKE ? OR {col} LIKE ?)")
                params += [f"% {tok} %", f"% {tok}"]
            else:
                where.append(f"{col} LIKE ?")
                params.append(f"% {tok}%")
        params.append(limit)
        return self.con.execute(
            f"{self._SELECT} WHERE {' AND '.join(where)} "
            f"ORDER BY {col}, word LIMIT ?",
            params,
        ).fetchall()

    def _fetch(self, rids):
        if not rids:
            return []
        placeholders = ",".join("?" * len(rids))
        rows = self.con.execute(
            f"{self._SELECT} WHERE rowid IN ({placeholders})", rids
        ).fetchall()
        by_id = {r[0]: r for r in rows}
        return [by_id[r] for r in rids if r in by_id]
