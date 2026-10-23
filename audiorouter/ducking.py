"""Turning channels down while a priority channel plays (owner, 23 Oct 2026).

The wake word's voice over a game: a priority channel (`Channel.ducks`) lists
the output channels it turns down, and the most it may (`Channel.duck_db`).
Each of those carries a duck stage after its fader (`effects._duck`), an
in-graph ramp, so going down and coming back are each ONE live control change
and the fade itself runs in the audio thread - no steps, no clicks.

Only as far as needed (owner): a channel goes down just enough to sit
`HEADROOM_DB` under the voice, so a loud game drops a lot, a quiet one a
little, and one that is already well under the voice is left alone. Loudness
is the mean square of what each channel puts out (its last level tap) over the
`WINDOW_S` before the voice started, plus the faders of the groups it plays
through; the voice's is a running average of its own loud blocks. The depth
is decided when the voice starts and held until the channel is fully back up.

`Ducker` runs in the login service. It reads the priority channel's first
level tap (before its effects and fader, so the voice's own fader does not
move the trigger) every `POLL_S`; a block above `THRESHOLD` counts as playing,
and the others stay down until it has been quiet for `HOLD_S`, so the gaps
between sentences do not let the game swell back in. A tap block is 21 ms, so
the first ~50 ms of speech plus the 80 ms fade play over the full game.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .channels import Channel, ChannelError
from .effects import DUCK_RELEASE_S, TAP_URI, db_to_linear, duck_controls
from .meter import TAP_FILE_SIZE, _running_nodes, meter_dir, read_tap
from .pwgraph import Graph

POLL_S = 0.03
#: A peak above this (-50 dBFS) is the priority channel playing.
THRESHOLD = db_to_linear(-50.0)
HOLD_S = 1.2
#: How often the tap files are looked up again (a channel restart moves them).
REFRESH_S = 1.0
#: The others are kept this far under the voice.
HEADROOM_DB = 10.0
#: How much of a channel's recent sound counts as "how loud it is".
WINDOW_S = 3.0
#: Less than this is not worth a fade.
MIN_DUCK_DB = 1.0
#: The voice's loudness before it has been heard: a typical TTS level.
DEFAULT_VOICE_DB = -20.0
#: A voice block quieter than this (mean square, dBFS) is a gap, not speech.
SPEECH_DB = -45.0
SILENT_DB = -120.0


def _tap_file(channel: Channel, last: bool) -> str | None:
    host = channel.pid()
    slots = [int(n.get("control", {}).get("slot", 0)) for n in _running_nodes(channel)
             if n.get("plugin") == TAP_URI]
    if host is None or not slots:
        return None
    return str(meter_dir() / f"{host}.{max(slots) if last else min(slots)}")


def trigger_file(channel: Channel) -> str | None:
    """The priority channel's first level tap file, or None when it has none running."""
    return _tap_file(channel, last=False)


def level_file(channel: Channel) -> str | None:
    """A channel's last level tap: what it puts out, after its fader."""
    return _tap_file(channel, last=True)


def _db(mean_square: float) -> float:
    return 10.0 * math.log10(mean_square) if mean_square > 1e-12 else SILENT_DB


@dataclass
class Tap:
    """New readings from one tap file, read as they arrive."""

    path: str | None = None
    seq: int = 0

    def follow(self, path: str | None) -> None:
        if path != self.path:
            # Readings already in the file are history, not news.
            self.path, self.seq = path, self.read_seq(path)

    @staticmethod
    def read_seq(path: str | None) -> int:
        if path is None:
            return 0
        try:
            with open(path, "rb") as fh:
                return read_tap(fh.read(TAP_FILE_SIZE), 0)[0]
        except OSError:
            return 0

    def new(self) -> list:
        if self.path is None:
            return []
        try:
            with open(self.path, "rb") as fh:
                data = fh.read(TAP_FILE_SIZE)
        except OSError:
            return []
        self.seq, levels = read_tap(data, self.seq)
        return levels


@dataclass
class Priority:
    trigger: Tap = field(default_factory=Tap)
    level: Tap = field(default_factory=Tap)
    heard: float = -HOLD_S - 1.0
    #: Running average of its loud blocks, dBFS; None until it has spoken.
    voice_db: float | None = None


@dataclass
class Ducked:
    level: Tap = field(default_factory=Tap)
    #: (time, mean square) while fully up: how loud it is when left alone.
    recent: deque = field(default_factory=deque)
    host: int | None = None
    down: bool = False
    depth: float = 0.0
    #: When it was last sent back up: it is fully up DUCK_RELEASE_S later.
    up_at: float = float("-inf")

    def loudness(self) -> float:
        if not self.recent:
            return SILENT_DB
        return _db(sum(ms for _, ms in self.recent) / len(self.recent))


