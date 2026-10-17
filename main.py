"""
Flashcard Quick-Add - versione mobile (Kivy/KivyMD)
----------------------------------------------------
App standalone, senza dipendenze da Anki: vocabolario e mazzo vivono
entrambi in database SQLite locali (nessun server, nessuna connessione
richiesta dopo la prima importazione del vocabolario).

Include ripasso a ripetizione spaziata (algoritmo SM-2 semplificato):
ogni carta ha un intervallo, un fattore di facilita' e una data di
scadenza che si aggiornano in base a come valuti il ripasso.

Include anche uno switch per invertire la direzione di ricerca:
di default cerchi parole tedesche e vedi la traduzione inglese;
con il pulsante "swap" nella barra in alto cerchi parole inglesi
e vedi la traduzione tedesca.

Funzioni del mazzo:
- Ordine di ripasso casuale (shuffle) ad ogni nuova sessione.
- Niente doppioni: se provi ad aggiungere una coppia fronte/retro gia'
  presente, l'app mostra un avviso invece di duplicarla.
- Puoi rimuovere singole flashcard dal mazzo (icona cestino).
- Puoi far ripartire il ripasso da capo quando vuoi (icona restart, in
  alto a destra nella schermata Ripasso) per un ripasso anticipato: ricarica
  SEMPRE tutte le carte presenti nel mazzo in questo momento, in un nuovo
  ordine casuale, indipendentemente da quali hai gia' valutato oggi o da
  quante volte hai chiuso/riaperto l'app. Le carte non vengono mai perse
  ne' cancellate da questa funzione: solo lette e rimostrate.
- Sessione di ripasso "con memoria": se chiudi l'app a meta' ripasso e la
  riapri entro poche ore, riprendi da dove avevi lasciato (stesse carte,
  stesso ordine) invece di rifare tutto da capo. Oltre quella finestra di
  tempo, la sessione scade e al prossimo ingresso si ricostruisce una coda
  nuova (mescolata) dalle carte effettivamente scadute in quel momento.

Note sulle prestazioni:
- Le connessioni SQLite (vocab e mazzo) vengono aperte UNA volta sola
  all'avvio e riutilizzate, invece di aprirle e chiuderle ad ogni ricerca.
- La ricerca usa un piccolo "debounce": aspetta ~180ms dopo l'ultimo
  tasto premuto prima di interrogare il database e ricostruire la lista,
  cosi' digitare veloce non genera una query/redraw per ogni lettera.
- La ricerca (vocab_search.py) lavora su colonne normalizzate e indicizzate
  (senza maiuscole/accenti), cosi' entrambe le direzioni restano rapide
  anche con centinaia di migliaia di voci.

Ricerca:
- Tolleranza ai refusi: lettere sbagliate, mancanti, in piu' o scambiate,
  anche sulla prima lettera; "uebung", "übung" e "Übung" sono equivalenti,
  come "strasse" e "Straße".
- Lo spazio conclude una parola: "haus " trova Haus e "Haus und Hof" ma non
  Hausarbeit; "haus u" trova "Haus und Hof"; se due parole non compaiono
  insieme in nessuna voce ("haus tuer"), ognuna viene cercata da sola e i
  risultati sono raggruppati per parola.

Prima del primo avvio, genera vocab.db dal CSV con:
    python import_vocab.py --csv vocabolario_finale.csv

Poi avvia l'app (funziona anche su desktop per svilupparla/testarla):
    python main.py
"""

import random
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.properties import StringProperty, BooleanProperty
from kivymd.app import MDApp
from kivymd.uix.screen import MDScreen
from kivymd.uix.list import (
    OneLineListItem, TwoLineListItem, TwoLineRightIconListItem, IconRightWidget,
)

from vocab_search import VocabSearcher

# Cartella dove si trova main.py: qui viene bundlato il vocab.db "di fabbrica"
# generato con import_vocab.py, sia sul desktop che dentro l'APK. Qui vive
# anche icon.png, usato sia come icona dell'app (in buildozer.spec) sia come
# logo mostrato nella schermata di avvio.
APP_DIR = Path(__file__).parent
BUNDLED_VOCAB_DB = APP_DIR / "vocab.db"

