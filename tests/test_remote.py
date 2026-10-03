import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from audiorouter.channels import INPUT, Channel
from audiorouter.config import Config
from audiorouter.effects import Effect
from audiorouter.engine import Engine
from audiorouter.pwgraph import Graph
from audiorouter import remote
from audiorouter.remote import Remote, RemoteError, RemoteServer, RemoteSettings, RemoteSwitch

from .test_engine import live_graph


class RemoteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.tmp.name) / "config.json"
        config = Config(channels=[
            Channel("speakers", "Speakers", "alsa_output.a",
                    effects=[Effect("gain"), Effect("limiter", enabled=False)]),
            Channel("music", "Music", "ar_speakers"),  # plays into Speakers: a group
            Channel("voice", "Voice", "", kind=INPUT),
        ])
        self.engine = Engine(config, path=self.path)
        self.engine.save()
        snapshot = mock.patch.object(Graph, "snapshot", staticmethod(live_graph))
        snapshot.start()
        self.addCleanup(snapshot.stop)
        # Applying would start real PipeWire hosts.
        apply = mock.patch.object(Engine, "apply")
        self.apply = apply.start()
        self.addCleanup(apply.stop)
        self.remote = Remote(self.engine)
        self.remote.notify(live_graph())


class SettingsTest(unittest.TestCase):
    def test_round_trip_and_only_the_user_can_read_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote.json"
            settings = RemoteSettings(enabled=True, port=48000)
            token = settings.ensure_token()
            settings.save(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            again = RemoteSettings.load(path)
            self.assertEqual((again.enabled, again.port, again.token), (True, 48000, token))

    def test_other_networks_are_off_unless_saved_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote.json"
            self.assertFalse(RemoteSettings.load(path).allow_outside)
            RemoteSettings(enabled=True, token="t", allow_outside=True).save(path)
            self.assertTrue(RemoteSettings.load(path).allow_outside)

    def test_a_missing_or_broken_file_means_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote.json"
            self.assertFalse(RemoteSettings.load(path).enabled)
            path.write_text("{not json")
            self.assertFalse(RemoteSettings.load(path).enabled)

    def test_a_new_token_unpairs(self):
        settings = RemoteSettings()
        first = settings.ensure_token()
        self.assertEqual(settings.ensure_token(), first)
        self.assertNotEqual(settings.new_token(), first)
        self.assertGreaterEqual(len(first), 32)

    def test_pairing_url_carries_every_address(self):
        url = remote.pairing_url(["192.168.1.50", "100.79.138.27"], 47800, "a-b_c")
        self.assertEqual(url, "audiorouter://pair?h=192.168.1.50&h=100.79.138.27&p=47800&t=a-b_c")
        self.assertEqual(remote.parse_pairing_url(url),
                         (["192.168.1.50", "100.79.138.27"], 47800, "a-b_c"))
        with self.assertRaises(ValueError):
            remote.parse_pairing_url("https://example.com/pair?p=1&t=x")

    def test_only_the_home_network_is_served(self):
        for address in ("192.168.1.50", "10.0.0.7", "172.16.4.4", "127.0.0.1", "::ffff:192.168.1.9"):
            self.assertTrue(remote.is_home_network(address), address)
        for address in ("100.79.138.27", "8.8.8.8", "169.254.3.4", "nonsense"):
            self.assertFalse(remote.is_home_network(address), address)

    def test_addresses_put_the_default_route_first_and_drop_loopback(self):
        with mock.patch.object(remote, "_default_route_address", return_value="192.168.1.50"), \
             mock.patch.object(remote, "_interface_addresses",
                               return_value=["127.0.0.1", "192.168.1.50", "100.79.138.27", "169.254.3.4"]):
            # Tailscale is left out: the remote is local only...
            self.assertEqual(remote.local_addresses(), ["192.168.1.50"])
            # ...unless other networks are allowed, and then home comes first.
            self.assertEqual(remote.local_addresses(outside=True), ["192.168.1.50", "100.79.138.27"])


class StateTest(RemoteTestCase):
    def test_strips_follow_the_desk_and_hide_companions(self):
        state = self.remote.state()
        slugs = [s["slug"] for s in state["strips"]]
        # Mic channels first, then app channels, then groups; no voice_mix.
        self.assertEqual(slugs, ["voice", "music", "speakers"])
        kinds = {s["slug"]: s["kind"] for s in state["strips"]}
        self.assertEqual(kinds, {"voice": "mic", "music": "apps", "speakers": "group"})

    def test_effects_are_listed_with_their_state(self):
        strip = next(s for s in self.remote.state()["strips"] if s["slug"] == "speakers")
        self.assertEqual([(e["index"], e["on"]) for e in strip["effects"]], [(0, True), (1, False)])

    def test_state_is_json(self):
        json.dumps(self.remote.state())


class CommandTest(RemoteTestCase):
    def saved(self) -> Config:
        return Config.load(self.path)

    def test_fader_is_saved_applied_and_clamped(self):
        self.remote.command({"cmd": "fader", "slug": "music", "db": 99})
        self.assertEqual(self.saved().channel("music").fader_db, 10.0)
        self.apply.assert_called()

    def test_pan_solo_and_effect(self):
        self.remote.command({"cmd": "pan", "slug": "music", "pan": -0.5})
        self.remote.command({"cmd": "solo", "slug": "music", "on": True})
        self.remote.command({"cmd": "effect", "slug": "speakers", "index": 1, "on": True})
        saved = self.saved()
        self.assertEqual(saved.channel("music").pan, -0.5)
        self.assertTrue(saved.channel("music").solo)
        self.assertTrue(saved.channel("speakers").effects[1].enabled)

    def test_a_change_the_window_saved_is_not_lost(self):
        # The window saved a new fader while this process held an older copy.
        other = Engine.load(self.path)
        other.config.channel("speakers").fader_db = -12.0
        other.save()
        os.utime(self.path, ns=(1, 1))  # make sure the stamp differs from ours
        self.remote.command({"cmd": "pan", "slug": "music", "pan": 0.25})
        saved = self.saved()
        self.assertEqual(saved.channel("speakers").fader_db, -12.0)
        self.assertEqual(saved.channel("music").pan, 0.25)

    def test_bypass_off_routes_what_is_playing(self):
        with mock.patch.object(Engine, "route") as route:
            self.remote.command({"cmd": "bypass", "on": True})
            route.assert_not_called()
            self.assertTrue(self.saved().bypass)
            self.remote.command({"cmd": "bypass", "on": False})
            route.assert_called_once()

    def test_bad_commands_say_why(self):
        cases = [
            ({"cmd": "nope"}, "unknown command"),
            ({"cmd": "fader", "slug": "ghost", "db": 0}, "no channel"),
            ({"cmd": "fader", "slug": "voice_mix", "db": 0}, "no channel"),
            ({"cmd": "fader", "slug": "music", "db": "loud"}, "must be a number"),
            ({"cmd": "fader", "slug": "music", "db": True}, "must be a number"),
            ({"cmd": "solo", "slug": "music", "on": 1}, "true or false"),
            ({"cmd": "effect", "slug": "speakers", "index": 5, "on": True}, "has no effect"),
            ({"cmd": "send", "stream": "60", "slug": "music"}, "stream id"),
        ]
        for body, words in cases:
            with self.subTest(body=body), self.assertRaises(RemoteError) as caught:
                self.remote.command(body)
            self.assertIn(words, str(caught.exception))

    def test_trim_goes_to_the_sink_not_the_file(self):
        with mock.patch.object(Engine, "set_channel_volume", return_value=0.5) as volume:
            result = self.remote.command({"cmd": "trim", "slug": "music", "volume": 0.5})
        volume.assert_called_once_with("music", 0.5)
        self.assertEqual(result, {"ok": True, "volume": 0.5})

    def test_a_command_wakes_the_event_streams(self):
        seen = self.remote.revision
        self.remote.command({"cmd": "pan", "slug": "music", "pan": 0.0})
        self.assertNotEqual(self.remote.wait(seen, 0), seen)


class HttpTest(RemoteTestCase):
    TOKEN = "secret-token"

    def setUp(self):
        super().setUp()
        delay = mock.patch.object(remote, "REFUSAL_DELAY_S", 0)
        delay.start()
        self.addCleanup(delay.stop)
        self.server = RemoteServer(self.remote, 0, self.TOKEN, host="127.0.0.1").start()
        self.addCleanup(self.server.stop)

    def request(self, method, path, body=None, token=TOKEN):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")

    def test_outside_the_home_network_is_refused_even_with_the_token(self):
        with mock.patch.object(remote, "is_home_network", return_value=False):
            self.assertEqual(self.request("GET", "/api/state")[0], 403)
            self.assertEqual(self.request("POST", "/api/command", {"cmd": "pan"})[0], 403)

    def test_other_networks_once_allowed_still_need_the_token(self):
        self.server._server.allow_outside = True
        with mock.patch.object(remote, "is_home_network", return_value=False):
            self.assertEqual(self.request("GET", "/api/state")[0], 200)
            self.assertEqual(self.request("GET", "/api/state", token="wrong")[0], 401)

    def test_hello_needs_no_token(self):
        status, body = self.request("GET", "/api/hello", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "audiorouter")

    def test_state_needs_the_token(self):
        self.assertEqual(self.request("GET", "/api/state", token=None)[0], 401)
        self.assertEqual(self.request("GET", "/api/state", token="wrong")[0], 401)
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("strips", body)

    def test_command(self):
        status, body = self.request("POST", "/api/command", {"cmd": "pan", "slug": "music", "pan": 1})
        self.assertEqual((status, body), (200, {"ok": True}))
        status, body = self.request("POST", "/api/command", {"cmd": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("unknown command", body["error"])
        self.assertEqual(self.request("POST", "/api/command", b"[1]")[0], 400)
        self.assertEqual(self.request("POST", "/api/command", {"cmd": "pan"}, token=None)[0], 401)

    def test_events_push_the_state_then_each_change(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/events", headers={"Authorization": f"Bearer {self.TOKEN}"})
        response = connection.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")

        def next_state():
            lines = []
            while True:
                line = response.fp.readline().decode().rstrip("\n")
                if line == "" and lines:
                    break
                if line:
                    lines.append(line)
            self.assertEqual(lines[0], "event: state")
            return json.loads(lines[1][len("data: "):])

        first = next_state()
        self.assertEqual(next(s for s in first["strips"] if s["slug"] == "music")["pan"], 0.0)
        threading.Timer(0.05, self.remote.command,
                        args=({"cmd": "pan", "slug": "music", "pan": 0.5},)).start()
        second = next_state()
        self.assertEqual(next(s for s in second["strips"] if s["slug"] == "music")["pan"], 0.5)


class SwitchTest(RemoteTestCase):
    def test_follows_the_settings_file(self):
        path = Path(self.tmp.name) / "remote.json"
        lines = []
        switch = RemoteSwitch(self.remote, path=path, log=lines.append)
        self.addCleanup(switch.stop)
        switch.check()
        self.assertIsNone(switch.server)
        RemoteSettings(enabled=True, port=0, token="t").save(path)
        switch.check()
        self.assertIsNotNone(switch.server)
        # Allowing other networks takes effect without switching off and on.
        RemoteSettings(enabled=True, port=0, token="t", allow_outside=True).save(path)
        os.utime(path, ns=(1, 1))
        switch.check()
        self.assertTrue(switch.server._server.allow_outside)
        self.assertIn("other networks allowed", lines[-1])
        RemoteSettings(enabled=False, port=0, token="t").save(path)
        os.utime(path, ns=(2, 2))
        switch.check()
        self.assertIsNone(switch.server)
        self.assertEqual(lines[-1], "phone remote: stopped")


if __name__ == "__main__":
    unittest.main()
