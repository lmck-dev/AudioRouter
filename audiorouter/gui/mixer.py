"""The mixer: every channel as a strip on one desk, the way a console lays it out.

Left to right in signal order (owner, 1 Oct 2026): MICS - the real
microphones, each with its own volume and mute; CHANNELS - every channel,
whether it takes a mic or apps; GROUPS - channels other channels play into;
OUTPUTS - the real speakers and headphones, with their own volume and mute.
A mic channel's hidden companion (its mix, see `Config.ensure_companions`) is
never a strip: apps sent into it are listed on the mic channel's strip. Each strip reads top to bottom
in signal order: where the sound comes from, the inserts (the effect chain,
each one lit while it is on), where it goes, then the fader, the mute and a
meter of what the channel puts out.

Clicking any strip's INSERTS title folds the inserts on every strip at once,
so the faders stay level across the desk and get the room.

Under the fader's buttons, PAN balances the channel between left and right
and S solos it: every other channel of the same kind (outputs, or inputs) is
cut until the solo is released. Both act at the fader, so both are live.

Like a console there are two levels. TRIM, near the top, is the channel's
desktop volume - it acts before the effects, so it sets how hard they are
driven. The big FADER is after the effects (`Channel.fader_db`, a gain at the
end of the chain), so it changes the level without changing how a compressor
behaves. Both move live.

The strips are another view of the same settings as the Channels view - they
edit the same `Channel` objects and report through the same signals, so the
window's one debounce still decides when audio restarts. Anything that needs
more room than a strip has (an effect's knobs, echo cancellation, a new
channel's name) opens the Channels view on that channel.

Meters read each channel's last level tap (`meter.output_tap`), so an output
strip costs no recording stream. An input's chain idles until something
records it, so while the mixer is on screen each running input is recorded
by a `meter.Driver` that discards the sound: its strip always moves, and the
desktop shows the mic in use.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QMenu,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..channels import NOWHERE, Channel
from ..config import Config, desk_order, kind_label  # noqa: F401 - re-exported for the window
from ..effects import FADER_MAX_DB, FADER_OFF_DB, EffectError
from ..meter import Driver, FileLevelReader, LevelReader, Levels, Tap, output_tap
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

#: Shown when a mic is listened to on the speakers its own echo canceller
#: listens to (Engine.listen_cancelled; owner, 6 Oct 2026).
LISTEN_ECHO_TITLE = "Listening through the speakers"
LISTEN_ECHO_HELP = (
    "This mic has echo cancellation, which removes from the mic whatever comes out of your "
    "speakers. When you listen to the mic on those same speakers, your own voice comes out of "
    "them - so the canceller takes your voice for echo and cuts it, and it sounds choppy.\n\n"
    "To hear the mic properly, listen on headphones, or keep the speakers and the mic apart "
    "so they cannot hear each other. A recording made this way will be choppy too."
)
NOT_LISTENING = ""


def volume_db(volume: float) -> str:
    """The desktop's volume (cubic) as the dB a sound technician expects."""
    if volume <= 0:
        return "-inf dB"
    return f"{60 * math.log10(volume):+.1f} dB"


#: The fader's law: (travel 0..1000, dB), as a console scale is printed -
#: more travel near 0 dB, where fine moves matter, and off at the bottom.
FADER_TRAVEL = 1000
FADER_LAW = ((40, -60.0), (160, -40.0), (300, -30.0), (440, -20.0),
             (600, -10.0), (760, 0.0), (FADER_TRAVEL, FADER_MAX_DB))
FADER_MARKS = (10, 0, -10, -20, -30, -40, -60)
#: Below -60 the last stretch of travel runs down to -90, then off.
BOTTOM_DB = -90.0


def fader_to_db(position: int) -> float:
    if position <= 0:
        return FADER_OFF_DB
    first_pos, first_db = FADER_LAW[0]
    if position <= first_pos:
        return BOTTOM_DB + (first_db - BOTTOM_DB) * position / first_pos
    for (p0, d0), (p1, d1) in zip(FADER_LAW, FADER_LAW[1:]):
        if position <= p1:
            return d0 + (d1 - d0) * (position - p0) / (p1 - p0)
    return FADER_MAX_DB


