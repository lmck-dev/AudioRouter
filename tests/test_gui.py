"""GUI tests, run headless.

These check wiring, not looks: that selecting a channel loads it, that an edit
schedules an apply rather than restarting audio on every keystroke, and that the
panels ask the engine for things instead of doing them.
"""

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - PyQt6 is optional for the engine
    QApplication = None

from audiorouter.channels import Channel
from audiorouter.config import Config
from audiorouter.effects import Effect
from audiorouter.engine import ApplyReport, Engine
from audiorouter.pwgraph import Graph

from . import fakes
from .test_engine import live_graph

if QApplication is not None:
    from audiorouter.gui.main import MainWindow as _MainWindow

    REAL_START_AUTO_ROUTER = _MainWindow._start_auto_router


def isolated_settings(folder):
    """A patch that keeps the window's view state out of the real ~/.config."""
    from PyQt6.QtCore import QSettings

    path = str(Path(folder) / "gui.conf")
    return mock.patch("audiorouter.gui.main.QSettings",
                      lambda *args: QSettings(path, QSettings.Format.IniFormat))


class FakeReader:
    """Stands in for meter.LevelReader: records what was started and stopped."""

    started: list = []

    def __init__(self, tap, on_levels, on_ended=None):
        self.tap, self.on_levels, self.on_ended = tap, on_levels, on_ended
        self.running = False

    def start(self):
        self.running = True
        FakeReader.started.append(self)

    def stop(self):
        self.running = False


