"""Evening out loud and quiet channels (auto-level, owner 23 Oct 2026)."""

import math
import os
import tempfile
import unittest
from unittest import mock

from audiorouter import levelling
from audiorouter.channels import Channel, read_auto_levels, write_auto_levels
from audiorouter.config import Config
from audiorouter.effects import (AUTO_ADD, AUTO_MULT, AUTO_TARGET, AUTO_TIME, auto_controls,
                                 db_to_linear, make_effect, render_chain)
from audiorouter.levelling import Levelled


def gain_at(values, fade):
    return fade * values[AUTO_MULT] + values[AUTO_ADD]


class ChainTest(unittest.TestCase):
    def nodes(self, chain):
        return {n["name"]: n for n in chain.nodes}

    def test_no_stage_unless_asked(self):
        self.assertFalse(any(n["name"].startswith("auto") for n in render_chain([]).nodes))

    def test_the_stage_sits_between_the_effects_and_the_fader(self):
        chain = render_chain([make_effect("gain", {})], taps=True, auto_level=True)
        self.assertIn({"output": "sw0_switch_l:Out", "input": "auto_l:In 1"}, chain.links)
        self.assertIn({"output": "auto_l:Out", "input": "fader_l:In 1"}, chain.links)

    def test_it_starts_at_the_gain_it_is_given(self):
        gain = self.nodes(render_chain([], auto_level=True, auto_db=-6.0))["auto_gain"]["control"]
        self.assertEqual(gain["Mult"], 0.0)
        self.assertAlmostEqual(gain["Add"], db_to_linear(-6.0), places=5)

    def test_an_empty_chain_gains_a_tap_before_the_stage(self):
        chain = render_chain([], taps=True, auto_level=True)
        self.assertEqual(chain.inputs, ("pre_l:In", "pre_r:In"))
        self.assertIn({"output": "pre_l:Out", "input": "tap0:in_l"}, chain.links)
        self.assertIn({"output": "pre_l:Out", "input": "auto_l:In 1"}, chain.links)

    def test_a_move_never_jumps_and_lands_exactly(self):
        for fade_at in (0, 1):
            values = auto_controls(fade_at, 4.0, -2.5)
            self.assertAlmostEqual(gain_at(values, fade_at), db_to_linear(4.0), places=5)
            self.assertAlmostEqual(gain_at(values, values[AUTO_TARGET]), db_to_linear(-2.5), places=5)
            self.assertEqual(values[AUTO_TARGET], 1 - fade_at)
            self.assertIn(AUTO_TIME, values)


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_saved_only_when_on(self):
        self.assertNotIn("auto_level", Channel("a", "A", "dev").to_dict())
        channel = Channel.from_dict(Channel("a", "A", "dev", auto_level=True).to_dict())
        self.assertTrue(channel.auto_level)

    def test_a_restarted_host_starts_at_the_last_gain(self):
        write_auto_levels({"a": -7.5})
        self.assertEqual(read_auto_levels(), {"a": -7.5})
        conf = Channel("a", "A", "dev", auto_level=True).render_config()
        nodes = [n for m in conf["context.modules"]
                 for n in (m.get("args") or {}).get("filter.graph", {}).get("nodes", [])]
        gain = next(n for n in nodes if n["name"] == "auto_gain")
        self.assertAlmostEqual(gain["control"]["Add"], db_to_linear(-7.5), places=5)

    def test_a_window_apply_never_sends_the_stage(self):
        channel = Channel("a", "A", "dev", auto_level=True)
        running = channel.render_config()
        write_auto_levels({"a": -7.5})   # the service moved it since
        channel.fader_db = -3.0
        with mock.patch.object(Channel, "running_config", return_value=running):
            changes = channel.control_changes()
        self.assertIn("fader_l:Gain 1", changes)
        self.assertFalse([key for key in changes if key.startswith("auto_")])

    def test_unreadable_levels_read_as_none(self):
        levelling_path = os.path.join(self.tmp.name, "audiorouter", "auto-levels.json")
        os.makedirs(os.path.dirname(levelling_path), exist_ok=True)
        with open(levelling_path, "w") as fh:
            fh.write("not json")
        self.assertEqual(read_auto_levels(), {})


def feed(state, now, db, seconds, peak_db=None):
    """`seconds` of sound at mean square `db`, arriving at `now`."""
    ms = 10 ** (db / 10)
    peak = 10 ** ((peak_db if peak_db is not None else db + 3) / 20)
    for _ in range(int(seconds / levelling.BLOCK_S)):
        state.recent.append((now, ms, peak))


