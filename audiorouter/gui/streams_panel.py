"""What is playing, and where it is going - and what is recording, and from where.

This is the table the user actually came for: every application making sound,
with a menu to send it somewhere else, and every application recording (Audacity,
OBS, a call), with a menu to choose which channel it hears. Moving something here takes effect
immediately, because a routing change the user cannot hear straight away is
indistinguishable from one that did not work.

Each row also shows the app's OWN volume (owner, 26 Oct 2026): the per-app
volume the desktop keeps, before any channel. WirePlumber remembers it per
app, so an app left at 5% stays quiet for good and looked like "no levels"
in the mixer; a low or muted app is marked so that cannot hide again.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..engine import Engine
from .mixer import VOLUME_SETTLE_S
from .theme import Theme

UNROUTED = "-"
#: On a row's name item: True for an app recording rather than playing.
RECORDING_ROLE = Qt.ItemDataRole.UserRole + 1
#: Below this an app's own volume is marked as low (25% is -36 dB).
LOW_VOLUME = 0.25
SEND_COLUMN = 2
VOLUME_COLUMN = 3


class VolumeCell(QWidget):
    """One app's own volume: a slider, the percentage, and a mute button."""

    volume_changed = pyqtSignal(int, float)
    mute_toggled = pyqtSignal(int, bool)

    def __init__(self, stream_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stream_id = stream_id
        self._last_local_edit = 0.0
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setPageStep(10)
        self.slider.setFixedWidth(110)
        self.slider.valueChanged.connect(self._moved)
        self.label = QLabel("", self)
        self.label.setMinimumWidth(self.label.fontMetrics().horizontalAdvance("muted 100%") + 4)
        self.mute = QToolButton(self)
        self.mute.setText("M")
        self.mute.setCheckable(True)
        self.mute.setToolTip("Mute this app")
        self.mute.toggled.connect(self._mute_toggled)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(self.slider)
        layout.addWidget(self.label)
        layout.addWidget(self.mute)

    def show_volume(self, volume: float | None, muted: bool | None) -> None:
        """Show the graph's value, unless the user is moving it right now."""
        self.setEnabled(volume is not None)
        if volume is None:
            self.label.setText("")
            return
        if self.slider.isSliderDown() or time.monotonic() - self._last_local_edit < VOLUME_SETTLE_S:
            return
        percent = round(volume * 100)
        self.slider.blockSignals(True)
        self.slider.setMaximum(150 if percent > 100 else 100)
        self.slider.setValue(percent)
        self.slider.blockSignals(False)
        self.mute.blockSignals(True)
        self.mute.setChecked(bool(muted))
        self.mute.blockSignals(False)
        self._show_label(percent, bool(muted))

    def _show_label(self, percent: int, muted: bool) -> None:
        low = muted or percent < LOW_VOLUME * 100
        self.label.setText(f"muted {percent}%" if muted else f"{percent}%")
        self.label.setStyleSheet(Theme(self).css(Theme(self).warn) if low else "")
        self.setToolTip(
            "This app's own volume is turned down, before any channel - it plays quietly "
            "wherever you send it. The desktop remembers it for the app." if low else
            "This app's own volume, before any channel. The desktop remembers it for the app.")

    def _moved(self, percent: int) -> None:
        self._last_local_edit = time.monotonic()
        self._show_label(percent, self.mute.isChecked())
        self.volume_changed.emit(self.stream_id, percent / 100.0)

    def _mute_toggled(self, muted: bool) -> None:
        self._last_local_edit = time.monotonic()
        self._show_label(self.slider.value(), muted)
        self.mute_toggled.emit(self.stream_id, muted)


class StreamsPanel(QWidget):
    """One row per playing application, with the channel it is going to.

    It folds away behind its heading: once apps remember their channels the
    table is rarely needed, and the heading still counts what is playing.
    """

    send_requested = pyqtSignal(int, str)
    remember_requested = pyqtSignal(int, str)
    #: An app's own volume (1.0 = 100%), and its mute.
    volume_requested = pyqtSignal(int, float)
    mute_requested = pyqtSignal(int, bool)
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

        self.table = QTableWidget(0, 4, self.body)
        self.table.setHorizontalHeaderLabels(["Application", "Doing", "Send to / record from", "App volume"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self.empty = QLabel("Nothing is playing or recording.", self.body)
        self.remember = QPushButton("Always send this app here", self.body)
        self.remember.setEnabled(False)
        self.remember.clicked.connect(self._remember)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        streams = QVBoxLayout()
        streams.addWidget(self.empty)
        streams.addWidget(self.table, 1)
        streams.addWidget(self.remember)
        # Anything placed beside the table folds away with it.
        self._body = QHBoxLayout(self.body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.addLayout(streams, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body, 1)
        self._show_heading()

    def add_beside(self, widget: QWidget) -> None:
        """Show `widget` to the right of the table, folding along with it."""
        widget.setParent(self.body)
        self._body.addWidget(widget, 1)

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
            widget = self.table.cellWidget(row, SEND_COLUMN)
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
            name.setData(RECORDING_ROLE, bool(stream.get("recording")))
            self.table.setItem(row, 0, name)
            doing = "Recording" if stream.get("recording") else (stream["title"] or "")
            self.table.setItem(row, 1, QTableWidgetItem(doing))
            self._volume_cell(row, stream)
            combo = self._record_combo(stream, status) if stream.get("recording") else None
            if combo is not None:
                combo.setProperty("stream_id", stream["id"])
                combo.activated.connect(self._combo_activated)
                self.table.setCellWidget(row, SEND_COLUMN, combo)
                continue

            combo = QComboBox(self)
            combo.addItem("Not routed", UNROUTED)
            for channel in status["channels"]:
                if channel.get("kind") == "input":
                    continue  # playback cannot be sent into a microphone
                label = channel["name"]
                if channel.get("companion_of"):
                    label += " (into the mic)"  # a mic channel's mix
                if channel["slug"] not in running:
                    label += " (stopped)"
                combo.addItem(label, channel["slug"])
                if channel["slug"] not in running:
                    combo.model().item(combo.count() - 1).setEnabled(False)
            current = combo.findData(stream["channel"] or UNROUTED)
            combo.setCurrentIndex(max(0, current))
            combo.setProperty("stream_id", stream["id"])
            combo.activated.connect(self._combo_activated)
            self.table.setCellWidget(row, SEND_COLUMN, combo)
        self._rebuilding = False

        self.table.setVisible(bool(streams))
        self.empty.setVisible(not streams)
        if selected is not None:
            self._select_stream(selected)
        self._selection_changed()

    def _volume_cell(self, row: int, stream: dict) -> None:
        """Keep the row's volume cell when it is the same app's, so a slider in use survives."""
        cell = self.table.cellWidget(row, VOLUME_COLUMN)
        if not isinstance(cell, VolumeCell) or cell.stream_id != stream["id"]:
            cell = VolumeCell(stream["id"], self)
            cell.volume_changed.connect(self.volume_requested)
            cell.mute_toggled.connect(self.mute_requested)
            self.table.setCellWidget(row, VOLUME_COLUMN, cell)
        cell.show_volume(stream.get("volume"), stream.get("muted"))

    def _record_combo(self, stream: dict, status: dict) -> QComboBox:
        """Which channel a recording app hears: every mic and recordable channel.

        Anything else it records from (a real mic, a device) shows by name, so
        the menu never claims it is on a channel when it is not.
        """
        combo = QComboBox(self)
        if stream["channel"] is None:
            combo.addItem(stream["sink"] or "Nothing yet", UNROUTED)
        for source in status.get("sources", []):
            label = f"{source['name']} (mic)" if source["input"] else source["name"]
            combo.addItem(label, source["slug"])
        current = combo.findData(stream["channel"] or UNROUTED)
        combo.setCurrentIndex(max(0, current))
        return combo

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
        combo = self.table.cellWidget(row, SEND_COLUMN) if row >= 0 else None
        name = self.table.item(row, 0) if row >= 0 else None
        recording = name is not None and bool(name.data(RECORDING_ROLE))
        self.remember.setText("Always record from here" if recording else "Always send this app here")
        has_channel = isinstance(combo, QComboBox) and combo.currentData() != UNROUTED
        self.remember.setEnabled(bool(self.table.selectedItems()) and has_channel)

    def _remember(self) -> None:
        stream_id = self._selected_stream_id()
        row = self.table.currentRow()
        combo = self.table.cellWidget(row, SEND_COLUMN) if row >= 0 else None
        if stream_id is None or not isinstance(combo, QComboBox):
            return
        slug = combo.currentData()
        if slug and slug != UNROUTED:
            self.remember_requested.emit(int(stream_id), str(slug))
