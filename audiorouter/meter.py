"""Signal levels for a channel, before and after its effects.

Each side of a channel is read by a `parec` recording stream - a *tap* - and
turned into a peak and a mean square per block. No audio library is needed:
`parec` delivers raw float32, and `array` plus the builtins handle a 21 ms
block of 48 kHz stereo in well under a millisecond.

Where each side is read (measured in the lab, 15 Sep 2026, a -20 dB channel:
In read -6.02 dB, Out -26.02 dB):

- output channel: In is its sink's monitor; Out is its own playback stream
  (`--monitor-stream=<object.serial>` - pipewire-pulse numbers sink inputs by
  serial) or, for a virtual cable, its recording source;
- input channel: In is the microphone itself (so metering opens it); Out is the
  virtual microphone.

Taps never follow a vanished target (`node.dont-reconnect`, `node.dont-fallback`)
- a meter must not wander onto the real mic or another device - and are marked
with `METER_KEY` so a restart's handover does not try to move them (a move of a
stream that refuses moves would make every restart wait its full timeout).
Whoever shows the levels reopens a tap when its `Tap.key` changes.

Inside a chain, levels come from our own level tap plugin (`native/meter/`)
instead: one listens at every boundary between effects, and each publishes a
ring of readings in a small file, `<runtime>/meters/<host pid>.<slot>`
(`FileLevelReader`). PipeWire lists a plugin's output controls but never
refreshes them (measured 30 Sep 2026), so a file is the way out. Measured on a
-20 dB tone through gain -6 / compressor / gain -10: -20.00, -26.00, -26.00,
-36.00 dB, and the host's CPU did not change.
"""

from __future__ import annotations

import array
import math
import struct
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .channels import Channel, _pid_alive, runtime_dir
from .effects import TAP_URI, output_slot, tap_slots
from .pwgraph import METER_KEY, Graph, require_tools

RATE = 48000
#: Frames per reading: ~21 ms, so a meter redrawn at 30 fps always has news.
BLOCK = 1024
DEFAULT_SOURCE = "@DEFAULT_SOURCE@"


@dataclass(frozen=True)
class Levels:
    """One block's peak (0..1+, linear) and mean square, per side."""

    peak_l: float
    peak_r: float
    ms_l: float
    ms_r: float


