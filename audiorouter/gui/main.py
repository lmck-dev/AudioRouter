"""The main window.

Laid out in the order someone actually thinks: the channels they have made, the
one they are editing, and underneath, everything that is currently making sound
with a menu to send it somewhere. Nothing here decides anything about audio; it
edits the configuration and asks the engine to make reality match.

Edits apply themselves. A channel has to be restarted to change its effects, so
changes are collected for a moment and applied in one go rather than on every
keystroke, and streams are put back where they were afterwards. The apply runs
on a worker thread (`applier.py`), so the window never freezes while it does.
"""

from __future__ import annotations

import sys
import time

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import install, native
from ..channels import INPUT, NOWHERE, ChannelError, validate_slug
from ..config import ConfigError
from ..effects import EffectError
from ..engine import AutoRouter, Engine, EngineError, daemon_pid
from ..pwgraph import PwError
from ..routing import RoutingError
from .applier import Applier
from .channel_panel import ChannelPanel
from .effects_panel import EffectsPanel
from .meters import MeterPanel
from .mixer import MixerView
from .monitor import GraphBridge
from .streams_panel import StreamsPanel
from .theme import Theme

#: Collecting edits for this long turns a burst of typing into one restart.
APPLY_DELAY_MS = 700
#: A knob change is applied to the running channel, so it can be heard almost
#: at once; this only batches the flood of values a slider drag produces.
TUNE_DELAY_MS = 60

USER_ERRORS = (EngineError, ChannelError, ConfigError, EffectError, RoutingError, PwError)


def slug_for(name: str, taken: set[str]) -> str:
    """A graph-safe id from a human name, unique among `taken`."""
    base = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")[:40]
    base = base or "channel"
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base[:37]}_{suffix}", suffix + 1
    return candidate


