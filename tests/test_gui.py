"""GUI tests, run headless.

These check wiring, not looks: that selecting a channel loads it, that an edit
schedules an apply rather than restarting audio on every keystroke, and that the
panels ask the engine for things instead of doing them.
"""

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

from .test_engine import live_graph

if QApplication is not None:
    from audiorouter.gui.main import MainWindow as _MainWindow

    REAL_START_AUTO_ROUTER = _MainWindow._start_auto_router


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
        labels = [self.window.channel_list.item(i).text()
                  for i in range(self.window.channel_list.count())]
        self.assertEqual(labels, ["Speakers  (not running)", "Headphones  (not running)"])

    def test_selecting_a_channel_loads_its_settings_and_effects(self):
        self.window.channel_list.setCurrentRow(0)
        self.assertEqual(self.window.channel_panel.name.text(), "Speakers")
        self.assertEqual(self.window.effects_panel.list.count(), 1)
        self.window.channel_list.setCurrentRow(1)
        self.assertEqual(self.window.channel_panel.name.text(), "Headphones")
        self.assertEqual(self.window.effects_panel.list.count(), 0)

    def test_an_unplugged_output_is_still_offered_so_it_is_not_silently_changed(self):
        self.window.channel_list.setCurrentRow(1)  # targets alsa_output.b, not in the graph
        self.assertIn("not connected", self.window.channel_panel.device.currentText())

    def test_a_virtual_target_that_exists_is_not_called_disconnected(self):
        # Only hardware appears in the device list, but a channel may point at
        # any sink; saying a working one is unplugged would be a lie.
        self.engine.channel("phones").device = "ar_speakers"
        self.window.refresh()
        self.window.channel_list.setCurrentRow(1)
        self.assertEqual(self.window.channel_panel.device.currentText(), "ar_speakers")

    def test_renaming_a_channel_saves_and_reaches_the_list(self):
        self.window.channel_list.setCurrentRow(0)
        self.window.channel_panel.name.setText("Desk")
        self.window.channel_panel.name.editingFinished.emit()
        self.assertEqual(Config.load(self.engine.path).channel("speakers").name, "Desk")
        self.assertIn("Desk", self.window.channel_list.item(0).text())

    def test_an_edit_schedules_one_apply_instead_of_restarting_at_once(self):
        # Every keystroke restarting the audio would be unusable.
        with mock.patch.object(Engine, "apply") as apply:
            self.window.channel_list.setCurrentRow(0)
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
        self.window.channel_list.setCurrentRow(0)
        panel = self.window.effects_panel
        panel.form._boxes["frequency"].spin.setValue(250)
        self.assertIn("250", panel.list.item(0).text())

    def test_effect_edits_reach_the_saved_config(self):
        self.window.channel_list.setCurrentRow(0)
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(120)
        self.assertEqual(
            Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 120
        )

    def test_switching_an_effect_off_is_a_quick_live_change(self):
        from PyQt6.QtCore import Qt

        from audiorouter.gui.main import TUNE_DELAY_MS

        self.window.channel_list.setCurrentRow(0)
        with mock.patch.object(Engine, "apply"):
            self.window.effects_panel.list.item(0).setCheckState(Qt.CheckState.Unchecked)
            self.assertEqual(self.window._pending_apply.interval(), TUNE_DELAY_MS)

    def test_switching_an_effect_off_keeps_it_in_the_chain(self):
        from PyQt6.QtCore import Qt

        self.window.channel_list.setCurrentRow(0)
        self.window.effects_panel.list.item(0).setCheckState(Qt.CheckState.Unchecked)
        saved = Config.load(self.engine.path).channel("speakers").effects
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0].enabled)

    def test_adding_an_effect_selects_it_so_its_knobs_are_visible(self):
        self.window.channel_list.setCurrentRow(1)
        panel = self.window.effects_panel
        panel.add_effect("gain")
        self.assertEqual(panel.list.currentRow(), 0)
        self.assertIn("gain_db", panel.form._boxes)

    def test_changing_effects_does_not_leave_the_previous_knobs_behind(self):
        # Orphaned rows stayed painted over the new ones when they were only
        # scheduled for deletion.
        self.window.channel_list.setCurrentRow(0)
        panel = self.window.effects_panel
        panel.add_effect("gain")
        self.assertEqual(set(panel.form._boxes), {"gain_db"})
        self.assertEqual(panel.form._layout.rowCount(), 1)

    def test_an_edit_that_reshapes_the_chain_waits_longer_than_a_knob(self):
        from audiorouter.gui.main import APPLY_DELAY_MS, TUNE_DELAY_MS

        self.window.channel_list.setCurrentRow(0)
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
        self.window.channel_list.setCurrentRow(0)
        box = self.window.effects_panel.form._boxes["frequency"]
        with mock.patch.object(Engine, "apply"):
            box.slider.setValue(400)
            remaining = self.window._pending_apply.remainingTime()
            box.slider.setValue(410)
            self.assertLessEqual(self.window._pending_apply.remainingTime(), remaining)

    def test_a_live_knob_change_does_not_rebuild_the_form_under_the_mouse(self):
        from audiorouter.engine import Action, ApplyReport

        self.window.channel_list.setCurrentRow(0)
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
        self.window.channel_list.setCurrentRow(0)
        panel = self.window.effects_panel
        panel.reset_button.click()
        self.assertEqual(panel.form._boxes["frequency"].value(), 100.0)
        self.assertEqual(Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 100.0)

    def test_deleting_a_channel_asks_first(self):
        from PyQt6.QtWidgets import QMessageBox

        self.window.channel_list.setCurrentRow(0)
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
        self.window.channel_list.setCurrentRow(0)
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(222)
        self.window.refresh()
        self.assertEqual(Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 222)
        self.release.set()
        self.settle()

    def test_the_worker_applies_a_copy_that_edits_cannot_change_under_it(self):
        self.blocking_apply()
        self.window.channel_list.setCurrentRow(0)
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
        self.window.channel_list.setCurrentRow(0)
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
        self.window.channel_list.setCurrentRow(0)
        self.window.apply_now()
        self.assertTrue(self.entered.wait(2))
        self.window.effects_panel.form._boxes["frequency"].spin.setValue(444)
        self.assertTrue(self.window._pending_apply.isActive())
        self.release.set()
        self.window.close()
        self.assertEqual([run["frequency"] for run in self.runs], [90, 444])
        self.assertFalse(self.window.applier.busy)


