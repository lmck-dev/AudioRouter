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
"""

from __future__ import annotations

import array
import math
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .channels import Channel
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

    @property
    def key(self) -> tuple:
        return (self.args, self.host)


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
