"""A channel playing to several places at once (owner, 16 Oct 2026)."""

import unittest

from audiorouter.channels import INPUT, NOWHERE, Channel
from audiorouter.config import Config

from .test_engine import EngineTestCase


def loopbacks(channel):
    """(capture target, playback target or None, playback name) per loopback."""
    found = []
    for module in channel.render_config()["context.modules"]:
        if module.get("name") == "libpipewire-module-loopback":
            args = module["args"]
            found.append((args["capture.props"].get("target.object"),
                          args["playback.props"].get("target.object"),
                          args["playback.props"]["node.name"]))
    return found


def chain_playback(channel):
    module = next(m for m in channel.render_config()["context.modules"]
                  if m.get("name") == "libpipewire-module-filter-chain")
    return module["args"]["playback.props"]


class ModelTest(unittest.TestCase):
    def test_targets_are_the_first_choice_then_the_others(self):
        channel = Channel("music", "Music", "alsa_output.a", also=["alsa_output.b"])
        self.assertEqual(channel.targets, ["alsa_output.a", "alsa_output.b"])

    def test_repeats_and_nowhere_are_dropped_from_the_others(self):
        channel = Channel("music", "Music", "alsa_output.a",
                          also=["alsa_output.a", "alsa_output.b", "alsa_output.b", NOWHERE])
        self.assertEqual(channel.also, ["alsa_output.b"])

    def test_choosing_none_plays_nowhere_and_stays_a_recording_source(self):
        channel = Channel("music", "Music", "alsa_output.a", also=["alsa_output.b"])
        channel.set_targets([])
        self.assertEqual((channel.device, channel.also, channel.recordable), (NOWHERE, [], True))

    def test_the_first_ticked_becomes_the_device(self):
        channel = Channel("music", "Music", NOWHERE, recordable=True)
        channel.set_targets(["alsa_output.b", "", "alsa_output.a"])
        self.assertEqual((channel.device, channel.also), ("alsa_output.b", ["", "alsa_output.a"]))

    def test_a_mic_listens_through_several_and_unticking_all_stops(self):
        mic = Channel("mic", "Mic", "", kind=INPUT)
        mic.set_targets(["speakers", "phones"])
        self.assertEqual((mic.listen, mic.also), ("speakers", ["phones"]))
        mic.set_targets([])
        self.assertEqual((mic.listen, mic.also), ("", []))

    def test_others_survive_a_save_and_old_files_load_without_them(self):
        channel = Channel("music", "Music", "alsa_output.a", also=["alsa_output.b"])
        self.assertEqual(Channel.from_dict(channel.to_dict()).also, ["alsa_output.b"])
        plain = Channel("music", "Music", "alsa_output.a")
        self.assertNotIn("also", plain.to_dict())
        self.assertEqual(Channel.from_dict(plain.to_dict()).also, [])


class RenderTest(unittest.TestCase):
    def test_one_place_is_rendered_exactly_as_before(self):
        channel = Channel("music", "Music", "alsa_output.a")
        self.assertEqual(loopbacks(channel), [])
        self.assertEqual(chain_playback(channel)["target.object"], "alsa_output.a")

    def test_several_places_get_one_loopback_each_from_an_internal_source(self):
        channel = Channel("music", "Music", "alsa_output.a", also=["alsa_output.b", ""])
        playback = chain_playback(channel)
        self.assertEqual(playback["node.name"], "ar_music_split")
        self.assertEqual(playback["media.class"], "Audio/Source")
        self.assertEqual(playback["priority.session"], 1)  # never the default mic
        self.assertEqual(loopbacks(channel), [
            ("ar_music_split", "alsa_output.a", "ar_music_out"),
            ("ar_music_split", "alsa_output.b", "ar_music_out2"),
            ("ar_music_split", None, "ar_music_out3"),   # "" = the default output
        ])

    def test_a_recording_source_feeds_the_places_itself(self):
        channel = Channel("music", "Music", "alsa_output.a", also=["alsa_output.b"], recordable=True)
        self.assertEqual(chain_playback(channel)["node.name"], "ar_music_rec")
        self.assertEqual([lb[:2] for lb in loopbacks(channel)],
                         [("ar_music_rec", "alsa_output.a"), ("ar_music_rec", "alsa_output.b")])

    def test_every_node_name_is_unique(self):
        channel = Channel("music", "Music", "alsa_output.a", also=["alsa_output.b", "alsa_output.c"])
        names = [m["args"][side]["node.name"]
                 for m in channel.render_config()["context.modules"] if "args" in m
                 for side in ("capture.props", "playback.props") if side in m["args"]]
        self.assertEqual(len(names), len(set(names)))


