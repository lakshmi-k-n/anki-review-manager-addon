# myreview/__init__.py

from __future__ import annotations

import datetime
from typing import Optional, List, Sequence, Callable

from aqt import mw
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QAction,
    QComboBox, QScrollArea, Qt, QWidget,
    QDateEdit, QCheckBox, QDate, QColor, QBrush,
    QAbstractItemView,
)
from aqt.utils import showInfo, qconnect, tooltip
from aqt.operations import QueryOp
from aqt.editor import Editor
from anki.notes import Note, NoteId
from anki.cards import Card, CardId
from anki.decks import DeckId
from anki.collection import Collection

_review_window: Optional["ReviewSlideshow"] = None
_search_dialog: Optional["BulkReviewDialog"] = None


def _qdate_to_ts_start(qdate: QDate) -> float:
    py_date = qdate.toPyDate()
    return datetime.datetime.combine(py_date, datetime.time.min).timestamp()


def _qdate_to_ts_end(qdate: QDate) -> float:
    py_date = qdate.toPyDate()
    return datetime.datetime.combine(py_date, datetime.time.max).timestamp()


def _ts_to_str(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def _card_status(card: Card) -> str:
    if card.queue == -1:
        return "Suspended"
    if card.queue == 0:
        return "New"
    if card.queue == 1:
        return "Learning"
    if card.queue == 2:
        return "Review"
    if card.queue == 3:
        return "Relearning"
    if card.queue == 4:
        return "Preview"
    return f"Unknown ({card.queue})"


def _card_added_ts(card: Card) -> float:
    try:
        return card.id / 1000.0
    except Exception:
        return 0.0


class BulkReviewDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bulk Review Manager")
        self.resize(1000, 800)

        self.notes: List[Note] = []
        self.cards: List[Card] = []
        self._build_ui()
        self._load_decks()
        self.start_btn.setEnabled(False)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        deck_layout = QHBoxLayout()
        deck_layout.addWidget(QLabel("Deck:"))
        self.deck_combo = QComboBox()
        self.deck_combo.setMinimumWidth(250)
        qconnect(self.deck_combo.currentIndexChanged, self._on_deck_changed)
        deck_layout.addWidget(self.deck_combo)

        deck_layout.addStretch()
        layout.addLayout(deck_layout)

        date_layout = QHBoxLayout()
        self.date_filter_enabled = QCheckBox("Filter by date")
        qconnect(self.date_filter_enabled.toggled, self._on_date_filter_toggled)
        date_layout.addWidget(self.date_filter_enabled)
        date_layout.addWidget(QLabel("Field:"))
        self.date_field_combo = QComboBox()
        self.date_field_combo.addItems(["added", "edited"])
        date_layout.addWidget(self.date_field_combo)
        date_layout.addWidget(QLabel("From:"))
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(QDate.currentDate().addMonths(-1))
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(self.date_from)

        date_layout.addWidget(QLabel("To:"))
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDate(QDate.currentDate())
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(self.date_to)
        date_layout.addStretch()
        layout.addLayout(date_layout)
        self._on_date_filter_toggled(False)

        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("Status:"))
        self.status_filter_combo = QComboBox()
        self.status_filter_combo.addItems([
            "(All)", "Suspended", "New", "Learning", "Review", "Relearning",
        ])
        qconnect(self.status_filter_combo.currentIndexChanged, self._run_search)
        status_layout.addWidget(self.status_filter_combo)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Additional search (e.g., tag:vocab, 'word', is:suspended)"
        )
        self.search_btn = QPushButton("Search")
        qconnect(self.search_btn.clicked, self._run_search)
        qconnect(self.search_input.returnPressed, self._run_search)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_btn)
        layout.addLayout(search_layout)

        self.status_label = QLabel("Pick a deck and click Search.")
        layout.addWidget(self.status_label)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list_widget.setAlternatingRowColors(True)
        qconnect(self.list_widget.itemChanged, self._on_item_changed)
        layout.addWidget(self.list_widget)

        self.start_btn = QPushButton("Start Review")
        qconnect(self.start_btn.clicked, self._start_review)
        layout.addWidget(self.start_btn)

    def _load_decks(self) -> None:
        self.deck_combo.blockSignals(True)
        self.deck_combo.clear()
        self.deck_combo.addItem("(All decks)", None)

        deck_names: dict[str, int] = {}
        for deck in mw.col.decks.all():
            full_name: str = mw.col.decks.name(DeckId(deck["id"]))
            deck_names[full_name] = deck["id"]

        for full_name in sorted(deck_names.keys(), key=str.lower):
            self.deck_combo.addItem(full_name, deck_names[full_name])

        self.deck_combo.blockSignals(False)

    def _on_deck_changed(self, _index: int) -> None:
        if self.deck_combo.count() > 0:
            self._run_search()

    def _on_date_filter_toggled(self, enabled: bool) -> None:
        self.date_field_combo.setEnabled(enabled)
        self.date_from.setEnabled(enabled)
        self.date_to.setEnabled(enabled)

    def _build_query(self) -> str:
        parts: List[str] = []

        deck_id: Optional[int] = self.deck_combo.currentData()
        if deck_id is not None:
            deck_name: str = mw.col.decks.name(DeckId(deck_id))
            parts.append(f'deck:"{deck_name}"')

        text: str = self.search_input.text().strip()
        if text:
            parts.append(text)

        return " ".join(parts)

    def _status_matches(self, status_text: str, filter_choice: str) -> bool:
        if filter_choice == "(All)":
            return True
        return filter_choice in status_text

    def _run_search(self) -> None:
        query: str = self._build_query()
        self.status_label.setText("Searching...")

        date_filter_on: bool = self.date_filter_enabled.isChecked()
        date_field: str = self.date_field_combo.currentText()
        from_ts: float = _qdate_to_ts_start(self.date_from.date())
        to_ts: float = _qdate_to_ts_end(self.date_to.date())
        status_choice: str = self.status_filter_combo.currentText()

        def search_op(col: Collection) -> Sequence[NoteId]:
            try:
                note_ids = col.find_notes(query)
            except Exception:
                return []

            filtered: List[NoteId] = []
            for nid in note_ids:
                if len(filtered) >= 500:
                    break
                try:
                    note = col.get_note(NoteId(nid))
                except Exception:
                    continue

                cards = note.cards()
                if not cards:
                    continue

                if status_choice != "(All)":
                    if not self._status_matches(_card_status(cards[0]), status_choice):
                        continue

                if date_filter_on:
                    if date_field == "edited":
                        ts: float = float(note.mod)
                    else:
                        ts = _card_added_ts(cards[0])
                    if not (from_ts <= ts <= to_ts):
                        continue

                filtered.append(NoteId(nid))

            return filtered

        def on_success(note_ids: Sequence[NoteId]) -> None:
            self._populate_list(note_ids)

        QueryOp(
            parent=self,
            op=search_op,
            success=on_success,
        ).with_progress("Searching...").run_in_background()

    def _populate_list(self, note_ids: Sequence[NoteId]) -> None:
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.notes = []
        self.cards = []

        for nid in note_ids:
            note: Note = mw.col.get_note(NoteId(nid))
            cards: List[Card] = note.cards()
            if not cards:
                continue

            card: Card = cards[0]
            self.notes.append(note)
            self.cards.append(card)

            preview: str = note.fields[0][:50] if note.fields else "(empty)"
            status: str = _card_status(card)
            added: str = _ts_to_str(_card_added_ts(card))
            edited: str = _ts_to_str(float(note.mod))

            text = (
                f"[{status}]  {preview}\n"
                f"    added: {added}   edited: {edited}"
            )
            item: QListWidgetItem = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, len(self.notes) - 1)
            item.setCheckState(Qt.CheckState.Checked)

            if card.queue == -1:
                item.setForeground(QBrush(QColor("gray")))

            self.list_widget.addItem(item)

        self.list_widget.blockSignals(False)
        self.status_label.setText(f"Found {len(self.notes)} notes.")
        self.start_btn.setEnabled(len(self.notes) > 0)

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        selected: bool = False
        for i in range(self.list_widget.count()):
            item: Optional[QListWidgetItem] = self.list_widget.item(i)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected = True
                break
        self.start_btn.setEnabled(selected)

    def _start_review(self) -> None:
        selected_indices: List[int] = []
        for i in range(self.list_widget.count()):
            item: Optional[QListWidgetItem] = self.list_widget.item(i)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                selected_indices.append(item.data(Qt.ItemDataRole.UserRole))

        if not selected_indices:
            showInfo("Please select at least one note.")
            return

        selected_notes: List[Note] = [self.notes[i] for i in selected_indices]
        selected_cards: List[Card] = [self.cards[i] for i in selected_indices]

        global _review_window
        _review_window = ReviewSlideshow(selected_notes, selected_cards, mw)
        _review_window.show()
        _review_window.raise_()
        _review_window.activateWindow()


