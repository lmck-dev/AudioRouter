"""Noticing a lone WirePlumber restart, which breaks echo cancellation."""

import json
import os
import unittest
from unittest import mock

from audiorouter import session
from audiorouter.channels import INPUT, Channel
from audiorouter.pwgraph import Graph

from . import fakes

PIPEWIRE, WIREPLUMBER = 100, 200


def graph(with_session=True):
    objects = [{"id": 0, "type": "PipeWire:Interface:Core",
                "info": {"props": {"application.process.id": PIPEWIRE}}}]
    if with_session:
        objects.append({"id": 34, "type": "PipeWire:Interface:Client",
                        "info": {"props": {"application.process.id": WIREPLUMBER,
                                           "wireplumber.daemon": True}}})
    objects.append(fakes.client(40, 999))  # an ordinary app
    return Graph(objects)


def started(pipewire, wireplumber):
    return mock.patch.object(session, "started_at",
                             side_effect=lambda pid: {PIPEWIRE: pipewire, WIREPLUMBER: wireplumber}.get(pid))


def channels(echo_cancel=True, enabled=True):
    return [Channel("speakers", "Speakers", ""),
            Channel("mic", "Mic", "", kind=INPUT, echo_cancel=echo_cancel, enabled=enabled)]


class DetectionTest(unittest.TestCase):
    def test_the_graph_names_both_processes(self):
        self.assertEqual((graph().daemon_pid(), graph().session_manager_pid()), (PIPEWIRE, WIREPLUMBER))
        self.assertIsNone(graph(with_session=False).session_manager_pid())

    def test_started_together_at_login_is_fine(self):
        with started(5.0, 5.4):
            self.assertIsNone(session.wireplumber_restarted_alone(graph()))

    def test_started_well_after_pipewire_was_restarted_alone(self):
        with started(5.0, 3600.0):
            self.assertEqual(session.wireplumber_restarted_alone(graph()), WIREPLUMBER)

    def test_only_an_enabled_echo_cancelling_input_is_broken_by_it(self):
        with started(5.0, 3600.0):
            self.assertEqual(session.echo_cancel_broken(channels(), graph()), WIREPLUMBER)
            self.assertIsNone(session.echo_cancel_broken(channels(echo_cancel=False), graph()))
            self.assertIsNone(session.echo_cancel_broken(channels(enabled=False), graph()))

    def test_no_wireplumber_or_a_gone_process_is_not_a_verdict(self):
        with started(5.0, 3600.0):
            self.assertIsNone(session.wireplumber_restarted_alone(graph(with_session=False)))
        with started(None, 3600.0):
            self.assertIsNone(session.wireplumber_restarted_alone(graph()))

    def test_start_times_come_from_proc(self):
        self.assertGreater(session.started_at(os.getpid()), 0)
        self.assertIsNone(session.started_at(2 ** 22 + 7))


