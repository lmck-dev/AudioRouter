"""Evening out loud and quiet channels (auto-level, owner 23 Oct 2026).

A channel with `Channel.auto_level` carries an auto-level stage between its
effects and its fader (`effects._auto`). `Leveller`, in the login service,
reads the level tap just before that stage - what the effects put out, before
the fader and any turn-down - and slowly moves the stage so the channel sits
near `TARGET_DB`. The fader then sets where the channel sits relative to the
others: the "bias".

Slow on purpose, so it levels programmes and apps, not words or notes: it
judges the mean square over the last `WINDOW_S` of sound (blocks quieter than
`GATE_DB` are pauses and do not count, so silence never winds it up), and
moves at most once every `STEP_S`, down by up to `DOWN_STEP_DB` and up by
`UP_STEP_DB`, so a sudden loud channel comes down faster than a quiet one
creeps up. It starts moving only when it is `START_DB` off and stops within
`SETTLE_DB`, so it does not hunt. It never boosts past `MAX_BOOST_DB` nor
cuts past `MAX_CUT_DB`, never lifts the window's loudest peak above
`PEAK_CEILING_DB`, and holds still while the channel is turned down.

One exception to slow: sound that would come out more than `JUMP_DB` over
the target after a quiet stretch (a quiet scene, then an explosion or a loud
new video) is judged on its last `SHORT_S` alone and cut at once, over
`FAST_FADE_S`. Measured without it: a +8 dB boost let a loud passage through
at -0.6 dBFS for a second.

Each move is one live control change (`effects.auto_controls`) whose fade
runs in the audio thread. The gain chosen is written to the runtime file
(`channels.write_auto_levels`) so a restarted host starts where the old one
was.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .channels import Channel, ChannelError, read_auto_levels, write_auto_levels
from .ducking import Tap
from .effects import AUTO_FADE_S, TAP_URI, auto_controls, linear_to_db
from .meter import _running_nodes, meter_dir
from .pwgraph import Graph

POLL_S = 0.1
#: How often the tap files are looked up again (a channel restart moves them).
REFRESH_S = 1.0
#: Where a levelled channel is brought to, mean square in dBFS (before its fader).
TARGET_DB = -20.0
#: A block quieter than this is a pause, not sound.
GATE_DB = -50.0
WINDOW_S = 5.0
#: At least this much sound in the window before it is judged.
MIN_SOUND_S = 1.0
STEP_S = 1.0
DOWN_STEP_DB = 4.0
UP_STEP_DB = 1.5
START_DB = 3.0
SETTLE_DB = 0.5
MAX_BOOST_DB = 10.0
MAX_CUT_DB = 20.0
PEAK_CEILING_DB = -1.0
#: The fast cut: this far over the target, judged on this much recent sound.
JUMP_DB = 6.0
SHORT_S = 0.4
FAST_FADE_S = 0.1
#: One tap block: 1024 frames at 48 kHz.
BLOCK_S = 1024 / 48000

assert STEP_S > AUTO_FADE_S, "a move must have finished before the next starts"


def pre_fader_file(channel: Channel) -> str | None:
    """The tap just before the auto-level stage (the one before the output tap)."""
    host = channel.pid()
    slots = [int(n.get("control", {}).get("slot", 0)) for n in _running_nodes(channel)
             if n.get("plugin") == TAP_URI]
    if host is None or len(slots) < 2:
        return None
    return str(meter_dir() / f"{host}.{max(slots) - 1}")


def running_gain_db(channel: Channel) -> float | None:
    """The gain the running host's auto-level stage started at, or None without one."""
    for node in _running_nodes(channel):
        if node.get("name") == "auto_gain":
            add = float((node.get("control") or {}).get("Add", 1.0))
            return round(linear_to_db(add), 2)
    return None


def _db(mean_square: float) -> float:
    return 10.0 * math.log10(mean_square) if mean_square > 1e-12 else -120.0


def wanted_db(loud_db: float, peak_db: float) -> float:
    """The gain that brings `loud_db` to the target, within every limit."""
    want = TARGET_DB - loud_db
    want = min(want, MAX_BOOST_DB, PEAK_CEILING_DB - peak_db)
    return max(want, -MAX_CUT_DB)


