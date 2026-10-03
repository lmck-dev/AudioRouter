"""Phone remote: switch it on, and pair a phone by scanning a code.

The remote itself runs in the login service (`remote.py`); this window only
writes `remote.json`, which the service follows within a second, and shows
the pairing link as a QR code. Whether the service is actually answering is
checked by asking it, every second while the window is open.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import remote
from ..engine import daemon_pid
from .theme import Theme

#: Light margin round the code, in modules: scanners need a quiet zone.
QUIET_ZONE = 4
CHECK_INTERVAL_MS = 1000


def qr_matrix(text: str) -> list[list[bool]] | None:
    """The QR code for `text` as rows of dark (True) modules; None without the library."""
    try:
        import qrcode
    except ImportError:
        return None
    code = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.get_matrix()]


def service_answers(port: int, timeout: float = 0.3) -> bool:
    """Is the login service's remote listening? Asked over loopback."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=timeout) as reply:
            return json.load(reply).get("app") == "audiorouter"
    except (OSError, ValueError):
        return False


class QrCode(QWidget):
    """Draws a QR code, always dark on white.

    Not theme colours, on purpose: a scanner needs dark modules on a light
    ground, and an inverted code on a dark theme fails on many phones.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.matrix: list[list[bool]] | None = None
        self.setMinimumSize(QSize(240, 240))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_text(self, text: str | None) -> None:
        self.matrix = qr_matrix(text) if text else None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if not self.matrix:
            return
        count = len(self.matrix) + 2 * QUIET_ZONE
        side = min(self.width(), self.height())
        module = max(1, side // count)  # whole pixels keep the edges crisp
        size = module * count
        left = (self.width() - size) // 2
        top = (self.height() - size) // 2
        painter = QPainter(self)
        painter.fillRect(left, top, size, size, QColor("white"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("black"))
        for y, row in enumerate(self.matrix):
            for x, dark in enumerate(row):
                if dark:
                    painter.drawRect(QRectF(left + (x + QUIET_ZONE) * module,
                                            top + (y + QUIET_ZONE) * module, module, module))
        painter.end()


class PhoneRemoteWindow(QDialog):
    """On/off, the pairing code, and Unpair all phones."""

    def __init__(self, parent: QWidget | None = None,
                 service_running: Callable[[], bool] = lambda: daemon_pid() is not None,
                 answers: Callable[[int], bool] = service_answers) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phone remote")
        self.resize(460, 640)
        self._service_running = service_running
        self._answers = answers
        self.settings = remote.RemoteSettings.load()
        theme = Theme(self)
        self._dim = theme.dim.name()
        self._warn = theme.warn.name()
        self._good = theme.good.name()

        intro = QLabel(
            "Control the mixer from your phone with the Audio Router Remote app. "
            "Unless you allow other networks below, the phone must be on the same "
            "network as this computer.", self)
        intro.setWordWrap(True)
        self.enabled = QCheckBox("Let the phone app control this desk", self)
        self.enabled.setChecked(self.settings.enabled)
        self.enabled.toggled.connect(self._toggled)
        self.outside = QCheckBox("Also allow other networks, such as a VPN like Tailscale", self)
        self.outside.setChecked(self.settings.allow_outside)
        self.outside.setToolTip(
            "Off: only phones on this computer's home network are answered.\n"
            "On: phones on any network that can reach this computer, with the pairing code.")
        self.outside.toggled.connect(self._outside_toggled)
        self.outside_note = QLabel(
            f"<span style='color:{self._warn}'>The connection is not encrypted. "
            "Only use this on networks you trust; Tailscale encrypts its own traffic.</span>", self)
        self.outside_note.setWordWrap(True)

        self.code = QrCode(self)
        self.how = QLabel(self)
        self.how.setWordWrap(True)
        self.how.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.state = QLabel(self)
        self.state.setWordWrap(True)
        self.state.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        buttons = QDialogButtonBox(self)
        self.unpair_button = QPushButton("Unpair all phones", self)
        self.unpair_button.setToolTip("Make a new pairing code; every phone must scan it again")
        self.unpair_button.clicked.connect(self._unpair)
        buttons.addButton(self.unpair_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 16)
        layout.setSpacing(14)
        layout.addWidget(intro)
        layout.addWidget(self.enabled)
        layout.addWidget(self.outside)
        layout.addWidget(self.outside_note)
        layout.addWidget(self.code, 1)
        layout.addWidget(self.how)
        layout.addWidget(self.state)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.show_state)
        self._timer.start()
        self.show_pairing()

    # -- actions ---------------------------------------------------------------

    def _save(self) -> bool:
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "Phone remote", f"Could not save the setting:\n\n{exc}")
            return False
        return True

    def _toggled(self, on: bool) -> None:
        self.settings.enabled = on
        if on:
            self.settings.ensure_token()
        if not self._save():
            self.enabled.blockSignals(True)
            self.enabled.setChecked(not on)
            self.enabled.blockSignals(False)
            self.settings.enabled = not on
        self.show_pairing()

    def _outside_toggled(self, on: bool) -> None:
        self.settings.allow_outside = on
        if not self._save():
            self.outside.blockSignals(True)
            self.outside.setChecked(not on)
            self.outside.blockSignals(False)
            self.settings.allow_outside = not on
        self.show_pairing()

    def _unpair(self) -> None:
        answer = QMessageBox.question(
            self, "Unpair all phones",
            "Every paired phone stops working until it scans the new code. Continue?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.new_token()
        if self._save():
            self.show_pairing()

    # -- display ---------------------------------------------------------------

    def pairing_link(self) -> str | None:
        addresses = remote.local_addresses(outside=self.settings.allow_outside)
        if not self.settings.enabled or not self.settings.token or not addresses:
            return None
        return remote.pairing_url(addresses, self.settings.port, self.settings.token)

    def show_pairing(self) -> None:
        on = self.settings.enabled
        link = self.pairing_link()
        self.code.set_text(link)
        self.code.setVisible(link is not None and self.code.matrix is not None)
        self.unpair_button.setEnabled(on)
        self.outside.setEnabled(on)
        self.outside_note.setVisible(on and self.settings.allow_outside)
        addresses = remote.local_addresses(outside=self.settings.allow_outside)
        if not on:
            self.how.setText("")
        elif not addresses:
            self.how.setText("This computer is not on a home network, so no phone can reach it.")
        elif self.code.matrix is None:
            # No QR library: the link can still be typed into the app.
            self.how.setText(f"Type this into the app's Pair screen:\n{link}")
            self.how.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        else:
            self.how.setText(f"In the app, tap <b>Pair</b> and scan this code.<br>"
                             f"<span style='color:{self._dim}'>{', '.join(addresses)}, "
                             f"port {self.settings.port}</span>")
        self.show_state()

    def show_state(self) -> None:
        if not self.settings.enabled:
            self.state.setText(f"<span style='color:{self._dim}'>Off. Phones cannot reach this desk.</span>")
        elif not self._service_running():
            self.state.setText(
                f"<span style='color:{self._warn}'>The phone talks to the background service, "
                "which is not running. Tick <b>Keep routing with this window closed</b>.</span>")
        elif self._answers(self.settings.port):
            self.state.setText(f"<span style='color:{self._good}'>Ready.</span>")
        else:
            self.state.setText(f"<span style='color:{self._dim}'>Starting...</span>")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self._timer.stop()
        super().closeEvent(event)