class FixTest(unittest.TestCase):
    def test_the_restart_runs_outside_our_own_service(self):
        with mock.patch.object(session.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(session.subprocess, "Popen") as popen:
            session.restart_sound_system()
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["systemd-run", "--user", "--collect"])
        script = command[-1]
        self.assertIn("systemctl --user restart pipewire pipewire-pulse wireplumber", script)
        self.assertIn("is-active audiorouter || ", script)  # the service re-applies if it runs
        self.assertIn("-m audiorouter apply", script)

    def test_the_notice_is_sent_once_per_wireplumber_and_its_button_fixes(self):
        notifier = session.EchoCancelNotifier()
        clicked = mock.Mock(stdout="fix\n")
        with started(5.0, 3600.0), \
                mock.patch.object(session.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(session.threading, "Thread") as thread, \
                mock.patch.object(session.subprocess, "run", return_value=clicked) as run, \
                mock.patch.object(session, "restart_sound_system") as restart:
            thread.side_effect = lambda target, **_: mock.Mock(start=target)
            notifier.check(channels(), graph())
            notifier.check(channels(), graph())  # the same WirePlumber: no second notice
        self.assertEqual(run.call_count, 1)
        self.assertIn("--action=fix=Restart the sound system", run.call_args.args[0])
        restart.assert_called_once_with()

    def test_dismissing_the_notice_restarts_nothing(self):
        notifier = session.EchoCancelNotifier()
        with started(5.0, 3600.0), \
                mock.patch.object(session.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(session.threading, "Thread") as thread, \
                mock.patch.object(session.subprocess, "run", return_value=mock.Mock(stdout="")), \
                mock.patch.object(session, "restart_sound_system") as restart:
            thread.side_effect = lambda target, **_: mock.Mock(start=target)
            notifier.check(channels(), graph())
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class HandleWarningTest(unittest.TestCase):
    """PipeWire nearing its open-file limit (it went silent at 1024 on 31 Oct 2026)."""

    def notifier(self, files, limit=1000):
        clock = [0.0]
        lines = []
        notifier = session.HandleNotifier(log=lines.append, clock=lambda: clock[0])
        notifier.use_graph(graph())
        patches = [mock.patch.object(session, "open_files", return_value=files),
                   mock.patch.object(session, "open_file_limit", return_value=limit),
                   mock.patch.object(session.shutil, "which", return_value=None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return notifier, lines, clock

    def test_well_under_the_limit_says_nothing(self):
        notifier, lines, _ = self.notifier(["socket:[1]"] * 500)
        notifier.check()
        self.assertEqual(lines, [])

    def test_near_the_limit_warns_once_with_the_kinds(self):
        files = ["/memfd:pipewire-memfd (deleted)"] * 500 + ["anon_inode:[eventfd]"] * 250 + ["socket:[7]"] * 60
        notifier, lines, clock = self.notifier(files)
        notifier.check()
        self.assertEqual(lines, ["PipeWire holds 810 of 1000 open files (memory 500, wakeup 250, socket 60, other 0)"])
        clock[0] += session.HANDLES_CHECK_S * 2
        notifier.check()
        self.assertEqual(len(lines), 1)

    def test_counts_at_most_once_a_minute(self):
        notifier, _, clock = self.notifier([])
        notifier.check()
        with mock.patch.object(session, "open_files") as count:
            clock[0] += 1.0
            notifier.check()
            count.assert_not_called()
            clock[0] += session.HANDLES_CHECK_S
            notifier.check()
            count.assert_called_once_with(PIPEWIRE)

    def test_reads_this_process_for_real(self):
        self.assertIsNotNone(session.open_files(os.getpid()))
        self.assertGreater(session.open_file_limit(os.getpid()), 0)


# -- self-check (11 Nov 2026) ---------------------------------------------------

HOST = 4242
STAMP = {"audiorouter.channel": "listener"}


def wired_graph(mic_linked=True, old_host_copy=False):
    """The Listener's echo-canceller mic capture, linked to the mic or not."""
    objects = [
        fakes.client(50, HOST), fakes.client(51, 777),
        fakes.node(1, "alsa_input.mic", "Audio/Source", **{"device.id": 9}),
        fakes.node(10, "ar_listener_ec_mic", "Stream/Input/Audio", client_id=50, **STAMP),
        fakes.node(11, "ar_listener_out", "Stream/Output/Audio", client_id=50, **STAMP),
        fakes.node(12, "ar_listener_mix", "Audio/Sink", client_id=50, **STAMP),
        fakes.node(13, "tap", "Stream/Input/Audio", client_id=50, **STAMP, **{"audiorouter.meter": "true"}),
        fakes.port(100, 1, "out"), fakes.port(110, 10, "in"),
        fakes.port(111, 11, "out"), fakes.port(112, 12, "in"),
        fakes.link(901, 111, 112),
    ]
    if mic_linked:
        objects.append(fakes.link(900, 100, 110))
    if old_host_copy:  # the old host during a restart: not ours to judge
        objects.append(fakes.node(20, "ar_listener_ec_mic", "Stream/Input/Audio", client_id=51, **STAMP))
    return Graph(objects)


def listening_conf(*extra):
    """A conf naming the nodes `wired_graph` has, plus any `extra` ones."""
    modules = [
        {"name": "libpipewire-module-echo-cancel",
         "args": {"capture.props": {"node.name": "ar_listener_ec_mic"}}},
        {"name": "libpipewire-module-filter-chain",
         "args": {"capture.props": {"node.name": "ar_listener_mix"},
                  "playback.props": {"node.name": "ar_listener_out"},
                  "filter.graph": {"nodes": []}}},
    ]
    for name in extra:
        modules.append({"name": "libpipewire-module-loopback",
                        "args": {"capture.props": {"node.name": f"{name}_in"},
                                 "playback.props": {"node.name": name}}})
    return json.dumps({"context.modules": modules})


class MissingTest(unittest.TestCase):
    def test_reads_every_props_name_from_the_conf(self):
        self.assertEqual(session.expected_nodes(listening_conf("ar_listener_out2")),
                         {"ar_listener_ec_mic", "ar_listener_mix", "ar_listener_out",
                          "ar_listener_out2_in", "ar_listener_out2"})

    def test_no_conf_expects_nothing(self):
        self.assertEqual(session.expected_nodes(""), set())

    def test_a_vanished_loopback_is_named(self):
        expected = session.expected_nodes(listening_conf("ar_listener_out2"))
        self.assertEqual(session.missing_nodes(expected, HOST, wired_graph()),
                         ["ar_listener_out2", "ar_listener_out2_in"])

    def test_another_hosts_nodes_do_not_count_as_present(self):
        self.assertEqual(session.missing_nodes({"ar_listener_out"}, 777, wired_graph()),
                         ["ar_listener_out"])

    def test_the_real_speakers_conf_shape(self):
        from audiorouter.channels import Channel as Ch
        speakers = Ch("speakers", "Speakers", "alsa_output.x", also=["somewhere"])
        names = session.expected_nodes(speakers.render_config_text())
        self.assertIn("ar_speakers", names)
        self.assertIn("ar_speakers_play2_in", names)


class UnwiredTest(unittest.TestCase):
    def test_a_linked_channel_is_fine_and_meters_never_count(self):
        self.assertEqual(session.unwired_streams("listener", HOST, wired_graph()), [])

    def test_a_capture_with_no_mic_is_named(self):
        self.assertEqual(session.unwired_streams("listener", HOST, wired_graph(mic_linked=False)),
                         ["ar_listener_ec_mic"])

    def test_another_hosts_copy_is_ignored(self):
        self.assertEqual(session.unwired_streams("listener", HOST, wired_graph(old_host_copy=True)), [])


class SelfCheckTest(unittest.TestCase):
    def make(self, graph, paused=False, enabled=True, conf=None):
        self.now = 0.0
        self.restarted, self.lines = [], []
        listener = Channel("listener", "Listener", "", kind=INPUT, enabled=enabled)
        conf = listening_conf() if conf is None else conf
        check = session.SelfCheck(lambda: [listener], self.restarted.append, is_paused=lambda: paused,
                                  log=self.lines.append, clock=lambda: self.now, pid_of=lambda c: HOST,
                                  conf_of=lambda c: conf)
        check.use_graph(graph)
        return check

    def tick(self, check, times=1):
        for _ in range(times):
            self.now += session.SELF_CHECK_S
            check.check()

    def test_broken_on_two_looks_in_a_row_restarts_once_then_waits(self):
        check = self.make(wired_graph(mic_linked=False))
        self.tick(check)
        self.assertEqual(self.restarted, [], "one look could be a link mid-change")
        self.tick(check)
        self.assertEqual(self.restarted, ["listener"])
        self.assertIn("ar_listener_ec_mic not connected", self.lines[0])
        self.tick(check, 5)
        self.assertEqual(self.restarted, ["listener"], "no loop when a restart did not help")
        self.now += session.SELF_CHECK_COOLDOWN_S
        self.tick(check, 2)
        self.assertEqual(self.restarted, ["listener", "listener"])

    def test_a_vanished_output_restarts_the_channel(self):
        check = self.make(wired_graph(), conf=listening_conf("ar_listener_out2"))
        self.tick(check, 2)
        self.assertEqual(self.restarted, ["listener"])
        self.assertIn("ar_listener_out2 (missing)", self.lines[0])

    def test_healthy_paused_or_disabled_is_left_alone(self):
        for kwargs, graph in (({}, wired_graph()), ({"paused": True}, wired_graph(mic_linked=False)),
                              ({"enabled": False}, wired_graph(mic_linked=False))):
            check = self.make(graph, **kwargs)
            self.tick(check, 3)
            self.assertEqual(self.restarted, [], kwargs)

    def test_a_failed_restart_is_logged_not_raised(self):
        check = self.make(wired_graph(mic_linked=False))
        def boom(slug):
            raise RuntimeError("no")
        check._restart = boom
        self.tick(check, 2)
        self.assertIn("failed", self.lines[-1])
