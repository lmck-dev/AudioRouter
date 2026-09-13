"""Read-only view of the PipeWire graph, plus a change monitor.

Everything here goes through `pw-dump`, which emits complete JSON objects. In
monitor mode (`-m`) it writes a full dump first and then one array per change;
an object whose "info" is null has been removed.

Two hard-won rules are encoded here:

* Never address a node by name unless the name is known to be unique. Several
  applications create identically named nodes (EasyEffects names every one of
  its sinks `easyeffects_sink`), and `pw-link -l` output cannot tell them apart.
* Never persist a node id or an object.serial. Both are reassigned on every
  restart of the owning process.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

SINK_CLASS = "Audio/Sink"
SOURCE_CLASS = "Audio/Source"
STREAM_OUTPUT_CLASS = "Stream/Output/Audio"
STREAM_INPUT_CLASS = "Stream/Input/Audio"

#: Stamped by us onto every node we create, so our own nodes can be told apart
#: from applications. This matters more than it looks: a filter-chain's playback
#: node has media.class Stream/Output/Audio, exactly like an app stream, so a
#: router that did not exclude it would route a channel's output back into a
#: channel and build a feedback loop.
OWNER_KEY = "audiorouter.channel"

_NODE = "PipeWire:Interface:Node"
_PORT = "PipeWire:Interface:Port"
_LINK = "PipeWire:Interface:Link"
_CLIENT = "PipeWire:Interface:Client"


class PwError(RuntimeError):
    """PipeWire was unreachable or a pw-* tool failed."""


def require_tools(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise PwError(
            "missing required PipeWire tool(s): "
            + ", ".join(missing)
            + " - install the pipewire-utils / pipewire-tools package"
        )


@dataclass(frozen=True)
class Node:
    """One PipeWire node, with the props we care about pulled out."""

    id: int
    serial: int | None
    name: str
    description: str
    media_class: str
    client_id: int | None
    props: dict[str, Any] = field(default_factory=dict, repr=False)
    #: The node's `Props` param holding volume and mute, when pw-dump has it.
    audio_props: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_sink(self) -> bool:
        return self.media_class.startswith(SINK_CLASS)

    @property
    def is_output_stream(self) -> bool:
        return self.media_class == STREAM_OUTPUT_CLASS

    @property
    def is_source(self) -> bool:
        """Something apps can record from (includes virtual sources we create)."""
        return self.media_class.startswith(SOURCE_CLASS)

    @property
    def is_input_stream(self) -> bool:
        """A recording stream: an app capturing, or one of our own loopbacks."""
        return self.media_class == STREAM_INPUT_CLASS

    @property
    def owned_channel(self) -> str | None:
        """The channel slug this node belongs to, if we created it."""
        value = self.props.get(OWNER_KEY)
        return str(value) if value else None

    @property
    def is_ours(self) -> bool:
        return self.owned_channel is not None

    @property
    def is_app_stream(self) -> bool:
        """A playback stream belonging to an application, safe to route."""
        return self.is_output_stream and not self.is_ours

    @property
    def is_device(self) -> bool:
        """True for a sink backed by real hardware rather than a virtual node.

        Hardware-backed sinks carry `device.id`; null sinks, filter-chain
        capture nodes and EasyEffects' sink do not.
        """
        return self.is_sink and "device.id" in self.props

    @property
    def app_name(self) -> str:
        """Best available human name for the application behind a stream."""
        for key in ("application.name", "application.process.binary", "node.name"):
            value = self.props.get(key)
            if value:
                return str(value)
        return f"node {self.id}"

    @property
    def binary(self) -> str:
        return str(self.props.get("application.process.binary", "") or "")

    @property
    def media_name(self) -> str:
        return str(self.props.get("media.name", "") or "")

    @property
    def pid(self) -> int | None:
        value = self.props.get("application.process.id")
        return int(value) if value is not None else None

    @property
    def label(self) -> str:
        return self.description or self.name or f"node {self.id}"

    @property
    def volume(self) -> float | None:
        """The volume as a desktop slider shows it (1.0 = 100%), if known.

        PipeWire stores linear per-channel gains; sliders everywhere (wpctl,
        pactl, Plasma) show their cube root, so 50% is -18 dB. Measured: a
        channel at 0.5 attenuated by 18.06 dB, at 0.25 by 36.13 dB.
        """
        gains = self.audio_props.get("channelVolumes")
        if not gains:
            return None
        return round(max(float(g) for g in gains) ** (1 / 3), 4)

    @property
    def muted(self) -> bool | None:
        value = self.audio_props.get("mute")
        return bool(value) if value is not None else None


@dataclass(frozen=True)
class Link:
    id: int
    output_node: int | None
    input_node: int | None


def _node_from_object(obj: dict[str, Any]) -> Node:
    info = obj.get("info") or {}
    props = info.get("props") or {}
    serial = props.get("object.serial")
    client_id = props.get("client.id")
    audio_props: dict[str, Any] = {}
    for entry in ((info.get("params") or {}).get("Props") or []):
        # A node lists several Props objects; only one carries the volume.
        if isinstance(entry, dict) and "channelVolumes" in entry:
            audio_props = entry
            break
    return Node(
        id=int(obj["id"]),
        serial=int(serial) if serial is not None else None,
        name=str(props.get("node.name", "") or ""),
        description=str(props.get("node.description", "") or ""),
        media_class=str(props.get("media.class", "") or ""),
        client_id=int(client_id) if client_id is not None else None,
        props=props,
        audio_props=audio_props,
    )


class Graph:
    """A snapshot of the graph that can be updated from monitor events."""

    def __init__(self, objects: list[dict[str, Any]] | None = None) -> None:
        self._objects: dict[int, dict[str, Any]] = {}
        if objects:
            self.apply(objects)

    # -- construction ----------------------------------------------------

    @classmethod
    def snapshot(cls) -> Graph:
        require_tools("pw-dump")
        try:
            out = subprocess.run(
                ["pw-dump", "-N"], capture_output=True, text=True, timeout=15, check=True
            ).stdout
        except subprocess.CalledProcessError as exc:  # pragma: no cover - needs daemon
            raise PwError(f"pw-dump failed: {exc.stderr.strip()}") from exc
        except subprocess.TimeoutExpired as exc:  # pragma: no cover
            raise PwError("pw-dump timed out; is PipeWire running?") from exc
        return cls(json.loads(out))

    def apply(self, objects: list[dict[str, Any]]) -> None:
        """Merge one pw-dump array. `info: null` means the object is gone."""
        for obj in objects:
            if "id" not in obj:
                continue
            oid = int(obj["id"])
            if obj.get("info", False) is None:
                self._objects.pop(oid, None)
            else:
                self._objects[oid] = obj

    # -- queries ---------------------------------------------------------

    def _of_type(self, type_name: str) -> Iterator[dict[str, Any]]:
        for obj in self._objects.values():
            if obj.get("type") == type_name:
                yield obj

    @property
    def nodes(self) -> list[Node]:
        return [_node_from_object(o) for o in self._of_type(_NODE)]

    def node(self, node_id: int) -> Node | None:
        obj = self._objects.get(int(node_id))
        if obj is None or obj.get("type") != _NODE:
            return None
        return _node_from_object(obj)

    def sinks(self) -> list[Node]:
        return [n for n in self.nodes if n.is_sink]

    def devices(self) -> list[Node]:
        """Real hardware output devices, sorted for stable presentation."""
        return sorted((n for n in self.nodes if n.is_device), key=lambda n: n.label.lower())

    def sources(self) -> list[Node]:
        return [n for n in self.nodes if n.is_source]

    def input_devices(self) -> list[Node]:
        """Real hardware inputs (microphones, line-in), sorted for presentation.

        Hardware sources carry `device.id`, like hardware sinks; virtual mics,
        including the ones our input channels create, do not.
        """
        return sorted(
            (n for n in self.nodes if n.is_source and "device.id" in n.props),
            key=lambda n: n.label.lower(),
        )

    def node_owned_by_pid(self, pid: int, name: str) -> Node | None:
        """The node called `name` that a given process created.

        Names repeat while a channel restart hands over from its old host to
        its new one; the owning process is what tells the two apart.
        """
        for node in self.nodes_named(name):
            if self.client_pid(node.client_id) == int(pid):
                return node
        return None

    def feeders_of(self, node_id: int) -> list[Node]:
        """Playback streams linked into a node (a sink)."""
        ids = {l.output_node for l in self.links() if l.input_node == int(node_id)}
        return [n for n in self.nodes if n.id in ids and n.is_output_stream]

    def readers_of(self, node_id: int) -> list[Node]:
        """Recording streams linked out of a node (a source)."""
        ids = {l.input_node for l in self.links() if l.output_node == int(node_id)}
        return [n for n in self.nodes if n.id in ids and n.is_input_stream]

    def streams(self) -> list[Node]:
        """Every playback stream, including our own channel outputs."""
        return [n for n in self.nodes if n.is_output_stream]

    def app_streams(self) -> list[Node]:
        """Playback streams belonging to applications. What the router may move."""
        return [n for n in self.nodes if n.is_app_stream]

    def conflicting_routers(self) -> list[str]:
        """Programs in the graph that move app streams themselves.

        EasyEffects (with its default "process all output streams") relinks
        every application onto its own `easyeffects_sink`, immediately undoing
        any move we make: the stream shows as "not routed" no matter what is
        chosen. It is recognised by that sink rather than by process name, so
        the native build, the Flatpak and its background service all count.
        """
        found: list[str] = []
        if any(n.name.startswith("easyeffects_sink") for n in self.nodes):
            found.append("EasyEffects")
        return found

    def nodes_named(self, name: str) -> list[Node]:
        """All nodes with this node.name - plural because names are not unique."""
        return [n for n in self.nodes if n.name == name]

    def unique_node_named(self, name: str) -> Node | None:
        """The single node with this name, or None if absent or ambiguous."""
        found = self.nodes_named(name)
        return found[0] if len(found) == 1 else None

    def _port_owners(self) -> dict[int, int]:
        owners: dict[int, int] = {}
        for obj in self._of_type(_PORT):
            props = (obj.get("info") or {}).get("props") or {}
            node_id = props.get("node.id")
            if node_id is not None:
                owners[int(obj["id"])] = int(node_id)
        return owners

    def links(self) -> list[Link]:
        owners = self._port_owners()
        result = []
        for obj in self._of_type(_LINK):
            info = obj.get("info") or {}
            result.append(
                Link(
                    id=int(obj["id"]),
                    output_node=owners.get(info.get("output-port-id")),
                    input_node=owners.get(info.get("input-port-id")),
                )
            )
        return result

    def sink_of_stream(self, stream_id: int) -> Node | None:
        """Which sink a stream is currently feeding, resolved through links.

        Link state is the ground truth; `target.object` metadata is only a
        request, and may name a sink the stream never reached.
        """
        for link in self.links():
            if link.output_node == int(stream_id) and link.input_node is not None:
                node = self.node(link.input_node)
                if node is not None and node.is_sink:
                    return node
        return None

    def client_pid(self, client_id: int | None) -> int | None:
        if client_id is None:
            return None
        obj = self._objects.get(int(client_id))
        if obj is None or obj.get("type") != _CLIENT:
            return None
        pid = ((obj.get("info") or {}).get("props") or {}).get("application.process.id")
        return int(pid) if pid is not None else None

    def sink_owned_by_pid(self, pid: int) -> Node | None:
        """Find the sink created by a given process.

        The only reliable way to identify a specific instance of a program that
        names all its sinks identically. Used to tell one helper process's sink
        from another's.
        """
        for node in self.sinks():
            if self.client_pid(node.client_id) == int(pid):
                return node
        return None


def iter_dump_blocks(stream: Any) -> Iterator[list[dict[str, Any]]]:
    """Yield each complete JSON array from a `pw-dump -m` stream.

    pw-dump pretty-prints one top-level array per update, so a line that is
    exactly "]" closes the current block.
    """
    buffer: list[str] = []
    for raw in stream:
        line = raw.rstrip("\n")
        if not buffer and not line.startswith("["):
            continue
        buffer.append(line)
        if line == "]":
            text = "\n".join(buffer)
            buffer = []
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                yield parsed


class GraphMonitor:
    """Runs `pw-dump -m` in a thread and keeps a Graph up to date.

    The callback fires after each applied update, on the monitor thread.
    """

    def __init__(self, on_change: Callable[[Graph], None] | None = None) -> None:
        self.graph = Graph()
        self._on_change = on_change
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        #: Set when pw-dump exits without being asked to - PipeWire went away.
        self.ended = threading.Event()

    def start(self, wait: float = 10.0) -> None:
        require_tools("pw-dump")
        self._stop.clear()
        self._ready.clear()
        self.ended.clear()
        self._proc = subprocess.Popen(
            ["pw-dump", "-m", "-N"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._run, name="pw-monitor", daemon=True)
        self._thread.start()
        if not self._ready.wait(wait):
            self.stop()
            raise PwError("timed out waiting for the first pw-dump snapshot")

    def _run(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for block in iter_dump_blocks(self._proc.stdout):
            if self._stop.is_set():
                return
            self.graph.apply(block)
            self._ready.set()
            if self._on_change is not None:
                try:
                    self._on_change(self.graph)
                except Exception:  # noqa: BLE001 - a bad callback must not kill the monitor
                    pass
        if not self._stop.is_set():
            self.ended.set()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._proc.kill()
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self) -> GraphMonitor:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
