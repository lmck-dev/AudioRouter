"""Noticing a lone WirePlumber restart, which breaks echo cancellation."""

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
