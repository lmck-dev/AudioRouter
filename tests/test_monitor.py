"""A window's graph feed survives a PipeWire restart."""

import subprocess
import threading
import time
import unittest
from unittest import mock

from audiorouter.pwgraph import GraphMonitor, PwError

from .test_gui import QApplication


class FastFailTest(unittest.TestCase):
    def test_start_gives_up_at_once_when_pw_dump_ends(self):
        # PipeWire down: pw-dump exits straight away. A window must not freeze
        # for the whole wait while it retries.
        real = subprocess.Popen
        with mock.patch("audiorouter.pwgraph.require_tools"), \
                mock.patch("audiorouter.pwgraph.subprocess.Popen",
                           lambda *a, **k: real(["true"], stdout=subprocess.PIPE, text=True)):
            began = time.monotonic()
            with self.assertRaisesRegex(PwError, "not answering"):
                GraphMonitor().start(wait=10.0)
        self.assertLess(time.monotonic() - began, 1.0)


class FakeMonitor:
    """A GraphMonitor whose feed a test can end, and whose start can fail."""

    made: list = []
    fail = False

    def __init__(self, on_change=None):
        self.ended = threading.Event()
        self.graph = object()
        self.stopped = False
        FakeMonitor.made.append(self)

    def start(self, wait=10.0):
        if FakeMonitor.fail:
            raise PwError("PipeWire is not answering")

    def stop(self):
        self.stopped = True


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class ReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        FakeMonitor.made, FakeMonitor.fail = [], False
        patcher = mock.patch("audiorouter.gui.monitor.GraphMonitor", FakeMonitor)
        patcher.start()
        self.addCleanup(patcher.stop)
        from audiorouter.gui.monitor import GraphBridge
        self.bridge = GraphBridge()
        self.reconnected = []
        self.bridge.reconnected.connect(lambda: self.reconnected.append(True))
        self.assertTrue(self.bridge.start())
        self.addCleanup(self.bridge.stop)

    def test_a_live_feed_is_left_alone(self):
        self.bridge._check_feed()
        self.assertEqual(len(FakeMonitor.made), 1)
        self.assertEqual(self.reconnected, [])

    def test_an_ended_feed_reconnects_with_a_fresh_graph(self):
        old = FakeMonitor.made[0]
        old.ended.set()  # PipeWire restarted
        self.bridge._check_feed()
        self.assertTrue(old.stopped)
        self.assertIs(self.bridge.graph, FakeMonitor.made[1].graph)
        self.assertTrue(self.bridge.running)
        self.assertEqual(self.reconnected, [True])

    def test_while_pipewire_is_down_it_keeps_trying(self):
        FakeMonitor.made[0].ended.set()
        FakeMonitor.fail = True
        self.bridge._check_feed()
        self.assertFalse(self.bridge.running)
        self.assertEqual(self.reconnected, [])
        FakeMonitor.fail = False  # back up
        self.bridge._check_feed()
        self.assertTrue(self.bridge.running)
        self.assertEqual(self.reconnected, [True])


if __name__ == "__main__":
    unittest.main()
