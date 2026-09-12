"""The orchestrator: one object that a CLI or a GUI drives.

Everything below this module is a mechanism - render a graph, start a process,
read the PipeWire graph, move a stream. This is where the *policy* lives: what
the configuration says should exist, what actually exists right now, and the
smallest set of actions that closes the gap.

Two rules shape the whole file:

* **Reconcile, never assume.** The user can start a channel from the CLI, then
  open the GUI, then unplug a device. So every operation reads the live graph
  and the on-disk pid files rather than trusting in-memory state.
* **Never fight the user.** Auto-routing places a stream once, when it first
  appears. If the user then drags it somewhere else in their desktop's sound
  settings, it stays there. A router that re-asserted its rule every second
  would make the system feel broken and unfixable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channels import Channel, ChannelError, runtime_dir, validate_slug
from .config import Config, ConfigError, default_config
from .effects import Effect, EffectError, spec_for
from .pwgraph import Graph, GraphMonitor, Node, PwError
from .routing import Placement, Router, Rule, RuleSet, plan


class EngineError(RuntimeError):
    pass


@dataclass
class Action:
    """One thing the engine did, or would do in dry-run."""

    kind: str
    target: str
    detail: str = ""

    def describe(self) -> str:
        return f"{self.kind} {self.target}" + (f": {self.detail}" if self.detail else "")


@dataclass
class ApplyReport:
    """The outcome of reconciling running processes with the configuration."""

    actions: list[Action] = field(default_factory=list)
    failures: list[Action] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def changed(self) -> bool:
        return bool(self.actions)

    def describe(self) -> list[str]:
        return [a.describe() for a in self.actions + self.failures]


@dataclass
class MoveResult:
    """One routing decision carried out (or not)."""

    stream: str
    channel: str | None
    moved: bool
    reason: str


class Engine:
    """Owns the configuration and reconciles it with the live audio graph."""

    def __init__(
        self,
        config: Config | None = None,
        path: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config if config is not None else Config()
        self.path = path
        self.dry_run = dry_run
        self.router = Router(dry_run=dry_run)
        self._graph: Graph | None = None

    # -- construction and persistence -------------------------------------

    @classmethod
    def load(cls, path: Path | None = None, dry_run: bool = False) -> Engine:
        return cls(Config.load(path), path=path, dry_run=dry_run)

    def save(self) -> Path | None:
        if self.dry_run:
            return None
        return self.config.save(self.path)

    # -- the graph ---------------------------------------------------------

    def graph(self, refresh: bool = False) -> Graph:
        """A graph snapshot, cached until explicitly refreshed.

        Callers that act on what they read (routing, status) refresh first;
        callers that ask several questions in a row reuse one snapshot, because
        each refresh is a `pw-dump` subprocess.
        """
        if refresh or self._graph is None:
            self._graph = Graph.snapshot()
        return self._graph

    def use_graph(self, graph: Graph) -> None:
        """Adopt an externally maintained graph, e.g. from a GraphMonitor."""
        self._graph = graph

    def devices(self) -> list[Node]:
        return self.graph().devices()

    def device_choices(self) -> list[tuple[str, str]]:
        """(node.name, human label) for every real output, for a picker."""
        return [(n.name, n.label) for n in self.devices()]

    def sink_map(self, graph: Graph | None = None) -> dict[str, Node]:
        """slug -> the live sink node for that channel, for channels that are up."""
        graph = graph if graph is not None else self.graph()
        found: dict[str, Node] = {}
        for channel in self.config.channels:
            node = channel.sink_node(graph)
            if node is not None:
                found[channel.slug] = node
        return found

    # -- channels ----------------------------------------------------------

    def channel(self, slug: str) -> Channel:
        return self.config.channel(slug)

    def create_channel(
        self,
        slug: str,
        name: str = "",
        device: str = "",
        effects: Iterable[Effect] | None = None,
    ) -> Channel:
        validate_slug(slug)
        channel = Channel(slug=slug, name=name, device=device, effects=list(effects or []))
        self.config.add_channel(channel)
        self.save()
        return channel

    def delete_channel(self, slug: str) -> Channel:
        """Remove a channel, stopping its process first so nothing is orphaned."""
        channel = self.config.channel(slug)
        if not self.dry_run:
            channel.stop()
            channel.config_path.unlink(missing_ok=True)
            channel.log_path.unlink(missing_ok=True)
        removed = self.config.remove_channel(slug)
        self.save()
        return removed

    def rename_channel(self, slug: str, name: str) -> Channel:
        channel = self.config.channel(slug)
        channel.name = name or channel.name
        self.save()
        return channel

    def set_device(self, slug: str, device: str) -> Channel:
        """Point a channel at a different output. Takes effect on the next apply."""
        channel = self.config.channel(slug)
        channel.device = device
        self.save()
        return channel

    def set_enabled(self, slug: str, enabled: bool) -> Channel:
        channel = self.config.channel(slug)
        channel.enabled = bool(enabled)
        self.save()
        return channel

    def adopt_devices(self) -> list[Channel]:
        """Seed an empty configuration with one pass-through channel per device."""
        if self.config.channels:
            raise EngineError("configuration already has channels; not overwriting it")
        seeded = default_config([n.name for n in self.devices()])
        self.config.channels = seeded.channels
        self.save()
        return self.config.channels

    # -- effects -----------------------------------------------------------

    def add_effect(
        self, slug: str, kind: str, params: dict[str, Any] | None = None, index: int | None = None
    ) -> Effect:
        channel = self.config.channel(slug)
        spec = spec_for(kind)
        effect = Effect(kind=kind, params=spec.normalise(params))
        if index is None:
            channel.effects.append(effect)
        else:
            channel.effects.insert(max(0, min(index, len(channel.effects))), effect)
        self.save()
        return effect

    def _effect_at(self, slug: str, index: int) -> tuple[Channel, Effect]:
        channel = self.config.channel(slug)
        if not 0 <= index < len(channel.effects):
            raise EffectError(
                f"channel {slug!r} has no effect at position {index}"
                f" (it has {len(channel.effects)})"
            )
        return channel, channel.effects[index]

    def remove_effect(self, slug: str, index: int) -> Effect:
        channel, effect = self._effect_at(slug, index)
        channel.effects.remove(effect)
        self.save()
        return effect

    def set_effect_params(self, slug: str, index: int, params: dict[str, Any]) -> Effect:
        """Update knobs. Values are clamped to the spec's range, never rejected."""
        _, effect = self._effect_at(slug, index)
        merged = dict(effect.params)
        merged.update(params)
        effect.params = effect.spec.normalise(merged)
        self.save()
        return effect

    def set_effect_enabled(self, slug: str, index: int, enabled: bool) -> Effect:
        _, effect = self._effect_at(slug, index)
        effect.enabled = bool(enabled)
        self.save()
        return effect

    def move_effect(self, slug: str, index: int, to: int) -> Effect:
        """Reorder the chain. Order is audible: a limiter belongs last."""
        channel, effect = self._effect_at(slug, index)
        channel.effects.pop(index)
        channel.effects.insert(max(0, min(to, len(channel.effects))), effect)
        self.save()
        return effect

    # -- rules -------------------------------------------------------------

    def add_rule(self, field_name: str, pattern: str, slug: str, index: int | None = None) -> Rule:
        if not self.config.has_channel(slug):
            raise ConfigError(f"no channel {slug!r} to route to")
        rule = Rule(field=field_name, pattern=pattern, channel=slug)
        rules = self.config.rules.rules
        if index is None:
            rules.append(rule)
        else:
            rules.insert(max(0, min(index, len(rules))), rule)
        self.save()
        return rule

    def remove_rule(self, index: int) -> Rule:
        rules = self.config.rules.rules
        if not 0 <= index < len(rules):
            raise ConfigError(f"no rule at position {index} (there are {len(rules)})")
        rule = rules.pop(index)
        self.save()
        return rule

    def move_rule(self, index: int, to: int) -> Rule:
        """Rules are first-match-wins, so order is the whole meaning."""
        rules = self.config.rules.rules
        if not 0 <= index < len(rules):
            raise ConfigError(f"no rule at position {index} (there are {len(rules)})")
        rule = rules.pop(index)
        rules.insert(max(0, min(to, len(rules))), rule)
        self.save()
        return rule

    # -- process reconciliation -------------------------------------------

    def needs_restart(self, channel: Channel) -> bool:
        """Would restarting this channel change what it does?

        The rendered conf is written next to the pid file, so comparing against
        that file answers the question without keeping any state in memory - and
        survives the GUI, the CLI and the daemon each editing the config.
        """
        if not channel.is_running():
            return False
        try:
            return channel.config_path.read_text() != channel.render_config_text()
        except OSError:
            return True

    def orphan_slugs(self) -> list[str]:
        """Channel processes still running for channels no longer configured."""
        known = set(self.config.channel_slugs)
        found: list[str] = []
        for pid_file in sorted(runtime_dir().glob("*.pid")):
            slug = pid_file.stem
            if slug in known:
                continue
            probe = Channel(slug=slug, name=slug, device="")
            if probe.is_running():
                found.append(slug)
            else:
                pid_file.unlink(missing_ok=True)
        return found

    def start_channel(self, slug: str) -> Action:
        channel = self.config.channel(slug)
        if self.dry_run:
            return Action("start", slug, "dry run")
        channel.start()
        return Action("start", slug)

    def stop_channel(self, slug: str) -> Action:
        channel = self.config.channel(slug)
        if self.dry_run:
            return Action("stop", slug, "dry run")
        stopped = channel.stop()
        return Action("stop", slug, "" if stopped else "was not running")

    def apply(self) -> ApplyReport:
        """Make the running processes match the configuration.

        Enabled channels are started or restarted, disabled ones stopped, and
        processes left behind by deleted channels are killed. Failures are
        collected rather than raised: one broken channel must not stop the rest
        of the system from coming up.
        """
        report = ApplyReport()
        for slug in self.orphan_slugs():
            probe = Channel(slug=slug, name=slug, device="")
            if not self.dry_run:
                probe.stop()
                probe.config_path.unlink(missing_ok=True)
            report.actions.append(Action("stop", slug, "orphaned process"))

        for channel in self.config.channels:
            running = channel.is_running()
            if not channel.enabled:
                if running:
                    report.actions.append(self.stop_channel(channel.slug))
                continue
            missing = channel.missing_plugins()
            if missing:
                if running and not self.dry_run:
                    channel.stop()
                report.failures.append(Action("skip", channel.slug, "; ".join(missing)))
                continue
            if running and not self.needs_restart(channel):
                continue
            try:
                action = self.start_channel(channel.slug)
                action.detail = action.detail or ("restart" if running else "")
                report.actions.append(action)
            except (ChannelError, PwError) as exc:
                report.failures.append(Action("start", channel.slug, str(exc)))
        self._graph = None
        return report

    def stop_all(self) -> ApplyReport:
        """Tear down every channel process we know about, configured or not."""
        report = ApplyReport()
        for slug in self.config.channel_slugs + self.orphan_slugs():
            channel = (
                self.config.channel(slug)
                if self.config.has_channel(slug)
                else Channel(slug=slug, name=slug, device="")
            )
            if channel.is_running():
                if not self.dry_run:
                    channel.stop()
                report.actions.append(Action("stop", slug))
        self._graph = None
        return report

    # -- routing -----------------------------------------------------------

    def placements(self, refresh: bool = True) -> list[Placement]:
        graph = self.graph(refresh=refresh)
        return plan(graph, self.config.rules, self.sink_map(graph))

    def route(
        self,
        streams: Iterable[Node] | None = None,
        refresh: bool = True,
    ) -> list[MoveResult]:
        """Apply the rules to the streams that are playing now."""
        graph = self.graph(refresh=refresh)
        chosen = list(streams) if streams is not None else None
        results: list[MoveResult] = []
        for placement in plan(graph, self.config.rules, self.sink_map(graph), chosen):
            if not placement.needs_move:
                results.append(
                    MoveResult(placement.stream.app_name, placement.channel, False, placement.reason)
                )
                continue
            assert placement.target_sink is not None
            try:
                self.router.move(placement.stream, placement.target_sink)
                results.append(
                    MoveResult(placement.stream.app_name, placement.channel, True, placement.reason)
                )
            except Exception as exc:  # noqa: BLE001 - one bad stream must not stop the rest
                results.append(
                    MoveResult(placement.stream.app_name, placement.channel, False, str(exc))
                )
        return results

    def send(self, stream_id: int, slug: str) -> MoveResult:
        """Move one stream to one channel, ignoring the rules. The manual override."""
        graph = self.graph(refresh=True)
        stream = graph.node(stream_id)
        if stream is None:
            raise EngineError(f"no stream with id {stream_id}")
        sink = self.sink_map(graph).get(slug)
        if sink is None:
            raise EngineError(f"channel {slug!r} is not running; start it first")
        self.router.move(stream, sink)
        return MoveResult(stream.app_name, slug, True, "manual")

    # -- status ------------------------------------------------------------

    def status(self, refresh: bool = True) -> dict[str, Any]:
        """Everything a UI needs for one refresh, from a single graph snapshot."""
        graph = self.graph(refresh=refresh)
        sinks = self.sink_map(graph)
        channels = []
        for channel in self.config.channels:
            entry = channel.status(graph)
            entry["needs_restart"] = self.needs_restart(channel)
            entry["problems"] = channel.missing_plugins()
            channels.append(entry)
        streams = []
        for stream in graph.app_streams():
            current = graph.sink_of_stream(stream.id)
            slug = next((s for s, node in sinks.items() if current and node.id == current.id), None)
            streams.append(
                {
                    "id": stream.id,
                    "app": stream.app_name,
                    "binary": stream.binary,
                    "title": stream.media_name,
                    "sink": current.label if current else None,
                    "channel": slug,
                    "rule_channel": self.config.rules.resolve(stream),
                }
            )
        return {
            "auto_route": self.config.auto_route,
            "devices": [{"name": n.name, "label": n.label} for n in graph.devices()],
            "channels": channels,
            "streams": streams,
            "rules": [r.describe() for r in self.config.rules.rules],
            "problems": self.config.problems(),
            "orphans": self.orphan_slugs(),
        }


