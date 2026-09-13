import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter.channels import Channel, ChannelError, runtime_dir, validate_slug
from audiorouter.effects import Effect
from audiorouter.pwgraph import Graph

from . import fakes


class SlugTest(unittest.TestCase):
    def test_ordinary_names_are_accepted(self):
        for slug in ("speakers", "hdmi-2", "desk_headphones", "a"):
            self.assertEqual(validate_slug(slug), slug)

    def test_names_that_would_break_the_graph_are_refused(self):
        for slug in ("", "Speakers", "my channel", "-lead", "x" * 41, "sp/eakers"):
            with self.assertRaises(ChannelError):
                validate_slug(slug)

    def test_a_channel_names_itself_when_asked_to(self):
        self.assertEqual(Channel("desk_speakers", "", "dev").name, "Desk Speakers")


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.channel = Channel("headphones", "Headphones", "alsa_output.usb")

    def args(self, channel=None):
        config = (channel or self.channel).render_config()
        return config["context.modules"][-1]["args"]

    def test_the_sink_is_named_and_prefixed(self):
        self.assertEqual(self.channel.sink_name, "ar_headphones")
        self.assertEqual(self.channel.playback_name, "ar_headphones_out")

    def test_the_capture_side_is_a_sink_the_desktop_can_see(self):
        capture = self.args()["capture.props"]
        self.assertEqual(capture["media.class"], "Audio/Sink")
        self.assertEqual(capture["node.name"], "ar_headphones")
        self.assertEqual(capture["node.description"], "Headphones")

    def test_both_of_our_nodes_are_stamped_as_ours(self):
        args = self.args()
        self.assertEqual(args["capture.props"]["audiorouter.channel"], "headphones")
        self.assertEqual(args["playback.props"]["audiorouter.channel"], "headphones")

    def test_the_output_is_bound_to_the_chosen_device(self):
        self.assertEqual(self.args()["playback.props"]["target.object"], "alsa_output.usb")

    def test_without_a_device_the_output_is_left_to_follow_the_default(self):
        self.assertNotIn("target.object", self.args(Channel("x", "X", ""))["playback.props"])

    def test_the_graph_declares_exactly_one_input_and_one_output_per_side(self):
        # Implicit ports let a mixer's seven spare inputs make the filter
        # unstartable; the channel then runs, links up, and is silent.
        channel = Channel("x", "X", "dev", effects=[Effect("gain", {"gain_db": -3})])
        graph = self.args(channel)["filter.graph"]
        self.assertEqual(graph["inputs"], ["sw0_in_l:In", "sw0_in_r:In"])
        self.assertEqual(graph["outputs"], ["sw0_switch_l:Out", "sw0_switch_r:Out"])

    def test_the_rendered_config_is_valid_json(self):
        self.assertEqual(
            json.loads(self.channel.render_config_text())["context.modules"][-1]["name"],
            "libpipewire-module-filter-chain",
        )

    def test_the_config_changes_when_an_effect_does(self):
        before = self.channel.render_config_text()
        self.channel.effects.append(Effect("gain", {"gain_db": -3}))
        self.assertNotEqual(before, self.channel.render_config_text())

    def test_round_trip_through_a_dict(self):
        self.channel.effects.append(Effect("lowpass", {"frequency": 800.0}))
        again = Channel.from_dict(self.channel.to_dict())
        self.assertEqual(again.to_dict(), self.channel.to_dict())


class ProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.channel = Channel("x", "X", "dev")

    def test_files_live_under_the_runtime_directory(self):
        self.assertTrue(str(self.channel.pid_path).startswith(self.tmp.name))
        self.assertEqual(runtime_dir().name, "audiorouter")

    def test_no_pid_file_means_not_running(self):
        self.assertFalse(self.channel.is_running())

    def test_a_pid_that_is_not_ours_is_ignored(self):
        # Guards against pid reuse: some unrelated process inheriting the
        # number must never be mistaken for a channel, or we would kill it.
        self.channel.pid_path.write_text(str(os.getpid()))
        self.assertIsNone(self.channel.pid())

    def test_a_dead_pid_is_ignored(self):
        self.channel.pid_path.write_text("999999")
        self.assertIsNone(self.channel.pid())

    def test_rubbish_in_the_pid_file_is_ignored(self):
        self.channel.pid_path.write_text("not a pid")
        self.assertIsNone(self.channel.pid())

    def test_a_channel_is_found_again_when_its_pid_file_is_lost(self):
        # A lost pid file used to mean a process nothing could ever stop,
        # holding a sink the user could not get rid of.
        with mock.patch("audiorouter.channels.running_hosts", return_value={"x": 4321}):
            self.assertEqual(self.channel.pid(), 4321)
        self.assertEqual(self.channel.pid_path.read_text(), "4321")

    def test_a_stale_pid_file_is_replaced_by_what_is_really_running(self):
        self.channel.pid_path.write_text("999999")
        with mock.patch("audiorouter.channels.running_hosts", return_value={"x": 4321}):
            self.assertEqual(self.channel.pid(), 4321)

    def test_nothing_is_adopted_when_nothing_is_running(self):
        with mock.patch("audiorouter.channels.running_hosts", return_value={}):
            self.assertIsNone(self.channel.pid())

    def test_stopping_something_that_is_not_running_says_so(self):
        self.assertFalse(self.channel.stop())

    def test_a_channel_needing_a_missing_plugin_refuses_to_start(self):
        channel = Channel("x", "X", "dev", effects=[Effect("limiter")])
        with mock.patch("audiorouter.plugins.available_loaders", return_value=frozenset({"builtin"})):
            self.assertTrue(channel.missing_plugins())
            with self.assertRaises(ChannelError):
                channel.start()


