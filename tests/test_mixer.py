"""The mixer view: strips edit the same channels as the Channels view."""

import unittest
from unittest import mock

from audiorouter.channels import INPUT, Channel
from audiorouter.config import Config
from audiorouter.engine import Engine
from audiorouter.meter import Tap

from .test_gui import QApplication, FakeReader, GuiTestCase


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
        self.assertEqual(self.strip("mic").kind.text(), "INPUT")
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
            self.assertEqual(sorted(r.tap.file for r in FakeReader.started if r.running),
                             sorted(t.file for t in taps.values()))
            self.window.views.setCurrentIndex(1)
            with mock.patch.object(self.window, "isVisible", return_value=True):
                self.window._update_meters()
        self.assertFalse(any(r.running for r in FakeReader.started if r.tap.file.startswith("/run/42")))
        self.assertFalse(self.mixer.active)
        self.assertTrue(self.window.meters.active)

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
