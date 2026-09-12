import subprocess
import unittest
from unittest import mock

from audiorouter.pwgraph import Graph
from audiorouter.routing import Router, RoutingError, Rule, RuleSet, plan

from . import fakes


def stream(**props):
    return Graph([fakes.stream(60, "player", serial=600, **props)]).node(60)


class RuleTest(unittest.TestCase):
    def test_a_plain_pattern_matches_as_a_substring(self):
        self.assertTrue(Rule("app", "fire", "x").matches(stream(**{"application.name": "Firefox"})))

    def test_matching_ignores_case(self):
        self.assertTrue(Rule("app", "FIREFOX", "x").matches(stream(**{"application.name": "firefox"})))

    def test_a_glob_pattern_is_anchored(self):
        rule = Rule("binary", "*.exe", "x")
        self.assertTrue(rule.matches(stream(**{"application.process.binary": "game.exe"})))
        self.assertFalse(rule.matches(stream(**{"application.process.binary": "game.exe.bak"})))

    def test_the_title_field_reads_media_name(self):
        self.assertTrue(Rule("title", "podcast", "x").matches(stream(**{"media.name": "A Podcast"})))

    def test_a_disabled_rule_never_matches(self):
        self.assertFalse(Rule("app", "player", "x", enabled=False).matches(stream()))

    def test_an_absent_property_does_not_match(self):
        self.assertFalse(Rule("binary", "player", "x").matches(stream()))

    def test_an_unknown_field_is_refused_with_the_valid_ones(self):
        with self.assertRaises(RoutingError) as caught:
            Rule("volume", "x", "y")
        self.assertIn("app", str(caught.exception))

    def test_an_empty_pattern_is_refused(self):
        with self.assertRaises(RoutingError):
            Rule("app", "", "y")

    def test_first_match_wins(self):
        rules = RuleSet([Rule("app", "play", "first"), Rule("app", "player", "second")])
        self.assertEqual(rules.resolve(stream()), "first")

    def test_nothing_matching_means_no_opinion(self):
        self.assertIsNone(RuleSet([Rule("app", "vlc", "x")]).resolve(stream()))

    def test_round_trip_through_a_dict(self):
        rule = Rule("title", "news", "speakers", enabled=False)
        self.assertEqual(Rule.from_dict(rule.to_dict()).to_dict(), rule.to_dict())


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.graph = Graph(
            [
                fakes.sink(50, "ar_speakers", serial=500, hardware=False,
                           **{"audiorouter.channel": "speakers"}),
                fakes.sink(51, "alsa_output.a", serial=510),
                fakes.stream(60, "firefox", serial=600, **{"application.name": "firefox"}),
                fakes.node(61, "ar_speakers_out", "Stream/Output/Audio", serial=610,
                           **{"audiorouter.channel": "speakers"}),
                fakes.port(80, 60, "out"),
                fakes.port(81, 51, "in"),
                fakes.link(90, 80, 81),
            ]
        )
        self.sinks = {"speakers": self.graph.node(50)}

    def test_a_stream_on_the_wrong_sink_is_planned_for_a_move(self):
        rules = RuleSet([Rule("app", "firefox", "speakers")])
        (placement,) = plan(self.graph, rules, self.sinks)
        self.assertTrue(placement.needs_move)
        self.assertEqual(placement.reason, "move to 'speakers'")

    def test_a_stream_already_in_place_is_left_alone(self):
        graph = Graph(
            [
                fakes.sink(50, "ar_speakers", serial=500, hardware=False,
                           **{"audiorouter.channel": "speakers"}),
                fakes.stream(60, "firefox", serial=600, **{"application.name": "firefox"}),
                fakes.port(80, 60, "out"),
                fakes.port(81, 50, "in"),
                fakes.link(90, 80, 81),
            ]
        )
        rules = RuleSet([Rule("app", "firefox", "speakers")])
        (placement,) = plan(graph, rules, {"speakers": graph.node(50)})
        self.assertFalse(placement.needs_move)
        self.assertEqual(placement.reason, "already on 'speakers'")

    def test_a_rule_for_a_stopped_channel_moves_nothing(self):
        rules = RuleSet([Rule("app", "firefox", "speakers")])
        (placement,) = plan(self.graph, rules, {})
        self.assertFalse(placement.needs_move)
        self.assertIn("not running", placement.reason)

    def test_unmatched_streams_are_reported_not_moved(self):
        (placement,) = plan(self.graph, RuleSet([]), self.sinks)
        self.assertFalse(placement.needs_move)
        self.assertEqual(placement.reason, "no rule matches")

    def test_our_own_chain_outputs_are_never_planned(self):
        # Moving one would wire a channel's output back into a channel.
        placements = plan(self.graph, RuleSet([Rule("title", "speakers", "speakers")]), self.sinks)
        self.assertEqual([p.stream.id for p in placements], [60])


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.graph = Graph(
            [
                fakes.sink(50, "ar_speakers", serial=500, hardware=False,
                           **{"audiorouter.channel": "speakers"}),
                fakes.stream(60, "firefox", serial=600),
                fakes.sink(51, "no_serial", hardware=False),
                fakes.node(61, "ar_speakers_out", "Stream/Output/Audio", serial=610,
                           **{"audiorouter.channel": "speakers"}),
            ]
        )

    def test_a_move_targets_the_serial_never_the_node_id(self):
        # pw-metadata with a node id silently retargets the default sink.
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            Router().move(self.graph.node(60), self.graph.node(50))
        metadata.assert_called_once_with(60, 500)

    def test_a_sink_with_no_serial_cannot_be_targeted(self):
        with self.assertRaises(RoutingError):
            Router().move(self.graph.node(60), self.graph.node(51))

    def test_moving_one_of_our_own_outputs_is_refused(self):
        with self.assertRaises(RoutingError):
            Router().move(self.graph.node(61), self.graph.node(50))

    def test_a_dry_run_changes_nothing(self):
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.assertTrue(Router(dry_run=True).move(self.graph.node(60), self.graph.node(50)))
        metadata.assert_not_called()

    def test_pactl_is_the_fallback_when_metadata_fails(self):
        # pipewire-pulse may be absent, but so may pw-metadata: whichever one
        # fails, the move has to be attempted the other way.
        for failure in (OSError("gone"), subprocess.CalledProcessError(1, "pw-metadata")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch("audiorouter.routing.Router._set_metadata", side_effect=failure), \
                     mock.patch("audiorouter.routing.Router._pactl_move") as pactl:
                    self.assertTrue(Router().move(self.graph.node(60), self.graph.node(50)))
                pactl.assert_called_once_with(60, "ar_speakers")

    def test_a_failure_on_both_paths_names_the_stream_and_the_sink(self):
        with mock.patch("audiorouter.routing.Router._set_metadata", side_effect=OSError("gone")), \
             mock.patch("audiorouter.routing.Router._pactl_move", side_effect=OSError("also gone")):
            with self.assertRaises(RoutingError) as caught:
                Router().move(self.graph.node(60), self.graph.node(50))
        self.assertIn("ar_speakers", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