TODAY = date.today().isoformat()

SEARCH_DEBOUNCE_SECONDS = 0.18
SPLASH_DURATION_SECONDS = 8.0

DE_HINT = "Search a German word..."
EN_HINT = "Search an English word..."

# Entro quante ore dall'ultima interazione una sessione di ripasso viene
# ripresa da dove era rimasta, invece di essere ricostruita da zero.
REVIEW_SESSION_TIMEOUT_HOURS = 6

# Etichette mostrate nel riquadro della traduzione. Il database contiene i
# valori in italiano (Sostantivo, Maschile, ...): qui vengono tradotti in
# inglese per la visualizzazione. Valori sconosciuti vengono mostrati cosi'
# come sono.
TYPE_LABELS = {
    "sostantivo": "Noun",
    "verbo": "Verb",
    "aggettivo": "Adjective",
    "frase": "Phrase",
}
GENDER_LABELS = {
    "maschile": "Masculine",
    "femminile": "Feminine",
    "neutro": "Neuter",
}


def _db_version(path):
    """Legge PRAGMA user_version di un database SQLite (0 se non leggibile).
    import_vocab.py lo imposta ad ogni importazione: serve all'app per capire
    se il vocab.db incluso nell'app e' piu' recente della copia locale."""
    try:
        con = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def format_meta(word_type, gender):
    """Riga discreta sopra la traduzione, es. 'Noun · Masculine'. Salta i
    valori mancanti (NULL, vuoto o la stringa 'None')."""
    parts = []
    for value, labels in ((word_type, TYPE_LABELS), (gender, GENDER_LABELS)):
        text = str(value).strip() if value is not None else ""
        if text and text.lower() != "none":
            parts.append(labels.get(text.lower(), text))
    return " · ".join(parts)