#: Where view state (not audio settings) is kept: ~/.config/audiorouter/gui.conf.
SETTINGS = ("audiorouter", "gui")


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine, settings: QSettings | None = None) -> None:
        super().__init__()
        self.engine = engine
        self.settings = settings if settings is not None else QSettings(*SETTINGS)
        self._selected_slug: str | None = None
        self.setWindowTitle("Audio Router")
        self.resize(920, 720)

        self.bridge = GraphBridge(self)
        self.bridge.changed.connect(self.refresh)
        self.bridge.failed.connect(self._monitor_failed)
        self.auto: AutoRouter | None = None

        self._pending_apply = QTimer(self)
        self._pending_apply.setSingleShot(True)
        self._pending_apply.setInterval(APPLY_DELAY_MS)
        self._pending_apply.timeout.connect(self.apply_now)
        self._structural_pending = False

        self.applier = Applier(engine, USER_ERRORS, self)
        self.applier.started.connect(self._apply_started)
        self.applier.finished.connect(self._apply_finished)
        self.applier.failed.connect(self._apply_failed)
        self.applier.idle.connect(self._apply_idle)
        self._busy_cursor = False

        # Volume goes straight to the sink, never through apply(); this only
        # batches a slider drag into a few wpctl calls a second.
        self._pending_volume: tuple[str, float] | None = None
        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.setInterval(TUNE_DELAY_MS)
        self._volume_timer.timeout.connect(self._write_volume)

        self._build()
        self._connect()

        if self.bridge.start():
            self.engine.use_graph(self.bridge.graph)
        self.refresh()
        self._offer_first_run()
        self._start_auto_router()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        self.auto_route = QCheckBox("Send new apps to their usual channel", self)
        self.auto_route.setChecked(self.engine.config.auto_route)
        self.background = QCheckBox("Keep routing with this window closed, and from login", self)
        self.background.setToolTip(
            "Runs a small background service that starts your channels when you log in "
            "and sends each app to its channel, without this window open."
        )
        self.background.setChecked(install.login_service_enabled())
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        # A conflict with another audio program breaks routing entirely, so it
        # gets a banner across the window rather than the small status text.
        self.conflict_banner = QLabel(self)
        self.conflict_banner.setWordWrap(True)
        self.conflict_banner.setHidden(True)

        top = QHBoxLayout()
        top.addWidget(self.auto_route)
        top.addWidget(self.background)
        top.addStretch(1)
        top.addWidget(self.status_label)

        # Outputs and inputs behave nothing alike, so they get a list each.
        self.output_list = QListWidget(self)
        self.input_list = QListWidget(self)
        self.add_output_button = QPushButton("New output", self)
        self.add_input_button = QPushButton("New input", self)
        self.remove_output_button = QPushButton("Delete", self)
        self.remove_input_button = QPushButton("Delete", self)

        output_buttons = QHBoxLayout()
        output_buttons.addWidget(self.add_output_button)
        output_buttons.addWidget(self.remove_output_button)
        input_buttons = QHBoxLayout()
        input_buttons.addWidget(self.add_input_button)
        input_buttons.addWidget(self.remove_input_button)

        # Remembered apps sit beside Playing now and fold away with it.
        self.rules = QWidget(self)
        self.rules_list = QListWidget(self.rules)
        self.forget_button = QPushButton("Forget", self.rules)
        rules_layout = QVBoxLayout(self.rules)
        rules_layout.setContentsMargins(0, 0, 0, 0)
        rules_layout.addWidget(QLabel("Remembered apps", self.rules))
        rules_layout.addWidget(self.rules_list, 1)
        rules_layout.addWidget(self.forget_button)

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Outputs - apps play into these", left))
        left_layout.addWidget(self.output_list, 2)
        left_layout.addLayout(output_buttons)
        left_layout.addWidget(QLabel("Inputs - microphones and line-in", left))
        left_layout.addWidget(self.input_list, 1)
        left_layout.addLayout(input_buttons)

        self.channel_panel = ChannelPanel(self)
        self.effects_panel = EffectsPanel(self)
        self.meters = MeterPanel(self, graph_source=self._meter_graph)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        # These two never scroll, so they must never be squeezed: an input's
        # extra rows once overlapped into each other when the window was short.
        # The effects list and Playing now scroll, so they give way instead.
        for fixed in (self.channel_panel, self.meters):
            fixed.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        right_layout.addWidget(self.channel_panel)
        right_layout.addWidget(self.meters)
        right_layout.addWidget(self.effects_panel, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # The mixer is the default view (owner, 30 Sep 2026); Channels keeps
        # every setting, and both edit the same channels.
        self.mixer = MixerView(self)
        self.views = QTabWidget(self)
        self.views.addTab(self.mixer, "Mixer")
        self.views.addTab(splitter, "Channels")

        self.streams_panel = StreamsPanel(self.engine, self)
        self.streams_panel.add_beside(self.rules)

        central = QWidget(self)
        self._layout = layout = QVBoxLayout(central)
        layout.addWidget(self.conflict_banner)
        layout.addLayout(top)
        layout.addWidget(self.views, 2)
        layout.addWidget(self.streams_panel, 1)
        self.setCentralWidget(central)
        self._streams_folded(self._setting_bool("streams_expanded", True))
        self.meters.follow_effect.setChecked(self._setting_bool("meters_follow_effect", True))

        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

    def _connect(self) -> None:
        self.output_list.currentItemChanged.connect(self._list_item_changed)
        self.input_list.currentItemChanged.connect(self._list_item_changed)
        self.add_output_button.clicked.connect(lambda: self._add_channel(is_input=False))
        self.add_input_button.clicked.connect(lambda: self._add_channel(is_input=True))
        self.remove_output_button.clicked.connect(self._remove_channel)
        self.remove_input_button.clicked.connect(self._remove_channel)
        self.streams_panel.expanded_changed.connect(self._streams_folded)
        self.views.currentChanged.connect(lambda _i: self._update_meters())
        self.mixer.volume_changed.connect(self._set_volume)
        self.mixer.fader_changed.connect(self._set_fader)
        self.mixer.mute_toggled.connect(self._set_muted)
        self.mixer.pan_changed.connect(self._set_pan)
        self.mixer.solo_toggled.connect(self._set_solo)
        self.mixer.effect_toggled.connect(self._toggle_effect)
        self.mixer.effect_opened.connect(self._open_effect)
        self.mixer.add_effect.connect(self._add_effect_to)
        self.mixer.device_chosen.connect(self._set_device)
        self.mixer.listen_chosen.connect(self._set_listen)
        self.mixer.open_settings.connect(lambda slug: self._open_effect(slug, -1))
        self.mixer.new_channel.connect(lambda is_input: self._add_channel(is_input=is_input))
        self.channel_panel.changed.connect(self._config_edited)
        self.channel_panel.renamed.connect(self._refresh_channel_list)
        self.channel_panel.volume_changed.connect(self._volume_changed)
        self.channel_panel.mute_changed.connect(self._mute_changed)
        self.effects_panel.changed.connect(self._config_edited)
        self.effects_panel.tuned.connect(self._config_tuned)
        self.effects_panel.highlighted.connect(
            lambda row: self.meters.show_effect(row, self.selected_channel)
        )
        self.meters.follow_effect.toggled.connect(
            lambda on: self.settings.setValue("meters_follow_effect", on)
        )
        self.auto_route.toggled.connect(self._auto_route_toggled)
        self.background.toggled.connect(self._background_toggled)
        self.streams_panel.send_requested.connect(self._send_stream)
        self.streams_panel.remember_requested.connect(self._remember_stream)
        self.forget_button.clicked.connect(self._forget_rule)

    # -- state -------------------------------------------------------------

    @property
    def selected_channel(self):
        return next(
            (c for c in self.engine.config.channels if c.slug == self._selected_slug), None
        )

    def select_channel(self, slug: str | None) -> None:
        """Select a channel in whichever list holds it, and load it."""
        self._selected_slug = slug
        for lst in (self.output_list, self.input_list):
            lst.blockSignals(True)
            match = None
            for i in range(lst.count()):
                if lst.item(i).data(Qt.ItemDataRole.UserRole) == slug:
                    match = lst.item(i)
            lst.setCurrentItem(match)
            if match is None:
                lst.clearSelection()
            lst.blockSignals(False)
        self._channel_selected()

    def _list_item_changed(self, item, _previous) -> None:
        if item is not None:
            self.select_channel(item.data(Qt.ItemDataRole.UserRole))

    def _setting_bool(self, key: str, default: bool) -> bool:
        value = self.settings.value(key, default)
        return value if isinstance(value, bool) else str(value).lower() == "true"

    def _streams_folded(self, expanded: bool) -> None:
        self.streams_panel.set_expanded(expanded)
        # Folded, the table's share of the height goes to the channel editor.
        self._layout.setStretchFactor(self.streams_panel, 1 if expanded else 0)
        self.settings.setValue("streams_expanded", expanded)

    def refresh(self) -> None:
        """Redraw everything from one engine status reading."""
        if self.bridge.running:
            self.engine.use_graph(self.bridge.graph)
        try:
            status = self.engine.status(refresh=not self.bridge.running)
        except PwError as exc:
            self._set_status(str(exc), warn=True)
            return
        self._status = status
        self._show_conflicts(status.get("conflicts", []))
        self._refresh_channel_list(status)
        self._refresh_rules()
        self.streams_panel.refresh(status)
        entry = next(
            (c for c in status["channels"]
             if self.selected_channel and c["slug"] == self.selected_channel.slug),
            None,
        )
        self.channel_panel.show_status(entry)
        self.mixer.refresh(self.engine.config.channels, status)
        # A restart replaces the nodes a meter reads; this reopens its taps.
        self.meters.follow(self.selected_channel, self.engine.graph())
        self.channel_panel.set_outputs(self._output_choices())
        self.channel_panel.set_devices(
            self._device_choices(status, self.selected_channel),
            present=entry["device_present"] if entry else True,
        )
        if not status["devices"]:
            self._set_status("No output devices - is anything plugged in?", warn=True)
        elif status["problems"]:
            self._set_status(status["problems"][0], warn=True)
        elif self.applier.busy or self._pending_apply.isActive():
            # A restart produces graph events mid-way; those refreshes must not
            # announce that everything is settled while it is still happening.
            self._set_status("Updating...")
        else:
            self._set_status("")

    def _refresh_channel_list(self, status: dict | None = None) -> None:
        status = status if status is not None else getattr(self, "_status", None)
        running = (
            {c["slug"] for c in status["channels"] if c["running"]} if status else set()
        )
        for lst in (self.output_list, self.input_list):
            lst.blockSignals(True)
            lst.clear()
        for channel in self.engine.config.channels:
            label = channel.name
            if not channel.is_input and channel.recordable:
                label += "  (cable)"
            if not channel.enabled:
                label += "  (off)"
            elif channel.slug not in running:
                label += "  (not running)"
            item = QListWidgetItem(label, self.input_list if channel.is_input else self.output_list)
            item.setData(Qt.ItemDataRole.UserRole, channel.slug)
        for lst in (self.output_list, self.input_list):
            lst.blockSignals(False)
        slugs = self.engine.config.channel_slugs
        if self._selected_slug not in slugs:
            # The first output, else the first input, else nothing.
            first = [c.slug for c in self.engine.config.channels if not c.is_input]
            first += [c.slug for c in self.engine.config.channels if c.is_input]
            self._selected_slug = first[0] if first else None
        self.select_channel(self._selected_slug)

    def _refresh_rules(self) -> None:
        self.rules_list.clear()
        for rule in self.engine.config.rules.rules:
            try:
                name = self.engine.config.channel(rule.channel).name
            except ConfigError:
                name = rule.channel
            QListWidgetItem(f"{rule.pattern} -> {name}", self.rules_list)
        self.forget_button.setEnabled(bool(self.engine.config.rules.rules))

    def _show_conflicts(self, conflicts: list[str]) -> None:
        self.conflict_banner.setHidden(not conflicts)
        if not conflicts:
            return
        theme = Theme(self)
        self.conflict_banner.setText("\n".join(conflicts))
        self.conflict_banner.setStyleSheet(
            f"color: {theme.warn.name()}; font-weight: bold; padding: 6px;"
            f"border: 1px solid {theme.warn.name()}; border-radius: 4px;"
        )

    def _set_status(self, text: str, warn: bool = False) -> None:
        theme = Theme(self)
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {(theme.warn if warn else theme.dim).name()};"
        )

    def _output_choices(self) -> list[tuple[str, str]]:
        return [(c.slug, c.name) for c in self.engine.config.channels if not c.is_input]

    @staticmethod
    def _device_choices(status: dict, channel) -> list[tuple[str, str]]:
        key = "input_devices" if channel is not None and channel.is_input else "devices"
        return [(d["name"], d["label"]) for d in status.get(key, [])]

    def _channel_selected(self) -> None:
        channel = self.selected_channel
        status = getattr(self, "_status", {})
        devices = self._device_choices(status, channel)
        self.channel_panel.set_outputs(self._output_choices())
        entry = next(
            (c for c in status.get("channels", [])
             if channel is not None and c["slug"] == channel.slug),
            None,
        )
        self.channel_panel.set_channel(
            channel, devices, present=entry["device_present"] if entry else True
        )
        self.channel_panel.show_status(entry)
        self.effects_panel.set_channel(channel)
        try:
            graph = self.engine.graph()
        except PwError:
            graph = None
        self.meters.follow(channel, graph)
        self.remove_output_button.setEnabled(channel is not None and not channel.is_input)
        self.remove_input_button.setEnabled(channel is not None and channel.is_input)

    # -- actions -----------------------------------------------------------

    def _config_edited(self) -> None:
        """An edit happened: save now, restart the audio in a moment."""
        try:
            self.engine.save()
        except OSError as exc:
            self._error("Could not save your settings", str(exc))
            return
        self._refresh_channel_list()
        self._set_status("Updating...")
        self._structural_pending = True
        self._pending_apply.start(APPLY_DELAY_MS)

    def _volume_changed(self, volume: float) -> None:
        channel = self.selected_channel
        if channel is not None:
            self._set_volume(channel.slug, volume)

    def _set_volume(self, slug: str, volume: float) -> None:
        if self._pending_volume is not None and self._pending_volume[0] != slug:
            self._write_volume()  # another channel's drag: do not drop it
        self._pending_volume = (slug, volume)
        if not self._volume_timer.isActive():
            self._volume_timer.start()

    def _write_volume(self) -> None:
        if self._pending_volume is None:
            return
        slug, volume = self._pending_volume
        self._pending_volume = None
        try:
            self.engine.set_channel_volume(slug, volume)
        except USER_ERRORS as exc:
            self._set_status(str(exc), warn=True)

    def _mute_changed(self, muted: bool) -> None:
        channel = self.selected_channel
        if channel is not None:
            self._set_muted(channel.slug, muted)

    def _set_muted(self, slug: str, muted: bool) -> None:
        try:
            self.engine.set_channel_muted(slug, muted)
        except USER_ERRORS as exc:
            self._set_status(str(exc), warn=True)

    # -- from the mixer ----------------------------------------------------

    def _toggle_effect(self, slug: str, index: int, on: bool) -> None:
        channel = self.engine.config.channel(slug)
        if not 0 <= index < len(channel.effects) or channel.effects[index].enabled == on:
            return
        effect = channel.effects[index]
        effect.enabled = on
        if channel is self.selected_channel:
            self.effects_panel.refresh()
        # On/off is live behind the bypass switch, unless the effect cannot run
        # here: then it is left out of the graph, which changes its shape.
        if effect.spec.unsatisfied():
            self._config_edited()
        else:
            self._config_tuned()

    def _set_fader(self, slug: str, db: float) -> None:
        """The post-insert fader: a live control change, like a knob."""
        channel = self.engine.config.channel(slug)
        if db != channel.fader_db:
            channel.fader_db = db
            self._config_tuned()

    def _set_pan(self, slug: str, pan: float) -> None:
        """Balance at the fader: live, like the fader itself."""
        channel = self.engine.config.channel(slug)
        if pan != channel.pan:
            channel.pan = pan
            self._config_tuned()

    def _set_solo(self, slug: str, on: bool) -> None:
        """A solo cuts the rest of its kind at their faders: live, no restarts."""
        channel = self.engine.config.channel(slug)
        if on != channel.solo:
            channel.solo = on
            self.engine.config.update_solo()
            self.mixer.show_solo()
            self._config_tuned()

    def _open_effect(self, slug: str, index: int) -> None:
        """Show a channel (and one of its effects) in the Channels view."""
        self.select_channel(slug)
        self.views.setCurrentIndex(1)
        if 0 <= index < self.effects_panel.list.count():
            self.effects_panel.list.setCurrentRow(index)

    def _add_effect_to(self, slug: str) -> None:
        self.select_channel(slug)
        self.effects_panel.choose_effect()

    def _set_device(self, slug: str, device: str) -> None:
        channel = self.engine.config.channel(slug)
        if device == channel.device:
            return
        channel.device = device
        if device == NOWHERE:
            channel.recordable = True  # as the Channels view does: it must go somewhere
        if channel is self.selected_channel:
            self._channel_selected()
        self._config_edited()

    def _set_listen(self, slug: str, through: str) -> None:
        channel = self.engine.config.channel(slug)
        if through != channel.listen:
            channel.listen = through
            if channel is self.selected_channel:
                self._channel_selected()
            self._config_edited()

    def _update_meters(self) -> None:
        """Run only the meters that can be seen."""
        shown = self.isVisible() and not self.isMinimized()
        mixer = self.views.currentWidget() is self.mixer
        self.mixer.set_active(shown and mixer)
        self.meters.set_active(shown and not mixer)

    def _config_tuned(self) -> None:
        """A knob moved: save now, and apply it live very soon.

        The timer is not restarted by each new value, or a continuous slider
        drag would never be heard until the mouse stopped.
        """
        try:
            self.engine.save()
        except OSError as exc:
            self._error("Could not save your settings", str(exc))
            return
        if not self._pending_apply.isActive():
            self._pending_apply.start(TUNE_DELAY_MS)

    def apply_now(self) -> None:
        """Hand the current settings to the worker; results arrive as signals."""
        self._pending_apply.stop()
        structural, self._structural_pending = self._structural_pending, False
        self.applier.request(structural)

    def _apply_started(self, structural: bool) -> None:
        # Only a restart is slow enough to deserve a busy cursor; flashing one
        # sixteen times a second during a slider drag would be worse than none.
        # BusyCursor, not WaitCursor: the window is still usable.
        if structural and not self._busy_cursor:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
            self._busy_cursor = True

    def _apply_finished(self, report, structural: bool) -> None:
        if report.failures:
            self._set_status(report.failures[0].describe(), warn=True)
        elif not structural and not report.restarted:
            return  # knobs changed in place: nothing else on screen is different
        self.refresh()

    def _apply_failed(self, message: str, expected: bool) -> None:
        title = "Could not update the audio channels"
        if not expected:
            title += " (unexpected error)"
        self._error(title, message)

    def _apply_idle(self) -> None:
        if self._busy_cursor:
            QGuiApplication.restoreOverrideCursor()
            self._busy_cursor = False
        if self.status_label.text() == "Updating...":
            self.refresh()

    def _add_channel(self, is_input: bool) -> None:
        name, ok = QInputDialog.getText(
            self, "New input" if is_input else "New output",
            "What is this microphone or input for?" if is_input else "What is this channel for?",
        )
        if not ok or not name.strip():
            return
        slug = slug_for(name.strip(), set(self.engine.config.channel_slugs))
        try:
            validate_slug(slug)
            if is_input:
                # Start on the default input: the right mic for most people,
                # and it follows the desktop's choice.
                self.engine.create_channel(slug, name.strip(), "", kind=INPUT)
            else:
                devices = getattr(self, "_status", {}).get("devices", [])
                self.engine.create_channel(slug, name.strip(),
                                           devices[0]["name"] if devices else "")
        except USER_ERRORS as exc:
            self._error("Could not create that channel", str(exc))
            return
        self._selected_slug = slug
        self._refresh_channel_list()
        self._config_edited()

    def _remove_channel(self) -> None:
        channel = self.selected_channel
        if channel is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete channel",
            f"Delete {channel.name}? Anything playing through it will move to "
            "your normal output.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.engine.delete_channel(channel.slug)
        except USER_ERRORS as exc:
            self._error("Could not delete that channel", str(exc))
            return
        self._refresh_channel_list()
        self.refresh()

    def _auto_route_toggled(self, on: bool) -> None:
        self.engine.config.auto_route = on
        self.engine.save()

    def _background_toggled(self, on: bool) -> None:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            if on:
                install.enable_login_service()
            else:
                install.disable_login_service()
        except (install.InstallError, OSError) as exc:
            self.background.blockSignals(True)
            self.background.setChecked(not on)
            self.background.blockSignals(False)
            self._error("Could not change the background service", str(exc))
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        if on:
            # The service routes from now on; two routers would both place
            # every new stream.
            self._stop_auto_router()
        else:
            self._start_auto_router()

    def _send_stream(self, stream_id: int, slug: str) -> None:
        try:
            self.engine.send(stream_id, slug, remember_new=True)
        except USER_ERRORS as exc:
            self._error("Could not move that app", str(exc))
        if self.auto is not None:
            # The user has spoken; do not let the rules drag it back.
            self.auto.remember(stream_id)
        self.refresh()

    def _remember_stream(self, stream_id: int, slug: str) -> None:
        node = self.engine.graph().node(stream_id)
        if node is None:
            return
        try:
            self.engine.remember_app(node.app_name, slug)
        except USER_ERRORS as exc:
            self._error("Could not remember that app", str(exc))
            return
        self._refresh_rules()

    def _forget_rule(self) -> None:
        row = self.rules_list.currentRow()
        if row < 0:
            return
        try:
            self.engine.remove_rule(row)
        except USER_ERRORS as exc:
            self._error("Could not forget that app", str(exc))
            return
        self._refresh_rules()

    # -- first run and shutdown -------------------------------------------

    def _offer_first_run(self) -> None:
        if self.engine.config.channels:
            return
        devices = getattr(self, "_status", {}).get("devices", [])
        if not devices:
            return
        answer = QMessageBox.question(
            self,
            "Set up channels",
            "Create one channel for each of your outputs to start with? "
            "They will sound exactly as they do now until you add an effect.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.engine.adopt_devices()
        except USER_ERRORS as exc:
            self._error("Could not create channels", str(exc))
            return
        self._refresh_channel_list()
        self.apply_now()

    def _start_auto_router(self) -> None:
        if self.auto is not None or daemon_pid() is not None:
            return
        try:
            self.auto = AutoRouter(self.engine)
            self.auto.start()
        except PwError:
            self.auto = None

    def _stop_auto_router(self) -> None:
        if self.auto is not None:
            self.auto.stop()
            self.auto = None

    def _monitor_failed(self, message: str) -> None:
        self._set_status(f"Not watching for new apps: {message}", warn=True)

    def _error(self, title: str, detail: str) -> None:
        QMessageBox.warning(self, title, detail)
        self._set_status(detail, warn=True)

    def _meter_graph(self):
        if self.bridge.running:
            return self.bridge.graph
        return self.engine.graph(refresh=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_meters()

    def hideEvent(self, event) -> None:
        # Also minimising: no meter runs - or holds a microphone open - unseen.
        super().hideEvent(event)
        self.mixer.set_active(False)
        self.meters.set_active(False)

    def closeEvent(self, event) -> None:
        # Never leave a restart half done (two hosts for one channel), and
        # never drop an edit that is saved but not yet heard.
        if self._pending_apply.isActive():
            self.apply_now()
        self.applier.flush()
        self.meters.stop()
        self.mixer.stop()
        self._stop_auto_router()
        self.bridge.stop()
        super().closeEvent(event)


def _set_up_package(wait_s: float = 3.0) -> None:
    """Switch routing from login on at a packaged app's first start.

    Then wait briefly for the service to announce itself: the window starts its
    own router when it finds none, and two routers both place every new stream.
    """
    try:
        if not install.set_up_for_user():
            return
    except (install.InstallError, OSError) as exc:
        print(f"audiorouter: could not start routing at login: {exc}", file=sys.stderr)
        return
    deadline = time.monotonic() + wait_s
    while daemon_pid() is None and time.monotonic() < deadline:
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Audio Router")
    app.setDesktopFileName(install.APP_ID)
    try:
        engine = Engine.load()
    except ConfigError as exc:
        QMessageBox.critical(None, "Audio Router", f"Your settings could not be read:\n\n{exc}")
        return 2
    native.ensure_all()  # well under a second each, and only when missing or stale
    _set_up_package()
    window = MainWindow(engine)
    window.show()
    return app.exec()


def _entry():  # pragma: no cover - console-script shim
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - `python -m audiorouter.gui.main`
    _entry()
