"""Per-channel volume: read from the sink, written with wpctl, shown in the window."""

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from audiorouter.channels import Channel, ChannelError
from audiorouter.cli import main
from audiorouter.pwgraph import Graph

from . import fakes
from .test_gui import isolated_settings
from .test_engine import EngineTestCase, live_graph

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


def graph_with_volume(gain=0.125, mute=False):
    """live_graph, with the speakers channel's sink at a given linear gain."""
    graph = live_graph()
    sink = fakes.sink(51, "ar_speakers", serial=510, hardware=False, description="Speakers",
                      **{"audiorouter.channel": "speakers"})
    sink["info"]["params"] = {"Props": [
        {"volume": 1.0},  # nodes list several Props; only one has channelVolumes
        {"volume": 1.0, "mute": mute, "channelVolumes": [gain, gain]},
    ]}
    graph.apply([sink])
    return graph


class NodeVolumeTest(unittest.TestCase):
    def test_volume_is_the_cube_root_of_the_linear_gain_like_every_desktop_slider(self):
        node = graph_with_volume(0.125).node(51)
        self.assertAlmostEqual(node.volume, 0.5)
        self.assertFalse(node.muted)

    def test_mute_is_read(self):
        self.assertTrue(graph_with_volume(mute=True).node(51).muted)

    def test_a_node_without_volume_reports_none(self):
        node = live_graph().node(60)
        self.assertIsNone(node.volume)
        self.assertIsNone(node.muted)


class ChannelVolumeTest(EngineTestCase):
    def run_wpctl(self, action):
        with mock.patch("audiorouter.channels.require_tools"), \
             mock.patch("audiorouter.channels.subprocess.run") as run:
            result = action()
        return result, run.call_args.args[0]

    def test_volume_is_set_on_the_channels_sink_by_id(self):
        channel = self.engine.channel("speakers")
        volume, argv = self.run_wpctl(lambda: channel.set_volume(0.8, live_graph()))
        self.assertEqual(argv, ["wpctl", "set-volume", "51", "0.8000"])
        self.assertEqual(volume, 0.8)

    def test_volume_is_kept_within_zero_and_150_percent(self):
        channel = self.engine.channel("speakers")
        self.assertEqual(self.run_wpctl(lambda: channel.set_volume(9, live_graph()))[1][-1], "1.5000")
        self.assertEqual(self.run_wpctl(lambda: channel.set_volume(-1, live_graph()))[1][-1], "0.0000")

    def test_mute(self):
        channel = self.engine.channel("speakers")
        _, argv = self.run_wpctl(lambda: channel.set_muted(True, live_graph()))
        self.assertEqual(argv, ["wpctl", "set-mute", "51", "1"])

    def test_a_channel_that_is_not_running_has_no_volume_to_set(self):
        with mock.patch("audiorouter.channels.require_tools"), self.assertRaises(ChannelError):
            Channel("ghost", "Ghost", "x").set_volume(0.5, live_graph())

    def test_status_reports_volume_and_mute(self):
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph_with_volume(0.125, True))):
            (entry,) = self.engine.status()["channels"]
        self.assertAlmostEqual(entry["volume"], 0.5)
        self.assertTrue(entry["muted"])

    def test_volume_is_not_written_to_the_config(self):
        # The sink holds it and WirePlumber restores it; a second copy in our
        # config would fight the desktop's own volume control.
        with mock.patch("audiorouter.channels.require_tools"), mock.patch("audiorouter.channels.subprocess.run"):
            self.engine.set_channel_volume("speakers", 0.3)
        self.assertNotIn("volume", self.path.read_text() if self.path.exists() else "")


class CliVolumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in (
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}),
            mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph_with_volume(0.125))),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.config = str(Path(self.tmp.name) / "config.json")

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", self.config, *args])
        return code, out.getvalue()

    def test_volume_takes_a_percentage(self):
        self.cli("channel", "add", "speakers", "--device", "alsa_output.a")
        with mock.patch("audiorouter.channels.require_tools"), \
             mock.patch("audiorouter.channels.subprocess.run") as run:
            code, out = self.cli("channel", "volume", "speakers", "80%")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0][-1], "0.8000")
        self.assertIn("80%", out)

    def test_status_shows_the_volume(self):
        self.cli("channel", "add", "speakers", "--device", "alsa_output.a")
        with mock.patch("audiorouter.channels.Channel.is_running", return_value=True):
            _, out = self.cli("status")
        self.assertIn("50%", out)


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class WindowVolumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from audiorouter.config import Config
        from audiorouter.engine import Engine
        from audiorouter.gui.main import MainWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = graph_with_volume(0.125)
        for target in (
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}),
            mock.patch.object(Graph, "snapshot", staticmethod(lambda: self.graph)),
            mock.patch("audiorouter.gui.monitor.GraphBridge.start", lambda self: False),
            mock.patch("audiorouter.gui.main.MainWindow._start_auto_router", lambda self: None),
            mock.patch("audiorouter.gui.main.MainWindow._offer_first_run", lambda self: None),
            mock.patch("audiorouter.install.login_service_enabled", return_value=False),
            isolated_settings(self.tmp.name),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.engine = Engine(Config(channels=[
            Channel("speakers", "Speakers", "alsa_output.a"),
            Channel("phones", "Headphones", "alsa_output.b"),
        ]), path=Path(self.tmp.name) / "c.json")
        self.window = MainWindow(self.engine)
        self.addCleanup(self.window.close)
        self.window.select_channel(self.engine.config.channels[0].slug)
        self.panel = self.window.channel_panel

    def test_the_slider_shows_the_sinks_volume(self):
        self.assertEqual(self.panel.volume.value(), 50)
        self.assertEqual(self.panel.volume_label.text(), "50%")
        self.assertTrue(self.panel.volume.isEnabled())

    def test_a_channel_that_is_not_running_has_its_volume_greyed_out(self):
        self.window.select_channel(self.engine.config.channels[1].slug)
        self.assertFalse(self.panel.volume.isEnabled())
        self.assertFalse(self.panel.mute.isEnabled())

    def test_dragging_sends_a_throttled_update_and_never_restarts_audio(self):
        with mock.patch.object(type(self.engine), "set_channel_volume") as setter, \
             mock.patch.object(type(self.engine), "apply") as apply:
            for value in (40, 35, 30):
                self.panel.volume.setValue(value)
            self.assertTrue(self.window._volume_timer.isActive())
            self.window._write_volume()
        setter.assert_called_once_with("speakers", 0.30)
        apply.assert_not_called()

    def test_mute_goes_straight_to_the_engine(self):
        with mock.patch.object(type(self.engine), "set_channel_muted") as setter:
            self.panel.mute.setChecked(True)
        setter.assert_called_once_with("speakers", True)

    def test_a_change_made_elsewhere_moves_the_slider(self):
        self.graph = graph_with_volume(0.729)  # 90% set from the desktop
        self.window.refresh()
        self.assertEqual(self.panel.volume.value(), 90)

    def test_a_stale_reading_right_after_a_drag_does_not_yank_the_slider_back(self):
        with mock.patch.object(type(self.engine), "set_channel_volume"):
            self.panel.volume.setValue(20)
        self.window.refresh()  # still reports the old 50%
        self.assertEqual(self.panel.volume.value(), 20)

    def test_a_level_above_100_percent_is_shown_not_clamped(self):
        self.graph = graph_with_volume(1.2 ** 3)
        self.window.refresh()
        self.assertEqual(self.panel.volume.value(), 120)

    def test_switching_channel_right_after_a_drag_still_shows_the_new_channels_volume(self):
        self.graph.apply([fakes.sink(52, "ar_phones", serial=520, hardware=False,
                                     **{"audiorouter.channel": "phones"})])
        obj = self.graph._objects[52]
        obj["info"]["params"] = {"Props": [{"mute": False, "channelVolumes": [0.343, 0.343]}]}
        with mock.patch.object(type(self.engine), "set_channel_volume"):
            self.panel.volume.setValue(20)
        self.window.refresh()
        self.window.select_channel(self.engine.config.channels[1].slug)
        self.assertEqual(self.panel.volume.value(), 70)


if __name__ == "__main__":
    unittest.main()
