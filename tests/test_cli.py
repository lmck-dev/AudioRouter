import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from audiorouter.cli import main
from audiorouter.config import Config
from audiorouter.pwgraph import Graph

from .test_engine import live_graph


class CliTest(unittest.TestCase):
    """The CLI must not think for itself; these check the plumbing only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.tmp.name) / "config.json"
        snapshot = mock.patch.object(Graph, "snapshot", staticmethod(live_graph))
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def config(self):
        return Config.load(self.path)

    def test_devices_lists_hardware_only(self):
        code, out, _ = self.run_cli("devices")
        self.assertEqual(code, 0)
        self.assertIn("alsa_output.a", out)
        self.assertNotIn("ar_speakers", out)

    def test_adding_a_channel_writes_the_config(self):
        self.run_cli("channel", "add", "phones", "--name", "Phones", "--device", "alsa_output.a")
        self.assertEqual(self.config().channel("phones").device, "alsa_output.a")

    def test_an_invalid_channel_name_fails_without_a_traceback(self):
        code, _, err = self.run_cli("channel", "add", "Not Valid")
        self.assertEqual(code, 2)
        self.assertIn("invalid channel id", err)

    def test_effect_settings_are_parsed_from_key_value_pairs(self):
        self.run_cli("channel", "add", "phones")
        self.run_cli("effect", "add", "phones", "lowpass", "frequency=800", "poles=2")
        effect = self.config().channel("phones").effects[0]
        self.assertEqual(effect.params["frequency"], 800.0)

    def test_a_bad_setting_is_a_clean_error(self):
        self.run_cli("channel", "add", "phones")
        code, _, err = self.run_cli("effect", "add", "phones", "lowpass", "frequency")
        self.assertEqual(code, 2)
        self.assertIn("key=value", err)

    def test_status_as_json_is_machine_readable(self):
        self.run_cli("channel", "add", "speakers", "--device", "alsa_output.a")
        code, out, _ = self.run_cli("status", "--json")
        data = json.loads(out)
        self.assertEqual([c["slug"] for c in data["channels"]], ["speakers"])

    def test_status_mentions_a_rule_that_points_nowhere(self):
        self.path.write_text(json.dumps(
            {"version": 1, "channels": [], "rules": [{"field": "app", "pattern": "x",
                                                      "channel": "ghost"}]}
        ))
        _, out, _ = self.run_cli("status")
        self.assertIn("ghost", out)

    def test_dry_run_reports_without_starting_anything(self):
        self.run_cli("channel", "add", "speakers", "--device", "alsa_output.a")
        with mock.patch("audiorouter.channels.Channel.start") as start:
            code, out, _ = self.run_cli("--dry-run", "apply")
        start.assert_not_called()
        self.assertIn("dry run", out)

    def test_init_seeds_one_channel_per_device(self):
        code, out, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.config().channels), 1)

    def test_effects_catalogue_marks_what_this_machine_cannot_run(self):
        with mock.patch("audiorouter.plugins.available_loaders",
                        return_value=frozenset({"builtin"})):
            _, out, _ = self.run_cli("effects")
        self.assertIn("[unavailable]", out)
        self.assertIn("lowpass", out)


    def test_a_plugin_effect_is_added_by_uri_and_listed_by_name(self):
        from .test_plugin_effects import STEREO, fake_catalogue

        self.run_cli("channel", "add", "phones")
        with mock.patch("audiorouter.lv2.catalogue", side_effect=fake_catalogue):
            code, out, err = self.run_cli("effect", "add", "phones", "lv2", "--plugin", STEREO, "al=0.1")
            self.assertEqual(code, 0, err)
            self.assertIn("Example Compressor", out)
            effect = self.config().channel("phones").effects[0]
            self.assertEqual((effect.plugin, effect.params["al"]), (STEREO, 0.1))
            _, listing, _ = self.run_cli("effects", "--plugins", "compressor")
            self.assertIn(STEREO, listing)
            self.assertNotIn("Synth", listing)
            _, detail, _ = self.run_cli("effects", "--plugin", STEREO)
            self.assertIn("0=Down", detail)
            self.assertNotIn("igv", detail)

    def test_plugin_is_refused_for_a_curated_effect(self):
        self.run_cli("channel", "add", "phones")
        code, _, err = self.run_cli("effect", "add", "phones", "gain", "--plugin", "urn:x")
        self.assertNotEqual(code, 0)
        self.assertIn("lv2", err)


if __name__ == "__main__":
    unittest.main()


class InputCliTest(unittest.TestCase):
    # The fixture, not the tests: subclassing CliTest would run them all twice.
    setUp = CliTest.setUp
    run_cli = CliTest.run_cli
    config = CliTest.config

    def test_an_input_channel_listening_through_an_output(self):
        self.run_cli("channel", "add", "speakers", "--device", "alsa_output.a")
        code, out, err = self.run_cli("channel", "add", "mic", "--input", "--listen", "speakers")
        self.assertEqual(code, 0, err)
        self.assertIn("apps can record it as: ar_mic", out)
        mic = self.config().channel("mic")
        self.assertEqual((mic.kind, mic.listen), ("input", "speakers"))
        self.run_cli("channel", "set", "mic", "--listen", "off")
        self.assertEqual(self.config().channel("mic").listen, "")

    def test_echo_cancel_on_and_off(self):
        code, out, err = self.run_cli("channel", "add", "mic", "--input", "--echo-cancel")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.config().channel("mic").echo_cancel)
        code, out, err = self.run_cli("channel", "set", "mic", "--echo-cancel", "off")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.config().channel("mic").echo_cancel)
        self.run_cli("channel", "set", "mic", "--echo-cancel", "on")
        self.assertTrue(self.config().channel("mic").echo_cancel)
        self.assertIn("echo cancelled", self.run_cli("channel", "set", "mic")[1])

    def test_a_cable_to_nowhere(self):
        code, out, err = self.run_cli("channel", "add", "stream", "--device", "nowhere")
        self.assertEqual(code, 0, err)
        cable = self.config().channel("stream")
        self.assertTrue(cable.recordable)
        self.assertIn("ar_stream_rec", out)
