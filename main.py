"""
Flashcard Quick-Add - versione mobile (Kivy/KivyMD)
----------------------------------------------------
App standalone, senza dipendenze da Anki: vocabolario e mazzo vivono
entrambi in database SQLite locali (nessun server, nessuna connessione
richiesta dopo la prima importazione del vocabolario).

Include ripasso a ripetizione spaziata (algoritmo SM-2 semplificato):
ogni carta ha un intervallo, un fattore di facilita' e una data di
scadenza che si aggiornano in base a come valuti il ripasso.

Prima del primo avvio, importa un vocabolario CSV con:
    python import_vocab.py --csv dizionario.csv --col-word word --col-translation translation

Poi avvia l'app (funziona anche su desktop per svilupparla/testarla):
    python main.py
"""

import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from kivy.lang import Builder
from kivy.properties import StringProperty, BooleanProperty
from kivymd.app import MDApp
from kivymd.uix.screen import MDScreen
from kivymd.uix.list import TwoLineListItem

# Cartella dove si trova main.py: qui viene bundlato il vocab.db "di fabbrica"
# generato con import_vocab.py, sia sul desktop che dentro l'APK.
APP_DIR = Path(__file__).parent
BUNDLED_VOCAB_DB = APP_DIR / "vocab.db"

TODAY = date.today().isoformat()

KV = """
ScreenManager:
    SearchScreen:
    DeckScreen:
    ReviewScreen:

<SearchScreen>:
    name: "search"
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "12dp"

        MDTopAppBar:
            title: "Flashcard Quick-Add"
            right_action_items: [["school-outline", lambda x: app.go_to_review()], ["cards-outline", lambda x: app.go_to_deck()]]

        MDTextField:
            id: search_field
            hint_text: "Cerca vocabolo..."
            mode: "rectangle"
            on_text: app.on_search_text(self.text)

        ScrollView:
            MDList:
                id: suggestions_list

        MDCard:
            id: translation_card
            orientation: "vertical"
            padding: "12dp"
            size_hint_y: None
            height: "90dp"
            radius: [12]
            MDLabel:
                id: translation_label
                text: app.current_translation
                halign: "center"
                theme_text_color: "Primary"
                font_style: "H6"

        MDRaisedButton:
            text: "+ Aggiungi flashcard (diretta + inversa)"
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
                md_bg_color: 0.8, 0.3, 0.3, 1
                on_release: app.grade_card(0)
            MDRaisedButton:
                text: "Difficile"
                md_bg_color: 0.8, 0.6, 0.2, 1
                on_release: app.grade_card(1)
            MDRaisedButton:
                text: "Bene"
                md_bg_color: 0.3, 0.6, 0.3, 1
                on_release: app.grade_card(2)
            MDRaisedButton:
                text: "Facile"
                md_bg_color: 0.2, 0.5, 0.7, 1
                on_release: app.grade_card(3)
"""


class SearchScreen(MDScreen):
    pass


class DeckScreen(MDScreen):
    pass


class ReviewScreen(MDScreen):
    pass


