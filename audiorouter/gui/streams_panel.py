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
    QFrame,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..engine import Engine
from .theme import Theme

UNROUTED = "-"


class StreamsPanel(QWidget):
    """One row per playing application, with the channel it is going to.

    It folds away behind its heading: once apps remember their channels the
    table is rarely needed, and the heading still counts what is playing.
    """

    send_requested = pyqtSignal(int, str)
    remember_requested = pyqtSignal(int, str)
    expanded_changed = pyqtSignal(bool)

    def __init__(self, engine: Engine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._rebuilding = False
        self._count = 0

        self.toggle = QToolButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setAutoRaise(True)
        self.toggle.toggled.connect(self._toggled)
        self.body = QFrame(self)

        self.table = QTableWidget(0, 3, self.body)
        self.table.setHorizontalHeaderLabels(["Application", "Playing", "Send to"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.empty = QLabel("Nothing is playing.", self.body)
        self.remember = QPushButton("Always send this app here", self.body)
        self.remember.setEnabled(False)
        self.remember.clicked.connect(self._remember)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.empty)
        body.addWidget(self.table, 1)
        body.addWidget(self.remember)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body, 1)
        self._show_heading()

    # -- folding -----------------------------------------------------------

    @property
    def expanded(self) -> bool:
        return self.toggle.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.toggle.setChecked(on)

    def _toggled(self, on: bool) -> None:
        self.body.setVisible(on)
        self._show_heading()
        self.expanded_changed.emit(on)

    def _show_heading(self) -> None:
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if self.expanded else Qt.ArrowType.RightArrow
        )
        self.toggle.setText(f"Playing now ({self._count})" if self._count else "Playing now")

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
        self._count = len(streams)
        self._show_heading()
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
                if channel.get("kind") == "input":
                    continue  # playback cannot be sent into a microphone
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
