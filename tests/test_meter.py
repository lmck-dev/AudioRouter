"""Level taps: what a channel's In and Out are read from, and the level maths."""

import array
import math
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import meter
from audiorouter.channels import INPUT, NOWHERE, Channel
from audiorouter.meter import LevelReader, Tap, channel_taps, measure
from audiorouter.pwgraph import Graph

from . import fakes


def stereo(left, right) -> bytes:
    samples = array.array("f")
    for l, r in zip(left, right):
        samples.extend((l, r))
    return samples.tobytes()


class MeasureTest(unittest.TestCase):
    def test_peak_and_mean_square_per_side(self):
        n = 4800
        left = [0.5 * math.sin(2 * math.pi * 440 * i / 48000) for i in range(n)]
        levels = measure(stereo(left, [0.0] * n))
        self.assertAlmostEqual(levels.peak_l, 0.5, places=3)
        self.assertAlmostEqual(levels.ms_l, 0.125, places=3)  # sine: peak^2 / 2
        self.assertEqual((levels.peak_r, levels.ms_r), (0.0, 0.0))

    def test_a_negative_peak_counts(self):
        levels = measure(stereo([0.1, -0.9], [0.2, 0.3]))
        self.assertAlmostEqual(levels.peak_l, 0.9, places=5)
        self.assertAlmostEqual(levels.peak_r, 0.3, places=5)

    def test_a_torn_block_is_trimmed_and_an_empty_one_is_silence(self):
        self.assertAlmostEqual(measure(stereo([0.5], [0.25]) + b"\x00\x01").peak_r, 0.25, places=5)
        self.assertEqual(measure(b"").peak_l, 0.0)


class TapTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        pid = mock.patch.object(Channel, "pid", return_value=4242)
        pid.start()
        self.addCleanup(pid.stop)

    def graph(self, *extra):
        return Graph([
            fakes.sink(51, "ar_speakers", serial=510, hardware=False, **{"audiorouter.channel": "speakers"}),
            fakes.node(61, "ar_speakers_out", "Stream/Output/Audio", serial=610,
                       **{"audiorouter.channel": "speakers"}),
            *extra,
        ])

    def test_an_output_reads_its_sink_monitor_before_and_its_own_stream_after(self):
        tap_in, tap_out = channel_taps(Channel("speakers", "Speakers", "alsa_output.a"), self.graph())
        self.assertEqual(tap_in.args, ("--device=ar_speakers.monitor",))
        # pipewire-pulse numbers sink inputs by object.serial (checked live).
        self.assertEqual(tap_out.args, ("--monitor-stream=610",))
        self.assertEqual(tap_in.host, 4242)

    def test_a_virtual_cable_is_read_after_its_effects_from_its_recording_source(self):
        graph = self.graph(fakes.node(62, "ar_speakers_rec", "Audio/Source", serial=620,
                                      **{"audiorouter.channel": "speakers"}))
        cable = Channel("speakers", "Speakers", NOWHERE)
        self.assertEqual(channel_taps(cable, graph)[1].args, ("--device=ar_speakers_rec",))

    def test_an_input_reads_the_microphone_and_its_virtual_mic(self):
        # The mic channel's own node is its capture; Out reads the virtual mic,
        # which its companion publishes under the mic channel's name.
        graph = Graph([fakes.node(70, "ar_mic_in", "Stream/Input/Audio", serial=700, **{"audiorouter.channel": "mic"})])
        tap_in, tap_out = channel_taps(Channel("mic", "Mic", "alsa_input.usb", kind=INPUT), graph)
        self.assertEqual((tap_in.args, tap_out.args), (("--device=alsa_input.usb",), ("--device=ar_mic",)))
        default_mic = channel_taps(Channel("mic", "Mic", "", kind=INPUT), graph)[0]
        self.assertEqual(default_mic.args, ("--device=@DEFAULT_SOURCE@",))

    def test_a_channel_that_is_not_running_has_nothing_to_read(self):
        with mock.patch.object(Channel, "pid", return_value=None):
            self.assertEqual(channel_taps(Channel("speakers", "Speakers", "a"), self.graph()), (None, None))
        self.assertEqual(channel_taps(Channel("other", "Other", "a"), self.graph()), (None, None))

    def test_a_restart_changes_the_key_even_when_the_names_do_not(self):
        channel = Channel("speakers", "Speakers", "alsa_output.a")
        before = channel_taps(channel, self.graph())[0]
        with mock.patch.object(Channel, "pid", return_value=5151):
            after = channel_taps(channel, self.graph())[0]
        self.assertEqual(before.args, after.args)
        self.assertNotEqual(before.key, after.key)


class ReaderTest(unittest.TestCase):
    def test_the_stream_is_marked_and_never_moved_or_sent_elsewhere(self):
        command = LevelReader(Tap("In", ("--device=x",), 1), lambda _l: None).command()
        self.assertEqual(command[0], "parec")
        for flag in ("--property=audiorouter.meter=true", "--property=node.dont-reconnect=true",
                     "--property=node.dont-fallback=true", "--raw", "--format=float32le", "--device=x"):
            self.assertIn(flag, command)

    def fake_parec(self, blocks: int, amplitude: float):
        # A stand-in parec: writes `blocks` blocks of a constant level, then exits.
        script = (
            "import array, sys\n"
            f"block = array.array('f', [{amplitude}] * {meter.BLOCK * 2}).tobytes()\n"
            f"for _ in range({blocks}): sys.stdout.buffer.write(block)\n"
            "sys.stdout.buffer.flush()\n"
        )
        return [sys.executable, "-c", script]

    def test_readings_arrive_per_block_and_an_ending_is_reported(self):
        readings, ended = [], threading.Event()
        reader = LevelReader(Tap("Out", (), 1), readings.append, ended.set)
        with mock.patch.object(LevelReader, "command", return_value=self.fake_parec(3, 0.25)), \
             mock.patch("audiorouter.meter.require_tools"):
            reader.start()
            self.assertTrue(ended.wait(5))
        self.assertEqual(len(readings), 3)
        self.assertAlmostEqual(readings[0].peak_l, 0.25, places=5)
        self.assertAlmostEqual(readings[0].ms_r, 0.0625, places=5)

    def test_stopping_is_not_reported_as_an_ending(self):
        ended = threading.Event()
        forever = [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"]
        reader = LevelReader(Tap("Out", (), 1), lambda _l: None, ended.set)
        with mock.patch.object(LevelReader, "command", return_value=forever), \
             mock.patch("audiorouter.meter.require_tools"):
            reader.start()
            self.assertTrue(reader.running)
            reader.stop()
        self.assertFalse(reader.running)
        self.assertFalse(ended.wait(0.3))


if __name__ == "__main__":
    unittest.main()