class FakeDriver:
    """Stands in for meter.Driver: no parec, and a test can make it end."""

    started: list = []

    def __init__(self, channel):
        self.slug, self.host = channel.slug, channel.pid()
        self.running = False

    def start(self):
        self.running = True
        FakeDriver.started.append(self)

    def stop(self):
        self.running = False


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class GuiTestCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in (
            mock.patch.object(Graph, "snapshot", staticmethod(live_graph)),
            mock.patch("audiorouter.gui.monitor.GraphBridge.start", lambda self: False),
            mock.patch("audiorouter.gui.main.MainWindow._start_auto_router", lambda self: None),
            mock.patch("audiorouter.install.login_service_enabled", return_value=False),
            # Closing the window runs any apply still queued, and each test's
            # own apply mock has ended by then: never start real channel hosts.
            mock.patch.object(Engine, "apply", return_value=ApplyReport()),
            # Never start a real parec from a test, even if a window is shown.
            mock.patch("audiorouter.gui.meters.LevelReader", FakeReader),
            mock.patch("audiorouter.gui.meters.FileLevelReader", FakeReader),
            mock.patch("audiorouter.gui.mixer.FileLevelReader", FakeReader),
            mock.patch("audiorouter.gui.mixer.LevelReader", FakeReader),  # device strips
            # Nor a real recorder of an input's virtual mic.
            mock.patch("audiorouter.gui.mixer.Driver", FakeDriver),
            # Whether this machine has the level tap must not change a test.
            mock.patch("audiorouter.gui.meters.taps_available", return_value=False),
            isolated_settings(self.tmp.name),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.engine = Engine(
            Config(channels=[
                Channel("speakers", "Speakers", "alsa_output.a",
                        effects=[Effect("highpass", {"frequency": 90})]),
                Channel("phones", "Headphones", "alsa_output.b"),
            ]),
            path=Path(self.tmp.name) / "config.json",
        )
        from audiorouter.gui.main import MainWindow

        self.window = MainWindow(self.engine)
        self.addCleanup(self.window.close)

    def settle(self):
        """Wait for the apply worker and deliver what it reported."""
        self.window.applier.flush()


class WindowTest(GuiTestCase):
    def test_channels_are_listed_with_their_state(self):
        channels = self.window.channel_list
        labels = [channels.item(i).text() for i in range(channels.count())]
        self.assertEqual(labels, ["Speakers  -  From apps  (not running)",
                                  "Headphones  -  From apps  (not running)"])

    def test_selecting_a_channel_loads_its_settings_and_effects(self):
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.assertEqual(self.window.channel_panel.name.text(), "Speakers")
        self.assertEqual(self.window.effects_panel.list.count(), 1)
        self.window.select_channel(self.engine.config.channels[1].slug)
        self.assertEqual(self.window.channel_panel.name.text(), "Headphones")
        self.assertEqual(self.window.effects_panel.list.count(), 0)

    def test_an_unplugged_output_is_still_offered_so_it_is_not_silently_changed(self):
        self.window.select_channel("phones")  # targets alsa_output.b, not in the graph
        self.assertIn("not connected", self.window.channel_panel.device.currentText())

    def test_a_virtual_target_that_exists_is_not_called_disconnected(self):
        # Only hardware appears in the device list, but a channel may point at
        # any sink; saying a working one is unplugged would be a lie.
        self.engine.channel("phones").device = "ar_speakers"
        self.window.refresh()
        self.window.select_channel(self.engine.config.channels[1].slug)
        # Another channel's sink is a group, offered by name.
        self.assertEqual(self.window.channel_panel.device.currentText(), "Into Speakers")

    def test_a_broken_echo_canceller_shows_a_banner_whose_button_restarts_the_sound(self):
        from audiorouter import session

        self.assertTrue(self.window.ec_banner.isHidden())
        with mock.patch.object(session, "echo_cancel_broken", return_value=200):
            self.window.refresh()
        self.assertFalse(self.window.ec_banner.isHidden())
        with mock.patch.object(session, "restart_sound_system") as restart:
            self.window.ec_fix.click()
        restart.assert_called_once_with()
        self.assertFalse(self.window.ec_fix.isEnabled())  # one click is enough
        with mock.patch.object(session, "echo_cancel_broken", return_value=None):
            self.window.refresh()
        self.assertTrue(self.window.ec_banner.isHidden())

    def test_after_a_pipewire_restart_the_window_re_applies_and_routes_again(self):
        from audiorouter.engine import AutoRouter

        dead = mock.Mock(spec=AutoRouter, ended=True)
        self.window.auto = dead
        with mock.patch("audiorouter.gui.main.daemon_pid", return_value=None), \
                mock.patch.object(self.window, "_start_auto_router") as start, \
                mock.patch.object(self.window, "apply_now") as apply_now:
            self.window._feed_reconnected()
        dead.stop.assert_called_once_with()
        start.assert_called_once_with()
        apply_now.assert_called_once_with()  # no login service to bring channels back

    def test_with_the_login_service_running_the_window_leaves_applying_to_it(self):
        with mock.patch("audiorouter.gui.main.daemon_pid", return_value=1234), \
                mock.patch.object(self.window, "apply_now") as apply_now:
            self.window._feed_reconnected()
        apply_now.assert_not_called()

    def test_the_user_guide_button_opens_the_guide_with_its_picture(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QTextDocument

        self.window.guide_button.click()
        guide = self.window._guide
        self.assertTrue(guide.isVisible())
        text = guide.browser.toPlainText()
        self.assertIn("A channel strip, control by control", text)
        self.assertIn("Mic channels", text)
        image = guide.browser.loadResource(QTextDocument.ResourceType.ImageResource.value,
                                           QUrl("signal-flow.png"))
        self.assertFalse(image.isNull())
        self.assertEqual(image.width(), 700)  # scaled to the column (the file is 1344 wide)
        self.window.guide_button.click()  # a second click reuses the window
        self.assertIs(self.window._guide, guide)
        guide.close()

    def test_about_shows_the_version_and_copies_the_details(self):
        from PyQt6.QtGui import QGuiApplication

        from audiorouter import __version__

        self.window.about_button.click()
        about = self.window._about
        self.assertTrue(about.isVisible())
        text = about.body.toPlainText()
        self.assertIn("Apache License 2.0", text)
        self.assertIn("Running on", text)
        self.assertIn("Ko-fi", text)
        with mock.patch("audiorouter.gui.about.QDesktopServices.openUrl") as open_url:
            about.support_button.click()
        self.assertEqual(open_url.call_args.args[0].toString(), "https://ko-fi.com/laughingmanck")
        about.copy_button.click()
        copied = QGuiApplication.clipboard().text()
        self.assertIn(f"Audio Router: {__version__}", copied)
        self.assertIn("Background service:", copied)
        self.assertIn("Settings file:", copied)
        about.close()

    def test_renaming_a_channel_saves_and_reaches_the_list(self):
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.window.channel_panel.name.setText("Desk")
        self.window.channel_panel.name.editingFinished.emit()
        self.assertEqual(Config.load(self.engine.path).channel("speakers").name, "Desk")
        self.assertIn("Desk", self.window.channel_list.item(0).text())

    def test_an_edit_schedules_one_apply_instead_of_restarting_at_once(self):
        # Every keystroke restarting the audio would be unusable.
        with mock.patch.object(Engine, "apply") as apply:
            self.window.select_channel(self.engine.config.channels[0].slug)
            self.window.effects_panel.form._boxes["frequency"].spin.setValue(120)
            self.window.effects_panel.form._boxes["frequency"].spin.setValue(130)
        apply.assert_not_called()
        self.assertTrue(self.window._pending_apply.isActive())

    def test_the_scheduled_apply_eventually_runs(self):
        with mock.patch.object(Engine, "apply", return_value=ApplyReport()) as apply:
            self.window._config_edited()
            self.window.apply_now()
            self.settle()
        apply.assert_called_once()

    def test_moving_a_knob_updates_the_line_in_the_chain(self):
        self.window.select_channel(self.engine.config.channels[0].slug)
        panel = self.window.effects_panel
        panel.form._boxes["frequency"].spin.setValue(250)
        self.assertIn("250", panel.list.item(0).text())

    def test_effect_edits_reach_the_saved_config(self):
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(120)
        self.assertEqual(
            Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 120
        )

    def test_switching_an_effect_off_is_a_quick_live_change(self):
        from PyQt6.QtCore import Qt

        from audiorouter.gui.main import TUNE_DELAY_MS

        self.window.select_channel(self.engine.config.channels[0].slug)
        with mock.patch.object(Engine, "apply"):
            self.window.effects_panel.list.item(0).setCheckState(Qt.CheckState.Unchecked)
            self.assertEqual(self.window._pending_apply.interval(), TUNE_DELAY_MS)

    def test_switching_an_effect_off_keeps_it_in_the_chain(self):
        from PyQt6.QtCore import Qt

        self.window.select_channel(self.engine.config.channels[0].slug)
        self.window.effects_panel.list.item(0).setCheckState(Qt.CheckState.Unchecked)
        saved = Config.load(self.engine.path).channel("speakers").effects
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0].enabled)

    def test_adding_an_effect_selects_it_so_its_knobs_are_visible(self):
        self.window.select_channel(self.engine.config.channels[1].slug)
        panel = self.window.effects_panel
        panel.add_effect("gain")
        self.assertEqual(panel.list.currentRow(), 0)
        self.assertIn("gain_db", panel.form._boxes)

    def test_changing_effects_does_not_leave_the_previous_knobs_behind(self):
        # Orphaned rows stayed painted over the new ones when they were only
        # scheduled for deletion.
        self.window.select_channel(self.engine.config.channels[0].slug)
        panel = self.window.effects_panel
        panel.add_effect("gain")
        self.assertEqual(set(panel.form._boxes), {"gain_db"})
        self.assertEqual(panel.form._layout.rowCount(), 1)

    def test_an_edit_that_reshapes_the_chain_waits_longer_than_a_knob(self):
        from audiorouter.gui.main import APPLY_DELAY_MS, TUNE_DELAY_MS

        self.window.select_channel(self.engine.config.channels[0].slug)
        panel = self.window.effects_panel
        with mock.patch.object(Engine, "apply"):
            panel.form._boxes["frequency"].spin.setValue(150)
            self.assertEqual(self.window._pending_apply.interval(), TUNE_DELAY_MS)
            self.window._pending_apply.stop()
            panel.add_effect("gain")
            self.assertEqual(self.window._pending_apply.interval(), APPLY_DELAY_MS)

    def test_a_slider_drag_is_heard_during_the_drag_not_only_after_it(self):
        # Restarting the short timer on every value would postpone the update
        # until the mouse stopped moving.
        self.window.select_channel(self.engine.config.channels[0].slug)
        box = self.window.effects_panel.form._boxes["frequency"]
        with mock.patch.object(Engine, "apply"):
            box.slider.setValue(400)
            remaining = self.window._pending_apply.remainingTime()
            box.slider.setValue(410)
            self.assertLessEqual(self.window._pending_apply.remainingTime(), remaining)

    def test_a_live_knob_change_does_not_rebuild_the_form_under_the_mouse(self):
        from audiorouter.engine import Action, ApplyReport

        self.window.select_channel(self.engine.config.channels[0].slug)
        box = self.window.effects_panel.form._boxes["frequency"]
        box.spin.setValue(300)
        report = ApplyReport(actions=[Action("tune", "speakers")])
        with mock.patch.object(Engine, "apply", return_value=report):
            self.window.apply_now()
            self.settle()
        self.window.refresh()  # a graph event, as the live change itself causes
        self.assertIs(self.window.effects_panel.form._boxes["frequency"], box)
        self.assertEqual(Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 300)

    def test_reset_puts_the_settings_back_to_defaults(self):
        self.window.select_channel(self.engine.config.channels[0].slug)
        panel = self.window.effects_panel
        panel.reset_button.click()
        self.assertEqual(panel.form._boxes["frequency"].value(), 100.0)
        self.assertEqual(Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 100.0)

    def test_deleting_a_channel_asks_first(self):
        from PyQt6.QtWidgets import QMessageBox

        self.window.select_channel(self.engine.config.channels[0].slug)
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.No), \
             mock.patch.object(Engine, "delete_channel") as delete:
            self.window._remove_channel()
        delete.assert_not_called()


