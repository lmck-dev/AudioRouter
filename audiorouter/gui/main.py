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
from PyQt6.QtGui import QAction, QFontMetrics, QGuiApplication
from PyQt6.QtWidgets import (
    QWIDGETSIZE_MAX,
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import install, native, session
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
from . import single
from .mixer import MixerView, desk_order, group_label, kind_label
from .monitor import GraphBridge
from .streams_panel import StreamsPanel
from .theme import Theme

#: Collecting edits for this long turns a burst of typing into one restart.
APPLY_DELAY_MS = 700
#: A knob change is applied to the running channel, so it can be heard almost
#: at once; this only batches the flood of values a slider drag produces.
TUNE_DELAY_MS = 60
#: How often to look for settings another process saved (a stat, nothing more).
ADOPT_INTERVAL_MS = 500

#: After "Restart the sound system", the button stays disabled this long.
EC_FIX_SETTLE_S = 20.0
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
        self.bridge.reconnected.connect(self._feed_reconnected)
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
        # The same for a real device's own volume (the mixer's MICS / OUTPUTS).
        self._pending_device_volume: tuple[str, float] | None = None
        self._device_volume_timer = QTimer(self)
        self._device_volume_timer.setSingleShot(True)
        self._device_volume_timer.setInterval(TUNE_DELAY_MS)
        self._device_volume_timer.timeout.connect(self._write_device_volume)

        # Settings saved by another process - the phone remote in the login
        # service, or the command line - are adopted here. Every edit made in
        # this window is saved at once, so nothing of ours is ever unsaved.
        self._adopt_timer = QTimer(self)
        self._adopt_timer.setInterval(ADOPT_INTERVAL_MS)
        self._adopt_timer.timeout.connect(self.adopt_saved_settings)

        self._build()
        self._connect()

        if self.bridge.start():
            self.engine.use_graph(self.bridge.graph)
        self.refresh()
        self._offer_first_run()
        self._start_auto_router()
        self._adopt_timer.start()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        # One switch for "is Audio Router the problem?": every channel off,
        # nothing routed, and all of it back when switched off (owner, 1 Oct).
        self.bypass = QPushButton("Bypass", self)
        self.bypass.setCheckable(True)
        self.bypass.setChecked(self.engine.config.bypass)
        # Wide enough for either label (bold when on), so the bar never shifts.
        bold = self.bypass.font()
        bold.setBold(True)
        self.bypass.setMinimumWidth(QFontMetrics(bold).horizontalAdvance("Bypassed") + 32)
        self.bypass.setToolTip(
            "Switch Audio Router off without losing anything: every channel stops and "
            "apps play straight to your devices. Switch it back to bring every channel, "
            "effect and routing back. Stays on across logins until you switch it off."
        )
        self._route_after_apply = False
        self.bypass_banner = QLabel(
            "Bypassed: every channel is stopped and nothing is routed. Apps play straight "
            "to your default devices. Click Bypass again to bring everything back.", self)
        self.bypass_banner.setWordWrap(True)
        self.bypass_banner.setHidden(True)
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
        # WirePlumber restarted on its own breaks echo cancellation until the
        # whole sound system restarts; the user chooses when (see session.py).
        self.ec_banner = QFrame(self)
        self.ec_label = QLabel(session.MESSAGE, self.ec_banner)
        self.ec_label.setWordWrap(True)
        self.ec_fix = QPushButton("Restart the sound system", self.ec_banner)
        self.ec_fix.setToolTip("Restarts PipeWire, its Pulse server and WirePlumber, then brings "
                               "your channels back. Every sound stops for a few seconds.")
        self.ec_fix.clicked.connect(self._restart_sound_system)
        ec_row = QHBoxLayout(self.ec_banner)
        ec_row.setContentsMargins(6, 6, 6, 6)
        ec_row.addWidget(self.ec_label, 1)
        ec_row.addWidget(self.ec_fix)
        self.ec_banner.setHidden(True)
        self._ec_fix_started = 0.0

        top = QHBoxLayout()
        top.addWidget(self.bypass)
        top.addWidget(self.auto_route)
        top.addWidget(self.background)
        top.addStretch(1)
        top.addWidget(self.status_label)
        self.phone_button = QPushButton("Phone remote", self)
        self.phone_button.setToolTip("Control the mixer from your phone, and pair it")
        self.phone_button.clicked.connect(self._show_phone)
        top.addWidget(self.phone_button)
        self.guide_button = QPushButton("User Guide", self)
        self.guide_button.setToolTip("What every part of Audio Router does")
        self.guide_button.clicked.connect(self._show_guide)
        top.addWidget(self.guide_button)
        self.about_button = QPushButton("About", self)
        self.about_button.setToolTip("Version, credits and system details")
        self.about_button.clicked.connect(self._show_about)
        top.addWidget(self.about_button)

        # One list, in the mixer's order and with its captions (owner, 2 Oct
        # 2026): every channel is the same kind of thing, read IN -> OUT.
        self.channel_list = QListWidget(self)
        self.add_button = QToolButton(self)
        self.add_button.setText("New channel")
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        add_menu = QMenu(self.add_button)
        add_menu.addAction("Channel for a microphone", lambda: self._add_channel(is_input=True))
        add_menu.addAction("Channel for apps", lambda: self._add_channel(is_input=False))
        self.add_button.setMenu(add_menu)
        self.remove_button = QPushButton("Delete", self)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.add_button)
        list_buttons.addWidget(self.remove_button)

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
        left_layout.addWidget(QLabel("Channels - as on the mixer, left to right", left))
        left_layout.addWidget(self.channel_list, 1)
        left_layout.addLayout(list_buttons)

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
        # The Channels view is tall (its panel never squeezes, see above), and
        # a tab widget is as tall as its tallest tab: unscrolled, it alone kept
        # Playing now a sliver at the bottom. It scrolls when short instead.
        channels_view = QScrollArea(self)
        channels_view.setWidgetResizable(True)
        channels_view.setFrameShape(QFrame.Shape.NoFrame)
        channels_view.setWidget(splitter)
        self.views.addTab(channels_view, "Channels")

        self.streams_panel = StreamsPanel(self.engine, self)
        self.streams_panel.add_beside(self.rules)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self.bypass_banner)
        layout.addWidget(self.conflict_banner)
        layout.addWidget(self.ec_banner)
        layout.addLayout(top)
        # Dragging the handle between the views and Playing now sets how tall
        # Playing now is; the window remembers it.
        self.streams_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.streams_splitter.setChildrenCollapsible(False)
        self.streams_splitter.setHandleWidth(8)
        self.streams_splitter.addWidget(self.views)
        self.streams_splitter.addWidget(self.streams_panel)
        self.streams_splitter.setStretchFactor(0, 2)
        self.streams_splitter.setStretchFactor(1, 1)
        state = self.settings.value("streams_splitter")
        if state is not None:
            self.streams_splitter.restoreState(state)
        self.streams_splitter.splitterMoved.connect(self._streams_resized)
        layout.addWidget(self.streams_splitter, 1)
        self.setCentralWidget(central)
        self._streams_folded(self._setting_bool("streams_expanded", True))
        self.meters.follow_effect.setChecked(self._setting_bool("meters_follow_effect", True))

        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

    def _connect(self) -> None:
        self.channel_list.currentItemChanged.connect(self._list_item_changed)
        self.remove_button.clicked.connect(self._remove_channel)
        self.streams_panel.expanded_changed.connect(self._streams_folded)
        self.views.currentChanged.connect(lambda _i: self._update_meters())
        self.mixer.volume_changed.connect(self._set_volume)
        self.mixer.fader_changed.connect(self._set_fader)
        self.mixer.mute_toggled.connect(self._set_muted)
        self.mixer.set_inserts_open(self._setting_bool("mixer_inserts_open", True))
        self.mixer.inserts_toggled.connect(lambda on: self.settings.setValue("mixer_inserts_open", on))
        self.mixer.device_volume_changed.connect(self._set_device_volume)
        self.mixer.device_mute_toggled.connect(self._set_device_muted)
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
        self.channel_panel.remember_app.connect(self._remember_on_selected)
        self.channel_panel.forget_rule.connect(self._forget_rule_at)
        self.channel_panel.fader_changed.connect(
            lambda db: self.selected_channel and self._set_fader(self.selected_channel.slug, db))
        self.effects_panel.changed.connect(self._config_edited)
        self.effects_panel.tuned.connect(self._config_tuned)
        self.effects_panel.highlighted.connect(
            lambda row: self.meters.show_effect(row, self.selected_channel)
        )
        self.meters.follow_effect.toggled.connect(
            lambda on: self.settings.setValue("meters_follow_effect", on)
        )
        self.bypass.toggled.connect(self._bypass_toggled)
        self.auto_route.toggled.connect(self._auto_route_toggled)
        self.background.toggled.connect(self._background_toggled)
        self.effects_panel.edit_plugin_folders = self._edit_plugin_folders
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
        """Select a channel in the list, and load it."""
        self._selected_slug = slug
        lst = self.channel_list
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
        # Folded, the panel is only its heading and the rest goes to the views;
        # unfolded, it may be dragged to any height again.
        heading = self.streams_panel.toggle.sizeHint().height()
        self.streams_panel.setMaximumHeight(QWIDGETSIZE_MAX if expanded else heading)
        self.settings.setValue("streams_expanded", expanded)

    def _streams_resized(self, _pos: int, _index: int) -> None:
        if self.streams_panel.expanded:
            self.settings.setValue("streams_splitter", self.streams_splitter.saveState())

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
        self._show_bypass(self.engine.config.bypass)
        self._show_conflicts(status.get("conflicts", []))
        self._show_echo_cancel(bool(status.get("echo_cancel_broken")))
        self._refresh_channel_list(status)
        self._refresh_rules()
        self.streams_panel.refresh(status)
        entry = next(
            (c for c in status["channels"]
             if self.selected_channel and c["slug"] == self.selected_channel.slug),
            None,
        )
        self.channel_panel.show_status(entry)
        self.mixer.refresh(self.engine.config, status)
        # A restart replaces the nodes a meter reads; this reopens its taps.
        self.meters.follow(self.selected_channel, self.engine.graph())
        self.channel_panel.set_outputs(self._output_choices())
        self.channel_panel.set_devices(
            self._device_choices(status, self.selected_channel)
            + self._group_choices(self.selected_channel),
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

    def bring_forward(self) -> None:
        """Audio Router was opened again: show this window rather than a second."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def adopt_saved_settings(self) -> None:
        """Show settings another process saved, without applying them.

        Whoever saved them applies them (the remote does, at once); applying
        here as well would restart the same channels twice. Values only (a
        fader, a solo) keep every object and widget; a change of shape - a
        channel or effect added or removed - rebuilds the lists.
        """
        try:
            changed = self.engine.adopt_saved()
        except OSError:
            return
        if changed is None:
            return
        config = self.engine.config
        self.bypass.blockSignals(True)
        self.bypass.setChecked(config.bypass)
        self.bypass.blockSignals(False)
        self.auto_route.blockSignals(True)
        self.auto_route.setChecked(config.auto_route)
        self.auto_route.blockSignals(False)
        if changed == "shape":
            self._refresh_channel_list()
        else:
            self.channel_panel.show_fader()
            # An effect switched on or off elsewhere; never under a held slider.
            if QGuiApplication.mouseButtons() == Qt.MouseButton.NoButton:
                self.effects_panel.refresh()
        self.refresh()

    def _refresh_channel_list(self, status: dict | None = None) -> None:
        status = status if status is not None else getattr(self, "_status", None)
        running = (
            {c["slug"] for c in status["channels"] if c["running"]} if status else set()
        )
        config = self.engine.config
        lst = self.channel_list
        lst.blockSignals(True)
        lst.clear()
        channels, groups = desk_order(config)
        for channel in channels + groups:
            label = f"{channel.name}  -  {kind_label(config, channel)}"
            if not channel.enabled:
                label += "  (off)"
            elif channel.slug not in running:
                label += "  (not running)"
            item = QListWidgetItem(label, lst)
            item.setData(Qt.ItemDataRole.UserRole, channel.slug)
        lst.blockSignals(False)
        shown = [c.slug for c in channels + groups]
        if self._selected_slug not in shown:
            self._selected_slug = shown[0] if shown else None
        self.select_channel(self._selected_slug)

    def _refresh_rules(self) -> None:
        self.rules_list.clear()
        for rule in self.engine.config.rules.rules:
            try:
                name = self.engine.config.channel(rule.channel).name
            except ConfigError:
                name = rule.channel
            arrow = "records" if rule.record else "->"
            QListWidgetItem(f"{rule.pattern} {arrow} {name}", self.rules_list)
        self.forget_button.setEnabled(bool(self.engine.config.rules.rules))
        # The channel panel lists the same rules for the channel it shows.
        self._show_sources(self.selected_channel, getattr(self, "_status", {}))

    def _show_echo_cancel(self, broken: bool) -> None:
        self.ec_banner.setHidden(not broken)
        if not broken:
            return
        theme = Theme(self)
        self.ec_banner.setStyleSheet(
            f"QFrame {{ border: 1px solid {theme.warn.name()}; border-radius: 4px; }}"
            f"QLabel {{ border: none; color: {theme.warn.name()}; font-weight: bold; }}")
        # A restart takes several seconds; one click is enough.
        restarting = time.monotonic() - self._ec_fix_started < EC_FIX_SETTLE_S
        self.ec_fix.setEnabled(not restarting)
        self.ec_fix.setText("Restarting..." if restarting else "Restart the sound system")

    def _restart_sound_system(self) -> None:
        self._ec_fix_started = time.monotonic()
        self.ec_fix.setEnabled(False)
        self.ec_fix.setText("Restarting...")
        try:
            session.restart_sound_system()
        except OSError as exc:
            self._ec_fix_started = 0.0
            self._error("Could not restart the sound system", str(exc))

    def _show_bypass(self, on: bool) -> None:
        self.bypass_banner.setHidden(not on)
        self.bypass.setText("Bypassed" if on else "Bypass")
        theme = Theme(self)
        warn = theme.warn.name()
        self.bypass.setStyleSheet(
            f"QPushButton:checked {{ color: {warn}; font-weight: bold; border: 1px solid {warn};"
            " border-radius: 4px; padding: 4px 10px; }")
        self.bypass_banner.setStyleSheet(
            f"color: {warn}; font-weight: bold; padding: 6px;"
            f"border: 1px solid {warn}; border-radius: 4px;"
        )

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
        return [(c.slug, c.name) for c in self.engine.config.channels
                if not c.is_input and not c.companion_of]

    @staticmethod
    def _device_choices(status: dict, channel) -> list[tuple[str, str]]:
        key = "input_devices" if channel is not None and channel.is_input else "devices"
        return [(d["name"], d["label"]) for d in status.get(key, [])]

    def _group_choices(self, channel) -> list[tuple[str, str]]:
        """Output channels this one can play into (as a group), loops left out."""
        if channel is None:
            return []
        return [(c.node_name, group_label(c.name, bool(c.companion_of)))
                for c in self.engine.config.group_choices(channel)]

    def _channel_selected(self) -> None:
        channel = self.selected_channel
        status = getattr(self, "_status", {})
        devices = self._device_choices(status, channel) + self._group_choices(channel)
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
        self._show_sources(channel, status)
        self.effects_panel.set_channel(channel)
        try:
            graph = self.engine.graph()
        except PwError:
            graph = None
        self.meters.follow(channel, graph)
        self.remove_button.setEnabled(channel is not None)

    def _feed_slug(self, channel) -> str:
        """Where apps sent to this channel actually play: a mic channel's mix."""
        companion = self.engine.config.companion(channel)
        return companion.slug if companion is not None else channel.slug

    def _show_sources(self, channel, status: dict) -> None:
        """The channel panel's IN: apps playing in, channels in, remembered apps."""
        if channel is None:
            self.channel_panel.show_sources([], [], [], [])
            return
        config = self.engine.config
        feed = self._feed_slug(channel)
        target = config.channel(feed)
        streams = status.get("streams", [])
        # IN is what plays into the channel: recording apps and their rules
        # belong elsewhere.
        streams = [s for s in streams if not s.get("recording")]
        playing = sorted({s["app"] or "?" for s in streams if s.get("channel") == feed})
        members = [m.name for m in config.members_of(target)]
        rules = [(i, r.pattern) for i, r in enumerate(config.rules.rules)
                 if r.channel == feed and not r.record]
        here = {pattern.casefold() for _i, pattern in rules}
        offered = [s["app"] for s in streams if s.get("app")]
        offered += [r.pattern for r in config.rules.rules if r.field == "app" and not r.record]
        candidates = sorted({a for a in offered if a.casefold() not in here}, key=str.casefold)
        self.channel_panel.set_kind_label(kind_label(config, channel))
        self.channel_panel.show_sources(playing, members, rules, candidates)

    def _remember_on_selected(self, app: str) -> None:
        channel = self.selected_channel
        if channel is None:
            return
        try:
            self.engine.remember_app(app, self._feed_slug(channel))
        except USER_ERRORS as exc:
            self._error("Could not remember that app", str(exc))
            return
        self._refresh_rules()

    def _forget_rule_at(self, index: int) -> None:
        try:
            self.engine.remove_rule(index)
        except USER_ERRORS as exc:
            self._error("Could not forget that app", str(exc))
            return
        self._refresh_rules()

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

    def _set_device_volume(self, name: str, volume: float) -> None:
        if self._pending_device_volume is not None and self._pending_device_volume[0] != name:
            self._write_device_volume()
        self._pending_device_volume = (name, volume)
        if not self._device_volume_timer.isActive():
            self._device_volume_timer.start()

    def _write_device_volume(self) -> None:
        if self._pending_device_volume is None:
            return
        name, volume = self._pending_device_volume
        self._pending_device_volume = None
        try:
            self.engine.set_device_volume(name, volume)
        except USER_ERRORS as exc:
            self._set_status(str(exc), warn=True)

    def _set_device_muted(self, name: str, muted: bool) -> None:
        try:
            self.engine.set_device_muted(name, muted)
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
            # Both views show this fader; keep the one not being moved in step.
            if channel is self.selected_channel:
                self.channel_panel.show_fader()
            if slug in self.mixer.strips:
                self.mixer.strips[slug].show_fader()
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
        if self._route_after_apply and not self.engine.config.bypass and not self.applier.busy:
            self._route_after_apply = False
            try:
                self.engine.route()
            except USER_ERRORS as exc:
                self._set_status(f"Could not route after bypass: {exc}", warn=True)
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

    def _bypass_toggled(self, on: bool) -> None:
        try:
            self.engine.set_bypass(on)
        except OSError as exc:
            self.bypass.blockSignals(True)
            self.bypass.setChecked(not on)
            self.bypass.blockSignals(False)
            self._error("Could not change bypass", str(exc))
            return
        # Switching back on: once the channels are up, send what is playing
        # to its usual channel - it spent the bypass on the default output.
        self._route_after_apply = not on
        self._show_bypass(on)
        self._structural_pending = True
        self.apply_now()

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

    def _edit_plugin_folders(self, parent: QWidget) -> bool:
        """The plugin folders dialog. True when the folders changed."""
        from .effects_panel import PluginFoldersDialog

        dialog = PluginFoldersDialog(list(self.engine.config.plugin_folders), parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        if dialog.folders() == self.engine.config.plugin_folders:
            return False
        try:
            self.engine.set_plugin_folders(dialog.folders())
        except USER_ERRORS as exc:
            self._error("Could not use those plugin folders", str(exc))
            return False
        return True

    def _remember_stream(self, stream_id: int, slug: str) -> None:
        node = self.engine.graph().node(stream_id)
        if node is None:
            return
        try:
            self.engine.remember_app(node.app_name, slug, record=node.is_input_stream)
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

    def _feed_reconnected(self) -> None:
        """PipeWire came back after a restart: pick everything up again."""
        self.engine.use_graph(self.bridge.graph)
        if self.auto is not None and self.auto.ended:
            self._stop_auto_router()
        self._start_auto_router()
        # The restart took every channel's sink. The login service re-applies
        # as systemd restarts it; without it, this window must.
        if daemon_pid() is None:
            self.apply_now()

    def _show_guide(self) -> None:
        """Open the user guide, or bring its window forward if it is open."""
        from .guide import GuideWindow

        if getattr(self, "_guide", None) is None:
            self._guide = GuideWindow(self)
        self._guide.show()
        self._guide.raise_()
        self._guide.activateWindow()

    def _show_about(self) -> None:
        from .about import AboutWindow, system_facts

        def facts():
            try:
                version = self.engine.graph().daemon_version()
            except PwError:
                version = None
            return system_facts(version, daemon_pid() is not None)

        if getattr(self, "_about", None) is None:
            self._about = AboutWindow(facts, self)
        self._about.show()
        self._about.raise_()
        self._about.activateWindow()

    def _show_phone(self) -> None:
        from .phone import PhoneRemoteWindow

        # Made afresh each time: the settings file may have changed meanwhile
        # (`audiorouter remote on` from a terminal).
        if getattr(self, "_phone", None) is not None:
            self._phone.close()
        self._phone = PhoneRemoteWindow(self)
        self._phone.show()

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
    if single.show_running_window():
        return 0  # one window at a time: the open one comes forward instead
    try:
        lock = single.WindowLock()
    except OSError as exc:  # pragma: no cover - the socket directory is unwritable
        print(f"audiorouter: could not claim the window lock: {exc}", file=sys.stderr)
        lock = None
    try:
        engine = Engine.load()
    except ConfigError as exc:
        QMessageBox.critical(None, "Audio Router", f"Your settings could not be read:\n\n{exc}")
        return 2
    native.ensure_all()  # well under a second each, and only when missing or stale
    _set_up_package()
    window = MainWindow(engine)
    if lock is not None:
        lock.show_requested.connect(window.bring_forward)
    window.show()
    return app.exec()


def _entry():  # pragma: no cover - console-script shim
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - `python -m audiorouter.gui.main`
    _entry()
