"""The mixer view: strips edit the same channels as the Channels view."""

import unittest
from unittest import mock

from audiorouter.channels import INPUT, Channel
from audiorouter.config import Config
from audiorouter.effects import Effect
from audiorouter.engine import Engine
from audiorouter.meter import Tap

try:
    from PyQt6.QtWidgets import QLabel
except ImportError:  # pragma: no cover
    QLabel = None

from .test_gui import QApplication, FakeDriver, FakeReader, GuiTestCase


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class MixerTest(GuiTestCase):
    def setUp(self):
        super().setUp()
        FakeReader.started = []
        self.engine.config.add_channel(Channel("mic", "Desk mic", "", kind=INPUT))
        self.window.refresh()
        self.mixer = self.window.mixer

    def strip(self, slug):
        return self.mixer.strips[slug]

    def test_it_is_the_default_view_with_inputs_before_outputs(self):
        self.assertIs(self.window.views.currentWidget(), self.mixer)
        self.assertEqual(list(self.mixer.strips), ["mic", "speakers", "phones"])
        self.assertEqual(self.strip("speakers").name.text(), "Speakers")
        self.assertEqual(self.strip("mic").kind.text(), "FROM A MIC")
        self.assertIsNotNone(self.strip("mic").source)
        self.assertIsNone(self.strip("speakers").source)

    def test_a_refresh_does_not_rebuild_the_strips(self):
        # Rebuilding would drop a fader out from under the mouse mid-drag.
        before = self.strip("speakers")
        self.window.refresh()
        self.assertIs(self.strip("speakers"), before)

    def test_a_new_effect_rebuilds_that_desk(self):
        before = self.strip("phones")
        from audiorouter.effects import Effect

        self.engine.config.channel("phones").effects.append(Effect("gain"))
        self.window.refresh()
        self.assertIsNot(self.strip("phones"), before)
        self.assertEqual(len(self.strip("phones").insert_buttons), 1)

    def test_an_insert_is_lit_while_on_and_switching_it_is_live(self):
        button = self.strip("speakers").insert_buttons[0]
        self.assertTrue(button.isChecked())
        with mock.patch.object(Engine, "apply"):
            button.click()
        self.assertFalse(Config.load(self.engine.path).channel("speakers").effects[0].enabled)
        self.assertTrue(self.window._pending_apply.isActive())
        self.assertFalse(self.window._structural_pending)  # a tune, not a restart

    def test_switching_an_insert_keeps_the_desk_and_its_scroll_position(self):
        # Every switch used to rebuild the whole desk, which threw the view back
        # to the start (owner, 5 Oct 2026).
        before = self.strip("speakers")
        with mock.patch.object(Engine, "apply"):
            before.insert_buttons[0].click()
            self.window.refresh()
        self.assertIs(self.strip("speakers"), before)

    def test_an_effect_switched_elsewhere_is_shown_in_place(self):
        # The phone, or the Channels view, switched it: the same button follows.
        button = self.strip("speakers").insert_buttons[0]
        self.engine.config.channel("speakers").effects[0].enabled = False
        self.window.refresh()
        self.assertIs(self.strip("speakers").insert_buttons[0], button)
        self.assertFalse(button.isChecked())

    def test_trim_sets_that_channels_desktop_volume(self):
        with mock.patch.object(Engine, "set_channel_volume") as set_volume:
            self.strip("phones").trim.setValue(50)
            self.window._write_volume()
        set_volume.assert_called_once_with("phones", 0.5)
        self.assertEqual(self.strip("phones").trim_label.text(), "-18.1 dB")

    def test_two_trims_moved_together_both_land(self):
        with mock.patch.object(Engine, "set_channel_volume") as set_volume:
            self.strip("phones").trim.setValue(50)
            self.strip("speakers").trim.setValue(25)
            self.window._write_volume()
        self.assertEqual([c.args for c in set_volume.call_args_list],
                         [("phones", 0.5), ("speakers", 0.25)])

    def test_the_fader_is_saved_and_applied_live(self):
        from audiorouter.gui.mixer import db_to_fader

        with mock.patch.object(Engine, "apply"):
            self.strip("speakers").fader.setValue(db_to_fader(-6.0))
        self.assertEqual(Config.load(self.engine.path).channel("speakers").fader_db, -6.0)
        self.assertEqual(self.strip("speakers").volume_label.text(), "fader -6.0 dB")
        self.assertTrue(self.window._pending_apply.isActive())
        self.assertFalse(self.window._structural_pending)  # a live change, not a restart

    def test_the_fader_starts_where_it_was_left_and_goes_off_at_the_bottom(self):
        from audiorouter.gui.mixer import db_to_fader, fader_text, fader_to_db

        self.assertEqual(self.strip("phones").fader.value(), db_to_fader(0.0))
        self.assertEqual(fader_text(fader_to_db(0)), "off")
        for db in (-60.0, -20.0, -6.0, 0.0, 10.0):
            self.assertAlmostEqual(fader_to_db(db_to_fader(db)), db, places=1)

    def test_pan_is_saved_applied_live_and_centres_on_double_click(self):
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        strip = self.strip("speakers")
        with mock.patch.object(Engine, "apply"):
            strip.pan.setValue(-40)
        self.assertEqual(Config.load(self.engine.path).channel("speakers").pan, -0.4)
        self.assertEqual(strip.pan_label.text(), "L40")
        self.assertTrue(self.window._pending_apply.isActive())
        self.assertFalse(self.window._structural_pending)
        click = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        with mock.patch.object(Engine, "apply"):
            QApplication.sendEvent(strip.pan, click)
        self.assertEqual(strip.pan.value(), 0)
        self.assertEqual(strip.pan_label.text(), "C")
        self.assertEqual(self.engine.config.channel("speakers").pan, 0.0)

    def test_solo_cuts_the_other_outputs_but_not_the_inputs(self):
        with mock.patch.object(Engine, "apply"):
            self.strip("phones").solo.click()
        saved = Config.load(self.engine.path)
        self.assertTrue(saved.channel("phones").solo)
        self.assertFalse(self.strip("speakers").cut_label.isHidden())
        self.assertTrue(self.strip("phones").cut_label.isHidden())
        self.assertTrue(self.strip("mic").cut_label.isHidden())
        self.assertTrue(self.window._pending_apply.isActive())
        self.assertFalse(self.window._structural_pending)  # cuts are live, not restarts
        self.assertEqual(self.engine.config.channel("speakers").controls()["fader_l:Gain 1"], 0.0)
        with mock.patch.object(Engine, "apply"):
            self.strip("phones").solo.click()
        self.assertTrue(self.strip("speakers").cut_label.isHidden())
        self.assertEqual(self.engine.config.channel("speakers").controls()["fader_l:Gain 1"], 1.0)

    def test_the_inserts_title_folds_every_strip_and_is_remembered(self):
        self.strip("phones").inserts_toggle.click()
        for strip in self.mixer.strips.values():
            self.assertTrue(strip.insert_area.isHidden())
        self.assertEqual(self.strip("speakers").inserts_toggle.text(), "INSERTS (1)")
        self.assertEqual(self.strip("phones").inserts_toggle.text(), "INSERTS")  # none to count
        self.assertFalse(self.window._setting_bool("mixer_inserts_open", True))
        # A rebuild (here: an effect added) keeps the desk folded.
        self.engine.config.channel("phones").effects.append(Effect("gain", enabled=False))
        self.window.refresh()
        self.assertTrue(self.strip("phones").insert_area.isHidden())
        self.assertEqual(self.strip("phones").inserts_toggle.text(), "INSERTS (0/1 on)")
        self.strip("speakers").inserts_toggle.click()
        self.assertFalse(self.strip("phones").insert_area.isHidden())
        self.assertTrue(self.window._setting_bool("mixer_inserts_open", False))

    def test_an_output_can_play_into_another_which_becomes_a_group(self):
        route = self.strip("phones").route
        index = route.findData("ar_speakers")
        self.assertEqual(route.itemText(index), "Into Speakers")
        self.assertLess(self.strip("mic").route.findData("ar_speakers"), 1)  # inputs listen instead
        with mock.patch.object(Engine, "apply"):
            route.activated.emit(index)
            self.window.refresh()
        self.assertEqual(Config.load(self.engine.path).channel("phones").device, "ar_speakers")
        self.assertTrue(self.window._structural_pending)
        # Speakers is now a group: its own section, its members named, and
        # no way back into Headphones (that would be a loop).
        self.assertEqual(list(self.mixer.strips), ["mic", "phones", "speakers"])
        self.assertIn("from Headphones", self.strip("speakers").apps.toolTip())  # the label may be shortened
        self.assertEqual(self.strip("speakers").kind.text(), "GROUP")
        self.assertLess(self.strip("speakers").route.findData("ar_phones"), 0)

    def test_the_channels_view_has_the_same_fader_and_both_stay_in_step(self):
        from audiorouter.gui.mixer import db_to_fader

        self.window.select_channel("speakers")
        panel = self.window.channel_panel
        self.assertEqual(panel.fader_label.text(), "+0.0 dB")
        with mock.patch.object(Engine, "apply"):
            panel.fader.setValue(db_to_fader(-12.0))
        self.assertEqual(Config.load(self.engine.path).channel("speakers").fader_db, -12.0)
        self.assertEqual(self.strip("speakers").fader.value(), db_to_fader(-12.0))
        self.assertEqual(self.strip("speakers").volume_label.text(), "fader -12.0 dB")
        self.assertFalse(self.window._structural_pending)  # live, like the mixer's
        with mock.patch.object(Engine, "apply"):
            self.strip("speakers").fader.setValue(db_to_fader(3.0))
        self.assertEqual(panel.fader.value(), db_to_fader(3.0))
        self.assertEqual(panel.fader_label.text(), "+3.0 dB")
        # Another channel moved on the mixer leaves the panel alone.
        with mock.patch.object(Engine, "apply"):
            self.strip("phones").fader.setValue(db_to_fader(-20.0))
        self.assertEqual(panel.fader_label.text(), "+3.0 dB")

    def test_a_fader_step_is_never_snapped_back(self):
        # Fine travel near the top is < 0.1 dB a step; rounding must not stall it.
        strip = self.strip("speakers")
        with mock.patch.object(Engine, "apply"):
            for value in range(900, 920):
                strip.fader.setValue(value)
                self.assertEqual(strip.fader.value(), value)

    def test_outputs_get_a_device_strip_whose_volume_and_mute_drive_the_device(self):
        strip = self.mixer.device_strips["alsa_output.a"]
        strip.show_state({"volume": 1.0, "muted": False})
        self.assertEqual(strip.kind.text(), "OUTPUT")
        with mock.patch.object(Engine, "set_device_volume") as set_volume:
            strip.volume.setValue(40)
            strip.volume.setValue(50)  # a drag: one write, the last value
            self.window._write_device_volume()
        set_volume.assert_called_once_with("alsa_output.a", 0.5)
        with mock.patch.object(Engine, "set_device_muted") as set_muted:
            strip.mute.click()
        set_muted.assert_called_once_with("alsa_output.a", True)

    def test_the_desk_reads_mics_channels_groups_outputs_and_hides_companions(self):
        status = {"channels": [], "streams": [{"id": 1, "app": "Spotify", "channel": "mic_mix"}],
                  "input_devices": [{"name": "alsa_input.usb", "label": "USB Mic", "volume": 0.5, "muted": False}],
                  "devices": [{"name": "alsa_output.a", "label": "Built-in Audio", "volume": 1.0, "muted": False}]}
        self.mixer.refresh(self.engine.config, status)
        self.assertNotIn("mic_mix", self.mixer.strips)
        self.assertEqual(list(self.mixer.device_strips), ["alsa_input.usb", "alsa_output.a"])
        self.assertEqual(self.mixer.device_strips["alsa_input.usb"].kind.text(), "MIC")
        self.assertEqual(self.mixer.device_strips["alsa_input.usb"].volume.value(), 50)
        # Apps sent into a mic channel are listed on its strip.
        self.assertEqual(self.strip("mic").apps.text(), "+ Spotify")
        headers = [self.mixer.row.itemAt(i).widget() for i in range(self.mixer.row.count())]
        titles = [w.findChild(QLabel).text() for w in headers
                  if w is not None and not hasattr(w, "channel") and not hasattr(w, "device")]
        self.assertEqual(titles, ["MICS", "CHANNELS", "OUTPUTS"])

    def test_an_app_can_be_sent_into_a_mic(self):
        combo = self.window.streams_panel.table.cellWidget(0, 2)
        labels = [combo.itemText(i) for i in range(combo.count())]
        self.assertIn("Desk mic (into the mic)", [l.replace(" (stopped)", "") for l in labels])

    def test_mic_app_and_group_strips_line_up_row_for_row(self):
        # Measured on the rendered desk, not the layout code: a mic strip has
        # a mic list that the others lack, which pushed every row out of line.
        from PyQt6.QtCore import QCoreApplication

        self.engine.config.add_channel(Channel("master", "Master", "alsa_output.a"))
        self.engine.config.channel("speakers").device = "ar_master"
        self.window.refresh()
        self.window.resize(1250, 800)
        self.window.show()
        for _ in range(4):
            QCoreApplication.processEvents()
        def rows(strip):
            return tuple(w.mapTo(self.mixer.desk, w.rect().topLeft()).y()
                         for w in (strip.trim, strip.inserts_toggle, strip.route, strip.pan, strip.fader))
        self.assertEqual({rows(s) for s in self.mixer.strips.values()}.__len__(), 1,
                         {slug: rows(s) for slug, s in self.mixer.strips.items()})
        self.window.hide()

    def test_mute_mutes_that_channel(self):
        with mock.patch.object(Engine, "set_channel_muted") as set_muted:
            self.strip("speakers").mute.setEnabled(True)
            self.strip("speakers").mute.click()
        set_muted.assert_called_once_with("speakers", True)

    def test_choosing_an_output_saves_and_schedules_a_restart(self):
        route = self.strip("phones").route
        index = route.findData("alsa_output.a")
        self.assertGreaterEqual(index, 0)
        route.setCurrentIndex(index)
        route.activated.emit(index)
        self.assertEqual(Config.load(self.engine.path).channel("phones").device, "alsa_output.a")
        self.assertTrue(self.window._structural_pending)

    def test_an_input_strip_listens_through_an_output(self):
        route = self.strip("mic").route
        index = route.findData("speakers")
        route.activated.emit(index)
        self.assertEqual(Config.load(self.engine.path).channel("mic").listen, "speakers")

    def test_double_clicking_an_insert_opens_it_in_the_channels_view(self):
        self.window._open_effect("speakers", 0)
        self.assertEqual(self.window.views.currentIndex(), 1)
        self.assertEqual(self.window.selected_channel.slug, "speakers")
        self.assertEqual(self.window.effects_panel.list.currentRow(), 0)

    def test_insert_opens_the_browser_for_that_channel(self):
        with mock.patch("audiorouter.gui.effects_panel.EffectsPanel.choose_effect") as choose:
            self.strip("phones").add_button.click()
        choose.assert_called_once()
        self.assertEqual(self.window.selected_channel.slug, "phones")
        self.assertIs(self.window.views.currentWidget(), self.mixer)

    def test_strip_meters_read_each_channels_output_tap_only_while_the_mixer_is_seen(self):
        taps = {s: Tap("Out", (), 42, f"/run/42.{s}") for s in ("speakers", "phones", "mic")}
        with mock.patch("audiorouter.gui.mixer.output_tap", side_effect=lambda c: taps[c.slug]):
            self.mixer.set_active(True)
            self.assertEqual(sorted(r.tap.file for r in FakeReader.started if r.running and r.tap.file),
                             sorted(t.file for t in taps.values()))
            self.window.views.setCurrentIndex(1)
            with mock.patch.object(self.window, "isVisible", return_value=True):
                self.window._update_meters()
        self.assertFalse(any(r.running for r in FakeReader.started if r.tap.file.startswith("/run/42")))
        self.assertFalse(self.mixer.active)
        self.assertTrue(self.window.meters.active)

    def test_inputs_are_driven_only_while_the_mixer_is_seen(self):
        FakeDriver.started = []
        taps = {s: Tap("Out", (), 42, f"/run/42.{s}") for s in ("speakers", "phones", "mic")}
        with mock.patch("audiorouter.gui.mixer.output_tap", side_effect=lambda c: taps[c.slug]), \
                mock.patch.object(Channel, "pid", return_value=42):
            self.mixer.set_active(True)
            self.assertEqual([(d.slug, d.running) for d in FakeDriver.started], [("mic", True)])
            self.mixer.set_active(False)
        self.assertFalse(FakeDriver.started[0].running)

    def test_a_driver_that_ends_is_replaced_and_a_restart_gets_a_new_one(self):
        FakeDriver.started = []
        tap = Tap("Out", (), 42, "/run/42.mic")
        with mock.patch("audiorouter.gui.mixer.output_tap",
                        side_effect=lambda c: tap if c.slug == "mic" else None), \
                mock.patch.object(Channel, "pid", return_value=42):
            self.mixer.set_active(True)
            FakeDriver.started[0].running = False  # parec ended on its own
            self.mixer._follow_taps()
            self.assertEqual(len(FakeDriver.started), 2)
            self.assertTrue(self.mixer._retry.isActive())  # and it looks again soon
        tap = Tap("Out", (), 43, "/run/43.mic")  # the input restarted: a new host
        with mock.patch("audiorouter.gui.mixer.output_tap",
                        side_effect=lambda c: tap if c.slug == "mic" else None), \
                mock.patch.object(Channel, "pid", return_value=43):
            self.mixer._follow_taps()
        self.assertEqual([d.host for d in FakeDriver.started], [42, 42, 43])
        self.assertEqual([d.running for d in FakeDriver.started], [False, False, True])
        self.mixer.stop()

    def test_readings_move_that_strips_meter(self):
        from PyQt6.QtCore import QCoreApplication

        from audiorouter.meter import Levels

        tap = Tap("Out", (), 42, "/run/42.speakers")
        with mock.patch("audiorouter.gui.mixer.output_tap",
                        side_effect=lambda c: tap if c.slug == "speakers" else None):
            self.mixer.set_active(True)
        reader = next(r for r in FakeReader.started if r.tap is tap)
        reader.on_levels(Levels(0.5, 0.5, 0.125, 0.125))
        QCoreApplication.sendPostedEvents(None, 0)
        self.assertAlmostEqual(self.strip("speakers").meter_l.state.hold_db, -6.02, places=1)
        self.assertLess(self.strip("phones").meter_l.state.hold_db, -50)