class ApplyWorkerTest(GuiTestCase):
    """Applying runs off the GUI thread; the window stays usable meanwhile."""

    def blocking_apply(self, report=None):
        """An apply that waits for `release`, recording what each run saw."""
        import threading

        self.release = threading.Event()
        self.entered = threading.Event()
        self.runs = []

        def apply(engine, *args, **kwargs):
            self.runs.append({
                "thread": threading.current_thread(),
                "frequency": engine.config.channel("speakers").effects[0].params["frequency"],
                "config": engine.config,
            })
            self.entered.set()
            self.assertTrue(self.release.wait(5), "apply was never released")
            return report or ApplyReport()

        # A plain function on the class, so each run receives its own engine.
        patcher = mock.patch.object(Engine, "apply", new=apply)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.release.set)  # never leave a worker hanging

    def test_apply_runs_on_another_thread_and_returns_at_once(self):
        import threading

        self.blocking_apply()
        self.window.apply_now()  # would hang here if it ran on this thread
        self.assertTrue(self.entered.wait(2))
        self.assertTrue(self.window.applier.busy)
        self.assertIsNot(self.runs[0]["thread"], threading.main_thread())
        self.release.set()
        self.settle()
        self.assertFalse(self.window.applier.busy)

    def test_the_window_can_be_edited_while_an_apply_runs(self):
        self.blocking_apply()
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(222)
        self.window.refresh()
        self.assertEqual(Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 222)
        self.release.set()
        self.settle()

    def test_the_worker_applies_a_copy_that_edits_cannot_change_under_it(self):
        self.blocking_apply()
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(333)
        running = self.runs[0]["config"]
        self.assertIsNot(running, self.engine.config)
        self.assertEqual(running.channel("speakers").effects[0].params["frequency"], 90)
        self.release.set()
        self.settle()

    def test_edits_during_an_apply_become_exactly_one_more_with_the_newest_settings(self):
        self.blocking_apply()
        self.window.select_channel(self.engine.config.channels[0].slug)
        box = self.window.effects_panel.form._boxes["frequency"]
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        for value in (150, 160, 170):
            box.spin.setValue(value)
            self.window.apply_now()
        self.release.set()
        self.settle()
        self.assertEqual([run["frequency"] for run in self.runs], [90, 170])

    def test_a_restart_shows_a_busy_cursor_until_it_is_over(self):
        from PyQt6.QtGui import QGuiApplication

        self.blocking_apply()
        self.window._config_edited()
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        self.assertIsNotNone(QGuiApplication.overrideCursor())
        self.assertEqual(self.window.status_label.text(), "Updating...")
        self.window.refresh()  # a graph event mid-restart must not clear it
        self.assertEqual(self.window.status_label.text(), "Updating...")
        self.release.set()
        self.settle()
        self.assertIsNone(QGuiApplication.overrideCursor())
        self.assertNotEqual(self.window.status_label.text(), "Updating...")

    def test_a_failure_on_the_worker_is_shown_in_the_window(self):
        from audiorouter.engine import EngineError

        with mock.patch.object(Engine, "apply", side_effect=EngineError("pipewire is gone")), \
             mock.patch("audiorouter.gui.main.QMessageBox.warning") as warning:
            self.window.apply_now()
            self.settle()
        warning.assert_called_once()
        self.assertIn("pipewire is gone", warning.call_args.args[2])
        self.assertFalse(self.window.applier.busy)

    def test_an_unexpected_error_is_shown_too_and_does_not_wedge_the_worker(self):
        with mock.patch.object(Engine, "apply", side_effect=KeyError("oops")), \
             mock.patch("audiorouter.gui.main.QMessageBox.warning") as warning:
            self.window.apply_now()
            self.settle()
        self.assertIn("KeyError", warning.call_args.args[2])
        with mock.patch.object(Engine, "apply", return_value=ApplyReport()) as apply:
            self.window.apply_now()
            self.settle()
        apply.assert_called_once()

    def test_closing_finishes_the_running_apply_and_the_queued_edit(self):
        self.blocking_apply()
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(444)
        self.assertTrue(self.window._pending_apply.isActive())
        self.release.set()
        self.window.close()
        self.assertEqual([run["frequency"] for run in self.runs], [90, 444])
        self.assertFalse(self.window.applier.busy)