@dataclass
class Levelled:
    level: Tap = field(default_factory=Tap)
    host: int | None = None
    #: The gain now, dB, and which end the stage's ramp sits at (0 or 1).
    gain_db: float = 0.0
    fade_at: int = 0
    moving: bool = False
    moved: float = float("-inf")
    #: (time, mean square, peak) of each block of sound in the window.
    recent: deque = field(default_factory=deque)

    #: How long the last move fades over.
    fade_s: float = AUTO_FADE_S

    def judged(self, now: float, span: float = WINDOW_S,
               least: float = MIN_SOUND_S) -> tuple[float, float] | None:
        """(loudness, loudest peak) in dB over the last `span`, or None with too little sound."""
        while self.recent and now - self.recent[0][0] > WINDOW_S:
            self.recent.popleft()
        blocks = [b for b in self.recent if now - b[0] <= span]
        if len(blocks) * BLOCK_S < least:
            return None
        loud = _db(sum(ms for _, ms, _ in blocks) / len(blocks))
        peak = 20.0 * math.log10(max(max(p for _, _, p in blocks), 1e-6))
        return loud, peak

    def step(self, now: float) -> tuple[float, float] | None:
        """(the next gain, its fade) to move to, or None to stay."""
        if now - self.moved < self.fade_s:
            return None   # the last move has not arrived yet
        short = self.judged(now, SHORT_S, SHORT_S / 4)
        if short is not None and short[0] + self.gain_db > TARGET_DB + JUMP_DB:
            self.moving = True
            return round(wanted_db(*short), 2), FAST_FADE_S
        if short is None and self.recent and now - self.recent[-1][0] <= SHORT_S:
            return None   # sound has just started: judge it once there is a little more
        judged = self.judged(now)
        if judged is None or now - self.moved < STEP_S:
            return None
        error = wanted_db(*judged) - self.gain_db
        if abs(error) < (SETTLE_DB if self.moving else START_DB):
            self.moving = False
            return None
        self.moving = True
        return round(self.gain_db + max(-DOWN_STEP_DB, min(UP_STEP_DB, error)), 2), AUTO_FADE_S


class Leveller:
    """Watches every auto-levelled channel and moves its stage."""

    def __init__(self, engine, is_down: Callable[[str], bool] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.engine = engine
        self.is_down = is_down or (lambda slug: False)
        self.log = log or (lambda line: None)
        self._graph: Graph | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._channels: dict[str, Levelled] = {}
        self._refreshed = float("-inf")

    def use_graph(self, graph: Graph) -> None:
        self._graph = graph

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="leveller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stopping.wait(POLL_S):
            try:
                self.tick(time.monotonic())
            except Exception as exc:  # never let one bad read end the watching
                self.log(f"auto-level: {exc}")

    def tick(self, now: float) -> None:
        config = self.engine.config
        levelled = [] if config.bypass else [c for c in config.channels if c.auto_level and c.enabled]
        slugs = {c.slug for c in levelled}
        if set(self._channels) - slugs:
            for slug in set(self._channels) - slugs:
                del self._channels[slug]
            self._save()
        if now - self._refreshed >= REFRESH_S:
            self._refreshed = now
            for channel in levelled:
                state = self._channels.setdefault(channel.slug, Levelled())
                state.level.follow(pre_fader_file(channel))
                host = channel.pid()
                if host != state.host:
                    # A new host starts with its ramp at 0, at the gain it was rendered with.
                    start = running_gain_db(channel)
                    state.host, state.fade_at, state.moving = host, 0, False
                    state.gain_db = start if start is not None else 0.0
                    state.moved = now
        for channel in levelled:
            state = self._channels.get(channel.slug)
            if state is None:
                continue
            for reading in state.level.new():
                ms = (reading.ms_l + reading.ms_r) / 2
                if _db(ms) > GATE_DB:
                    state.recent.append((now, ms, max(reading.peak_l, reading.peak_r)))
            if self.is_down(channel.slug):
                continue
            move = state.step(now)
            if move is not None:
                self._move(channel, state, *move, now=now)

    def _move(self, channel: Channel, state: Levelled, new_db: float, fade_s: float, now: float) -> None:
        values = auto_controls(state.fade_at, state.gain_db, new_db, fade_s)
        graph = self._graph or Graph.snapshot()
        node = channel.control_node(graph) or channel.control_node(Graph.snapshot())
        try:
            if node is None:
                raise ChannelError(f"channel {channel.slug!r} is not in the graph")
            channel._send_props(node.id, values)
        except ChannelError as exc:
            self.log(f"auto-level: {exc}")
            return
        state.gain_db, state.fade_at, state.moved, state.fade_s = new_db, 1 - state.fade_at, now, fade_s
        self._save()
        self.log(f"auto-level: {channel.name} {new_db:+.1f} dB")

    def _save(self) -> None:
        levels = {slug: s.gain_db for slug, s in self._channels.items()}
        if levels != read_auto_levels():
            write_auto_levels(levels)

    def __enter__(self) -> Leveller:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
