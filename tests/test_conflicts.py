"""Another program that moves streams itself makes routing impossible; say so."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from audiorouter.cli import main
from audiorouter.pwgraph import Graph

from . import fakes
from .test_gui import isolated_settings
from .test_engine import live_graph

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


def graph_with_easyeffects():
    graph = live_graph()
    graph.apply([fakes.sink(88, "easyeffects_sink", hardware=False, description="Easy Effects Sink")])
    return graph


class DetectionTest(unittest.TestCase):
    def test_easyeffects_is_recognised_by_its_sink(self):
        self.assertEqual(graph_with_easyeffects().conflicting_routers(), ["EasyEffects"])

    def test_a_graph_without_it_has_no_conflict(self):
        self.assertEqual(live_graph().conflicting_routers(), [])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.path = Path(self.tmp.name) / "config.json"

    def status_output(self, graph):
        out = io.StringIO()
        with mock.patch.object(Graph, "snapshot", staticmethod(lambda: graph)), redirect_stdout(out):
            main(["--config", str(self.path), "status"])
        return out.getvalue()

    def test_status_warns_first_and_says_what_to_do(self):
        text = self.status_output(graph_with_easyeffects())
        self.assertTrue(text.startswith("! WARNING: EasyEffects is running"))
        self.assertIn("Quit EasyEffects", text)

    def test_status_is_quiet_without_it(self):
        self.assertNotIn("WARNING", self.status_output(live_graph()))


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class BannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from audiorouter.config import Config
        from audiorouter.engine import Engine

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = graph_with_easyeffects()
        for target in (
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}),
            mock.patch.object(Graph, "snapshot", staticmethod(lambda: self.graph)),
            mock.patch("audiorouter.gui.monitor.GraphBridge.start", lambda self: False),
            mock.patch("audiorouter.gui.main.MainWindow._start_auto_router", lambda self: None),
            # An empty config offers first-run setup in a modal dialog, which
            # blocks forever under the offscreen platform.
            mock.patch("audiorouter.gui.main.MainWindow._offer_first_run", lambda self: None),
            mock.patch("audiorouter.install.login_service_enabled", return_value=False),
            isolated_settings(self.tmp.name),
        ):
            target.start()
            self.addCleanup(target.stop)
        from audiorouter.gui.main import MainWindow

        self.window = MainWindow(Engine(Config(), path=Path(self.tmp.name) / "c.json"))
        self.addCleanup(self.window.close)

    def test_the_window_shows_a_banner_while_easyeffects_runs_and_drops_it_after(self):
        self.assertFalse(self.window.conflict_banner.isHidden())
        self.assertIn("EasyEffects is running", self.window.conflict_banner.text())
        self.graph = live_graph()  # EasyEffects quit
        self.window.refresh()
        self.assertTrue(self.window.conflict_banner.isHidden())


if __name__ == "__main__":
    unittest.main()
