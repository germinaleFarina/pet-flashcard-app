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

Backup, voci personali e correzioni (icona "backup-restore" in alto):
- L'utente sceglie UNA cartella (su Android con il selettore di cartelle di
  sistema, senza permessi speciali). Li' l'app tiene tre file di testo:
  digivol_deck.txt (il mazzo, con i progressi di ripasso),
  digivol_my_entries.txt (le voci aggiunte dall'utente) e
  digivol_corrections.txt (le correzioni segnalate, da inviare agli
  sviluppatori). I file sopravvivono ad aggiornamenti e reinstallazioni:
  scegliendo di nuovo la cartella, l'app li rilegge e UNISCE il contenuto a
  quello che ha gia' (non cancella e non sovrascrive mai nulla).
- "Report correction": con una traduzione selezionata apre un modulo con i
  suoi campi; la correzione va nel file delle correzioni.
- "Add my entry": aggiunge una voce personale al vocabolario (subito
  ricercabile, anche con i refusi).
- Tutto e' salvato anche dentro l'app: la cartella e' una copia in piu'.
- La cartella viene aperta solo quando serve (mai all'avvio) e ogni accesso e'
  protetto: prima di ogni chiamata ad Android l'app annota su file il passo
  in corso. Se Android chiudesse l'app proprio li', al riavvio il backup
  automatico resta in pausa invece di far richiudere l'app, e il rapporto
  diagnostico (Backup & data) dice quale passo era in corso.

Mazzi per categoria:
- Ogni flashcard sta in un mazzo: "Default" oppure una categoria creata
  dall'utente. Il pulsante "Deck: ..." nella ricerca sceglie dove vanno le
  nuove carte. Nella lista delle carte si puo' filtrare per mazzo e, con la
  modalita' selezione, spostare piu' carte in un colpo solo in un mazzo.
  I mazzi fanno parte del backup. Il ripasso, per ora, include tutti i mazzi.

Prima del primo avvio, genera vocab.db dal CSV con:
    python import_vocab.py --csv vocabolario_finale.csv

Poi avvia l'app (funziona anche su desktop per svilupparla/testarla):
    python main.py
"""

import os
import random
import shutil
import sqlite3
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.properties import StringProperty, BooleanProperty
from kivy.uix.scrollview import ScrollView
from kivy.utils import escape_markup, platform
from kivymd.app import MDApp
from kivymd.uix.screen import MDScreen
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDFlatButton, MDRaisedButton
from kivymd.uix.dialog import MDDialog
from kivymd.uix.label import MDLabel
from kivymd.uix.list import (
    IconLeftWidgetWithoutTouch, IconRightWidget, OneLineListItem,
    TwoLineAvatarIconListItem, TwoLineListItem, TwoLineRightIconListItem,
)

import folder_storage
import userdata
from vocab_search import VocabSearcher

# Cartella dove si trova main.py: qui viene bundlato il vocab.db "di fabbrica"
# generato con import_vocab.py, sia sul desktop che dentro l'APK. Qui vive
# anche icon.png, usato sia come icona dell'app (in buildozer.spec) sia come
# logo mostrato nella schermata di avvio.
APP_DIR = Path(__file__).resolve().parent  # sempre assoluto (su Android __file__ e' relativo)
BUNDLED_VOCAB_DB = APP_DIR / "vocab.db"

TODAY = date.today().isoformat()

SEARCH_DEBOUNCE_SECONDS = 0.18
SPLASH_DURATION_SECONDS = 8.0

DE_HINT = "Search a German word..."
EN_HINT = "Search an English word..."

# Entro quante ore dall'ultima interazione una sessione di ripasso viene
# ripresa da dove era rimasta, invece di essere ricostruita da zero.
REVIEW_SESSION_TIMEOUT_HOURS = 6

# Dopo una modifica al mazzo, attende questi secondi prima di aggiornare il
# file di backup (cosi' piu' modifiche ravvicinate producono una sola scrittura).
AUTOSAVE_DELAY_SECONDS = 3.0

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
    se il vocab.db incluso nell'app e' piu' recente della copia locale.
    Non deve MAI far fallire l'avvio: qualunque problema vale come versione 0.
    (Nessun indirizzo file:// qui: con un percorso relativo, come su Android,
    Path.as_uri() solleva ValueError.)"""
    try:
        con = sqlite3.connect(str(path))
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
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
    FormScreen:
    DataScreen:
    DecksScreen:

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
            right_action_items: [["swap-horizontal", lambda x: app.toggle_search_direction()], ["school-outline", lambda x: app.go_to_review()], ["cards-outline", lambda x: app.go_to_deck()], ["folder-sync-outline", lambda x: app.go_to_data()]]

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

        MDBoxLayout:
            size_hint_y: None
            height: "48dp"
            spacing: "8dp"
            Widget:
            MDRaisedButton:
                text: "+ Add flashcard"
                disabled: not app.current_word
                on_release: app.add_flashcard()
            MDFlatButton:
                text: "Deck: " + app.target_deck_label
                on_release: app.open_decks("target")
            Widget:

        MDBoxLayout:
            size_hint_y: None
            height: "40dp"
            spacing: "8dp"
            Widget:
            MDFlatButton:
                text: "Report correction"
                disabled: not app.current_word
                on_release: app.open_correction_form()
            MDFlatButton:
                text: "Add my entry"
                on_release: app.open_new_entry_form()
            Widget:

        MDLabel:
            id: status_label
            text: app.status_text
            halign: "center"
            theme_text_color: "Secondary"
            font_style: "Caption"

<FormScreen>:
    name: "form"
    MDBoxLayout:
        orientation: "vertical"

        MDTopAppBar:
            title: app.form_title
            left_action_items: [["arrow-left", lambda x: app.cancel_form()]]

        ScrollView:
            do_scroll_x: False
            MDBoxLayout:
                orientation: "vertical"
                adaptive_height: True
                padding: "16dp"
                spacing: "14dp"

                MDTextField:
                    id: f_word
                    hint_text: "German word"
                    mode: "rectangle"

                MDTextField:
                    id: f_translation
                    hint_text: "English translation"
                    mode: "rectangle"

                MDBoxLayout:
                    adaptive_height: True
                    spacing: "8dp"
                    MDRaisedButton:
                        text: "Type: " + app.form_type_label
                        on_release: app.cycle_form_type()
                    MDRaisedButton:
                        text: "Gender: " + app.form_gender_label
                        on_release: app.cycle_form_gender()

                MDBoxLayout:
                    size_hint_y: None
                    height: "64dp" if app.form_mode == "correction" else 0
                    opacity: 1 if app.form_mode == "correction" else 0
                    disabled: app.form_mode != "correction"
                    MDTextField:
                        id: f_note
                        hint_text: "Note for the developers (optional)"
                        mode: "rectangle"

                MDLabel:
                    text: app.form_error
                    theme_text_color: "Error"
                    adaptive_height: True

                MDRaisedButton:
                    text: "Save"
                    pos_hint: {"center_x": 0.5}
                    on_release: app.save_form()

<DataScreen>:
    name: "data"
    MDBoxLayout:
        orientation: "vertical"

        MDTopAppBar:
            title: "Backup & data"
            left_action_items: [["arrow-left", lambda x: app.go_to_search()]]

        ScrollView:
            do_scroll_x: False
            MDBoxLayout:
                orientation: "vertical"
                adaptive_height: True
                padding: "16dp"
                spacing: "14dp"

                MDLabel:
                    text: "Your deck, your own entries and your corrections are saved as plain text files in a folder you choose. They stay there when the app is updated or reinstalled."
                    theme_text_color: "Secondary"
                    adaptive_height: True

                MDLabel:
                    text: app.backup_folder_text
                    adaptive_height: True

                MDRaisedButton:
                    text: "Choose folder"
                    pos_hint: {"center_x": 0.5}
                    on_release: app.choose_backup_folder()

                MDRaisedButton:
                    text: "Back up now"
                    pos_hint: {"center_x": 0.5}
                    disabled: not app.backup_configured
                    on_release: app.backup_now()

                MDRaisedButton:
                    text: "Restore from folder"
                    pos_hint: {"center_x": 0.5}
                    disabled: not app.backup_configured
                    on_release: app.restore_from_folder()

                MDLabel:
                    text: app.backup_status_text
                    theme_text_color: "Secondary"
                    adaptive_height: True

                MDLabel:
                    text: "Diagnostics"
                    font_style: "Subtitle2"
                    adaptive_height: True

                MDLabel:
                    text: app.diagnostics_text[:700] if app.diagnostics_text else "No problems recorded."
                    font_style: "Caption"
                    theme_text_color: "Secondary"
                    adaptive_height: True

                MDBoxLayout:
                    adaptive_height: True
                    spacing: "8dp"
                    MDFlatButton:
                        text: "Copy diagnostic report"
                        on_release: app.copy_diagnostics()
                    MDFlatButton:
                        text: "Clear"
                        on_release: app.clear_diagnostics()

<DeckScreen>:
    name: "deck"
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "8dp"

        MDTopAppBar:
            title: app.deck_view_title
            left_action_items: [["arrow-left", lambda x: app.go_to_search()]]
            right_action_items: [["filter-variant", lambda x: app.open_decks("view")], ["checkbox-multiple-marked-outline", lambda x: app.toggle_select_mode()]]

        MDLabel:
            text: app.deck_message
            halign: "center"
            theme_text_color: "Secondary"
            font_style: "Caption"
            adaptive_height: True

        ScrollView:
            MDList:
                id: deck_list

        MDBoxLayout:
            size_hint_y: None
            height: "48dp" if app.select_mode else 0
            opacity: 1 if app.select_mode else 0
            disabled: not app.select_mode
            spacing: "8dp"
            MDFlatButton:
                text: "Select all"
                on_release: app.select_all_cards()
            MDLabel:
                text: app.selection_text
                halign: "center"
                theme_text_color: "Secondary"
            MDRaisedButton:
                text: "Move to deck"
                on_release: app.open_decks("move")

<DecksScreen>:
    name: "decks"
    MDBoxLayout:
        orientation: "vertical"

        MDTopAppBar:
            title: app.decks_title
            left_action_items: [["arrow-left", lambda x: app.decks_back()]]

        MDBoxLayout:
            size_hint_y: None
            height: "72dp"
            padding: ["16dp", "8dp"]
            spacing: "8dp"
            MDTextField:
                id: new_deck_name
                hint_text: "New deck name"
                mode: "rectangle"
            MDRaisedButton:
                text: "Create"
                pos_hint: {"center_y": 0.5}
                on_release: app.create_deck_from_field()

        MDLabel:
            text: app.decks_message
            halign: "center"
            theme_text_color: "Secondary"
            font_style: "Caption"
            size_hint_y: None
            height: self.texture_size[1] + dp(8)

        ScrollView:
            MDList:
                id: decks_list

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


class FormScreen(MDScreen):
    pass


class DataScreen(MDScreen):
    pass


class DecksScreen(MDScreen):
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

    # ---------- Moduli (correzioni / voci personali) e backup ----------
    form_title = StringProperty("")
    form_mode = StringProperty("entry")          # "entry" oppure "correction"
    form_type_label = StringProperty("none")
    form_gender_label = StringProperty("none")
    form_error = StringProperty("")
    backup_folder_text = StringProperty("")
    backup_status_text = StringProperty("")
    backup_configured = BooleanProperty(False)

    diagnostics_text = StringProperty("")

    # ---------- Mazzi per categoria ----------
    target_deck_label = StringProperty("Default")   # dove vanno le nuove carte
    decks_title = StringProperty("Decks")
    decks_message = StringProperty("")
    decks_mode = StringProperty("target")            # target | move | view
    deck_view_title = StringProperty("Your cards")
    deck_message = StringProperty("")
    select_mode = BooleanProperty(False)
    selection_text = StringProperty("")
    target_deck_id = None          # None = mazzo Default
    _deck_filter = "all"           # "all", None (Default) oppure id categoria
    _selected_card_ids = None
    _visible_card_ids = None
    _select_icons = None
    _dialog = None

    storage = None                 # cartella di backup, aperta solo quando serve
    _storage_spec = None           # specifica salvata della cartella
    _storage_blocked = False       # backup automatico in pausa dopo un crash
    _guard_path = None
    _log_path = None
    _selected_row = None           # riga del vocabolario selezionata (dict)
    _form_original = None
    _form_type_idx = 0
    _form_gender_idx = 0
    _autosave_event = None
    _search_event = None

    def build(self):
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Gray"
        self.title = "DigiVol"
        try:
            return self._build_app()
        except Exception:  # noqa: BLE001 - qualunque errore di avvio
            # Invece di chiudersi, l'app mostra l'errore a schermo.
            return self._startup_error_screen(traceback.format_exc())

    def _startup_error_screen(self, report):
        """Schermata minimale mostrata se l'avvio fallisce: riporta l'errore
        completo e permette di copiarlo, cosi' non serve un cavo e adb."""
        try:
            with open(Path(self.user_data_dir) / "startup_error.txt", "w", encoding="utf-8") as handle:
                handle.write(report)
        except Exception:  # noqa: BLE001
            pass

        def copy_report(*_args):
            try:
                from kivy.core.clipboard import Clipboard  # noqa: WPS433

                Clipboard.copy(report)
            except Exception:  # noqa: BLE001
                pass

        text = ("DigiVol could not start.\nTap \"Copy report\" and paste it into a "
                "message to the developer (or take a screenshot).\n\n" + report[-2500:])
        layout = MDBoxLayout(orientation="vertical", padding="12dp", spacing="8dp")
        scroll = ScrollView()
        scroll.add_widget(MDLabel(text=escape_markup(text), font_style="Caption", adaptive_height=True))
        layout.add_widget(scroll)
        layout.add_widget(MDRaisedButton(text="Copy report", on_release=copy_report,
                                         pos_hint={"center_x": 0.5}))
        return layout

    def _build_app(self):

        # Con la tastiera aperta, sposta lo schermo quanto basta a tenere
        # visibile il campo in cui si sta scrivendo (moduli).
        Window.softinput_mode = "below_target"

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
        self._selected_card_ids = set()
        self._visible_card_ids = []
        self._select_icons = {}
        self._setup_diagnostics(data_dir)
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
        # Prima di bloccare il dizionario in sola lettura: prepara le tabelle
        # dell'utente e (ri)scrive nel dizionario locale le sue voci personali
        # (dopo un aggiornamento del dizionario sarebbero altrimenti sparite
        # dalla ricerca). Questa funzione rimette query_only = ON da sola.
        self._init_deck_db()
        userdata.sync_user_entries_into_vocab(
            self.vocab_con, userdata.list_user_entries(self.deck_con)
        )
        self.vocab_con.execute("PRAGMA query_only = ON")

        self.searcher = VocabSearcher(self.vocab_con)
        self._init_backup_storage()
        self._load_target_deck()
        self.root = Builder.load_string(KV)
        self._refresh_deck_count()

        # Mostra la schermata di avvio (logo + nome + slogan) per un tempo
        # fisso, poi passa automaticamente alla ricerca.
        Clock.schedule_once(lambda dt: self.go_to_search(), SPLASH_DURATION_SECONDS)

        # Mentre l'utente guarda la schermata iniziale, prepara in memoria
        # l'elenco usato per correggere i refusi (qualche decina di ms).
        Clock.schedule_once(lambda dt: self.searcher.warm_up(False), 1.0)

        return self.root

    def on_pause(self):
        # Android manda l'app in secondo piano: ultimo momento sicuro per
        # aggiornare il file di backup. Deve restituire True per restare viva.
        self._autosave_deck()
        return True

    def on_stop(self):
        # Su Android il backup e' gia' stato fatto in on_pause: durante la
        # chiusura non si toccano piu' le API Java.
        if platform != "android":
            self._autosave_deck()
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

        # Voci personali, correzioni e impostazioni (solo CREATE IF NOT EXISTS).
        userdata.init_user_tables(con)

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
        self._selected_row = None

        # groups: lista di (intestazione, righe). L'intestazione c'e' solo
        # quando la ricerca contiene piu' parole che non compaiono insieme in
        # nessuna voce: in quel caso ogni parola ha il suo gruppo.
        groups = self.searcher.search(text, self.reversed_search)

        for header, rows in groups:
            if header:
                suggestions_list.add_widget(OneLineListItem(text=header, disabled=True))
            for row in rows:
                _rowid, word, translation, _word_type, _gender = row
                if self.reversed_search:
                    # In modalita' invertita il termine cercato (inglese) va in
                    # primo piano nella lista, la parola tedesca come sottotitolo.
                    item = TwoLineListItem(
                        text=translation,
                        secondary_text=word,
                        on_release=lambda inst, r=row: self.select_row(r),
                    )
                else:
                    item = TwoLineListItem(
                        text=word,
                        secondary_text=translation,
                        on_release=lambda inst, r=row: self.select_row(r),
                    )
                suggestions_list.add_widget(item)

    def select_row(self, row):
        """Seleziona una voce della lista: riquadro della traduzione e dati
        grezzi (usati dal modulo di correzione)."""
        _rowid, word, translation, word_type, gender = row
        self._selected_row = {
            "word": word, "translation": translation,
            "word_type": word_type, "gender": gender,
        }
        meta = format_meta(word_type, gender)
        if self.reversed_search:
            self.select_word(translation, word, meta)
        else:
            self.select_word(word, translation, meta)

    def select_word(self, word, translation, meta=""):
        self.current_word = word
        self.current_translation = translation
        self.current_meta = meta

    # ---------- Moduli: correzioni e voci personali ----------
    def _form_ids(self):
        return self.root.get_screen("form").ids

    def _open_form(self, mode, title, word="", translation="", word_type=None, gender=None):
        ids = self._form_ids()
        self.form_mode = mode
        self.form_title = title
        self.form_error = ""
        ids.f_word.text = word or ""
        ids.f_translation.text = translation or ""
        ids.f_note.text = ""
        self._form_type_idx = userdata.TYPE_VALUES.index(userdata.canon_meta(word_type) or "")
        self._form_gender_idx = userdata.GENDER_VALUES.index(userdata.canon_meta(gender) or "")
        self._refresh_form_labels()
        self.root.current = "form"

    def _refresh_form_labels(self):
        type_value = userdata.TYPE_VALUES[self._form_type_idx]
        gender_value = userdata.GENDER_VALUES[self._form_gender_idx]
        self.form_type_label = TYPE_LABELS.get(type_value.lower(), "none")
        self.form_gender_label = GENDER_LABELS.get(gender_value.lower(), "none")

    def cycle_form_type(self):
        self._form_type_idx = (self._form_type_idx + 1) % len(userdata.TYPE_VALUES)
        self._refresh_form_labels()

    def cycle_form_gender(self):
        self._form_gender_idx = (self._form_gender_idx + 1) % len(userdata.GENDER_VALUES)
        self._refresh_form_labels()

    def open_correction_form(self):
        row = self._selected_row
        if not row:
            return
        self._form_original = dict(row)
        self._open_form(
            "correction", "Report a correction",
            row["word"], row["translation"], row["word_type"], row["gender"],
        )

    def open_new_entry_form(self):
        # Se l'utente aveva digitato qualcosa nella ricerca, lo usa come
        # punto di partenza (parola tedesca, o traduzione in modalita' EN).
        typed = self.root.get_screen("search").ids.search_field.text.strip()
        word, translation = ("", typed) if self.reversed_search else (typed, "")
        self._open_form("entry", "Add my own entry", word, translation)

    def cancel_form(self):
        self.root.current = "search"

    def save_form(self):
        ids = self._form_ids()
        word = userdata.clean_text(ids.f_word.text, userdata.MAX_WORD_LENGTH)
        translation = userdata.clean_text(ids.f_translation.text, userdata.MAX_TRANSLATION_LENGTH)
        note = userdata.clean_text(ids.f_note.text, userdata.MAX_NOTE_LENGTH)
        word_type = userdata.TYPE_VALUES[self._form_type_idx] or None
        gender = userdata.GENDER_VALUES[self._form_gender_idx] or None

        if not word or not translation:
            self.form_error = "Please fill in both the German word and its English translation."
            return

        try:
            if self.form_mode == "correction":
                corrected = {
                    "word": word, "translation": translation,
                    "word_type": word_type, "gender": gender,
                }
                if userdata.same_entry(self._form_original, corrected) and not note:
                    self.form_error = "Nothing was changed. Edit a field or write a note."
                    return
                vocab_version = self.vocab_con.execute("PRAGMA user_version").fetchone()[0]
                userdata.add_correction(
                    self.deck_con, self._form_original, corrected, note, vocab_version
                )
                message = "Correction saved"
            else:
                if userdata.add_user_entry(self.deck_con, word, translation, word_type, gender) is None:
                    self.form_error = "You have already added this entry."
                    return
                self._sync_user_entries()
                message = "Entry added"
        except Exception as exc:  # noqa: BLE001 - mai chiudere l'app per un salvataggio
            self.form_error = f"Could not save: {exc}"
            return

        in_folder = self._backup_extras_quietly()
        self.root.current = "search"
        if self.form_mode == "entry":
            # Mostra subito la nuova voce nei risultati.
            self.root.get_screen("search").ids.search_field.text = (
                translation if self.reversed_search else word
            )
        self._flash_status(
            message + (" and saved to your backup folder" if in_folder
                       else ". Backup is paused: see the folder icon at the top"
                       if self._storage_blocked and self._storage_spec
                       else ". Choose a backup folder (folder icon at the top) to also get the text file"),
            duration=4,
        )

    def _sync_user_entries(self):
        userdata.sync_user_entries_into_vocab(
            self.vocab_con, userdata.list_user_entries(self.deck_con)
        )
        self.searcher.reset()

    # ---------- Diagnostica e protezione dai crash ----------
    def _setup_diagnostics(self, data_dir):
        """Percorsi dei file di diagnostica. Nessun gestore di segnali di
        sistema viene installato: su Android potrebbe interferire con la
        macchina virtuale Java."""
        self._guard_path = data_dir / "storage_guard.txt"
        self._log_path = data_dir / "backup_log.txt"
        self._refresh_diagnostics()

    def _read_tail(self, path, size):
        try:
            if path is not None and path.exists():
                return path.read_text(encoding="utf-8", errors="replace")[-size:].strip()
        except OSError:
            pass
        return ""

    def _refresh_diagnostics(self):
        self.diagnostics_text = self._read_tail(self._log_path, 1500)

    def _log_text(self, message):
        try:
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
            if self._log_path.stat().st_size > 60000:  # tiene solo la parte finale
                tail = self._log_path.read_text(encoding="utf-8", errors="replace")[-30000:]
                self._log_path.write_text(tail, encoding="utf-8")
        except OSError:
            pass
        self._refresh_diagnostics()

    def _log_error(self, where, exc):
        frames = "".join(traceback.format_exc().splitlines(keepends=True)[-6:])
        self._log_text(f"{where}: {exc!r}\n{frames}")

    def _diagnostics_report(self):
        vocab_version = ""
        try:
            vocab_version = self.vocab_con.execute("PRAGMA user_version").fetchone()[0]
        except Exception:  # noqa: BLE001
            pass
        header = (
            f"DigiVol diagnostic report\nplatform: {platform}\n"
            f"python: {sys.version.split()[0]}\nvocab version: {vocab_version}\n"
            f"backup folder: {folder_storage.describe_spec(self._storage_spec) or 'none'}\n"
            f"automatic backup paused: {self._storage_blocked}\n"
        )
        return header + "\n" + self._read_tail(self._log_path, 8000)

    def copy_diagnostics(self):
        try:
            from kivy.core.clipboard import Clipboard  # noqa: WPS433

            Clipboard.copy(self._diagnostics_report())
            self.backup_status_text = "Report copied: paste it into a message to send it."
        except Exception as exc:  # noqa: BLE001
            self.backup_status_text = f"Could not copy the report: {exc}"

    def clear_diagnostics(self):
        try:
            if self._log_path is not None and self._log_path.exists():
                self._log_path.unlink()
        except OSError:
            pass
        self._refresh_diagnostics()
        self.backup_status_text = "Diagnostics cleared."

    def _guard_enter(self, label):
        """Scrive un segnaposto PRIMA di toccare la cartella e lo toglie
        DOPO. Se Android chiude l'app a meta', il segnaposto resta e al
        riavvio il backup automatico viene messo in pausa."""
        try:
            with open(self._guard_path, "w", encoding="utf-8") as handle:
                handle.write(f"{label} at {datetime.now().isoformat(timespec='seconds')}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass

    def _guard_exit(self):
        try:
            self._guard_path.unlink()
        except OSError:
            pass

    # ---------- Backup su cartella scelta dall'utente ----------
    def _init_backup_storage(self):
        # Nessun accesso ad Android qui: la cartella si apre solo quando serve.
        self.storage = None
        self._storage_spec = userdata.get_setting(self.deck_con, "backup_location")
        self._storage_blocked = userdata.get_setting(self.deck_con, "storage_blocked") == "1"
        if self._guard_path is not None and self._guard_path.exists():
            label = self._read_tail(self._guard_path, 200) or "an unknown step"
            self._storage_blocked = True
            userdata.set_setting(self.deck_con, "storage_blocked", "1")
            self._log_text(f"The app was closed while using the backup folder. Last step: {label}")
            self._guard_exit()
        if self._storage_blocked and self._storage_spec:
            self.backup_status_text = (
                "Automatic backup is paused: the app closed unexpectedly the last time "
                "it used the backup folder. Tap Back up now to try again."
            )
        self._update_backup_texts()

    def _update_backup_texts(self):
        self.backup_configured = bool(self._storage_spec)
        if not self._storage_spec:
            self.backup_folder_text = (
                "No folder chosen yet. Until you choose one, everything is stored "
                "only inside the app."
            )
        else:
            self.backup_folder_text = (
                f"Folder: {folder_storage.describe_spec(self._storage_spec)}"
            )

    def _open_storage(self):
        if self.storage is None:
            self.storage = folder_storage.open_storage(
                self._storage_spec, trace=self._guard_enter
            )
        return self.storage

    def _storage_op(self, label, func, manual=False):
        """Esegue func(storage) con la protezione anti-crash. manual=True per
        le azioni volute dall'utente (anche a backup in pausa: se riescono, il
        backup automatico riparte). Le eccezioni risalgono a chi chiama."""
        if not self._storage_spec:
            return None
        if self._storage_blocked and not manual:
            return None
        self._guard_enter(label)
        try:
            result = func(self._open_storage())
        finally:
            self._guard_exit()
        if manual and self._storage_blocked:
            self._storage_blocked = False
            userdata.set_setting(self.deck_con, "storage_blocked", "0")
        return result

    def go_to_data(self):
        self._update_backup_texts()
        self.root.current = "data"

    def choose_backup_folder(self):
        if platform == "android":
            try:
                self._guard_enter("open the folder picker")
                try:
                    folder_storage.pick_folder_android(self._on_folder_picked)
                finally:
                    self._guard_exit()
            except Exception as exc:  # noqa: BLE001
                self._log_error("folder picker", exc)
                self.backup_status_text = f"Could not open the folder picker: {exc}"
        else:
            # Sul desktop (solo per provare l'app) si usa la cartella ~/DigiVol.
            self._activate_storage("dir:" + str(Path.home() / "DigiVol"))

    @mainthread
    def _on_folder_picked(self, uri_text):
        # Chiamata dal thread di Android: @mainthread rientra in quello di Kivy.
        if uri_text:
            self._activate_storage("android:" + uri_text)
        else:
            self.backup_status_text = "No folder selected."

    def _activate_storage(self, spec):
        self._storage_spec = spec
        self.storage = None
        userdata.set_setting(self.deck_con, "backup_location", spec)
        self._update_backup_texts()
        # Prima legge quello che la cartella gia' contiene (utile dopo una
        # reinstallazione), poi scrive i file aggiornati.
        restored = self._do_restore()
        saved = self._do_backup()
        self._load_target_deck()
        self.backup_status_text = f"{restored}\n{saved}"

    def _stamp(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _do_restore(self):
        try:
            rep = self._storage_op(
                "restore", lambda st: userdata.restore_all(self.deck_con, st), manual=True
            )
        except Exception as exc:  # noqa: BLE001
            self._log_error("restore", exc)
            return f"Restore failed: {exc}"
        if rep is None:
            return "No backup folder chosen."
        if rep["entries_added"]:
            self._sync_user_entries()
        self._refresh_deck_count()
        parts = []
        for key, label in (("cards_added", "cards"), ("entries_added", "own entries"),
                           ("corrections_added", "corrections")):
            if rep[key]:
                parts.append(f"{rep[key]} {label}")
        if parts:
            text = "Restored from the folder: " + ", ".join(parts) + "."
        elif rep["found"]:
            text = "Nothing new to restore: the app already has everything in the folder."
        else:
            text = "No DigiVol files found in the folder yet."
        if rep["bad_lines"]:
            text += f" ({rep['bad_lines']} unreadable lines skipped)"
        if rep["problems"]:
            text += " " + "; ".join(rep["problems"])
        return text

    def _do_backup(self):
        try:
            rep = self._storage_op(
                "backup", lambda st: userdata.backup_all(self.deck_con, st), manual=True
            )
        except Exception as exc:  # noqa: BLE001
            self._log_error("backup", exc)
            return f"Backup failed: {exc}"
        if rep is None:
            return "No backup folder chosen."
        if rep["entries_added"]:
            self._sync_user_entries()
        text = f"Backed up at {self._stamp()}."
        if rep["deck"] == "skipped_empty":
            text = ("Your deck is empty but the folder holds a saved deck, so it was not "
                    "overwritten. Tap Restore.")
        if rep["problems"]:
            text += " " + "; ".join(rep["problems"])
        return text

    def backup_now(self):
        if self._storage_spec:
            self.backup_status_text = self._do_backup()

    def restore_from_folder(self):
        if self._storage_spec:
            self.backup_status_text = self._do_restore()
            self._load_target_deck()

    def _backup_extras_quietly(self):
        """Dopo una correzione o una voce nuova: aggiorna i relativi file nella
        cartella (se scelta e se il backup non e' in pausa). True se scritti."""
        try:
            rep = self._storage_op(
                "save corrections and entries",
                lambda st: userdata.backup_extras(self.deck_con, st),
            )
        except Exception as exc:  # noqa: BLE001
            self._log_error("save corrections and entries", exc)
            self.backup_status_text = f"Could not write to the backup folder: {exc}"
            return False
        if rep is None:
            return False
        if rep["entries_added"]:
            self._sync_user_entries()
        return not rep["problems"]

    def _schedule_autosave(self):
        if not self._storage_spec or self._storage_blocked:
            return
        if self._autosave_event is not None:
            self._autosave_event.cancel()
        self._autosave_event = Clock.schedule_once(
            lambda dt: self._autosave_deck(), AUTOSAVE_DELAY_SECONDS
        )

    def _autosave_deck(self):
        if self._autosave_event is not None:
            self._autosave_event.cancel()
            self._autosave_event = None
        if (not self._storage_spec or self._storage_blocked
                or getattr(self, "deck_con", None) is None):
            return
        try:
            result = self._storage_op(
                "automatic deck backup", lambda st: userdata.backup_deck(self.deck_con, st)
            )
            if result == "written":
                self.backup_status_text = f"Deck saved to your folder at {self._stamp()}."
        except Exception as exc:  # noqa: BLE001
            self._log_error("automatic backup", exc)
            self.backup_status_text = f"Automatic backup failed: {exc}"

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

        # Le due carte (fronte->retro e retro->fronte) vanno nel mazzo scelto
        # con il pulsante "Deck: ..." (Default se non e' stato scelto nulla).
        for card_front, card_back in ((front, back), (back, front)):
            con.execute(
                "INSERT INTO deck (front, back, due_date, interval_days, ease_factor, "
                "repetitions, category_id) VALUES (?, ?, ?, 0, 2.5, 0, ?)",
                (card_front, card_back, TODAY, self.target_deck_id),
            )
        con.commit()
        self._schedule_autosave()
        where = "" if self.target_deck_id is None else (
            f" to {userdata.deck_name(con, self.target_deck_id)}"
        )
        self._flash_status(f"Flashcard added{where} (front + reverse)", duration=2.5)

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
        # Ingresso dalla schermata di ricerca: parte sempre da "tutte le carte".
        self._deck_filter = "all"
        self.select_mode = False
        self._selected_card_ids = set()
        self.selection_text = ""
        self.deck_message = ""
        self._show_deck_screen()

    def _show_deck_screen(self):
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
        flt = self._deck_filter
        sql = ("SELECT d.id, d.front, d.back, c.name FROM deck d "
               "LEFT JOIN categories c ON d.category_id = c.id")
        params = ()
        if flt is None:
            sql += " WHERE d.category_id IS NULL"
        elif flt != "all":
            sql += " WHERE d.category_id = ?"
            params = (flt,)
        rows = self.deck_con.execute(sql + " ORDER BY d.id DESC", params).fetchall()

        self.deck_view_title = (
            "Your cards" if flt == "all"
            else f"Cards: {userdata.deck_name(self.deck_con, flt)}"
        )
        self._visible_card_ids = [r[0] for r in rows]
        self._select_icons = {}
        for card_id, front, back, card_deck in rows:
            # Nella vista "tutte le carte" indica il mazzo delle carte categorizzate.
            secondary = back if (card_deck is None or flt != "all") else f"{back}  ·  {card_deck}"
            if self.select_mode:
                item = TwoLineAvatarIconListItem(
                    text=front, secondary_text=secondary,
                    on_release=lambda inst, cid=card_id: self.toggle_card_selected(cid),
                )
                icon = IconLeftWidgetWithoutTouch(
                    icon="checkbox-marked" if card_id in self._selected_card_ids
                    else "checkbox-blank-outline"
                )
                item.add_widget(icon)
                self._select_icons[card_id] = icon
            else:
                item = TwoLineRightIconListItem(text=front, secondary_text=secondary)
                delete_icon = IconRightWidget(icon="trash-can-outline")
                delete_icon.bind(
                    on_release=lambda inst, cid=card_id: self.delete_flashcard(cid)
                )
                item.add_widget(delete_icon)
            deck_list.add_widget(item)

    def delete_flashcard(self, card_id):
        self.deck_con.execute("DELETE FROM deck WHERE id = ?", (card_id,))
        self.deck_con.commit()
        self._selected_card_ids.discard(card_id)
        self._schedule_autosave()
        self._refresh_deck_count()
        self._populate_deck_screen()

    # ---------- Selezione multipla di carte ----------
    def _update_selection_text(self):
        count = len(self._selected_card_ids)
        self.selection_text = f"{count} selected" if count else "Tap cards to select"

    def toggle_select_mode(self):
        self.select_mode = not self.select_mode
        self._selected_card_ids = set()
        self.deck_message = ""
        if self.select_mode:
            self._update_selection_text()
        else:
            self.selection_text = ""
        self._populate_deck_screen()

    def toggle_card_selected(self, card_id):
        if card_id in self._selected_card_ids:
            self._selected_card_ids.discard(card_id)
        else:
            self._selected_card_ids.add(card_id)
        icon = self._select_icons.get(card_id)
        if icon is not None:
            icon.icon = ("checkbox-marked" if card_id in self._selected_card_ids
                         else "checkbox-blank-outline")
        self._update_selection_text()

    def select_all_cards(self):
        """Seleziona tutte le carte mostrate; se lo sono gia' tutte, le deseleziona."""
        visible = set(self._visible_card_ids)
        if visible and visible <= self._selected_card_ids:
            self._selected_card_ids -= visible
        else:
            self._selected_card_ids |= visible
        for card_id, icon in self._select_icons.items():
            icon.icon = ("checkbox-marked" if card_id in self._selected_card_ids
                         else "checkbox-blank-outline")
        self._update_selection_text()

    # ---------- Mazzi per categoria ----------
    def _refresh_target_label(self):
        name = userdata.deck_name(self.deck_con, self.target_deck_id)
        self.target_deck_label = name if len(name) <= 14 else name[:13] + "…"

    def _load_target_deck(self):
        raw = userdata.get_setting(self.deck_con, "target_deck")
        self.target_deck_id = None
        if raw and raw.isdigit() and userdata.deck_exists(self.deck_con, int(raw)):
            self.target_deck_id = int(raw)
        self._refresh_target_label()

    def _set_target_deck(self, deck_id):
        self.target_deck_id = deck_id
        userdata.set_setting(self.deck_con, "target_deck", "" if deck_id is None else str(deck_id))
        self._refresh_target_label()

    def open_decks(self, mode):
        """mode: 'target' = dove vanno le nuove carte, 'move' = dove spostare
        le carte selezionate, 'view' = quali carte mostrare."""
        if mode == "move" and not self._selected_card_ids:
            self.selection_text = "Select at least one card first"
            return
        self.decks_mode = mode
        self.decks_title = {
            "target": "Add new cards to…",
            "move": f"Move {len(self._selected_card_ids)} cards to…",
            "view": "Show cards from…",
        }[mode]
        self.decks_message = ""
        self._populate_decks_screen()
        self.root.current = "decks"

    def _populate_decks_screen(self):
        decks_list = self.root.get_screen("decks").ids.decks_list
        decks_list.clear_widgets()
        decks = userdata.list_decks(self.deck_con)
        entries = []
        if self.decks_mode == "view":
            entries.append(("all", "All cards", sum(d["count"] for d in decks)))
        entries += [(d["id"], d["name"], d["count"]) for d in decks]
        for deck_id, name, count in entries:
            secondary = f"{count} card{'' if count == 1 else 's'}"
            if self.decks_mode == "target" and deck_id == self.target_deck_id:
                secondary += " · new cards go here"
            elif self.decks_mode == "view" and deck_id == self._deck_filter:
                secondary += " · showing"
            if isinstance(deck_id, int):      # categorie: eliminabili. Default e "All" no.
                item = TwoLineRightIconListItem(
                    text=name, secondary_text=secondary,
                    on_release=lambda inst, i=deck_id: self.deck_chosen(i),
                )
                trash = IconRightWidget(icon="trash-can-outline")
                trash.bind(on_release=lambda inst, i=deck_id, n=name: self.confirm_delete_deck(i, n))
                item.add_widget(trash)
            else:
                item = TwoLineListItem(
                    text=name, secondary_text=secondary,
                    on_release=lambda inst, i=deck_id: self.deck_chosen(i),
                )
            decks_list.add_widget(item)

    def deck_chosen(self, deck_id):
        name = "All cards" if deck_id == "all" else userdata.deck_name(self.deck_con, deck_id)
        if self.decks_mode == "target":
            self._set_target_deck(deck_id)
            self.root.current = "search"
            self._flash_status(f"New cards will go to: {name}", duration=2.5)
        elif self.decks_mode == "move":
            moved = userdata.move_cards(self.deck_con, self._selected_card_ids, deck_id)
            self.select_mode = False
            self._selected_card_ids = set()
            self.selection_text = ""
            self.deck_message = f"Moved {moved} card{'' if moved == 1 else 's'} to {name}"
            self._schedule_autosave()
            self._show_deck_screen()
        else:  # view
            self._deck_filter = deck_id
            self.deck_message = ""
            self._show_deck_screen()

    def decks_back(self):
        if self.decks_mode == "target":
            self.root.current = "search"
        else:
            self._show_deck_screen()

    def create_deck_from_field(self):
        field = self.root.get_screen("decks").ids.new_deck_name
        try:
            new_id = userdata.create_deck(self.deck_con, field.text)
        except ValueError as exc:
            self.decks_message = str(exc)
            return
        name = userdata.deck_name(self.deck_con, new_id)
        field.text = ""
        self.decks_message = f'Deck "{name}" created'
        if self.decks_mode == "target":      # probabilmente lo si vuole usare subito
            self._set_target_deck(new_id)
            self.decks_message += " · new cards will go here"
        self._schedule_autosave()
        self._populate_decks_screen()

    def confirm_delete_deck(self, deck_id, name):
        count = next((d["count"] for d in userdata.list_decks(self.deck_con)
                      if d["id"] == deck_id), 0)
        self._dialog = MDDialog(
            title="Delete this deck?",
            text=(f'"{name}" will be deleted. Its {count} card{"" if count == 1 else "s"} '
                  "are not lost: they move to the Default deck."),
            buttons=[
                MDFlatButton(text="CANCEL", on_release=lambda *a: self._dialog.dismiss()),
                MDRaisedButton(
                    text="DELETE",
                    on_release=lambda *a: self._delete_deck_confirmed(deck_id, name),
                ),
            ],
        )
        self._dialog.open()

    def _delete_deck_confirmed(self, deck_id, name):
        if self._dialog is not None:
            self._dialog.dismiss()
        moved = userdata.delete_deck(self.deck_con, deck_id)
        if self.target_deck_id == deck_id:
            self._set_target_deck(None)
        if self._deck_filter == deck_id:
            self._deck_filter = "all"
        self.decks_message = f'Deck "{name}" deleted · {moved} card{"" if moved == 1 else "s"} moved to Default'
        self._schedule_autosave()
        self._populate_decks_screen()

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
        self._schedule_autosave()

        self._save_review_session()
        self._show_next_review_card()


if __name__ == "__main__":
    FlashcardApp().run()
