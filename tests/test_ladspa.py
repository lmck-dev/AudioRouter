import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import effects, ladspa, lv2, plugins
from audiorouter.config import Config
from audiorouter.engine import Engine, EngineError

AMP = Path("/usr/lib64/ladspa/amp.so")


def amp_plugin(path="/plugins/amp.so"):
    return lv2.Plugin.from_dict({
        "uri": ladspa.uri(path, "amp_mono"), "name": "Mono Amplifier", "bundle": path,
        "maker": "Richard Furse (LADSPA example plugins)",
        "audio_in": ["Input"], "audio_out": ["Output"],
        "controls": [{"symbol": "Gain", "name": "Gain", "index": 0,
                      "minimum": 0.0, "maximum": 2.0, "default": 1.0}],
    })


class DescriptionTest(unittest.TestCase):
    def test_a_uri_names_the_file_and_the_label(self):
        made = ladspa.uri("/a/b#c/amp.so", "amp_mono")
        self.assertTrue(ladspa.is_ladspa(made))
        self.assertEqual(ladspa.split(made), ("/a/b#c/amp.so", "amp_mono"))

    def test_defaults_follow_the_ladspa_table(self):
        self.assertEqual(ladspa.default_value(0x40, 2, 8), 2)      # minimum
        self.assertEqual(ladspa.default_value(0xC0, 2, 8), 5)      # middle
        self.assertAlmostEqual(ladspa.default_value(0xC0 | 0x10, 1, 100), 10)  # log middle
        self.assertEqual(ladspa.default_value(0x2C0, 0, 1000), 440)

    def test_a_sample_rate_bound_is_read_at_48_khz(self):
        control = ladspa._control("Cutoff", 0, 0x1 | 0x2 | 0x8, 0.0, 0.5)
        self.assertEqual(control["maximum"], 24000.0)

    def test_an_unbounded_control_still_gets_a_range(self):
        control = ladspa._control("Drive", 0, 0x240, 0.0, 0.0)
        self.assertGreater(control["maximum"], control["minimum"])
        self.assertEqual(control["default"], 1.0)

    @unittest.skipUnless(AMP.is_file(), "the LADSPA example plugins are not installed")
    def test_a_real_file_is_read_in_a_child_process(self):
        found, unreadable = ladspa.read_files([AMP])
        names = {p["name"]: p for p in found}
        self.assertEqual(unreadable, {})
        self.assertEqual(names["Mono Amplifier"]["audio_in"], ["Input"])
        self.assertEqual(names["Stereo Amplifier"]["problems"], [])

    def test_a_file_that_crashes_the_reader_is_skipped_alone(self):
        def probe(files, timeout):
            if len(files) > 1 or files[0] == "bad.so":
                return None
            return {files[0]: [{"uri": ladspa.uri(files[0], "x")}]}

        with mock.patch.object(ladspa, "_probe", side_effect=probe):
            found, unreadable = ladspa.read_files([Path("good.so"), Path("bad.so")])
        self.assertEqual([p["uri"] for p in found], [ladspa.uri("good.so", "x")])
        self.assertIn("bad.so", unreadable)


class FolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "daw/lv2/thing.lv2").mkdir(parents=True)
        (self.root / "daw/lv2/thing.lv2/thing.so").write_bytes(b"")
        (self.root / "daw/ladspa/deep").mkdir(parents=True)
        (self.root / "daw/ladspa/deep/amp.so").write_bytes(b"")
        self.addCleanup(plugins.set_extra_folders, [])

    def test_bundles_and_ladspa_files_are_found_at_any_depth(self):
        plugins.set_extra_folders([str(self.root / "daw")])
        self.assertEqual(plugins.extra_lv2_roots(), (self.root / "daw/lv2",))
        with mock.patch.dict(os.environ, {"LADSPA_PATH": ""}):
            files = plugins.ladspa_files()
        # A .so inside an LV2 bundle is that bundle's code, not a LADSPA plugin.
        self.assertEqual(files, [self.root / "daw/ladspa/deep/amp.so"])

    def test_a_copy_of_a_plugin_in_another_folder_is_listed_once(self):
        first = {**amp_plugin("/usr/lib64/ladspa/amp.so").to_dict()}
        copy = {**amp_plugin("/home/me/daw/amp.so").to_dict()}
        with mock.patch.object(ladspa, "read_files", return_value=([first, copy], {})):
            catalogue = lv2.build([], [Path("x.so")])
        self.assertEqual(list(catalogue.plugins), [first["uri"]])

    def test_channel_hosts_are_told_where_the_lv2_folders_are(self):
        self.assertIsNone(lv2.host_lv2_path())
        plugins.set_extra_folders([str(self.root / "daw")])
        self.assertTrue(lv2.host_lv2_path().endswith(str(self.root / "daw/lv2")))

    def test_the_folders_are_saved_and_a_missing_one_refused(self):
        path = self.root / "config.json"
        engine = Engine(Config(), path=path)
        engine.set_plugin_folders([str(self.root / "daw"), str(self.root / "daw")])
        self.assertEqual(json.loads(path.read_text())["plugin_folders"], [str(self.root / "daw")])
        self.assertEqual(Config.load(path).plugin_folders, [str(self.root / "daw")])
        with self.assertRaises(EngineError):
            engine.set_plugin_folders([str(self.root / "nowhere")])


class RenderTest(unittest.TestCase):
    def test_a_ladspa_effect_renders_as_a_ladspa_node_with_its_full_path(self):
        catalogue = lv2.Catalogue(plugins={amp_plugin().uri: amp_plugin()})
        with mock.patch.object(lv2, "catalogue", return_value=catalogue):
            effect = effects.make_effect("lv2", {"Gain": 0.5}, plugin=amp_plugin().uri)
            fragment = effect.render(0)
            spec = effects.plugin_spec(amp_plugin().uri)
        node = fragment.nodes[0]
        self.assertEqual((node["type"], node["plugin"], node["label"]),
                         ("ladspa", "/plugins/amp.so", "amp_mono"))
        self.assertEqual(node["control"], {"Gain": 0.5})
        self.assertEqual(len(fragment.nodes), 2)  # mono: once per side
        self.assertIn("LADSPA", spec.summary)
        self.assertEqual(spec.group, "Richard Furse")
