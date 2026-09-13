"""The main window.

Laid out in the order someone actually thinks: the channels they have made, the
one they are editing, and underneath, everything that is currently making sound
with a menu to send it somewhere. Nothing here decides anything about audio; it
edits the configuration and asks the engine to make reality match.

Edits apply themselves. A channel has to be restarted to change its effects, so
changes are collected for a moment and applied in one go rather than on every
keystroke, and streams are put back where they were afterwards.
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt, QTimer
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
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import install
from ..channels import ChannelError, validate_slug
from ..config import ConfigError
from ..effects import EffectError
from ..engine import AutoRouter, Engine, EngineError, daemon_pid
from ..pwgraph import PwError
from ..routing import RoutingError
from .channel_panel import ChannelPanel
from .effects_panel import EffectsPanel
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


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
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

        top = QHBoxLayout()
        top.addWidget(self.auto_route)
        top.addWidget(self.background)
        top.addStretch(1)
        top.addWidget(self.status_label)

        self.channel_list = QListWidget(self)
        self.add_channel_button = QPushButton("New channel", self)
        self.remove_channel_button = QPushButton("Delete", self)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.add_channel_button)
        list_buttons.addWidget(self.remove_channel_button)

        self.rules_list = QListWidget(self)
        self.rules_list.setMaximumHeight(120)
        self.forget_button = QPushButton("Forget", self)
        rules_label = QLabel("Remembered apps", self)

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Channels", left))
        left_layout.addWidget(self.channel_list, 1)
        left_layout.addLayout(list_buttons)
        left_layout.addWidget(rules_label)
        left_layout.addWidget(self.rules_list)
        left_layout.addWidget(self.forget_button)

        self.channel_panel = ChannelPanel(self)
        self.effects_panel = EffectsPanel(self)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.channel_panel)
        right_layout.addWidget(self.effects_panel, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        self.streams_panel = StreamsPanel(self.engine, self)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(splitter, 2)
        layout.addWidget(self.streams_panel, 1)
        self.setCentralWidget(central)

        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

    def _connect(self) -> None:
        self.channel_list.currentRowChanged.connect(self._channel_selected)
        self.add_channel_button.clicked.connect(self._add_channel)
        self.remove_channel_button.clicked.connect(self._remove_channel)
        self.channel_panel.changed.connect(self._config_edited)
        self.channel_panel.renamed.connect(self._refresh_channel_list)
        self.effects_panel.changed.connect(self._config_edited)
        self.effects_panel.tuned.connect(self._config_tuned)
        self.auto_route.toggled.connect(self._auto_route_toggled)
        self.background.toggled.connect(self._background_toggled)
        self.streams_panel.send_requested.connect(self._send_stream)
        self.streams_panel.remember_requested.connect(self._remember_stream)
        self.forget_button.clicked.connect(self._forget_rule)

    # -- state -------------------------------------------------------------

    @property
    def selected_channel(self):
        row = self.channel_list.currentRow()
        channels = self.engine.config.channels
        return channels[row] if 0 <= row < len(channels) else None

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
        self._refresh_channel_list(status)
        self._refresh_rules()
        self.streams_panel.refresh(status)
        entry = next(
            (c for c in status["channels"]
             if self.selected_channel and c["slug"] == self.selected_channel.slug),
            None,
        )
        self.channel_panel.show_status(entry)
        self.channel_panel.set_devices(
            [(d["name"], d["label"]) for d in status["devices"]],
            present=entry["device_present"] if entry else True,
        )
        if not status["devices"]:
            self._set_status("No output devices - is anything plugged in?", warn=True)
        elif status["problems"]:
            self._set_status(status["problems"][0], warn=True)
        else:
            self._set_status("")

    def _refresh_channel_list(self, status: dict | None = None) -> None:
        status = status if status is not None else getattr(self, "_status", None)
        running = (
            {c["slug"] for c in status["channels"] if c["running"]} if status else set()
        )
        row = self.channel_list.currentRow()
        self.channel_list.blockSignals(True)
        self.channel_list.clear()
        for channel in self.engine.config.channels:
            label = channel.name
            if not channel.enabled:
                label += "  (off)"
            elif channel.slug not in running:
                label += "  (not running)"
            QListWidgetItem(label, self.channel_list)
        self.channel_list.blockSignals(False)
        if self.channel_list.count():
            self.channel_list.setCurrentRow(min(max(row, 0), self.channel_list.count() - 1))
        else:
            self._channel_selected(-1)

    def _refresh_rules(self) -> None:
        self.rules_list.clear()
        for rule in self.engine.config.rules.rules:
            try:
                name = self.engine.config.channel(rule.channel).name
            except ConfigError:
                name = rule.channel
            QListWidgetItem(f"{rule.pattern} -> {name}", self.rules_list)
        self.forget_button.setEnabled(bool(self.engine.config.rules.rules))

    def _set_status(self, text: str, warn: bool = False) -> None:
        theme = Theme(self)
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {(theme.warn if warn else theme.dim).name()};"
        )

    def _channel_selected(self, row: int) -> None:
        channel = self.selected_channel
        status = getattr(self, "_status", {})
        devices = [(d["name"], d["label"]) for d in status.get("devices", [])]
        entry = next(
            (c for c in status.get("channels", [])
             if channel is not None and c["slug"] == channel.slug),
            None,
        )
        self.channel_panel.set_channel(
            channel, devices, present=entry["device_present"] if entry else True
        )
        self.effects_panel.set_channel(channel)
        self.remove_channel_button.setEnabled(channel is not None)

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
        self._pending_apply.stop()
        structural = getattr(self, "_structural_pending", False)
        self._structural_pending = False
        # Only a restart is slow enough to deserve a busy cursor; flashing one
        # sixteen times a second during a slider drag would be worse than none.
        if structural:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            report = self.engine.apply()
        except USER_ERRORS as exc:
            self._error("Could not update the audio channels", str(exc))
            return
        finally:
            if structural:
                QGuiApplication.restoreOverrideCursor()
        if report.failures:
            self._set_status(report.failures[0].describe(), warn=True)
        elif not structural and not report.restarted:
            return  # knobs changed in place: nothing else on screen is different
        self.refresh()

    def _add_channel(self) -> None:
        name, ok = QInputDialog.getText(self, "New channel", "What is this channel for?")
        if not ok or not name.strip():
            return
        slug = slug_for(name.strip(), set(self.engine.config.channel_slugs))
        devices = getattr(self, "_status", {}).get("devices", [])
        try:
            validate_slug(slug)
            self.engine.create_channel(slug, name.strip(),
                                       devices[0]["name"] if devices else "")
        except USER_ERRORS as exc:
            self._error("Could not create that channel", str(exc))
            return
        self._refresh_channel_list()
        self.channel_list.setCurrentRow(len(self.engine.config.channels) - 1)
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

    def closeEvent(self, event) -> None:
        self._stop_auto_router()
        self.bridge.stop()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Audio Router")
    app.setDesktopFileName(install.APP_ID)
    try:
        engine = Engine.load()
    except ConfigError as exc:
        QMessageBox.critical(None, "Audio Router", f"Your settings could not be read:\n\n{exc}")
        return 2
    window = MainWindow(engine)
    window.show()
    return app.exec()


def _entry():  # pragma: no cover - console-script shim
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - `python -m audiorouter.gui.main`
    _entry()
