"""
Importa un vocabolario CSV in un database SQLite (vocab.db) pronto per
la ricerca veloce per prefisso nell'app Kivy (main.py).

Uso:
    python import_vocab.py --csv dizionario.csv --col-word word --col-translation translation
"""

import argparse
import csv
import sqlite3
from pathlib import Path

APP_DIR = Path(__file__).parent
VOCAB_DB = APP_DIR / "vocab.db"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Percorso del CSV del vocabolario")
    parser.add_argument("--col-word", default="word")
    parser.add_argument("--col-translation", default="translation")
    parser.add_argument("--delimiter", default=",")
    args = parser.parse_args()

    if VOCAB_DB.exists():
        VOCAB_DB.unlink()

    con = sqlite3.connect(VOCAB_DB)
    con.execute("CREATE TABLE vocab (word TEXT NOT NULL, translation TEXT NOT NULL)")

    inserted = 0
    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=args.delimiter)
        for row in reader:
            word = (row.get(args.col_word) or "").strip()
            translation = (row.get(args.col_translation) or "").strip()
            if word and translation:
                con.execute(
                    "INSERT INTO vocab (word, translation) VALUES (?, ?)",
                    (word, translation),
                )
                inserted += 1

    con.execute("CREATE INDEX idx_word ON vocab (word COLLATE NOCASE)")
    con.commit()
    con.close()
    print(f"Importate {inserted} voci in {VOCAB_DB}")


if __name__ == "__main__":
    main()