class Ducker:
    """Watches every priority channel and turns its ducked channels down and up."""

    def __init__(self, engine, log: Callable[[str], None] | None = None) -> None:
        self.engine = engine
        self.log = log or (lambda line: None)
        self._graph: Graph | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="duck")
        self._priorities: dict[str, Priority] = {}
        self._ducked: dict[str, Ducked] = {}
        self._refreshed = float("-inf")   # look the files up on the first tick

    def use_graph(self, graph: Graph) -> None:
        """The latest graph, from the service's monitor: where to find each sink."""
        self._graph = graph

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="ducker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._apply({slug: None for slug, state in self._ducked.items() if state.down}, wait=True)
        self._pool.shutdown(wait=True)

    def _run(self) -> None:
        while not self._stopping.wait(POLL_S):
            try:
                self.tick(time.monotonic())
            except Exception as exc:  # never let one bad read end the watching
                self.log(f"ducking: {exc}")

    def _offset(self, channel: Channel) -> float:
        """The faders a channel's sound still passes after its own (first chain)."""
        config = self.engine.config
        return sum(g.fader_db for g in config.groups_below(channel))

    def tick(self, now: float) -> None:
        config = self.engine.config
        priorities = [] if config.bypass else config.priority_channels()
        ducked = [c for c in config.channels if c.ducked_db > 0.0]
        for slug in set(self._ducked) - {c.slug for c in ducked}:
            del self._ducked[slug]
        if now - self._refreshed >= REFRESH_S:
            self._refreshed = now
            for channel in priorities:
                state = self._priorities.setdefault(channel.slug, Priority())
                state.trigger.follow(trigger_file(channel))
                state.level.follow(level_file(channel))
            for channel in ducked:
                state = self._ducked.setdefault(channel.slug, Ducked())
                state.level.follow(level_file(channel))
                host = channel.pid()
                if host != state.host:
                    # A restarted host starts up, at its full depth.
                    state.host, state.down, state.up_at = host, False, float("-inf")

        # How loud each ducked channel is while left alone.
        for channel in ducked:
            state = self._ducked.setdefault(channel.slug, Ducked())
            levels = state.level.new()
            if state.down or now - state.up_at < DUCK_RELEASE_S:
                continue
            for reading in levels:
                state.recent.append((now, (reading.ms_l + reading.ms_r) / 2))
            while state.recent and now - state.recent[0][0] > WINDOW_S:
                state.recent.popleft()

        # Which priority channels are playing, and how loud they speak.
        playing: list[Channel] = []
        for channel in priorities:
            state = self._priorities.get(channel.slug)
            if state is None:
                continue
            if any(max(r.peak_l, r.peak_r) > THRESHOLD for r in state.trigger.new()):
                state.heard = now
            for reading in state.level.new():
                db = _db((reading.ms_l + reading.ms_r) / 2)
                if db > SPEECH_DB:
                    state.voice_db = db if state.voice_db is None else 0.9 * state.voice_db + 0.1 * db
            if now - state.heard < HOLD_S:
                playing.append(channel)

        # How far each ducked channel must go: enough to sit under every voice.
        needed: dict[str, float] = {}
        for channel in playing:
            state = self._priorities[channel.slug]
            voice = (state.voice_db if state.voice_db is not None else DEFAULT_VOICE_DB) \
                + self._offset(channel)
            for slug in config.ducked_by(channel):
                if slug not in self._ducked:
                    continue
                other = config.channel(slug)
                loud = self._ducked[slug].loudness() + self._offset(other)
                need = min(loud - (voice - HEADROOM_DB), channel.duck_db)
                needed[slug] = max(needed.get(slug, 0.0), need)

        wanted: dict[str, float | None] = {}
        for slug, state in self._ducked.items():
            need = needed.get(slug)
            if need is not None and not state.down and need >= MIN_DUCK_DB:
                wanted[slug] = need
            elif need is None and state.down:
                wanted[slug] = None
        self._apply(wanted, now=now)

    def _apply(self, wanted: dict[str, float | None], wait: bool = False, now: float | None = None) -> None:
        """Send each channel down by its depth, or back up (None)."""
        if not wanted:
            return
        now = time.monotonic() if now is None else now
        config = self.engine.config
        jobs = []
        for slug, depth in wanted.items():
            try:
                channel = config.channel(slug)
            except Exception:
                continue
            state = self._ducked.setdefault(slug, Ducked())
            if depth is None:
                state.down, state.up_at = False, now
                values = duck_controls(False)
            else:
                # A new depth only once it is fully up again, or it would jump.
                if now - state.up_at >= DUCK_RELEASE_S:
                    state.depth = depth
                state.down = True
                values = duck_controls(True, state.depth)
            jobs.append(self._pool.submit(self._send, channel, values, state))
        if wait:
            for job in jobs:
                job.result()

    def _send(self, channel: Channel, values: dict[str, float], state: Ducked) -> None:
        graph = self._graph or Graph.snapshot()
        node = channel.control_node(graph) or channel.control_node(Graph.snapshot())
        try:
            if node is None:
                raise ChannelError(f"channel {channel.slug!r} is not in the graph")
            channel._send_props(node.id, values)
        except ChannelError as exc:
            # Not sent: the next tick sees the old state and tries again.
            state.down = not state.down
            self.log(f"ducking: {exc}")
            return
        if state.down:
            self.log(f"ducking: {channel.name} down {state.depth:.1f} dB")
        else:
            self.log(f"ducking: {channel.name} up")

    def __enter__(self) -> Ducker:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
