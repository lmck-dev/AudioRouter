"""The phone remote: a small HTTP API served by the login service.

The Android app (a separate, paid Play app; owner, 3 Oct 2026) drives the desk
through this. It lives in the login service (`audiorouter watch`) because that
is the one process that runs for the whole session; with the window closed
there would otherwise be nothing to answer the phone.

Off until switched on (`audiorouter remote on`). Every request but `/api/hello`
carries the pairing token as `Authorization: Bearer <token>`; the token is made
once and kept in `remote.json` beside the config, readable only by the user.
Local only unless `allow_outside` is set: requests from outside the home
network (Tailscale included) are refused before the token is even looked at. Plain HTTP: the token is the lock, so whoever can read the
LAN can read it. TLS with the certificate pinned through the pairing code is a
planned step, not done.

Endpoints (JSON in and out):

    GET  /api/hello    who this is; no token needed, so the phone can check an address

Pairing link (QR code in the window, or `audiorouter remote`):
`audiorouter://pair?h=<address>&h=<address>&p=<port>&t=<token>`
    GET  /api/state    the whole desk (see `Remote.state`)
    POST /api/command  one change: {"cmd": "fader", "slug": "music", "db": -6}
    GET  /api/events   Server-Sent Events: `state` whenever the desk changes

One writer: the window saves every edit the moment it is made, so a change
from the phone reloads the settings file first, edits, saves and applies. The
window adopts the file when it changes on disk.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .channels import MAX_VOLUME, ChannelError
from .config import ConfigError, config_dir, desk_order, kind_label
from .effects import FADER_MAX_DB, FADER_OFF_DB, EffectError
from .engine import Engine, EngineError, MoveResult
from .pwgraph import Graph, PwError

#: Unassigned by IANA and clear of the usual development ports.
DEFAULT_PORT = 47800
#: How long an event stream waits before a keep-alive comment.
KEEPALIVE_S = 15.0
#: A wrong token waits this long before its refusal, to slow guessing.
REFUSAL_DELAY_S = 0.5
#: Requests larger than this are refused unread.
MAX_BODY = 16 * 1024

#: The errors a bad command can raise; each becomes a 400 with its message.
COMMAND_ERRORS = (ChannelError, ConfigError, EffectError, EngineError, PwError,
                  KeyError, ValueError, TypeError, OSError)


class RemoteError(ValueError):
    """A command the phone sent that cannot be carried out."""


def settings_path() -> Path:
    return config_dir() / "remote.json"


@dataclass
class RemoteSettings:
    """Whether the remote is on, its port and its pairing token.

    Kept out of config.json on purpose: the window rewrites that file from
    its own fields and would drop these, and the token should not travel in a
    file people paste into bug reports.
    """

    enabled: bool = False
    port: int = DEFAULT_PORT
    token: str = ""
    #: Answer phones on other networks too (a VPN such as Tailscale). Off:
    #: local only, the owner's default (3 Oct 2026), for the rare user who
    #: needs it to switch on.
    allow_outside: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> RemoteSettings:
        target = path or settings_path()
        try:
            data = json.loads(target.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            port=int(data.get("port", DEFAULT_PORT)),
            token=str(data.get("token", "")),
            allow_outside=bool(data.get("allow_outside", False)),
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(self), handle, indent=2)
        os.replace(temporary, target)
        return target

    def ensure_token(self) -> str:
        if not self.token:
            self.new_token()
        return self.token

    def new_token(self) -> str:
        """A fresh token. Every paired phone must pair again."""
        self.token = secrets.token_urlsafe(24)
        return self.token


def settings_stamp(path: Path | None = None) -> tuple[int, int] | None:
    try:
        info = (path or settings_path()).stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _interface_addresses() -> list[str]:
    """The IPv4 address of every network interface (Linux: SIOCGIFADDR)."""
    import fcntl
    import struct

    found = []
    for _, name in socket.if_nameindex():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(probe.fileno(), 0x8915,  # SIOCGIFADDR
                                 struct.pack("256s", name[:15].encode()))
            found.append(socket.inet_ntoa(packed[20:24]))
        except OSError:
            pass  # an interface with no IPv4 address
        finally:
            probe.close()
    return found


def _default_route_address() -> str | None:
    """The address the default route leaves from (a UDP connect sends nothing)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def is_home_network(address: str) -> bool:
    """A home-network (or this computer's own) address: the only ones served.

    The remote is local only (owner, 3 Oct 2026): nobody needs to move a
    fader without being at the desk. Private ranges and loopback qualify;
    Tailscale's 100.64.0.0/10 and anything public do not, token or no token.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False  # carrier-grade NAT space, which Tailscale uses
    return ip.is_private and not ip.is_link_local


def local_addresses(outside: bool = False) -> list[str]:
    """The addresses a phone can reach this computer on, best first.

    The default route's address first, then any other home-network interface.
    With `outside`, every other interface follows (a Tailscale address, say).
    Loopback and link-local addresses are always left out.
    """
    found: list[str] = []
    for address in [_default_route_address(), *_interface_addresses()]:
        if (not address or address in found or address.startswith("127.")
                or address.startswith("169.254.")):
            continue
        if is_home_network(address) or outside:
            found.append(address)
    # Home-network addresses first: they are what a phone at the desk uses.
    return sorted(found, key=lambda a: not is_home_network(a))


def pairing_url(addresses: list[str], port: int, token: str) -> str:
    """What the phone's pairing screen reads, from a QR code or typed.

    Every home-network address goes in, best first (a desk on both wired and
    wireless has two); the phone tries each.
    """
    query = urllib.parse.urlencode([*(("h", a) for a in addresses), ("p", port), ("t", token)])
    return f"audiorouter://pair?{query}"


def parse_pairing_url(url: str) -> tuple[list[str], int, str]:
    """The reverse of `pairing_url`, for tests and the command line."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "audiorouter" or parsed.netloc != "pair":
        raise ValueError(f"not a pairing link: {url!r}")
    query = urllib.parse.parse_qs(parsed.query)
    return query.get("h", []), int(query["p"][0]), query["t"][0]


