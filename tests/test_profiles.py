import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter.channels import NOWHERE, Channel
from audiorouter.config import Config, ConfigError
from audiorouter.engine import Engine
from audiorouter.pwgraph import Graph
from audiorouter.profiles import ProfileError

from . import test_cli
from .test_engine import live_graph


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        snapshot = mock.patch.object(Graph, "snapshot", staticmethod(live_graph))
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.path = Path(self.tmp.name) / "config.json"
        config = Config(plugin_folders=["/opt/lv2"])
        config.add_channel(Channel(slug="speakers", name="Speakers", device="alsa_output.a"))
        config.add_channel(Channel(slug="game", name="Game", device="alsa_output.a"))
        self.engine = Engine(config, path=self.path)
        self.engine.add_effect("game", "gain", {"gain_db": -6})
        self.engine.add_rule("app", "firefox", "speakers")

    def test_save_then_switch_restores_the_desk(self):
        self.engine.save_profile("Gaming")
        self.engine.delete_channel("game")
        self.engine.create_channel("music", "Music", "alsa_output.a")
        self.assertTrue(self.engine.profile_modified())

        missing = self.engine.switch_profile("gaming")   # names are not case-sensitive

        config = Config.load(self.path)
        self.assertEqual(config.channel_slugs, ["speakers", "game"])
        self.assertEqual(config.channel("game").effects[0].params["gain_db"], -6)
        self.assertEqual([r.pattern for r in config.rules.rules], ["firefox"])
        self.assertEqual(config.profile, "Gaming")
        self.assertEqual(missing, [])
        self.assertFalse(self.engine.profile_modified())

    def test_switch_keeps_this_computers_settings(self):
        self.engine.save_profile("Calm")
        self.engine.config.plugin_folders = ["/home/me/lv2"]
        self.engine.config.bypass = True
        self.engine.switch_profile("Calm")
        self.assertEqual(self.engine.config.plugin_folders, ["/home/me/lv2"])
        self.assertTrue(self.engine.config.bypass)
        saved = json.loads(self.engine.profile_store.path_for("Calm").read_text())
        self.assertNotIn("plugin_folders", saved)
        self.assertNotIn("bypass", saved)

    def test_unplugged_device_is_reported_not_refused(self):
        self.engine.set_device("game", "bluez_output.headset")
        self.engine.set_device("speakers", NOWHERE)
        self.engine.save_profile("Headset")
        self.assertEqual(self.engine.switch_profile("Headset"), ["bluez_output.headset"])
        self.assertEqual(self.engine.config.channel("game").device, "bluez_output.headset")

    def test_list_rename_delete(self):
        self.engine.save_profile("Recording")
        self.engine.save_profile("Gaming")
        self.assertEqual(self.engine.profile_store.names(), ["Gaming", "Recording"])
        self.engine.rename_profile("Gaming", "Games")
        self.assertEqual(self.engine.config.profile, "Games")
        self.assertEqual(self.engine.profile_store.names(), ["Games", "Recording"])
        self.engine.delete_profile("games")
        self.assertEqual(self.engine.config.profile, "")
        self.assertEqual(self.engine.profile_store.names(), ["Recording"])

    def test_unknown_or_bad_names(self):
        with self.assertRaises(ProfileError):
            self.engine.switch_profile("Nope")
        with self.assertRaises(ProfileError):
            self.engine.save_profile("   ")
        self.assertTrue(issubclass(ProfileError, ConfigError))   # CLI and GUI catch it

    def test_name_cannot_escape_the_folder(self):
        path = self.engine.profile_store.path_for("../../etc/passwd")
        self.assertEqual(path.parent, self.path.parent / "profiles")

    def test_chain_loads_onto_another_channel(self):
        self.engine.config.channel("game").fader_db = -3.0
        self.engine.save_chain("game", "Quiet game")
        self.engine.load_chain("speakers", "quiet game")
        speakers = Config.load(self.path).channel("speakers")
        self.assertEqual([e.kind for e in speakers.effects], ["gain"])
        self.assertEqual(speakers.fader_db, -3.0)
        self.assertEqual(self.engine.chain_store.names(), ["Quiet game"])

    def test_old_config_without_profile_loads(self):
        data = self.engine.config.to_dict()
        del data["profile"]
        self.assertEqual(Config.from_dict(data).profile, "")


class ProfileCliTest(unittest.TestCase):
    setUp = test_cli.CliTest.setUp
    run_cli = test_cli.CliTest.run_cli

    def test_save_list_load(self):
        self.run_cli("channel", "add", "speakers", "--name", "Speakers", "--device", "alsa_output.a")
        code, out, _ = self.run_cli("profile", "save", "Gaming")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("profile", "list")
        self.assertIn("* Gaming", out)
        code, out, _ = self.run_cli("--dry-run", "profile", "load", "Gaming")
        self.assertEqual(code, 0, out)
        self.assertIn("switched to profile 'Gaming'", out)
        code, _, err = self.run_cli("profile", "load", "Missing")
        self.assertEqual(code, 2)
        self.assertIn("nothing saved", err)

    def test_chains(self):
        self.run_cli("channel", "add", "speakers", "--name", "Speakers", "--device", "alsa_output.a")
        self.run_cli("effect", "add", "speakers", "gain", "gain_db=-6")
        self.assertEqual(self.run_cli("chain", "save", "speakers", "Voice")[0], 0)
        self.assertIn("Voice", self.run_cli("chain", "list")[1])
        self.assertEqual(self.run_cli("chain", "rm", "Voice")[0], 0)


if __name__ == "__main__":
    unittest.main()
