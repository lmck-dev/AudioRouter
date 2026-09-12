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
from audiorouter.engine import Engine
from audiorouter.pwgraph import Graph

from .test_engine import live_graph


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
            self.window.effects_panel.form._boxes["frequency"].setValue(120)
            self.window.effects_panel.form._boxes["frequency"].setValue(130)
        apply.assert_not_called()
        self.assertTrue(self.window._pending_apply.isActive())

    def test_the_scheduled_apply_eventually_runs(self):
        from audiorouter.engine import ApplyReport

        with mock.patch.object(Engine, "apply", return_value=ApplyReport()) as apply:
            self.window._config_edited()
            self.window.apply_now()
        apply.assert_called_once()

    def test_moving_a_knob_updates_the_line_in_the_chain(self):
        self.window.channel_list.setCurrentRow(0)
        panel = self.window.effects_panel
        panel.form._boxes["frequency"].setValue(250)
        self.assertIn("250", panel.list.item(0).text())

    def test_effect_edits_reach_the_saved_config(self):
        self.window.channel_list.setCurrentRow(0)
        self.window.effects_panel.form._boxes["frequency"].setValue(120)
        self.assertEqual(
            Config.load(self.engine.path).channel("speakers").effects[0].params["frequency"], 120
        )

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
        panel.picker.setCurrentIndex(panel.picker.findData("gain"))
        panel._add()
        self.assertEqual(panel.list.currentRow(), 0)
        self.assertIn("gain_db", panel.form._boxes)

    def test_changing_effects_does_not_leave_the_previous_knobs_behind(self):
        # Orphaned rows stayed painted over the new ones when they were only
        # scheduled for deletion.
        self.window.channel_list.setCurrentRow(0)
        panel = self.window.effects_panel
        panel.picker.setCurrentIndex(panel.picker.findData("gain"))
        panel._add()
        self.assertEqual(set(panel.form._boxes), {"gain_db"})
        self.assertEqual(panel.form._layout.rowCount(), 1)

    def test_the_add_menu_defaults_to_something_this_machine_can_run(self):
        from audiorouter.effects import spec_for

        panel = self.window.effects_panel
        with mock.patch("audiorouter.plugins.available_loaders",
                        return_value=frozenset({"builtin"})):
            panel._select_first_available()
            self.assertTrue(spec_for(panel.picker.currentData()).available)

    def test_deleting_a_channel_asks_first(self):
        from PyQt6.QtWidgets import QMessageBox

        self.window.channel_list.setCurrentRow(0)
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.No), \
             mock.patch.object(Engine, "delete_channel") as delete:
            self.window._remove_channel()
        delete.assert_not_called()


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
        send.assert_called_once_with(60, "speakers")

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

    def test_forgetting_removes_the_rule(self):
        self.window._remember_stream(60, "speakers")
        self.window.rules_list.setCurrentRow(0)
        self.window._forget_rule()
        self.assertEqual(Config.load(self.engine.path).rules.rules, [])


class NamingTest(unittest.TestCase):
    def test_a_human_name_becomes_a_graph_safe_id(self):
        from audiorouter.gui.main import slug_for

        self.assertEqual(slug_for("Desk Speakers", set()), "desk_speakers")
        self.assertEqual(slug_for("Desk Speakers", {"desk_speakers"}), "desk_speakers_2")
        self.assertEqual(slug_for("!!!", set()), "channel")


if __name__ == "__main__":
    unittest.main()