class MeterTest(GuiTestCase):
    """The In/Out meters follow the selected channel and run only while shown."""

    def setUp(self):
        super().setUp()
        FakeReader.started = []
        pid = mock.patch.object(Channel, "pid", return_value=4242)
        pid.start()
        self.addCleanup(pid.stop)
        self.meters = self.window.meters

    def running(self):
        return sorted(r.tap.label for r in FakeReader.started if r.running)

    def select(self, row):
        self.window.select_channel(self.engine.config.channels[row].slug)

    def test_nothing_is_measured_until_the_window_is_shown(self):
        self.select(0)
        self.assertEqual(self.running(), [])
        self.meters.set_active(True)
        self.assertEqual(self.running(), ["In", "Out"])
        self.assertEqual(
            {r.tap.label: r.tap.args for r in FakeReader.started},
            {"In": ("--device=ar_speakers.monitor",), "Out": ("--monitor-stream=610",)},
        )

    def test_hiding_the_window_stops_every_tap(self):
        self.select(0)
        self.meters.set_active(True)
        from PyQt6.QtGui import QHideEvent

        self.window.hideEvent(QHideEvent())  # also what minimising sends
        self.assertEqual(self.running(), [])

    def test_choosing_another_channel_stops_the_first_channels_taps(self):
        self.meters.set_active(True)
        self.select(0)
        first = [r for r in FakeReader.started if r.running]
        self.select(1)  # "phones" is not in the graph, so nothing to measure
        self.assertTrue(first and not any(r.running for r in first))
        self.assertEqual(self.running(), [])
        self.assertIn("Not running", self.meters.hint.text())

    def test_a_restart_reopens_the_taps_and_a_refresh_without_one_does_not(self):
        self.meters.set_active(True)
        self.select(0)
        self.window.refresh()
        self.assertEqual(len(FakeReader.started), 2)
        with mock.patch.object(Channel, "pid", return_value=5151):
            self.window.refresh()
        self.assertEqual(len(FakeReader.started), 4)
        self.assertEqual(self.running(), ["In", "Out"])

    def test_readings_move_the_bars_and_a_stopped_taps_late_readings_are_ignored(self):
        from PyQt6.QtCore import QCoreApplication

        from audiorouter.meter import Levels

        self.meters.set_active(True)
        self.select(0)
        reader = next(r for r in FakeReader.started if r.tap.label == "Out")
        reader.on_levels(Levels(0.5, 0.25, 0.125, 0.03125))
        QCoreApplication.sendPostedEvents(None, 0)
        left, right = self.meters.bars["Out"]
        self.assertAlmostEqual(left.state.hold_db, -6.02, places=1)
        self.assertAlmostEqual(right.state.hold_db, -12.04, places=1)
        self.meters.set_active(False)
        reader.on_levels(Levels(1.0, 1.0, 0.5, 0.5))
        QCoreApplication.sendPostedEvents(None, 0)
        self.assertLess(left.state.hold_db, -50)

    def effect_taps_on(self):
        for target in (mock.patch("audiorouter.meter.host_has_taps", return_value=True),
                       mock.patch("audiorouter.gui.meters.taps_available", return_value=True)):
            target.start()
            self.addCleanup(target.stop)

    def files(self):
        return {r.tap.label: Path(r.tap.file).name for r in FakeReader.started if r.running}

    def test_the_highlighted_effect_is_measured_between_its_taps(self):
        self.effect_taps_on()
        self.meters.set_active(True)
        self.select(0)  # Speakers: one high-pass, highlighted
        self.assertEqual(self.files(), {"In": "4242.0", "Out": "4242.1"})
        self.assertEqual(self.meters.title(), "Levels - High-pass")
        self.assertEqual(self.meters.titles["Out"].toolTip(), "What High-pass puts out")

    def test_switching_following_off_measures_the_whole_channel_and_is_remembered(self):
        self.effect_taps_on()
        self.meters.set_active(True)
        self.select(0)
        self.meters.follow_effect.setChecked(False)
        self.assertEqual(self.files(), {"In": "", "Out": ""})  # parec taps again
        self.assertEqual(self.meters.title(), "Levels - whole channel")
        self.assertEqual(self.window.settings.value("meters_follow_effect"), False)

    def test_a_host_without_taps_falls_back_to_the_whole_channel_and_says_why(self):
        with mock.patch("audiorouter.gui.meters.taps_available", return_value=True):
            self.meters.set_active(True)
            self.select(0)
        self.assertEqual(self.running(), ["In", "Out"])
        self.assertEqual(self.files(), {"In": "", "Out": ""})
        self.assertIn("next restarts", self.meters.hint.text())

    def test_a_channel_with_no_effects_offers_no_following(self):
        self.effect_taps_on()
        self.select(1)
        self.assertTrue(self.meters.follow_effect.isHidden())
        self.assertEqual(self.meters.title(), "Levels")

    def test_without_the_level_tap_there_is_nothing_to_choose_and_no_nagging(self):
        self.meters.set_active(True)
        self.select(0)
        self.assertTrue(self.meters.follow_effect.isHidden())
        self.assertEqual(self.meters.hint.text(), "")

    def test_a_tap_that_ends_by_itself_is_not_retried_at_once(self):
        from PyQt6.QtCore import QCoreApplication

        self.meters.set_active(True)
        self.select(0)
        tap_in = next(r for r in FakeReader.started if r.tap.label == "In")
        tap_in.on_ended()
        QCoreApplication.sendPostedEvents(None, 0)
        self.assertEqual(self.running(), ["Out"])
        self.window.refresh()  # a graph event straight after: still waiting
        self.assertEqual(self.running(), ["Out"])

    def test_the_first_retry_is_quick_and_repeated_failures_back_off(self):
        from PyQt6.QtCore import QCoreApplication

        from audiorouter.gui import meters

        self.meters.set_active(True)
        self.select(0)
        next(r for r in FakeReader.started if r.tap.label == "In").on_ended()
        QCoreApplication.sendPostedEvents(None, 0)
        self.assertLessEqual(self.meters._retry.interval(), int(meters.FIRST_RETRY_S * 1000) + 50)
        self.meters._retry.stop()  # as it is once it has fired
        with mock.patch.object(meters, "FIRST_RETRY_S", 0.0):
            self.meters._retry_now()
        again = [r for r in FakeReader.started if r.tap.label == "In" and r.running][-1]
        again.on_ended()  # ended again with no reading in between: a mic that is gone
        QCoreApplication.sendPostedEvents(None, 0)
        self.assertGreaterEqual(self.meters._retry.interval(), int(meters.RETRY_S * 1000))

    def test_a_tap_that_ended_comes_back_by_itself_with_no_graph_event(self):
        # Measured live: after a restart settled there was no further event,
        # and the Out meter stayed dark for good.
        from PyQt6.QtCore import QCoreApplication

        from audiorouter.gui import meters

        self.meters.set_active(True)
        self.select(0)
        tap_out = next(r for r in FakeReader.started if r.tap.label == "Out")
        tap_out.on_ended()
        QCoreApplication.sendPostedEvents(None, 0)
        self.assertTrue(self.meters._retry.isActive())
        self.assertEqual(self.running(), ["In"])
        self.meters._retry.stop()
        with mock.patch.object(meters, "FIRST_RETRY_S", 0.0):
            self.meters._retry_now()  # what the timer does when it fires
        self.assertEqual(self.running(), ["In", "Out"])

    def test_hiding_cancels_a_pending_retry(self):
        from PyQt6.QtCore import QCoreApplication

        self.meters.set_active(True)
        self.select(0)
        next(r for r in FakeReader.started if r.tap.label == "Out").on_ended()
        QCoreApplication.sendPostedEvents(None, 0)
        self.meters.set_active(False)
        self.assertFalse(self.meters._retry.isActive())

    def test_an_input_channel_says_that_measuring_opens_the_microphone(self):
        self.engine.create_channel("mic", "Mic", "", kind="input")
        graph = live_graph()
        # A running mic channel's own node is its capture.
        graph.apply([fakes.node(70, "ar_mic_in", "Stream/Input/Audio", serial=700, **{"audiorouter.channel": "mic"})])
        self.meters.set_active(True)
        self.meters.follow(self.engine.channel("mic"), graph)
        self.assertEqual(
            {r.tap.label: r.tap.args for r in FakeReader.started if r.running},
            {"In": ("--device=@DEFAULT_SOURCE@",), "Out": ("--device=ar_mic",)},
        )
        self.assertIn("microphone", self.meters.hint.text())

    def test_closing_the_window_stops_the_meters(self):
        self.meters.set_active(True)
        self.select(0)
        self.window.close()
        self.assertEqual(self.running(), [])


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class BallisticsTest(unittest.TestCase):
    def setUp(self):
        from audiorouter.gui import meters

        self.m = meters
        self.bar = meters.Ballistics()

    def feed_tone(self, peak, seconds, start=0.0):
        steps = int(seconds / self.m.BLOCK_S)
        for i in range(steps):
            self.bar.feed(peak, peak * peak / 2, start + i * self.m.BLOCK_S)
        return start + steps * self.m.BLOCK_S

    def test_a_steady_tone_settles_with_average_and_peak_together(self):
        self.feed_tone(0.5, 2.0)
        self.assertAlmostEqual(self.bar.average_db, -6.02, places=1)
        self.assertAlmostEqual(self.bar.peak_db, -6.02, places=1)

    def test_the_average_takes_a_moment_like_a_needle(self):
        self.feed_tone(0.5, self.m.AVERAGE_TAU_S)
        # One time constant: 63% of the power, about 4.3 dB short.
        self.assertAlmostEqual(self.bar.average_db, -6.02 + 10 * math.log10(1 - math.exp(-1)), delta=0.3)

    def test_the_peak_falls_back_and_the_hold_waits_first(self):
        end = self.feed_tone(0.5, 0.2)
        self.bar.tick(end + 1.0)
        self.assertAlmostEqual(self.bar.peak_db, -6.02 - self.m.PEAK_FALL_DB_PER_S, delta=0.6)
        self.assertAlmostEqual(self.bar.hold_db, -6.02, places=1)
        self.bar.tick(end + self.m.HOLD_S + 0.5)
        self.assertLess(self.bar.hold_db, -6.02 - 5)

    def test_a_full_scale_peak_lights_the_clip_marker_for_a_while(self):
        self.bar.feed(1.0, 0.5, 10.0)
        self.assertTrue(self.bar.clipped(10.0 + self.m.CLIP_SHOW_S - 0.1))
        self.assertFalse(self.bar.clipped(10.0 + self.m.CLIP_SHOW_S + 0.1))
        self.bar.silence()
        self.assertFalse(self.bar.clipped(10.0))