class Remote:
    """What the phone can see and change: the engine, one request at a time."""

    def __init__(
        self,
        engine: Engine,
        on_sent: Callable[[int], None] | None = None,
    ) -> None:
        self.engine = engine
        #: Told about a stream moved by hand, so the auto router leaves it there.
        self.on_sent = on_sent
        self.lock = threading.RLock()
        self._graph: Graph | None = None
        self._revision = 0
        self._changed = threading.Condition()

    # -- change notification ------------------------------------------------

    @property
    def revision(self) -> int:
        return self._revision

    def notify(self, graph: Graph | None = None) -> None:
        """Something may have changed: a graph event, or a command."""
        if graph is not None:
            self._graph = graph
        with self._changed:
            self._revision += 1
            self._changed.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        """Block until the revision moves past `seen`, or the timeout ends."""
        with self._changed:
            self._changed.wait_for(lambda: self._revision != seen, timeout)
            return self._revision

    # -- the desk -------------------------------------------------------------

    def _current_graph(self) -> Graph:
        if self._graph is not None:
            return self._graph
        return self.engine.graph(refresh=True)

    def state(self) -> dict[str, Any]:
        """The desk as the phone draws it: left to right, as the window does."""
        with self.lock:
            graph = self._current_graph()
            config = self.engine.config
            channels, groups = desk_order(config)
            sinks = self.engine.sink_map(graph)
            sources = self.engine.source_map(graph)
            strips = []
            for channel in channels + groups:
                node = channel.sink_node(graph)
                strips.append({
                    "slug": channel.slug,
                    "name": channel.name,
                    "kind": ("mic" if channel.is_input else "group" if channel in groups
                             else "cable" if channel.recordable else "apps"),
                    "label": kind_label(config, channel),
                    "enabled": channel.enabled,
                    "running": node is not None,
                    "trim": node.volume if node else None,
                    "muted": bool(node.muted) if node else False,
                    "fader_db": channel.fader_db,
                    "pan": channel.pan,
                    "solo": channel.solo,
                    "cut": channel.solo_cut,
                    "effects": [{"index": i, "label": e.label, "on": e.enabled}
                                for i, e in enumerate(channel.effects)],
                })
            streams = []
            for stream in graph.app_streams() + graph.app_recorders():
                recording = stream.is_input_stream
                current = (graph.source_of_stream(stream.id) if recording
                           else graph.sink_of_stream(stream.id))
                targets = sources if recording else sinks
                slug = next((s for s, n in targets.items() if current and n.id == current.id), None)
                streams.append({"id": stream.id, "app": stream.app_name,
                                "title": stream.media_name, "recording": recording,
                                "channel": slug})
            return {
                "version": __version__,
                "bypass": config.bypass,
                "mics": [_device(n) for n in graph.input_devices()],
                "strips": strips,
                "outputs": [_device(n) for n in graph.devices()],
                "streams": streams,
                # Where a playing app can be sent, and where a recorder can listen.
                "play_targets": [{"slug": s, "name": config.channel(s).name} for s in sinks
                                 if config.has_channel(s) and not config.channel(s).companion_of],
                "record_targets": [{"slug": s, "name": config.channel(s).name} for s in sources],
                "fader_range": [FADER_OFF_DB, FADER_MAX_DB],
                "max_trim": MAX_VOLUME,
            }

    # -- commands -------------------------------------------------------------

    def command(self, body: dict[str, Any]) -> dict[str, Any]:
        """Carry out one change. Raises RemoteError with a message for the phone."""
        name = body.get("cmd")
        handler = _COMMANDS.get(name) if isinstance(name, str) else None
        if handler is None:
            raise RemoteError(f"unknown command {name!r}")
        with self.lock:
            try:
                result = handler(self, body) or {}
            except RemoteError:
                raise
            except COMMAND_ERRORS as exc:
                raise RemoteError(str(exc) or exc.__class__.__name__) from exc
        self.notify()
        return {"ok": True, **result}

    def _edit(self, change: Callable[[], None], route_after: bool = False) -> None:
        """A settings change: adopt the window's latest save, change, save, apply."""
        self.engine.reload_if_changed()
        change()
        self.engine.save()
        self.engine.apply()
        if route_after and not self.engine.config.bypass:
            # Back from bypass: what played meanwhile sits on the default output.
            self.engine.route()

    def _channel(self, body: dict[str, Any]):
        self.engine.reload_if_changed()
        slug = body.get("slug")
        if not isinstance(slug, str) or not self.engine.config.has_channel(slug):
            raise RemoteError(f"no channel {slug!r}")
        channel = self.engine.config.channel(slug)
        if channel.companion_of:
            raise RemoteError(f"no channel {slug!r}")
        return channel


