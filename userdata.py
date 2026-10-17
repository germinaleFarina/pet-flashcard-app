"""
userdata.py - i dati dell'utente (nessuna dipendenza da Kivy).

Tre tipi di dati, tutti salvati nel database dell'app (deck.db) e copiati
come FILE DI TESTO nella cartella di backup scelta dall'utente:

    digivol_deck.txt         il mazzo di flashcard (con i progressi di ripasso)
    digivol_my_entries.txt   le voci aggiunte dall'utente al vocabolario
    digivol_corrections.txt  le correzioni segnalate (da inviare agli sviluppatori)

Formato: una riga di intestazione e poi un record JSON per riga (JSON Lines),
UTF-8. E' testo normale, si apre con qualsiasi editor.

Regole di sicurezza (sono il motivo per cui nessun dato si perde):
- importare e' sempre un'UNIONE: non cancella e non sovrascrive nulla di
  quello che c'e' gia' nell'app;
- il file del mazzo non viene mai sovrascritto con un mazzo VUOTO se in
  cartella ce n'e' uno pieno (succede dopo una reinstallazione);
- un file con lo stesso nome che non e' un file DigiVol valido non viene mai
  sovrascritto;
- voci personali e correzioni vengono unite per identificativo: il file in
  cartella non perde mai righe.
"""

import json
import sqlite3
import uuid
from datetime import date, datetime

from vocab_search import normalize, normalize_translation

FORMAT_VERSION = 1

DECK_FILE = "digivol_deck.txt"
ENTRIES_FILE = "digivol_my_entries.txt"
CORRECTIONS_FILE = "digivol_corrections.txt"

# Le voci personali vengono inserite nel vocab.db locale con rowid a partire
# da questo valore: cosi' si distinguono dalle voci di fabbrica e si possono
# rigenerare ad ogni aggiornamento del dizionario.
USER_ROWID_BASE = 10_000_000

# Valori "canonici" salvati nel database e nei file (come nel CSV originale).
TYPE_VALUES = ["", "Sostantivo", "Verbo", "Aggettivo", "Frase"]
GENDER_VALUES = ["", "Maschile", "Femminile", "Neutro"]

_CANON = {
    "sostantivo": "Sostantivo", "noun": "Sostantivo",
    "verbo": "Verbo", "verb": "Verbo",
    "aggettivo": "Aggettivo", "adjective": "Aggettivo",
    "frase": "Frase", "phrase": "Frase",
    "maschile": "Maschile", "masculine": "Maschile",
    "femminile": "Femminile", "feminine": "Femminile",
    "neutro": "Neutro", "neuter": "Neutro",
}

MAX_WORD_LENGTH = 200
MAX_TRANSLATION_LENGTH = 1000
MAX_NOTE_LENGTH = 1000


# ---------------------------------------------------------------------------
# Tabelle (solo "IF NOT EXISTS": un aggiornamento non puo' cancellare nulla)
# ---------------------------------------------------------------------------

def init_user_tables(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS user_entries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE, "
        "word TEXT NOT NULL, translation TEXT NOT NULL, "
        "word_type TEXT, gender TEXT, created TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS corrections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE, "
        "created TEXT NOT NULL, vocab_version INTEGER, "
        "orig_word TEXT, orig_translation TEXT, orig_word_type TEXT, orig_gender TEXT, "
        "new_word TEXT, new_translation TEXT, new_word_type TEXT, new_gender TEXT, "
        "note TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
    )
    con.commit()


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    con.commit()


# ---------------------------------------------------------------------------
# Pulizia dei valori
# ---------------------------------------------------------------------------

def canon_meta(value):
    """Tipo/genere -> valore canonico (es. 'noun' -> 'Sostantivo') oppure None."""
    v = (str(value).strip().lower() if value is not None else "")
    return _CANON.get(v)


def clean_text(value, max_length):
    """Toglie spazi e a-capo in eccesso (una riga di file = un record)."""
    if value is None:
        return ""
    return " ".join(str(value).split())[:max_length]


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Lettura/scrittura del formato a righe
# ---------------------------------------------------------------------------