class StreamsTest(GuiTestCase):
    def test_a_recording_app_chooses_what_it_hears(self):
        panel = self.window.streams_panel
        status = {
            "channels": [],
            "sources": [{"slug": "teams", "name": "Teams", "input": False},
                        {"slug": "voice", "name": "Voice", "input": True}],
            "streams": [{"id": 70, "app": "Audacity", "title": "ALSA Capture", "recording": True,
                         "sink": "Built-in Mic", "channel": None}],
        }
        panel.refresh(status)
        self.assertEqual(panel.table.item(0, 1).text(), "Recording")
        combo = panel.table.cellWidget(0, 2)
        self.assertEqual([combo.itemText(i) for i in range(combo.count())],
                         ["Built-in Mic", "Teams", "Voice (mic)"])
        panel.table.selectRow(0)
        self.assertEqual(panel.remember.text(), "Always record from here")
        with mock.patch.object(Engine, "send") as send:
            combo.activated.emit(1)
        send.assert_called_once_with(70, "teams", remember_new=True)
    def test_playing_apps_are_listed_with_a_destination(self):
        table = self.window.streams_panel.table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 0).text(), "firefox")
        combo = table.cellWidget(0, 2)
        self.assertEqual([combo.itemText(i) for i in range(combo.count())],
                         ["Not routed", "Speakers (stopped)", "Headphones (stopped)"])

    def test_choosing_a_channel_moves_that_stream(self):
        with mock.patch.object(Engine, "send") as send:
            combo = self.window.streams_panel.table.cellWidget(0, 2)
            combo.setCurrentIndex(1)
            combo.activated.emit(1)
        send.assert_called_once_with(60, "speakers", remember_new=True)

    def test_a_hand_placed_stream_is_not_dragged_back_by_the_rules(self):
        self.window.auto = mock.Mock()
        with mock.patch.object(Engine, "send"):
            self.window._send_stream(60, "speakers")
        self.window.auto.remember.assert_called_once_with(60)

    def test_remembering_an_app_writes_a_rule_for_it(self):
        self.window._remember_stream(60, "speakers")
        rules = Config.load(self.engine.path).rules.rules
        self.assertEqual([(r.field, r.pattern, r.channel) for r in rules],
                         [("app", "firefox", "speakers")])
        self.assertEqual(self.window.rules_list.count(), 1)

    def test_remembering_again_changes_the_channel_instead_of_adding_a_rule(self):
        self.window._remember_stream(60, "speakers")
        self.window._remember_stream(60, "phones")
        rules = Config.load(self.engine.path).rules.rules
        self.assertEqual([(r.pattern, r.channel) for r in rules], [("firefox", "phones")])

    def test_forgetting_removes_the_rule(self):
        self.window._remember_stream(60, "speakers")
        self.window.rules_list.setCurrentRow(0)
        self.window._forget_rule()
        self.assertEqual(Config.load(self.engine.path).rules.rules, [])


class BackgroundServiceTest(GuiTestCase):
    def test_switching_it_on_hands_routing_to_the_service(self):
        own = mock.Mock()
        self.window.auto = own
        with mock.patch("audiorouter.install.enable_login_service") as enable:
            self.window.background.setChecked(True)
        enable.assert_called_once()
        own.stop.assert_called_once()
        self.assertIsNone(self.window.auto)

    def test_switching_it_off_takes_routing_back(self):
        with mock.patch("audiorouter.install.enable_login_service"):
            self.window.background.setChecked(True)
        with mock.patch("audiorouter.install.disable_login_service") as disable, \
             mock.patch("audiorouter.gui.main.MainWindow._start_auto_router") as start:
            self.window.background.setChecked(False)
        disable.assert_called_once()
        start.assert_called_once()

    def test_a_failure_puts_the_box_back_and_says_why(self):
        from PyQt6.QtWidgets import QMessageBox

        from audiorouter.install import InstallError

        with mock.patch("audiorouter.install.enable_login_service",
                        side_effect=InstallError("no systemd")), \
             mock.patch.object(QMessageBox, "warning") as warning:
            self.window.background.setChecked(True)
        self.assertFalse(self.window.background.isChecked())
        warning.assert_called_once()


