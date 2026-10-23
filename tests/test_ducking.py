"""Turning channels down while a priority channel plays (owner, 23 Oct 2026)."""

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import ducking
from audiorouter.channels import INPUT, Channel
from audiorouter.config import Config
from audiorouter.effects import DUCK_DEPTH, DUCK_TARGET, DUCK_TIME, db_to_linear, duck_controls, render_chain
from audiorouter.engine import EngineError
from audiorouter.meter import TAP_FILE_SIZE, TAP_MAGIC, TAP_RING, _TAP_ENTRY, _TAP_HEADER

from .test_engine import EngineTestCase


def desk(**voice):
    return Config(channels=[
        Channel("speakers", "Speakers", "alsa_output.speakers"),
        Channel("game", "Game", "ar_speakers"),
        Channel("phones", "Headphones", "bluez_output.phones"),
        Channel("voice", "TTS Voice", "", **voice),
    ])


class ChainTest(unittest.TestCase):
    def nodes(self, chain):
        return {n["name"]: n for n in chain.nodes}

    def test_no_duck_stage_unless_turned_down(self):
        self.assertFalse(any(n["name"].startswith("duck") for n in render_chain([]).nodes))

    def test_the_stage_follows_the_fader_and_reaches_exactly_the_amount(self):
        chain = render_chain([], duck_db=15.0)
        self.assertEqual(chain.outputs, ("duck_l:Out", "duck_r:Out"))
        self.assertIn({"output": "fader_l:Out", "input": "duck_l:In 1"}, chain.links)
        gain = self.nodes(chain)["duck_gain"]["control"]
        # Fully down (fade = 1): 1 - depth = -15 dB.
        self.assertAlmostEqual(gain["Add"] + gain["Mult"], db_to_linear(-15.0), places=5)
        self.assertEqual(self.nodes(chain)["duck_target"]["control"]["Add"], 0.0)  # starts up

    def test_a_tapped_chain_meters_after_the_stage(self):
        chain = render_chain([], taps=True, duck_db=10.0)
        tap = next(n["name"] for n in chain.nodes if n["name"].startswith("tap"))
        self.assertIn({"output": "duck_l:Out", "input": f"{tap}:in_l"}, chain.links)

    def test_down_is_fast_and_up_is_slow(self):
        down, up = duck_controls(True), duck_controls(False)
        self.assertEqual(down[DUCK_TARGET], 1.0)
        self.assertEqual(up[DUCK_TARGET], 0.0)
        self.assertLess(down[DUCK_TIME], up[DUCK_TIME])


class ConfigTest(unittest.TestCase):
    def test_listed_channels_are_turned_down_by_the_amount(self):
        config = desk(ducks=["phones"], duck_db=12.0)
        self.assertEqual(config.channel("phones").ducked_db, 12.0)
        self.assertEqual(config.channel("speakers").ducked_db, 0.0)

    def test_a_channel_playing_into_a_turned_down_group_goes_down_once(self):
        config = desk(ducks=["speakers", "game"])
        self.assertEqual(config.ducked_by(config.channel("voice")), ["speakers"])
        self.assertEqual(config.channel("game").ducked_db, 0.0)

    def test_never_a_channel_the_voice_plays_through(self):
        config = Config(channels=[
            Channel("speakers", "Speakers", "alsa_output.speakers"),
            Channel("voice", "TTS Voice", "ar_speakers", ducks=["speakers"]),
        ])
        self.assertEqual(config.channel("speakers").ducked_db, 0.0)

    def test_saved_only_when_set_and_round_trips(self):
        plain = Channel("a", "A", "").to_dict()
        self.assertNotIn("ducks", plain)
        data = Channel("v", "V", "", ducks=["a", "a", "v"], duck_db=99).to_dict()
        self.assertEqual(data["ducks"], ["a"])          # no repeats, never itself
        self.assertEqual(data["duck_db"], 40.0)         # clamped
        self.assertEqual(Channel.from_dict(data).ducks, ["a"])

    def test_inputs_turn_nothing_down(self):
        self.assertEqual(Channel("m", "Mic", "", kind=INPUT, ducks=["a"]).ducks, [])

    def test_a_disabled_voice_turns_nothing_down(self):
        config = desk(ducks=["phones"], enabled=False)
        self.assertEqual(config.channel("phones").ducked_db, 0.0)


