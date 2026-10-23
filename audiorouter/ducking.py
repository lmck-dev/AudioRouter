"""Turning channels down while a priority channel plays (owner, 23 Oct 2026).

The wake word's voice over a game: a priority channel (`Channel.ducks`) lists
the output channels it turns down, and by how much (`Channel.duck_db`). Each
of those carries a duck stage after its fader (`effects._duck`), an in-graph
ramp, so going down and coming back are each ONE live control change and the
fade itself runs in the audio thread - no steps, no clicks.

`Ducker` runs in the login service. It reads the priority channel's first
level tap (before its effects and fader, so the voice's own fader does not
move the trigger) every `POLL_S`; a block above `THRESHOLD` counts as playing,
and the others stay down until it has been quiet for `HOLD_S`, so the gaps
between sentences do not let the game swell back in. A tap block is 21 ms, so
the first ~50 ms of speech plus the 80 ms fade play over the full game.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .channels import Channel, ChannelError
from .effects import TAP_URI, db_to_linear, duck_controls
from .meter import TAP_FILE_SIZE, _running_nodes, meter_dir, read_tap
from .pwgraph import Graph

POLL_S = 0.03
#: A peak above this (-50 dBFS) is the priority channel playing.
THRESHOLD = db_to_linear(-50.0)
HOLD_S = 1.2
#: How often the tap files are looked up again (a channel restart moves them).
REFRESH_S = 1.0


def trigger_file(channel: Channel) -> str | None:
    """The priority channel's first level tap file, or None when it has none running."""
    host = channel.pid()
    slots = [int(n.get("control", {}).get("slot", 0)) for n in _running_nodes(channel)
             if n.get("plugin") == TAP_URI]
    if host is None or not slots:
        return None
    return str(meter_dir() / f"{host}.{min(slots)}")


def _seq(path: str | None) -> int:
    """How many readings a tap file holds so far (0 when there is none yet)."""
    if path is None:
        return 0
    try:
        with open(path, "rb") as fh:
            return read_tap(fh.read(TAP_FILE_SIZE), 0)[0]
    except OSError:
        return 0


class Ducker:
    """Watches every priority channel and turns its ducked channels down and up."""

    def __init__(self, engine, log: Callable[[str], None] | None = None) -> None:
        self.engine = engine
        self.log = log or (lambda line: None)
        self._graph: Graph | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="duck")
        #: priority slug -> (tap file, last seq read, last time heard)
        self._watch: dict[str, list] = {}
        #: ducked slug -> (host pid it was sent to, down?)
        self._sent: dict[str, tuple[int | None, bool]] = {}
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
        self._apply({slug: False for slug, (_, down) in self._sent.items() if down}, wait=True)
        self._pool.shutdown(wait=True)

    def _run(self) -> None:
        while not self._stopping.wait(POLL_S):
            try:
                self.tick(time.monotonic())
            except Exception as exc:  # never let one bad read end the watching
                self.log(f"ducking: {exc}")

    def tick(self, now: float) -> None:
        config = self.engine.config
        priorities = [] if config.bypass else config.priority_channels()
        if now - self._refreshed >= REFRESH_S:
            self._refreshed = now
            for channel in priorities:
                entry = self._watch.setdefault(channel.slug, [None, 0, -HOLD_S - 1.0])
                path = trigger_file(channel)
                if path != entry[0]:
                    # Readings already in the file are history, not news.
                    entry[0], entry[1] = path, _seq(path)
        playing: set[str] = set()
        for channel in priorities:
            entry = self._watch.get(channel.slug)
            if entry is None or entry[0] is None:
                continue
            try:
                with open(entry[0], "rb") as fh:
                    data = fh.read(TAP_FILE_SIZE)
            except OSError:
                data = b""
            entry[1], levels = read_tap(data, entry[1])
            if any(max(r.peak_l, r.peak_r) > THRESHOLD for r in levels):
                entry[2] = now
            if now - entry[2] < HOLD_S:
                playing.add(channel.slug)
        down = {slug for c in priorities if c.slug in playing for slug in config.ducked_by(c)}
        wanted: dict[str, bool] = {}
        for channel in config.channels:
            if channel.ducked_db <= 0.0:
                self._sent.pop(channel.slug, None)
                continue
            state = channel.slug in down
            host = channel.pid()
            # A restarted host starts up; it only needs telling when it should be down.
            sent = self._sent.get(channel.slug, (host, False))
            if sent != (host, state):
                wanted[channel.slug] = state
        self._apply(wanted)

    def _apply(self, wanted: dict[str, bool], wait: bool = False) -> None:
        if not wanted:
            return
        graph = self._graph or Graph.snapshot()
        config = self.engine.config
        jobs = []
        for slug, state in wanted.items():
            try:
                channel = config.channel(slug)
            except Exception:
                continue
            self._sent[slug] = (channel.pid(), state)
            jobs.append(self._pool.submit(self._send, channel, state, graph))
        if wait:
            for job in jobs:
                job.result()

    def _send(self, channel: Channel, down: bool, graph: Graph) -> None:
        node = channel.control_node(graph)
        if node is None:
            node = channel.control_node(Graph.snapshot())
        if node is None:
            self._sent.pop(channel.slug, None)   # try again next tick
            return
        try:
            channel._send_props(node.id, duck_controls(down))
        except ChannelError as exc:
            self._sent.pop(channel.slug, None)
            self.log(f"ducking: {exc}")
            return
        self.log(f"ducking: {channel.name} {'down' if down else 'up'}")

    def __enter__(self) -> Ducker:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