class BypassTest(GuiTestCase):
    def test_bypass_saves_shows_a_banner_and_applies(self):
        with mock.patch.object(Engine, "route") as route:
            self.window.bypass.setChecked(True)
            self.settle()
        self.assertTrue(Config.load(self.engine.path).bypass)
        self.assertFalse(self.window.bypass_banner.isHidden())
        route.assert_not_called()

    def test_switching_bypass_off_routes_what_is_playing(self):
        self.window.bypass.setChecked(True)
        self.settle()
        with mock.patch.object(Engine, "route", return_value=[]) as route:
            self.window.bypass.setChecked(False)
            self.settle()
        self.assertFalse(Config.load(self.engine.path).bypass)
        self.assertTrue(self.window.bypass_banner.isHidden())
        route.assert_called_once()

    def test_the_window_opens_bypassed_if_it_was_left_bypassed(self):
        from audiorouter.gui.main import MainWindow

        self.engine.set_bypass(True)
        window = MainWindow(self.engine)
        self.addCleanup(window.close)
        self.assertTrue(window.bypass.isChecked())
        self.assertFalse(window.bypass_banner.isHidden())


class OwnRouterTest(GuiTestCase):
    # setUp patches _start_auto_router out, so these call the real one.
    def real_start(self):
        from audiorouter.gui import main

        REAL_START_AUTO_ROUTER(self.window)
        return main

    def test_the_window_does_not_route_while_a_daemon_already_is(self):
        with mock.patch("audiorouter.gui.main.daemon_pid", return_value=4321), \
             mock.patch("audiorouter.gui.main.AutoRouter") as router:
            self.real_start()
        router.assert_not_called()
        self.assertIsNone(self.window.auto)

    def test_the_window_routes_by_itself_when_no_daemon_is_running(self):
        with mock.patch("audiorouter.gui.main.daemon_pid", return_value=None), \
             mock.patch("audiorouter.gui.main.AutoRouter") as router:
            self.real_start()
        router.return_value.start.assert_called_once()
        self.window.auto = None  # the mock has nothing to stop


