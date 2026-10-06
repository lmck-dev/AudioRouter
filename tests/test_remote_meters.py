import http.client
import json
import unittest
from unittest import mock

from audiorouter.channels import INPUT, Channel
from audiorouter.meter import Levels, Tap
from audiorouter import remote_meters
from audiorouter.remote_meters import FLOOR_DB, MeterHub


class FakeReader:
    made: list = []

    def __init__(self, tap, on_levels, on_ended=None):
        self.tap, self.on_levels = tap, on_levels
        self.running = False
        FakeReader.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class FakeDriver:
    made: list = []

    def __init__(self, channel):
        self.slug, self.host = channel.slug, channel.pid()
        self.running = False
        FakeDriver.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class HubTestCase(unittest.TestCase):
    def setUp(self):
        FakeReader.made, FakeDriver.made = [], []
        self.music = Channel("music", "Music", "")
        self.voice = Channel("voice", "Voice", "", kind=INPUT)
        self.channels = [self.voice, self.music]
        self.hosts = {"music": 100, "voice": 200}
        self.mics, self.outputs = ["alsa_input.a"], ["alsa_output.a"]
        for channel in self.channels:
            patcher = mock.patch.object(channel, "pid", side_effect=lambda c=channel: self.hosts.get(c.slug))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub = MeterHub(
            lambda: list(self.channels),
            lambda: (list(self.mics), list(self.outputs)),
            file_reader=FakeReader, device_reader=FakeReader, driver=FakeDriver,
            tap_for=self.tap_for,
        )

    def tap_for(self, channel):
        host = self.hosts.get(channel.slug)
        return None if host is None else Tap("Out", (), host, f"/tmp/meters/{host}.3")

    def reader(self, kind, name):
        return self.hub._readers[(kind, name)]


class FollowTest(HubTestCase):
    def test_every_strip_and_device_gets_a_tap_and_mics_a_driver(self):
        self.hub.follow()
        self.assertEqual(sorted(self.hub._readers), [
            ("mics", "alsa_input.a"), ("outputs", "alsa_output.a"),
            ("strips", "music"), ("strips", "voice")])
        self.assertEqual(self.reader("outputs", "alsa_output.a").tap.args, ("--device=alsa_output.a.monitor",))
        # Only the mic channel needs recording to run its chain.
        self.assertEqual([d.slug for d in FakeDriver.made], ["voice"])

    def test_a_restarted_channel_gets_a_new_tap_and_driver(self):
        self.hub.follow()
        old_reader, old_driver = self.reader("strips", "voice"), self.hub._drivers["voice"]
        self.hosts["voice"] = 201
        self.hub.follow()
        self.assertFalse(old_reader.running)
        self.assertFalse(old_driver.running)
        self.assertEqual(self.reader("strips", "voice").tap.host, 201)
        self.assertEqual(self.hub._drivers["voice"].host, 201)

    def test_a_stopped_channel_and_an_unplugged_device_lose_their_taps(self):
        self.hub.follow()
        music = self.reader("strips", "music")
        del self.hosts["music"]
        self.outputs.clear()
        self.hub.follow()
        self.assertFalse(music.running)
        self.assertNotIn(("strips", "music"), self.hub._readers)
        self.assertNotIn(("outputs", "alsa_output.a"), self.hub._readers)