def measure(block: bytes) -> Levels:
    """Peak and mean square of interleaved float32 stereo."""
    samples = array.array("f")
    samples.frombytes(block[: len(block) // 8 * 8])
    if sys.byteorder != "little":  # pragma: no cover - parec sends float32le
        samples.byteswap()
    left, right = samples[0::2], samples[1::2]
    if not left:
        return Levels(0.0, 0.0, 0.0, 0.0)

    def side(values: array.array) -> tuple[float, float]:
        peak = max(max(values), -min(values))
        return peak, math.fsum(v * v for v in values) / len(values)

    (peak_l, ms_l), (peak_r, ms_r) = side(left), side(right)
    return Levels(peak_l, peak_r, ms_l, ms_r)


def to_db(linear: float, floor: float = -120.0) -> float:
    return 20 * math.log10(linear) if linear > 10 ** (floor / 20) else floor


@dataclass(frozen=True)
class Tap:
    """Where to read one side of a channel. `key` changes when the tap must be reopened."""

    label: str
    args: tuple[str, ...]
    #: The channel's host pid: a restart replaces every node a tap reads.
    host: int | None
    #: A level tap's file inside the chain; empty for a `parec` tap.
    file: str = ""

    @property
    def key(self) -> tuple:
        return (self.args, self.host, self.file)


def channel_taps(channel: Channel, graph: Graph) -> tuple[Tap | None, Tap | None]:
    """The (In, Out) taps for a running channel, or None for a side not in the graph."""
    host = channel.pid()
    if host is None or channel.sink_node(graph) is None:
        return None, None
    if channel.is_input:
        mic = channel.device or DEFAULT_SOURCE
        return (Tap("In", (f"--device={mic}",), host),
                Tap("Out", (f"--device={channel.node_name}",), host))
    tap_in = Tap("In", (f"--device={channel.node_name}.monitor",), host)
    if channel.recordable:
        tap_out = Tap("Out", (f"--device={channel.recording_name}",), host)
    else:
        playback = channel.playback_node(graph)
        if playback is None or playback.serial is None:
            return tap_in, None
        tap_out = Tap("Out", (f"--monitor-stream={playback.serial}",), host)
    return tap_in, tap_out


def meter_dir() -> Path:
    """Where level taps publish; the plugin builds the same path from its env."""
    return runtime_dir() / "meters"


def host_has_taps(channel: Channel) -> bool:
    """Was the running host started with level taps in its chain?

    Not the same as "could it have them": a host started before the plugin was
    built, or by an older version, has none until its next restart, and its
    files would simply never appear.
    """
    conf = channel.running_config()
    if conf is None:
        return False
    for module in conf.get("context.modules", []):
        graph = (module.get("args") or {}).get("filter.graph") or {}
        if any(node.get("plugin") == TAP_URI for node in graph.get("nodes", [])):
            return True
    return False


def effect_taps(channel: Channel, index: int) -> tuple[Tap | None, Tap | None]:
    """(In, Out) around the channel's `index`-th effect, read from level taps.

    None for both when the channel is not running, its host has no taps, or
    that effect is not rendered. Needs no graph: the host pid names the files.
    A tap's file only appears once audio has flowed through it, which the
    reader waits for.
    """
    host = channel.pid()
    slots = tap_slots(channel.effects)
    if host is None or not 0 <= index < len(slots) or slots[index] is None:
        return None, None
    if not host_has_taps(channel):
        return None, None
    before, after = slots[index]
    return tuple(  # type: ignore[return-value]
        Tap(label, (), host, str(meter_dir() / f"{host}.{slot}"))
        for label, slot in (("In", before), ("Out", after))
    )


def output_tap(channel: Channel) -> Tap | None:
    """What the channel puts out, after all its effects, read from its last tap.

    A mixer strip's meter. No `parec`, so metering every strip at once costs
    nothing and never opens a microphone: an input channel reads only while
    something records it (its capture is passive until then).
    """
    host = channel.pid()
    if host is None or not host_has_taps(channel):
        return None
    return Tap("Out", (), host, str(meter_dir() / f"{host}.{output_slot(channel.effects)}"))


def sweep_stale(directory: Path | None = None) -> None:
    """Remove the files of hosts that died without cleaning up (SIGKILL)."""
    directory = directory or meter_dir()
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        pid = entry.name.split(".", 1)[0]
        if pid.isdigit() and not _pid_alive(int(pid)):
            try:
                entry.unlink()
            except OSError:
                pass


#: The plugin's file: magic, entries written, then a ring of readings
#: {peak_l, peak_r, ms_l, ms_r}, one per 1024 frames. See audiorouter_meter.c.
TAP_MAGIC = 0x4D525241
TAP_RING = 64
_TAP_HEADER = struct.Struct("<II")
_TAP_ENTRY = struct.Struct("<4f")
TAP_FILE_SIZE = _TAP_HEADER.size + TAP_RING * _TAP_ENTRY.size
#: How often a file is read: faster than the ~21 ms blocks arrive is pointless.
POLL_S = 0.03


def read_tap(data: bytes, after: int) -> tuple[int, list[Levels]]:
    """The readings written since entry `after`, and the new count.

    A reader that fell more than a ring behind gets the newest ring's worth.
    A file that is not (yet) a tap's reads as nothing new.
    """
    if len(data) < TAP_FILE_SIZE:
        return after, []
    magic, seq = _TAP_HEADER.unpack_from(data, 0)
    if magic != TAP_MAGIC:
        return after, []
    if seq < after:  # a new file under the same name: start over
        after = 0
    first = max(after, seq - TAP_RING)
    levels = []
    for k in range(first, seq):
        peak_l, peak_r, ms_l, ms_r = _TAP_ENTRY.unpack_from(
            data, _TAP_HEADER.size + (k % TAP_RING) * _TAP_ENTRY.size
        )
        levels.append(Levels(peak_l, peak_r, ms_l, ms_r))
    return seq, levels


class FileLevelReader:
    """Reads a level tap's file from its own thread; same contract as LevelReader.

    A missing file is silence, not an ending: the plugin creates it the first
    time audio flows, and a suspended channel has none. The tap has ended when
    its host has.
    """

    def __init__(
        self,
        tap: Tap,
        on_levels: Callable[[Levels], None],
        on_ended: Callable[[], None] | None = None,
    ) -> None:
        self.tap = tap
        self.on_levels = on_levels
        self.on_ended = on_ended
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._read, name=f"tap-{self.tap.label}", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        # Readings already in the file are history, not news.
        seq = self._current_seq()
        while not self._stopping.wait(POLL_S):
            if self.tap.host is not None and not _pid_alive(self.tap.host):
                if self.on_ended is not None:
                    self.on_ended()
                return
            try:
                with open(self.tap.file, "rb") as fh:
                    data = fh.read(TAP_FILE_SIZE)
            except OSError:
                continue
            seq, levels = read_tap(data, seq)
            for reading in levels:
                self.on_levels(reading)

    def _current_seq(self) -> int:
        try:
            with open(self.tap.file, "rb") as fh:
                return read_tap(fh.read(TAP_FILE_SIZE), 0)[0]
        except OSError:
            return 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None


class LevelReader:
    """Runs one tap and calls `on_levels` from its own thread for every block.

    `on_ended` is called (from that thread) if `parec` stops on its own - the
    node it read went away - so the owner can reopen the tap.
    """

    def __init__(
        self,
        tap: Tap,
        on_levels: Callable[[Levels], None],
        on_ended: Callable[[], None] | None = None,
    ) -> None:
        self.tap = tap
        self.on_levels = on_levels
        self.on_ended = on_ended
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def command(self) -> list[str]:
        return [
            "parec", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=2",
            "--latency-msec=20",
            "--client-name=Audio Router meter",
            "--property=application.name=Audio Router meter",
            f"--property={METER_KEY}=true",
            "--property=node.dont-reconnect=true",
            "--property=node.dont-fallback=true",
            *self.tap.args,
        ]

    def start(self) -> None:
        require_tools("parec")
        self._proc = subprocess.Popen(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._read, name=f"meter-{self.tap.label}", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        size = BLOCK * 8
        try:
            while True:
                block = proc.stdout.read(size)
                if not block:
                    break
                self.on_levels(measure(block))
        except (OSError, ValueError):
            pass  # the pipe was closed by stop()
        finally:
            proc.stdout.close()  # one leaked descriptor per reopened meter otherwise
        proc.wait()
        if not self._stopping.is_set() and self.on_ended is not None:
            self.on_ended()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self._stopping.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover - parec exits on TERM
                proc.kill()
                proc.wait()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._proc = None
        self._thread = None