def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _header(kind):
    return _dumps({"digivol": kind, "version": FORMAT_VERSION})


def parse_records(text, kind):
    """-> (lista di record, numero di righe non valide).
    ValueError se il file non e' un file DigiVol del tipo atteso."""
    lines = [ln.strip() for ln in text.lstrip("\ufeff").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise ValueError("the file is empty")
    try:
        head = json.loads(lines[0])
    except ValueError:
        head = None
    if not isinstance(head, dict) or head.get("digivol") != kind:
        raise ValueError(f"not a DigiVol {kind} file")
    if not isinstance(head.get("version"), int) or head["version"] > FORMAT_VERSION:
        raise ValueError("file created by a newer version of the app")
    records, bad = [], 0
    for ln in lines[1:]:
        try:
            rec = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            bad += 1
    return records, bad


def _as_int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value, default, lo, hi):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _as_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return date.today().isoformat()


# ---------------------------------------------------------------------------
# Mazzo
# ---------------------------------------------------------------------------

def export_deck_text(con):
    rows = con.execute(
        "SELECT front, back, due_date, interval_days, ease_factor, repetitions "
        "FROM deck ORDER BY id"
    ).fetchall()
    lines = [_header("deck")]
    for front, back, due, interval, ease, reps in rows:
        lines.append(_dumps({
            "front": front, "back": back, "due": due,
            "interval": interval, "ease": ease, "reps": reps,
        }))
    return "\n".join(lines) + "\n"


def merge_deck_text(con, text):
    """Aggiunge al mazzo le carte del file che non ci sono ancora (stessa
    coppia fronte/retro = stessa carta, si tiene quella gia' presente).
    -> (aggiunte, gia_presenti, righe_non_valide)"""
    records, bad = parse_records(text, "deck")
    added = skipped = 0
    for rec in records:
        front = clean_text(rec.get("front"), 2000)
        back = clean_text(rec.get("back"), 2000)
        if not front or not back:
            bad += 1
            continue
        if con.execute(
            "SELECT 1 FROM deck WHERE front = ? AND back = ? LIMIT 1", (front, back)
        ).fetchone():
            skipped += 1
            continue
        con.execute(
            "INSERT INTO deck (front, back, due_date, interval_days, ease_factor, repetitions) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (front, back, _as_date(rec.get("due")),
             _as_int(rec.get("interval"), 0, 0, 36500),
             _as_float(rec.get("ease"), 2.5, 1.3, 5.0),
             _as_int(rec.get("reps"), 0, 0, 100000)),
        )
        added += 1
    con.commit()
    return added, skipped, bad


# ---------------------------------------------------------------------------
# Voci personali
# ---------------------------------------------------------------------------

def _entry_exists(con, word, translation):
    return con.execute(
        "SELECT 1 FROM user_entries WHERE lower(word) = lower(?) "
        "AND lower(translation) = lower(?) LIMIT 1",
        (word, translation),
    ).fetchone() is not None


def add_user_entry(con, word, translation, word_type=None, gender=None, uid=None, created=None):
    """-> id della nuova voce, oppure None se esiste gia' (stessa parola e
    stessa traduzione)."""
    word = clean_text(word, MAX_WORD_LENGTH)
    translation = clean_text(translation, MAX_TRANSLATION_LENGTH)
    if not word or not translation:
        raise ValueError("word and translation are required")
    if _entry_exists(con, word, translation):
        return None
    uid = uid or uuid.uuid4().hex
    if con.execute("SELECT 1 FROM user_entries WHERE uid = ?", (uid,)).fetchone():
        return None
    cur = con.execute(
        "INSERT INTO user_entries (uid, word, translation, word_type, gender, created) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uid, word, translation, canon_meta(word_type), canon_meta(gender), created or _now()),
    )
    con.commit()
    return cur.lastrowid


