import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter.channels import Channel
from audiorouter.config import Config, ConfigError
from audiorouter.effects import Effect, EffectError
from audiorouter.engine import AutoRouter, DaemonRecord, Engine, EngineError, daemon_pid
from audiorouter.pwgraph import Graph
from audiorouter.routing import Rule, RuleSet

from . import fakes


def live_graph():
    return Graph(
        [
            fakes.sink(50, "alsa_output.a", serial=500, description="Built-in Audio"),
            fakes.sink(51, "ar_speakers", serial=510, hardware=False, description="Speakers",
                       **{"audiorouter.channel": "speakers"}),
            fakes.stream(60, "firefox", serial=600, **{"application.name": "firefox"}),
            fakes.node(61, "ar_speakers_out", "Stream/Output/Audio", serial=610,
                       **{"audiorouter.channel": "speakers"}),
            fakes.port(80, 60, "out"),
            fakes.port(81, 50, "in"),
            fakes.link(90, 80, 81),
        ]
    )


class EngineTestCase(unittest.TestCase):
    """Engine tests with the config and runtime directories redirected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.tmp.name) / "config.json"
        self.engine = Engine(
            Config(channels=[Channel("speakers", "Speakers", "alsa_output.a")]),
            path=self.path,
        )
        self.engine.use_graph(live_graph())
        # XDG_RUNTIME_DIR is redirected above, so a real pw-dump could not reach
        # PipeWire anyway; make that explicit rather than accidental.
        snapshot = mock.patch.object(Graph, "snapshot", staticmethod(live_graph))
        snapshot.start()
        self.addCleanup(snapshot.stop)


class EditingTest(EngineTestCase):
    def test_every_edit_is_saved_immediately(self):
        # The GUI, the CLI and the daemon each read the file; an unsaved edit
        # is an edit the other two cannot see.
        self.engine.add_effect("speakers", "gain", {"gain_db": -3})
        self.assertEqual(len(Config.load(self.path).channel("speakers").effects), 1)

    def test_effect_parameters_are_normalised_on_the_way_in(self):
        effect = self.engine.add_effect("speakers", "gain", {"gain_db": 999})
        self.assertEqual(effect.params["gain_db"], 20.0)

    def test_effects_can_be_inserted_at_a_position(self):
        self.engine.add_effect("speakers", "gain")
        self.engine.add_effect("speakers", "limiter", index=0)
        self.assertEqual([e.kind for e in self.engine.channel("speakers").effects],
                         ["limiter", "gain"])

    def test_reordering_the_chain(self):
        self.engine.add_effect("speakers", "gain")
        self.engine.add_effect("speakers", "lowpass")
        self.engine.move_effect("speakers", 0, 1)
        self.assertEqual([e.kind for e in self.engine.channel("speakers").effects],
                         ["lowpass", "gain"])

    def test_editing_an_effect_that_is_not_there_says_how_many_are(self):
        with self.assertRaises(EffectError) as caught:
            self.engine.set_effect_params("speakers", 4, {"gain_db": 0})
        self.assertIn("it has 0", str(caught.exception))

    def test_setting_parameters_keeps_the_ones_not_given(self):
        self.engine.add_effect("speakers", "peaking", {"frequency": 500, "gain_db": 4})
        effect = self.engine.set_effect_params("speakers", 0, {"gain_db": -4})
        self.assertEqual(effect.params["frequency"], 500)
        self.assertEqual(effect.params["gain_db"], -4)

    def test_a_rule_must_point_at_a_channel_that_exists(self):
        with self.assertRaises(ConfigError):
            self.engine.add_rule("app", "firefox", "ghost")

    def test_rules_can_be_reordered_because_the_first_one_wins(self):
        self.engine.add_rule("app", "firefox", "speakers")
        self.engine.add_rule("app", "fire", "speakers")
        self.engine.move_rule(1, 0)
        self.assertEqual(self.engine.config.rules.rules[0].pattern, "fire")

    def test_deleting_a_channel_stops_it_first(self):
        with mock.patch.object(Channel, "stop") as stop:
            self.engine.delete_channel("speakers")
        stop.assert_called_once()
        self.assertEqual(self.engine.config.channels, [])

    def test_seeding_refuses_to_overwrite_an_existing_setup(self):
        with self.assertRaises(EngineError):
            self.engine.adopt_devices()

    def test_seeding_an_empty_config_uses_the_real_devices(self):
        engine = Engine(Config(), path=self.path)
        engine.use_graph(live_graph())
        channels = engine.adopt_devices()
        self.assertEqual([c.device for c in channels], ["alsa_output.a"])


class ReconcileTest(EngineTestCase):
    def test_a_stale_config_on_disk_means_a_restart_is_due(self):
        channel = self.engine.channel("speakers")
        channel.config_path.write_text("{}")
        with mock.patch.object(Channel, "is_running", return_value=True):
            self.assertTrue(self.engine.needs_restart(channel))
            channel.config_path.write_text(channel.render_config_text())
            self.assertFalse(self.engine.needs_restart(channel))

    def test_a_stopped_channel_is_never_restarted_by_reconciling(self):
        self.assertFalse(self.engine.needs_restart(self.engine.channel("speakers")))

    def test_apply_starts_an_enabled_channel(self):
        with mock.patch.object(Channel, "is_running", return_value=False), \
             mock.patch.object(Channel, "start", return_value=1) as start:
            report = self.engine.apply()
        start.assert_called_once()
        self.assertTrue(report.ok)
        self.assertEqual(report.actions[0].kind, "start")

    def test_apply_stops_a_disabled_channel(self):
        self.engine.set_enabled("speakers", False)
        with mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "stop", return_value=True) as stop:
            report = self.engine.apply()
        stop.assert_called_once()
        self.assertEqual(report.actions[0].kind, "stop")

    def test_apply_leaves_a_healthy_channel_alone(self):
        channel = self.engine.channel("speakers")
        channel.config_path.write_text(channel.render_config_text())
        with mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "start") as start:
            report = self.engine.apply()
        start.assert_not_called()
        self.assertFalse(report.changed)

    def test_a_channel_that_cannot_start_does_not_stop_the_others(self):
        self.engine.create_channel("phones", "Phones", "alsa_output.b")
        self.engine.add_effect("speakers", "limiter")
        with mock.patch("audiorouter.plugins.available_loaders", return_value=frozenset({"builtin"})), \
             mock.patch.object(Channel, "is_running", return_value=False), \
             mock.patch.object(Channel, "start", return_value=1) as start:
            report = self.engine.apply()
        self.assertFalse(report.ok)
        self.assertEqual([f.target for f in report.failures], ["speakers"])
        self.assertEqual([a.target for a in report.actions], ["phones"])
        self.assertEqual(start.call_count, 1)

    def test_a_process_left_by_a_deleted_channel_is_found_and_stopped(self):
        (Path(self.tmp.name) / "audiorouter").mkdir(exist_ok=True)
        (Path(self.tmp.name) / "audiorouter" / "ghost.pid").write_text("1234")
        with mock.patch.object(Channel, "is_running", side_effect=lambda self=None: True):
            self.assertEqual(self.engine.orphan_slugs(), ["ghost"])

    def test_streams_are_put_back_on_a_channel_that_restarted(self):
        # A restart destroys the sink; every stream pointed at it falls back to
        # the default output and stays there unless it is moved back.
        before = Graph(
            [
                fakes.sink(51, "ar_speakers", serial=510, hardware=False,
                           **{"audiorouter.channel": "speakers"}),
                fakes.stream(60, "firefox", serial=600),
                fakes.port(80, 60, "out"),
                fakes.port(81, 51, "in"),
                fakes.link(90, 80, 81),
            ]
        )
        after = Graph(
            [
                fakes.sink(52, "ar_speakers", serial=520, hardware=False,
                           **{"audiorouter.channel": "speakers"}),
                fakes.stream(60, "firefox", serial=600),
            ]
        )
        with mock.patch.object(Graph, "snapshot", side_effect=[before, after]), \
             mock.patch.object(Channel, "is_running", return_value=False), \
             mock.patch.object(Channel, "start", return_value=1), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            report = self.engine.apply()
        metadata.assert_called_once_with(60, 520)
        self.assertIn("restore", [a.kind for a in report.actions])

    def test_nothing_is_restored_when_no_channel_changed(self):
        channel = self.engine.channel("speakers")
        channel.config_path.write_text(channel.render_config_text())
        with mock.patch.object(Graph, "snapshot", return_value=live_graph()), \
             mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.engine.apply()
        metadata.assert_not_called()

    def test_a_dry_run_never_moves_anything(self):
        engine = Engine(self.engine.config, path=self.path, dry_run=True)
        with mock.patch.object(Graph, "snapshot", return_value=live_graph()), \
             mock.patch.object(Channel, "is_running", return_value=False), \
             mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            report = engine.apply()
        metadata.assert_not_called()
        self.assertEqual(report.actions[0].detail, "dry run")

    def test_a_running_channel_whose_sink_has_gone_is_restarted(self):
        # A PipeWire restart leaves the host process alive, its conf unchanged,
        # and no sink: skipping it would route every app into nothing.
        channel = self.engine.channel("speakers")
        channel.config_path.write_text(channel.render_config_text())
        no_sink = Graph([fakes.sink(50, "alsa_output.a", serial=500)])
        with mock.patch.object(Graph, "snapshot", return_value=no_sink), \
             mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "start", return_value=1) as start:
            report = self.engine.apply()
        start.assert_called_once()
        self.assertIn("sink had gone", report.actions[0].detail)

    def test_an_unreadable_graph_does_not_restart_healthy_channels(self):
        from audiorouter.pwgraph import PwError

        channel = self.engine.channel("speakers")
        channel.config_path.write_text(channel.render_config_text())
        with mock.patch.object(Graph, "snapshot", side_effect=PwError("down")), \
             mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "start") as start:
            self.engine.apply()
        start.assert_not_called()

    def test_a_dead_pid_file_is_cleaned_up_rather_than_reported(self):
        directory = Path(self.tmp.name) / "audiorouter"
        directory.mkdir(exist_ok=True)
        (directory / "ghost.pid").write_text("999999")
        self.assertEqual(self.engine.orphan_slugs(), [])
        self.assertFalse((directory / "ghost.pid").exists())


class ReloadTest(EngineTestCase):
    def test_settings_saved_by_another_process_are_picked_up(self):
        self.engine.save()
        other = Engine.load(self.path)
        other.add_rule("app", "firefox", "speakers")
        os.utime(self.path, ns=(1, 1))  # a same-size rewrite within one mtime tick
        self.assertTrue(self.engine.reload_if_changed())
        self.assertEqual(len(self.engine.config.rules.rules), 1)
        self.assertFalse(self.engine.reload_if_changed())

    def test_our_own_save_is_not_mistaken_for_someone_elses(self):
        self.engine.add_rule("app", "firefox", "speakers")
        self.assertFalse(self.engine.reload_if_changed())

    def test_a_broken_file_keeps_the_last_good_settings(self):
        self.engine.add_rule("app", "firefox", "speakers")
        self.path.write_text("{ not json")
        self.assertFalse(self.engine.reload_if_changed())
        self.assertEqual(len(self.engine.config.rules.rules), 1)

    def test_the_daemon_router_reloads_before_routing(self):
        self.engine.save()
        other = Engine.load(self.path)
        other.add_rule("app", "firefox", "speakers")
        os.utime(self.path, ns=(1, 1))
        auto = AutoRouter(self.engine, follow_config=True)
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            auto._changed(live_graph())
        metadata.assert_called_once_with(60, 510)

    def test_the_window_router_never_reloads(self):
        with mock.patch.object(Engine, "reload_if_changed") as reload:
            AutoRouter(self.engine)._changed(live_graph())
        reload.assert_not_called()


class DaemonRecordTest(EngineTestCase):
    def test_a_running_daemon_is_found(self):
        with mock.patch("audiorouter.engine._cmdline",
                        return_value=["/usr/bin/python3", "-m", "audiorouter", "watch"]):
            self.assertIsNone(daemon_pid())
            with DaemonRecord():
                self.assertEqual(daemon_pid(), os.getpid())
            self.assertIsNone(daemon_pid())

    def test_cleaning_up_orphaned_channels_leaves_the_daemon_record_alone(self):
        with mock.patch("audiorouter.engine._cmdline",
                        return_value=["/usr/bin/python3", "-m", "audiorouter", "watch"]), \
             DaemonRecord():
            self.engine.orphan_slugs()
            self.engine.status(refresh=False)
            self.assertEqual(daemon_pid(), os.getpid())

    def test_a_reused_pid_is_not_mistaken_for_the_daemon(self):
        with mock.patch("audiorouter.engine._cmdline", return_value=["/usr/bin/kate"]), \
             DaemonRecord():
            self.assertIsNone(daemon_pid())


class RoutingTest(EngineTestCase):
    def test_route_moves_a_matching_stream_to_its_channel(self):
        self.engine.add_rule("app", "firefox", "speakers")
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            results = self.engine.route(refresh=False)
        metadata.assert_called_once_with(60, 510)
        self.assertEqual([(r.stream, r.moved) for r in results], [("firefox", True)])

    def test_a_failed_move_is_reported_not_raised(self):
        self.engine.add_rule("app", "firefox", "speakers")
        with mock.patch("audiorouter.routing.Router.move", side_effect=RuntimeError("busy")):
            results = self.engine.route(refresh=False)
        self.assertFalse(results[0].moved)
        self.assertIn("busy", results[0].reason)

    def test_sending_a_stream_to_a_stopped_channel_explains_itself(self):
        self.engine.create_channel("phones", "Phones", "alsa_output.b")
        with mock.patch.object(Engine, "graph", return_value=live_graph()):
            with self.assertRaises(EngineError) as caught:
                self.engine.send(60, "phones")
        self.assertIn("not running", str(caught.exception))

    def test_the_first_manual_send_remembers_the_app(self):
        with mock.patch("audiorouter.routing.Router._set_metadata"):
            result = self.engine.send(60, "speakers", remember_new=True)
        self.assertEqual(result.reason, "manual, remembered")
        rules = Config.load(self.path).rules.rules
        self.assertEqual([(r.field, r.pattern, r.channel) for r in rules],
                         [("app", "firefox", "speakers")])

    def test_a_later_manual_send_is_a_one_off(self):
        self.engine.create_channel("phones", "Phones", "alsa_output.b")
        self.engine.add_rule("app", "firefox", "phones")
        with mock.patch("audiorouter.routing.Router._set_metadata"):
            self.engine.send(60, "speakers", remember_new=True)
        self.assertEqual([r.channel for r in self.engine.config.rules.rules], ["phones"])

    def test_a_plain_send_remembers_nothing(self):
        with mock.patch("audiorouter.routing.Router._set_metadata"):
            self.engine.send(60, "speakers")
        self.assertEqual(self.engine.config.rules.rules, [])

    def test_status_reports_where_each_stream_actually_is(self):
        self.engine.add_rule("app", "firefox", "speakers")
        status = self.engine.status(refresh=False)
        (stream,) = status["streams"]
        self.assertEqual(stream["app"], "firefox")
        self.assertEqual(stream["sink"], "Built-in Audio")
        self.assertIsNone(stream["channel"])
        self.assertEqual(stream["rule_channel"], "speakers")

    def test_status_does_not_list_our_own_outputs_as_applications(self):
        self.assertEqual([s["id"] for s in self.engine.status(refresh=False)["streams"]], [60])


class AutoRouterTest(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.engine.add_rule("app", "firefox", "speakers")
        self.auto = AutoRouter(self.engine)

    def test_a_new_stream_is_placed_once(self):
        graph = live_graph()
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.auto._changed(graph)
            self.auto._changed(graph)
        # Placing it again on the next graph event would undo any move the user
        # made themselves, every second, for as long as the app ran.
        metadata.assert_called_once()

    def test_a_restarted_stream_is_placed_again(self):
        graph = live_graph()
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.auto._changed(graph)
            graph.apply([fakes.removal(60)])
            self.auto._changed(graph)
            graph.apply([fakes.stream(60, "firefox", serial=700,
                                      **{"application.name": "firefox"})])
            self.auto._changed(graph)
        self.assertEqual(metadata.call_count, 2)

    def test_a_stream_moved_by_hand_is_left_where_the_user_put_it(self):
        self.auto.remember(60)
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.auto._changed(live_graph())
        metadata.assert_not_called()

    def test_nothing_moves_while_auto_routing_is_off(self):
        self.engine.config.auto_route = False
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.auto._changed(live_graph())
        metadata.assert_not_called()

    def test_forgetting_a_stream_allows_it_to_be_placed_again(self):
        graph = live_graph()
        with mock.patch("audiorouter.routing.Router._set_metadata") as metadata:
            self.auto._changed(graph)
            self.auto.forget(60)
            self.auto._changed(graph)
        self.assertEqual(metadata.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class InputChannelApplyTest(EngineTestCase):
    def test_a_running_input_channel_whose_mic_node_is_present_is_not_restarted(self):
        # Regression: the "has its node gone" check used the routing map, which
        # holds outputs only, so every input channel restarted on every apply -
        # including plain knob changes that should have been live.
        from audiorouter.channels import INPUT

        mic = Channel("mic", "Mic", "", kind=INPUT)
        self.engine.config.add_channel(mic)
        mic.config_path.write_text(mic.render_config_text())
        graph = live_graph()
        graph.apply([fakes.node(70, "ar_mic", "Audio/Source", serial=700, **{"audiorouter.channel": "mic"})])
        self.engine.use_graph(graph)
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), \
             mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "start") as start:
            self.engine.apply()
        self.assertEqual(start.call_count, 1)  # only "speakers", whose conf was never written
