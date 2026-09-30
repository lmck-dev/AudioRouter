"""The mixer: every channel as a strip on one desk, the way a console lays it out.

Inputs sit on the left, outputs on the right. Each strip reads top to bottom
in signal order: where the sound comes from, the inserts (the effect chain,
each one lit while it is on), where it goes, then the fader, the mute and a
meter of what the channel puts out.

The strips are another view of the same settings as the Channels view - they
edit the same `Channel` objects and report through the same signals, so the
window's one debounce still decides when audio restarts. Anything that needs
more room than a strip has (an effect's knobs, echo cancellation, a new
channel's name) opens the Channels view on that channel.

Meters read each channel's last level tap (`meter.output_tap`), so a desk of
strips costs no recording streams and never opens a microphone: an input
strip moves only while something records that input.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..channels import NOWHERE, Channel
from ..effects import EffectError
from ..meter import FileLevelReader, Levels, Tap, output_tap
from .meters import FLOOR_DB, FRAME_MS, SCALE_MARKS, LevelBar, bar_span, fraction
from .theme import Theme

STRIP_WIDTH = 132
#: Room for an insert's name: the strip less its margins and a scroll bar.
INSERT_TEXT_WIDTH = STRIP_WIDTH - 12 - 16 - 10
#: A local fader move wins over graph readings for this long (see channel_panel).
VOLUME_SETTLE_S = 0.8
#: How often a strip whose tap ended looks for its channel's new host.
RETRY_MS = 1000
FOLLOW_DEFAULT = ""
NOT_LISTENING = ""


def volume_db(volume: float) -> str:
    """The desktop's volume (cubic) as the dB a sound technician expects."""
    if volume <= 0:
        return "-inf dB"
    return f"{60 * math.log10(volume):+.1f} dB"