def _device(node) -> dict[str, Any]:
    return {"name": node.name, "label": node.label, "volume": node.volume,
            "muted": bool(node.muted)}


def _number(body: dict[str, Any], key: str, low: float, high: float) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RemoteError(f"{key} must be a number")
    return max(low, min(high, float(value)))


def _flag(body: dict[str, Any], key: str = "on") -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise RemoteError(f"{key} must be true or false")
    return value


def _cmd_fader(remote: Remote, body: dict[str, Any]) -> None:
    channel = remote._channel(body)
    db = _number(body, "db", FADER_OFF_DB, FADER_MAX_DB)
    remote._edit(lambda: setattr(channel, "fader_db", db))


def _cmd_pan(remote: Remote, body: dict[str, Any]) -> None:
    channel = remote._channel(body)
    pan = _number(body, "pan", -1.0, 1.0)
    remote._edit(lambda: setattr(channel, "pan", pan))


def _cmd_solo(remote: Remote, body: dict[str, Any]) -> None:
    channel = remote._channel(body)
    on = _flag(body)

    def change() -> None:
        channel.solo = on
        remote.engine.config.update_solo()

    remote._edit(change)


def _cmd_enabled(remote: Remote, body: dict[str, Any]) -> None:
    channel = remote._channel(body)
    on = _flag(body)
    remote._edit(lambda: setattr(channel, "enabled", on))


def _cmd_effect(remote: Remote, body: dict[str, Any]) -> None:
    channel = remote._channel(body)
    index = body.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(channel.effects):
        raise RemoteError(f"{channel.name} has no effect {index!r}")
    on = _flag(body)
    effect = channel.effects[index]
    remote._edit(lambda: setattr(effect, "enabled", on))


def _cmd_trim(remote: Remote, body: dict[str, Any]) -> dict[str, Any]:
    channel = remote._channel(body)
    volume = _number(body, "volume", 0.0, MAX_VOLUME)
    return {"volume": remote.engine.set_channel_volume(channel.slug, volume)}


def _cmd_mute(remote: Remote, body: dict[str, Any]) -> None:
    channel = remote._channel(body)
    remote.engine.set_channel_muted(channel.slug, _flag(body))


def _device_name(body: dict[str, Any]) -> str:
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise RemoteError("name must be a device name")
    return name


def _cmd_device_volume(remote: Remote, body: dict[str, Any]) -> dict[str, Any]:
    name = _device_name(body)
    return {"volume": remote.engine.set_device_volume(name, _number(body, "volume", 0.0, MAX_VOLUME))}


def _cmd_device_mute(remote: Remote, body: dict[str, Any]) -> None:
    remote.engine.set_device_muted(_device_name(body), _flag(body))


def _cmd_send(remote: Remote, body: dict[str, Any]) -> dict[str, Any]:
    stream = body.get("stream")
    if isinstance(stream, bool) or not isinstance(stream, int):
        raise RemoteError("stream must be a stream id")
    slug = body.get("slug")
    if not isinstance(slug, str):
        raise RemoteError("slug must be a channel")
    remote.engine.reload_if_changed()
    # The first choice made for an app is remembered, as in the window.
    result: MoveResult = remote.engine.send(stream, slug, remember_new=True)
    if remote.on_sent is not None:
        remote.on_sent(stream)
    return {"app": result.stream, "channel": result.channel, "reason": result.reason}


