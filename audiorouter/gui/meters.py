"""Level meters for the selected channel: before (In) and after (Out) its effects.

With "Show the highlighted effect" on (the default), In and Out are instead
what the effect highlighted in the chain receives and puts out, read from the
level taps between effects (`meter.effect_taps`). Without taps - the plugin
cannot be built here, or the host predates them - it falls back to the whole
channel and says why.

Each side has a bar per ear showing three things at once, as the owner chose:
the average level (a solid bar, ~300 ms like a VU meter), the peak (a bright
line that falls back slowly), a peak-hold tick, and a clip marker when a peak
reaches 0 dBFS.

Levels arrive from `meter.LevelReader` threads; the panel only ever touches
widgets on the GUI thread. Taps run only while the window is shown, and only
for the selected channel. An input channel's In meter opens the microphone -
the owner accepted that the desktop may show it as in use while it is selected.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPalette
from PyQt6.QtWidgets import QCheckBox, QGridLayout, QGroupBox, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..channels import Channel
from ..effects import EffectError, taps_available
from ..meter import (
    BLOCK,
    RATE,
    FileLevelReader,
    LevelReader,
    Levels,
    Tap,
    channel_taps,
    effect_taps,
)
from ..pwgraph import Graph, PwError
from .theme import Theme

FLOOR_DB = -60.0
#: Mean-square smoothing time constant: roughly how a VU needle integrates.
AVERAGE_TAU_S = 0.3
PEAK_FALL_DB_PER_S = 24.0
HOLD_S = 1.5
CLIP_SHOW_S = 2.0
#: A peak at or above this is a clip (0 dBFS, allowing for float rounding).
CLIP_LINEAR = 0.999
FRAME_MS = 33
#: A tap that stopped on its own is retried quickly once - a restart ends
#: every tap, and the new host is ready within a few hundred ms - then no more
#: often than RETRY_S (a mic that stays unplugged).
FIRST_RETRY_S = 0.3
RETRY_S = 2.0
#: Seconds of audio per reading.
BLOCK_S = BLOCK / RATE


def _db(linear: float) -> float:
    return 20 * math.log10(linear) if linear > 1e-6 else -120.0


class Ballistics:
    """What one bar shows, advanced by readings and by the passage of time."""

    def __init__(self) -> None:
        self.mean_square = 0.0
        self.peak_db = FLOOR_DB
        self.hold_db = FLOOR_DB
        self.hold_at = 0.0
        self.clip_at = -math.inf
        self.last = None  # time of the previous update

    @property
    def average_db(self) -> float:
        # RMS scaled by sqrt(2): a steady sine reads the same average as peak,
        # so the two marks line up on a tone instead of sitting 3 dB apart.
        return _db(math.sqrt(self.mean_square) * math.sqrt(2)) if self.mean_square > 0 else -120.0

    def feed(self, peak: float, mean_square: float, now: float) -> None:
        self._decay(now)
        # Exponential integration of power, per reading.
        dt = BLOCK_S
        alpha = 1 - math.exp(-dt / AVERAGE_TAU_S)
        self.mean_square += (mean_square - self.mean_square) * alpha
        db = _db(peak)
        if db >= self.peak_db:
            self.peak_db = db
        if db >= self.hold_db:
            self.hold_db, self.hold_at = db, now
        if peak >= CLIP_LINEAR:
            self.clip_at = now

    def tick(self, now: float) -> None:
        self._decay(now)

    def _decay(self, now: float) -> None:
        if self.last is None:
            self.last = now
            return
        dt = max(0.0, now - self.last)
        self.last = now
        self.peak_db = max(FLOOR_DB, self.peak_db - PEAK_FALL_DB_PER_S * dt)
        if now - self.hold_at > HOLD_S:
            self.hold_db = max(FLOOR_DB, self.hold_db - PEAK_FALL_DB_PER_S * dt)

    def clipped(self, now: float) -> bool:
        return now - self.clip_at <= CLIP_SHOW_S

    def silence(self) -> None:
        self.mean_square = 0.0
        self.peak_db = self.hold_db = FLOOR_DB
        self.hold_at = 0.0
        self.clip_at = -math.inf
        self.last = None


def fraction(db: float) -> float:
    """Where a level sits along the bar, 0 at FLOOR_DB and 1 at 0 dBFS."""
    return min(1.0, max(0.0, (db - FLOOR_DB) / -FLOOR_DB))


#: Meter zones, as on a studio meter: green below, amber up to, red above.
AMBER_FROM_DB = -18.0
RED_FROM_DB = -6.0
#: Segment pitch along the bar in pixels, one pixel of it a gap.
SEGMENT_PX = 4.0


def zone_colour(theme: Theme, db: float) -> QColor:
    if db >= RED_FROM_DB:
        return theme.meter_red
    if db >= AMBER_FROM_DB:
        return theme.meter_amber
    return theme.meter_green


#: dB marks printed under the meters.
SCALE_MARKS = (-48, -36, -24, -12, -6, 0)


def bar_span(rect: QRectF) -> QRectF:
    """The part of a bar's width the level uses; the rest is the clip marker.

    Shared by the bars and the scale under them, so the marks line up.
    """
    clip_w = min(8.0, rect.width() * 0.04)
    return QRectF(rect.left(), rect.top(), rect.width() - clip_w - 2, rect.height())


class LevelBar(QWidget):
    """One ear's bar. Colours come from the palette (see theme.py).

    `vertical` stands it up for a mixer strip, rising from the bottom; it is
    painted as the same bar, turned.
    """

    def __init__(self, parent: QWidget | None = None, vertical: bool = False) -> None:
        super().__init__(parent)
        self.state = Ballistics()
        self.vertical = vertical
        if vertical:
            self.setFixedWidth(7)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(9)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        now = time.monotonic()
        theme = Theme(self)
        palette = self.palette()
        painter = QPainter(self)
        if self.vertical:
            painter.translate(0, self.height())
            painter.rotate(-90)
            rect = QRectF(0, 0, self.height(), self.width()).adjusted(0.5, 0.5, -0.5, -0.5)
        else:
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillRect(rect, palette.color(QPalette.ColorRole.Base))

        bar = bar_span(rect)
        clip_w = rect.width() - bar.width() - 2

        # LED segments, lit up to the average, half-lit up to the peak, and
        # faintly visible above it so the zones always show.
        average = fraction(self.state.average_db)
        peak = fraction(self.state.peak_db)
        count = max(1, int(bar.width() // SEGMENT_PX))
        pitch = bar.width() / count
        for index in range(count):
            middle = (index + 0.5) / count
            colour = QColor(zone_colour(theme, FLOOR_DB * (1 - middle)))
            if middle > peak:
                colour.setAlpha(38)
            elif middle > average:
                colour.setAlpha(150)
            painter.fillRect(
                QRectF(bar.left() + index * pitch, bar.top(), max(1.0, pitch - 1), bar.height()),
                colour,
            )
        if self.state.hold_db > FLOOR_DB:
            painter.setPen(theme.text)
            x = bar.left() + bar.width() * fraction(self.state.hold_db)
            painter.drawLine(int(x), int(bar.top()), int(x), int(bar.bottom()))

        clip = QRectF(rect.right() - clip_w, rect.top(), clip_w, rect.height())
        painter.fillRect(clip, theme.meter_red if self.state.clipped(now) else palette.color(QPalette.ColorRole.Mid))
        painter.end()


class MeterScale(QWidget):
    """dB labels aligned under the bars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.fontMetrics().height() + 3)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        theme = Theme(self)
        painter = QPainter(self)
        font = painter.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.85))
        painter.setFont(font)
        painter.setPen(theme.dim)
        span = bar_span(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        metrics = painter.fontMetrics()
        for mark in SCALE_MARKS:
            x = span.left() + span.width() * fraction(mark)
            painter.drawLine(int(x), 0, int(x), 2)
            text = str(mark)
            w = metrics.horizontalAdvance(text)
            left = min(max(x - w / 2, span.left()), span.right() - w)
            painter.drawText(int(left), metrics.ascent() + 3, text)
        painter.end()


class MeterPanel(QGroupBox):
    """In and Out meters for one channel, and the taps that feed them."""

    #: From reader threads: (label, Levels). Delivered queued onto the GUI thread.
    _levels = pyqtSignal(str, object)
    _ended = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None, reader_factory=None, graph_source=None) -> None:
        super().__init__("Levels", parent)
        #: Returns a current graph. A retry fires with no graph event behind it
        #: (measured: after a restart settles there may be none), so it must be
        #: able to fetch one; the last graph passed to `follow` may be stale.
        self.graph_source = graph_source
        # Looked up at call time, so tests can replace LevelReader module-wide.
        self.reader_factory = reader_factory
        self.channel: Channel | None = None
        #: The effect highlighted in the chain, or None.
        self.effect_index: int | None = None
        self.active = False
        self._readers: dict[str, LevelReader] = {}
        self._graph: Graph | None = None
        self._failed_at: dict[str, float] = {}
        #: Endings in a row with no reading in between, per tap.
        self._failures: dict[str, int] = {}
        self._levels.connect(self._on_levels)
        self._ended.connect(self._on_ended)

        self.bars: dict[str, tuple[LevelBar, LevelBar]] = {}
        self.readouts: dict[str, QLabel] = {}
        self.titles: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for row, label in enumerate(("In", "Out")):
            title = QLabel(label, self)
            left, right = LevelBar(self), LevelBar(self)
            readout = QLabel("", self)
            readout.setMinimumWidth(64)
            readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            pair = QVBoxLayout()
            pair.setSpacing(2)
            pair.addWidget(left)
            pair.addWidget(right)
            grid.addWidget(title, row, 0)
            grid.addLayout(pair, row, 1)
            grid.addWidget(readout, row, 2)
            self.bars[label] = (left, right)
            self.readouts[label] = readout
            self.titles[label] = title
        self.scale = MeterScale(self)
        grid.addWidget(self.scale, 2, 1)
        dbfs = QLabel("dBFS", self)
        dbfs.setStyleSheet(Theme(self).css(Theme(self).dim))
        dbfs.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid.addWidget(dbfs, 2, 2)
        grid.setRowMinimumHeight(2, 0)
        self.hint = QLabel("", self)
        self.hint.setWordWrap(True)
        self.follow_effect = QCheckBox("Show the highlighted effect", self)
        self.follow_effect.setToolTip(
            "On: In and Out are what the effect highlighted below receives and puts out.\n"
            "Off: the whole channel, before and after all of its effects."
        )
        self.follow_effect.setChecked(True)
        self.follow_effect.toggled.connect(lambda _on: self._retry_now())
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(self.follow_effect)
        layout.addWidget(self.hint)

        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._retry_now)

        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)
        self._frame.timeout.connect(self._redraw)
        self._show_channel(None, running=False)

    # -- what to measure ---------------------------------------------------

    def follow(self, channel: Channel | None, graph: Graph | None) -> None:
        """Measure this channel, reopening taps whose target changed."""
        if channel is not self.channel:
            self._stop_readers()
            self._reset_bars()
            self._failed_at.clear()
            self._failures.clear()
        self.channel = channel
        self._graph = graph
        taps: tuple[Tap | None, Tap | None] = (None, None)
        effect = self._wanted_effect()
        if channel is not None and effect is not None:
            try:
                taps = effect_taps(channel, effect)
            except OSError:
                taps = (None, None)
        showing_effect = effect if any(taps) else None
        if channel is not None and graph is not None and not any(taps):
            try:
                taps = channel_taps(channel, graph)
            except (PwError, OSError):
                taps = (None, None)
        self._show_channel(channel, running=any(taps), effect=showing_effect,
                           wanted=effect)
        if self.active:
            self._apply_taps(taps)

    def show_effect(self, index: int | None, channel: Channel | None) -> None:
        """Measure around the effect highlighted in `channel`'s chain (None: none)."""
        self.effect_index = index if index is not None and index >= 0 else None
        self.follow(channel, self._graph if channel is self.channel else self._current_graph())

    def _wanted_effect(self) -> int | None:
        return self.effect_index if self.follow_effect.isChecked() else None

    def set_active(self, active: bool) -> None:
        """Run taps only while the window is shown."""
        if active == self.active:
            return
        self.active = active
        if active:
            self.follow(self.channel, self._current_graph())
        else:
            self._retry.stop()
            self._stop_readers()
            self._reset_bars()

    def stop(self) -> None:
        self.active = False
        self._retry.stop()
        self._stop_readers()

    def _current_graph(self) -> Graph | None:
        if self.graph_source is None:
            return self._graph
        try:
            return self.graph_source()
        except (PwError, OSError):
            return self._graph

    def _retry_now(self) -> None:
        if self.active:
            self.follow(self.channel, self._current_graph())

    def _apply_taps(self, taps: tuple[Tap | None, Tap | None]) -> None:
        now = time.monotonic()
        wanted = {tap.label: tap for tap in taps if tap is not None}
        for label in list(self._readers):
            reader = self._readers[label]
            tap = wanted.get(label)
            if tap is None or tap.key != reader.tap.key:
                reader.stop()
                del self._readers[label]
                self.bars[label][0].state.silence()
                self.bars[label][1].state.silence()
        for label, tap in wanted.items():
            if label in self._readers:
                continue
            delay = FIRST_RETRY_S if self._failures.get(label, 0) <= 1 else RETRY_S
            wait = delay - (now - self._failed_at.get(label, -math.inf))
            if wait > 0:
                if not self._retry.isActive():
                    self._retry.start(int(wait * 1000) + 50)
                continue
            factory = self.reader_factory or (FileLevelReader if tap.file else LevelReader)
            reader = factory(
                tap,
                lambda levels, l=label: self._levels.emit(l, levels),
                lambda l=label, k=tap.key: self._ended.emit(l, k),
            )
            try:
                reader.start()
            except (OSError, PwError):
                self._failed_at[label] = now
                self._failures[label] = self._failures.get(label, 0) + 1
                continue
            self._readers[label] = reader
        if self._readers and not self._frame.isActive():
            self._frame.start()
        elif not self._readers:
            self._frame.stop()
            self._redraw()

    # -- readings ----------------------------------------------------------

    @pyqtSlot(str, object)
    def _on_levels(self, label: str, levels: Levels) -> None:
        if label not in self._readers:
            return  # a late reading from a tap already stopped
        now = time.monotonic()
        self._failures.pop(label, None)
        left, right = self.bars[label]
        left.state.feed(levels.peak_l, levels.ms_l, now)
        right.state.feed(levels.peak_r, levels.ms_r, now)

    @pyqtSlot(str, object)
    def _on_ended(self, label: str, key: tuple) -> None:
        reader = self._readers.get(label)
        if reader is None or reader.tap.key != key:
            return
        reader.stop()
        del self._readers[label]
        self._failed_at[label] = time.monotonic()
        self._failures[label] = self._failures.get(label, 0) + 1
        for bar in self.bars[label]:
            bar.state.silence()
        delay = FIRST_RETRY_S if self._failures[label] <= 1 else RETRY_S
        if not self._retry.isActive():
            self._retry.start(int(delay * 1000) + 50)

    def _redraw(self) -> None:
        now = time.monotonic()
        for label, (left, right) in self.bars.items():
            left.state.tick(now)
            right.state.tick(now)
            left.update()
            right.update()
            hold = max(left.state.hold_db, right.state.hold_db)
            clipped = left.state.clipped(now) or right.state.clipped(now)
            if label not in self._readers:
                self.readouts[label].setText("")
            elif clipped:
                self.readouts[label].setText("CLIP")
                self.readouts[label].setStyleSheet(Theme(self).css(Theme(self).meter_red))
            else:
                self.readouts[label].setText("-inf dB" if hold <= FLOOR_DB else f"{hold:.1f} dB")
                self.readouts[label].setStyleSheet(Theme(self).css(Theme(self).dim))

    # -- presentation ------------------------------------------------------

    def _show_channel(
        self,
        channel: Channel | None,
        running: bool,
        effect: int | None = None,
        wanted: int | None = None,
    ) -> None:
        name = self._effect_name(channel, effect)
        # Where taps can never run there is nothing to choose, and no nagging.
        self.follow_effect.setHidden(channel is None or not channel.effects or not taps_available())
        if name is not None:
            self.setTitle(f"Levels - {name}")
            self.titles["In"].setToolTip(f"What {name} receives")
            self.titles["Out"].setToolTip(f"What {name} puts out")
        else:
            self.setTitle("Levels - whole channel" if channel is not None and channel.effects else "Levels")
            if channel is not None and channel.is_input:
                self.titles["In"].setToolTip("The microphone itself, before any effects")
                self.titles["Out"].setToolTip("The processed microphone apps record")
            else:
                self.titles["In"].setToolTip("What apps play into this channel, before its effects")
                self.titles["Out"].setToolTip("The sound after this channel's effects")
        lines = []
        if channel is None:
            pass
        elif not running:
            lines.append("Not running - nothing to measure.")
        else:
            if wanted is not None and effect is None and taps_available():
                lines.append(self._why_no_effect_levels(channel, wanted))
            if channel.is_input and effect is None:
                lines.append("Measuring opens the microphone while this channel is selected.")
        self.hint.setText("\n".join(lines))
        self.hint.setStyleSheet(Theme(self).css(Theme(self).dim))
        self.hint.setHidden(not self.hint.text())

    @staticmethod
    def _effect_name(channel: Channel | None, index: int | None) -> str | None:
        if channel is None or index is None or not 0 <= index < len(channel.effects):
            return None
        try:
            return channel.effects[index].spec.label
        except EffectError:
            return channel.effects[index].kind

    @staticmethod
    def _why_no_effect_levels(channel: Channel, index: int) -> str:
        effect = channel.effects[index] if 0 <= index < len(channel.effects) else None
        if effect is not None and not effect.enabled and effect.spec.unsatisfied():
            return "Showing the whole channel: this effect cannot run here."
        return ("Showing the whole channel: per-effect levels start when this channel "
                "next restarts (any change to its effects does that).")

    def _reset_bars(self) -> None:
        for left, right in self.bars.values():
            left.state.silence()
            right.state.silence()
        self._redraw()

    def _stop_readers(self) -> None:
        for reader in self._readers.values():
            reader.stop()
        self._readers.clear()
        self._frame.stop()
