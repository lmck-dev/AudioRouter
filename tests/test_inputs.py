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
    def test_an_input_channel_reads_the_mic_and_plays_into_its_companion(self):
        mic = Channel("mic", "Desk mic", "alsa_input.usb", kind=INPUT)
        args = modules(mic)["libpipewire-module-filter-chain"]
        self.assertEqual(args["capture.props"]["target.object"], "alsa_input.usb")
        self.assertEqual(args["capture.props"]["node.name"], "ar_mic_in")
        playback = args["playback.props"]
        self.assertNotIn("media.class", playback)  # a stream now, not the virtual mic
        self.assertEqual(playback["target.object"], "ar_mic_mix")
        # Passive, or the mic would run with nothing recording (measured);
        # never the speakers if the companion is missing.
        self.assertTrue(playback["node.passive"])
        self.assertTrue(playback[NO_FALLBACK])

    def test_the_companion_offers_the_virtual_mic_under_the_old_name(self):
        config = Config(channels=[Channel("mic", "Desk mic", "alsa_input.usb", kind=INPUT)])
        companion = config.companion(config.channel("mic"))
        self.assertEqual((companion.slug, companion.device, companion.recordable), ("mic_mix", NOWHERE, True))
        args = modules(companion)["libpipewire-module-filter-chain"]
        self.assertEqual(args["playback.props"]["media.class"], "Audio/Source")
        self.assertEqual(args["playback.props"]["node.name"], "ar_mic")  # what apps already picked
        self.assertEqual(args["playback.props"]["node.description"], "Desk mic")
        self.assertEqual(args["capture.props"]["node.name"], "ar_mic_mix")

    def test_the_real_mic_is_only_opened_while_something_records(self):
        args = modules(Channel("mic", "Mic", "", kind=INPUT))["libpipewire-module-filter-chain"]
        self.assertTrue(args["capture.props"]["node.passive"])

    def test_a_chosen_mic_is_never_swapped_for_another_but_the_default_may_follow(self):
        chosen = modules(Channel("mic", "Mic", "alsa_input.usb", kind=INPUT))["libpipewire-module-filter-chain"]
        default = modules(Channel("mic", "Mic", "", kind=INPUT))["libpipewire-module-filter-chain"]
        self.assertTrue(chosen["capture.props"][NO_FALLBACK])
        self.assertNotIn(NO_FALLBACK, default["capture.props"])
        self.assertNotIn("target.object", default["capture.props"])

    def test_listening_is_the_companion_playing_into_the_output_that_never_falls_back(self):
        config = Config(channels=[Channel("speakers", "Speakers", "dev"),
                                  Channel("mic", "Mic", "", kind=INPUT, listen="speakers")])
        companion = config.companion(config.channel("mic"))
        self.assertEqual(companion.device, "ar_speakers")
        loop = modules(companion)["libpipewire-module-loopback"]
        self.assertEqual(loop["capture.props"]["target.object"], "ar_mic")
        self.assertEqual(loop["playback.props"]["target.object"], "ar_speakers")
        # Measured without it: removing the output relinked the loopback onto
        # the real speakers - a live microphone on the speakers.
        self.assertTrue(loop["playback.props"][NO_FALLBACK])
        self.assertTrue(loop["capture.props"][NO_FALLBACK])

    def test_not_listening_means_the_companion_plays_nowhere(self):
        config = Config(channels=[Channel("mic", "Mic", "", kind=INPUT)])
        companion = config.companion(config.channel("mic"))
        self.assertEqual(companion.device, NOWHERE)
        self.assertNotIn("libpipewire-module-loopback", modules(companion))
        self.assertNotIn("libpipewire-module-loopback", modules(config.channel("mic")))

    def test_every_node_is_stamped_as_ours(self):
        config = Config(channels=[Channel("speakers", "Speakers", "dev"),
                                  Channel("mic", "Mic", "", kind=INPUT, listen="speakers")])
        text = config.channel("mic").render_config_text()
        conf = json.loads(text)
        for module in conf["context.modules"][4:]:
            for side in ("capture.props", "playback.props"):
                self.assertEqual(module["args"][side]["audiorouter.channel"], "mic")

    def test_controls_live_on_the_capture_side(self):
        self.assertEqual(Channel("mic", "Mic", "", kind=INPUT).control_name, "ar_mic_in")
        self.assertEqual(Channel("out", "Out", "dev").control_name, "ar_out")