def list_user_entries(con):
    cols = ("id", "uid", "word", "translation", "word_type", "gender", "created")
    rows = con.execute(
        "SELECT id, uid, word, translation, word_type, gender, created "
        "FROM user_entries ORDER BY id"
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def export_entries_text(con):
    lines = [_header("entries")]
    for e in list_user_entries(con):
        lines.append(_dumps({
            "uid": e["uid"], "word": e["word"], "translation": e["translation"],
            "type": e["word_type"], "gender": e["gender"], "created": e["created"],
        }))
    return "\n".join(lines) + "\n"


def merge_entries_text(con, text):
    records, bad = parse_records(text, "entries")
    added = skipped = 0
    for rec in records:
        try:
            new_id = add_user_entry(
                con, rec.get("word"), rec.get("translation"),
                rec.get("type"), rec.get("gender"),
                uid=clean_text(rec.get("uid"), 64) or None,
                created=clean_text(rec.get("created"), 40) or None,
            )
        except ValueError:
            bad += 1
            continue
        if new_id is None:
            skipped += 1
        else:
            added += 1
    return added, skipped, bad


def sync_user_entries_into_vocab(vocab_con, entries):
    """Rende le voci personali ricercabili: le (ri)scrive nel vocab.db locale
    con rowid >= USER_ROWID_BASE. Si puo' chiamare quante volte si vuole."""
    vocab_con.execute("PRAGMA query_only = OFF")
    try:
        vocab_con.execute("DELETE FROM vocab WHERE rowid >= ?", (USER_ROWID_BASE,))
        vocab_con.executemany(
            "INSERT INTO vocab (rowid, word, translation, word_type, gender, "
            "word_norm, translation_norm) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (USER_ROWID_BASE + e["id"], e["word"], e["translation"],
                 e["word_type"], e["gender"],
                 normalize(e["word"]), normalize_translation(e["translation"]))
                for e in entries
            ],
        )
        vocab_con.commit()
    finally:
        vocab_con.execute("PRAGMA query_only = ON")


# ---------------------------------------------------------------------------
# Correzioni
# ---------------------------------------------------------------------------

_FIELDS = ("word", "translation", "word_type", "gender")


def same_entry(a, b):
    return all((a.get(k) or "") == (b.get(k) or "") for k in _FIELDS)


def add_correction(con, original, corrected, note="", vocab_version=None, uid=None, created=None):
    """Registra una correzione. original/corrected: dict con word,
    translation, word_type, gender. -> uid, o None se esiste gia'."""
    uid = uid or uuid.uuid4().hex
    if con.execute("SELECT 1 FROM corrections WHERE uid = ?", (uid,)).fetchone():
        return None
    o = {
        "word": clean_text(original.get("word"), MAX_WORD_LENGTH),
        "translation": clean_text(original.get("translation"), MAX_TRANSLATION_LENGTH),
        "word_type": canon_meta(original.get("word_type")),
        "gender": canon_meta(original.get("gender")),
    }
    n = {
        "word": clean_text(corrected.get("word"), MAX_WORD_LENGTH),
        "translation": clean_text(corrected.get("translation"), MAX_TRANSLATION_LENGTH),
        "word_type": canon_meta(corrected.get("word_type")),
        "gender": canon_meta(corrected.get("gender")),
    }
    con.execute(
        "INSERT INTO corrections (uid, created, vocab_version, orig_word, orig_translation, "
        "orig_word_type, orig_gender, new_word, new_translation, new_word_type, new_gender, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, created or _now(), vocab_version, o["word"], o["translation"],
         o["word_type"], o["gender"], n["word"], n["translation"],
         n["word_type"], n["gender"], clean_text(note, MAX_NOTE_LENGTH)),
    )
    con.commit()
    return uid


