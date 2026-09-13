"""Channels: a named sink, its effect chain, and the process that hosts it.

Each channel is one `pipewire -c <conf>` process running a filter-chain. That
gives every channel a node name we choose ourselves, which is the whole reason
this design works: the name is stable across restarts, unique in the graph, and
therefore safe to route to by name and to show in the desktop's own sound
settings.

Channel processes are tracked by a pid file in the runtime directory so that a
CLI invocation, a daemon and a GUI all see the same set of running channels.
"""

from __future__ import annotations

import errno
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .effects import Effect, render_chain, unsatisfied_requirements
from .pwgraph import Graph, PwError, require_tools

#: Prefix on every node we create, so our nodes are recognisable in the graph
#: and cannot collide with sinks belonging to anything else.
NODE_PREFIX = "ar_"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: Minimal module set a standalone filter-chain host needs. Verified working;
#: leaving out client-node or adapter gives a process that starts and does
#: nothing.
_CONTEXT_MODULES = [
    {"name": "libpipewire-module-rt", "args": {}, "flags": ["ifexists", "nofail"]},
    {"name": "libpipewire-module-protocol-native"},
    {"name": "libpipewire-module-client-node"},
    {"name": "libpipewire-module-adapter"},
]


class ChannelError(RuntimeError):
    pass


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug or ""):
        raise ChannelError(
            f"invalid channel id {slug!r}: use lower-case letters, digits, '-' or '_', "
            "starting with a letter or digit, up to 40 characters"
        )
    return slug


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/audiorouter-{os.getuid()}"
    path = Path(base) / "audiorouter"
    path.mkdir(parents=True, exist_ok=True)
    return path


