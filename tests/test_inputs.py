"""Input channels (processed microphones) and virtual-cable output channels."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter.channels import INPUT, NO_FALLBACK, NOWHERE, Channel, ChannelError
from audiorouter.config import Config, ConfigError
from audiorouter.engine import Engine, EngineError
from audiorouter.pwgraph import Graph

from . import fakes
from .test_engine import EngineTestCase, live_graph


def modules(channel):
    return {m["name"]: m["args"] for m in channel.render_config()["context.modules"]
            if m["name"] in ("libpipewire-module-filter-chain", "libpipewire-module-loopback")}


class InputRenderTest(unittest.TestCase):
    def test_an_input_channel_reads_the_mic_and_offers_a_virtual_microphone(self):
        mic = Channel("mic", "Desk mic", "alsa_input.usb", kind=INPUT)
        args = modules(mic)["libpipewire-module-filter-chain"]
        self.assertEqual(args["capture.props"]["target.object"], "alsa_input.usb")
        self.assertEqual(args["capture.props"]["node.name"], "ar_mic_in")
        self.assertEqual(args["playback.props"]["media.class"], "Audio/Source")
        self.assertEqual(args["playback.props"]["node.name"], "ar_mic")
        self.assertEqual(args["playback.props"]["node.description"], "Desk mic")

    def test_the_real_mic_is_only_opened_while_something_records(self):
        args = modules(Channel("mic", "Mic", "", kind=INPUT))["libpipewire-module-filter-chain"]
        self.assertTrue(args["capture.props"]["node.passive"])

    def test_a_chosen_mic_is_never_swapped_for_another_but_the_default_may_follow(self):
        chosen = modules(Channel("mic", "Mic", "alsa_input.usb", kind=INPUT))["libpipewire-module-filter-chain"]
        default = modules(Channel("mic", "Mic", "", kind=INPUT))["libpipewire-module-filter-chain"]
        self.assertTrue(chosen["capture.props"][NO_FALLBACK])
        self.assertNotIn(NO_FALLBACK, default["capture.props"])
        self.assertNotIn("target.object", default["capture.props"])

    def test_listening_adds_a_loopback_into_the_output_channel_that_never_falls_back(self):
        loop = modules(Channel("mic", "Mic", "", kind=INPUT, listen="speakers"))["libpipewire-module-loopback"]
        self.assertEqual(loop["capture.props"]["target.object"], "ar_mic")
        self.assertEqual(loop["playback.props"]["target.object"], "ar_speakers")
        # Measured without it: removing the output relinked the loopback onto
        # the real speakers - a live microphone on the speakers.
        self.assertTrue(loop["playback.props"][NO_FALLBACK])
        self.assertTrue(loop["capture.props"][NO_FALLBACK])

    def test_not_listening_means_no_loopback(self):
        self.assertNotIn("libpipewire-module-loopback", modules(Channel("mic", "Mic", "", kind=INPUT)))

    def test_every_node_is_stamped_as_ours(self):
        text = Channel("mic", "Mic", "", kind=INPUT, listen="speakers").render_config_text()
        conf = json.loads(text)
        for module in conf["context.modules"][4:]:
            for side in ("capture.props", "playback.props"):
                self.assertEqual(module["args"][side]["audiorouter.channel"], "mic")

    def test_controls_live_on_the_capture_side(self):
        self.assertEqual(Channel("mic", "Mic", "", kind=INPUT).control_name, "ar_mic_in")
        self.assertEqual(Channel("out", "Out", "dev").control_name, "ar_out")


class CableRenderTest(unittest.TestCase):
    def test_a_plain_output_is_unchanged(self):
        mods = modules(Channel("out", "Out", "alsa_output.a"))
        self.assertNotIn("libpipewire-module-loopback", mods)
        self.assertEqual(mods["libpipewire-module-filter-chain"]["playback.props"]["target.object"], "alsa_output.a")
        self.assertIsNone(Channel("out", "Out", "dev").recording_name)

    def test_a_recordable_output_offers_its_processed_sound_and_still_plays(self):
        mods = modules(Channel("game", "Game", "alsa_output.a", recordable=True))
        chain = mods["libpipewire-module-filter-chain"]
        self.assertEqual(chain["playback.props"]["media.class"], "Audio/Source")
        self.assertEqual(chain["playback.props"]["node.name"], "ar_game_rec")
        loop = mods["libpipewire-module-loopback"]
        self.assertEqual(loop["capture.props"]["target.object"], "ar_game_rec")
        self.assertEqual(loop["playback.props"]["target.object"], "alsa_output.a")
        self.assertEqual(loop["playback.props"]["node.name"], "ar_game_out")

    def test_a_cable_to_nowhere_is_only_recordable(self):
        channel = Channel("cable", "Cable", NOWHERE)
        self.assertTrue(channel.recordable)
        self.assertNotIn("libpipewire-module-loopback", modules(channel))


class ModelTest(unittest.TestCase):
    def test_round_trip(self):
        for channel in (Channel("mic", "Mic", "alsa_input.a", kind=INPUT, listen="speakers"),
                        Channel("game", "Game", "dev", recordable=True)):
            again = Channel.from_dict(json.loads(json.dumps(channel.to_dict())))
            self.assertEqual((again.kind, again.device, again.listen, again.recordable),
                             (channel.kind, channel.device, channel.listen, channel.recordable))

    def test_old_configs_load_as_outputs(self):
        channel = Channel.from_dict({"slug": "speakers", "name": "Speakers", "device": "dev"})
        self.assertEqual(channel.kind, "output")
        self.assertFalse(channel.recordable)

    def test_invalid_combinations_are_refused_or_normalised(self):
        with self.assertRaises(ChannelError):
            Channel("x", "X", "", kind="sideways")
        with self.assertRaises(ChannelError):
            Channel("mic", "Mic", NOWHERE, kind=INPUT)
        self.assertEqual(Channel("out", "Out", "dev", listen="other").listen, "")
        self.assertFalse(Channel("mic", "Mic", "", kind=INPUT, recordable=True).recordable)

    def test_problems_catch_listening_through_an_input_and_rules_to_inputs(self):
        from audiorouter.routing import Rule

        config = Config(channels=[
            Channel("mic", "Mic", "", kind=INPUT, listen="mic2"),
            Channel("mic2", "Mic 2", "", kind=INPUT),
        ])
        config.rules.rules.append(Rule("app", "Discord", "mic2"))
        problems = config.problems()
        self.assertTrue(any("listens through 'mic2'" in p for p in problems))
        self.assertTrue(any("sends playback to input channel" in p for p in problems))
        self.assertFalse(any("has no output device" in p for p in problems))


class EngineInputTest(EngineTestCase):
    def test_inputs_are_not_routing_destinations(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        graph = live_graph()
        graph.apply([fakes.node(70, "ar_mic", "Audio/Source", serial=700, **{"audiorouter.channel": "mic"})])
        self.assertNotIn("mic", self.engine.sink_map(graph))
        self.assertIn("speakers", self.engine.sink_map(graph))

    def test_listen_must_go_through_an_output(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        self.engine.set_listen("mic", "speakers")
        self.assertEqual(self.engine.channel("mic").listen, "speakers")
        self.engine.create_channel("mic2", "Mic 2", kind=INPUT)
        with self.assertRaises(EngineError):
            self.engine.set_listen("mic", "mic2")
        with self.assertRaises(EngineError):
            self.engine.set_listen("speakers", "mic")

    def test_deleting_the_output_stops_inputs_listening_through_it(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        self.engine.set_listen("mic", "speakers")
        with mock.patch.object(Channel, "stop"):
            self.engine.delete_channel("speakers")
        self.assertEqual(self.engine.channel("mic").listen, "")

    def test_a_cable_to_nowhere_cannot_be_made_unrecordable(self):
        self.engine.create_channel("cable", "Cable", device=NOWHERE)
        with self.assertRaises(EngineError):
            self.engine.set_recordable("cable", False)
        self.engine.set_recordable("speakers", True)
        self.assertTrue(self.engine.channel("speakers").recordable)

    def test_status_lists_input_devices_but_not_virtual_mics(self):
        graph = live_graph()
        graph.apply([
            fakes.node(71, "alsa_input.usb", "Audio/Source", description="USB Mic", **{"device.id": 90}),
            fakes.node(72, "ar_mic", "Audio/Source", **{"audiorouter.channel": "mic"}),
        ])
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)):
            status = self.engine.status()
        self.assertEqual(status["input_devices"], [{"name": "alsa_input.usb", "label": "USB Mic"}])


class HandOverTest(EngineTestCase):
    """What moves to the new host during a restart."""

    def graph_during_restart(self):
        old, new = 111, 222
        return Graph([
            fakes.client(1, old), fakes.client(2, new),
            # an output channel "speakers", old and new sinks
            fakes.sink(51, "ar_speakers", serial=510, hardware=False, client_id=1, **{"audiorouter.channel": "speakers"}),
            fakes.sink(52, "ar_speakers", serial=520, hardware=False, client_id=2, **{"audiorouter.channel": "speakers"}),
            # an app, and another channel's listen-through, both on the old sink
            fakes.stream(60, "firefox", serial=600),
            fakes.node(61, "ar_mic_listen", "Stream/Output/Audio", serial=610, **{"audiorouter.channel": "mic"}),
            fakes.port(80, 60, "out"), fakes.port(81, 61, "out"), fakes.port(82, 51, "in"),
            fakes.link(90, 80, 82), fakes.link(91, 81, 82),
        ])

    def test_apps_and_other_channels_streams_both_follow_an_output_restart(self):
        graph = self.graph_during_restart()
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata, \
             mock.patch("audiorouter.engine.time.sleep"):
            self.engine._hand_over(self.engine.channel("speakers"), 222, timeout=0)
        moved = sorted(c.args for c in metadata.call_args_list)
        self.assertEqual(moved, [(60, 520), (61, 520)])

    def test_recorders_of_an_input_channel_follow_its_restart(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        graph = Graph([
            fakes.client(1, 111), fakes.client(2, 222),
            fakes.node(70, "ar_mic", "Audio/Source", serial=700, client_id=1, **{"audiorouter.channel": "mic"}),
            fakes.node(71, "ar_mic", "Audio/Source", serial=710, client_id=2, **{"audiorouter.channel": "mic"}),
            fakes.node(75, "discord", "Stream/Input/Audio", serial=750, **{"application.name": "Discord"}),
            fakes.port(80, 70, "out"), fakes.port(81, 75, "in"), fakes.link(90, 80, 81),
        ])
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata, \
             mock.patch("audiorouter.engine.time.sleep"):
            self.engine._hand_over(self.engine.channel("mic"), 222, timeout=0)
        self.assertEqual([c.args for c in metadata.call_args_list], [(75, 710)])

    def test_the_new_hosts_own_loopback_is_pointed_at_its_own_source(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        self.engine.set_listen("mic", "speakers")
        graph = Graph([
            fakes.client(1, 111), fakes.client(2, 222),
            fakes.node(70, "ar_mic", "Audio/Source", serial=700, client_id=1, **{"audiorouter.channel": "mic"}),
            fakes.node(71, "ar_mic", "Audio/Source", serial=710, client_id=2, **{"audiorouter.channel": "mic"}),
            fakes.node(76, "ar_mic_listen_in", "Stream/Input/Audio", serial=760, client_id=2,
                       **{"audiorouter.channel": "mic", "target.object": "ar_mic"}),
        ])
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata, \
             mock.patch("audiorouter.engine.time.sleep"):
            self.engine._hand_over(self.engine.channel("mic"), 222, timeout=0)
        self.assertEqual([c.args for c in metadata.call_args_list], [(76, 710)])


if __name__ == "__main__":
    unittest.main()
