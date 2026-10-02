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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import lv2
from .effects import Effect, render_chain, taps_available, unsatisfied_requirements
from .pwgraph import OWNER_KEY, Graph, Node, PwError, require_tools

#: Prefix on every node we create, so our nodes are recognisable in the graph
#: and cannot collide with sinks belonging to anything else.
NODE_PREFIX = "ar_"

#: Highest channel volume offered: 150%, as desktop sound settings allow when
#: "raise maximum volume" is on. Above 100% the signal can clip.
MAX_VOLUME = 1.5

#: WirePlumber: never move this stream to a default device when its target
#: vanishes. Measured without it: a loopback whose output channel was removed
#: was relinked straight onto the real speakers - for a listen-through, that is
#: a live microphone on the speakers.
NO_FALLBACK = "node.dont-fallback"

OUTPUT = "output"
INPUT = "input"
KINDS = (OUTPUT, INPUT)
#: An output channel's device when it should play nowhere - a virtual cable
#: that only recording apps hear.
NOWHERE = "@nowhere"

#: WebRTC echo cancellation, from pipewire-libs (every PipeWire install has it).
AEC_LIBRARY = "aec/libspa-aec-webrtc"
#: Noise suppression and gain control are the channel's own effects' job
#: (RNNoise, a compressor), and doubling them up only fights those. Measured on
#: the voice mic 18 Sep 2026 with these settings: a phrase played through the
#: speakers scored 1.00 for a wake-word detector on the raw mic, 0.00 here.
AEC_ARGS = {
    "webrtc.high_pass_filter": True,
    "webrtc.noise_suppression": False,
    "webrtc.gain_control": False,
    "webrtc.extended_filter": True,
    "webrtc.delay_agnostic": True,
}

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


def companion_slug_for(slug: str) -> str:
    """The companion's slug: `<input>_mix`, shortened to stay a valid slug."""
    return f"{slug[:36]}_mix"


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


def _loopback(description: str, capture: dict[str, Any], playback: dict[str, Any]) -> dict[str, Any]:
    """A loopback module: record one node and play it into another."""
    return {
        "name": "libpipewire-module-loopback",
        "args": {
            "node.description": description,
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
            "capture.props": capture,
            "playback.props": playback,
        },
    }


