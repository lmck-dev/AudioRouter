"""The settings for one channel, in the order its sound travels.

Every channel reads the same way, as a mixer strip does (owner, 2 Oct 2026):
IN - where the sound comes from (a microphone, or the apps and channels sent
here); LEVEL - trim before the effects, fader after; OUT - where it plays
through, and whether recording apps see it as a microphone. Only the IN
source differs: a mic channel picks a microphone, an apps channel lists apps.
On a mic channel, "plays through" is its listen-through, and recording apps
always see it, since that is what it is for.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QInputDialog,
    QMenu,
    QToolButton,
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
    #: Always send this app (by name) to the channel shown.
    remember_app = pyqtSignal(str)
    #: Forget the rule at this position in the rule list.
    forget_rule = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Channel", parent)
        self.channel: Channel | None = None
        self._loading = False
        self._outputs: list[tuple[str, str]] = []

        self.name = QLineEdit(self)
        self.name.editingFinished.connect(self._name_edited)
        self.kind = QLabel(self)

        # -- IN: where the sound comes from.
        self.source = QComboBox(self)  # a mic channel's microphone
        self.source.activated.connect(self._source_chosen)
        self.source_text = QLabel("Apps and channels you send here", self)  # any other channel
        self.channels_in = QLabel(self)
        self.channels_in.setWordWrap(True)
        self.playing = QLabel(self)
        self.playing.setWordWrap(True)
        # One line of text, like Playing in: the panel never scrolls and has
        # no height to spare, and a squeezed list drew its buttons over each other.
        self.remembered = QLabel(self)
        self.remembered.setWordWrap(True)
        self.remembered.setToolTip("Apps that always start on this channel")
        self.add_app = QToolButton(self)
        self.add_app.setText("Add app")
        self.add_app.setToolTip("Always send an app to this channel")
        self.add_app.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_app_menu = QMenu(self.add_app)
        self.add_app.setMenu(self.add_app_menu)
        self.forget = QToolButton(self)
        self.forget.setText("Forget")
        self.forget.setToolTip("Stop sending an app here; it stays where it is for now")
        self.forget.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.forget_menu = QMenu(self.forget)
        self.forget.setMenu(self.forget_menu)
        self._rules: list[tuple[int, str]] = []
        self._candidates: list[str] = []
        remembered_box = QWidget(self)
        remembered_layout = QHBoxLayout(remembered_box)
        remembered_layout.setContentsMargins(0, 0, 0, 0)
        remembered_layout.addWidget(self.remembered, 1)
        remembered_layout.addWidget(self.add_app, 0, Qt.AlignmentFlag.AlignTop)
        remembered_layout.addWidget(self.forget, 0, Qt.AlignmentFlag.AlignTop)
        self.remembered_box = remembered_box

        # -- OUT: where it goes.
        self.device = QComboBox(self)  # plays through: a device, a group, or a listen-through
        self.device.activated.connect(self._device_chosen)
        self.recordable = QCheckBox(self)
        self.recordable.setToolTip(
            "Offers this channel's sound, after its effects, in every app's list of "
            "microphones - so OBS, Discord or a recorder can capture it."
        )
        self.recordable.toggled.connect(self._recordable_toggled)
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

        self.volume.setToolTip("TRIM: the channel's volume before its effects - the same "
                               "volume the desktop's sound settings show.")

        name_row = QHBoxLayout()
        name_row.addWidget(self.name, 1)
        name_row.addWidget(self.kind)
        self.form = QFormLayout(self)
        self.form.addRow("Name", name_row)
        self.form.addRow(self._section("IN"))
        self.form.addRow("Source", self.source)
        self.form.addRow("Source ", self.source_text)
        self.form.addRow("", self.echo_cancel)
        self.form.addRow("Channels in", self.channels_in)
        self.form.addRow("Playing in", self.playing)
        self.form.addRow("Remembered", self.remembered_box)
        self.form.addRow(self._section("LEVEL"))
        self.form.addRow("Trim", volume_row)
        self.form.addRow("Fader", fader_row)
        self.form.addRow(self._section("OUT"))
        self.form.addRow("Plays through", self.device)
        self.form.addRow("", self.recordable)
        self.form.addRow("", self.hint)
        self.form.addRow("", self.enabled)
        self.form.addRow("", self.status)
        self._show_kind(None)

    def _section(self, title: str) -> QLabel:
        """A section title, in the mixer's small dim capitals."""
        label = QLabel(title, self)
        theme = Theme(self)
        label.setStyleSheet(f"{theme.css(theme.dim)} font-weight: bold; padding-top: 6px;")
        return label

    # -- population --------------------------------------------------------

    def _show_kind(self, channel: Channel | None) -> None:
        is_input = channel is not None and channel.is_input
        self.form.setRowVisible(self.source, is_input)
        self.form.setRowVisible(self.source_text, not is_input)
        self.form.setRowVisible(self.echo_cancel, is_input)

    def set_kind_label(self, text: str) -> None:
        """The mixer's caption for this channel: From a mic, From apps, Cable, Group."""
        theme = Theme(self)
        self.kind.setText(text)
        self.kind.setStyleSheet(theme.css(theme.dim))

    def set_outputs(self, outputs: list[tuple[str, str]]) -> None:
        """The output channels a mic channel can play through: (slug, name)."""
        self._outputs = outputs
        if self.channel is not None and self.channel.is_input:
            self._fill_listen()

    def _fill_listen(self) -> None:
        """A mic channel's OUT: nowhere (apps only record it) or an output channel."""
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem("Nowhere (only apps record it)", NOT_LISTENING)
        for slug, name in self._outputs:
            self.device.addItem(name, slug)
        target = self.channel.listen if self.channel is not None else NOT_LISTENING
        if target and self.device.findData(target) < 0:
            self.device.addItem(f"{target} (missing)", target)
        self.device.setCurrentIndex(max(0, self.device.findData(target)))
        self.device.blockSignals(False)

    def show_sources(self, playing: list[str], members: list[str], rules: list[tuple[int, str]],
                     candidates: list[str]) -> None:
        """What feeds the channel: apps playing in now, channels playing in,
        and the remembered apps as (rule position, app name).

        `candidates` are the apps the Add menu offers: playing now, or
        remembered on another channel.
        """
        theme = Theme(self)
        self.channels_in.setText(", ".join(members))
        self.form.setRowVisible(self.channels_in, bool(members))
        self.playing.setText(", ".join(playing) if playing else "nothing playing")
        self.playing.setStyleSheet("" if playing else theme.css(theme.dim))
        self._rules = list(rules)
        self.remembered.setText(", ".join(app for _i, app in rules) if rules else "none")
        self.remembered.setStyleSheet("" if rules else theme.css(theme.dim))
        self.forget_menu.clear()
        for index, app in rules:
            self.forget_menu.addAction(app, lambda i=index: self.forget_rule.emit(i))
        self.forget.setEnabled(bool(rules))
        self._candidates = list(candidates)
        self.add_app_menu.clear()
        for app in candidates:
            self.add_app_menu.addAction(app, lambda a=app: self._remember(a))
        if candidates:
            self.add_app_menu.addSeparator()
        self.add_app_menu.addAction("Another app...", self._remember_other)

    def _remember(self, app: str) -> None:
        if self.channel is not None and app.strip():
            self.remember_app.emit(app.strip())

    def _remember_other(self) -> None:
        if self.channel is None:
            return
        app, ok = QInputDialog.getText(
            self, "Always send an app here",
            f"The app's name, as Playing now shows it. It will start on {self.channel.name}.")
        if ok:
            self._remember(app)

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
        # A mic channel's device is its microphone (IN); any other's is where it plays (OUT).
        combo = self.source if is_input else self.device
        chosen = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Default input" if is_input else "Default output", FOLLOW_DEFAULT)
        for name, label in devices:
            combo.addItem(label, name)
        if not is_input:
            combo.addItem("Nowhere (recording only)", NOWHERE)
        if self.channel is not None and self.channel.device:
            if combo.findData(self.channel.device) < 0:
                suffix = "" if present else " (not connected)"
                combo.addItem(f"{self.channel.device}{suffix}", self.channel.device)
        target = self.channel.device if self.channel is not None else chosen
        index = combo.findData(target)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
        if is_input:
            self._fill_listen()

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
        self._sync_options()
        self._loading = False

    def _sync_options(self) -> None:
        """The cable box and the hint follow the channel's current settings."""
        channel = self.channel
        theme = Theme(self)
        self.recordable.blockSignals(True)
        if channel is None:
            self.recordable.setChecked(False)
            self.recordable.setText("Recording apps see it as a microphone")
        elif channel.is_input:
            # What a mic channel is for: always offered, so never unticked.
            self.recordable.setChecked(True)
            self.recordable.setEnabled(False)
            self.recordable.setText(f'Recording apps see it as "{channel.name}"')
        else:
            self.recordable.setText(f'Recording apps see it as "{channel.name} (recording)"')
            self.recordable.setChecked(channel.recordable)
            # Playing nowhere is only useful as a cable, so it cannot be unticked.
            self.recordable.setEnabled(channel.device != NOWHERE)
        self.recordable.blockSignals(False)

        self.echo_cancel.blockSignals(True)
        self.echo_cancel.setChecked(channel is not None and channel.is_input and channel.echo_cancel)
        self.echo_cancel.blockSignals(False)

        text, colour = "", theme.dim
        if channel is not None and channel.is_input and channel.listen:
            text = "You hear this mic: use headphones - through speakers a mic can feed back into itself."
            colour = theme.warn
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

    def _source_chosen(self, index: int) -> None:
        if self._loading or self.channel is None or not self.channel.is_input:
            return
        device = self.source.itemData(index) or FOLLOW_DEFAULT
        if device != self.channel.device:
            self.channel.device = device
            self._sync_options()
            self.changed.emit()

    def _device_chosen(self, index: int) -> None:
        if self._loading or self.channel is None:
            return
        if self.channel.is_input:
            self._listen_chosen(index)
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
        through = self.device.itemData(index) or NOT_LISTENING
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