KV = """
ScreenManager:
    SplashScreen:
    SearchScreen:
    DeckScreen:
    ReviewScreen:

<SplashScreen>:
    name: "splash"
    MDBoxLayout:
        orientation: "vertical"
        md_bg_color: 0, 0, 0, 1
        padding: "24dp"
        spacing: "16dp"

        Widget:
            size_hint_y: 0.3

        Image:
            source: "icon.png"
            size_hint: None, None
            size: "250dp", "250dp"
            pos_hint: {"center_x": 0.5}

        MDLabel:
            text: "DigiVol"
            halign: "center"
            theme_text_color: "Custom"
            text_color: 1, 1, 1, 1
            font_style: "H3"
            font_name: "Roboto-Bold"
            bold: True
            size_hint_y: None
            height: self.texture_size[1] + dp(4)

        MDLabel:
            text: "your minimalist digital vocabulary"
            halign: "center"
            theme_text_color: "Custom"
            text_color: 0.6, 0.6, 0.6, 1
            font_style: "Subtitle1"
            font_name: "Roboto"
            font_size: "18sp"
            size_hint_y: None
            height: self.texture_size[1]

        Widget:
            size_hint_y: 0.3

        MDLabel:
            text: "Powered and Developed by Germinale Shinoda"
            halign: "center"
            theme_text_color: "Custom"
            text_color: 0.4, 0.4, 0.4, 1
            font_style: "Caption"
            size_hint_y: None
            height: self.texture_size[1] + dp(8)

<SearchScreen>:
    name: "search"
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "12dp"

        MDTopAppBar:
            title: "DigiVol"
            right_action_items: [["swap-horizontal", lambda x: app.toggle_search_direction()], ["school-outline", lambda x: app.go_to_review()], ["cards-outline", lambda x: app.go_to_deck()]]

        MDTextField:
            id: search_field
            hint_text: app.search_hint_text
            mode: "rectangle"
            on_text: app.on_search_text(self.text)

        ScrollView:
            MDList:
                id: suggestions_list

        MDCard:
            id: translation_card
            orientation: "vertical"
            padding: "12dp"
            spacing: "6dp"
            size_hint_y: None
            height: "170dp"
            radius: [12]
            MDLabel:
                id: meta_label
                text: app.current_meta
                halign: "center"
                theme_text_color: "Secondary"
                font_style: "Caption"
                size_hint_y: None
                height: self.texture_size[1]
            ScrollView:
                do_scroll_x: False
                MDLabel:
                    id: translation_label
                    text: app.current_translation
                    halign: "center"
                    theme_text_color: "Primary"
                    font_style: "H6"
                    size_hint_y: None
                    text_size: self.width, None
                    height: self.texture_size[1]

        MDRaisedButton:
            text: "+ Add flashcard (front + reverse)"
            pos_hint: {"center_x": 0.5}
            disabled: not app.current_word
            on_release: app.add_flashcard()

        MDLabel:
            id: status_label
            text: app.status_text
            halign: "center"
            theme_text_color: "Secondary"
            font_style: "Caption"

<DeckScreen>:
    name: "deck"
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "12dp"

        MDTopAppBar:
            title: "Il tuo mazzo"
            left_action_items: [["arrow-left", lambda x: app.go_to_search()]]

        ScrollView:
            MDList:
                id: deck_list

<ReviewScreen>:
    name: "review"
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "16dp"

        MDTopAppBar:
            title: "Ripasso"
            left_action_items: [["arrow-left", lambda x: app.go_to_search()]]
            right_action_items: [["restart", lambda x: app.restart_review()]]

        MDLabel:
            id: review_status_label
            text: app.review_status_text
            halign: "center"
            theme_text_color: "Secondary"
            font_style: "Caption"

        MDCard:
            orientation: "vertical"
            padding: "20dp"
            size_hint_y: None
            height: "160dp"
            radius: [16]
            pos_hint: {"center_x": 0.5}
            MDLabel:
                id: review_front_label
                text: app.current_card_front
                halign: "center"
                theme_text_color: "Primary"
                font_style: "H5"
            MDLabel:
                id: review_back_label
                text: app.current_card_back if app.answer_revealed else ""
                halign: "center"
                theme_text_color: "Secondary"
                font_style: "H6"

        MDRaisedButton:
            text: "Mostra risposta"
            pos_hint: {"center_x": 0.5}
            disabled: app.answer_revealed or not app.current_card_front
            on_release: app.reveal_answer()

        MDBoxLayout:
            id: grade_buttons
            orientation: "horizontal"
            spacing: "8dp"
            size_hint_y: None
            height: "48dp"
            opacity: 1 if app.answer_revealed else 0
            disabled: not app.answer_revealed

            MDRaisedButton:
                text: "Di nuovo"
                md_bg_color: 0.25, 0.25, 0.25, 1
                on_release: app.grade_card(0)
            MDRaisedButton:
                text: "Difficile"
                md_bg_color: 0.4, 0.4, 0.4, 1
                on_release: app.grade_card(1)
            MDRaisedButton:
                text: "Bene"
                md_bg_color: 0.55, 0.55, 0.55, 1
                on_release: app.grade_card(2)
            MDRaisedButton:
                text: "Facile"
                md_bg_color: 0.75, 0.75, 0.75, 1
                on_release: app.grade_card(3)
"""


class SplashScreen(MDScreen):
    pass


class SearchScreen(MDScreen):
    pass


class DeckScreen(MDScreen):
    pass


class ReviewScreen(MDScreen):
    pass