class EchoCancelRenderTest(unittest.TestCase):
    def aec(self, channel):
        return next(m["args"] for m in channel.render_config()["context.modules"]
                    if m["name"] == "libpipewire-module-echo-cancel")

    def test_off_by_default_and_no_canceller_in_the_conf(self):
        mic = Channel("mic", "Mic", "alsa_input.usb", kind=INPUT)
        self.assertFalse(mic.echo_cancel)
        names = [m["name"] for m in mic.render_config()["context.modules"]]
        self.assertNotIn("libpipewire-module-echo-cancel", names)

    def test_the_effects_read_the_cancelled_mic_and_the_canceller_reads_the_real_one(self):
        mic = Channel("mic", "Mic", "alsa_input.usb", kind=INPUT, echo_cancel=True)
        chain = modules(mic)["libpipewire-module-filter-chain"]
        self.assertEqual(chain["capture.props"]["target.object"], "ar_mic_ec")
        self.assertTrue(chain["capture.props"][NO_FALLBACK])
        aec = self.aec(mic)
        self.assertEqual(aec["capture.props"]["target.object"], "alsa_input.usb")
        self.assertTrue(aec["capture.props"][NO_FALLBACK])
        self.assertEqual(aec["source.props"]["node.name"], "ar_mic_ec")

    def test_the_default_input_is_followed_when_no_mic_is_chosen(self):
        aec = self.aec(Channel("mic", "Mic", "", kind=INPUT, echo_cancel=True))
        self.assertNotIn("target.object", aec["capture.props"])
        self.assertNotIn(NO_FALLBACK, aec["capture.props"])

    def test_the_reference_is_the_speakers_monitor_and_nothing_is_rerouted(self):
        aec = self.aec(Channel("mic", "Mic", "", kind=INPUT, echo_cancel=True))
        self.assertTrue(aec["monitor.mode"])
        self.assertEqual(aec["library.name"], "aec/libspa-aec-webrtc")

    def test_noise_suppression_and_gain_are_left_to_the_channel_effects(self):
        args = self.aec(Channel("mic", "Mic", "", kind=INPUT, echo_cancel=True))["aec.args"]
        self.assertFalse(args["webrtc.noise_suppression"])
        self.assertFalse(args["webrtc.gain_control"])

    def test_the_cancelled_mic_can_never_become_the_default_microphone(self):
        aec = self.aec(Channel("mic", "Mic", "", kind=INPUT, echo_cancel=True))
        self.assertEqual(aec["source.props"]["priority.session"], 1)

    def test_every_canceller_node_is_stamped_as_ours(self):
        aec = self.aec(Channel("mic", "Mic", "", kind=INPUT, echo_cancel=True))
        for side in ("capture.props", "source.props", "sink.props"):
            self.assertEqual(aec[side]["audiorouter.channel"], "mic")

    def test_outputs_never_cancel_echo(self):
        self.assertFalse(Channel("out", "Out", "dev", echo_cancel=True).echo_cancel)

    def test_round_trip_and_old_configs(self):
        mic = Channel("mic", "Mic", "", kind=INPUT, echo_cancel=True)
        self.assertTrue(Channel.from_dict(mic.to_dict()).echo_cancel)
        old = {"slug": "mic", "name": "Mic", "kind": "input", "device": ""}
        self.assertFalse(Channel.from_dict(old).echo_cancel)


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

    def test_a_new_mic_starts_echo_cancelled(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        self.assertTrue(Config.load(self.engine.path).channel("mic").echo_cancel)
        self.engine.create_channel("out", "Out")
        self.assertFalse(self.engine.channel("out").echo_cancel)

    def test_echo_cancelling_is_for_inputs_and_restarts_the_channel(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        with self.assertRaises(EngineError):
            self.engine.set_echo_cancel("speakers", True)
        self.engine.set_echo_cancel("mic", False)
        mic = self.engine.channel("mic")
        before = mic.render_config_text()
        self.engine.set_echo_cancel("mic", True)
        self.assertTrue(Config.load(self.engine.path).channel("mic").echo_cancel)
        # A shape change, not a knob: the rendered conf differs outside controls.
        self.assertNotEqual(self.engine.channel("mic").render_config_text(), before)
        self.engine.set_echo_cancel("mic", False)
        self.assertEqual(self.engine.channel("mic").render_config_text(), before)

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
        self.assertEqual(status["input_devices"],
                         [{"name": "alsa_input.usb", "label": "USB Mic", "volume": None, "muted": None}])


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

    def test_recorders_of_a_mic_follow_its_companions_restart(self):
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
            self.engine._hand_over(self.engine.channel("mic_mix"), 222, timeout=0)
        self.assertEqual([c.args for c in metadata.call_args_list], [(75, 710)])

    def test_after_upgrading_the_mic_hands_its_recorders_to_its_companion(self):
        # An older mic host published the virtual mic itself. Its first
        # restart must move Discord onto the companion's copy (measured live:
        # no gap), or Discord falls back to the raw default mic.
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        graph = Graph([
            fakes.client(1, 111), fakes.client(3, 333),
            fakes.node(70, "ar_mic", "Audio/Source", serial=700, client_id=1, **{"audiorouter.channel": "mic"}),
            fakes.node(73, "ar_mic", "Audio/Source", serial=730, client_id=3, **{"audiorouter.channel": "mic_mix"}),
            fakes.node(75, "discord", "Stream/Input/Audio", serial=750, **{"application.name": "Discord"}),
            fakes.port(80, 70, "out"), fakes.port(81, 75, "in"), fakes.link(90, 80, 81),
        ])
        companion = self.engine.channel("mic_mix")
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch.object(type(companion), "pid", lambda self: 333 if self.slug == "mic_mix" else 222), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata, \
             mock.patch("audiorouter.engine.time.sleep"):
            self.engine._hand_over(self.engine.channel("mic"), 222, timeout=0)
        self.assertEqual([c.args for c in metadata.call_args_list], [(75, 730)])

    def test_a_level_meter_reading_the_old_source_is_left_alone(self):
        # Meter taps refuse moves; waiting for one held every restart for the
        # full timeout. The meter reopens itself on the new host instead.
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        graph = Graph([
            fakes.client(1, 111), fakes.client(2, 222),
            fakes.node(70, "ar_mic", "Audio/Source", serial=700, client_id=1, **{"audiorouter.channel": "mic"}),
            fakes.node(71, "ar_mic", "Audio/Source", serial=710, client_id=2, **{"audiorouter.channel": "mic"}),
            fakes.node(75, "discord", "Stream/Input/Audio", serial=750, **{"application.name": "Discord"}),
            fakes.node(77, "meter", "Stream/Input/Audio", serial=770, **{"audiorouter.meter": "true"}),
            fakes.port(80, 70, "out"), fakes.port(81, 75, "in"), fakes.port(82, 77, "in"),
            fakes.link(90, 80, 81), fakes.link(91, 80, 82),
        ])
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata, \
             mock.patch("audiorouter.engine.time.sleep"):
            self.engine._hand_over(self.engine.channel("mic_mix"), 222, timeout=0)
        self.assertEqual([c.args for c in metadata.call_args_list], [(75, 710)])

    def test_the_new_hosts_own_loopback_is_pointed_at_its_own_source(self):
        self.engine.create_channel("mic", "Mic", kind=INPUT)
        self.engine.set_listen("mic", "speakers")
        graph = Graph([
            fakes.client(1, 111), fakes.client(2, 222),
            fakes.node(70, "ar_mic", "Audio/Source", serial=700, client_id=1, **{"audiorouter.channel": "mic"}),
            fakes.node(71, "ar_mic", "Audio/Source", serial=710, client_id=2, **{"audiorouter.channel": "mic"}),
            fakes.node(76, "ar_mic_mix_play_in", "Stream/Input/Audio", serial=760, client_id=2,
                       **{"audiorouter.channel": "mic_mix", "target.object": "ar_mic"}),
        ])
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata, \
             mock.patch("audiorouter.engine.time.sleep"):
            self.engine._hand_over(self.engine.channel("mic_mix"), 222, timeout=0)
        self.assertEqual([c.args for c in metadata.call_args_list], [(76, 710)])


if __name__ == "__main__":
    unittest.main()
