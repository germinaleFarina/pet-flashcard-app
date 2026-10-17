"""
import_vocab.py - crea vocab.db a partire dal CSV del vocabolario.

Uso (dalla cartella del progetto, accanto a main.py):
    python import_vocab.py --csv vocabolario_finale.csv

Opzioni (tutte facoltative, i default vanno bene per vocabolario_finale.csv):
    --out vocab.db            file di destinazione
    --col-word word           nome della colonna con la parola tedesca
    --col-translation translation
    --col-type Type           colonna con il tipo di parola
    --col-gender Gender       colonna con il genere

Cosa fa:
- legge il CSV in UTF-8 (anche con BOM);
- trasforma "None"/vuoto di Type e Gender in NULL;
- calcola le colonne di ricerca word_norm e translation_norm (testo senza
  maiuscole, accenti e punteggiatura, vedi vocab_search.py), che l'app usa per
  la ricerca e per la correzione dei refusi;
- ripara le righe con virgolette rotte (l'intera riga finita nella colonna
  word) invece di scartarle;
- scrive prima in un file temporaneo e lo sostituisce solo a import riuscito,
  cosi' un errore non distrugge il vocab.db precedente;
- marca il database con un numero di versione (PRAGMA user_version): al primo
  avvio l'app lo confronta con la copia locale e la sostituisce se questa e'
  piu' vecchia. Il mazzo (deck.db) non viene toccato.
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vocab_search import normalize, normalize_translation  # noqa: E402


def clean_meta(value):
    """'None', 'none' e vuoto diventano NULL; il resto resta com'e'."""
    v = (value or "").strip()
    return None if v == "" or v.lower() == "none" else v


def repair_row(word, translation):
    """Alcune righe del CSV hanno l'intero record dentro la colonna word
    (translation vuota). Le rileggiamo come una riga CSV a se' stante.
    Restituisce (word, translation, type, gender) oppure None se non
    recuperabile."""
    if translation.strip():
        return None
    try:
        parts = next(csv.reader(io.StringIO(word)))
    except (csv.Error, StopIteration):
        return None
    if len(parts) == 4 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip(), parts[2], parts[3]
    return None


def main():
    ap = argparse.ArgumentParser(description="Crea vocab.db dal CSV del vocabolario.")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", default=str(Path(__file__).parent / "vocab.db"))
    ap.add_argument("--col-word", default="word")
    ap.add_argument("--col-translation", default="translation")
    ap.add_argument("--col-type", default="Type")
    ap.add_argument("--col-gender", default="Gender")
    args = ap.parse_args()

    out_path = Path(args.out)
    tmp_path = out_path.with_suffix(".db.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    rows = []
    repaired = skipped = 0
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in (args.col_word, args.col_translation) if c not in (reader.fieldnames or [])]
        if missing:
            sys.exit(f"Colonne non trovate nel CSV: {missing}. Colonne presenti: {reader.fieldnames}")
        has_type = args.col_type in reader.fieldnames
        has_gender = args.col_gender in reader.fieldnames

        for r in reader:
            word = (r.get(args.col_word) or "").strip()
            translation = (r.get(args.col_translation) or "").strip()
            wtype = r.get(args.col_type) if has_type else None
            gender = r.get(args.col_gender) if has_gender else None

            if not translation:
                fixed = repair_row(word, translation)
                if fixed is None:
                    skipped += 1
                    continue
                word, translation, wtype, gender = fixed
                repaired += 1

            if not word:
                skipped += 1
                continue
            rows.append((
                word, translation, clean_meta(wtype), clean_meta(gender),
                normalize(word), normalize_translation(translation),
            ))

    con = sqlite3.connect(str(tmp_path))
    con.execute(
        "CREATE TABLE vocab ("
        "word TEXT NOT NULL, translation TEXT NOT NULL, "
        "word_type TEXT, gender TEXT, "
        "word_norm TEXT NOT NULL, translation_norm TEXT NOT NULL)"
    )
    con.executemany(
        "INSERT INTO vocab (word, translation, word_type, gender, word_norm, translation_norm) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    # Indici sulle colonne normalizzate: la ricerca per prefisso e' un
    # intervallo (>= prefisso e < prefisso+\uffff), istantaneo in entrambe
    # le direzioni.
    con.execute("CREATE INDEX idx_word_norm ON vocab (word_norm)")
    con.execute("CREATE INDEX idx_translation_norm ON vocab (translation_norm)")
    # Versione = minuti dall'epoch: ogni nuovo import ha un numero piu' alto.
    con.execute(f"PRAGMA user_version = {int(time.time() // 60)}")
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.execute("VACUUM")
    con.close()

    os.replace(tmp_path, out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Fatto: {len(rows)} voci in {out_path} ({size_mb:.1f} MB)")
    print(f"  righe riparate: {repaired} | righe scartate: {skipped}")


if __name__ == "__main__":
    main()
