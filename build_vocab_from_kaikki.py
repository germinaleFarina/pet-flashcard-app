"""
Costruisce un vocabolario tedesco-inglese a partire dal dizionario
Wiktionary (kaikki.org). Due modalita':

  1) Solo le parole piu' frequenti (comportamento originale), incrociando
     con una lista di frequenza:
        python build_vocab_from_kaikki.py --freq de_50k.txt --kaikki kaikki-german.jsonl --top 5000 --out de-en-vocab-real.csv

  2) TUTTE le parole presenti nel dizionario kaikki, senza filtrare per
     frequenza (--all-words). In questo caso --freq non serve piu':
        python build_vocab_from_kaikki.py --kaikki kaikki-german.jsonl --all-words --out de-en-vocab-full.csv

Sorgenti da scaricare manualmente (sono troppo grandi per essere incluse qui):

  1) Lista di frequenza tedesca (licenza MIT, serve solo in modalita' 1):
     https://github.com/hermitdave/FrequencyWords/blob/master/content/2016/de/de_50k.txt

  2) Dizionario tedesco estratto da Wiktionary (kaikki.org, dati Wiktionary
     sotto licenza CC BY-SA 4.0 / GFDL - se distribuisci l'app, cita la fonte):
     https://kaikki.org/dictionary/German/index.html

Poi importa il risultato come al solito:
    python import_vocab.py --csv de-en-vocab-full.csv
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


def best_gloss(entry):
    for sense in entry.get("senses", []):
        if sense.get("form_of"):
            continue
        for g in sense.get("glosses") or []:
            if not FORM_OF_PATTERN.match(g):
                return g.split(";")[0].strip()
    return None


def extract_translations_filtered(kaikki_path, wanted_lower):
    """Modalita' 1: solo le parole in wanted_lower."""
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

            gloss = best_gloss(entry)
            if gloss:
                found[key] = (word, gloss)
                remaining.discard(key)

    return found


def extract_all_translations(kaikki_path):
    """Modalita' 2: TUTTE le parole tedesche presenti nel dizionario."""
    found = {}
    count = 0

    with open(kaikki_path, encoding="utf-8") as f:
        for line in f:
            if '"German"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("lang") != "German":
                continue

            word = entry.get("word", "")
            if not word:
                continue
            key = word.lower()
            if key in found:
                continue

            gloss = best_gloss(entry)
            if gloss:
                found[key] = (word, gloss)
                count += 1
                if count % 20000 == 0:
                    print(f"  ...{count} parole trovate finora")

    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freq", help="File di frequenza (es. de_50k.txt) - non serve con --all-words")
    parser.add_argument("--kaikki", required=True, help="File JSONL scaricato da kaikki.org")
    parser.add_argument("--top", type=int, default=5000, help="Quante parole piu' frequenti usare (modalita' 1)")
    parser.add_argument("--all-words", action="store_true", help="Estrai tutte le parole del dizionario, ignora --freq/--top")
    parser.add_argument("--out", default="de-en-vocab-real.csv")
    args = parser.parse_args()

    if args.all_words:
        print("Estraggo TUTTE le parole tedesche dal dizionario kaikki (puo' richiedere qualche minuto)...")
        translations = extract_all_translations(args.kaikki)
        print(f"Trovate {len(translations)} parole totali.")
        rows = sorted(translations.values(), key=lambda pair: pair[0].lower())
    else:
        if not args.freq:
            parser.error("--freq e' obbligatorio se non usi --all-words")
        freq_words = load_frequency_list(args.freq, args.top)
        wanted_lower = {w.lower() for w in freq_words}

        print(f"Cerco traduzioni per {len(wanted_lower)} parole tra le piu' frequenti...")
        translations = extract_translations_filtered(args.kaikki, wanted_lower)
        print(f"Trovate traduzioni per {len(translations)} parole su {len(wanted_lower)}.")

        rows = []
        seen = set()
        for w in freq_words:
            key = w.lower()
            if key in translations and key not in seen:
                rows.append(translations[key])
                seen.add(key)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["word", "translation"])
        writer.writerows(rows)

    print(f"Scritte {len(rows)} righe in {args.out}")


if __name__ == "__main__":
    main()
