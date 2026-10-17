"""
Costruisce un vocabolario tedesco-inglese a partire dal dump COMPLETO di
Wikidata Lexemes (la parte lessicografica di Wikidata: lemmi, forme
grammaticali, sensi/glosse - non il dump enciclopedico "di tutto Wikidata",
che e' 100x piu' grande e non e' un dizionario).

Licenza dei dati: Wikidata e' CC0 (dominio pubblico) - nessun obbligo di
attribuzione, anche per uso commerciale.

--- Come scaricare il dump (una tantum, ~390 MB compressi) ---

    curl -L -o wikidata-lexemes.json.bz2 https://dumps.wikimedia.org/wikidatawiki/entities/latest-lexemes.json.bz2

Non serve decomprimerlo a mano: questo script legge direttamente il file
.bz2 in streaming (riga per riga), senza scriverlo mai per intero su disco
decompresso.

--- Uso ---

    python build_vocab_from_wikidata.py --dump wikidata-lexemes.json.bz2 --out de-en-vocab-wikidata.csv

Poi importalo come al solito (o uniscilo a un CSV gia' esistente prima di
importarlo, per sommare le due fonti):

    python import_vocab.py --csv de-en-vocab-wikidata.csv
"""

import argparse
import bz2
import csv
import json

# QID Wikidata delle lingue: tedesco e inglese. Sono identificatori stabili
# e pubblici (es. https://www.wikidata.org/wiki/Q188 = lingua tedesca).
GERMAN_LANGUAGE_QID = "Q188"
ENGLISH_LANGUAGE_QID = "Q1860"


def iter_lexeme_lines(dump_path):
    """Itera sulle righe del dump JSON (bz2), saltando le parentesi [ ] di
    apertura/chiusura dell'array e le virgole finali di ogni riga."""
    with bz2.open(dump_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line in ("[", "]"):
                continue
            if line.endswith(","):
                line = line[:-1]
            yield line


def extract_german_english(dump_path):
    """Ritorna {parola_minuscola: (parola_originale, glossa_inglese)} per
    ogni lessema tedesco che ha almeno un senso con glossa in inglese."""
    found = {}
    processed = 0
    matched = 0

    for line in iter_lexeme_lines(dump_path):
        processed += 1
        if processed % 200000 == 0:
            print(f"  ...{processed} lessemi esaminati, {matched} tedeschi con glossa inglese trovati finora")

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("language") != GERMAN_LANGUAGE_QID:
            continue

        lemma = (entry.get("lemmas") or {}).get("de", {}).get("value")
        if not lemma:
            continue

        key = lemma.lower()
        if key in found:
            continue

        gloss = None
        for sense in entry.get("senses", []):
            en_gloss = (sense.get("glosses") or {}).get("en", {}).get("value")
            if en_gloss:
                gloss = en_gloss
                break

        if gloss:
            found[key] = (lemma, gloss)
            matched += 1

    print(f"Esaminati {processed} lessemi totali (tutte le lingue). Trovati {matched} lemmi tedeschi con glossa inglese.")
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", required=True, help="File wikidata-lexemes.json.bz2 scaricato")
    parser.add_argument("--out", default="de-en-vocab-wikidata.csv")
    args = parser.parse_args()

    translations = extract_german_english(args.dump)

    rows = sorted(translations.values(), key=lambda pair: pair[0].lower())

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["word", "translation"])
        writer.writerows(rows)

    print(f"Scritte {len(rows)} righe in {args.out}")


if __name__ == "__main__":
    main()