def export_corrections_text(con):
    lines = [_header("corrections")]
    rows = con.execute(
        "SELECT uid, created, vocab_version, orig_word, orig_translation, orig_word_type, "
        "orig_gender, new_word, new_translation, new_word_type, new_gender, note "
        "FROM corrections ORDER BY id"
    ).fetchall()
    for (uid, created, ver, ow, ot, oty, og, nw, nt, nty, ng, note) in rows:
        lines.append(_dumps({
            "uid": uid, "created": created, "vocab_version": ver,
            "original": {"word": ow, "translation": ot, "type": oty, "gender": og},
            "corrected": {"word": nw, "translation": nt, "type": nty, "gender": ng},
            "note": note,
        }))
    return "\n".join(lines) + "\n"


def merge_corrections_text(con, text):
    records, bad = parse_records(text, "corrections")
    added = skipped = 0
    for rec in records:
        o, n = rec.get("original"), rec.get("corrected")
        uid = clean_text(rec.get("uid"), 64)
        if not (isinstance(o, dict) and isinstance(n, dict) and uid):
            bad += 1
            continue
        to_db = lambda d: {  # noqa: E731
            "word": d.get("word"), "translation": d.get("translation"),
            "word_type": d.get("type"), "gender": d.get("gender"),
        }
        result = add_correction(
            con, to_db(o), to_db(n), rec.get("note") or "",
            rec.get("vocab_version") if isinstance(rec.get("vocab_version"), int) else None,
            uid=uid, created=clean_text(rec.get("created"), 40) or None,
        )
        if result is None:
            skipped += 1
        else:
            added += 1
    return added, skipped, bad


# ---------------------------------------------------------------------------
# Backup e ripristino (storage = oggetto con read_text(name) e write_text(name, text))
# ---------------------------------------------------------------------------

def backup_deck(con, storage):
    """Scrive il mazzo nella cartella. -> 'written' | 'skipped_empty' |
    'skipped_invalid' (il file esistente non e' un mazzo DigiVol)."""
    existing = storage.read_text(DECK_FILE)
    if existing and existing.strip():
        try:
            cards, _ = parse_records(existing, "deck")
        except ValueError:
            return "skipped_invalid"
        local = con.execute("SELECT COUNT(*) FROM deck").fetchone()[0]
        if local == 0 and cards:
            return "skipped_empty"
    storage.write_text(DECK_FILE, export_deck_text(con))
    return "written"


def backup_extras(con, storage):
    """Voci personali e correzioni: prima unisce quello che c'e' nel file,
    poi riscrive il file. -> {'entries_added': n, 'problems': [...]}"""
    report = {"entries_added": 0, "problems": []}
    for name, merger, exporter, key in (
        (ENTRIES_FILE, merge_entries_text, export_entries_text, "entries_added"),
        (CORRECTIONS_FILE, merge_corrections_text, export_corrections_text, None),
    ):
        existing = storage.read_text(name)
        if existing and existing.strip():
            try:
                added = merger(con, existing)[0]
            except ValueError as exc:
                report["problems"].append(f"{name}: {exc} (left untouched)")
                continue
            if key:
                report[key] += added
        storage.write_text(name, exporter(con))
    return report


def backup_all(con, storage):
    report = backup_extras(con, storage)
    report["deck"] = backup_deck(con, storage)
    if report["deck"] == "skipped_invalid":
        report["problems"].append(f"{DECK_FILE}: not a DigiVol deck file (left untouched)")
    return report


def restore_all(con, storage):
    """Unisce al contenuto dell'app tutto quello che c'e' nei file della
    cartella (non cancella e non sovrascrive nulla)."""
    report = {"cards_added": 0, "entries_added": 0, "corrections_added": 0,
              "bad_lines": 0, "found": 0, "problems": []}
    for name, merger, key in (
        (DECK_FILE, merge_deck_text, "cards_added"),
        (ENTRIES_FILE, merge_entries_text, "entries_added"),
        (CORRECTIONS_FILE, merge_corrections_text, "corrections_added"),
    ):
        text = storage.read_text(name)
        if text is None:
            continue
        report["found"] += 1
        try:
            added, _skipped, bad = merger(con, text)
        except ValueError as exc:
            report["problems"].append(f"{name}: {exc}")
            continue
        report[key] += added
        report["bad_lines"] += bad
    return report
