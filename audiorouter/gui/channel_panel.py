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
    QDoubleSpinBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QWidget,
)

from ..channels import MAX_DUCK_DB, MIN_DUCK_DB, NOWHERE, Channel
from .mixer import FADER_TRAVEL, LISTEN_ECHO_HELP, db_to_fader, fader_text, fader_to_db
from .theme import Theme

FOLLOW_DEFAULT = ""
NOT_LISTENING = ""


class TargetPicker(QWidget):
    """OUT: every place a channel can play, as ticks, folded behind a summary.

    Several may be ticked (owner, 16 Oct 2026). The order things were ticked
    in is kept, because the first one is the channel's `device` (or a mic's
    `listen`), which the mixer strip and the phone remote show. Nothing ticked
    is "nowhere"; `nothing` is how that reads.
    """

    changed = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None, title: str = "") -> None:
        """With a `title`, the list stands open in a column of its own under that
        title and scrolls when long (the Channels view, owner 16 Oct 2026: folded
        in the form it had no room). Without one it folds behind a summary."""
        super().__init__(parent)
        self.nothing = "Nowhere"
        self._options: list[tuple[str, str]] = []
        self._ticked: list[str] = []
        self.header = QToolButton(self)
        self.header.setCheckable(True)
        self.header.setAutoRaise(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.ArrowType.RightArrow)
        self.header.setToolTip("Tick every output this channel should play through")
        self.header.toggled.connect(self.set_open)
        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(18, 0, 0, 0)
        self.body_layout.setSpacing(2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._boxes: dict[str, QCheckBox] = {}
        self.title = None
        if not title:
            layout.addWidget(self.header, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(self.body)
            self.body.hide()
            return
        # A column of its own: always open, and as tall as the panel beside it,
        # never taller - the list scrolls instead of stretching the panel.
        self.header.hide()
        self.title = QLabel(title, self)
        theme = Theme(self)
        self.title.setStyleSheet(f"{theme.css(theme.dim)} font-weight: bold; padding-top: 6px;")
        self.empty = QLabel(self)
        self.empty.setStyleSheet(theme.css(theme.dim))
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.body)
        scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.scroll = scroll
        layout.addWidget(self.title)
        layout.addWidget(self.empty)
        layout.addWidget(scroll, 1)

    def set_open(self, open_: bool) -> None:
        if self.title is not None:
            return   # a column of its own is always open
        self.header.blockSignals(True)
        self.header.setChecked(open_)
        self.header.blockSignals(False)
        self.header.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)
        self.body.setVisible(open_)

    def is_open(self) -> bool:
        return self.title is not None or self.body.isVisibleTo(self)

    def set_options(self, options: list[tuple[str, str]], ticked: list[str]) -> None:
        """(value, label) per place, and which are ticked, first one first. A
        ticked value missing from `options` is added as itself (unplugged)."""
        known = {value for value, _ in options}
        self._options = list(options) + [(v, f"{v} (not connected)") for v in ticked if v not in known]
        self._ticked = [v for v in ticked if any(v == o for o, _ in self._options)]
        for box in self._boxes.values():
            self.body_layout.removeWidget(box)
            box.deleteLater()
        self._boxes = {}
        for n, (value, label) in enumerate(self._options):
            box = QCheckBox(label, self.body)
            box.setChecked(value in self._ticked)
            box.toggled.connect(lambda on, v=value: self._toggled(v, on))
            self.body_layout.insertWidget(n, box)   # above the stretch, when there is one
            self._boxes[value] = box
        self._show_summary()

    def ticked(self) -> list[str]:
        return list(self._ticked)

    def labels(self) -> list[str]:
        return [label for _, label in self._options]

    def box(self, value: str) -> QCheckBox:
        return self._boxes[value]

    def summary(self) -> str:
        return self.header.text()

    def _toggled(self, value: str, on: bool) -> None:
        if on and value not in self._ticked:
            self._ticked.append(value)
        elif not on and value in self._ticked:
            self._ticked.remove(value)
        self._show_summary()
        self.changed.emit(self.ticked())

    def _show_summary(self) -> None:
        names = dict(self._options)
        self.header.setText(", ".join(names[v] for v in self._ticked) if self._ticked else self.nothing)
        if self.title is not None:
            self.empty.setText(self.nothing)
            self.empty.setVisible(not self._ticked)

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
        self.device = TargetPicker(self, title="Plays through")  # devices, groups, or listen-throughs
        self.device.changed.connect(self._targets_ticked)
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
        self._listen_cancelled = False  # from the engine: the mic plays on its canceller's speakers
        # Priority (owner, 23 Oct 2026): while this channel plays, the ticked
        # channels go down, and come back once it has been quiet a moment.
        duck_help = ("While this channel plays, the channels ticked here go down just enough to sit "
                     "under it - a loud game a lot, a quiet one not at all - never more than this, "
                     "and come back a second after it stops. For a voice assistant over a game "
                     "or music. Needs the login service running.")
        self.ducks = QToolButton(self)
        self.ducks.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.ducks.setToolTip(duck_help)
        self.ducks_menu = QMenu(self.ducks)
        self.ducks.setMenu(self.ducks_menu)
        self.duck_db = QDoubleSpinBox(self)
        self.duck_db.setRange(MIN_DUCK_DB, MAX_DUCK_DB)
        self.duck_db.setDecimals(0)
        self.duck_db.setSingleStep(1.0)
        self.duck_db.setPrefix("up to ")
        self.duck_db.setSuffix(" dB")
        self.duck_db.setToolTip(duck_help)
        self.duck_db.valueChanged.connect(self._duck_db_changed)
        self._duck_choices: list[tuple[str, str, str]] = []
        duck_row = QHBoxLayout()
        duck_row.addWidget(self.ducks)
        duck_row.addWidget(self.duck_db)
        duck_row.addStretch(1)
        self.duck_row = QWidget(self)
        self.duck_row.setLayout(duck_row)
        duck_row.setContentsMargins(0, 0, 0, 0)
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
        # The form, and beside it OUT's list of places in a column of its own
        # (owner, 16 Oct 2026: it needs the room, and the right side was empty).
        self.form = QFormLayout()
        columns = QHBoxLayout(self)
        columns.addLayout(self.form, 3)
        columns.addSpacing(12)
        columns.addWidget(self.device, 1)
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
        self.form.addRow("", self.recordable)
        self.form.addRow("Turns down", self.duck_row)
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
        self.form.setRowVisible(self.duck_row, channel is not None and not is_input)

    def set_kind_label(self, text: str) -> None:
        """The mixer's caption for this channel: From a mic, From apps, Cable, Group."""
        theme = Theme(self)
        self.kind.setText(text)
        self.kind.setStyleSheet(theme.css(theme.dim))

    def set_duck_choices(self, choices: list[tuple[str, str, str]]) -> None:
        """The channels this one could turn down: (slug, name, why not, or "")."""
        self._duck_choices = choices
        self._fill_ducks()

    def _fill_ducks(self) -> None:
        channel = self.channel
        ducks = channel.ducks if channel is not None else []
        self.ducks_menu.clear()
        for slug, name, why_not in self._duck_choices:
            action = self.ducks_menu.addAction(f"{name} ({why_not})" if why_not else name)
            action.setCheckable(True)
            action.setChecked(slug in ducks)
            action.setEnabled(not why_not or slug in ducks)
            action.toggled.connect(lambda on, slug=slug: self._duck_ticked(slug, on))
        names = dict((slug, name) for slug, name, _ in self._duck_choices)
        chosen = [names.get(slug, slug) for slug in ducks]
        self.ducks.setText(", ".join(chosen) if chosen else "Nothing")
        self.ducks.setEnabled(bool(self._duck_choices))
        self.duck_db.blockSignals(True)
        self.duck_db.setValue(channel.duck_db if channel is not None else MIN_DUCK_DB)
        self.duck_db.setEnabled(bool(ducks))
        self.duck_db.blockSignals(False)

    def _duck_ticked(self, slug: str, on: bool) -> None:
        if self._loading or self.channel is None or self.channel.is_input:
            return
        ducks = [s for s in self.channel.ducks if s != slug] + ([slug] if on else [])
        if ducks != self.channel.ducks:
            self.channel.ducks = ducks
            self._fill_ducks()
            self.changed.emit()

    def _duck_db_changed(self, db: float) -> None:
        if self._loading or self.channel is None or db == self.channel.duck_db:
            return
        self.channel.duck_db = db
        self.changed.emit()

    def set_outputs(self, outputs: list[tuple[str, str]]) -> None:
        """The output channels a mic channel can play through: (slug, name)."""
        self._outputs = outputs
        if self.channel is not None and self.channel.is_input:
            self._fill_listen()

    def _fill_listen(self) -> None:
        """A mic channel's OUT: the output channels it plays through; none = only apps record it."""
        self.device.nothing = "Nowhere (only apps record it)"
        targets = self.channel.targets if self.channel is not None else []
        self.device.set_options(list(self._outputs), targets)

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
        if not is_input:
            # Any channel but a mic's: OUT is where it plays, as ticks.
            self.device.nothing = "Nowhere (recording only)"
            options = [(FOLLOW_DEFAULT, "Default output"), *devices]
            targets = self.channel.targets if self.channel is not None else []
            # A target in the graph but not a hardware device (a virtual one) is
            # connected, so only an absent one reads "not connected".
            labels = dict(options)
            for target in targets:
                if target not in labels:
                    options.append((target, target if present else f"{target} (not connected)"))
            self.device.set_options(options, targets)
            return
        # A mic channel's device is its microphone (IN).
        combo = self.source
        chosen = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Default input", FOLLOW_DEFAULT)
        for name, label in devices:
            combo.addItem(label, name)
        if self.channel is not None and self.channel.device:
            if combo.findData(self.channel.device) < 0:
                suffix = "" if present else " (not connected)"
                combo.addItem(f"{self.channel.device}{suffix}", self.channel.device)
        target = self.channel.device if self.channel is not None else chosen
        index = combo.findData(target)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
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

        self._fill_ducks()
        self._show_hint()

    def _show_hint(self) -> None:
        theme = Theme(self)
        channel = self.channel
        text = ""
        if channel is not None and channel.is_input and channel.listen:
            text = ("\u24d8 " + LISTEN_ECHO_HELP if self._listen_cancelled else
                    "You hear this mic: use headphones - through speakers a mic can feed back into itself.")
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color: {theme.warn.name()};")
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
        cancelled = bool(entry and entry.get("listen_cancelled"))
        if cancelled != self._listen_cancelled:
            self._listen_cancelled = cancelled
            self._show_hint()
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

    def _targets_ticked(self, targets: list[str]) -> None:
        if self._loading or self.channel is None:
            return
        if targets != self.channel.targets:
            self.channel.set_targets(targets)
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
