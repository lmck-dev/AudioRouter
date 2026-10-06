"""Level meters for the phone remote.

The same taps the window's mixer reads (`meter.py`): each channel strip reads
its chain's last level tap file, which costs nothing; a mic channel idles
until something records it, so a `Driver` records it while a phone watches
(the desktop then shows the mic in use, exactly as with the window's mixer);
each real device is read by one `parec`.

Nothing runs until a phone asks for meters, and everything stops when the
last one goes. A frame is made FRAME_HZ times a second from everything read
since the last one - the highest peak and the mean level - so a short peak
between frames is never lost, and every phone sees the same frames.

A frame, in dB with one decimal, FLOOR_DB meaning silence:

    {"strips": {slug: [peak_l, peak_r, rms_l, rms_r]},
     "mics": {device name: [...]}, "outputs": {device name: [...]}}
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from typing import Any

from .channels import Channel
from .config import desk_order
from .meter import Driver, FileLevelReader, LevelReader, Levels, Tap, output_tap

FRAME_HZ = 15
#: Taps are looked at again this often: a restarted channel has a new host.
FOLLOW_S = 1.0
FLOOR_DB = -90.0


def _db(linear_peak: float) -> float:
    if linear_peak <= 10 ** (FLOOR_DB / 20):
        return FLOOR_DB
    return round(20 * math.log10(linear_peak), 1)


def _db_power(mean_square: float) -> float:
    if mean_square <= 10 ** (FLOOR_DB / 10):
        return FLOOR_DB
    return round(10 * math.log10(mean_square), 1)


class _Gather:
    """Everything one tap read since the last frame."""

    __slots__ = ("peak_l", "peak_r", "ms_l", "ms_r", "count")

    def __init__(self) -> None:
        self.peak_l = self.peak_r = self.ms_l = self.ms_r = 0.0
        self.count = 0

    def add(self, levels: Levels) -> None:
        self.peak_l = max(self.peak_l, levels.peak_l)
        self.peak_r = max(self.peak_r, levels.peak_r)
        self.ms_l += levels.ms_l
        self.ms_r += levels.ms_r
        self.count += 1

    def reading(self) -> list[float]:
        n = self.count or 1
        return [_db(self.peak_l), _db(self.peak_r), _db_power(self.ms_l / n), _db_power(self.ms_r / n)]


SILENT = [FLOOR_DB] * 4


class MeterHub:
    """Runs the taps while any phone watches, and makes the frames."""

    def __init__(
        self,
        channels: Callable[[], list[Channel]],
        devices: Callable[[], tuple[list[str], list[str]]],
        file_reader=FileLevelReader,
        device_reader=LevelReader,
        driver=Driver,
        tap_for: Callable[[Channel], Tap | None] = output_tap,
    ) -> None:
        #: The channels to meter, in desk order (no companions).
        self.channels = channels
        #: (mic device names, output device names).
        self.devices = devices
        self._file_reader = file_reader
        self._device_reader = device_reader
        self._driver = driver
        self._tap_for = tap_for
        self._lock = threading.Lock()
        self._gathered: dict[tuple[str, str], _Gather] = {}
        self._readers: dict[tuple[str, str], Any] = {}
        self._drivers: dict[str, Any] = {}
        self._watchers = 0
        self._frame: dict[str, Any] | None = None
        self._seq = 0
        self._new_frame = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- watchers --------------------------------------------------------------

    def watch(self) -> None:
        """A phone wants meters. The first one starts the taps."""
        with self._lock:
            self._watchers += 1
            if self._watchers > 1:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="audiorouter-meters", daemon=True)
            self._thread.start()

    def unwatch(self) -> None:
        """A phone stopped watching. The last one stops every tap."""
        with self._lock:
            self._watchers = max(0, self._watchers - 1)
            if self._watchers:
                return
            thread, self._thread = self._thread, None
        self._stop.set()
        with self._new_frame:
            self._new_frame.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    @property
    def watchers(self) -> int:
        return self._watchers

    def wait_frame(self, seen: int, timeout: float) -> tuple[int, dict[str, Any] | None]:
        """The newest frame once there is one after `seen`, or (seen, None) on timeout."""
        with self._new_frame:
            def fresh() -> bool:
                return self._frame is not None and self._seq != seen

            self._new_frame.wait_for(lambda: fresh() or self._stop.is_set(), timeout)
            if not fresh():
                return seen, None
            return self._seq, self._frame

    # -- the work ----------------------------------------------------------------

    def _run(self) -> None:
        ticks_per_follow = max(1, round(FOLLOW_S * FRAME_HZ))
        tick = 0
        try:
            while not self._stop.is_set():
                if tick % ticks_per_follow == 0:
                    self.follow()
                tick += 1
                if self._stop.wait(1 / FRAME_HZ):
                    break
                self.make_frame()
        finally:
            self._close_all()

    def _feed(self, key: tuple[str, str]) -> Callable[[Levels], None]:
        def feed(levels: Levels) -> None:
            with self._lock:
                gathered = self._gathered.get(key)
                if gathered is None:
                    gathered = self._gathered[key] = _Gather()
                gathered.add(levels)
        return feed

    def follow(self) -> None:
        """Open, reopen and close taps to match what is running now."""
        wanted: dict[tuple[str, str], Tap] = {}
        inputs: dict[str, Channel] = {}
        try:
            channels = self.channels()
            mics, outputs = self.devices()
        except Exception:  # noqa: BLE001 - a meter must never break the service
            return
        for channel in channels:
            try:
                tap = self._tap_for(channel)
            except OSError:
                tap = None
            if tap is None:
                continue
            wanted[("strips", channel.slug)] = tap
            if channel.is_input:
                inputs[channel.slug] = channel
        for name in mics:
            wanted[("mics", name)] = Tap("Device", (f"--device={name}",), None)
        for name in outputs:
            wanted[("outputs", name)] = Tap("Device", (f"--device={name}.monitor",), None)

        for key in list(self._readers):
            reader = self._readers[key]
            tap = wanted.get(key)
            if tap is None or reader.tap.key != tap.key or not reader.running:
                reader.stop()
                del self._readers[key]
        for key, tap in wanted.items():
            if key in self._readers:
                continue
            kind = self._file_reader if key[0] == "strips" else self._device_reader
            reader = kind(tap, self._feed(key))
            try:
                reader.start()
            except Exception:  # noqa: BLE001 - e.g. parec missing: that meter stays silent
                continue
            self._readers[key] = reader
        self._follow_drivers(inputs)

    def _follow_drivers(self, inputs: dict[str, Channel]) -> None:
        """Keep one recorder on each running mic channel, so its chain runs."""
        for slug in list(self._drivers):
            driver = self._drivers[slug]
            channel = inputs.get(slug)
            if channel is None or driver.host != channel.pid() or not driver.running:
                driver.stop()
                del self._drivers[slug]
        for slug, channel in inputs.items():
            if slug in self._drivers:
                continue
            driver = self._driver(channel)
            try:
                driver.start()
            except Exception:  # noqa: BLE001
                continue
            self._drivers[slug] = driver

    def make_frame(self) -> dict[str, Any]:
        with self._lock:
            gathered, self._gathered = self._gathered, {}
            keys = list(self._readers)
        frame: dict[str, Any] = {"strips": {}, "mics": {}, "outputs": {}}
        for kind, name in keys:
            found = gathered.get((kind, name))
            frame[kind][name] = found.reading() if found else list(SILENT)
        with self._new_frame:
            self._frame = frame
            self._seq += 1
            self._new_frame.notify_all()
        return frame

    def _close_all(self) -> None:
        for reader in self._readers.values():
            reader.stop()
        self._readers.clear()
        for driver in self._drivers.values():
            driver.stop()
        self._drivers.clear()
        with self._lock:
            self._gathered.clear()
        with self._new_frame:
            self._frame = None  # the next phone must not start on an old frame


def hub_for(remote) -> MeterHub:
    """A hub that meters what `remote` shows: its desk, its devices."""

    def channels() -> list[Channel]:
        with remote.lock:
            shown, groups = desk_order(remote.engine.config)
            return shown + groups

    def devices() -> tuple[list[str], list[str]]:
        graph = remote.current_graph()
        return ([n.name for n in graph.input_devices()], [n.name for n in graph.devices()])

    return MeterHub(channels, devices)
