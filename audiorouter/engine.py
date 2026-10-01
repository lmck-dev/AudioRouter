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

import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channels import (
    NODE_PREFIX,
    NOWHERE,
    OUTPUT,
    SLUG_RE,
    Channel,
    ChannelError,
    running_hosts,
    runtime_dir,
    validate_slug,
)
from . import session
from .config import Config, ConfigError, config_path, default_config
from .effects import Effect, EffectError, make_effect
from .meter import sweep_stale
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

    @property
    def restarted(self) -> bool:
        """Did any sink get replaced? Only then can streams have fallen off."""
        return any(a.kind == "start" for a in self.actions)

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
        self._loaded_stamp = self._config_stamp()

    # -- construction and persistence -------------------------------------

    @classmethod
    def load(cls, path: Path | None = None, dry_run: bool = False) -> Engine:
        return cls(Config.load(path), path=path, dry_run=dry_run)

    def save(self) -> Path | None:
        if self.dry_run:
            return None
        saved = self.config.save(self.path)
        self._loaded_stamp = self._config_stamp()
        return saved

    def _config_stamp(self) -> tuple[int, int] | None:
        try:
            info = (self.path or config_path()).stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def reload_if_changed(self) -> bool:
        """Adopt settings another process saved since we last read or wrote them.

        The login daemon runs for the whole session while the GUI comes and
        goes; without this, an app remembered in the window would not be routed
        until the next login. A file that cannot be read is ignored and the last
        good settings kept - half-understood rules are worse than stale ones.
        Only a process that never edits the config itself should call this.
        """
        stamp = self._config_stamp()
        if stamp is None or stamp == self._loaded_stamp:
            return False
        try:
            config = Config.load(self.path)
        except ConfigError:
            return False
        self.config = config
        self._loaded_stamp = stamp
        return True

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
            if channel.is_input:
                continue  # routing sends playback to outputs only
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
        kind: str = OUTPUT,
    ) -> Channel:
        validate_slug(slug)
        channel = Channel(slug=slug, name=name, device=device, effects=list(effects or []), kind=kind)
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
        for other in self.config.channels:
            if other.listen == slug:
                other.listen = ""  # nothing left to listen through
        self.save()
        return removed

    def set_listen(self, slug: str, through: str) -> Channel:
        """Play an input channel through an output channel ("" to stop)."""
        channel = self.config.channel(slug)
        if not channel.is_input:
            raise EngineError(f"channel {slug!r} is an output; only inputs can be listened to")
        if through:
            target = self.config.channel(through)
            if target.is_input:
                raise EngineError(f"channel {through!r} is an input; listen through an output channel")
        channel.listen = through
        self.save()
        return channel

    def set_recordable(self, slug: str, recordable: bool) -> Channel:
        """Offer an output channel to recording apps as a virtual cable."""
        channel = self.config.channel(slug)
        if channel.is_input:
            raise EngineError(f"channel {slug!r} is an input; it is always recordable")
        if not recordable and channel.device == NOWHERE:
            raise EngineError(f"channel {slug!r} plays nowhere, so it must stay recordable")
        channel.recordable = bool(recordable)
        self.save()
        return channel

    def set_echo_cancel(self, slug: str, on: bool) -> Channel:
        """Subtract what the speakers play from an input channel's mic. A shape
        change: the channel restarts on the next apply."""
        channel = self.config.channel(slug)
        if not channel.is_input:
            raise EngineError(f"channel {slug!r} is an output; echo cancelling is for microphones")
        channel.echo_cancel = bool(on)
        self.save()
        return channel

    def rename_channel(self, slug: str, name: str) -> Channel:
        channel = self.config.channel(slug)
        channel.name = name or channel.name
        self.save()
        return channel

    def set_device(self, slug: str, device: str) -> Channel:
        """Point a channel at a different output. Takes effect on the next apply."""
        channel = self.config.channel(slug)
        if device.startswith(NODE_PREFIX):
            group = next((c for c in self.config.channels if c.node_name == device), None)
            if group is not None:
                if channel.is_input or group.is_input:
                    raise EngineError("only an output channel can play into another output channel")
                if group not in self.config.group_choices(channel):
                    raise EngineError(f"{channel.name} cannot play into {group.name}: "
                                      f"{group.name} already plays into {channel.name}")
        channel.device = device
        self.save()
        return channel

    def set_enabled(self, slug: str, enabled: bool) -> Channel:
        channel = self.config.channel(slug)
        channel.enabled = bool(enabled)
        self.save()
        return channel

    def set_channel_volume(self, slug: str, volume: float) -> float:
        """Set a running channel's volume (1.0 = 100%). Not saved in our config:
        the sink holds it, WirePlumber restores it, and the desktop shares it."""
        if self.dry_run:
            return volume
        try:
            return self.config.channel(slug).set_volume(volume, self._live_graph())
        finally:
            self._graph = None

    def set_channel_muted(self, slug: str, muted: bool) -> None:
        if self.dry_run:
            return
        try:
            self.config.channel(slug).set_muted(muted, self._live_graph())
        finally:
            self._graph = None

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
        self,
        slug: str,
        kind: str,
        params: dict[str, Any] | None = None,
        index: int | None = None,
        plugin: str = "",
    ) -> Effect:
        """Add an effect; `plugin` is the LV2 URI when `kind` is "lv2"."""
        channel = self.config.channel(slug)
        effect = make_effect(kind, params, plugin)
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

    def remember_app(self, app_name: str, slug: str) -> Rule:
        """Always send this app to `slug`: update its own rule, or add one.

        Updating matters because rules are first-match-wins: appending a second
        rule for the same app would be saved, listed, and never take effect.
        """
        if not self.config.has_channel(slug):
            raise ConfigError(f"no channel {slug!r} to route to")
        for rule in self.config.rules.rules:
            if rule.field == "app" and rule.pattern.casefold() == app_name.casefold():
                rule.channel = slug
                self.save()
                return rule
        return self.add_rule("app", app_name, slug)

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

    def _tune(self, channel: Channel, graph: Graph | None) -> Action | None:
        """Apply knob-only changes to a running channel without restarting it.

        None means it could not be done live - the graph's shape changed, or
        PipeWire refused - and the caller restarts the channel instead, which
        always works, only with a moment of silence.
        """
        changes = channel.control_changes()
        if not changes:
            return None
        try:
            channel.set_controls(changes, graph)
        except (ChannelError, PwError):
            return None
        return Action("tune", channel.slug, f"{len(changes)} setting(s) changed live")

    def orphan_slugs(self) -> list[str]:
        """Channel processes still running for channels no longer configured.

        Both records are consulted: the pid files we wrote, and the processes
        actually running. A process whose pid file was lost is exactly the one
        that would otherwise hold a sink forever with nothing able to stop it.
        """
        known = set(self.config.channel_slugs)
        candidates = {p.stem for p in runtime_dir().glob("*.pid")}
        candidates |= set(running_hosts())
        found: list[str] = []
        for slug in sorted(candidates - known):
            if not SLUG_RE.match(slug):
                continue
            if Channel(slug=slug, name=slug, device="").is_running():
                found.append(slug)
            else:
                (runtime_dir() / f"{slug}.pid").unlink(missing_ok=True)
        return found

    def start_channel(self, slug: str, occupants: dict[int, str] | None = None) -> Action:
        """Start a channel, or restart it without dropping anything using it.

        A restart hands over from the old host to the new one (see
        `_hand_over`); `occupants` is accepted for older callers and unused.
        """
        channel = self.config.channel(slug)
        self.config.update_solo()
        if self.dry_run:
            return Action("start", slug, "dry run")
        channel.start(handover=lambda new_pid: self._hand_over(channel, new_pid))
        return Action("start", slug)

    def _hand_over(self, channel: Channel, new_pid: int, timeout: float = 1.0) -> None:
        """Move everything using a restarting channel onto its new host, and wait.

        While both hosts run, each of the channel's node names exists twice.
        Three kinds of stream have to end up on the new host's nodes before the
        old host stops, or they fall back to a default device when it does:

        - playback streams feeding the channel's sink - apps, and other
          channels' own streams (an input channel listening through this one;
          missing those played a microphone out of the default speakers);
        - recording streams reading its virtual source - apps recording the
          processed mic or the virtual cable;
        - the new host's own internal loopbacks, which find their source by
          name and may have attached to the old host's copy of it.

        Waiting until each is linked matters: stopping the old host while a
        move is still in flight takes its node away first.
        """
        graph = Graph.snapshot()
        moves: list[tuple[Node, Node]] = []

        def copies(name: str | None) -> tuple[list[Node], Node | None]:
            if name is None:
                return [], None
            new = graph.node_owned_by_pid(new_pid, name)
            old = [n for n in graph.nodes_named(name) if new is None or n.id != new.id]
            return old, new

        if not channel.is_input:
            old_sinks, new_sink = copies(channel.node_name)
            for old in old_sinks:
                for stream in graph.feeders_of(old.id):
                    if new_sink is not None and stream.owned_channel != channel.slug:
                        moves.append((stream, new_sink))
        old_sources, new_source = copies(channel.recording_name)
        for old in old_sources:
            for stream in graph.readers_of(old.id):
                # A level meter refuses moves by design; waiting for one would
                # hold every restart for the full timeout. It reopens itself.
                if stream.is_meter:
                    continue
                if new_source is not None and stream.owned_channel != channel.slug:
                    moves.append((stream, new_source))
        for node in graph.nodes:
            target = node.props.get("target.object")
            if (node.is_input_stream and isinstance(target, str)
                    and graph.client_pid(node.client_id) == new_pid):
                own = graph.node_owned_by_pid(new_pid, target)
                if own is not None:
                    moves.append((node, own))

        pending: list[tuple[int, int]] = []
        for stream, target in moves:
            try:
                if stream.is_ours:
                    self.router.retarget(stream, target)
                else:
                    self.router.move(stream, target)
                pending.append((stream.id, target.id))
            except Exception:  # noqa: BLE001 - one stream must not strand the rest
                continue
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            time.sleep(0.02)
            graph = Graph.snapshot()
            linked = {(l.output_node, l.input_node) for l in graph.links()}
            pending = [
                (stream_id, target_id) for stream_id, target_id in pending
                if graph.node(stream_id) is not None
                and (stream_id, target_id) not in linked
                and (target_id, stream_id) not in linked
            ]

    def stop_channel(self, slug: str) -> Action:
        channel = self.config.channel(slug)
        if self.dry_run:
            return Action("stop", slug, "dry run")
        stopped = channel.stop()
        return Action("stop", slug, "" if stopped else "was not running")

    def _live_graph(self) -> Graph | None:
        try:
            return self.graph(refresh=True)
        except PwError:
            # Knowing the graph is a courtesy here; not knowing must never stop
            # the channels themselves from being brought up.
            return None

    def _occupants(self, graph: Graph) -> dict[int, str]:
        """Which channel each playing stream is sitting on right now."""
        sinks = self.sink_map(graph)
        by_id = {node.id: slug for slug, node in sinks.items()}
        placed: dict[int, str] = {}
        for stream in graph.app_streams():
            current = graph.sink_of_stream(stream.id)
            if current is not None and current.id in by_id:
                placed[stream.id] = by_id[current.id]
        return placed

    def _restore(self, occupants: dict[int, str]) -> list[Action]:
        """Put streams back on the channels they were on before a restart.

        Restarting a channel destroys its sink, and every stream pointed at it
        falls back to the default output and stays there. Without this, changing
        one setting would scatter everything the user had arranged - including
        streams they placed by hand, which no rule would ever bring back.
        """
        if not occupants:
            return []
        graph = self.graph(refresh=True)
        sinks = self.sink_map(graph)
        done: list[Action] = []
        for stream_id, slug in occupants.items():
            stream = graph.node(stream_id)
            sink = sinks.get(slug)
            if stream is None or sink is None or not stream.is_app_stream:
                continue
            current = graph.sink_of_stream(stream_id)
            if current is not None and current.id == sink.id:
                continue
            try:
                self.router.move(stream, sink)
                done.append(Action("restore", slug, stream.app_name))
            except Exception:  # noqa: BLE001 - a stream that went away is fine
                continue
        return done

    def apply(self, restore: bool = True) -> ApplyReport:
        """Make the running processes match the configuration.

        Enabled channels are started or restarted, disabled ones stopped, and
        processes left behind by deleted channels are killed. Failures are
        collected rather than raised: one broken channel must not stop the rest
        of the system from coming up.

        `restore` puts streams back on the channels they were playing through,
        because a restart takes the sink out from under them.
        """
        report = ApplyReport()
        self.config.update_solo()  # a solo may have been switched since the last apply
        if not self.dry_run:
            sweep_stale()  # level-tap files of hosts that were killed outright
        live = None if self.dry_run else self._live_graph()
        occupants = self._occupants(live) if restore and live is not None else {}
        # Channels whose sink is in the graph. A host process can outlive the
        # PipeWire daemon it was attached to (a PipeWire restart, a login race):
        # alive, config unchanged, and no sink. Unknown when the graph is.
        present = (
            {c.slug for c in self.config.channels if c.sink_node(live) is not None}
            if live is not None else None
        )  # every kind of channel; sink_map holds routable outputs only
        for slug in self.orphan_slugs():
            probe = Channel(slug=slug, name=slug, device="")
            if not self.dry_run:
                probe.stop()
                probe.config_path.unlink(missing_ok=True)
            report.actions.append(Action("stop", slug, "orphaned process"))

        # Groups first: a member started before its group would find nothing
        # to play into and follow the default output until the group appeared.
        for channel in self.config.start_order():
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
            lost_sink = present is not None and channel.slug not in present
            if running and not self.needs_restart(channel) and not lost_sink:
                continue
            if running and not lost_sink and not self.dry_run:
                tuned = self._tune(channel, live)
                if tuned is not None:
                    report.actions.append(tuned)
                    continue
            try:
                action = self.start_channel(channel.slug)
                if running and not action.detail:
                    action.detail = "restart: its sink had gone" if lost_sink else "restart"
                report.actions.append(action)
            except (ChannelError, PwError) as exc:
                report.failures.append(Action("start", channel.slug, str(exc)))
        self._graph = None
        if occupants and report.restarted:
            report.actions.extend(self._restore(occupants))
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

    def send(self, stream_id: int, slug: str, remember_new: bool = False) -> MoveResult:
        """Move one stream to one channel, ignoring the rules. The manual override.

        With `remember_new`, an app no rule covers yet is remembered on this
        channel - the first choice someone makes for an app is almost always
        where they want it next time. An app that already has a rule is left
        alone: moving it again is a one-off, not a change of mind about it.
        """
        graph = self.graph(refresh=True)
        stream = graph.node(stream_id)
        if stream is None:
            raise EngineError(f"no stream with id {stream_id}")
        sink = self.sink_map(graph).get(slug)
        if sink is None:
            raise EngineError(f"channel {slug!r} is not running; start it first")
        self.router.move(stream, sink)
        if remember_new and stream.app_name and self.config.rules.resolve(stream) is None:
            self.add_rule("app", stream.app_name, slug)
            return MoveResult(stream.app_name, slug, True, "manual, remembered")
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
            "input_devices": [{"name": n.name, "label": n.label} for n in graph.input_devices()],
            "channels": channels,
            "streams": streams,
            "rules": [r.describe() for r in self.config.rules.rules],
            "problems": self.config.problems(),
            "conflicts": [conflict_message(name) for name in graph.conflicting_routers()],
            # WirePlumber restarted alone: echo cancellation needs a full restart.
            "echo_cancel_broken": session.echo_cancel_broken(self.config.channels, graph) is not None,
            "orphans": self.orphan_slugs(),
        }


