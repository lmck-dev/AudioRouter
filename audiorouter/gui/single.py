"""One window at a time.

Two windows each hold their own copy of the settings; whichever saves last
wins. On 3 Oct 2026 a window left open from before an upgrade was still
running old code beside the new one. Opening Audio Router while a window is
open now brings that window forward instead.

A local socket named per user is the lock: the first window listens on it,
and a later start connects, says "show", and exits.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = f"audiorouter-window-{os.getuid()}"
TIMEOUT_MS = 500


def show_running_window(name: str = SERVER_NAME) -> bool:
    """Ask an open window to come forward. False when none is open."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(TIMEOUT_MS):
        return False
    socket.write(b"show\n")
    socket.waitForBytesWritten(TIMEOUT_MS)
    socket.disconnectFromServer()
    return True


class WindowLock(QObject):
    """Held by the one open window; tells it when another start wants it shown."""

    show_requested = pyqtSignal()

    def __init__(self, name: str = SERVER_NAME, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server = QLocalServer(self)
        # Only reached when nobody answered on the name, so a socket file there
        # was left by a window that crashed; listening would fail on it.
        QLocalServer.removeServer(name)
        if not self.server.listen(name):
            raise OSError(self.server.errorString())
        self.server.newConnection.connect(self._connected)

    def _connected(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.disconnected.connect(socket.deleteLater)
            self.show_requested.emit()

    def close(self) -> None:
        self.server.close()
