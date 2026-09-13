"""The settings for one channel: what it is called and where it comes out."""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QWidget,
)

from ..channels import Channel
from .theme import Theme

FOLLOW_DEFAULT = ""

#: After the user moves the volume, readings from the graph are ignored for
#: this long: a refresh can carry the value from just *before* the change, and
#: would otherwise yank the slider back under the mouse.
VOLUME_SETTLE_S = 0.8


class ChannelPanel(QGroupBox):
    """Name, output device and on/off for the selected channel."""

    changed = pyqtSignal()
    renamed = pyqtSignal()
    #: The user moved the volume slider (1.0 = 100%).
    volume_changed = pyqtSignal(float)
    mute_changed = pyqtSignal(bool)

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

        self.volume = QSlider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setPageStep(10)
        self.volume.valueChanged.connect(self._volume_moved)
        self.volume_label = QLabel("", self)
        self.volume_label.setMinimumWidth(48)
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.mute = QCheckBox("Mute", self)
        self.mute.toggled.connect(self._mute_toggled)
        self._last_local_edit = 0.0
        volume_row = QHBoxLayout()
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_label)
        volume_row.addWidget(self.mute)

        layout = QFormLayout(self)
        layout.addRow("Name", self.name)
        layout.addRow("Volume", volume_row)
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
        if channel is not self.channel:
            # The settle window protects a slider the user just moved; it must
            # not hide the newly selected channel's own volume.
            self._last_local_edit = 0.0
        self.channel = channel
        self.setEnabled(channel is not None)
        self.name.setText(channel.name if channel else "")
        self.enabled.setChecked(channel.enabled if channel else False)
        self.set_devices(devices, present)
        self._loading = False

    def show_volume(self, volume: float | None, muted: bool | None) -> None:
        """Reflect the sink's volume, wherever it was changed from.

        None means the channel is not running, so there is nothing to set.
        """
        available = volume is not None
        self.volume.setEnabled(available)
        self.mute.setEnabled(available)
        if not available:
            self.volume_label.setText("-")
            self.volume.setToolTip("Start the channel to change its volume.")
            return
        self.volume.setToolTip("")
        if self.volume.isSliderDown() or time.monotonic() - self._last_local_edit < VOLUME_SETTLE_S:
            return
        percent = round(volume * 100)
        self.volume.blockSignals(True)
        # 100% is the normal top; a level raised past it elsewhere must still
        # show truthfully instead of being clamped and then written back.
        self.volume.setMaximum(150 if percent > 100 else 100)
        self.volume.setValue(percent)
        self.volume.blockSignals(False)
        self.volume_label.setText(f"{percent}%")
        self.mute.blockSignals(True)
        self.mute.setChecked(bool(muted))
        self.mute.blockSignals(False)

    def show_status(self, entry: dict | None) -> None:
        self.show_volume(entry.get("volume") if entry else None, entry.get("muted") if entry else None)
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

    def _volume_moved(self, percent: int) -> None:
        self.volume_label.setText(f"{percent}%")
        if self._loading or self.channel is None:
            return
        self._last_local_edit = time.monotonic()
        self.volume_changed.emit(percent / 100.0)

    def _mute_toggled(self, on: bool) -> None:
        if self._loading or self.channel is None:
            return
        self._last_local_edit = time.monotonic()
        self.mute_changed.emit(on)

    def _enabled_toggled(self, on: bool) -> None:
        if self._loading or self.channel is None:
            return
        if on != self.channel.enabled:
            self.channel.enabled = on
            self.changed.emit()
