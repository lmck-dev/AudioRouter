"""Bridge from the PipeWire monitor thread to the Qt event loop.

`GraphMonitor` calls back on its own thread, and touching a widget from there
would be a crash waiting to happen. The bridge turns each callback into a Qt
signal, which Qt delivers on the GUI thread, and coalesces bursts: starting one
application can produce a dozen graph events in a few milliseconds and the UI
only needs to be rebuilt once.

**A PipeWire restart ends the feed** (pw-dump exits). The login service exits
and is restarted by systemd; a window has no such parent, and without
reconnecting it showed a frozen, empty graph - "No output devices" while
sound was playing (owner hit this 1 Oct 2026). The bridge therefore checks the
feed every second and reconnects with a fresh graph, then emits `reconnected`.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..pwgraph import Graph, GraphMonitor, PwError


class GraphBridge(QObject):
    """Emits `changed` on the GUI thread when the audio graph moves."""

    changed = pyqtSignal()
    failed = pyqtSignal(str)
    #: The feed ended (PipeWire restarted) and is back, with a new graph.
    reconnected = pyqtSignal()

    #: Long enough to absorb the burst of events one app start produces, short
    #: enough that the window still feels live.
    COALESCE_MS = 250
    #: How often a lost feed is noticed, and retried while PipeWire is down.
    WATCH_MS = 1000
    #: How long one reconnect may hold the window waiting for a first snapshot.
    RECONNECT_WAIT_S = 2.0

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._monitor = GraphMonitor(self._on_graph)
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(self.COALESCE_MS)
        self._pending.timeout.connect(self.changed)
        self._running = False
        self._watch = QTimer(self)
        self._watch.setInterval(self.WATCH_MS)
        self._watch.timeout.connect(self._check_feed)

    @property
    def graph(self) -> Graph:
        return self._monitor.graph

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        try:
            self._monitor.start()
        except PwError as exc:
            self.failed.emit(str(exc))
            return False
        self._running = True
        self._watch.start()
        self.changed.emit()
        return True

    def stop(self) -> None:
        self._watch.stop()
        if self._running:
            self._monitor.stop()
            self._running = False

    def _check_feed(self) -> None:
        """Reconnect a feed that ended; keep trying while PipeWire is down."""
        if self._running and not self._monitor.ended.is_set():
            return
        if self._running:
            self._monitor.stop()
            self._running = False
        monitor = GraphMonitor(self._on_graph)  # a fresh graph: the old one is stale
        try:
            monitor.start(wait=self.RECONNECT_WAIT_S)
        except PwError:
            return  # still down; the next tick tries again
        self._monitor = monitor
        self._running = True
        self.reconnected.emit()
        self.changed.emit()

    def _on_graph(self, _graph: Graph) -> None:
        # Runs on the monitor thread: start the timer through the event loop so
        # it lives on the GUI thread, where it was created.
        QTimer.singleShot(0, self._pending.start)
