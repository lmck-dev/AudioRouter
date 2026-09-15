"""Runs `Engine.apply()` off the GUI thread.

A restart starts processes and waits for sinks - ~0.4 s in practice, up to 8 s
when a channel is slow to come up - and the window froze for all of it.

The worker never touches the window's own configuration. The panels edit
`Channel` and `Effect` objects in place, so each run applies a copy taken on
the GUI thread when it starts: an edit made mid-run cannot change a channel
underneath the thread rendering it. A copy is safe because everything a
channel knows about its running host lives on disk (pid file, rendered conf).

Only one apply runs at a time. Edits that arrive meanwhile are folded into a
single follow-up run with the newest settings, so a slider drag during a
restart costs one more apply, not one per value.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal, pyqtSlot

from ..config import Config
from ..engine import ApplyReport, Engine


class Applier(QObject):
    """Emits `finished(report, structural)` or `failed(message, expected)` on the GUI thread."""

    started = pyqtSignal(bool)
    finished = pyqtSignal(object, bool)
    failed = pyqtSignal(str, bool)
    idle = pyqtSignal()
    #: Worker -> GUI thread: report, structural, error message, error was expected.
    _done = pyqtSignal(object, bool, str, bool)

    def __init__(self, engine: Engine, user_errors: tuple[type[BaseException], ...],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.user_errors = user_errors
        self._thread: threading.Thread | None = None
        self._again = False
        self._again_structural = False
        # Delivered on the GUI thread: signals emitted from the worker to an
        # object living there are queued.
        self._done.connect(self._on_done)

    @property
    def busy(self) -> bool:
        """Running, or about to run again with edits made meanwhile."""
        return self._thread is not None or self._again

    def request(self, structural: bool) -> None:
        """Apply the current settings, now or as soon as the running apply ends."""
        if self._thread is not None:
            self._again = True
            self._again_structural |= structural
            return
        self._start(structural)

    def _snapshot(self) -> Engine:
        engine = self.engine
        config = Config.from_dict(engine.config.to_dict())
        return Engine(config, path=engine.path, dry_run=engine.dry_run)

    def _start(self, structural: bool) -> None:
        snapshot = self._snapshot()
        self._thread = threading.Thread(
            target=self._run, args=(snapshot, structural),
            name="audiorouter-apply", daemon=True,
        )
        self.started.emit(structural)
        self._thread.start()

    def _run(self, snapshot: Engine, structural: bool) -> None:
        # Runs on the worker thread: no widgets, no window state, only signals.
        try:
            report = snapshot.apply()
        except self.user_errors as exc:
            self._done.emit(None, structural, str(exc), True)
        except Exception as exc:  # noqa: BLE001 - must reach the window, not a log
            self._done.emit(None, structural, f"{type(exc).__name__}: {exc}", False)
        else:
            self._done.emit(report, structural, "", True)

    # A real slot, not a plain method: PyQt delivers a queued call to a plain
    # method through a hidden proxy object, which `flush()`'s
    # sendPostedEvents(self) never reaches - closing would then skip the result.
    @pyqtSlot(object, bool, str, bool)
    def _on_done(self, report: ApplyReport | None, structural: bool,
                 error: str, expected: bool) -> None:
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if report is None:
            self.failed.emit(error, expected)
        else:
            self.finished.emit(report, structural)
        if self._again:
            again, self._again, self._again_structural = self._again_structural, False, False
            self._start(again)
        else:
            self.idle.emit()

    def flush(self) -> None:
        """Block until nothing is running or queued. For closing the window.

        Results are still reported; the thread is joined here rather than
        waiting for the event loop, which may already have stopped.
        """
        while self._thread is not None:
            thread = self._thread
            thread.join()
            # The completion signal is queued; deliver it now. It may start the
            # follow-up run, which the loop then waits for too.
            QCoreApplication.sendPostedEvents(self)
            if self._thread is thread:  # pragma: no cover - never delivered
                break