def db_to_fader(db: float) -> int:
    if db <= FADER_OFF_DB or db <= BOTTOM_DB:
        return 0
    first_pos, first_db = FADER_LAW[0]
    if db <= first_db:
        return max(1, round(first_pos * (db - BOTTOM_DB) / (first_db - BOTTOM_DB)))
    for (p0, d0), (p1, d1) in zip(FADER_LAW, FADER_LAW[1:]):
        if db <= d1:
            return round(p0 + (p1 - p0) * (db - d0) / (d1 - d0))
    return FADER_TRAVEL


def fader_text(db: float) -> str:
    return "off" if db <= FADER_OFF_DB else f"{db:+.1f} dB"


def group_label(name: str, mic: bool = False) -> str:
    """How another channel appears in an OUT list (a mic channel's mix says so)."""
    return f"Into {name} (mic)" if mic else f"Into {name}"




#: The pan slider's travel each side of centre.
PAN_TRAVEL = 100


def pan_text(pan: float) -> str:
    """As a console prints it: C, or how far left or right out of 100."""
    amount = round(abs(pan) * PAN_TRAVEL)
    if amount == 0:
        return "C"
    return f"{'L' if pan < 0 else 'R'}{amount}"


class FaderScale(QWidget):
    """The fader's dB marks beside it, level with the handle's centre."""

    def __init__(self, fader: QSlider, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.fader = fader
        self.setFixedWidth(self.fontMetrics().horizontalAdvance("-60") + 2)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def paintEvent(self, _event) -> None:
        theme = Theme(self)
        painter = QPainter(self)
        font = painter.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.8))
        painter.setFont(font)
        painter.setPen(theme.dim)
        option = QStyleOptionSlider()
        self.fader.initStyleOption(option)
        handle = self.fader.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self.fader)
        travel = self.fader.height() - handle.height()
        top = self.fader.y() - self.y()  # the two sit side by side in one row
        metrics = painter.fontMetrics()
        for mark in FADER_MARKS:
            fraction = db_to_fader(mark) / self.fader.maximum()
            y = top + handle.height() / 2 + travel * (1 - fraction)
            text = f"+{mark}" if mark > 0 else str(mark)
            painter.drawText(self.width() - metrics.horizontalAdvance(text) - 1,
                             int(y + metrics.ascent() / 2 - 1), text)
        painter.end()


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
    fader_changed = pyqtSignal(str, float)
    mute_toggled = pyqtSignal(str, bool)
    pan_changed = pyqtSignal(str, float)
    solo_toggled = pyqtSignal(str, bool)
    effect_toggled = pyqtSignal(str, int, bool)
    effect_opened = pyqtSignal(str, int)
    add_effect = pyqtSignal(str)
    device_chosen = pyqtSignal(str, str)
    listen_chosen = pyqtSignal(str, str)
    open_settings = pyqtSignal(str)
    #: The INSERTS title was clicked: fold or unfold the inserts.
    inserts_clicked = pyqtSignal()

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
        # Every strip has the same two-row source area, so everything below it
        # lines up across the desk (owner, 1 Oct): row 1 is the mic's list on a
        # mic channel and the apps (or a group's members) on any other; row 2
        # is the apps mixed into a mic, kept even when empty.
        self.apps = caption("")
        self.apps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.apps.setFixedHeight(self.apps.fontMetrics().height() + 2)
        probe = QComboBox()  # parentless: only measured, never shown
        row_height = probe.sizeHint().height()
        probe.deleteLater()
        if self.source is not None:
            self.source.setFixedHeight(row_height)
            self.source_rows = (self.source, self.apps)
        else:
            self.apps.setFixedHeight(row_height)
            spare = caption("")
            spare.setFixedHeight(self.apps.fontMetrics().height() + 2)
            self.source_rows = (self.apps, spare)

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

        # The INSERTS title folds the list away, giving the fader the room.
        self.inserts_toggle = QToolButton(self)
        self.inserts_toggle.setFont(small)
        self.inserts_toggle.setAutoRaise(True)
        self.inserts_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.inserts_toggle.setStyleSheet(f"QToolButton {{ {theme.css(theme.dim)} padding: 0; }}")
        self.inserts_toggle.setToolTip("Show or hide the inserts on every strip")
        self.inserts_toggle.clicked.connect(self.inserts_clicked.emit)
        self.set_inserts_open(True)

        # Where it goes: the device for an output, listen-through for an input.
        self.route = QComboBox(self)
        if channel.is_input:
            self.route.setToolTip("Listen through (hear this input on an output channel)")
            self.route.activated.connect(self._listen_chosen)
        else:
            self.route.setToolTip("Plays through")
            self.route.activated.connect(self._device_chosen)
        # "i" beside LISTEN when the mic plays on the speakers its canceller listens to.
        self.listen_info = QToolButton(self)
        self.listen_info.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.listen_info.setAutoRaise(True)
        self.listen_info.setToolTip(LISTEN_ECHO_TITLE + ": the echo canceller will cut your voice. Click to see why.")
        self.listen_info.clicked.connect(
            lambda: QMessageBox.information(self, LISTEN_ECHO_TITLE, LISTEN_ECHO_HELP))
        self.listen_info.hide()

        # TRIM: the desktop's volume for this channel, before the effects.
        self.trim = QSlider(Qt.Orientation.Horizontal, self)
        self.trim.setRange(0, 100)
        self.trim.setToolTip("Trim: the channel's volume before its effects - the same one the "
                             "desktop's sound settings show. Sets how hard the effects are driven.")
        self.trim.valueChanged.connect(self._trim_moved)
        self.trim_label = caption("")
        self.trim_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # FADER: after the effects, like a console's.
        self.fader = QSlider(Qt.Orientation.Vertical, self)
        self.fader.setRange(0, FADER_TRAVEL)
        self.fader.setPageStep(40)
        self.fader.setToolTip("Fader: the channel's level after its effects")
        self.fader.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.fader.setValue(db_to_fader(channel.fader_db))
        self.fader.valueChanged.connect(self._fader_moved)
        self.fader_scale = FaderScale(self.fader, self)

        # PAN: balance after the fader. Double-click puts it back in the centre.
        self.pan = QSlider(Qt.Orientation.Horizontal, self)
        self.pan.setRange(-PAN_TRAVEL, PAN_TRAVEL)
        self.pan.setPageStep(10)
        self.pan.setValue(round(channel.pan * PAN_TRAVEL))
        self.pan.setToolTip("Pan: balance between left and right, after the fader.\n"
                            "Double-click to centre.")
        self.pan.valueChanged.connect(self._pan_moved)
        self.pan.installEventFilter(self)
        self.pan_label = caption(pan_text(channel.pan))
        self.pan_label.setFixedWidth(self.pan_label.fontMetrics().horizontalAdvance("L100") + 2)
        self.pan_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.cut_label = caption("cut by a solo")
        self.cut_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cut_label.setStyleSheet(theme.css(theme.warn))
        # Its room is kept while hidden, so every fader on the desk lines up.
        keep = self.cut_label.sizePolicy()
        keep.setRetainSizeWhenHidden(True)
        self.cut_label.setSizePolicy(keep)
        self.cut_label.setHidden(True)
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
        self.mute.toggled.connect(self._mute_toggled)
        self.solo = QPushButton("S", self)
        self.solo.setCheckable(True)
        self.solo.setChecked(channel.solo)
        self.solo.setToolTip("Solo: hear only this channel - every other "
                             + ("input" if channel.is_input else "output") + " is cut")
        self.solo.setFixedWidth(30)
        self.solo.toggled.connect(self._solo_toggled)
        self.mute.setFixedWidth(30)
        self.edit = QPushButton("Edit", self)
        self.edit.setToolTip("Open this channel in the Channels view")
        self.edit.clicked.connect(lambda: self.open_settings.emit(self.slug))

        faders = QHBoxLayout()
        faders.setSpacing(2)
        faders.addStretch(1)
        faders.addWidget(self.fader_scale)
        faders.addWidget(self.fader)
        faders.addSpacing(4)
        faders.addWidget(self.meter_l)
        faders.addWidget(self.meter_r)
        faders.addWidget(self.scale)
        faders.addStretch(1)

        trim_row = QHBoxLayout()
        trim_row.setSpacing(3)
        trim_row.addWidget(caption("TRIM"))
        trim_row.addWidget(self.trim, 1)

        pan_row = QHBoxLayout()
        pan_row.setSpacing(3)
        pan_row.addWidget(caption("PAN"))
        pan_row.addWidget(self.pan, 1)
        pan_row.addWidget(self.pan_label)

        buttons = QHBoxLayout()
        buttons.addWidget(self.mute)
        buttons.addWidget(self.solo)
        buttons.addWidget(self.edit, 1)

        # Top to bottom in signal order, the name first so a strip is always
        # identifiable even when the desk is too short for all of it.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self.name)
        layout.addWidget(self.kind)
        for row in self.source_rows:
            layout.addWidget(row)
        layout.addLayout(trim_row)
        layout.addWidget(self.trim_label)
        layout.addWidget(self.inserts_toggle)
        layout.addWidget(self.insert_area, 2)
        layout.addWidget(caption("LISTEN" if channel.is_input else "OUT"))
        route_row = QHBoxLayout()
        route_row.setSpacing(2)
        route_row.addWidget(self.route, 1)
        route_row.addWidget(self.listen_info)
        layout.addLayout(route_row)
        layout.addLayout(pan_row)
        layout.addLayout(buttons)
        layout.addWidget(self.cut_label)
        layout.addLayout(faders, 3)
        layout.addWidget(self.volume_label)
        layout.addWidget(self.level_label)

    def sizeHint(self) -> QSize:
        return QSize(STRIP_WIDTH, 520)

    # -- inserts -------------------------------------------------------------

    def set_inserts_open(self, open_: bool) -> None:
        """Show the inserts, or fold them to a title that still counts them."""
        self.insert_area.setHidden(not open_)
        self.inserts_toggle.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)
        count = len(self.channel.effects)
        on = sum(1 for e in self.channel.effects if e.enabled)
        if open_ or not count:
            self.inserts_toggle.setText("INSERTS")
        else:
            self.inserts_toggle.setText(f"INSERTS ({on}/{count} on)" if on != count else f"INSERTS ({count})")

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
        # Lit while on: a soft tint of the palette's highlight behind the
        # ordinary text colour, and a solid bar on the left like an LED. (A
        # full highlight with white text was hard to read - owner, 30 Sep.)
        palette = self.palette()
        accent = palette.highlight().color()
        tint = Theme.blend(accent, palette.button().color(), 0.3)
        button.setStyleSheet(
            "QToolButton { padding: 2px 2px 2px 6px; }"
            f"QToolButton:checked {{ background: {tint.name()};"
            f" color: {palette.buttonText().color().name()};"
            f" border: 1px solid {accent.name()}; border-left: 5px solid {accent.name()};"
            " border-radius: 3px; }"
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

    def show_inserts(self) -> None:
        """Light each insert as its effect is now, without rebuilding the strip."""
        for button, effect in zip(self.insert_buttons, self.channel.effects):
            if button.isChecked() != effect.enabled:
                button.blockSignals(True)
                button.setChecked(effect.enabled)
                button.blockSignals(False)
            try:
                unavailable = bool(effect.spec.unsatisfied())
            except EffectError:
                unavailable = True
            if unavailable:
                button.setEnabled(effect.enabled)  # an unavailable one can still be switched off
        self.set_inserts_open(not self.insert_area.isHidden())

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonDblClick and watched is self.pan:
            self.pan.setValue(0)
            return True
        if event.type() == QEvent.Type.MouseButtonDblClick and watched in self.insert_buttons:
            self.effect_opened.emit(self.slug, int(watched.property("effect_index")))
            return True
        return super().eventFilter(watched, event)

    # -- state from the window ---------------------------------------------

    def show_state(self, entry: dict | None, apps: list[str],
                   devices: list[tuple[str, str]], outputs: list[tuple[str, str]],
                   groups: list[tuple[str, str]] = (), members: list[str] = ()) -> None:
        """Refresh everything that can change without the strip being rebuilt.

        `groups` are the channels this one may play into; `members` name the
        channels playing into this one, which makes it a group.
        """
        self._fill_routes(entry, devices, outputs, groups)
        self.listen_info.setVisible(bool(entry and entry.get("listen_cancelled")))
        self.show_inserts()
        if members:
            self.apps.setText("from " + ", ".join(members) + (f"; {', '.join(apps)}" if apps else ""))
        elif apps:
            # On a mic channel these are apps sent into the mic, mixed with it.
            self.apps.setText(("+ " if self.channel.is_input else "") + ", ".join(apps))
        elif not self.channel.is_input:
            self.apps.setText("no apps playing")
        else:
            self.apps.setText("")
        # One line, shortened to fit; the whole list is in the tooltip.
        full = self.apps.text()
        self.apps.setToolTip(full)
        self.apps.setText(self.apps.fontMetrics().elidedText(full, Qt.TextElideMode.ElideRight,
                                                             STRIP_WIDTH - 16))
        volume = entry.get("volume") if entry else None
        running = bool(entry and entry.get("running"))
        self.trim.setEnabled(volume is not None)
        self.mute.setEnabled(volume is not None)
        if volume is None:
            self.trim_label.setText("off" if entry and not entry.get("enabled") else "not running")
        elif not self.trim.isSliderDown() and time.monotonic() - self._last_local_edit >= VOLUME_SETTLE_S:
            percent = round(volume * 100)
            self.trim.blockSignals(True)
            self.trim.setMaximum(150 if percent > 100 else 100)
            self.trim.setValue(percent)
            self.trim.blockSignals(False)
            self.trim_label.setText(volume_db(volume))
            self.mute.blockSignals(True)
            self.mute.setChecked(bool(entry.get("muted")))
            self.mute.blockSignals(False)
        self.show_fader()
        if not self.pan.isSliderDown():
            self.pan.blockSignals(True)
            self.pan.setValue(round(self.channel.pan * PAN_TRAVEL))
            self.pan.blockSignals(False)
            self.pan_label.setText(pan_text(self.channel.pan))
        self._show_mute()
        self.show_solo()
        self.name.setEnabled(running)

    def show_fader(self) -> None:
        """Show the channel's fader, unless the user is holding it."""
        # Only when it reads differently: writing the rounded dB back into the
        # slider being moved would snap it and could stall a wheel step.
        if not self.fader.isSliderDown() and round(fader_to_db(self.fader.value()), 1) != self.channel.fader_db:
            self.fader.blockSignals(True)
            self.fader.setValue(db_to_fader(self.channel.fader_db))
            self.fader.blockSignals(False)
        self.volume_label.setText(f"fader {fader_text(self.channel.fader_db)}")

    def show_solo(self) -> None:
        """Light S while soloed; say so while another solo cuts this one."""
        self.solo.blockSignals(True)
        self.solo.setChecked(self.channel.solo)
        self.solo.blockSignals(False)
        theme = Theme(self)
        self.solo.setStyleSheet(
            f"background: {theme.solo.name()}; color: {self.palette().base().color().name()};"
            if self.channel.solo else ""
        )
        self.cut_label.setHidden(not self.channel.solo_cut)

    def _fill_routes(self, entry: dict | None, devices, outputs, groups=()) -> None:
        present = entry["device_present"] if entry else True
        if self.source is not None:
            self._fill(self.source, [(FOLLOW_DEFAULT, "Default input"), *[(n, l) for n, l in devices]],
                       self.channel.device, present)
            self._fill(self.route, [(NOT_LISTENING, "Don't listen"), *outputs], self.channel.listen, True)
        else:
            choices = [(FOLLOW_DEFAULT, "Default output"), *devices, *groups,
                       (NOWHERE, "Nowhere (recording only)")]
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

    def _trim_moved(self, value: int) -> None:
        self._last_local_edit = time.monotonic()
        self.trim_label.setText(volume_db(value / 100))
        self.volume_changed.emit(self.slug, value / 100)

    def _fader_moved(self, value: int) -> None:
        db = round(fader_to_db(value), 1)
        self.volume_label.setText(f"fader {fader_text(db)}")
        self.fader_changed.emit(self.slug, db)

    def _pan_moved(self, value: int) -> None:
        pan = value / PAN_TRAVEL
        self.pan_label.setText(pan_text(pan))
        self.pan_changed.emit(self.slug, pan)

    def _solo_toggled(self, on: bool) -> None:
        self.solo_toggled.emit(self.slug, on)

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
            self.level_label.setStyleSheet(theme.css(theme.meter_red))
        else:
            self.level_label.setText("" if hold <= FLOOR_DB else f"peak {hold:.1f}")
            self.level_label.setStyleSheet(theme.css(theme.dim))

    def silence(self) -> None:
        for bar in (self.meter_l, self.meter_r):
            bar.state.silence()
            bar.update()
        self.level_label.setText("")


DEVICE_STRIP_WIDTH = 96


class DeviceStrip(QFrame):
    """A real microphone or output: its own volume and mute, and a meter.

    The volume is the device's, the one the desktop's sound settings show.
    For a mic it sets how hot every channel reading that mic is driven; for an
    output it is the last control before the sound leaves the computer.
    """

    volume_changed = pyqtSignal(str, float)
    mute_toggled = pyqtSignal(str, bool)

    def __init__(self, name: str, label: str, is_mic: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = name
        self.is_mic = is_mic
        self._last_local_edit = 0.0
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(DEVICE_STRIP_WIDTH)
        theme = Theme(self)
        small = self.font()
        small.setPointSizeF(max(6.5, small.pointSizeF() * 0.85))

        self.name = QLabel(self)
        bold = self.name.font()
        bold.setBold(True)
        self.name.setFont(bold)
        self.name.setText(self.name.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight,
                                                             DEVICE_STRIP_WIDTH - 14))
        self.name.setToolTip(label)
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.kind = QLabel("MIC" if is_mic else "OUTPUT", self)
        self.kind.setFont(small)
        self.kind.setStyleSheet(theme.css(theme.dim))
        self.kind.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.volume = QSlider(Qt.Orientation.Vertical, self)
        self.volume.setRange(0, 100)
        self.volume.setPageStep(10)
        self.volume.setMinimumHeight(140)
        self.volume.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.volume.setToolTip("This microphone's own volume, for every channel that uses it"
                               if is_mic else "This device's own volume: the last control before your ears")
        self.volume.valueChanged.connect(self._volume_moved)
        self.meter_l = LevelBar(self, vertical=True)
        self.meter_r = LevelBar(self, vertical=True)
        self.meter_l.setToolTip("What this microphone picks up" if is_mic else "What this device is playing")
        self.meter_r.setToolTip(self.meter_l.toolTip())
        self.volume_label = QLabel("", self)
        self.volume_label.setFont(small)
        self.volume_label.setStyleSheet(theme.css(theme.dim))
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mute = QPushButton("M", self)
        self.mute.setCheckable(True)
        self.mute.setFixedWidth(30)
        self.mute.setToolTip("Mute this microphone everywhere" if is_mic else "Mute this device")
        self.mute.toggled.connect(self._mute_toggled)

        bars = QHBoxLayout()
        bars.setSpacing(2)
        bars.addStretch(1)
        bars.addWidget(self.volume)
        bars.addSpacing(4)
        bars.addWidget(self.meter_l)
        bars.addWidget(self.meter_r)
        bars.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self.name)
        layout.addWidget(self.kind)
        layout.addStretch(1)
        layout.addWidget(self.mute, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(bars, 3)
        layout.addWidget(self.volume_label)

    def sizeHint(self) -> QSize:
        return QSize(DEVICE_STRIP_WIDTH, 520)

    def show_state(self, entry: dict) -> None:
        volume, muted = entry.get("volume"), entry.get("muted")
        self.volume.setEnabled(volume is not None)
        self.mute.setEnabled(muted is not None)
        if volume is None or self.volume.isSliderDown() \
                or time.monotonic() - self._last_local_edit < VOLUME_SETTLE_S:
            return
        percent = round(volume * 100)
        self.volume.blockSignals(True)
        self.volume.setMaximum(150 if percent > 100 else 100)
        self.volume.setValue(percent)
        self.volume.blockSignals(False)
        self.volume_label.setText(volume_db(volume))
        self.mute.blockSignals(True)
        self.mute.setChecked(bool(muted))
        self.mute.blockSignals(False)
        self._show_mute()

    def _show_mute(self) -> None:
        theme = Theme(self)
        self.mute.setStyleSheet(
            f"background: {theme.warn.name()}; color: {self.palette().base().color().name()};"
            if self.mute.isChecked() else "")

    def _volume_moved(self, value: int) -> None:
        self._last_local_edit = time.monotonic()
        self.volume_label.setText(volume_db(value / 100))
        self.volume_changed.emit(self.device, value / 100)

    def _mute_toggled(self, on: bool) -> None:
        self._last_local_edit = time.monotonic()
        self._show_mute()
        self.mute_toggled.emit(self.device, on)

    def tap(self) -> Tap:
        """What its meter reads: the mic itself, or what the output is playing."""
        source = self.device if self.is_mic else f"{self.device}.monitor"
        return Tap("Device", (f"--device={source}",), None)

    feed = ChannelStrip.feed

    def silence(self) -> None:
        for bar in (self.meter_l, self.meter_r):
            bar.state.silence()
            bar.update()

    def tick(self, now: float) -> None:
        for bar in (self.meter_l, self.meter_r):
            bar.state.tick(now)
            bar.update()


class MixerView(QWidget):
    """All channels as strips; the meters run only while the view is on screen."""

    volume_changed = pyqtSignal(str, float)
    fader_changed = pyqtSignal(str, float)
    mute_toggled = pyqtSignal(str, bool)
    pan_changed = pyqtSignal(str, float)
    solo_toggled = pyqtSignal(str, bool)
    effect_toggled = pyqtSignal(str, int, bool)
    effect_opened = pyqtSignal(str, int)
    add_effect = pyqtSignal(str)
    device_chosen = pyqtSignal(str, str)
    listen_chosen = pyqtSignal(str, str)
    open_settings = pyqtSignal(str)
    new_channel = pyqtSignal(bool)  # is_input
    #: A real device's own volume / mute (by node name).
    device_volume_changed = pyqtSignal(str, float)
    device_mute_toggled = pyqtSignal(str, bool)
    #: The inserts were folded (False) or unfolded (True), on every strip.
    inserts_toggled = pyqtSignal(bool)

    #: From reader threads: (slug, Levels). Delivered queued onto the GUI thread.
    _levels = pyqtSignal(str, object)
    _ended = pyqtSignal(str, object)
    _device_levels = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.strips: dict[str, ChannelStrip] = {}
        self.device_strips: dict[str, DeviceStrip] = {}
        self._mics: list[tuple[str, str]] = []
        self._outputs: list[tuple[str, str]] = []
        self._device_readers: dict[str, LevelReader] = {}
        self._device_levels.connect(self._on_device_levels)
        self.active = False
        #: One switch for the whole desk, so the faders stay level across strips.
        self.inserts_open = True
        self._channels: list[Channel] = []
        self._config = Config()
        self._signature: tuple = ()
        self._readers: dict[str, FileLevelReader] = {}
        self._drivers: dict[str, Driver] = {}
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

    def refresh(self, config: Config, status: dict) -> None:
        """Show the desk's channels; rebuild it only when its shape changed."""
        self._config = config
        # A mic channel's companion is plumbing, never a strip of its own.
        channels = [c for c in config.channels if not c.companion_of]
        mics = [(d["name"], d["label"]) for d in status.get("input_devices", [])]
        outs = [(d["name"], d["label"]) for d in status.get("devices", [])]
        signature = (tuple(
            (c.slug, c.name, c.kind, c.recordable, c.enabled, config.is_group(c),
             # Not e.enabled: switching an effect is shown in place (show_inserts).
             # Rebuilding for it reset the desk's scroll position on every click.
             tuple((e.kind, e.plugin) for e in c.effects))
            for c in channels
        ), tuple(mics), tuple(outs))
        if signature != self._signature or any(
            self.strips.get(c.slug) is None or self.strips[c.slug].channel is not c for c in channels
        ):
            self._signature = signature
            self._channels = channels
            self._mics, self._outputs = mics, outs
            self._rebuild()
        entries = {c["slug"]: c for c in status.get("channels", [])}
        apps: dict[str, list[str]] = {}
        for stream in status.get("streams", []):
            if stream.get("channel"):
                apps.setdefault(stream["channel"], []).append(stream["app"] or "?")
        outputs = [(c.slug, c.name) for c in channels if not c.is_input]
        for channel in channels:
            devices = status.get("input_devices" if channel.is_input else "devices", [])
            # Apps sent into a mic channel play into its companion.
            companion = config.companion(channel)
            playing = apps.get(companion.slug if companion is not None else channel.slug, [])
            self.strips[channel.slug].show_state(
                entries.get(channel.slug),
                sorted(set(playing)),
                [(d["name"], d["label"]) for d in devices],
                outputs,
                [(g.node_name, group_label(g.name, bool(g.companion_of))) for g in config.group_choices(channel)],
                [m.name for m in config.members_of(companion if companion is not None else channel)],
            )
        for device in [*status.get("input_devices", []), *status.get("devices", [])]:
            strip = self.device_strips.get(device["name"])
            if strip is not None:
                strip.show_state(device)
        self._follow_taps()

    def set_inserts_open(self, open_: bool) -> None:
        self.inserts_open = open_
        for strip in self.strips.values():
            strip.set_inserts_open(open_)

    def _toggle_inserts(self) -> None:
        self.set_inserts_open(not self.inserts_open)
        self.inserts_toggled.emit(self.inserts_open)

    def show_solo(self) -> None:
        """A solo changes what every strip of its kind shows, not just its own."""
        for strip in self.strips.values():
            strip.show_solo()

    def _rebuild(self) -> None:
        self._stop_readers()
        while self.row.count():
            item = self.row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.strips.clear()
        self.device_strips.clear()
        channels, groups = desk_order(self._config)

        self._add_devices("Mics", self._mics, is_mic=True)
        self.row.addWidget(self._group_header("Channels", "new"))
        for channel in channels:
            self._add_strip(channel, group=False)
        if groups:
            self.row.addWidget(self._group_header("Groups", None))
            for channel in groups:
                self._add_strip(channel, group=True)
        self._add_devices("Outputs", self._outputs, is_mic=False)
        self.row.addStretch(1)

    def _add_strip(self, channel: Channel, group: bool) -> None:
        strip = ChannelStrip(channel, self.desk)
        strip.kind.setText(kind_label(self._config, channel).upper())
        for name in ("volume_changed", "fader_changed", "mute_toggled", "pan_changed", "solo_toggled",
                     "effect_toggled", "effect_opened", "add_effect", "device_chosen",
                     "listen_chosen", "open_settings"):
            getattr(strip, name).connect(getattr(self, name).emit)
        strip.inserts_clicked.connect(self._toggle_inserts)
        strip.set_inserts_open(self.inserts_open)
        self.strips[channel.slug] = strip
        self.row.addWidget(strip)

    def _add_devices(self, title: str, devices: list[tuple[str, str]], is_mic: bool) -> None:
        if not devices:
            return
        self.row.addWidget(self._group_header(title, None))
        for name, label in devices:
            strip = DeviceStrip(name, label, is_mic, self.desk)
            strip.volume_changed.connect(self.device_volume_changed.emit)
            strip.mute_toggled.connect(self.device_mute_toggled.emit)
            self.device_strips[name] = strip
            self.row.addWidget(strip)

    def _group_header(self, title: str, new_kind: str | None) -> QWidget:
        """A narrow column naming the section; CHANNELS has a New button."""
        column = QWidget(self.desk)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title.upper(), column)
        label.setStyleSheet(Theme(self).css(Theme(self).dim))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        if new_kind is not None:
            new = QToolButton(column)
            new.setText("+")
            new.setToolTip("New channel")
            new.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(new)
            menu.addAction("Channel for a microphone", lambda: self.new_channel.emit(True))
            menu.addAction("Channel for apps", lambda: self.new_channel.emit(False))
            new.setMenu(menu)
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
                self._stop_driver(channel.slug)
                continue
            if channel.is_input and not self._drive(channel, tap):
                missing = True  # its driver ended (a restart): look again soon
            if reader is None:
                reader = FileLevelReader(
                    tap,
                    lambda levels, s=channel.slug: self._levels.emit(s, levels),
                    lambda s=channel.slug, k=tap.key: self._ended.emit(s, k),
                )
                reader.start()
                self._readers[channel.slug] = reader
        for name, strip in self.device_strips.items():
            reader = self._device_readers.get(name)
            if reader is None or not reader.running:
                if reader is not None:
                    reader.stop()
                reader = LevelReader(strip.tap(), lambda levels, n=name: self._device_levels.emit(n, levels))
                try:
                    reader.start()
                except Exception:  # noqa: BLE001 - a meter must never break the desk
                    continue
                self._device_readers[name] = reader
        if (self._readers or self._device_readers) and not self._frame.isActive():
            self._frame.start()
        # A host restarting has no tap for a moment; look again shortly.
        if missing and not self._retry.isActive():
            self._retry.start()
        elif not missing:
            self._retry.stop()

    def _drive(self, channel: Channel, tap: Tap) -> bool:
        """Keep one driver recording this input's running host; False if it ended."""
        driver = self._drivers.get(channel.slug)
        if driver is not None and driver.host == tap.host and driver.running:
            return True
        ended = driver is not None and driver.host == tap.host
        self._stop_driver(channel.slug)
        driver = Driver(channel)
        driver.start()
        self._drivers[channel.slug] = driver
        return not ended

    def _stop_driver(self, slug: str) -> None:
        driver = self._drivers.pop(slug, None)
        if driver is not None:
            driver.stop()

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

    @pyqtSlot(str, object)
    def _on_device_levels(self, name: str, levels: Levels) -> None:
        strip = self.device_strips.get(name)
        if strip is not None and name in self._device_readers:
            strip.feed(levels, time.monotonic())

    def _redraw(self) -> None:
        now = time.monotonic()
        for strip in [*self.strips.values(), *self.device_strips.values()]:
            strip.tick(now)
        if not self._readers and not self._device_readers:
            self._frame.stop()

    def _stop_readers(self) -> None:
        for reader in self._readers.values():
            reader.stop()
        self._readers.clear()
        for slug in list(self._drivers):
            self._stop_driver(slug)
        for reader in self._device_readers.values():
            reader.stop()
        self._device_readers.clear()
        for device in self.device_strips.values():
            device.silence()
        self._frame.stop()
        self._retry.stop()
        for strip in self.strips.values():
            strip.silence()
