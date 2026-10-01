"""The settings for one channel: what it is called and where its sound goes.

An output channel plays through a device (or nowhere) and may also be offered
to recording apps as a virtual cable. An input channel records from a
microphone or line-in, always appears in apps' input lists, and may be heard
through one of the output channels.
"""

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

from ..channels import NOWHERE, Channel
from .mixer import FADER_TRAVEL, db_to_fader, fader_text, fader_to_db
from .theme import Theme

FOLLOW_DEFAULT = ""
NOT_LISTENING = ""

#: After the user moves the volume, readings from the graph are ignored for
#: this long: a refresh can carry the value from just *before* the change, and
#: would otherwise yank the slider back under the mouse.
VOLUME_SETTLE_S = 0.8


class ChannelPanel(QGroupBox):
    """Name, device, volume and on/off for the selected channel."""

    changed = pyqtSignal()
    renamed = pyqtSignal()
    #: The user moved the volume slider (1.0 = 100%).
    volume_changed = pyqtSignal(float)
    mute_changed = pyqtSignal(bool)
    #: The user moved the fader (dB after the effects, as on the mixer).
    fader_changed = pyqtSignal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Channel", parent)
        self.channel: Channel | None = None
        self._loading = False
        self._outputs: list[tuple[str, str]] = []

        self.name = QLineEdit(self)
        self.name.editingFinished.connect(self._name_edited)
        self.device = QComboBox(self)
        self.device.activated.connect(self._device_chosen)
        self.recordable = QCheckBox("Apps can record this channel (virtual cable)", self)
        self.recordable.setToolTip(
            "Offers this channel's sound, after its effects, in every app's list of "
            "microphones - so OBS, Discord or a recorder can capture it."
        )
        self.recordable.toggled.connect(self._recordable_toggled)
        self.listen = QComboBox(self)
        self.listen.activated.connect(self._listen_chosen)
        self.echo_cancel = QCheckBox("Echo cancellation (keep the speakers out of this mic)", self)
        self.echo_cancel.setToolTip(
            "Subtracts whatever your speakers are playing - a video, music, a game - from "
            "this microphone before its effects, so a call hears you and not them. "
            "Not needed with headphones."
        )
        self.echo_cancel.toggled.connect(self._echo_cancel_toggled)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.enabled = QCheckBox("Switched on", self)
        self.enabled.toggled.connect(self._enabled_toggled)
        self.status = QLabel(self)
        self.status.setWordWrap(True)

        self.volume = QSlider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setPageStep(10)
        self.volume.valueChanged.connect(self._volume_moved)
        self.volume_label = QLabel("", self)
        self.volume_label.setMinimumWidth(self.volume_label.fontMetrics().horizontalAdvance("+10.0 dB") + 4)
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.mute = QCheckBox("Mute", self)
        self.mute.toggled.connect(self._mute_toggled)
        self._last_local_edit = 0.0
        volume_row = QHBoxLayout()
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_label)
        volume_row.addWidget(self.mute)

        # The mixer's big fader, here too: the level after the effects. The
        # volume above is the desktop's, which acts before them.
        self.fader = QSlider(Qt.Orientation.Horizontal, self)
        self.fader.setRange(0, FADER_TRAVEL)
        self.fader.setPageStep(40)
        self.fader.setToolTip("The channel's level after its effects - the mixer's fader. "
                              "The volume above acts before the effects.")
        self.fader.valueChanged.connect(self._fader_moved)
        self.fader_label = QLabel("", self)
        self.fader_label.setMinimumWidth(self.fader_label.fontMetrics().horizontalAdvance("+10.0 dB") + 4)
        self.fader_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fader_row = QHBoxLayout()
        fader_row.addWidget(self.fader, 1)
        fader_row.addWidget(self.fader_label)
        # Room where the volume row has Mute, so the two sliders line up.
        fader_row.addSpacing(self.mute.sizeHint().width() + volume_row.spacing())

        self.form = QFormLayout(self)
        self.form.addRow("Name", self.name)
        self.form.addRow("Volume", volume_row)
        self.form.addRow("Fader", fader_row)
        self.form.addRow("Plays through", self.device)
        self.form.addRow("", self.recordable)
        self.form.addRow("Listen through", self.listen)
        self.form.addRow("", self.echo_cancel)
        self.form.addRow("", self.hint)
        self.form.addRow("", self.enabled)
        self.form.addRow("", self.status)
        self._show_kind(None)

    # -- population --------------------------------------------------------

    def _show_kind(self, channel: Channel | None) -> None:
        is_input = channel is not None and channel.is_input
        label = self.form.labelForField(self.device)
        if label is not None:
            label.setText("Records from" if is_input else "Plays through")
        self.form.setRowVisible(self.recordable, channel is not None and not is_input)
        self.form.setRowVisible(self.listen, is_input)
        self.form.setRowVisible(self.echo_cancel, is_input)

    def set_outputs(self, outputs: list[tuple[str, str]]) -> None:
        """The output channels an input can be listened through: (slug, name)."""
        self._outputs = outputs
        self.listen.blockSignals(True)
        self.listen.clear()
        self.listen.addItem("Don't listen", NOT_LISTENING)
        for slug, name in outputs:
            self.listen.addItem(name, slug)
        target = self.channel.listen if self.channel is not None else NOT_LISTENING
        if target and self.listen.findData(target) < 0:
            self.listen.addItem(f"{target} (missing)", target)
        self.listen.setCurrentIndex(max(0, self.listen.findData(target)))
        self.listen.blockSignals(False)

    def set_devices(self, devices: list[tuple[str, str]], present: bool = True) -> None:
        """Rebuild the device list, keeping whatever the channel points at.

        A device the channel uses but which is not plugged in right now still
        has to appear, or opening the window with headphones unplugged would
        silently repoint the channel at something else. `present` says whether
        that target is actually in the graph - a channel may legitimately target
        a virtual device, which is not hardware and so is not in `devices`, but
        is perfectly connected.
        """
        is_input = self.channel is not None and self.channel.is_input
        chosen = self.device.currentData()
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem("Default input" if is_input else "Default output", FOLLOW_DEFAULT)
        for name, label in devices:
            self.device.addItem(label, name)
        if not is_input:
            self.device.addItem("Nowhere (recording only)", NOWHERE)
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
        self._show_kind(channel)
        self.show_fader()
        self.set_devices(devices, present)
        self.set_outputs(self._outputs)
        self._sync_options()
        self._loading = False

    def _sync_options(self) -> None:
        """The cable box and the hint follow the channel's current settings."""
        channel = self.channel
        theme = Theme(self)
        self.recordable.blockSignals(True)
        if channel is None or channel.is_input:
            self.recordable.setChecked(False)
        else:
            self.recordable.setChecked(channel.recordable)
            # Playing nowhere is only useful as a cable, so it cannot be unticked.
            self.recordable.setEnabled(channel.device != NOWHERE)
        self.recordable.blockSignals(False)

        self.echo_cancel.blockSignals(True)
        self.echo_cancel.setChecked(channel is not None and channel.is_input and channel.echo_cancel)
        self.echo_cancel.blockSignals(False)

        text, colour = "", theme.dim
        if channel is not None and channel.is_input:
            text = f'Apps list it as a microphone called "{channel.name}".'
            if channel.listen:
                text += " Listening: use headphones - through speakers a mic can feed back into itself."
                colour = theme.warn
        elif channel is not None and channel.recordable:
            text = f'Apps list it as a microphone called "{channel.name} (recording)".'
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color: {colour.name()};")
        self.form.setRowVisible(self.hint, bool(text))

    def show_volume(self, volume: float | None, muted: bool | None) -> None:
        """Reflect the channel's volume, wherever it was changed from.

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
        is_input = entry.get("kind") == "input"
        if entry["problems"]:
            text, colour = "; ".join(entry["problems"]), theme.warn
        elif not entry["enabled"]:
            text, colour = "Switched off.", theme.dim
        elif not entry["device_present"]:
            text = "Its microphone is not connected." if is_input else "Its output is not connected."
            colour = theme.warn
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
            self._sync_options()
            self.renamed.emit()
            self.changed.emit()

    def _device_chosen(self, index: int) -> None:
        if self._loading or self.channel is None:
            return
        device = self.device.itemData(index) or FOLLOW_DEFAULT
        if device != self.channel.device:
            self.channel.device = device
            if device == NOWHERE:
                self.channel.recordable = True
            self._sync_options()
            self.changed.emit()

    def _recordable_toggled(self, on: bool) -> None:
        if self._loading or self.channel is None or self.channel.is_input:
            return
        if on != self.channel.recordable:
            self.channel.recordable = on
            self._sync_options()
            self.changed.emit()

    def _echo_cancel_toggled(self, on: bool) -> None:
        if self._loading or self.channel is None or not self.channel.is_input:
            return
        if on != self.channel.echo_cancel:
            self.channel.echo_cancel = on
            self._sync_options()
            self.changed.emit()

    def _listen_chosen(self, index: int) -> None:
        if self._loading or self.channel is None or not self.channel.is_input:
            return
        through = self.listen.itemData(index) or NOT_LISTENING
        if through != self.channel.listen:
            self.channel.listen = through
            self._sync_options()
            self.changed.emit()

    def _volume_moved(self, percent: int) -> None:
        self.volume_label.setText(f"{percent}%")
        if self._loading or self.channel is None:
            return
        self._last_local_edit = time.monotonic()
        self.volume_changed.emit(percent / 100.0)

    def show_fader(self) -> None:
        """Show the channel's fader, unless the user is holding it."""
        if self.fader.isSliderDown():
            return
        db = self.channel.fader_db if self.channel is not None else 0.0
        # Only when it reads differently (see ChannelStrip.show_fader).
        if round(fader_to_db(self.fader.value()), 1) != db:
            self.fader.blockSignals(True)
            self.fader.setValue(db_to_fader(db))
            self.fader.blockSignals(False)
        self.fader_label.setText(fader_text(db) if self.channel is not None else "")

    def _fader_moved(self, value: int) -> None:
        db = round(fader_to_db(value), 1)
        self.fader_label.setText(fader_text(db))
        if not self._loading and self.channel is not None:
            self.fader_changed.emit(db)

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