class GroupsTest(unittest.TestCase):
    def setUp(self):
        self.config = Config(channels=[
            Channel("bus_a", "Bus A", "alsa_output.a"),
            Channel("bus_b", "Bus B", "alsa_output.b"),
            Channel("music", "Music", "ar_bus_a", also=["ar_bus_b"]),
        ])
        self.a, self.b, self.music = self.config.channels

    def test_a_channel_is_a_member_of_every_group_it_plays_into(self):
        self.assertEqual(self.config.groups_of(self.music), [self.a, self.b])
        self.assertEqual(self.config.members_of(self.b), [self.music])

    def test_groups_start_before_a_channel_playing_into_several(self):
        order = self.config.start_order()
        self.assertLess(order.index(self.b), order.index(self.music))
        self.assertLess(order.index(self.a), order.index(self.music))

    def test_a_loop_through_a_second_output_is_found_and_refused(self):
        self.assertNotIn(self.music, self.config.group_choices(self.b))
        self.b.also = ["ar_music"]
        self.assertTrue(self.config.in_loop(self.music))

    def test_deleting_a_group_drops_it_from_the_others(self):
        self.config.remove_channel("bus_b")
        self.assertEqual(self.music.also, [])


class MicTest(unittest.TestCase):
    def test_the_mix_plays_to_every_output_the_mic_is_listened_through(self):
        config = Config(channels=[
            Channel("speakers", "Speakers", "alsa_output.a"),
            Channel("phones", "Phones", "alsa_output.b"),
            Channel("mic", "Mic", "", kind=INPUT, listen="speakers", also=["phones"]),
        ])
        config.ensure_companions()
        mix = config.channel("mic_mix")
        self.assertEqual(mix.targets, ["ar_speakers", "ar_phones"])
        self.assertEqual([lb[1] for lb in loopbacks(mix)], ["ar_speakers", "ar_phones"])
        self.assertEqual(config.listen_ends(config.channel("mic")), ["alsa_output.a", "alsa_output.b"])


class EngineTest(EngineTestCase):
    def test_set_outputs_saves_every_place(self):
        self.engine.create_channel("music", "Music", "")
        self.engine.set_outputs("music", ["alsa_output.a", "ar_speakers"])
        saved = Config.load(self.path).channel("music")
        self.assertEqual(saved.targets, ["alsa_output.a", "ar_speakers"])

    def test_set_outputs_refuses_a_loop(self):
        self.engine.create_channel("music", "Music", "")
        self.engine.set_outputs("music", ["ar_speakers"])
        with self.assertRaisesRegex(Exception, "already plays into"):
            self.engine.set_outputs("speakers", ["alsa_output.a", "ar_music"])

    def test_deleting_a_listened_output_moves_the_next_one_up(self):
        self.engine.create_channel("phones", "Phones", "alsa_output.a")
        self.engine.create_channel("mic", "Mic", "", kind="input")
        self.engine.set_outputs("mic", ["speakers", "phones"])
        self.engine.delete_channel("speakers")
        mic = self.engine.config.channel("mic")
        self.assertEqual((mic.listen, mic.also), ("phones", []))


if __name__ == "__main__":
    unittest.main()
