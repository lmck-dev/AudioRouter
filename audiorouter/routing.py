"""Deciding where a stream belongs, and putting it there.

Two separate jobs live here. `RuleSet` answers "which channel should this
application use?" from user-editable rules. `Router` performs the move.

Moves are done by writing `target.object` metadata, which is the same mechanism
the desktop's own sound settings use, so a move made here looks to the rest of
the system exactly like one the user made by hand. The value must be the target
sink's object.serial, never its node id.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .pwgraph import Graph, Node, PwError

#: Stream properties a rule may match against, in the order a user would expect
#: to reach for them.
MATCH_FIELDS = ("app", "binary", "title")

_FIELD_READERS = {
    "app": lambda n: n.app_name,
    "binary": lambda n: n.binary,
    "title": lambda n: n.media_name,
}


class RoutingError(RuntimeError):
    pass


@dataclass
class Rule:
    """Send streams whose `field` matches `pattern` to `channel`.

    Matching is case-insensitive. A pattern containing a glob character is
    treated as a glob; anything else matches as a substring, because
    "send firefox to headphones" is what a user means, not an anchored regex.
    """

    field: str
    pattern: str
    channel: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.field not in MATCH_FIELDS:
            raise RoutingError(
                f"unknown match field {self.field!r}; use one of {', '.join(MATCH_FIELDS)}"
            )
        if not self.pattern:
            raise RoutingError("a rule needs a pattern")

    def matches(self, node: Node) -> bool:
        if not self.enabled:
            return False
        value = (_FIELD_READERS[self.field](node) or "").casefold()
        if not value:
            return False
        pattern = self.pattern.casefold()
        if any(ch in pattern for ch in "*?["):
            return fnmatch.fnmatchcase(value, pattern)
        return pattern in value

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "pattern": self.pattern,
            "channel": self.channel,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rule:
        return cls(
            field=str(data.get("field", "app")),
            pattern=str(data.get("pattern", "")),
            channel=str(data.get("channel", "")),
            enabled=bool(data.get("enabled", True)),
        )

    def describe(self) -> str:
        state = "" if self.enabled else " (disabled)"
        return f"{self.field} contains {self.pattern!r} -> {self.channel}{state}"


@dataclass
class RuleSet:
    """Ordered rules; the first match wins."""

    rules: list[Rule]

    def resolve(self, node: Node) -> str | None:
        for rule in self.rules:
            if rule.matches(node):
                return rule.channel
        return None

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rules]

    @classmethod
    def from_list(cls, data: Iterable[Mapping[str, Any]] | None) -> RuleSet:
        return cls([Rule.from_dict(d) for d in (data or [])])


class Router:
    """Moves playback streams between sinks."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    # -- primitives -------------------------------------------------------

    @staticmethod
    def _set_metadata(node_id: int, serial: int) -> None:
        if shutil.which("pw-metadata") is None:
            raise PwError("pw-metadata is not installed")
        subprocess.run(
            ["pw-metadata", str(int(node_id)), "target.object", str(int(serial))],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

    @staticmethod
    def _pactl_move(node_id: int, sink_name: str) -> None:
        if shutil.which("pactl") is None:
            raise PwError("pactl is not installed")
        subprocess.run(
            ["pactl", "move-sink-input", str(int(node_id)), sink_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

    # -- operations -------------------------------------------------------

    def move(self, stream: Node, sink: Node) -> bool:
        """Point `stream` at `sink`. False if it was already there.

        Tries the native metadata route first and falls back to pactl, because
        pipewire-pulse is not guaranteed to be running while pw-metadata is part
        of PipeWire itself.
        """
        if stream.is_ours:
            raise RoutingError(
                f"refusing to move {stream.name!r}: it is one of our own channel outputs"
            )
        if sink.serial is None:
            raise RoutingError(f"sink {sink.name!r} has no object.serial; cannot target it")
        if self.dry_run:
            return True
        try:
            self._set_metadata(stream.id, sink.serial)
            return True
        except (subprocess.SubprocessError, PwError, OSError) as exc:
            # OSError covers pw-metadata having vanished from PATH; that is
            # exactly the case the pactl fallback exists for, so it must not
            # escape as a raw exception.
            try:
                self._pactl_move(stream.id, sink.name)
                return True
            except Exception:
                raise RoutingError(
                    f"could not move {stream.app_name!r} to {sink.name!r}: {exc}"
                ) from exc

    def retarget(self, stream: Node, target: Node) -> None:
        """Point one of our own streams at a node - only during a handover.

        `move` refuses our own streams, because routing one by accident builds
        a feedback loop. A restart handover is the one deliberate exception: a
        listen-through loopback must follow the output channel it plays into.
        """
        if target.serial is None:
            raise RoutingError(f"{target.name!r} has no object.serial; cannot target it")
        if self.dry_run:
            return
        self._set_metadata(stream.id, target.serial)

    def clear(self, stream: Node) -> None:
        """Forget a pinned target so the stream follows the default sink again."""
        if self.dry_run:
            return
        subprocess.run(
            ["pw-metadata", "-d", str(int(stream.id)), "target.object"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


@dataclass(frozen=True)
class Placement:
    """What the router decided about one stream."""

    stream: Node
    channel: str | None
    current_sink: Node | None
    target_sink: Node | None

    @property
    def needs_move(self) -> bool:
        if self.target_sink is None:
            return False
        return self.current_sink is None or self.current_sink.id != self.target_sink.id

    @property
    def reason(self) -> str:
        if self.channel is None:
            return "no rule matches"
        if self.target_sink is None:
            return f"channel {self.channel!r} is not running"
        if not self.needs_move:
            return f"already on {self.channel!r}"
        return f"move to {self.channel!r}"


def plan(
    graph: Graph,
    rules: RuleSet,
    sink_for_channel: Mapping[str, Node],
    streams: Iterable[Node] | None = None,
) -> list[Placement]:
    """Work out where every routable stream should go, without moving anything."""
    result = []
    for stream in streams if streams is not None else graph.app_streams():
        channel = rules.resolve(stream)
        result.append(
            Placement(
                stream=stream,
                channel=channel,
                current_sink=graph.sink_of_stream(stream.id),
                target_sink=sink_for_channel.get(channel) if channel else None,
            )
        )
    return result
