"""Each app's own volume in Playing now (owner, 26 Oct 2026)."""

from unittest import mock

from audiorouter.engine import Engine, EngineError
from audiorouter.gui.streams_panel import VOLUME_COLUMN, VolumeCell
from audiorouter.pwgraph import Graph

from . import fakes
from .test_engine import EngineTestCase, live_graph
from .test_gui import GuiTestCase


def quiet_graph():
    """Firefox left at 5% (0.05 on the slider = 0.000125 linear), as Chrome once was."""
    stream = fakes.stream(60, "firefox", serial=600, **{"application.name": "firefox"})
    stream["info"]["params"] = {"Props": [{"channelVolumes": [0.05 ** 3, 0.05 ** 3], "mute": False}]}
    return Graph([
        fakes.sink(50, "alsa_output.a", serial=500, description="Built-in Audio"),
        stream,
        fakes.port(80, 60, "out"),
        fakes.port(81, 50, "in"),
        fakes.link(90, 80, 81),
    ])


class EngineTest(EngineTestCase):
    def test_status_carries_each_apps_own_volume(self):
        with mock.patch.object(Graph, "snapshot", staticmethod(quiet_graph)):
            stream = self.engine.status()["streams"][0]
        self.assertEqual((stream["volume"], stream["muted"]), (0.05, False))

    def test_an_apps_volume_and_mute_are_set_on_its_own_node(self):
        with mock.patch.object(Graph, "snapshot", staticmethod(live_graph)), \
             mock.patch("audiorouter.engine._wpctl") as wpctl:
            self.assertEqual(self.engine.set_stream_volume(60, 9.0), 1.5)  # capped
            self.engine.set_stream_muted(60, True)
        self.assertEqual([c.args for c in wpctl.call_args_list],
                         [("set-volume", "60", "1.5000"), ("set-mute", "60", "1")])

    def test_only_an_app_can_be_set(self):
        # 50 is a device, 61 a channel's own stream: neither is an app.
        for node_id in (50, 61, 999):
            with mock.patch.object(Graph, "snapshot", staticmethod(live_graph)), \
                 mock.patch("audiorouter.engine._wpctl") as wpctl, \
                 self.assertRaises(EngineError):
                self.engine.set_stream_volume(node_id, 0.5)
            wpctl.assert_not_called()


class PanelTest(GuiTestCase):
    def cell(self):
        return self.window.streams_panel.table.cellWidget(0, VOLUME_COLUMN)

    def refresh(self, graph):
        with mock.patch.object(Graph, "snapshot", staticmethod(graph)):
            self.window.streams_panel.refresh(self.engine.status())

    def test_a_quiet_app_is_marked_low(self):
        self.refresh(quiet_graph)
        cell = self.cell()
        self.assertIsInstance(cell, VolumeCell)
        self.assertEqual(cell.slider.value(), 5)
        self.assertEqual(cell.label.text(), "5%")
        self.assertIn("turned down", cell.toolTip())

    def test_moving_the_slider_sets_that_apps_volume(self):
        self.refresh(quiet_graph)
        with mock.patch.object(Engine, "set_stream_volume") as set_volume:
            self.cell().slider.setValue(80)
            self.window._stream_volume_timer.timeout.emit()
        set_volume.assert_called_once_with(60, 0.8)
        self.assertNotIn("turned down", self.cell().toolTip())

    def test_a_refresh_keeps_the_cell_and_does_not_undo_a_fresh_move(self):
        self.refresh(quiet_graph)
        cell = self.cell()
        with mock.patch.object(Engine, "set_stream_volume"):
            cell.slider.setValue(80)
        self.refresh(quiet_graph)   # the graph has not caught up yet
        self.assertIs(self.cell(), cell)
        self.assertEqual(cell.slider.value(), 80)

    def test_mute(self):
        self.refresh(quiet_graph)
        with mock.patch.object(Engine, "set_stream_muted") as set_muted:
            self.cell().mute.setChecked(True)
        set_muted.assert_called_once_with(60, True)
        self.assertEqual(self.cell().label.text(), "muted 5%")