class FlashcardApp(MDApp):
    current_translation = StringProperty("Digita una parola per iniziare")
    current_word = StringProperty("")
    status_text = StringProperty("")

    # ---------- Stato ripasso ----------
    review_queue = []
    current_card_id = None
    current_card_front = StringProperty("")
    current_card_back = StringProperty("")
    answer_revealed = BooleanProperty(False)
    review_status_text = StringProperty("")

    def build(self):
        self.theme_cls.primary_palette = "Teal"
        self.theme_cls.theme_style = "Light"

        # user_data_dir e' una cartella privata e SEMPRE scrivibile fornita da
        # Kivy su ogni piattaforma (desktop, Android, iOS) - qui vivono i
        # database reali dell'utente, a differenza della cartella dell'app
        # che su alcuni dispositivi puo' essere di sola lettura.
        data_dir = Path(self.user_data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.vocab_db_path = data_dir / "vocab.db"
        self.deck_db_path = data_dir / "deck.db"

        self._ensure_vocab_db()
        self._init_deck_db()
        self.root = Builder.load_string(KV)
        self._refresh_deck_count()
        return self.root

    # ---------- Setup database ----------
    def _ensure_vocab_db(self):
        if self.vocab_db_path.exists():
            return
        if not BUNDLED_VOCAB_DB.exists():
            raise FileNotFoundError(
                "vocab.db non trovato. Esegui prima import_vocab.py per creare il "
                "database del vocabolario a partire dal tuo CSV, prima di impacchettare l'app."
            )
        shutil.copy(BUNDLED_VOCAB_DB, self.vocab_db_path)

    def _init_deck_db(self):
        con = sqlite3.connect(self.deck_db_path)
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
        con.commit()
        con.close()

    # ---------- Ricerca ----------
    def on_search_text(self, text):
        suggestions_list = self.root.get_screen("search").ids.suggestions_list
        suggestions_list.clear_widgets()
        self.current_word = ""
        self.current_translation = "Digita una parola per iniziare"

        prefix = text.strip().lower()
        if not prefix:
            return

        con = sqlite3.connect(self.vocab_db_path)
        cur = con.execute(
            "SELECT word, translation FROM vocab WHERE word LIKE ? ORDER BY word LIMIT 15",
            (prefix + "%",),
        )
        rows = cur.fetchall()
        con.close()

        for word, translation in rows:
            item = TwoLineListItem(
                text=word,
                secondary_text=translation,
                on_release=lambda inst, w=word, t=translation: self.select_word(w, t),
            )
            suggestions_list.add_widget(item)

    def select_word(self, word, translation):
        self.current_word = word
        self.current_translation = translation

    # ---------- Aggiunta flashcard ----------
    def add_flashcard(self):
        if not self.current_word:
            return
        front, back = self.current_word, self.current_translation
        con = sqlite3.connect(self.deck_db_path)
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
        con.close()
        self._refresh_deck_count()

    def _refresh_deck_count(self):
        con = sqlite3.connect(self.deck_db_path)
        count = con.execute("SELECT COUNT(*) FROM deck").fetchone()[0]
        con.close()
        self.status_text = f"Mazzo locale: {count} carte"

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
        con = sqlite3.connect(self.deck_db_path)
        rows = con.execute("SELECT front, back FROM deck ORDER BY id DESC").fetchall()
        con.close()
        for front, back in rows:
            deck_list.add_widget(TwoLineListItem(text=front, secondary_text=back))

    # ---------- Ripasso a ripetizione spaziata ----------
    def _load_review_queue(self):
        con = sqlite3.connect(self.deck_db_path)
        rows = con.execute(
            "SELECT id, front, back, interval_days, ease_factor, repetitions "
            "FROM deck WHERE due_date <= ? ORDER BY due_date ASC, id ASC",
            (TODAY,),
        ).fetchall()
        con.close()
        self.review_queue = [
            {
                "id": r[0], "front": r[1], "back": r[2],
                "interval_days": r[3], "ease_factor": r[4], "repetitions": r[5],
            }
            for r in rows
        ]

    def _show_next_review_card(self):
        self.answer_revealed = False
        remaining = len(self.review_queue)
        if remaining == 0:
            self.current_card_id = None
            self.current_card_front = ""
            self.current_card_back = ""
            self.review_status_text = "Nessuna carta da ripassare oggi! 🎉"
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

        con = sqlite3.connect(self.deck_db_path)
        con.execute(
            "UPDATE deck SET due_date=?, interval_days=?, ease_factor=?, repetitions=? WHERE id=?",
            (due_date, interval, ease_factor, repetitions, card["id"]),
        )
        con.commit()
        con.close()

        self._show_next_review_card()


if __name__ == "__main__":
    FlashcardApp().run()
