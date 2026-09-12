"""Bridge from the PipeWire monitor thread to the Qt event loop.

`GraphMonitor` calls back on its own thread, and touching a widget from there
would be a crash waiting to happen. The bridge turns each callback into a Qt
signal, which Qt delivers on the GUI thread, and coalesces bursts: starting one
application can produce a dozen graph events in a few milliseconds and the UI
only needs to be rebuilt once.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..pwgraph import Graph, GraphMonitor, PwError


class GraphBridge(QObject):
    """Emits `changed` on the GUI thread when the audio graph moves."""

    changed = pyqtSignal()
    failed = pyqtSignal(str)

    #: Long enough to absorb the burst of events one app start produces, short
    #: enough that the window still feels live.
    COALESCE_MS = 250

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._monitor = GraphMonitor(self._on_graph)
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(self.COALESCE_MS)
        self._pending.timeout.connect(self.changed)
        self._running = False

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
        self.changed.emit()
        return True

    def stop(self) -> None:
        if self._running:
            self._monitor.stop()
            self._running = False

    def _on_graph(self, _graph: Graph) -> None:
        # Runs on the monitor thread: start the timer through the event loop so
        # it lives on the GUI thread, where it was created.
        QTimer.singleShot(0, self._pending.start)