def _terminate(pid: int, timeout: float = 4.0) -> bool:
    """SIGTERM, then SIGKILL if it will not go. True if it was running."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def _pid_alive(pid: int) -> bool:
    """Is this process still running? A zombie is not.

    A channel host is a child of whoever started it (the window, the CLI or the
    login service). Once it exits it stays a zombie until reaped, and
    `kill(pid, 0)` still succeeds on a zombie - so every stop waited out its
    whole timeout and then sent SIGKILL, and the login service accumulated
    zombie hosts. Our own children are reaped here; anyone else's zombie is
    recognised from /proc.
    """
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child: fall through to the generic check
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno != errno.EPERM:
            return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    # The state letter follows the parenthesised command name.
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


@dataclass
class Channel:
    """One named audio path with its own effect chain.

    An *output* channel is a sink apps play into; its sound goes to `device`
    (a real output, the default output, or NOWHERE) and, when `recordable`, it
    is also offered to recording apps as a virtual source - a virtual cable.

    An *input* channel reads a microphone or line-in (`device`, or the default
    input) and offers the processed sound as a virtual microphone apps choose
    from their input list; `listen` optionally plays it through an output
    channel as well.
    """

    slug: str
    name: str
    device: str
    effects: list[Effect] = field(default_factory=list)
    enabled: bool = True
    kind: str = OUTPUT
    #: Output channels: also offer the processed sound as a recording source.
    recordable: bool = False
    #: Input channels: the slug of an output channel to play the sound through.
    listen: str = ""
    #: Input channels: subtract whatever the speakers are playing from the mic
    #: before the effects, so a call does not hear your video, music or game.
    echo_cancel: bool = False
    #: The post-insert fader, in dB (the mixer's big fader). The desktop's
    #: volume for the channel is separate and acts before the effects.
    fader_db: float = 0.0
    #: Balance after the fader: -1 hard left, 0 centre, +1 hard right.
    pan: float = 0.0
    #: Soloed on the mixer: every other channel of the same kind is cut.
    solo: bool = False
    #: Silenced because another channel of the same kind is soloed. Worked out
    #: by `Config.update_solo` from the whole desk, never saved.
    solo_cut: bool = field(default=False, repr=False, compare=False)
    #: Outputs only: this channel is the hidden COMPANION of the input channel
    #: with this slug (see `Config.ensure_companions`). The mic channel plays
    #: into it, apps can play into it too, and it offers the sum to recording
    #: apps under the name they always used for the mic (`ar_<input slug>`).
    companion_of: str = ""

    def __post_init__(self) -> None:
        validate_slug(self.slug)
        if not self.name:
            self.name = self.slug.replace("_", " ").replace("-", " ").title()
        if self.kind not in KINDS:
            raise ChannelError(f"channel {self.slug!r}: kind must be one of {', '.join(KINDS)}")
        if self.kind == INPUT:
            self.recordable = False
            self.companion_of = ""
            if self.device == NOWHERE:
                raise ChannelError(f"input channel {self.slug!r} needs something to record from")
        else:
            self.listen = ""
            self.echo_cancel = False
            if self.device == NOWHERE:
                # Playing nowhere only makes sense as a virtual cable.
                self.recordable = True

    @property
    def is_input(self) -> bool:
        return self.kind == INPUT

    # -- identity in the graph -------------------------------------------

    @property
    def node_name(self) -> str:
        """The node people see: an output's sink, or an input's virtual mic."""
        return f"{NODE_PREFIX}{self.slug}"

    @property
    def sink_name(self) -> str:
        """Kept for outputs, where the channel's node is a sink."""
        return self.node_name

    @property
    def control_name(self) -> str:
        """The node carrying the effect controls: the filter's capture side."""
        return f"{NODE_PREFIX}{self.slug}_in" if self.is_input else self.node_name

    @property
    def recording_name(self) -> str | None:
        """The source recording apps pick, if this channel offers one.

        A mic channel's is published by its companion, under the mic channel's
        own node name, so apps that chose the mic keep it.
        """
        if self.is_input:
            return self.node_name
        if self.companion_of:
            return f"{NODE_PREFIX}{self.companion_of}"
        return f"{NODE_PREFIX}{self.slug}_rec" if self.recordable else None

    @property
    def companion_slug(self) -> str:
        """An input channel's companion: the output its mic plays into."""
        return companion_slug_for(self.slug)

    @property
    def companion_sink_name(self) -> str:
        return f"{NODE_PREFIX}{self.companion_slug}"

    @property
    def playback_name(self) -> str:
        return f"{NODE_PREFIX}{self.slug}_out"

    @property
    def echo_cancel_name(self) -> str:
        """An input channel's echo-cancelled microphone, which its effects read."""
        return f"{NODE_PREFIX}{self.slug}_ec"

    @property
    def listen_name(self) -> str:
        """An input channel's stream playing into the channel it listens through."""
        return f"{NODE_PREFIX}{self.slug}_listen"

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slug": self.slug,
            "name": self.name,
            "kind": self.kind,
            "device": self.device,
            "enabled": self.enabled,
            "effects": [e.to_dict() for e in self.effects],
            "fader_db": self.fader_db,
            "pan": self.pan,
            "solo": self.solo,
        }
        if self.is_input:
            data["listen"] = self.listen
            data["echo_cancel"] = self.echo_cancel
        else:
            data["recordable"] = self.recordable
            if self.companion_of:
                data["companion_of"] = self.companion_of
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Channel:
        return cls(
            slug=str(data.get("slug", "")),
            name=str(data.get("name", "")),
            device=str(data.get("device", "")),
            enabled=bool(data.get("enabled", True)),
            effects=[Effect.from_dict(e) for e in (data.get("effects") or [])],
            kind=str(data.get("kind", OUTPUT)),
            recordable=bool(data.get("recordable", False)),
            listen=str(data.get("listen", "")),
            echo_cancel=bool(data.get("echo_cancel", False)),
            fader_db=float(data.get("fader_db", 0.0)),
            pan=max(-1.0, min(1.0, float(data.get("pan", 0.0)))),
            solo=bool(data.get("solo", False)),
            companion_of=str(data.get("companion_of", "")),
        )

    # -- rendering --------------------------------------------------------

    def _chain(self):
        return render_chain(self.effects, taps=taps_available(), fader_db=self.fader_db,
                            pan=self.pan, cut=self.solo_cut)

    def render_config(self) -> dict[str, Any]:
        """The complete conf structure for this channel's host process."""
        chain = self._chain()
        stamp = {OWNER_KEY: self.slug}
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
        }
        modules: list[dict[str, Any]] = [{"name": "libpipewire-module-filter-chain", "args": args}]

        if self.is_input:
            args["capture.props"] = {
                "node.name": self.control_name,
                "node.description": f"{self.name} (microphone in)",
                # Passive: the real microphone is only opened while something
                # records the processed one (or listens to it). Measured: the
                # mic stayed suspended until a recorder appeared.
                "node.passive": True,
                **stamp,
            }
            if self.echo_cancel:
                # mic -> echo canceller -> effects. The canceller is a second
                # module in this same process, so it starts, stops and restarts
                # with the channel and needs no service of its own.
                modules.insert(0, self._echo_canceller(stamp))
                args["capture.props"]["target.object"] = self.echo_cancel_name
                args["capture.props"][NO_FALLBACK] = True
            elif self.device:
                args["capture.props"]["target.object"] = self.device
                # A chosen mic that is unplugged must not be silently swapped
                # for whichever other microphone happens to be the default.
                args["capture.props"][NO_FALLBACK] = True
            args["playback.props"] = {
                # Into the companion, which mixes in any apps and offers the
                # sum as the mic apps record (and plays it to `listen`).
                "node.name": self.playback_name,
                "node.description": f"{self.name} (into its mix)",
                "target.object": self.companion_sink_name,
                # Passive: the mic wakes only while something records the
                # companion. dont-fallback: never the speakers - with the
                # companion missing, filter-chain destroys this channel
                # instead (measured), and `apply` restarts it once it is back.
                "node.passive": True,
                NO_FALLBACK: True,
                **stamp,
            }
        else:
            args["capture.props"] = {
                "node.name": self.node_name,
                # A companion's sink is where apps are mixed into a mic.
                "node.description": f"{self.name} (mix into the mic)" if self.companion_of else self.name,
                "media.class": "Audio/Sink",
                **stamp,
            }
            plays = self.device != NOWHERE
            if self.recordable:
                args["playback.props"] = {
                    "node.name": self.recording_name,
                    # A companion's is the mic apps already know by name.
                    "node.description": self.name if self.companion_of else f"{self.name} (recording)",
                    "media.class": "Audio/Source",
                    **stamp,
                }
                if plays:
                    playback = {"node.name": self.playback_name, **stamp}
                    if self.device:
                        playback["target.object"] = self.device
                        if self.companion_of:
                            # A mic's listen-through: never onto the real
                            # speakers if the output it plays into vanishes.
                            playback[NO_FALLBACK] = True
                    modules.append(_loopback(
                        f"{self.name} output",
                        capture={"node.name": f"{self.node_name}_play_in",
                                 "target.object": self.recording_name, NO_FALLBACK: True, **stamp},
                        playback=playback,
                    ))
            else:
                args["playback.props"] = {
                    "node.name": self.playback_name,
                    "node.description": f"{self.name} output",
                    "node.passive": True,
                    **stamp,
                }
                if self.device:
                    # Bind this channel's output to one real device. Without it
                    # the chain follows the default sink, which can be another
                    # channel.
                    args["playback.props"]["target.object"] = self.device
                    # Never node.dont-fallback here, not even into a group: a
                    # missing target then makes filter-chain destroy the whole
                    # channel ("defined target not found", measured 1 Oct
                    # 2026), and its apps land on the default output anyway.
        return {
            "context.properties": {"log.level": 0},
            "context.spa-libs": {
                "audio.convert.*": "audioconvert/libspa-audioconvert",
                "support.*": "support/libspa-support",
            },
            "context.modules": [*_CONTEXT_MODULES, *modules],
        }

    def _echo_canceller(self, stamp: dict[str, Any]) -> dict[str, Any]:
        """PipeWire's echo-cancel module in monitor mode: the reference is the
        monitor of the default output, so nothing is re-routed and no extra
        sink appears. What reaches the speakers is subtracted from the mic."""
        capture: dict[str, Any] = {
            "node.name": f"{self.echo_cancel_name}_mic",
            "node.description": f"{self.name} (microphone, before echo cancelling)",
            "node.passive": True,
            **stamp,
        }
        if self.device:
            capture["target.object"] = self.device
            capture[NO_FALLBACK] = True
        return {
            "name": "libpipewire-module-echo-cancel",
            "args": {
                "library.name": AEC_LIBRARY,
                "monitor.mode": True,
                "audio.channels": 1,
                "audio.position": ["MONO"],
                "aec.args": dict(AEC_ARGS),
                "capture.props": capture,
                "source.props": {
                    "node.name": self.echo_cancel_name,
                    "node.description": f"{self.name} (echo cancelled, internal)",
                    # Never the default microphone: apps should pick the
                    # channel's own virtual mic, which has the effects on it.
                    "priority.session": 1,
                    "priority.driver": 1,
                    **stamp,
                },
                "sink.props": {
                    "node.name": f"{self.echo_cancel_name}_ref",
                    "node.description": f"{self.name} (echo reference)",
                    "node.passive": True,
                    **stamp,
                },
            },
        }

    def render_config_text(self) -> str:
        # SPA-JSON is a superset of JSON, so strict JSON is always accepted and
        # saves us hand-quoting keys that contain dots.
        return json.dumps(self.render_config(), indent=2) + "\n"

    def controls(self) -> dict[str, float]:
        """Every knob value in the rendered graph, as `node:control`."""
        return self._chain().controls()

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

        Switching an effect on or off needs nothing special here: it is a
        change of its ramp's `Start`/`Stop`, and the crossfade then runs inside
        the audio thread (see `effects._with_bypass`).
        """
        if not values:
            return
        require_tools("pw-cli")
        graph = graph if graph is not None else Graph.snapshot()
        node = self.control_node(graph)
        if node is None:
            raise ChannelError(f"channel {self.slug!r} is not in the graph to update")
        self._send_props(node.id, values)
        self.config_path.write_text(self.render_config_text())

    def _send_props(self, node_id: int, values: Mapping[str, float]) -> None:
        params: list[Any] = []
        for key, value in values.items():
            params.extend([key, round(float(value), 6)])
        pod = json.dumps({"params": params})
        try:
            subprocess.run(
                ["pw-cli", "set-param", str(node_id), "Props", pod],
                check=True, capture_output=True, text=True, timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelError(f"could not update channel {self.slug!r} live: {exc}") from exc

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

    def start(
        self,
        graph_timeout: float = 8.0,
        handover: Callable[[int], None] | None = None,
    ) -> int:
        """Start or restart this channel's host process and wait for its sink.

        A restart is make-before-break. The new host starts while the old one
        is still playing; once its node exists, `handover` is given the new
        host's pid to move streams onto its nodes, and only then is the old host
        stopped. Stopping
        first took the sink away and every stream fell back to the default
        output - measured, ~110 ms of unprocessed sound on the speakers, even
        from the headphones channel. If the new host fails, the old one is left
        running and nothing is lost.
        """
        require_tools("pipewire")
        missing = self.missing_plugins()
        if missing:
            raise ChannelError(
                f"channel {self.slug!r} cannot start: " + "; ".join(missing)
            )
        old_pid = self.pid()
        previous_conf = self.config_path.read_text() if old_pid and self.config_path.exists() else None
        self.config_path.write_text(self.render_config_text())
        with self.log_path.open("w") as log:
            # The user's plugin folders reach the host's LV2 loader this way;
            # LADSPA effects carry their file's full path instead.
            lv2_path = lv2.host_lv2_path()
            proc = subprocess.Popen(
                ["pipewire", "-c", str(self.config_path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "LV2_PATH": lv2_path} if lv2_path else None,
            )
        self.pid_path.write_text(str(proc.pid))
        try:
            self._await_sink(proc, graph_timeout)
        except Exception:
            _terminate(proc.pid)
            if old_pid is not None:
                # The old host is still running: it stays the channel.
                self.pid_path.write_text(str(old_pid))
                if previous_conf is not None:
                    self.config_path.write_text(previous_conf)
            else:
                self.pid_path.unlink(missing_ok=True)
            raise
        if old_pid is not None:
            if handover is not None:
                try:
                    handover(proc.pid)
                except Exception:  # noqa: BLE001 - a failed move must not leave two hosts
                    pass
            _terminate(old_pid)
        return proc.pid

    def _await_sink(self, proc: subprocess.Popen[Any], timeout: float) -> Node:
        """The new host's own node - by process, as the old one shares its name."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ChannelError(
                    f"channel {self.slug!r} host process exited immediately "
                    f"(code {proc.returncode}); see {self.log_path}"
                )
            try:
                # An input's virtual mic belongs to its companion; its own is the capture.
                own = self.control_name if self.is_input else self.node_name
                node = Graph.snapshot().node_owned_by_pid(proc.pid, own)
                if node is not None:
                    return node
            except PwError:
                pass
            time.sleep(0.05)
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
        return _terminate(pid, timeout)

    # -- volume -----------------------------------------------------------

    def _sink_id(self, graph: Graph | None) -> int:
        graph = graph if graph is not None else Graph.snapshot()
        node = self.sink_node(graph)
        if node is None:
            raise ChannelError(f"channel {self.slug!r} is not running, so it has no volume to set")
        return node.id

    def _wpctl(self, *argv: str) -> None:
        require_tools("wpctl")
        try:
            subprocess.run(["wpctl", *argv], check=True, capture_output=True, text=True, timeout=5)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise ChannelError(f"could not change the volume of {self.slug!r}: {exc}") from exc

    def set_volume(self, volume: float, graph: Graph | None = None) -> float:
        """Set the channel's volume, 1.0 being 100%, on its sink.

        The sink's own volume is used rather than a gain effect: it is what the
        desktop's sound settings show and change too, WirePlumber remembers it
        across restarts by node name, and changing it never touches the graph.
        """
        volume = max(0.0, min(MAX_VOLUME, float(volume)))
        self._wpctl("set-volume", str(self._sink_id(graph)), f"{volume:.4f}")
        return volume

    def set_muted(self, muted: bool, graph: Graph | None = None) -> None:
        self._wpctl("set-mute", str(self._sink_id(graph)), "1" if muted else "0")

    # -- status -----------------------------------------------------------

    def _node(self, graph: Graph, name: str | None) -> Node | None:
        if name is None:
            return None
        found = graph.nodes_named(name)
        if len(found) <= 1:
            return found[0] if found else None
        # Names repeat only while a restart hands over from the old host to the
        # new; the pid file already names the new one.
        pid = self.pid()
        return graph.node_owned_by_pid(pid, name) if pid is not None else None

    def sink_node(self, graph: Graph) -> Node | None:
        """The channel's own node: an output's sink, or an input's capture.

        An input's virtual mic belongs to its companion, so the node that says
        this host is alive (and carries its trim volume, before the effects,
        like an output's sink) is the capture.
        """
        return self._node(graph, self.control_name if self.is_input else self.node_name)

    def control_node(self, graph: Graph) -> Node | None:
        return self._node(graph, self.control_name)

    def recording_node(self, graph: Graph) -> Node | None:
        return self._node(graph, self.recording_name)

    def playback_node(self, graph: Graph) -> Node | None:
        """An output channel's stream into its device (absent for a cable playing nowhere)."""
        return self._node(graph, self.playback_name)

    def device_present(self, graph: Graph) -> bool:
        """Is this channel's device currently in the graph?

        Checks every sink (or source, for inputs), not only hardware: a channel
        may legitimately use a virtual device, and a Bluetooth device vanishes
        entirely when it disconnects rather than merely going idle.
        """
        if not self.device or self.device == NOWHERE:
            return True
        candidates = graph.sources() if self.is_input else graph.sinks()
        return any(n.name == self.device for n in candidates)

    def status(self, graph: Graph | None = None) -> dict[str, Any]:
        graph = graph if graph is not None else Graph.snapshot()
        node = self.sink_node(graph)
        return {
            "slug": self.slug,
            "name": self.name,
            "kind": self.kind,
            "device": self.device,
            "listen": self.listen,
            "echo_cancel": self.echo_cancel,
            "recordable": self.recordable,
            "recording_name": self.recording_name,
            "companion_of": self.companion_of,
            "device_present": self.device_present(graph),
            "enabled": self.enabled,
            "running": self.is_running(),
            "pid": self.pid(),
            "sink_node_id": node.id if node else None,
            "sink_serial": node.serial if node else None,
            "volume": node.volume if node else None,
            "muted": node.muted if node else None,
            "effects": [e.label if e.plugin else e.kind for e in self.effects if e.enabled],
        }