class StepTest(unittest.TestCase):
    def test_quiet_sound_creeps_up_in_small_steps(self):
        state = Levelled()
        feed(state, 10.0, -28.0, 2.0)
        self.assertEqual(state.step(10.0), (levelling.UP_STEP_DB, levelling.AUTO_FADE_S))

    def test_loud_sound_comes_down_in_bigger_steps(self):
        state = Levelled()
        feed(state, 10.0, -15.0, 2.0)   # 5 dB over: under the fast cut's 6
        self.assertEqual(state.step(10.0), (-levelling.DOWN_STEP_DB, levelling.AUTO_FADE_S))

    def test_close_enough_stays_put(self):
        state = Levelled()
        feed(state, 10.0, -22.0, 2.0)
        self.assertIsNone(state.step(10.0))

    def test_once_moving_it_settles_close_to_the_target(self):
        state = Levelled(gain_db=1.5, moving=True)
        feed(state, 10.0, -22.0, 2.0)
        self.assertEqual(state.step(10.0), (2.0, levelling.AUTO_FADE_S))

    def test_silence_and_too_little_sound_change_nothing(self):
        state = Levelled()
        self.assertIsNone(state.step(10.0))
        feed(state, 10.0, -40.0, 0.5)
        self.assertIsNone(state.step(10.0))

    def test_old_sound_leaves_the_window(self):
        state = Levelled()
        feed(state, 10.0, -40.0, 2.0)
        self.assertIsNone(state.step(10.0 + levelling.WINDOW_S + 1))

    def test_one_move_at_a_time(self):
        state = Levelled(moved=9.5)
        feed(state, 10.0, -40.0, 2.0)
        self.assertIsNone(state.step(10.0))

    def test_boost_is_capped(self):
        self.assertEqual(levelling.wanted_db(-45.0, -40.0), levelling.MAX_BOOST_DB)

    def test_cut_is_capped(self):
        self.assertEqual(levelling.wanted_db(10.0, 12.0), -levelling.MAX_CUT_DB)

    def test_peaks_are_never_lifted_past_the_ceiling(self):
        # Quiet on average but with loud peaks: lifted only until the peak hits -1 dBFS.
        self.assertAlmostEqual(levelling.wanted_db(-30.0, -5.0), 4.0)

    def test_a_loud_start_after_a_boost_is_cut_at_once(self):
        state = Levelled(gain_db=8.0)
        feed(state, 5.0, -28.0, 3.0)
        feed(state, 10.0, -8.0, levelling.SHORT_S)
        db, fade = state.step(10.0)
        self.assertAlmostEqual(db, -12.0, places=1)
        self.assertEqual(fade, levelling.FAST_FADE_S)

    def test_sound_that_has_just_started_waits_one_look(self):
        state = Levelled(gain_db=8.0)
        feed(state, 7.0, -28.0, 3.0)
        feed(state, 10.0, -8.0, 0.05)
        self.assertIsNone(state.step(10.0))


class FakeEngine:
    def __init__(self, config):
        self.config = config


class LevellerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(channels=[Channel("game", "Game", "dev", auto_level=True),
                                       Channel("music", "Music", "dev")])
        self.sent = []
        self.leveller = levelling.Leveller(FakeEngine(self.config))
        for name, value in (("pre_fader_file", None), ("running_gain_db", -3.0)):
            patcher = mock.patch.object(levelling, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(Channel, "pid", return_value=111)
        patcher.start()
        self.addCleanup(patcher.stop)

    def move(self):
        def fake(channel, state, new_db, fade_s, now):
            self.sent.append((channel.slug, new_db))
            state.gain_db, state.moved = new_db, now
        return mock.patch.object(self.leveller, "_move", side_effect=fake)

    def test_only_levelled_channels_are_watched_and_a_new_host_starts_where_it_was_rendered(self):
        self.leveller.tick(100.0)
        self.assertEqual(list(self.leveller._channels), ["game"])
        self.assertEqual(self.leveller._channels["game"].gain_db, -3.0)

    def test_it_holds_still_while_turned_down(self):
        self.leveller.is_down = lambda slug: True
        self.leveller.tick(100.0)
        feed(self.leveller._channels["game"], 102.0, -40.0, 2.0)
        with self.move():
            self.leveller.tick(102.0)
        self.assertEqual(self.sent, [])

    def test_it_moves_when_left_alone(self):
        self.leveller.tick(100.0)
        feed(self.leveller._channels["game"], 102.0, -40.0, 2.0)
        with self.move():
            self.leveller.tick(102.0)
        self.assertEqual(self.sent, [("game", -1.5)])

    def test_switching_it_off_forgets_the_gain(self):
        self.leveller.tick(100.0)
        self.leveller._save()
        self.assertIn("game", read_auto_levels())
        self.config.channel("game").auto_level = False
        self.leveller.tick(100.1)
        self.assertNotIn("game", read_auto_levels())

    def test_nothing_is_levelled_in_bypass(self):
        self.config.bypass = True
        self.leveller.tick(100.0)
        self.assertEqual(self.leveller._channels, {})


if __name__ == "__main__":
    unittest.main()


try:
    from .test_gui import GuiTestCase
except ImportError:  # pragma: no cover - no PyQt6
    GuiTestCase = None

if GuiTestCase is not None:
    class PanelTest(GuiTestCase):
        def test_the_switch_turns_it_on_and_shows_where_it_is(self):
            write_auto_levels({"speakers": -4.0})
            self.window.select_channel("speakers")
            panel = self.window.channel_panel
            self.assertEqual(panel.auto_now.text(), "")
            panel.auto_level.setChecked(True)
            self.assertTrue(self.engine.config.channel("speakers").auto_level)
            self.assertEqual(panel.auto_now.text(), "now -4.0 dB")
            strip = self.window.mixer.strips["speakers"]
            strip.show_fader()
            self.assertIn("auto -4.0 dB", strip.volume_label.text())
            self.assertTrue(strip.meter_l.auto and strip.meter_r.auto)
            panel.auto_level.setChecked(False)
            self.assertFalse(self.engine.config.channel("speakers").auto_level)
            strip.show_fader()
            self.assertFalse(strip.meter_l.auto)