class FrameTest(HubTestCase):
    def test_a_frame_keeps_the_highest_peak_and_the_mean_level(self):
        self.hub.follow()
        feed = self.reader("strips", "music").on_levels
        feed(Levels(0.5, 0.25, 0.01, 0.01))
        feed(Levels(1.0, 0.1, 0.03, 0.01))
        frame = self.hub.make_frame()
        peak_l, peak_r, rms_l, rms_r = frame["strips"]["music"]
        self.assertEqual((peak_l, peak_r), (0.0, -12.0))
        self.assertEqual(rms_l, -17.0)   # mean square 0.02
        self.assertEqual(rms_r, -20.0)

    def test_a_tap_that_read_nothing_is_silent_and_the_next_frame_starts_afresh(self):
        self.hub.follow()
        self.reader("strips", "music").on_levels(Levels(1.0, 1.0, 1.0, 1.0))
        self.hub.make_frame()
        frame = self.hub.make_frame()
        self.assertEqual(frame["strips"]["music"], [FLOOR_DB] * 4)
        self.assertEqual(frame["mics"]["alsa_input.a"], [FLOOR_DB] * 4)

    def test_frames_are_small(self):
        self.hub.follow()
        text = json.dumps(self.hub.make_frame(), separators=(",", ":"))
        self.assertLess(len(text), 300)


class WatchTest(HubTestCase):
    def test_taps_run_only_while_someone_watches(self):
        with mock.patch.object(remote_meters, "FRAME_HZ", 100):
            self.hub.watch()
            self.hub.watch()  # a second phone
            seq, frame = self.hub.wait_frame(-1, 2)
            self.assertIsNotNone(frame)
            self.assertIn("music", frame["strips"])
            self.hub.unwatch()
            self.assertTrue(any(r.running for r in FakeReader.made))  # one phone still watching
            self.hub.unwatch()
        self.assertFalse(any(r.running for r in FakeReader.made))
        self.assertFalse(any(d.running for d in FakeDriver.made))
        self.assertEqual(self.hub.watchers, 0)


class StreamTest(unittest.TestCase):
    """The event stream carries meters only when asked."""

    def setUp(self):
        from audiorouter.remote import RemoteServer

        from .test_remote import RemoteTestCase

        FakeReader.made, FakeDriver.made = [], []
        case = RemoteTestCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.remote = case.remote
        hub = MeterHub(lambda: [], lambda: ([], ["alsa_output.a"]),
                       file_reader=FakeReader, device_reader=FakeReader, driver=FakeDriver)
        self.remote._meters = hub
        fast = mock.patch.object(remote_meters, "FRAME_HZ", 50)
        fast.start()
        self.addCleanup(fast.stop)
        self.server = RemoteServer(self.remote, 0, "tok", host="127.0.0.1").start()
        self.addCleanup(self.server.stop)

    def open_events(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        connection.request("GET", path, headers={"Authorization": "Bearer tok"})
        return connection, connection.getresponse()

    def read_events(self, response, wanted):
        seen, event = [], None
        while len(seen) < wanted:
            line = response.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                seen.append((event, json.loads(line[6:])))
        return seen

    def test_meters_when_asked(self):
        connection, response = self.open_events("/api/events?meters=1")
        self.addCleanup(connection.close)
        seen = self.read_events(response, 3)
        self.assertEqual(seen[0][0], "state")
        self.assertEqual([e for e, _ in seen[1:]], ["meters", "meters"])
        self.assertIn("alsa_output.a", seen[1][1]["outputs"])

    def test_no_meters_unless_asked(self):
        connection, response = self.open_events("/api/events")
        self.addCleanup(connection.close)
        self.assertEqual(self.read_events(response, 1)[0][0], "state")
        self.assertEqual(self.remote.meters.watchers, 0)

    def test_the_taps_stop_when_the_phone_goes(self):
        import time

        connection, response = self.open_events("/api/events?meters=1")
        self.read_events(response, 2)
        self.assertEqual(self.remote.meters.watchers, 1)
        response.close()
        connection.close()
        # The count drops first, then the tap thread stops its readers: wait for both
        # (a busy build container caught the readers mid-stop).
        deadline = time.monotonic() + 3
        while ((self.remote.meters.watchers or any(r.running for r in FakeReader.made))
               and time.monotonic() < deadline):
            time.sleep(0.02)
        self.assertEqual(self.remote.meters.watchers, 0)
        self.assertFalse(any(r.running for r in FakeReader.made))


if __name__ == "__main__":
    unittest.main()