class NamingTest(unittest.TestCase):
    def test_a_human_name_becomes_a_graph_safe_id(self):
        from audiorouter.gui.main import slug_for

        self.assertEqual(slug_for("Desk Speakers", set()), "desk_speakers")
        self.assertEqual(slug_for("Desk Speakers", {"desk_speakers"}), "desk_speakers_2")
        self.assertEqual(slug_for("!!!", set()), "channel")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class PluginGuiTest(GuiTestCase):
    """The browser and the generated controls, against a hand-built catalogue."""

    def setUp(self):
        from .test_plugin_effects import fake_catalogue

        for target in (
            mock.patch("audiorouter.lv2.catalogue", side_effect=fake_catalogue),
            mock.patch("audiorouter.plugins.available_loaders", return_value=frozenset({"builtin", "lv2"})),
            mock.patch("audiorouter.plugins.lv2_installed", return_value=True),
        ):
            target.start()
            self.addCleanup(target.stop)
        super().setUp()

    def browser(self):
        from audiorouter.gui.effects_panel import EffectBrowser

        browser = EffectBrowser(self.window)
        self.addCleanup(browser.close)
        return browser

    def names(self, browser):
        return [item.text(0) for _, item in browser._items() if not item.isHidden()]

    def test_effects_are_grouped_by_what_they_do_with_the_maker_alongside(self):
        from audiorouter.lv2 import CATEGORIES

        browser = self.browser()
        groups = [browser.tree.topLevelItem(i).text(0).split("  (")[0]
                  for i in range(browser.tree.topLevelItemCount())]
        self.assertEqual(groups, list(CATEGORIES))
        dynamics = browser.tree.topLevelItem(groups.index("Dynamics"))
        rows = [(dynamics.child(i).text(0), dynamics.child(i).text(1)) for i in range(dynamics.childCount())]
        self.assertEqual(rows[0], ("Compressor", "Built in"))  # built-ins first
        self.assertIn(("Example Compressor", "example.org"), rows)

    def test_search_matches_the_maker_and_the_kind_of_effect(self):
        browser = self.browser()
        browser.search.setText("example.org")
        self.assertIn("Example Compressor", self.names(browser))
        browser.search.setText("dynamics")
        self.assertIn("Limiter", self.names(browser))

    def test_a_word_in_an_effects_own_name_beats_the_name_of_its_group(self):
        # Regression: "noise" showed every effect in "Noise & gates".
        browser = self.browser()
        browser.search.setText("trim")  # only Volume trim, not all of Utility
        self.assertEqual(self.names(browser), ["Volume trim"])

    def test_search_filters_and_selects_the_first_usable_match(self):
        browser = self.browser()
        browser.search.setText("compressor")
        visible = self.names(browser)
        self.assertIn("Example Compressor", visible)
        self.assertNotIn("Volume trim", visible)
        self.assertIsNotNone(browser.tree.currentItem())
        self.assertTrue(browser.buttons.button(browser.buttons.StandardButton.Ok).isEnabled())

    def test_unusable_plugins_are_hidden_unless_asked_and_cannot_be_added(self):
        browser = self.browser()
        self.assertNotIn("Synth", self.names(browser))
        browser.show_unusable.setChecked(True)
        (item,) = [i for _, i in browser._items() if i.text(0) == "Synth"]
        browser.tree.setCurrentItem(item)
        self.assertFalse(browser.buttons.button(browser.buttons.StandardButton.Ok).isEnabled())
        self.assertIn("makes sound", browser.detail.text())
        browser._accept_item(item)
        self.assertIsNone(browser.choice)

    def test_choosing_a_plugin_adds_it_with_its_controls(self):
        from PyQt6.QtWidgets import QCheckBox, QComboBox

        from .test_plugin_effects import STEREO

        self.window.select_channel(self.engine.config.channels[1].slug)
        panel = self.window.effects_panel
        browser = self.browser()
        browser.search.setText("example compressor")
        browser._accept_item(browser.tree.currentItem())
        self.assertEqual(browser.choice, ("lv2", STEREO))
        panel.add_effect(*browser.choice)
        self.assertEqual(set(panel.form._boxes), {"al", "cm", "sw"})  # igv is UI-only
        self.assertIsInstance(panel.form._boxes["cm"].combo, QComboBox)
        self.assertIsInstance(panel.form._boxes["sw"].check, QCheckBox)
        saved = Config.load(self.engine.path).channel("phones").effects[0]
        self.assertEqual((saved.kind, saved.plugin), ("lv2", STEREO))

    def test_a_linear_gain_is_shown_and_edited_in_db(self):
        from .test_plugin_effects import STEREO

        self.window.select_channel(self.engine.config.channels[1].slug)
        panel = self.window.effects_panel
        panel.add_effect("lv2", STEREO)
        box = panel.form._boxes["al"]
        self.assertAlmostEqual(box.spin.value(), -12.0, places=1)  # default 0.25
        self.assertEqual(box.spin.suffix(), " dB")
        box.spin.setValue(-6.0)
        effect = self.engine.config.channel("phones").effects[0]
        self.assertAlmostEqual(effect.params["al"], 0.501, places=3)

    def test_a_logarithmic_slider_puts_the_middle_of_its_travel_at_the_geometric_middle(self):
        self.window.select_channel(self.engine.config.channels[0].slug)
        box = self.window.effects_panel.form._boxes["frequency"]  # 20 Hz .. 20 kHz, log
        box.slider.setValue(500)
        self.assertAlmostEqual(box.value(), 632.5, delta=2)

    def test_a_plugin_that_is_no_longer_installed_is_shown_as_unavailable(self):
        self.window.select_channel(self.engine.config.channels[1].slug)
        panel = self.window.effects_panel
        self.engine.config.channel("phones").effects.append(
            Effect("lv2", plugin="http://example.org/uninstalled"))
        panel.refresh()
        self.assertIn("unavailable", panel.list.item(0).text())
        self.assertIn("not installed", panel.summary.text())


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class InputAndCableGuiTest(GuiTestCase):
    def setUp(self):
        super().setUp()
        from audiorouter.channels import INPUT

        self.engine.config.add_channel(Channel("mic", "Desk mic", "", kind=INPUT))
        self.window.refresh()
        self.panel = self.window.channel_panel

    def select(self, slug):
        self.window.select_channel(slug)

    def test_a_mic_channel_reads_in_level_out_like_any_other(self):
        self.select("mic")
        # IN: a microphone instead of apps.
        self.assertTrue(self.panel.form.isRowVisible(self.panel.source))
        self.assertFalse(self.panel.form.isRowVisible(self.panel.source_text))
        self.assertEqual(self.panel.source.itemText(0), "Default input")
        # OUT: plays through (its listen-through), and recording apps always see it.
        self.assertEqual(self.panel.form.labelForField(self.panel.device).text(), "Plays through")
        self.assertEqual(self.panel.device.itemText(0), "Nowhere (only apps record it)")
        self.assertTrue(self.panel.form.isRowVisible(self.panel.recordable))
        self.assertTrue(self.panel.recordable.isChecked())
        self.assertFalse(self.panel.recordable.isEnabled())
        self.assertIn('"Desk mic"', self.panel.recordable.text())
        self.assertEqual(self.panel.kind.text(), "From a mic")

    def test_one_list_in_the_mixers_order(self):
        channels = self.window.channel_list
        labels = [channels.item(i).text() for i in range(channels.count())]
        # Mic channels first, as on the desk; the companion never appears.
        self.assertEqual(labels, ["Desk mic  -  From a mic  (not running)",
                                  "Speakers  -  From apps  (not running)",
                                  "Headphones  -  From apps  (not running)"])

    def test_choosing_a_mic_changes_the_source_not_the_listen_through(self):
        self.select("mic")
        self.panel.source.addItem("USB mic", "alsa_input.usb")
        index = self.panel.source.findData("alsa_input.usb")
        self.panel.source.setCurrentIndex(index)
        self.panel.source.activated.emit(index)
        saved = Config.load(self.engine.path).channel("mic")
        self.assertEqual((saved.device, saved.listen), ("alsa_input.usb", ""))

    def test_echo_cancellation_is_offered_for_inputs_only_and_saved(self):
        self.select("phones")
        self.assertFalse(self.panel.form.isRowVisible(self.panel.echo_cancel))
        self.select("mic")
        self.assertTrue(self.panel.form.isRowVisible(self.panel.echo_cancel))
        self.assertFalse(self.panel.echo_cancel.isChecked())
        self.panel.echo_cancel.setChecked(True)
        self.assertTrue(Config.load(self.engine.path).channel("mic").echo_cancel)
        self.select("phones")
        self.select("mic")
        self.assertTrue(self.panel.echo_cancel.isChecked())

    def test_listen_offers_output_channels_only_and_warns_about_feedback(self):
        self.select("mic")
        names = [self.panel.device.itemText(i) for i in range(self.panel.device.count())]
        self.assertEqual(names, ["Nowhere (only apps record it)", "Speakers", "Headphones"])
        self.panel.device.setCurrentIndex(1)
        self.panel.device.activated.emit(1)
        self.assertEqual(Config.load(self.engine.path).channel("mic").listen, "speakers")
        self.assertIn("headphones", self.panel.hint.text())

    def test_an_output_can_become_a_cable_and_nowhere_keeps_it_one(self):
        from audiorouter.channels import NOWHERE

        self.select("phones")
        self.assertEqual(self.panel.form.labelForField(self.panel.device).text(), "Plays through")
        self.assertFalse(self.panel.form.isRowVisible(self.panel.source))
        self.panel.recordable.setChecked(True)
        self.assertTrue(Config.load(self.engine.path).channel("phones").recordable)
        index = self.panel.device.findData(NOWHERE)
        self.panel.device.setCurrentIndex(index)
        self.panel.device.activated.emit(index)
        self.assertFalse(self.panel.recordable.isEnabled())
        self.assertIn('"Headphones (recording)"', self.panel.recordable.text())

    def test_apps_cannot_be_sent_into_an_input(self):
        streams = self.window.streams_panel
        combo = streams.table.cellWidget(0, 2)
        options = [combo.itemData(i) for i in range(combo.count())]
        self.assertNotIn("mic", options)
        self.assertIn("speakers", options)

    def test_clicking_in_the_list_selects_any_kind_of_channel(self):
        self.select("mic")
        self.assertIs(self.window.channel_list.currentItem(), self.window.channel_list.item(0))
        self.assertTrue(self.window.remove_button.isEnabled())
        self.window.channel_list.setCurrentRow(2)
        self.assertEqual(self.window.selected_channel.slug, "phones")
        self.assertEqual(self.panel.name.text(), "Headphones")

    def test_the_selection_survives_a_refresh(self):
        self.select("mic")
        self.window.refresh()
        self.assertEqual(self.window.selected_channel.slug, "mic")

    def test_new_input_creates_an_input_on_the_default_mic_and_selects_it(self):
        with mock.patch("audiorouter.gui.main.QInputDialog.getText", return_value=("Streaming mic", True)), \
             mock.patch.object(Engine, "apply"):
            self.window.add_button.menu().actions()[0].trigger()
        created = self.engine.config.channels[-2]  # its hidden companion follows it
        self.assertEqual((created.name, created.kind, created.device), ("Streaming mic", "input", ""))
        self.assertEqual(self.window.selected_channel.slug, created.slug)

    def test_new_output_needs_no_kind_question(self):
        with mock.patch("audiorouter.gui.main.QInputDialog.getItem") as ask_kind, \
             mock.patch("audiorouter.gui.main.QInputDialog.getText", return_value=("Game", True)), \
             mock.patch.object(Engine, "apply"):
            self.window.add_button.menu().actions()[1].trigger()
        ask_kind.assert_not_called()
        self.assertEqual(self.engine.config.channels[-1].kind, "output")


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class ChannelSourcesTest(GuiTestCase):
    """The panel's IN section: what feeds the channel, and remembering apps."""

    def setUp(self):
        super().setUp()
        from audiorouter.channels import INPUT

        self.engine.config.add_channel(Channel("mic", "Desk mic", "", kind=INPUT))
        self.engine.add_rule("app", "spotify", "speakers")
        self.window.refresh()
        self.panel = self.window.channel_panel

    def rows(self):
        return [a.text() for a in self.panel.forget_menu.actions()]

    def test_remembered_apps_are_listed_on_their_channel(self):
        self.window.select_channel("speakers")
        self.assertEqual(self.rows(), ["spotify"])
        self.assertEqual(self.panel.remembered.text(), "spotify")
        self.window.select_channel("phones")
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.panel.forget.isEnabled())

    def test_playing_apps_are_shown_on_the_channel_they_play_through(self):
        status = dict(self.window._status)
        status["streams"] = [{"id": 1, "app": "Zen", "channel": "speakers"}]
        self.window._show_sources(self.engine.channel("speakers"), status)
        self.assertEqual(self.panel.playing.text(), "Zen")
        self.window._show_sources(self.engine.channel("phones"), status)
        self.assertEqual(self.panel.playing.text(), "nothing playing")

    def test_add_app_moves_an_apps_rule_here_and_forget_removes_it(self):
        self.window.select_channel("phones")
        offered = [a.text() for a in self.panel.add_app_menu.actions() if a.text()]
        self.assertIn("spotify", offered)  # remembered elsewhere: can be moved here
        next(a for a in self.panel.add_app_menu.actions() if a.text() == "spotify").trigger()
        self.assertEqual(self.rows(), ["spotify"])
        rules = Config.load(self.engine.path).rules.rules
        self.assertEqual([(r.pattern, r.channel) for r in rules], [("spotify", "phones")])
        self.panel.forget_menu.actions()[0].trigger()
        self.assertEqual(Config.load(self.engine.path).rules.rules, [])
        self.assertEqual(self.rows(), [])

    def test_an_app_remembered_on_a_mic_channel_goes_into_its_mix(self):
        self.window.select_channel("mic")
        with mock.patch("audiorouter.gui.channel_panel.QInputDialog.getText", return_value=("obs", True)):
            self.panel.add_app_menu.actions()[-1].trigger()  # Another app...
        companion = self.engine.config.companion(self.engine.channel("mic"))
        rule = Config.load(self.engine.path).rules.rules[-1]
        self.assertEqual((rule.pattern, rule.channel), ("obs", companion.slug))
        self.assertEqual(self.rows(), ["obs"])


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class PlayingNowFoldTest(GuiTestCase):
    def test_folding_hides_the_table_counts_streams_and_is_remembered(self):
        streams = self.window.streams_panel
        self.assertTrue(streams.expanded)
        self.assertRegex(streams.toggle.text(), r"^Playing now \(\d+\)$")
        streams.toggle.click()
        self.assertFalse(streams.expanded)
        self.assertTrue(streams.body.isHidden())
        self.assertFalse(self.window.rules_list.isVisibleTo(self.window))  # folds along
        self.assertEqual(self.window.settings.value("streams_expanded"), False)

        from audiorouter.gui.main import MainWindow

        again = MainWindow(self.engine, settings=self.window.settings)
        self.addCleanup(again.close)
        self.assertFalse(again.streams_panel.expanded)

    def test_playing_now_is_resized_by_dragging_and_remembered(self):
        window = self.window
        window.resize(1200, 900)
        window.show()
        self.app.processEvents()
        splitter = window.streams_splitter
        splitter.setSizes([300, 500])
        splitter.splitterMoved.emit(300, 1)  # what a drag of the handle sends
        tall = splitter.sizes()[1]
        self.assertGreater(tall, 400)

        from audiorouter.gui.main import MainWindow

        again = MainWindow(self.engine, settings=window.settings)
        self.addCleanup(again.close)
        again.resize(1200, 900)
        again.show()
        self.app.processEvents()
        self.assertAlmostEqual(again.streams_splitter.sizes()[1], tall, delta=20)

    def test_folded_playing_now_is_only_its_heading(self):
        window = self.window
        window.resize(1200, 900)
        window.show()
        window.streams_panel.toggle.click()
        self.app.processEvents()
        self.assertLessEqual(window.streams_splitter.sizes()[1],
                             window.streams_panel.toggle.sizeHint().height())


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class PluginFoldersTest(GuiTestCase):
    def test_folders_chosen_in_the_dialog_are_saved_and_the_browser_reloads(self):
        from audiorouter.gui import effects_panel

        folder = Path(self.tmp.name) / "daw"
        folder.mkdir()

        def choose(dialog):
            dialog.add_folder(str(folder))
            return effects_panel.QDialog.DialogCode.Accepted

        with mock.patch.object(effects_panel.PluginFoldersDialog, "exec", choose):
            changed = self.window.effects_panel.edit_plugin_folders(self.window)
        self.addCleanup(__import__("audiorouter.plugins").plugins.set_extra_folders, [])
        self.assertTrue(changed)
        self.assertEqual(Config.load(self.engine.path).plugin_folders, [str(folder)])

        browser = effects_panel.EffectBrowser(self.window, edit_folders=lambda parent: True)
        self.addCleanup(browser.close)
        self.assertTrue(browser.folders_button.isVisibleTo(browser))
        with mock.patch.object(browser, "_populate") as populate:
            browser.folders_button.click()
        populate.assert_called_once()

    def test_cancelling_the_dialog_changes_nothing(self):
        from audiorouter.gui import effects_panel

        with mock.patch.object(effects_panel.PluginFoldersDialog, "exec",
                               lambda d: effects_panel.QDialog.DialogCode.Rejected):
            self.assertFalse(self.window.effects_panel.edit_plugin_folders(self.window))
        self.assertEqual(self.engine.config.plugin_folders, [])