class FlashcardApp(MDApp):
    current_translation = StringProperty("")
    current_word = StringProperty("")
    current_meta = StringProperty("")  # es. "Sostantivo · Maschile"
    status_text = StringProperty("")

    # Direzione di ricerca: False = tedesco -> inglese, True = inglese -> tedesco
    reversed_search = BooleanProperty(False)
    search_hint_text = StringProperty(DE_HINT)

    # ---------- Stato ripasso ----------
    review_queue = []
    # Id delle carte del lotto con cui e' iniziata la sessione di ripasso
    # corrente. Serve a "restart_review" per rimescolare lo STESSO lotto,
    # invece di ri-filtrare per due_date (che nel frattempo puo' essere
    # gia' stata spostata avanti dalle valutazioni date con grade_card).
    _original_ids = []
    current_card_id = None
    current_card_front = StringProperty("")
    current_card_back = StringProperty("")
    answer_revealed = BooleanProperty(False)
    review_status_text = StringProperty("")

    _search_event = None

    def build(self):
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Gray"
        self.title = "DigiVol"

        # Icona della finestra su desktop (angolo in alto a sinistra e
        # barra delle applicazioni): riusa lo stesso file del logo mostrato
        # nello splash, cosi' l'immagine e' sempre coerente in ogni punto
        # dell'app. Su Android l'icona dell'app (quella quadrata nella
        # schermata home) NON dipende da questa riga: va impostata nel
        # buildozer.spec con "icon.filename = %(source.dir)s/icon.png".
        self.icon = str(APP_DIR / "icon.png")

        # user_data_dir e' una cartella privata e SEMPRE scrivibile fornita da
        # Kivy su ogni piattaforma (desktop, Android, iOS) - qui vivono i
        # database reali dell'utente, a differenza della cartella dell'app
        # che su alcuni dispositivi puo' essere di sola lettura.
        data_dir = Path(self.user_data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.vocab_db_path = data_dir / "vocab.db"
        self.deck_db_path = data_dir / "deck.db"

        self._ensure_vocab_db()

        # Connessioni aperte una sola volta e riutilizzate per tutta la vita
        # dell'app, invece di aprirle/chiuderle ad ogni interazione.
        self.vocab_con = sqlite3.connect(str(self.vocab_db_path), check_same_thread=False)
        self.deck_con = sqlite3.connect(str(self.deck_db_path), check_same_thread=False)

        # Ottimizzazioni di lettura: vocab.db non viene mai scritto a runtime
        # dopo l'indice (e' un dizionario "di sola lettura" per l'app), quindi
        # possiamo usare cache/memory-map per velocizzare ogni ricerca.
        self.vocab_con.execute("PRAGMA temp_store = MEMORY")
        self.vocab_con.execute("PRAGMA cache_size = -20000")  # ~20 MB di cache
        self.vocab_con.execute("PRAGMA mmap_size = 268435456")  # 256 MB memory-map

        # Il dizionario deve avere le colonne di ricerca create da
        # import_vocab.py (word_norm, translation_norm, word_type, gender).
        VocabSearcher.check_schema(self.vocab_con)

        # Indici sulle colonne normalizzate: di norma esistono gia' (li crea
        # import_vocab.py) e questa istruzione non fa nulla.
        self.vocab_con.execute(
            "CREATE INDEX IF NOT EXISTS idx_word_norm ON vocab (word_norm)"
        )
        self.vocab_con.execute(
            "CREATE INDEX IF NOT EXISTS idx_translation_norm ON vocab (translation_norm)"
        )
        self.vocab_con.commit()

        # Da qui in poi vocab_con fa solo letture: lo blocchiamo esplicitamente
        # in sola lettura, un'ulteriore ottimizzazione che SQLite puo' sfruttare.
        self.vocab_con.execute("PRAGMA query_only = ON")

        self.searcher = VocabSearcher(self.vocab_con)

        self._init_deck_db()
        self.root = Builder.load_string(KV)
        self._refresh_deck_count()

        # Mostra la schermata di avvio (logo + nome + slogan) per un tempo
        # fisso, poi passa automaticamente alla ricerca.
        Clock.schedule_once(lambda dt: self.go_to_search(), SPLASH_DURATION_SECONDS)

        # Mentre l'utente guarda la schermata iniziale, prepara in memoria
        # l'elenco usato per correggere i refusi (qualche decina di ms).
        Clock.schedule_once(lambda dt: self.searcher.warm_up(False), 1.0)

        return self.root

    def on_stop(self):
        # Chiude in modo pulito le connessioni persistenti quando l'app termina.
        for con in (getattr(self, "vocab_con", None), getattr(self, "deck_con", None)):
            if con is not None:
                con.close()

    # ---------- Setup database ----------
    def _ensure_vocab_db(self):
        """Copia il vocab.db incluso nell'app nella cartella dati dell'utente
        al primo avvio, e lo RIcopia quando quello incluso e' piu' recente
        (versione piu' alta in PRAGMA user_version, impostata da
        import_vocab.py). Riguarda solo il dizionario: il mazzo (deck.db) e'
        un file separato e non viene mai toccato qui."""
        if not BUNDLED_VOCAB_DB.exists():
            if self.vocab_db_path.exists():
                return  # nessun dizionario incluso: usa la copia locale
            raise FileNotFoundError(
                "vocab.db non trovato. Esegui prima import_vocab.py per creare il "
                "database del vocabolario a partire dal tuo CSV, prima di impacchettare l'app."
            )
        if (not self.vocab_db_path.exists()
                or _db_version(BUNDLED_VOCAB_DB) > _db_version(self.vocab_db_path)):
            shutil.copy(BUNDLED_VOCAB_DB, self.vocab_db_path)

    def _init_deck_db(self):
        # REGOLA PER OGNI FUTURA MODIFICA A QUESTA FUNZIONE: usare SOLO
        # "CREATE TABLE IF NOT EXISTS" e "ALTER TABLE ... ADD COLUMN".
        # Mai DROP TABLE, mai DELETE, mai un CREATE TABLE senza "IF NOT
        # EXISTS" su "deck": deck.db vive nella cartella privata
        # dell'utente (user_data_dir) e sopravvive agli aggiornamenti
        # dell'app SOLO se questa funzione resta puramente additiva.
        # Un aggiornamento che ricreasse la tabella "deck" da zero
        # cancellerebbe il mazzo di ogni utente esistente.
        con = self.deck_con
        con.execute(
            "CREATE TABLE IF NOT EXISTS deck ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "front TEXT NOT NULL, back TEXT NOT NULL)"
        )
        # Migrazione: aggiunge le colonne per la ripetizione spaziata se mancanti
        # (cosi' un deck.db creato con la versione precedente continua a funzionare).
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(deck)")}
        migrations = {
            "due_date": f"ALTER TABLE deck ADD COLUMN due_date TEXT DEFAULT '{TODAY}'",
            "interval_days": "ALTER TABLE deck ADD COLUMN interval_days INTEGER DEFAULT 0",
            "ease_factor": "ALTER TABLE deck ADD COLUMN ease_factor REAL DEFAULT 2.5",
            "repetitions": "ALTER TABLE deck ADD COLUMN repetitions INTEGER DEFAULT 0",
        }
        for col, stmt in migrations.items():
            if col not in existing_cols:
                con.execute(stmt)

        # Indice per controllare velocemente i doppioni (stessa coppia
        # fronte/retro) prima di inserire una nuova flashcard.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_deck_front_back ON deck (front, back)"
        )

        # Tabella a riga singola che tiene lo stato della sessione di
        # ripasso in corso, per poterla riprendere se l'app viene chiusa
        # e riaperta entro REVIEW_SESSION_TIMEOUT_HOURS. original_ids tiene
        # traccia del lotto di partenza della sessione, cosi' "restart" puo'
        # rimescolarlo anche se, nel frattempo, le due_date delle carte sono
        # gia' state aggiornate dalle valutazioni date durante il ripasso.
        con.execute(
            "CREATE TABLE IF NOT EXISTS review_session ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "queue_ids TEXT, "
            "original_ids TEXT, "
            "updated_at TEXT)"
        )
        # Migrazione per chi aveva gia' la tabella review_session senza
        # la colonna original_ids (versione precedente dell'app).
        session_cols = {row[1] for row in con.execute("PRAGMA table_info(review_session)")}
        if "original_ids" not in session_cols:
            con.execute("ALTER TABLE review_session ADD COLUMN original_ids TEXT")
        con.commit()

    # ---------- Switch direzione di ricerca ----------
    def toggle_search_direction(self):
        self.reversed_search = not self.reversed_search
        self.search_hint_text = EN_HINT if self.reversed_search else DE_HINT

        # Ripulisce lo stato della ricerca corrente, cosi' non resta a schermo
        # un risultato nella direzione sbagliata.
        self.current_word = ""
        self.current_translation = ""
        self.current_meta = ""
        search_screen = self.root.get_screen("search")
        search_screen.ids.search_field.text = ""
        search_screen.ids.suggestions_list.clear_widgets()

    # ---------- Ricerca (con debounce) ----------
    def on_search_text(self, text):
        # Annulla la ricerca programmata precedente se l'utente sta ancora
        # digitando: evita una query + ricostruzione lista per ogni lettera.
        if self._search_event is not None:
            self._search_event.cancel()
        self._search_event = Clock.schedule_once(
            lambda dt: self._run_search(text), SEARCH_DEBOUNCE_SECONDS
        )

    def _run_search(self, text):
        suggestions_list = self.root.get_screen("search").ids.suggestions_list
        suggestions_list.clear_widgets()
        self.current_word = ""
        self.current_translation = ""
        self.current_meta = ""

        # groups: lista di (intestazione, righe). L'intestazione c'e' solo
        # quando la ricerca contiene piu' parole che non compaiono insieme in
        # nessuna voce: in quel caso ogni parola ha il suo gruppo.
        groups = self.searcher.search(text, self.reversed_search)

        for header, rows in groups:
            if header:
                suggestions_list.add_widget(OneLineListItem(text=header, disabled=True))
            for _rowid, word, translation, word_type, gender in rows:
                meta = format_meta(word_type, gender)
                if self.reversed_search:
                    # In modalita' invertita il termine cercato (inglese) va in
                    # primo piano nella lista, la parola tedesca come sottotitolo.
                    item = TwoLineListItem(
                        text=translation,
                        secondary_text=word,
                        on_release=lambda inst, w=word, t=translation, m=meta: self.select_word(t, w, m),
                    )
                else:
                    item = TwoLineListItem(
                        text=word,
                        secondary_text=translation,
                        on_release=lambda inst, w=word, t=translation, m=meta: self.select_word(w, t, m),
                    )
                suggestions_list.add_widget(item)

    def select_word(self, word, translation, meta=""):
        self.current_word = word
        self.current_translation = translation
        self.current_meta = meta

    # ---------- Aggiunta flashcard ----------
    def add_flashcard(self):
        if not self.current_word:
            return
        front, back = self.current_word, self.current_translation
        con = self.deck_con

        # Evita i doppioni: se questa esatta coppia fronte/retro esiste
        # gia' nel mazzo, avvisa l'utente invece di duplicarla.
        already_exists = con.execute(
            "SELECT 1 FROM deck WHERE front = ? AND back = ? LIMIT 1", (front, back)
        ).fetchone()
        if already_exists:
            self._flash_status("Already in your deck")
            return

        con.execute(
            "INSERT INTO deck (front, back, due_date, interval_days, ease_factor, repetitions) "
            "VALUES (?, ?, ?, 0, 2.5, 0)",
            (front, back, TODAY),
        )
        con.execute(
            "INSERT INTO deck (front, back, due_date, interval_days, ease_factor, repetitions) "
            "VALUES (?, ?, ?, 0, 2.5, 0)",
            (back, front, TODAY),
        )
        con.commit()
        self._flash_status("Flashcard added")

    def _flash_status(self, message, duration=1.5):
        """Mostra un messaggio temporaneo nella label di stato in basso,
        poi torna al contatore normale del mazzo dopo 'duration' secondi.
        Non dipende da nessun widget extra (Snackbar, Dialog...) la cui API
        cambia da una versione di KivyMD all'altra - solo la label di testo
        che gia' usiamo sempre, quindi e' garantito stabile."""
        self.status_text = message
        Clock.schedule_once(lambda dt: self._refresh_deck_count(), duration)

    def _refresh_deck_count(self):
        count = self.deck_con.execute("SELECT COUNT(*) FROM deck").fetchone()[0]
        self.status_text = f"Local deck: {count} cards"

    # ---------- Navigazione ----------
    def go_to_deck(self):
        self._populate_deck_screen()
        self.root.current = "deck"

    def go_to_search(self):
        self.root.current = "search"

    def go_to_review(self):
        self._load_review_queue()
        self.root.current = "review"
        self._show_next_review_card()

    def _populate_deck_screen(self):
        deck_list = self.root.get_screen("deck").ids.deck_list
        deck_list.clear_widgets()
        rows = self.deck_con.execute("SELECT id, front, back FROM deck ORDER BY id DESC").fetchall()
        for card_id, front, back in rows:
            item = TwoLineRightIconListItem(text=front, secondary_text=back)
            delete_icon = IconRightWidget(icon="trash-can-outline")
            delete_icon.bind(
                on_release=lambda inst, cid=card_id: self.delete_flashcard(cid)
            )
            item.add_widget(delete_icon)
            deck_list.add_widget(item)

    def delete_flashcard(self, card_id):
        self.deck_con.execute("DELETE FROM deck WHERE id = ?", (card_id,))
        self.deck_con.commit()
        self._refresh_deck_count()
        self._populate_deck_screen()

    # ---------- Ripasso a ripetizione spaziata ----------
    def _load_review_queue(self, force_restart=False):
        resumed_ids = None if force_restart else self._load_saved_session()

        if resumed_ids:
            placeholders = ",".join("?" * len(resumed_ids))
            rows = self.deck_con.execute(
                "SELECT id, front, back, interval_days, ease_factor, repetitions "
                f"FROM deck WHERE id IN ({placeholders})",
                resumed_ids,
            ).fetchall()
            rows_by_id = {r[0]: r for r in rows}
            # Mantiene l'ordine (gia' mescolato) della sessione salvata.
            ordered_rows = [rows_by_id[i] for i in resumed_ids if i in rows_by_id]
            # Recupera anche il lotto "originale" della sessione ripresa
            # (salvato su disco), cosi' restart_review puo' rimescolarlo
            # anche dopo che l'app e' stata chiusa e riaperta.
            self._original_ids = self._load_original_ids() or [r[0] for r in ordered_rows]
        else:
            rows = self.deck_con.execute(
                "SELECT id, front, back, interval_days, ease_factor, repetitions "
                "FROM deck WHERE due_date <= ? ORDER BY due_date ASC, id ASC",
                (TODAY,),
            ).fetchall()
            ordered_rows = list(rows)
            random.shuffle(ordered_rows)  # ordine casuale ad ogni nuova sessione
            # Questo e' il lotto "originale" della nuova sessione: restart
            # potra' rimescolarlo anche quando le due_date delle carte
            # cambieranno via via che le valuti.
            self._original_ids = [r[0] for r in ordered_rows]

        self.review_queue = [
            {
                "id": r[0], "front": r[1], "back": r[2],
                "interval_days": r[3], "ease_factor": r[4], "repetitions": r[5],
            }
            for r in ordered_rows
        ]
        self._save_review_session()

    def _load_saved_session(self):
        row = self.deck_con.execute(
            "SELECT queue_ids, updated_at FROM review_session WHERE id = 1"
        ).fetchone()
        if not row or not row[0]:
            return None
        queue_ids_str, updated_at = row
        try:
            last_update = datetime.fromisoformat(updated_at)
        except (TypeError, ValueError):
            return None
        if datetime.now() - last_update > timedelta(hours=REVIEW_SESSION_TIMEOUT_HOURS):
            return None
        ids = [int(x) for x in queue_ids_str.split(",") if x]
        return ids or None

    def _load_original_ids(self):
        """Legge il lotto originale della sessione salvata (id delle carte
        con cui era iniziato il ripasso), indipendentemente dalla coda
        residua corrente. Usato da restart_review per rimescolare lo stesso
        lotto anche dopo la chiusura/riapertura dell'app."""
        row = self.deck_con.execute(
            "SELECT original_ids FROM review_session WHERE id = 1"
        ).fetchone()
        if not row or not row[0]:
            return None
        return [int(x) for x in row[0].split(",") if x]

    def _save_review_session(self):
        ids_str = ",".join(str(card["id"]) for card in self.review_queue)
        original_str = ",".join(str(i) for i in self._original_ids)
        now = datetime.now().isoformat()
        self.deck_con.execute(
            "INSERT INTO review_session (id, queue_ids, original_ids, updated_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET queue_ids = excluded.queue_ids, "
            "original_ids = excluded.original_ids, updated_at = excluded.updated_at",
            (ids_str, original_str, now),
        )
        self.deck_con.commit()

    def _clear_review_session(self):
        self.deck_con.execute("DELETE FROM review_session WHERE id = 1")
        self.deck_con.commit()

    def restart_review(self):
        """Ripasso anticipato: ricarica SEMPRE l'intero mazzo, in un nuovo
        ordine casuale, indipendentemente da:
        - la data di scadenza (due_date) delle singole carte (una carta gia'
          valutata oggi e quindi "spostata" a domani viene comunque inclusa);
        - qualsiasi sessione di ripasso precedente salvata o persa (anche se
          hai chiuso e riaperto l'app, o hai gia' completato il ripasso di
          oggi, funziona sempre allo stesso modo).

        Prima del fix, questa funzione si basava sul lotto di id della
        sessione corrente (self._original_ids), che pero' viveva solo in
        memoria e veniva cancellato dal database non appena il ripasso
        finiva. Risultato: dopo aver chiuso/riaperto l'app o completato una
        sessione, il tasto ripiegava su una query filtrata per due_date, che
        pero' escludeva proprio le carte gia' valutate - facendo sembrare il
        tasto "morto" a meno di aggiungere una carta nuova (l'unica ancora
        con due_date di oggi). Interrogare qui direttamente l'intera tabella
        deck elimina questa dipendenza fragile: il tasto legge sempre lo
        stato reale e attuale del mazzo, non uno stato "ricordato" altrove.

        NOTA per manutenzione futura: questa funzione fa solo SELECT sulla
        tabella deck, mai DELETE o DROP - il mazzo dell'utente non viene mai
        toccato da qui, solo letto.
        """
        rows = self.deck_con.execute(
            "SELECT id, front, back, interval_days, ease_factor, repetitions FROM deck"
        ).fetchall()

        if not rows:
            # Il mazzo e' del tutto vuoto (nessuna carta aggiunta finora).
            self.review_queue = []
            self._original_ids = []
            self._clear_review_session()
            self.answer_revealed = False
            self.current_card_id = None
            self.current_card_front = ""
            self.current_card_back = ""
            self.review_status_text = "Il mazzo e' vuoto: aggiungi qualche flashcard prima."
            return

        ordered_rows = list(rows)
        random.shuffle(ordered_rows)  # nuovo ordine casuale ad ogni ripasso anticipato

        self.review_queue = [
            {
                "id": r[0], "front": r[1], "back": r[2],
                "interval_days": r[3], "ease_factor": r[4], "repetitions": r[5],
            }
            for r in ordered_rows
        ]
        self._original_ids = [r[0] for r in ordered_rows]
        self._save_review_session()
        self._show_next_review_card()

    def _show_next_review_card(self):
        self.answer_revealed = False
        remaining = len(self.review_queue)
        if remaining == 0:
            self.current_card_id = None
            self.current_card_front = ""
            self.current_card_back = ""
            self.review_status_text = "Nessuna carta da ripassare oggi! 🎉"
            self._clear_review_session()
            return

        card = self.review_queue[0]
        self.current_card_id = card["id"]
        self.current_card_front = card["front"]
        self.current_card_back = card["back"]
        self.review_status_text = f"{remaining} carte da ripassare"

    def reveal_answer(self):
        self.answer_revealed = True

    def grade_card(self, grade):
        """grade: 0=Di nuovo, 1=Difficile, 2=Bene, 3=Facile (SM-2 semplificato)."""
        if self.current_card_id is None:
            return

        card = self.review_queue.pop(0)
        ease_factor = card["ease_factor"]
        interval = card["interval_days"]
        repetitions = card["repetitions"]

        if grade == 0:
            repetitions = 0
            interval = 0
            ease_factor = max(1.3, ease_factor - 0.2)
            # la rivedi di nuovo in questa stessa sessione
            card["ease_factor"] = ease_factor
            card["interval_days"] = interval
            card["repetitions"] = repetitions
            self.review_queue.append(card)
        else:
            if repetitions == 0:
                interval = 1
            elif repetitions == 1:
                interval = 6
            else:
                interval = round(interval * ease_factor) or 1
            repetitions += 1
            if grade == 1:
                ease_factor = max(1.3, ease_factor - 0.15)
            elif grade == 3:
                ease_factor = ease_factor + 0.15

        due_date = (date.today() + timedelta(days=interval)).isoformat()

        self.deck_con.execute(
            "UPDATE deck SET due_date=?, interval_days=?, ease_factor=?, repetitions=? WHERE id=?",
            (due_date, interval, ease_factor, repetitions, card["id"]),
        )
        self.deck_con.commit()

        self._save_review_session()
        self._show_next_review_card()


if __name__ == "__main__":
    FlashcardApp().run()
