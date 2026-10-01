"""About Audio Router: what it is, who made it, and what it is running on.

Set like the user guide (`gui/guide.py`): a reading column, the theme's accent
for headings, colours from the palette. "Copy details" puts the version and
the system facts on the clipboard, which is what a bug report needs first.
"""

from __future__ import annotations

import platform
from collections.abc import Callable

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import config_path
from .theme import Theme

ICON = "multimedia-volume-control"  # the launcher's icon (packaging/audiorouter.desktop)
TAGLINE = "Send each app's sound to its own channel, with its own effects."
AUTHOR = "LMCK.DEV"
YEAR = "2026"
LICENCE = "Apache License 2.0"
PROJECT_URL = "https://github.com/lmck-dev/AudioRouter"
#: Donations (owner's choice of Ko-fi over Buy Me a Coffee, 1 Oct 2026;
#: ~/Documents/USEFUL_LINKS.md keeps every project's support links).
SUPPORT_URL = "https://ko-fi.com/laughingmanck"
#: What Audio Router stands on, and what each part does for it.
CREDITS = (
    ("PipeWire", "the sound system every channel runs inside"),
    ("WirePlumber", "connects apps and devices as PipeWire's session manager"),
    ("Qt and PyQt6", "the window"),
    ("LSP Plugins", "the built-in compressor and limiter"),
    ("RNNoise", "voice noise suppression"),
    ("WebRTC audio processing", "echo cancellation"),
)


def system_facts(pipewire_version: str | None, service_running: bool) -> list[tuple[str, str]]:
    """The facts a bug report needs, as (label, value) pairs."""
    return [
        ("Audio Router", __version__),
        ("PipeWire", pipewire_version or "not answering"),
        ("Qt / PyQt", f"{QT_VERSION_STR} / {PYQT_VERSION_STR}"),
        ("Python", platform.python_version()),
        ("System", system_name()),
        ("Background service", "running" if service_running else "not running"),
        ("Settings file", str(config_path())),
    ]


def system_name() -> str:
    """The distribution's own name and the kernel, e.g. "Nobara Linux 44, kernel 7.2.4"."""
    try:
        name = platform.freedesktop_os_release().get("NAME", "Linux")
        version = platform.freedesktop_os_release().get("VERSION_ID", "")
    except OSError:
        name, version = platform.system(), ""
    kernel = platform.release().split("-")[0]
    return f"{name} {version}".strip() + f", kernel {kernel}"


def details_text(facts: list[tuple[str, str]]) -> str:
    return "\n".join(f"{label}: {value}" for label, value in facts)


class AboutWindow(QDialog):
    """Who made Audio Router, under what licence, and what it is running on."""

    def __init__(self, facts: Callable[[], list[tuple[str, str]]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Audio Router")
        self.resize(580, 780)
        self._facts = facts
        theme = Theme(self)
        accent = self.palette().highlight().color().name()
        dim = theme.dim.name()

        icon = QLabel(self)
        icon.setPixmap(QIcon.fromTheme(ICON).pixmap(64, 64))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon.setHidden(QIcon.fromTheme(ICON).isNull())  # no icon theme: no empty gap
        title = QLabel(f"<div style='font-size:20pt; font-weight:700'>Audio Router</div>"
                       f"<div style='color:{dim}; margin-top:2px'>Version {__version__}</div>"
                       f"<div style='margin-top:10px'>{TAGLINE}</div>", self)
        title.setWordWrap(True)
        head = QHBoxLayout()
        head.setSpacing(18)
        head.addWidget(icon)
        head.addWidget(title, 1)

        self.body = QTextBrowser(self)
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.body.document().setDocumentMargin(4)
        self.body.setStyleSheet("QTextBrowser { background: transparent; }")
        self._fill(accent, dim)

        buttons = QDialogButtonBox(self)
        self.support_button = QPushButton("Support on Ko-fi", self)
        self.support_button.setToolTip(f"Opens {SUPPORT_URL} in your browser")
        self.support_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
        buttons.addButton(self.support_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button = QPushButton("Copy details", self)
        self.copy_button.setToolTip("Copy the version and system details, for a bug report")
        self.copy_button.clicked.connect(self.copy_details)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 18)
        layout.setSpacing(16)
        layout.addLayout(head)
        layout.addWidget(self.body, 1)
        layout.addWidget(buttons)

    def _fill(self, accent: str, dim: str) -> None:
        heading = f"color:{accent}; font-size:13pt; font-weight:600; margin-top:14px; margin-bottom:6px"
        credits = "".join(
            f"<tr><td style='padding:4px 18px 4px 0; font-weight:600'>{name}</td>"
            f"<td style='padding:4px 0'>{what}</td></tr>" for name, what in CREDITS)
        facts = "".join(
            f"<tr><td style='padding:4px 18px 4px 0; color:{dim}'>{label}</td>"
            f"<td style='padding:4px 0'>{value}</td></tr>" for label, value in self._facts())
        self.body.setHtml(
            f"<p style='line-height:150%'>&copy; {YEAR} {AUTHOR}. Released under the {LICENCE}.<br>"
            f"<a href='{PROJECT_URL}'>{PROJECT_URL.removeprefix('https://')}</a></p>"
            f"<p style='{heading}'>Support Audio Router</p>"
            f"<p style='line-height:150%'>Audio Router is free. If it is useful to you, you can buy "
            f"its developer a coffee on <a href='{SUPPORT_URL}'>Ko-fi</a>.</p>"
            f"<p style='{heading}'>Built with</p><table>{credits}</table>"
            f"<p style='{heading}'>Running on</p><table>{facts}</table>")

    def copy_details(self) -> None:
        QGuiApplication.clipboard().setText(details_text(self._facts()))
        self.copy_button.setText("Copied")