def conflict_message(program: str) -> str:
    """Why another audio program stops routing working, and what to do."""
    return (
        f"{program} is running. It moves every app onto its own output, so apps "
        f"cannot stay on your channels and will show as not routed. Quit {program} "
        "(including its background service) to use Audio Router - the two cannot "
        "run together. Your channels' own effects replace it."
    )


class AutoRouter:
    """Watches the graph and routes each new stream once, as it appears.

    Placing a stream *once* is deliberate. The rules express where an app should
    start, not where it must stay; anything stronger would undo the user's own
    choice every time they made one, with no way to win.
    """

    def __init__(
        self,
        engine: Engine,
        on_move: Callable[[MoveResult], None] | None = None,
        follow_config: bool = False,
        on_graph: Callable[[Graph], None] | None = None,
    ) -> None:
        self.engine = engine
        self.on_move = on_move
        #: Called with every graph update, before any routing.
        self.on_graph = on_graph
        #: Re-read the config file on each graph event. For the daemon, whose
        #: settings are edited by the GUI in another process; never for the GUI,
        #: whose panels hold the very objects a reload would replace.
        self.follow_config = follow_config
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

    @property
    def ended(self) -> bool:
        """The graph feed died on its own, e.g. PipeWire restarted. Nothing routes now."""
        return self._monitor.ended.is_set()

    def forget(self, stream_id: int) -> None:
        """Allow a stream to be auto-placed again, e.g. after its rules changed."""
        with self._lock:
            self._placed.discard(int(stream_id))

    def remember(self, stream_id: int) -> None:
        """Treat a stream as already placed, so the rules leave it alone.

        Called when the user moves something by hand. Without it, a stream the
        watcher had not yet seen would be dragged back to its rule the moment
        the next graph event arrived.
        """
        with self._lock:
            self._placed.add(int(stream_id))

    def _changed(self, graph: Graph) -> None:
        if self.follow_config:
            self.engine.reload_if_changed()
        if self.on_graph is not None:
            self.on_graph(graph)
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


# -- the background daemon -------------------------------------------------


def daemon_pid_path() -> Path:
    # Not `*.pid`: every pid file in this directory is taken to be a channel's,
    # so `daemon.pid` was deleted as an orphan by the next status refresh - and
    # would have collided with a channel the user named "daemon".
    return runtime_dir() / "routing.daemon"


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="ignore") for part in raw.split(b"\0") if part]


def daemon_pid() -> int | None:
    """The routing daemon's pid if one is running, whoever started it.

    The GUI asks this before routing by itself: two routers would both place
    every new stream, and the GUI's would stop the moment its window closed.
    The pid file alone is not trusted - pids are reused.
    """
    try:
        pid = int(daemon_pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    args = _cmdline(pid)
    if "watch" in args and any("audiorouter" in part for part in args):
        return pid
    return None


class DaemonRecord:
    """Marks this process as the routing daemon for as long as the block runs."""

    def __enter__(self) -> DaemonRecord:
        daemon_pid_path().write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc: object) -> None:
        path = daemon_pid_path()
        try:
            if path.read_text().strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass
