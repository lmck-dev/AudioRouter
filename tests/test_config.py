import json
import tempfile
import unittest
from pathlib import Path

from audiorouter.channels import Channel
from audiorouter.config import CONFIG_VERSION, Config, ConfigError, default_config
from audiorouter.effects import Effect
from audiorouter.routing import Rule, RuleSet


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.config = Config(
            channels=[
                Channel("speakers", "Speakers", "alsa_output.a",
                        effects=[Effect("lowpass", {"frequency": 800.0})]),
                Channel("headphones", "Headphones", "alsa_output.b"),
            ],
            rules=RuleSet([Rule("app", "firefox", "headphones")]),
        )

    def test_round_trip_through_a_file(self):
        self.config.save(self.path)
        again = Config.load(self.path)
        self.assertEqual(again.to_dict(), self.config.to_dict())

    def test_a_missing_file_is_an_empty_configuration(self):
        empty = Config.load(self.path)
        self.assertEqual(empty.channels, [])
        self.assertTrue(empty.auto_route)

    def test_saving_leaves_no_temporary_file_behind(self):
        self.config.save(self.path)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["config.json"])

    def test_a_newer_config_is_refused_rather_than_downgraded(self):
        self.path.write_text(json.dumps({"version": CONFIG_VERSION + 1}))
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_unreadable_json_names_the_file(self):
        self.path.write_text("{not json")
        with self.assertRaises(ConfigError) as caught:
            Config.load(self.path)
        self.assertIn(str(self.path), str(caught.exception))

    def test_an_invalid_channel_is_reported_as_a_config_error(self):
        self.path.write_text(json.dumps({"channels": [{"slug": "Not Valid"}]}))
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_looking_up_an_unknown_channel_lists_the_known_ones(self):
        with self.assertRaises(ConfigError) as caught:
            self.config.channel("nope")
        self.assertIn("speakers", str(caught.exception))

    def test_adding_a_duplicate_channel_is_refused(self):
        with self.assertRaises(ConfigError):
            self.config.add_channel(Channel("speakers", "Again", "alsa_output.c"))

    def test_removing_a_channel_takes_its_rules_with_it(self):
        # A rule pointing at a deleted channel would silently never fire.
        self.config.remove_channel("headphones")
        self.assertEqual(self.config.rules.rules, [])

    def test_problems_report_channels_with_no_output(self):
        self.config.channels.append(Channel("void", "Void", ""))
        self.assertIn("channel 'void' has no output device", self.config.problems())

    def test_problems_report_rules_pointing_nowhere(self):
        self.config.rules.rules.append(Rule("app", "mpv", "ghost"))
        self.assertTrue(any("ghost" in p for p in self.config.problems()))

    def test_a_healthy_config_has_no_problems(self):
        self.assertEqual(self.config.problems(), [])


class DefaultConfigTest(unittest.TestCase):
    def test_one_channel_per_device(self):
        config = default_config(["alsa_output.pci-0000_0f_00.6.analog-stereo", "bluez_output.x"])
        self.assertEqual(len(config.channels), 2)

    def test_slugs_are_unique_even_when_names_collide(self):
        config = default_config(["a" * 50 + "-one", "a" * 50 + "-two"])
        self.assertEqual(len(set(config.channel_slugs)), 2)

    def test_the_first_run_applies_no_effects(self):
        # A first run that changed how audio sounds is indistinguishable
        # from a bug, so every seeded channel is a pass-through.
        config = default_config(["alsa_output.a"])
        self.assertEqual(config.channels[0].effects, [])


if __name__ == "__main__":
    unittest.main()
