"""
Costruisce un vocabolario tedesco-inglese reale e ampio, combinando:
  - una lista di frequenza (le parole piu' usate nel tedesco reale)
  - i lemmi/traduzioni estratti da Wiktionary (kaikki.org)

Il risultato e' un CSV con le N parole piu' utili da imparare, ciascuna
con una traduzione inglese vera - compatibile con import_vocab.py.
Tutto avviene offline dopo aver scaricato una tantum i due file sorgente.

Sorgenti da scaricare manualmente (sono troppo grandi per essere incluse qui):

  1) Lista di frequenza tedesca (licenza MIT):
     https://github.com/hermitdave/FrequencyWords/blob/master/content/2016/de/de_50k.txt
     -> clicca "Download raw file" e salvala come de_50k.txt

  2) Dizionario tedesco estratto da Wiktionary (kaikki.org, dati Wiktionary
     sotto licenza CC BY-SA 4.0 / GFDL - se distribuisci l'app, cita la fonte):
     https://kaikki.org/dictionary/German/index.html
     -> cerca il link di download del file JSONL "postprocessed" (circa 1 GB)
        e salvalo come kaikki-german.jsonl

Uso:
    python build_vocab_from_kaikki.py \
        --freq de_50k.txt \
        --kaikki kaikki-german.jsonl \
        --top 5000 \
        --out de-en-vocab-real.csv

Poi importa il risultato come al solito:
    python import_vocab.py --csv de-en-vocab-real.csv
"""

import argparse
import csv
import json
import re

# Riconosce glosse che sono in realta' forme flesse ("genitivo di...",
# "plurale di...") cosi' da preferire il lemma con significato vero.
FORM_OF_PATTERN = re.compile(
    r"^(inflection of|plural of|genitive|dative|accusative|comparative|"
    r"superlative|form of|alternative (spelling|form) of)",
    re.IGNORECASE,
)


def load_frequency_list(path, top_n):
    words = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if not parts:
                continue
            words.append(parts[0])
            if len(words) >= top_n:
                break
    return words


def extract_translations(kaikki_path, wanted_lower):
    """Ritorna {parola_minuscola: (parola_originale, traduzione)}."""
    found = {}
    remaining = set(wanted_lower)

    with open(kaikki_path, encoding="utf-8") as f:
        for line in f:
            if not remaining:
                break
            if '"German"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("lang") != "German":
                continue

            word = entry.get("word", "")
            key = word.lower()
            if key not in remaining:
                continue

            gloss = None
            for sense in entry.get("senses", []):
                if sense.get("form_of"):
                    continue
                for g in sense.get("glosses") or []:
                    if not FORM_OF_PATTERN.match(g):
                        gloss = g
                        break
                if gloss:
                    break

            if gloss:
                gloss = gloss.split(";")[0].strip()
                found[key] = (word, gloss)
                remaining.discard(key)

    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freq", required=True, help="File di frequenza (es. de_50k.txt)")
    parser.add_argument("--kaikki", required=True, help="File JSONL scaricato da kaikki.org")
    parser.add_argument("--top", type=int, default=5000, help="Quante parole piu' frequenti usare")
    parser.add_argument("--out", default="de-en-vocab-real.csv")
    args = parser.parse_args()

    freq_words = load_frequency_list(args.freq, args.top)
    wanted_lower = {w.lower() for w in freq_words}

    print(f"Cerco traduzioni per {len(wanted_lower)} parole tra le piu' frequenti...")
    translations = extract_translations(args.kaikki, wanted_lower)
    print(f"Trovate traduzioni per {len(translations)} parole su {len(wanted_lower)}.")

    rows = []
    seen = set()
    for w in freq_words:
        key = w.lower()
        if key in translations and key not in seen:
            original_word, gloss = translations[key]
            rows.append((original_word, gloss))
            seen.add(key)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["word", "translation"])
        writer.writerows(rows)

    print(f"Scritte {len(rows)} righe in {args.out}")


if __name__ == "__main__":
    main()