class ProcessDiscoveryTest(unittest.TestCase):
    """running_hosts() reads /proc, so it is tested against a fake one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proc = Path(self.tmp.name) / "proc"
        self.proc.mkdir()
        self.conf_dir = runtime_dir()

    def add_process(self, pid: int, *argv: str) -> None:
        entry = self.proc / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def hosts(self):
        with mock.patch("audiorouter.channels.Path", side_effect=lambda p:
                        self.proc if str(p) == "/proc" else Path(p)):
            from audiorouter.channels import running_hosts
            return running_hosts()

    def test_our_host_processes_are_recognised_by_their_conf_path(self):
        self.add_process(101, "pipewire", "-c", str(self.conf_dir / "speakers.conf"))
        self.assertEqual(self.hosts(), {"speakers": 101})

    def test_the_users_own_pipewire_daemon_is_not_ours(self):
        self.add_process(102, "/usr/bin/pipewire")
        self.assertEqual(self.hosts(), {})

    def test_another_programs_pipewire_config_is_not_ours(self):
        self.add_process(103, "pipewire", "-c", "/etc/pipewire/pipewire.conf")
        self.assertEqual(self.hosts(), {})

    def test_a_process_that_exits_mid_scan_is_skipped(self):
        entry = self.proc / "104"
        entry.mkdir()  # no cmdline file at all
        self.assertEqual(self.hosts(), {})


class StatusTest(unittest.TestCase):
    def test_a_missing_output_device_is_reported(self):
        graph = Graph([fakes.sink(50, "alsa_output.present", serial=500)])
        self.assertTrue(Channel("a", "A", "alsa_output.present").device_present(graph))
        self.assertFalse(Channel("b", "B", "alsa_output.unplugged").device_present(graph))

    def test_a_channel_may_target_a_virtual_sink(self):
        graph = Graph([fakes.sink(50, "other_virtual", serial=500, hardware=False)])
        self.assertTrue(Channel("a", "A", "other_virtual").device_present(graph))


if __name__ == "__main__":
    unittest.main()


class ProcessLifetimeTest(unittest.TestCase):
    def test_a_stopped_child_is_not_alive_even_before_anyone_reaps_it(self):
        # Regression: kill(pid, 0) succeeds on a zombie, so every stop waited
        # four seconds and then SIGKILLed a process that had already exited.
        import subprocess
        import time as _time

        from audiorouter.channels import _pid_alive, _terminate

        proc = subprocess.Popen(["sleep", "30"])
        self.assertTrue(_pid_alive(proc.pid))
        started = _time.monotonic()
        self.assertTrue(_terminate(proc.pid))
        self.assertLess(_time.monotonic() - started, 1.0)
        self.assertFalse(_pid_alive(proc.pid))


class SeamlessRestartTest(unittest.TestCase):
    """A restart starts the new host before stopping the old one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.channel = Channel("x", "X", "dev")
        self.channel.config_path.write_text("old conf")
        self.events = []
        for target in (
            mock.patch("audiorouter.channels.require_tools"),
            mock.patch.object(Channel, "pid", return_value=111),
            mock.patch("audiorouter.channels.subprocess.Popen",
                       side_effect=lambda *a, **k: mock.Mock(pid=222)),
            mock.patch("audiorouter.channels._terminate",
                       side_effect=lambda pid, *a: self.events.append(("stop", pid))),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_streams_are_handed_over_before_the_old_host_stops(self):
        sink = mock.Mock(name="new sink")
        with mock.patch.object(Channel, "_await_sink", return_value=sink):
            pid = self.channel.start(handover=lambda s: self.events.append(("handover", s)))
        self.assertEqual(pid, 222)
        self.assertEqual(self.events, [("handover", sink), ("stop", 111)])
        self.assertEqual(self.channel.pid_path.read_text(), "222")

    def test_a_new_host_that_fails_leaves_the_old_one_playing(self):
        with mock.patch.object(Channel, "_await_sink", side_effect=ChannelError("died")):
            with self.assertRaises(ChannelError):
                self.channel.start(handover=lambda s: self.events.append(("handover", s)))
        self.assertEqual(self.events, [("stop", 222)])  # only the new one
        self.assertEqual(self.channel.pid_path.read_text(), "111")
        self.assertEqual(self.channel.config_path.read_text(), "old conf")

    def test_a_failed_handover_still_stops_the_old_host(self):
        def broken(sink):
            raise RuntimeError("move failed")

        with mock.patch.object(Channel, "_await_sink", return_value=mock.Mock()):
            self.channel.start(handover=broken)
        self.assertEqual(self.events, [("stop", 111)])

    def test_while_two_hosts_share_the_sink_name_the_pid_file_decides(self):
        from audiorouter.pwgraph import Graph

        from . import fakes

        graph = Graph([
            fakes.sink(70, "ar_x", hardware=False, client_id=7),
            fakes.sink(71, "ar_x", hardware=False, client_id=8),
            fakes.client(7, 111),
            fakes.client(8, 222),
        ])
        with mock.patch.object(Channel, "pid", return_value=222):
            self.assertEqual(self.channel.sink_node(graph).id, 71)