class VerticalScale(QWidget):
    """dB marks beside a vertical meter, lined up with its bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(self.fontMetrics().horizontalAdvance("-48") + 4)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def paintEvent(self, _event) -> None:
        theme = Theme(self)
        painter = QPainter(self)
        font = painter.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.8))
        painter.setFont(font)
        painter.setPen(theme.dim)
        # The same span a vertical LevelBar of this height uses.
        span = bar_span(QRectF(0, 0, self.height(), 7).adjusted(0.5, 0.5, -0.5, -0.5))
        metrics = painter.fontMetrics()
        for mark in SCALE_MARKS:
            y = self.height() - (span.left() + span.width() * fraction(mark))
            text = str(mark)
            baseline = min(max(y + metrics.ascent() / 2 - 1, metrics.ascent()), self.height() - 1)
            painter.drawText(self.width() - metrics.horizontalAdvance(text) - 2, int(baseline), text)
        painter.end()


class ChannelStrip(QFrame):
    """One channel as a console strip. Emits what the user did; decides nothing."""

    volume_changed = pyqtSignal(str, float)
    mute_toggled = pyqtSignal(str, bool)
    effect_toggled = pyqtSignal(str, int, bool)
    effect_opened = pyqtSignal(str, int)
    add_effect = pyqtSignal(str)
    device_chosen = pyqtSignal(str, str)
    listen_chosen = pyqtSignal(str, str)
    open_settings = pyqtSignal(str)

    def __init__(self, channel: Channel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.slug = channel.slug
        self._last_local_edit = 0.0
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(STRIP_WIDTH)
        theme = Theme(self)
        small = self.font()
        small.setPointSizeF(max(6.5, small.pointSizeF() * 0.85))

        def caption(text: str) -> QLabel:
            label = QLabel(text, self)
            label.setFont(small)
            label.setStyleSheet(theme.css(theme.dim))
            return label

        self.name = QLabel(channel.name, self)
        bold = self.name.font()
        bold.setBold(True)
        self.name.setFont(bold)
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setToolTip(channel.name)
        kind = "INPUT" if channel.is_input else ("CABLE" if channel.recordable else "OUTPUT")
        self.kind = caption(kind)
        self.kind.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Where the sound comes from: the mic for an input, the apps for an output.
        self.source = QComboBox(self) if channel.is_input else None
        if self.source is not None:
            self.source.setToolTip("Records from")
            self.source.activated.connect(self._source_chosen)
        self.apps = caption("")
        self.apps.setWordWrap(True)
        self.apps.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # The inserts scroll inside the strip: a long chain must never push
        # the fader off the bottom of the desk.
        self.insert_area = QScrollArea(self)
        self.insert_area.setWidgetResizable(True)
        self.insert_area.setFrameShape(QFrame.Shape.NoFrame)
        self.insert_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget(self.insert_area)
        self.inserts = QVBoxLayout(holder)
        self.inserts.setContentsMargins(0, 0, 0, 0)
        self.inserts.setSpacing(2)
        self.insert_buttons: list[QToolButton] = []
        for index, effect in enumerate(channel.effects):
            self.inserts.addWidget(self._insert_button(index, effect))
        self.add_button = QToolButton(holder)
        self.add_button.setText("+ Insert")
        self.add_button.setToolTip("Add an effect to this channel")
        self.add_button.setAutoRaise(True)
        self.add_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.add_button.clicked.connect(lambda: self.add_effect.emit(self.slug))
        self.inserts.addWidget(self.add_button)
        self.inserts.addStretch(1)
        self.insert_area.setWidget(holder)
        self.insert_area.setMinimumHeight(60)

        # Where it goes: the device for an output, listen-through for an input.
        self.route = QComboBox(self)
        if channel.is_input:
            self.route.setToolTip("Listen through (hear this input on an output channel)")
            self.route.activated.connect(self._listen_chosen)
        else:
            self.route.setToolTip("Plays through")
            self.route.activated.connect(self._device_chosen)

        self.fader = QSlider(Qt.Orientation.Vertical, self)
        self.fader.setRange(0, 100)
        self.fader.setToolTip("Channel volume - the same one the desktop's sound settings show")
        self.fader.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.fader.valueChanged.connect(self._fader_moved)
        self.meter_l = LevelBar(self, vertical=True)
        self.meter_r = LevelBar(self, vertical=True)
        self.meter_l.setToolTip("What this channel puts out, after its effects")
        self.meter_r.setToolTip(self.meter_l.toolTip())
        self.scale = VerticalScale(self)
        self.fader.setMinimumHeight(140)

        self.volume_label = caption("")
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.level_label = caption("")
        self.level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.mute = QPushButton("M", self)
        self.mute.setCheckable(True)
        self.mute.setToolTip("Mute")
        self.mute.setFixedWidth(34)
        self.mute.toggled.connect(self._mute_toggled)
        self.edit = QPushButton("Edit", self)
        self.edit.setToolTip("Open this channel in the Channels view")
        self.edit.clicked.connect(lambda: self.open_settings.emit(self.slug))

        faders = QHBoxLayout()
        faders.setSpacing(3)
        faders.addStretch(1)
        faders.addWidget(self.fader)
        faders.addWidget(self.scale)
        faders.addWidget(self.meter_l)
        faders.addWidget(self.meter_r)
        faders.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addWidget(self.mute)
        buttons.addWidget(self.edit, 1)

        # Top to bottom in signal order, the name first so a strip is always
        # identifiable even when the desk is too short for all of it.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self.name)
        layout.addWidget(self.kind)
        if self.source is not None:
            layout.addWidget(self.source)
        layout.addWidget(self.apps)
        layout.addWidget(caption("INSERTS"))
        layout.addWidget(self.insert_area, 2)
        layout.addWidget(caption("LISTEN" if channel.is_input else "OUT"))
        layout.addWidget(self.route)
        layout.addLayout(buttons)
        layout.addLayout(faders, 3)
        layout.addWidget(self.volume_label)
        layout.addWidget(self.level_label)

    def sizeHint(self) -> QSize:
        return QSize(STRIP_WIDTH, 520)

    # -- inserts -------------------------------------------------------------

    def _insert_button(self, index: int, effect) -> QToolButton:
        try:
            label = effect.spec.label
            unavailable = bool(effect.spec.unsatisfied())
        except EffectError:
            label, unavailable = effect.kind, True
        button = QToolButton(self)
        # Shortened in the middle to fit: a button's text would otherwise
        # widen the list past the strip and be cut off at the edge.
        button.setText(button.fontMetrics().elidedText(
            label, Qt.TextElideMode.ElideMiddle, INSERT_TEXT_WIDTH))
        button.setCheckable(True)
        button.setChecked(effect.enabled)
        button.setMinimumWidth(0)
        button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        # Lit while on, like an insert's LED: the palette's highlight, so it
        # reads on light and dark themes alike.
        palette = self.palette()
        button.setStyleSheet(
            "QToolButton { padding: 2px; }"
            f"QToolButton:checked {{ background: {palette.highlight().color().name()};"
            f" color: {palette.highlightedText().color().name()};"
            f" border: 1px solid {palette.highlight().color().darker(130).name()}; border-radius: 3px; }}"
        )
        tip = f"{label}\nClick: switch on or off.  Double-click: open its settings."
        if unavailable:
            tip = f"{label} is not available here.\n" + tip
            button.setEnabled(effect.enabled)  # an unavailable one can still be switched off
        button.setToolTip(tip)
        button.toggled.connect(lambda on, i=index: self.effect_toggled.emit(self.slug, i, on))
        button.installEventFilter(self)
        button.setProperty("effect_index", index)
        self.insert_buttons.append(button)
        return button

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonDblClick and watched in self.insert_buttons:
            self.effect_opened.emit(self.slug, int(watched.property("effect_index")))
            return True
        return super().eventFilter(watched, event)

    # -- state from the window ---------------------------------------------

    def show_state(self, entry: dict | None, apps: list[str],
                   devices: list[tuple[str, str]], outputs: list[tuple[str, str]]) -> None:
        """Refresh everything that can change without the strip being rebuilt."""
        self._fill_routes(entry, devices, outputs)
        if not self.channel.is_input:
            self.apps.setText(", ".join(apps) if apps else "no apps playing")
        else:
            self.apps.setText("")
        self.apps.setHidden(not self.apps.text())
        volume = entry.get("volume") if entry else None
        running = bool(entry and entry.get("running"))
        self.fader.setEnabled(volume is not None)
        self.mute.setEnabled(volume is not None)
        if volume is None:
            self.volume_label.setText("off" if entry and not entry.get("enabled") else "not running")
        elif not self.fader.isSliderDown() and time.monotonic() - self._last_local_edit >= VOLUME_SETTLE_S:
            percent = round(volume * 100)
            self.fader.blockSignals(True)
            self.fader.setMaximum(150 if percent > 100 else 100)
            self.fader.setValue(percent)
            self.fader.blockSignals(False)
            self.volume_label.setText(volume_db(volume))
            self.mute.blockSignals(True)
            self.mute.setChecked(bool(entry.get("muted")))
            self.mute.blockSignals(False)
        self._show_mute()
        self.name.setEnabled(running)

    def _fill_routes(self, entry: dict | None, devices, outputs) -> None:
        present = entry["device_present"] if entry else True
        if self.source is not None:
            self._fill(self.source, [(FOLLOW_DEFAULT, "Default input"), *[(n, l) for n, l in devices]],
                       self.channel.device, present)
            self._fill(self.route, [(NOT_LISTENING, "Don't listen"), *outputs], self.channel.listen, True)
        else:
            choices = [(FOLLOW_DEFAULT, "Default output"), *devices, (NOWHERE, "Nowhere (recording only)")]
            self._fill(self.route, choices, self.channel.device, present)

    @staticmethod
    def _fill(combo: QComboBox, choices: list[tuple[str, str]], current: str, present: bool) -> None:
        if combo.view().isVisible():
            return  # never rebuild a list the user has open
        combo.blockSignals(True)
        combo.clear()
        for data, label in choices:
            combo.addItem(label, data)
        if current and combo.findData(current) < 0:
            combo.addItem(current if present else f"{current} (not connected)", current)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.setToolTip(f"{combo.toolTip().split(chr(10))[0]}\n{combo.currentText()}")
        combo.blockSignals(False)

    def _show_mute(self) -> None:
        theme = Theme(self)
        self.mute.setStyleSheet(
            f"background: {theme.warn.name()}; color: {self.palette().base().color().name()};"
            if self.mute.isChecked() else ""
        )

    # -- user actions --------------------------------------------------------

    def _fader_moved(self, value: int) -> None:
        self._last_local_edit = time.monotonic()
        self.volume_label.setText(volume_db(value / 100))
        self.volume_changed.emit(self.slug, value / 100)

    def _mute_toggled(self, on: bool) -> None:
        self._show_mute()
        self.mute_toggled.emit(self.slug, on)

    def _device_chosen(self, index: int) -> None:
        self.device_chosen.emit(self.slug, self.route.itemData(index) or FOLLOW_DEFAULT)

    def _source_chosen(self, index: int) -> None:
        assert self.source is not None
        self.device_chosen.emit(self.slug, self.source.itemData(index) or FOLLOW_DEFAULT)

    def _listen_chosen(self, index: int) -> None:
        self.listen_chosen.emit(self.slug, self.route.itemData(index) or NOT_LISTENING)

    # -- meter -----------------------------------------------------------------

    def feed(self, levels: Levels, now: float) -> None:
        self.meter_l.state.feed(levels.peak_l, levels.ms_l, now)
        self.meter_r.state.feed(levels.peak_r, levels.ms_r, now)

    def tick(self, now: float) -> None:
        for bar in (self.meter_l, self.meter_r):
            bar.state.tick(now)
            bar.update()
        hold = max(self.meter_l.state.hold_db, self.meter_r.state.hold_db)
        theme = Theme(self)
        if self.meter_l.state.clipped(now) or self.meter_r.state.clipped(now):
            self.level_label.setText("CLIP")
            self.level_label.setStyleSheet(theme.css(theme.warn))
        else:
            self.level_label.setText("" if hold <= FLOOR_DB else f"peak {hold:.1f}")
            self.level_label.setStyleSheet(theme.css(theme.dim))

    def silence(self) -> None:
        for bar in (self.meter_l, self.meter_r):
            bar.state.silence()
            bar.update()
        self.level_label.setText("")


class MixerView(QWidget):
    """All channels as strips; the meters run only while the view is on screen."""

    volume_changed = pyqtSignal(str, float)
    mute_toggled = pyqtSignal(str, bool)
    effect_toggled = pyqtSignal(str, int, bool)
    effect_opened = pyqtSignal(str, int)
    add_effect = pyqtSignal(str)
    device_chosen = pyqtSignal(str, str)
    listen_chosen = pyqtSignal(str, str)
    open_settings = pyqtSignal(str)
    new_channel = pyqtSignal(bool)  # is_input

    #: From reader threads: (slug, Levels). Delivered queued onto the GUI thread.
    _levels = pyqtSignal(str, object)
    _ended = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.strips: dict[str, ChannelStrip] = {}
        self.active = False
        self._channels: list[Channel] = []
        self._signature: tuple = ()
        self._readers: dict[str, FileLevelReader] = {}
        self._levels.connect(self._on_levels)
        self._ended.connect(self._on_ended)

        self.desk = QWidget(self)
        self.row = QHBoxLayout(self.desk)
        self.row.setContentsMargins(4, 4, 4, 4)
        self.row.setSpacing(6)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.desk)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)
        self._frame.timeout.connect(self._redraw)
        self._retry = QTimer(self)
        self._retry.setInterval(RETRY_MS)
        self._retry.timeout.connect(self._follow_taps)

    # -- building ------------------------------------------------------------

    def refresh(self, channels: list[Channel], status: dict) -> None:
        """Show these channels; rebuild the desk only when its shape changed."""
        signature = tuple(
            (c.slug, c.name, c.kind, c.recordable, c.enabled,
             tuple((e.kind, e.plugin, e.enabled) for e in c.effects))
            for c in channels
        )
        if signature != self._signature or any(
            self.strips.get(c.slug) is None or self.strips[c.slug].channel is not c for c in channels
        ):
            self._signature = signature
            self._channels = list(channels)
            self._rebuild()
        entries = {c["slug"]: c for c in status.get("channels", [])}
        apps: dict[str, list[str]] = {}
        for stream in status.get("streams", []):
            if stream.get("channel"):
                apps.setdefault(stream["channel"], []).append(stream["app"] or "?")
        outputs = [(c.slug, c.name) for c in channels if not c.is_input]
        for channel in channels:
            devices = status.get("input_devices" if channel.is_input else "devices", [])
            self.strips[channel.slug].show_state(
                entries.get(channel.slug),
                sorted(set(apps.get(channel.slug, []))),
                [(d["name"], d["label"]) for d in devices],
                outputs,
            )
        self._follow_taps()

    def _rebuild(self) -> None:
        self._stop_readers()
        while self.row.count():
            item = self.row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.strips.clear()
        inputs = [c for c in self._channels if c.is_input]
        outputs = [c for c in self._channels if not c.is_input]
        for title, group, is_input in (("Inputs", inputs, True), ("Outputs", outputs, False)):
            self.row.addWidget(self._group_header(title, is_input))
            for channel in group:
                strip = ChannelStrip(channel, self.desk)
                for name in ("volume_changed", "mute_toggled", "effect_toggled", "effect_opened",
                             "add_effect", "device_chosen", "listen_chosen", "open_settings"):
                    getattr(strip, name).connect(getattr(self, name).emit)
                self.strips[channel.slug] = strip
                self.row.addWidget(strip)
        self.row.addStretch(1)

    def _group_header(self, title: str, is_input: bool) -> QWidget:
        """A narrow column naming the group, with its New button."""
        column = QWidget(self.desk)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title.upper(), column)
        label.setStyleSheet(Theme(self).css(Theme(self).dim))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        new = QToolButton(column)
        new.setText("+")
        new.setToolTip(f"New {'input' if is_input else 'output'} channel")
        new.clicked.connect(lambda: self.new_channel.emit(is_input))
        layout.addWidget(label)
        layout.addWidget(new, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        column.setFixedWidth(max(label.sizeHint().width(), 28) + 4)
        return column

    # -- meters ----------------------------------------------------------------

    def set_active(self, active: bool) -> None:
        """Run the strip meters only while the mixer is on screen."""
        if active == self.active:
            return
        self.active = active
        if active:
            self._follow_taps()
        else:
            self._stop_readers()

    def stop(self) -> None:
        self.active = False
        self._stop_readers()

    def _follow_taps(self) -> None:
        if not self.active:
            return
        missing = False
        for channel in self._channels:
            strip = self.strips.get(channel.slug)
            try:
                tap = output_tap(channel)
            except OSError:
                tap = None
            reader = self._readers.get(channel.slug)
            if reader is not None and (tap is None or reader.tap.key != tap.key):
                reader.stop()
                del self._readers[channel.slug]
                if strip is not None:
                    strip.silence()
                reader = None
            if tap is None:
                missing = missing or channel.enabled
                continue
            if reader is None:
                reader = FileLevelReader(
                    tap,
                    lambda levels, s=channel.slug: self._levels.emit(s, levels),
                    lambda s=channel.slug, k=tap.key: self._ended.emit(s, k),
                )
                reader.start()
                self._readers[channel.slug] = reader
        if self._readers and not self._frame.isActive():
            self._frame.start()
        # A host restarting has no tap for a moment; look again shortly.
        if missing and not self._retry.isActive():
            self._retry.start()
        elif not missing:
            self._retry.stop()

    @pyqtSlot(str, object)
    def _on_levels(self, slug: str, levels: Levels) -> None:
        strip = self.strips.get(slug)
        if strip is not None and slug in self._readers:
            strip.feed(levels, time.monotonic())

    @pyqtSlot(str, object)
    def _on_ended(self, slug: str, key: tuple) -> None:
        reader = self._readers.get(slug)
        if reader is None or reader.tap.key != key:
            return
        reader.stop()
        del self._readers[slug]
        if slug in self.strips:
            self.strips[slug].silence()
        if not self._retry.isActive():
            self._retry.start()

    def _redraw(self) -> None:
        now = time.monotonic()
        for strip in self.strips.values():
            strip.tick(now)
        if not self._readers:
            self._frame.stop()

    def _stop_readers(self) -> None:
        for reader in self._readers.values():
            reader.stop()
        self._readers.clear()
        self._frame.stop()
        self._retry.stop()
        for strip in self.strips.values():
            strip.silence()
