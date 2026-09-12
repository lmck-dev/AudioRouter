"""What is playing, and where it is going.

This is the table the user actually came for: every application making sound,
with a menu to send it somewhere else. Moving something here takes effect
immediately, because a routing change the user cannot hear straight away is
indistinguishable from one that did not work.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..engine import Engine
from .theme import Theme

UNROUTED = "-"


class StreamsPanel(QGroupBox):
    """One row per playing application, with the channel it is going to."""

    send_requested = pyqtSignal(int, str)
    remember_requested = pyqtSignal(int, str)

    def __init__(self, engine: Engine, parent: QWidget | None = None) -> None:
        super().__init__("Playing now", parent)
        self.engine = engine
        self._rebuilding = False

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Application", "Playing", "Send to"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.empty = QLabel("Nothing is playing.", self)
        self.remember = QPushButton("Always send this app here", self)
        self.remember.setEnabled(False)
        self.remember.clicked.connect(self._remember)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.remember)

    # -- population --------------------------------------------------------

    def refresh(self, status: dict) -> None:
        """Rebuild from an engine status dict.

        A combo box the user has open must not be yanked away underneath them,
        so nothing is rebuilt while one is showing its popup.
        """
        for row in range(self.table.rowCount()):
            widget = self.table.cellWidget(row, 2)
            if isinstance(widget, QComboBox) and widget.view().isVisible():
                return

        streams = status["streams"]
        running = [c["slug"] for c in status["channels"] if c["running"]]
        selected = self._selected_stream_id()

        self._rebuilding = True
        self.table.setRowCount(len(streams))
        for row, stream in enumerate(streams):
            name = QTableWidgetItem(stream["app"])
            name.setData(Qt.ItemDataRole.UserRole, stream["id"])
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(stream["title"] or ""))

            combo = QComboBox(self)
            combo.addItem("Not routed", UNROUTED)
            for channel in status["channels"]:
                label = channel["name"]
                if channel["slug"] not in running:
                    label += " (stopped)"
                combo.addItem(label, channel["slug"])
                if channel["slug"] not in running:
                    combo.model().item(combo.count() - 1).setEnabled(False)
            current = combo.findData(stream["channel"] or UNROUTED)
            combo.setCurrentIndex(max(0, current))
            combo.setProperty("stream_id", stream["id"])
            combo.activated.connect(self._combo_activated)
            self.table.setCellWidget(row, 2, combo)
        self._rebuilding = False

        self.table.setVisible(bool(streams))
        self.empty.setVisible(not streams)
        if selected is not None:
            self._select_stream(selected)
        self._selection_changed()

    # -- interaction -------------------------------------------------------

    def _combo_activated(self, index: int) -> None:
        if self._rebuilding:
            return
        combo = self.sender()
        if not isinstance(combo, QComboBox):
            return
        stream_id = int(combo.property("stream_id"))
        slug = combo.itemData(index)
        if slug and slug != UNROUTED:
            self.send_requested.emit(stream_id, slug)

    def _selected_stream_id(self) -> int | None:
        items = self.table.selectedItems()
        if not items:
            return None
        item = self.table.item(items[0].row(), 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _select_stream(self, stream_id: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == stream_id:
                self.table.selectRow(row)
                return

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        combo = self.table.cellWidget(row, 2) if row >= 0 else None
        has_channel = isinstance(combo, QComboBox) and combo.currentData() != UNROUTED
        self.remember.setEnabled(bool(self.table.selectedItems()) and has_channel)

    def _remember(self) -> None:
        stream_id = self._selected_stream_id()
        row = self.table.currentRow()
        combo = self.table.cellWidget(row, 2) if row >= 0 else None
        if stream_id is None or not isinstance(combo, QComboBox):
            return
        slug = combo.currentData()
        if slug and slug != UNROUTED:
            self.remember_requested.emit(int(stream_id), str(slug))
