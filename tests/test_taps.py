"""Level taps between effects: the chain, the file they publish, and the plugin.

The plugin test loads the compiled tap straight through its LV2 entry point
(no PipeWire), feeds it a known signal and reads its file back with the same
code the window uses.
"""

import ctypes
import math
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audiorouter import lv2, meter, native
from audiorouter.channels import Channel
from audiorouter.effects import TAP_URI, Effect, render_chain, tap_slots

CAN_BUILD = not native.missing_for(native.METER)


def tap_file(readings, seq=None):
    """A tap's file holding `readings` (peak_l, peak_r, ms_l, ms_r) as the newest."""
    seq = len(readings) if seq is None else seq
    ring = [(0.0, 0.0, 0.0, 0.0)] * meter.TAP_RING
    for k, reading in enumerate(readings):
        ring[(seq - len(readings) + k) % meter.TAP_RING] = reading
    return struct.pack("<II", meter.TAP_MAGIC, seq) + b"".join(struct.pack("<4f", *r) for r in ring)


class ChainTest(unittest.TestCase):
    def chain(self, effects):
        return render_chain(effects, taps=True)

    def taps(self, chain):
        return [n for n in chain.nodes if n.get("plugin") == TAP_URI]

    def test_a_tap_listens_before_the_chain_and_after_every_effect(self):
        chain = self.chain([Effect("gain"), Effect("highpass"), Effect("gain")])
        self.assertEqual([n["control"]["slot"] for n in self.taps(chain)], [0.0, 1.0, 2.0, 3.0])
        feeds = {l["input"]: l["output"] for l in chain.links if l["input"].startswith("tap")}
        self.assertEqual(feeds["tap0:in_l"], "sw0_in_l:Out")
        self.assertEqual(feeds["tap1:in_r"], "sw0_switch_r:Out")
        self.assertEqual(feeds["tap3:in_l"], "sw2_switch_l:Out")

    def test_taps_only_listen(self):
        chain = self.chain([Effect("gain")])
        self.assertFalse([l for l in chain.links if l["output"].startswith("tap")])

    def test_a_tapped_chain_ends_in_copies_so_its_last_output_can_also_feed_a_tap(self):
        # filter-graph refuses a graph output port that also feeds a link, and
        # the channel then passes silence (measured 30 Sep 2026).
        chain = self.chain([Effect("gain")])
        self.assertEqual(chain.outputs, ("tail_l:Out", "tail_r:Out"))
        linked_out = {l["output"] for l in chain.links}
        self.assertFalse(set(chain.outputs) & linked_out)

    def test_without_taps_the_chain_is_unchanged(self):
        plain = render_chain([Effect("gain")])
        self.assertFalse(self.taps(plain))
        self.assertEqual(plain.outputs, ("sw0_switch_l:Out", "sw0_switch_r:Out"))

    def test_an_empty_chain_has_no_taps(self):
        self.assertFalse(self.taps(self.chain([])))

    def test_tap_slots_skip_an_effect_that_is_not_rendered(self):
        missing = Effect("lv2", plugin="http://example.org/uninstalled", enabled=False)
        self.assertEqual(tap_slots([Effect("gain"), missing, Effect("gain")]),
                         [(0, 1), None, (1, 2)])

    def test_the_tap_is_never_offered_as_an_effect(self):
        catalogue = lv2.Catalogue()
        catalogue.plugins[TAP_URI] = SimpleNamespace(uri=TAP_URI, vendor="", name="tap", usable=True)
        self.assertEqual(catalogue.all(), [])
        self.assertIsNotNone(catalogue.get(TAP_URI))


class TapFileTest(unittest.TestCase):
    def test_new_readings_come_back_in_order(self):
        data = tap_file([(0.1, 0.2, 0.01, 0.04), (0.3, 0.4, 0.09, 0.16)])
        seq, levels = meter.read_tap(data, 0)
        self.assertEqual(seq, 2)
        self.assertAlmostEqual(levels[1].peak_r, 0.4, places=6)
        self.assertEqual(meter.read_tap(data, 2), (2, []))

    def test_a_reader_more_than_a_ring_behind_gets_the_newest_ring(self):
        readings = [(k / 1000, 0.0, 0.0, 0.0) for k in range(100)]
        seq, levels = meter.read_tap(tap_file(readings[-meter.TAP_RING:], seq=100), 0)
        self.assertEqual((seq, len(levels)), (100, meter.TAP_RING))
        self.assertAlmostEqual(levels[-1].peak_l, 0.099, places=6)

    def test_anything_else_reads_as_nothing_new(self):
        self.assertEqual(meter.read_tap(b"", 5), (5, []))
        self.assertEqual(meter.read_tap(b"\0" * meter.TAP_FILE_SIZE, 5), (5, []))


class EffectTapsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.channel = Channel("speakers", "Speakers", "dev", effects=[Effect("gain"), Effect("gain")])

    def conf(self, taps):
        self.channel.config_path.write_text(
            __import__("json").dumps({"context.modules": [{"args": {"filter.graph": {
                "nodes": render_chain(self.channel.effects, taps=taps).nodes}}}]}))

    def test_an_effect_is_read_between_the_taps_around_it(self):
        self.conf(taps=True)
        with mock.patch.object(Channel, "pid", return_value=77):
            tap_in, tap_out = meter.effect_taps(self.channel, 1)
        self.assertTrue(tap_in.file.endswith("/meters/77.1"))
        self.assertTrue(tap_out.file.endswith("/meters/77.2"))
        self.assertEqual((tap_in.label, tap_out.label), ("In", "Out"))

    def test_a_host_started_without_taps_has_none_to_read(self):
        self.conf(taps=False)
        with mock.patch.object(Channel, "pid", return_value=77):
            self.assertEqual(meter.effect_taps(self.channel, 0), (None, None))

    def test_a_stopped_channel_has_none(self):
        self.conf(taps=True)
        with mock.patch.object(Channel, "pid", return_value=None):
            self.assertEqual(meter.effect_taps(self.channel, 0), (None, None))

    def test_files_of_dead_hosts_are_swept_and_live_ones_kept(self):
        directory = meter.meter_dir()
        directory.mkdir(parents=True)
        live, dead = directory / f"{os.getpid()}.0", directory / "999999999.0"
        live.write_bytes(b"x")
        dead.write_bytes(b"x")
        meter.sweep_stale()
        self.assertTrue(live.exists())
        self.assertFalse(dead.exists())


class FileLevelReaderTest(unittest.TestCase):
    def test_it_delivers_new_readings_and_waits_for_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tap"
            got, arrived = [], threading.Event()

            def on_levels(levels):
                got.append(levels)
                arrived.set()

            reader = meter.FileLevelReader(meter.Tap("In", (), os.getpid(), str(path)), on_levels)
            reader.start()
            try:
                time.sleep(meter.POLL_S * 2)  # no file yet: silence, not an ending
                self.assertTrue(reader.running)
                path.write_bytes(tap_file([(0.5, 0.25, 0.1, 0.05)]))
                self.assertTrue(arrived.wait(2))
            finally:
                reader.stop()
            self.assertAlmostEqual(got[0].peak_l, 0.5, places=6)
            self.assertFalse(reader.running)

    def test_it_ends_when_its_host_does(self):
        ended = threading.Event()
        with mock.patch("audiorouter.meter._pid_alive", return_value=False):
            reader = meter.FileLevelReader(meter.Tap("Out", (), 12345, "/nonexistent"),
                                           lambda _l: None, ended.set)
            reader.start()
            self.assertTrue(ended.wait(2))
            reader.stop()


@unittest.skipUnless(CAN_BUILD, "needs a C compiler")
class PluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        with mock.patch("audiorouter.lv2.reset_cache"), mock.patch("audiorouter.plugins.reset_cache"):
            cls.bundle = native.build(native.METER, cls.root / "lv2")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_its_description_is_found_by_our_catalogue_reader(self):
        plugin = lv2.read_bundle(self.bundle)[0]
        self.assertEqual(plugin.uri, native.METER_URI)

    def test_it_publishes_peak_and_mean_square_per_block_and_cleans_up(self):
        lib = ctypes.CDLL(str(self.bundle / "audiorouter_meter.so"))

        class Descriptor(ctypes.Structure):
            _fields_ = [
                ("uri", ctypes.c_char_p),
                ("instantiate", ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_double,
                                                 ctypes.c_char_p, ctypes.c_void_p)),
                ("connect_port", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)),
                ("activate", ctypes.c_void_p),
                ("run", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)),
                ("deactivate", ctypes.c_void_p),
                ("cleanup", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
                ("extension_data", ctypes.c_void_p),
            ]

        lib.lv2_descriptor.restype = ctypes.POINTER(Descriptor)
        d = lib.lv2_descriptor(0).contents
        self.assertEqual(d.uri.decode(), native.METER_URI)
        runtime = self.root / "run"
        runtime.mkdir()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            handle = d.instantiate(None, 48000.0, b"", None)
            slot = ctypes.c_float(3.0)
            d.connect_port(handle, 2, ctypes.addressof(slot))
            amplitude = 10 ** (-20 / 20)
            pos = 0
            for n in (100, 512, 1000, 300, 2184):  # odd block sizes: 4096 frames in all
                left = (ctypes.c_float * n)(*[amplitude * math.sin(2 * math.pi * 1000 * (pos + i) / 48000)
                                              for i in range(n)])
                right = (ctypes.c_float * n)(*[x * 0.5 for x in left])
                d.connect_port(handle, 0, ctypes.addressof(left))
                d.connect_port(handle, 1, ctypes.addressof(right))
                d.run(handle, n)
                pos += n
            path = runtime / "audiorouter" / "meters" / f"{os.getpid()}.3"
            seq, levels = meter.read_tap(path.read_bytes(), 0)
            d.cleanup(handle)
        self.assertEqual(seq, 4096 // 1024)
        last = levels[-1]
        self.assertAlmostEqual(meter.to_db(last.peak_l), -20.0, delta=0.05)
        self.assertAlmostEqual(meter.to_db(last.peak_r), -26.02, delta=0.05)
        self.assertAlmostEqual(10 * math.log10(last.ms_l), -23.01, delta=0.05)  # a sine: peak - 3 dB
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
