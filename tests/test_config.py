import json
import tempfile
import unittest
from pathlib import Path

from audiorouter.channels import INPUT, Channel
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

    def test_following_the_default_output_is_not_a_problem(self):
        self.config.channels.append(Channel("void", "Void", ""))
        self.assertFalse(any("void" in p for p in self.config.problems()))

    def test_problems_report_rules_pointing_nowhere(self):
        self.config.rules.rules.append(Rule("app", "mpv", "ghost"))
        self.assertTrue(any("ghost" in p for p in self.config.problems()))

    def test_a_healthy_config_has_no_problems(self):
        self.assertEqual(self.config.problems(), [])


class SoloTest(unittest.TestCase):
    def config(self):
        return Config(channels=[
            Channel("speakers", "Speakers", ""), Channel("phones", "Phones", ""),
            Channel("mic", "Mic", "", kind=INPUT), Channel("mic2", "Mic 2", "", kind=INPUT),
        ])

    def cut(self, config):
        return {c.slug for c in config.channels if c.solo_cut}

    def test_a_solo_cuts_only_the_rest_of_its_own_kind(self):
        config = self.config()
        self.assertEqual(self.cut(config), set())
        config.channel("phones").solo = True
        config.update_solo()
        self.assertEqual(self.cut(config), {"speakers"})  # the mics are untouched
        config.channel("mic").solo = True
        config.update_solo()
        self.assertEqual(self.cut(config), {"speakers", "mic2", "mic2_mix"})  # a mic's mix goes with it

    def test_two_solos_are_both_heard(self):
        config = self.config()
        for slug in ("speakers", "phones"):
            config.channel(slug).solo = True
        config.update_solo()
        self.assertEqual(self.cut(config), set())

    def test_a_switched_off_channel_soloed_cuts_nothing(self):
        config = self.config()
        config.channel("phones").solo = True
        config.channel("phones").enabled = False
        config.update_solo()
        self.assertEqual(self.cut(config), set())

    def test_the_cut_is_worked_out_on_load_and_never_saved(self):
        config = self.config()
        config.channel("phones").solo = True
        again = Config.from_dict(config.to_dict())
        self.assertEqual(self.cut(again), {"speakers"})
        self.assertNotIn("solo_cut", config.channel("speakers").to_dict())

    def test_removing_the_soloed_channel_releases_the_others(self):
        config = self.config()
        config.channel("phones").solo = True
        config.update_solo()
        config.remove_channel("phones")
        self.assertEqual(self.cut(config), set())


class GroupTest(unittest.TestCase):
    """A group is an output channel other output channels play into."""

    def config(self):
        return Config(channels=[
            Channel("music", "Music", "ar_master"), Channel("game", "Game", "ar_master"),
            Channel("master", "Master", "alsa_output.a"), Channel("phones", "Phones", ""),
            Channel("mic", "Mic", "", kind=INPUT),
        ])

    def slugs(self, channels):
        return [c.slug for c in channels]

    def test_members_and_groups_are_found_by_node_name(self):
        config = self.config()
        master = config.channel("master")
        self.assertIs(config.group_of(config.channel("music")), master)
        self.assertEqual(self.slugs(config.members_of(master)), ["music", "game"])
        self.assertTrue(config.is_group(master))
        self.assertFalse(config.is_group(config.channel("music")))
        # A real device whose name happens to start with ar_ is not a group.
        self.assertIsNone(config.group_of(Channel("x", "X", "ar_testdev")))

    def test_groups_start_before_their_members(self):
        config = self.config()
        config.channel("master").device = "ar_phones"  # a group of a group
        order = self.slugs(config.start_order())
        self.assertLess(order.index("phones"), order.index("master"))
        self.assertLess(order.index("master"), order.index("music"))

    def test_choices_leave_out_itself_inputs_and_loops(self):
        config = self.config()
        # A mic's mix is a choice too: that is how music reaches a mic.
        self.assertEqual(self.slugs(config.group_choices(config.channel("master"))), ["phones", "mic_mix"])
        self.assertEqual(config.group_choices(config.channel("mic")), [])

    def test_a_loop_is_reported(self):
        config = self.config()
        config.channel("master").device = "ar_music"
        self.assertTrue(config.in_loop(config.channel("music")))
        self.assertTrue(any("loop" in p for p in config.problems()))

    def test_deleting_a_group_sends_its_members_to_the_default_output(self):
        config = self.config()
        config.remove_channel("master")
        self.assertEqual([config.channel(s).device for s in ("music", "game")], ["", ""])

    def test_a_solo_never_cuts_the_group_it_plays_through(self):
        config = self.config()
        config.channel("music").solo = True
        config.update_solo()
        self.assertEqual({c.slug for c in config.channels if c.solo_cut}, {"game", "phones"})

    def test_soloing_a_group_keeps_its_members(self):
        config = self.config()
        config.channel("master").solo = True
        config.update_solo()
        self.assertEqual({c.slug for c in config.channels if c.solo_cut}, {"phones"})


class CompanionTest(unittest.TestCase):
    """Every input channel has a hidden companion output (its mix)."""

    def config(self):
        return Config(channels=[Channel("speakers", "Speakers", "dev"),
                                Channel("mic", "Desk mic", "", kind=INPUT)])

    def test_every_input_gets_one_right_after_it_and_it_is_saved(self):
        config = self.config()
        self.assertEqual([c.slug for c in config.channels], ["speakers", "mic", "mic_mix"])
        again = Config.from_dict(config.to_dict())
        self.assertEqual(again.channel("mic_mix").companion_of, "mic")
        self.assertEqual([c.slug for c in again.channels], ["speakers", "mic", "mic_mix"])  # not doubled

    def test_it_follows_its_mic_channels_name_switch_and_listen(self):
        config = self.config()
        mic = config.channel("mic")
        mic.name, mic.enabled, mic.listen = "Teams mic", False, "speakers"
        self.assertTrue(config.ensure_companions())
        companion = config.channel("mic_mix")
        self.assertEqual((companion.name, companion.enabled, companion.device),
                         ("Teams mic", False, "ar_speakers"))
        self.assertFalse(config.ensure_companions())  # nothing more to do

    def test_it_starts_before_its_mic_channel(self):
        order = [c.slug for c in self.config().start_order()]
        self.assertLess(order.index("mic_mix"), order.index("mic"))

    def test_it_goes_with_its_mic_channel_and_cannot_be_deleted_alone(self):
        config = self.config()
        with self.assertRaisesRegex(ConfigError, "belongs to its mic channel"):
            config.remove_channel("mic_mix")
        config.channel("speakers").device = "ar_mic_mix"  # music into the mic
        config.remove_channel("mic")
        self.assertEqual([c.slug for c in config.channels], ["speakers"])
        self.assertEqual(config.channel("speakers").device, "")

    def test_an_output_solo_never_cuts_a_mic(self):
        config = self.config()
        config.channel("speakers").solo = True
        config.update_solo()
        self.assertFalse(config.channel("mic_mix").solo_cut)

    def test_a_clashing_slug_gets_another_name(self):
        config = Config(channels=[Channel("mic_mix", "Something else", "dev"),
                                  Channel("mic", "Mic", "", kind=INPUT)])
        self.assertEqual(config.companion(config.channel("mic")).slug, "mic_c_mix")


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
