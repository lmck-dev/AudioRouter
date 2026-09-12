"""Palette-derived colours.

Never hardcode a colour. A hardcoded dark scheme in an earlier project made
every label invisible on a light Plasma theme, and the same mistake here would
hide exactly the status text a user needs when something is wrong.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QWidget


class Theme:
    def __init__(self, widget: QWidget) -> None:
        palette = widget.palette()
        self.text: QColor = palette.color(QPalette.ColorRole.WindowText)
        self.dim: QColor = palette.color(QPalette.ColorRole.PlaceholderText)
        self.accent: QColor = palette.color(QPalette.ColorRole.Highlight)
        window = palette.color(QPalette.ColorRole.Window)
        self.dark = window.lightness() < 128
        # Warnings have to read on both light and dark themes; the palette has
        # no "warning" role, so derive one that keeps its contrast.
        self.warn = QColor("#e8a33d") if self.dark else QColor("#9a5b00")
        self.good = QColor("#5bbf7a") if self.dark else QColor("#1f7a3f")

    def css(self, colour: QColor) -> str:
        return f"color: {colour.name()};"
