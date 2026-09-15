"""The noise suppression plugin we build ourselves.

The description is read with our own catalogue reader (no compiler needed).
The build and the processing tests run only where a compiler and librnnoise
are present; the processing test loads the plugin straight through its LV2
entry point, so it proves the frame buffering without PipeWire.
"""

import ctypes
import math
import os
import random
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import lv2, native

SOURCE = Path(native.__file__).resolve().parent / "rnnoise"
CAN_BUILD = not native.missing_for_rnnoise()


class DescriptionTest(unittest.TestCase):
    def setUp(self):
        self.plugin = lv2.read_bundle(SOURCE)[0]

    def test_it_is_a_usable_mono_effect_with_ordinary_controls(self):
        p = self.plugin
        self.assertEqual(p.uri, native.RNNOISE_URI)
        self.assertTrue(p.usable, p.problems)
        self.assertEqual((p.audio_in, p.audio_out), (("in",), ("out",)))
        self.assertEqual(p.enable_port, "enabled")
        visible = [c.symbol for c in p.controls if not c.hidden]
        self.assertEqual(visible, ["threshold", "hold", "mix"])

    def test_the_gate_is_off_and_the_mix_full_by_default(self):
        self.assertEqual(self.plugin.control("threshold").default, 0.0)
        self.assertEqual(self.plugin.control("mix").default, 100.0)


class KnownBrokenTest(unittest.TestCase):
    def test_the_werman_plugins_are_listed_as_unusable_with_the_replacement(self):
        for uri in lv2.KNOWN_BROKEN:
            self.assertIn("Voice noise suppression", lv2.KNOWN_BROKEN[uri])
        self.assertEqual(len(lv2.KNOWN_BROKEN), 2)


class EnsureWithoutToolsTest(unittest.TestCase):
    def test_nothing_is_built_and_nothing_raises_without_a_compiler(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("audiorouter.native.compiler", return_value=None):
            self.assertIsNone(native.ensure_rnnoise(Path(tmp)))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_a_packaged_plugin_is_used_instead_of_building_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = Path(tmp) / "usr-lv2"
            (system / native.RNNOISE_BUNDLE).mkdir(parents=True)
            (system / native.RNNOISE_BUNDLE / "audiorouter_rnnoise.so").write_bytes(b"")
            calls = []
            with mock.patch.object(native, "_SYSTEM_LV2_DIRS", (system,)), \
                    mock.patch.object(native, "user_lv2_dir", return_value=Path(tmp) / "home"):
                found = native.ensure_rnnoise(run=lambda args: calls.append(args))
            self.assertEqual(found, system / native.RNNOISE_BUNDLE)
            self.assertEqual(calls, [])

    def test_a_failed_compile_is_not_fatal(self):
        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(native.ensure_rnnoise(Path(tmp), run=lambda args: failed))
            self.assertFalse(native.rnnoise_bundle(Path(tmp)).exists())


@unittest.skipUnless(CAN_BUILD, "needs a C compiler and librnnoise")
class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bundle = native.build_rnnoise(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_bundle_is_complete_and_up_to_date(self):
        for name in ("audiorouter_rnnoise.so", "manifest.ttl", "rnnoise.ttl"):
            self.assertTrue((self.bundle / name).is_file(), name)
        self.assertTrue(native.rnnoise_up_to_date(self.root))

    def test_ensure_does_not_rebuild_an_up_to_date_bundle(self):
        calls = []
        native.ensure_rnnoise(self.root, run=lambda args: calls.append(args))
        self.assertEqual(calls, [])

    def test_a_newer_source_makes_it_stale(self):
        so = self.bundle / "audiorouter_rnnoise.so"
        # Older than the source itself, not merely older than now: the source's
        # mtime is whenever it was last edited.
        source = native._SOURCE / native._C_FILE
        old = source.stat().st_mtime_ns - 10**9
        os.utime(so, ns=(old, old))
        try:
            self.assertFalse(native.rnnoise_up_to_date(self.root))
        finally:
            os.utime(so)

    def _instance(self):
        lib = ctypes.CDLL(str(self.bundle / "audiorouter_rnnoise.so"))

        class Descriptor(ctypes.Structure):
            _fields_ = [
                ("uri", ctypes.c_char_p),
                ("instantiate", ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_double,
                                                 ctypes.c_char_p, ctypes.c_void_p)),
                ("connect_port", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)),
                ("activate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
                ("run", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)),
                ("deactivate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
                ("cleanup", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
                ("extension_data", ctypes.c_void_p),
            ]

        lib.lv2_descriptor.restype = ctypes.POINTER(Descriptor)
        d = lib.lv2_descriptor(0).contents
        self.assertEqual(d.uri.decode(), native.RNNOISE_URI)
        self.assertFalse(lib.lv2_descriptor(1))
        return d

    def _process(self, signal, blocks, threshold=0.0, mix=100.0):
        d = self._instance()
        handle = d.instantiate(None, 48000.0, str(self.bundle).encode(), None)
        self.assertTrue(handle)
        controls = {2: ctypes.c_float(threshold), 3: ctypes.c_float(200.0),
                    4: ctypes.c_float(mix), 5: ctypes.c_float(1.0), 6: ctypes.c_float(0.0)}
        for port, value in controls.items():
            d.connect_port(handle, port, ctypes.addressof(value))
        d.activate(handle)
        out = []
        pos = 0
        i = 0
        while pos < len(signal):
            n = min(blocks[i % len(blocks)], len(signal) - pos)
            buf_in = (ctypes.c_float * n)(*signal[pos:pos + n])
            buf_out = (ctypes.c_float * n)()
            d.connect_port(handle, 0, ctypes.addressof(buf_in))
            d.connect_port(handle, 1, ctypes.addressof(buf_out))
            d.run(handle, n)
            out.extend(buf_out)
            pos += n
            i += 1
        latency = controls[6].value
        d.cleanup(handle)
        return out, latency

    def test_a_dry_mix_is_the_input_delayed_by_the_reported_latency_at_any_block_size(self):
        signal = [math.sin(i * 0.05) * 0.5 for i in range(9600)]
        for blocks in ([1024], [256], [480], [1000, 37, 2048]):
            out, latency = self._process(signal, blocks, mix=0.0)
            self.assertEqual(latency, 1440.0)
            lag = int(latency)
            worst = max(abs(out[i] - signal[i - lag]) for i in range(lag, len(signal)))
            self.assertLess(worst, 1e-6, blocks)
            self.assertTrue(all(v == 0.0 for v in out[:lag]))

    def test_block_size_does_not_change_the_processed_sound(self):
        signal = [math.sin(i * 0.03) * 0.3 + math.sin(i * 0.31) * 0.1 for i in range(14400)]
        reference, _ = self._process(signal, [480])
        for blocks in ([1024], [1000, 37, 2048]):
            out, _ = self._process(signal, blocks)
            self.assertLess(max(abs(a - b) for a, b in zip(out, reference)), 1e-6, blocks)

    def test_the_voice_gate_silences_what_is_not_speech(self):
        # A steady tone can score as voice; noise does not.
        rng = random.Random(1)
        signal = [rng.uniform(-0.1, 0.1) for _ in range(48000)]
        out, _ = self._process(signal, [1024], threshold=60.0)
        tail = out[-4800:]
        self.assertEqual(max(abs(v) for v in tail), 0.0)


if __name__ == "__main__":
    unittest.main()
