"""On-disk configuration: the channels and the routing rules.

Written atomically, because a half-written config read by a daemon at login is
the kind of failure a non-technical user cannot diagnose.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channels import Channel, ChannelError
from .routing import Rule, RuleSet

CONFIG_VERSION = 1


class ConfigError(RuntimeError):
    pass


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "audiorouter"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    channels: list[Channel] = field(default_factory=list)
    rules: RuleSet = field(default_factory=lambda: RuleSet([]))
    #: Move streams automatically as they appear. Off means rules are only
    #: applied when the user explicitly asks.
    auto_route: bool = True

    # -- lookup -----------------------------------------------------------

    def channel(self, slug: str) -> Channel:
        for channel in self.channels:
            if channel.slug == slug:
                return channel
        raise ConfigError(f"no channel {slug!r}; known channels: {', '.join(self.channel_slugs) or 'none'}")

    @property
    def channel_slugs(self) -> list[str]:
        return [c.slug for c in self.channels]

    def has_channel(self, slug: str) -> bool:
        return any(c.slug == slug for c in self.channels)

    def add_channel(self, channel: Channel) -> Channel:
        if self.has_channel(channel.slug):
            raise ConfigError(f"channel {channel.slug!r} already exists")
        self.channels.append(channel)
        return channel

    def remove_channel(self, slug: str) -> Channel:
        channel = self.channel(slug)
        self.channels.remove(channel)
        # Rules pointing at a channel that no longer exists would silently stop
        # working, so drop them with the channel.
        self.rules.rules = [r for r in self.rules.rules if r.channel != slug]
        return channel

    # -- validation -------------------------------------------------------

    def problems(self) -> list[str]:
        """Configuration that is structurally valid but will not behave."""
        issues: list[str] = []
        seen: set[str] = set()
        for channel in self.channels:
            if channel.slug in seen:
                issues.append(f"duplicate channel id {channel.slug!r}")
            seen.add(channel.slug)
            if not channel.device and not channel.is_input:
                issues.append(f"channel {channel.slug!r} has no output device")
            for reason in channel.missing_plugins():
                issues.append(f"channel {channel.slug!r}: {reason}")
        kinds = {c.slug: c.kind for c in self.channels}
        for channel in self.channels:
            if channel.listen and kinds.get(channel.listen) != "output":
                issues.append(
                    f"input channel {channel.slug!r} listens through {channel.listen!r}, "
                    "which is not an output channel"
                )
        for rule in self.rules.rules:
            if rule.channel not in seen:
                issues.append(f"rule {rule.pattern!r} points at unknown channel {rule.channel!r}")
            elif kinds.get(rule.channel) == "input":
                issues.append(f"rule {rule.pattern!r} sends playback to input channel {rule.channel!r}")
        return issues

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "auto_route": self.auto_route,
            "channels": [c.to_dict() for c in self.channels],
            "rules": self.rules.to_list(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        version = int(data.get("version", CONFIG_VERSION))
        if version > CONFIG_VERSION:
            raise ConfigError(
                f"config version {version} was written by a newer AudioRouter; "
                "upgrade rather than risk losing settings"
            )
        try:
            channels = [Channel.from_dict(c) for c in (data.get("channels") or [])]
        except (ChannelError, ValueError) as exc:
            raise ConfigError(f"invalid channel in config: {exc}") from exc
        return cls(
            channels=channels,
            rules=RuleSet.from_list(data.get("rules")),
            auto_route=bool(data.get("auto_route", True)),
        )

    # -- files ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        target = path or config_path()
        if not target.exists():
            return cls()
        try:
            data = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {target}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{target} does not contain a configuration object")
        return cls.from_dict(data)

    def save(self, path: Path | None = None) -> Path:
        target = path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        os.replace(temporary, target)
        return target


def default_config(devices: list[str]) -> Config:
    """A starting configuration: one pass-through channel per output device.

    Deliberately effect-free. A first run that silently changed how audio sounds
    would be indistinguishable from a bug.
    """
    config = Config(auto_route=True)
    used: set[str] = set()
    for device in devices:
        slug = "".join(ch if ch.isalnum() else "_" for ch in device.lower()).strip("_")[:40]
        slug = slug or "channel"
        candidate, suffix = slug, 2
        while candidate in used:
            candidate, suffix = f"{slug[:37]}_{suffix}", suffix + 1
        used.add(candidate)
        config.add_channel(Channel(slug=candidate, name="", device=device))
    return config