class EngineTest(EngineTestCase):
    def test_set_ducks_saves_and_refuses_what_the_voice_plays_through(self):
        self.engine.config.add_channel(Channel("phones", "Headphones", "bluez_output.phones"))
        self.engine.config.add_channel(Channel("voice", "Voice", "ar_speakers"))
        with self.assertRaises(EngineError):
            self.engine.set_ducks("voice", ["speakers"])
        self.engine.set_ducks("voice", ["phones"], 20.0)
        self.assertEqual(self.engine.config.channel("phones").ducked_db, 20.0)
        self.engine.set_ducks("voice", [])
        self.assertEqual(self.engine.config.channel("phones").ducked_db, 0.0)


def tap_file(path, readings):
    """A level tap's file holding these (peak, mean square) readings."""
    data = bytearray(TAP_FILE_SIZE)
    _TAP_HEADER.pack_into(data, 0, TAP_MAGIC, len(readings))
    for k, (peak, ms) in enumerate(readings):
        _TAP_ENTRY.pack_into(data, _TAP_HEADER.size + (k % TAP_RING) * _TAP_ENTRY.size,
                             peak, peak, ms, ms)
    Path(path).write_bytes(bytes(data))


def at(db):
    """A reading of a steady sound at this level (dBFS)."""
    return (db_to_linear(db) * 1.41, db_to_linear(db) ** 2)


class Inline:
    """An executor that runs each job at once, so a test sees what was sent."""

    def submit(self, job, *args):
        from concurrent.futures import Future
        future = Future()
        future.set_result(job(*args))
        return future

    def shutdown(self, wait=True):
        pass


class DuckerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.files = {}
        self.readings = {}
        for name in ("trigger", "voice", "phones", "game"):
            self.files[name] = str(Path(self.dir.name) / name)
            self.readings[name] = []
            tap_file(self.files[name], [])
        self.config = desk(ducks=["phones", "speakers"], duck_db=24.0)
        self.config.channel("speakers").fader_db = 0.0
        self.engine = mock.Mock(config=self.config)
        self.sent = []
        self.ducker = ducking.Ducker(self.engine)

        def send(channel, values, state):
            self.sent.append((channel.slug, dict(values)))
        self.ducker._send = send
        self.ducker._pool.shutdown()
        self.ducker._pool = Inline()
        level = {"voice": "voice", "phones": "phones", "speakers": "game"}
        patches = [
            mock.patch.object(ducking, "trigger_file", return_value=self.files["trigger"]),
            mock.patch.object(ducking, "level_file", side_effect=lambda c: self.files[level[c.slug]]),
            mock.patch.object(Channel, "pid", return_value=1),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def add(self, name, *readings):
        self.readings[name].extend(readings)
        tap_file(self.files[name], self.readings[name])

    def depth(self, slug):
        """How far the last 'down' sent to this channel goes, in dB (None: none sent)."""
        for sent, values in reversed(self.sent):
            if sent == slug and values[DUCK_TARGET] == 1.0:
                return round(-20 * math.log10(1 + values[DUCK_DEPTH]), 1)
        return None

    def speak(self, now, voice_db=-20.0):
        self.add("trigger", at(voice_db))
        self.add("voice", at(voice_db))
        self.ducker.tick(now)

    def test_a_loud_channel_goes_down_just_enough_and_a_quiet_one_not_at_all(self):
        self.ducker.tick(0.0)                       # finds the files
        self.add("game", *[at(-12.0)] * 20)         # loud: 8 dB over the voice
        self.add("phones", *[at(-40.0)] * 20)       # already 20 dB under it
        self.ducker.tick(1.0)
        self.speak(1.03)
        # Down to 10 dB under a -20 dB voice: -12 -> -30 is 18 dB.
        self.assertEqual(self.depth("speakers"), 18.0)
        self.assertIsNone(self.depth("phones"))

    def test_never_more_than_the_channel_allows(self):
        self.config.channel("voice").duck_db = 6.0
        self.ducker.tick(0.0)
        self.add("game", *[at(-6.0)] * 20)
        self.ducker.tick(1.0)
        self.speak(1.03)
        self.assertEqual(self.depth("speakers"), 6.0)

    def test_the_groups_fader_counts_toward_how_loud_it_is(self):
        self.config.channel("speakers").fader_db = 0.0
        self.config.channel("game").ducks = []
        config = Config(channels=[
            Channel("speakers", "Speakers", "alsa_output.speakers", fader_db=-10.0),
            Channel("phones", "Headphones", "ar_speakers"),
            Channel("voice", "TTS Voice", "", ducks=["phones"]),
        ])
        self.engine.config = config
        self.ducker.tick(0.0)
        self.add("phones", *[at(-12.0)] * 20)       # -22 after the speakers' fader
        self.ducker.tick(1.0)
        self.speak(1.03)
        self.assertEqual(self.depth("phones"), 8.0)

    def test_up_after_the_hold(self):
        self.ducker.tick(0.0)
        self.add("game", *[at(-12.0)] * 20)
        self.ducker.tick(1.0)
        self.speak(1.03)
        self.add("trigger", at(-200.0))
        self.ducker.tick(1.03 + ducking.HOLD_S - 0.1)
        self.assertEqual(self.sent[-1][1][DUCK_TARGET], 1.0)
        self.ducker.tick(1.03 + ducking.HOLD_S + 0.01)
        self.assertEqual(self.sent[-1], ("speakers", duck_controls(False)))

    def test_the_voice_level_is_learned(self):
        self.ducker.tick(0.0)
        self.add("game", *[at(-12.0)] * 20)
        self.ducker.tick(1.0)
        for k in range(60):                          # a quiet voice, -30 dB
            self.speak(1.03 + k * 0.03, voice_db=-30.0)
        self.ducker.tick(5.0)                        # quiet, back up
        self.ducker.tick(5.0 + ducking.DUCK_RELEASE_S + 0.01)
        self.add("game", *[at(-12.0)] * 20)
        self.ducker.tick(8.0)
        self.speak(8.03, voice_db=-30.0)
        self.assertAlmostEqual(self.depth("speakers"), 24.0, delta=0.1)   # 28 needed, capped

    def test_bypass_brings_everything_back(self):
        self.ducker.tick(0.0)
        self.add("game", *[at(-12.0)] * 20)
        self.ducker.tick(1.0)
        self.speak(1.03)
        self.config.bypass = True
        self.ducker.tick(1.06)
        self.assertEqual(self.sent[-1], ("speakers", duck_controls(False)))

try:
    from .test_gui import QApplication, GuiTestCase
except ImportError:  # pragma: no cover
    QApplication = None
    GuiTestCase = unittest.TestCase


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class PanelTest(GuiTestCase):
    def setUp(self):
        super().setUp()
        self.engine.config.add_channel(Channel("voice", "TTS Voice", "ar_speakers"))
        self.engine.config.add_channel(Channel("mic", "Desk mic", "", kind=INPUT))
        self.window.refresh()
        self.panel = self.window.channel_panel

    def actions(self):
        return {a.text(): a for a in self.panel.ducks_menu.actions()}

    def test_ticking_a_channel_turns_it_down_and_saves(self):
        self.window.select_channel("voice")
        self.assertTrue(self.panel.form.isRowVisible(self.panel.duck_row))
        self.assertEqual(self.panel.ducks.text(), "Nothing")
        self.assertFalse(self.panel.duck_db.isEnabled())
        actions = self.actions()
        # The voice plays through the speakers: turning them down would take it too.
        self.assertFalse(actions["Speakers (it plays through this)"].isEnabled())
        self.assertNotIn("Desk mic", actions)
        actions["Headphones"].setChecked(True)
        self.assertEqual(self.engine.config.channel("voice").ducks, ["phones"])
        self.assertEqual(self.panel.ducks.text(), "Headphones")
        self.assertTrue(self.panel.duck_db.isEnabled())
        self.panel.duck_db.setValue(20)
        self.assertEqual(self.engine.config.channel("voice").duck_db, 20.0)

    def test_a_mic_channel_has_no_such_row(self):
        self.window.select_channel("mic")
        self.assertFalse(self.panel.form.isRowVisible(self.panel.duck_row))


if __name__ == "__main__":
    unittest.main()