class StreamsTest(GuiTestCase):
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

        self.window.channel_list.setCurrentRow(1)
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

        self.window.channel_list.setCurrentRow(1)
        panel = self.window.effects_panel
        panel.add_effect("lv2", STEREO)
        box = panel.form._boxes["al"]
        self.assertAlmostEqual(box.spin.value(), -12.0, places=1)  # default 0.25
        self.assertEqual(box.spin.suffix(), " dB")
        box.spin.setValue(-6.0)
        effect = self.engine.config.channel("phones").effects[0]
        self.assertAlmostEqual(effect.params["al"], 0.501, places=3)

    def test_a_logarithmic_slider_puts_the_middle_of_its_travel_at_the_geometric_middle(self):
        self.window.channel_list.setCurrentRow(0)
        box = self.window.effects_panel.form._boxes["frequency"]  # 20 Hz .. 20 kHz, log
        box.slider.setValue(500)
        self.assertAlmostEqual(box.value(), 632.5, delta=2)

    def test_a_plugin_that_is_no_longer_installed_is_shown_as_unavailable(self):
        self.window.channel_list.setCurrentRow(1)
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
        self.window.channel_list.setCurrentRow(self.engine.config.channel_slugs.index(slug))

    def test_an_input_shows_records_from_and_listen_instead_of_the_cable_box(self):
        self.select("mic")
        self.assertEqual(self.panel.form.labelForField(self.panel.device).text(), "Records from")
        self.assertEqual(self.panel.device.itemText(0), "Default input")
        self.assertTrue(self.panel.form.isRowVisible(self.panel.listen))
        self.assertFalse(self.panel.form.isRowVisible(self.panel.recordable))
        self.assertIn('microphone called "Desk mic"', self.panel.hint.text())
        self.assertIn("(input)", self.window.channel_list.item(2).text())

    def test_listen_offers_output_channels_only_and_warns_about_feedback(self):
        self.select("mic")
        names = [self.panel.listen.itemText(i) for i in range(self.panel.listen.count())]
        self.assertEqual(names, ["Don't listen", "Speakers", "Headphones"])
        self.panel.listen.setCurrentIndex(1)
        self.panel.listen.activated.emit(1)
        self.assertEqual(Config.load(self.engine.path).channel("mic").listen, "speakers")
        self.assertIn("headphones", self.panel.hint.text())

    def test_an_output_can_become_a_cable_and_nowhere_keeps_it_one(self):
        from audiorouter.channels import NOWHERE

        self.select("phones")
        self.assertEqual(self.panel.form.labelForField(self.panel.device).text(), "Plays through")
        self.assertFalse(self.panel.form.isRowVisible(self.panel.listen))
        self.panel.recordable.setChecked(True)
        self.assertTrue(Config.load(self.engine.path).channel("phones").recordable)
        index = self.panel.device.findData(NOWHERE)
        self.panel.device.setCurrentIndex(index)
        self.panel.device.activated.emit(index)
        self.assertFalse(self.panel.recordable.isEnabled())
        self.assertIn("(recording)", self.panel.hint.text())

    def test_apps_cannot_be_sent_into_an_input(self):
        streams = self.window.streams_panel
        combo = streams.table.cellWidget(0, 2)
        options = [combo.itemData(i) for i in range(combo.count())]
        self.assertNotIn("mic", options)
        self.assertIn("speakers", options)

    def test_new_channel_asks_what_kind_and_creates_an_input_on_the_default_mic(self):
        from audiorouter.gui.main import NEW_INPUT

        with mock.patch("audiorouter.gui.main.QInputDialog.getItem", return_value=(NEW_INPUT, True)), \
             mock.patch("audiorouter.gui.main.QInputDialog.getText", return_value=("Streaming mic", True)), \
             mock.patch.object(Engine, "apply"):
            self.window._add_channel()
        created = self.engine.config.channels[-1]
        self.assertEqual((created.name, created.kind, created.device), ("Streaming mic", "input", ""))