class ReviewSlideshow(QDialog):
    def __init__(
        self,
        notes: List[Note],
        cards: List[Card],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Slideshow")
        self.resize(1000, 800)
        self.setMinimumSize(700, 500)

        self.notes: List[Note] = notes
        self.cards: List[Card] = cards
        self.current_index: int = 0
        self._processed: set = set()

        self._build_ui()
        self._show_card(0)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.progress_label = QLabel("")
        layout.addWidget(self.progress_label)

        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color: #666;")
        layout.addWidget(self.meta_label)

        self.editor_container = QScrollArea()
        self.editor_container.setWidgetResizable(True)
        self.editor_container.setMinimumHeight(400)

        self.editor = Editor(mw, self.editor_container, self)
        layout.addWidget(self.editor_container, stretch=1)

        nav_layout = QHBoxLayout()
        self.prev_btn = QPushButton("Previous")
        self.next_btn = QPushButton("Next")
        qconnect(self.prev_btn.clicked, self._go_prev)
        qconnect(self.next_btn.clicked, self._go_next)
        nav_layout.addWidget(self.prev_btn)
        nav_layout.addWidget(self.next_btn)
        nav_layout.addStretch()
        layout.addLayout(nav_layout)

        btn_layout = QHBoxLayout()
        self.approve_btn = QPushButton("Approve (Save, Unsuspend and Remove)")
        self.reject_btn = QPushButton("Reject (Suspend and Remove)")
        qconnect(self.approve_btn.clicked, self._approve)
        qconnect(self.reject_btn.clicked, self._reject)
        btn_layout.addWidget(self.approve_btn)
        btn_layout.addWidget(self.reject_btn)
        layout.addLayout(btn_layout)

    def _update_labels(self) -> None:
        card: Card = self.cards[self.current_index]
        note: Note = self.notes[self.current_index]
        status: str = _card_status(card)
        total = len(self.notes) + len(self._processed)
        done = len(self._processed)
        self.progress_label.setText(
            f"Card {self.current_index + 1} of {len(self.notes)}  |  "
            f"Processed {done} of {total}  |  Status: {status}"
        )
        added: str = _ts_to_str(_card_added_ts(card))
        edited: str = _ts_to_str(float(note.mod))
        self.meta_label.setText(f"Added: {added}    Last edited: {edited}")

    def _update_nav_buttons(self) -> None:
        if not self.notes:
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return
        self.prev_btn.setEnabled(self.current_index > 0)
        self.next_btn.setEnabled(self.current_index < len(self.notes) - 1)

    def _show_card(self, index: int) -> None:
        if not self.notes:
            showInfo("Review finished.")
            self.close()
            return

        index = max(0, min(index, len(self.notes) - 1))
        self.current_index = index
        note: Note = self.notes[index]

        self._update_labels()
        self.editor.set_note(note)
        self._update_nav_buttons()

    def _save_current_note(
        self,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        idx: int = self.current_index
        note: Note = self.notes[idx]

        def after_save() -> None:
            mw.col.update_note(note)
            if on_done is not None:
                on_done()

        self.editor.saveNow(after_save)

    def _go_prev(self) -> None:
        if self.current_index <= 0:
            return
        idx = self.current_index
        self._save_current_note(on_done=lambda: self._show_card(idx - 1))

    def _go_next(self) -> None:
        if self.current_index >= len(self.notes) - 1:
            tooltip("You are on the last card.")
            return
        idx = self.current_index
        self._save_current_note(on_done=lambda: self._show_card(idx + 1))

    def _approve(self) -> None:
        idx = self.current_index
        card: Card = self.cards[idx]

        def after_save() -> None:
            if card.queue == -1:
                mw.col.sched.unsuspend_cards([card.id])
            self._processed.add(card.id)
            self._remove_current()

        self._save_current_note(on_done=after_save)

    def _reject(self) -> None:
        idx = self.current_index
        card: Card = self.cards[idx]

        if card.queue != -1:
            mw.col.sched.suspend_cards([card.id])

        self._processed.add(card.id)
        tooltip("Card rejected. Edits discarded.")
        self._remove_current()

    def _remove_current(self) -> None:
        idx = self.current_index
        del self.notes[idx]
        del self.cards[idx]

        if not self.notes:
            showInfo("Review finished. All cards were approved or rejected.")
            self.close()
            return

        new_index = min(idx, len(self.notes) - 1)
        self._show_card(new_index)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.editor.cleanup()
        except Exception:
            pass
        event.accept()


def open_bulk_reviewer() -> None:
    global _search_dialog
    _search_dialog = BulkReviewDialog(mw)
    _search_dialog.show()


action: QAction = QAction("Bulk Review", mw)
qconnect(action.triggered, open_bulk_reviewer)
mw.form.menuTools.addAction(action)
