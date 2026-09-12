"""The settings for one channel: what it is called and where it comes out."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QWidget,
)

from ..channels import Channel
from .theme import Theme

FOLLOW_DEFAULT = ""


class ChannelPanel(QGroupBox):
    """Name, output device and on/off for the selected channel."""

    changed = pyqtSignal()
    renamed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Channel", parent)
        self.channel: Channel | None = None
        self._loading = False

        self.name = QLineEdit(self)
        self.name.editingFinished.connect(self._name_edited)
        self.device = QComboBox(self)
        self.device.activated.connect(self._device_chosen)
        self.enabled = QCheckBox("Switched on", self)
        self.enabled.toggled.connect(self._enabled_toggled)
        self.status = QLabel(self)
        self.status.setWordWrap(True)

        layout = QFormLayout(self)
        layout.addRow("Name", self.name)
        layout.addRow("Plays through", self.device)
        layout.addRow("", self.enabled)
        layout.addRow("", self.status)

    # -- population --------------------------------------------------------

    def set_devices(self, devices: list[tuple[str, str]], present: bool = True) -> None:
        """Rebuild the output list, keeping whatever the channel points at.

        A device the channel uses but which is not plugged in right now still
        has to appear, or opening the window with headphones unplugged would
        silently repoint the channel at something else. `present` says whether
        that target is actually in the graph - a channel may legitimately target
        a virtual sink, which is not hardware and so is not in `devices`, but is
        perfectly connected.
        """
        chosen = self.device.currentData()
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem("Default output", FOLLOW_DEFAULT)
        for name, label in devices:
            self.device.addItem(label, name)
        if self.channel is not None and self.channel.device:
            if self.device.findData(self.channel.device) < 0:
                suffix = "" if present else " (not connected)"
                self.device.addItem(f"{self.channel.device}{suffix}", self.channel.device)
        target = self.channel.device if self.channel is not None else chosen
        index = self.device.findData(target)
        self.device.setCurrentIndex(max(0, index))
        self.device.blockSignals(False)

    def set_channel(
        self,
        channel: Channel | None,
        devices: list[tuple[str, str]],
        present: bool = True,
    ) -> None:
        self._loading = True
        self.channel = channel
        self.setEnabled(channel is not None)
        self.name.setText(channel.name if channel else "")
        self.enabled.setChecked(channel.enabled if channel else False)
        self.set_devices(devices, present)
        self._loading = False

    def show_status(self, entry: dict | None) -> None:
        if entry is None:
            self.status.setText("")
            return
        theme = Theme(self)
        if entry["problems"]:
            text, colour = "; ".join(entry["problems"]), theme.warn
        elif not entry["enabled"]:
            text, colour = "Switched off.", theme.dim
        elif not entry["device_present"]:
            text, colour = "Its output is not connected.", theme.warn
        elif entry["running"]:
            text, colour = "Running.", theme.good
        else:
            text, colour = "Not running.", theme.warn
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {colour.name()};")

    # -- editing -----------------------------------------------------------

    def _name_edited(self) -> None:
        if self._loading or self.channel is None:
            return
        text = self.name.text().strip()
        if text and text != self.channel.name:
            self.channel.name = text
            self.renamed.emit()
            self.changed.emit()

    def _device_chosen(self, index: int) -> None:
        if self._loading or self.channel is None:
            return
        device = self.device.itemData(index) or FOLLOW_DEFAULT
        if device != self.channel.device:
            self.channel.device = device
            self.changed.emit()

    def _enabled_toggled(self, on: bool) -> None:
        if self._loading or self.channel is None:
            return
        if on != self.channel.enabled:
            self.channel.enabled = on
            self.changed.emit()