def _cmd_bypass(remote: Remote, body: dict[str, Any]) -> None:
    on = _flag(body)
    remote._edit(lambda: setattr(remote.engine.config, "bypass", on), route_after=not on)


_COMMANDS: dict[str, Callable[[Remote, dict[str, Any]], dict[str, Any] | None]] = {
    "fader": _cmd_fader,
    "pan": _cmd_pan,
    "solo": _cmd_solo,
    "enabled": _cmd_enabled,
    "effect": _cmd_effect,
    "trim": _cmd_trim,
    "mute": _cmd_mute,
    "device_volume": _cmd_device_volume,
    "device_mute": _cmd_device_mute,
    "send": _cmd_send,
    "bypass": _cmd_bypass,
}


# -- the HTTP side ---------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib's name
        pass  # the service log is for routing; a fader drag would bury it

    # -- plumbing

    def _send_json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local(self) -> bool:
        if self.server.allow_outside or is_home_network(self.client_address[0]):
            return True
        self.close_connection = True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "only the home network may use the remote"})
        return False

    def _authorised(self) -> bool:
        given = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if self.server.token and hmac.compare_digest(given.encode(), expected.encode()):
            return True
        time.sleep(REFUSAL_DELAY_S)
        # A refused POST's body was never read; it must not be taken for the
        # next request on a kept-alive connection.
        self.close_connection = True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "not paired"})
        return False

    # -- routes

    def do_GET(self) -> None:  # noqa: N802 - stdlib's name
        if not self._local():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/hello":
            self._send_json(HTTPStatus.OK, {"app": "audiorouter", "version": __version__,
                                            "host": socket.gethostname()})
        elif path == "/api/state":
            if self._authorised():
                self._send_json(HTTPStatus.OK, self.server.remote.state())
        elif path == "/api/events":
            if self._authorised():
                self._events()
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib's name
        if not self._local():
            return
        if self.path.split("?", 1)[0] != "/api/command":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})
            return
        if not self._authorised():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self.close_connection = True
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "a JSON body is needed"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "the body is not a JSON object"})
            return
        try:
            result = self.server.remote.command(body)
        except RemoteError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, result)

    def _events(self) -> None:
        """Push the desk whenever it changes, until the phone goes or we stop."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        remote = self.server.remote
        last = ""
        seen = -1
        try:
            while not self.server.stopping.is_set():
                if seen != remote.revision:
                    seen = remote.revision
                    text = json.dumps(remote.state())
                    if text != last:
                        last = text
                        self.wfile.write(f"event: state\ndata: {text}\n\n".encode())
                        self.wfile.flush()
                moved = remote.wait(seen, KEEPALIVE_S)
                if moved == seen:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], remote: Remote, token: str,
                 allow_outside: bool = False) -> None:
        super().__init__(address, _Handler)
        self.remote = remote
        self.token = token
        self.allow_outside = allow_outside
        self.stopping = threading.Event()


class RemoteServer:
    """Serves one `Remote` on a port in a background thread."""

    def __init__(self, remote: Remote, port: int, token: str, allow_outside: bool = False,
                 host: str = "0.0.0.0") -> None:
        self.remote = remote
        self._server = _Server((host, port), remote, token, allow_outside)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="audiorouter-remote", daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> RemoteServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.stopping.set()
        self.remote.notify()  # wakes every event stream so it sees the stop
        self._server.shutdown()
        self._server.server_close()


class RemoteSwitch:
    """Starts and stops the server as `remote.json` says, for the login service.

    `audiorouter remote on` only writes the file; the service follows it
    within a second, so no restart is needed.
    """

    def __init__(self, remote: Remote, path: Path | None = None,
                 log: Callable[[str], None] = print) -> None:
        self.remote = remote
        self.path = path
        self.log = log
        self.server: RemoteServer | None = None
        self._stamp: tuple[int, int] | None | bool = False  # False: never looked
        self._serving: tuple[int, str] | None = None

    def check(self) -> None:
        stamp = settings_stamp(self.path)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        settings = RemoteSettings.load(self.path)
        wanted = ((settings.port, settings.token, settings.allow_outside)
                  if settings.enabled and settings.token else None)
        if wanted == self._serving:
            return
        self.stop()
        if wanted is None:
            return
        try:
            self.server = RemoteServer(self.remote, *wanted).start()
        except OSError as exc:
            self.log(f"phone remote: could not listen on port {wanted[0]}: {exc}")
            return
        self._serving = wanted
        self.log(f"phone remote: listening on port {wanted[0]}"
                 + (", other networks allowed" if wanted[2] else ", home network only"))

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
            self.log("phone remote: stopped")
        self._serving = None