def running_hosts() -> dict[str, int]:
    """Channel slug -> pid, read from what is actually running.

    Pid files are the fast path, not the truth. One can be deleted, lost with a
    wiped runtime directory, or never written if we die between spawning and
    recording - and a channel whose pid file has gone is a process nothing can
    ever stop, holding a sink the user cannot get rid of. The conf path we
    launch with carries the slug, so the process itself is the record of last
    resort.
    """
    directory = runtime_dir()
    found: dict[str, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = [
                part.decode(errors="ignore")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
        except OSError:  # the process went away between listing and reading
            continue
        if not parts or Path(parts[0]).name != "pipewire":
            continue
        for part in parts[1:]:
            candidate = Path(part)
            if candidate.suffix == ".conf" and candidate.parent == directory:
                found[candidate.stem] = int(entry.name)
    return found


def _filter_graph(conf: Mapping[str, Any]) -> dict[str, Any]:
    for module in conf.get("context.modules", ()):
        if module.get("name") == "libpipewire-module-filter-chain":
            return module.get("args", {}).get("filter.graph", {})
    return {}


def _graph_controls(conf: Mapping[str, Any]) -> dict[str, float]:
    return {
        f"{node.get('name')}:{key}": value
        for node in _filter_graph(conf).get("nodes", ())
        for key, value in (node.get("control") or {}).items()
    }


def _without_controls(conf: Mapping[str, Any]) -> Any:
    """The conf with every knob value removed: what only a restart can change."""
    stripped = json.loads(json.dumps(conf))
    for node in _filter_graph(stripped).get("nodes", ()):
        node.pop("control", None)
    return stripped


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


@dataclass
class Channel:
    """One routable destination with its own effect chain."""

    slug: str
    name: str
    device: str
    effects: list[Effect] = field(default_factory=list)
    enabled: bool = True

    def __post_init__(self) -> None:
        validate_slug(self.slug)
        if not self.name:
            self.name = self.slug.replace("_", " ").replace("-", " ").title()

    # -- identity in the graph -------------------------------------------

    @property
    def sink_name(self) -> str:
        return f"{NODE_PREFIX}{self.slug}"

    @property
    def playback_name(self) -> str:
        return f"{NODE_PREFIX}{self.slug}_out"

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "device": self.device,
            "enabled": self.enabled,
            "effects": [e.to_dict() for e in self.effects],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Channel:
        return cls(
            slug=str(data.get("slug", "")),
            name=str(data.get("name", "")),
            device=str(data.get("device", "")),
            enabled=bool(data.get("enabled", True)),
            effects=[Effect.from_dict(e) for e in (data.get("effects") or [])],
        )

    # -- rendering --------------------------------------------------------

    def render_config(self) -> dict[str, Any]:
        """The complete conf structure for this channel's host process."""
        chain = render_chain(self.effects)
        args: dict[str, Any] = {
            "node.description": self.name,
            "media.name": self.name,
            "filter.graph": {
                "nodes": chain.nodes,
                "links": chain.links,
                # One port per side, named explicitly. Left implicit, every
                # spare plugin port (a mixer's unused inputs, a sidechain)
                # becomes a filter port and the graph refuses to start.
                "inputs": list(chain.inputs),
                "outputs": list(chain.outputs),
            },
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
            "capture.props": {
                "node.name": self.sink_name,
                "node.description": self.name,
                "media.class": "Audio/Sink",
                "audiorouter.channel": self.slug,
            },
            "playback.props": {
                "node.name": self.playback_name,
                "node.description": f"{self.name} output",
                "node.passive": True,
                "audiorouter.channel": self.slug,
            },
        }
        if self.device:
            # Bind this channel's output to one real device. Without it the
            # chain follows the default sink, which can be another channel.
            args["playback.props"]["target.object"] = self.device
        return {
            "context.properties": {"log.level": 0},
            "context.spa-libs": {
                "audio.convert.*": "audioconvert/libspa-audioconvert",
                "support.*": "support/libspa-support",
            },
            "context.modules": [
                *_CONTEXT_MODULES,
                {"name": "libpipewire-module-filter-chain", "args": args},
            ],
        }

    def render_config_text(self) -> str:
        # SPA-JSON is a superset of JSON, so strict JSON is always accepted and
        # saves us hand-quoting keys that contain dots.
        return json.dumps(self.render_config(), indent=2) + "\n"

    def controls(self) -> dict[str, float]:
        """Every knob value in the rendered graph, as `node:control`."""
        return render_chain(self.effects).controls()

    def running_config(self) -> dict[str, Any] | None:
        """The conf the running host was started with (or last updated to)."""
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return None

    def control_changes(self) -> dict[str, float] | None:
        """Knob values that differ from the running host, if that is all that does.

        None means the graph itself changed - an effect added, removed, moved,
        switched off, or a setting that alters the graph's shape such as a
        filter's steepness - and only a restart can apply it. An empty dict
        means nothing changed at all.
        """
        running = self.running_config()
        if running is None:
            return None
        wanted = self.render_config()
        if _without_controls(running) != _without_controls(wanted):
            return None
        before = _graph_controls(running)
        after = _graph_controls(wanted)
        return {key: value for key, value in after.items() if before.get(key) != value}

    def set_controls(self, values: Mapping[str, float], graph: Graph | None = None) -> None:
        """Change knobs on the running host without restarting it.

        Filter-chain exposes every control as a `Props` param on its sink node,
        so this is heard at once and nothing playing is interrupted. The conf
        beside the pid file is rewritten afterwards, because it is the record of
        what the host is running that `control_changes` compares against.
        """
        if not values:
            return
        require_tools("pw-cli")
        graph = graph if graph is not None else Graph.snapshot()
        node = self.sink_node(graph)
        if node is None:
            raise ChannelError(f"channel {self.slug!r} has no sink in the graph to update")
        params: list[Any] = []
        for key, value in values.items():
            params.extend([key, float(value)])
        pod = json.dumps({"params": params})
        try:
            subprocess.run(
                ["pw-cli", "set-param", str(node.id), "Props", pod],
                check=True, capture_output=True, text=True, timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelError(f"could not update channel {self.slug!r} live: {exc}") from exc
        self.config_path.write_text(self.render_config_text())

    # -- process lifecycle ------------------------------------------------

    @property
    def config_path(self) -> Path:
        return runtime_dir() / f"{self.slug}.conf"

    @property
    def pid_path(self) -> Path:
        return runtime_dir() / f"{self.slug}.pid"

    @property
    def log_path(self) -> Path:
        return runtime_dir() / f"{self.slug}.log"

    def pid(self) -> int | None:
        """The live host process for this channel, if any."""
        try:
            pid = int(self.pid_path.read_text().strip())
        except (OSError, ValueError):
            return self._adopt()
        if not _pid_alive(pid):
            return self._adopt()
        # Guard against pid reuse: the process must still be ours.
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            return self._adopt()
        return pid if str(self.config_path) in cmdline else self._adopt()

    def _adopt(self) -> int | None:
        """Find our host process without a usable pid file, and record it again.

        Scanning /proc is far more expensive than reading a file, so it only
        happens when the cheap answer failed - which is also the only time a
        channel would otherwise be lost.
        """
        pid = running_hosts().get(self.slug)
        if pid is None:
            return None
        try:
            self.pid_path.write_text(str(pid))
        except OSError:
            pass
        return pid

    def is_running(self) -> bool:
        return self.pid() is not None

    def missing_plugins(self) -> list[str]:
        """Human-readable reasons this channel's effects cannot run here."""
        return [r.explain() for r in unsatisfied_requirements(self.effects)]

    def start(self, graph_timeout: float = 8.0) -> int:
        """Start (or restart) this channel's host process and wait for its sink."""
        require_tools("pipewire")
        missing = self.missing_plugins()
        if missing:
            raise ChannelError(
                f"channel {self.slug!r} cannot start: " + "; ".join(missing)
            )
        self.stop()
        self.config_path.write_text(self.render_config_text())
        with self.log_path.open("w") as log:
            proc = subprocess.Popen(
                ["pipewire", "-c", str(self.config_path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.pid_path.write_text(str(proc.pid))
        try:
            self._await_sink(proc, graph_timeout)
        except Exception:
            self.stop()
            raise
        return proc.pid

    def _await_sink(self, proc: subprocess.Popen[Any], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ChannelError(
                    f"channel {self.slug!r} host process exited immediately "
                    f"(code {proc.returncode}); see {self.log_path}"
                )
            try:
                if Graph.snapshot().unique_node_named(self.sink_name) is not None:
                    return
            except PwError:
                pass
            time.sleep(0.2)
        raise ChannelError(
            f"channel {self.slug!r} did not appear in the graph within {timeout:g}s; "
            f"see {self.log_path}"
        )

    def stop(self, timeout: float = 4.0) -> bool:
        """Stop the host process if running. True if something was stopped."""
        pid = self.pid()
        self.pid_path.unlink(missing_ok=True)
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return True

    # -- status -----------------------------------------------------------

    def sink_node(self, graph: Graph):
        return graph.unique_node_named(self.sink_name)

    def device_present(self, graph: Graph) -> bool:
        """Is this channel's output target currently in the graph?

        Checks every sink, not only hardware ones: a channel may legitimately
        target a virtual sink, and a Bluetooth device disappears entirely when
        it disconnects rather than merely going idle.
        """
        if not self.device:
            return True
        return any(n.name == self.device for n in graph.sinks())

    def status(self, graph: Graph | None = None) -> dict[str, Any]:
        graph = graph if graph is not None else Graph.snapshot()
        node = self.sink_node(graph)
        return {
            "slug": self.slug,
            "name": self.name,
            "device": self.device,
            "device_present": self.device_present(graph),
            "enabled": self.enabled,
            "running": self.is_running(),
            "pid": self.pid(),
            "sink_node_id": node.id if node else None,
            "sink_serial": node.serial if node else None,
            "effects": [e.label if e.plugin else e.kind for e in self.effects if e.enabled],
        }