class AutoRouter:
    """Watches the graph and routes each new stream once, as it appears.

    Placing a stream *once* is deliberate. The rules express where an app should
    start, not where it must stay; anything stronger would undo the user's own
    choice every time they made one, with no way to win.
    """

    def __init__(self, engine: Engine, on_move: Callable[[MoveResult], None] | None = None) -> None:
        self.engine = engine
        self.on_move = on_move
        self._placed: set[int] = set()
        self._lock = threading.Lock()
        self._monitor = GraphMonitor(self._changed)

    def start(self) -> None:
        self._monitor.start()
        # The first snapshot already contains whatever was playing before we
        # started, and those streams deserve the same treatment as new ones.
        self._changed(self._monitor.graph)

    def stop(self) -> None:
        self._monitor.stop()

    def forget(self, stream_id: int) -> None:
        """Allow a stream to be auto-placed again, e.g. after its rules changed."""
        with self._lock:
            self._placed.discard(int(stream_id))

    def _changed(self, graph: Graph) -> None:
        if not self.engine.config.auto_route:
            return
        with self._lock:
            live = {n.id for n in graph.app_streams()}
            self._placed &= live
            fresh = [n for n in graph.app_streams() if n.id not in self._placed]
            if not fresh:
                return
            self._placed |= {n.id for n in fresh}
        self.engine.use_graph(graph)
        for result in self.engine.route(streams=fresh, refresh=False):
            if result.moved and self.on_move is not None:
                self.on_move(result)

    def __enter__(self) -> AutoRouter:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
