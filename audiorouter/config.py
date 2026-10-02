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

from .channels import INPUT, NODE_PREFIX, NOWHERE, OUTPUT, Channel, ChannelError, companion_slug_for
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
    #: Bypass: every channel stopped and nothing routed, as if Audio Router
    #: were not installed. Saved, so it lasts across logins until switched
    #: off; switching it off brings everything back from these same settings.
    bypass: bool = False
    #: Folders the user added for more LV2 and LADSPA plugins (a DAW's plugin
    #: folder, say), searched at any depth. Here, not in the window's settings,
    #: because the login service starts the channels that load them.
    plugin_folders: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ensure_companions()
        self.update_solo()

    # -- companions -----------------------------------------------------------
    #
    # Every input channel has a hidden companion output channel (owner, 1 Oct
    # 2026). The mic channel (mic, echo cancel, mic effects) plays into it over
    # a passive link; apps can play into it too; it offers the sum to recording
    # apps as the mic, and plays it to `listen` if set. Lab: exact levels, no
    # added delay, the mic still sleeps until something records.

    def companion(self, channel: Channel) -> Channel | None:
        """An input channel's companion, or None."""
        if not channel.is_input:
            return None
        return next((c for c in self.channels if c.companion_of == channel.slug), None)

    def mic_channel_of(self, companion: Channel) -> Channel | None:
        """The input channel a companion belongs to."""
        if not companion.companion_of:
            return None
        return next((c for c in self.channels if c.is_input and c.slug == companion.companion_of), None)

    def ensure_companions(self) -> bool:
        """Give every input a companion and keep it in step; True if anything changed.

        The companion follows its input's name, on/off and listen-through
        (which it plays as its output: into the chosen output channel, or
        nowhere). Companions whose input is gone are removed.
        """
        changed = False
        inputs = {c.slug: c for c in self.channels if c.is_input}
        for orphan in [c for c in self.channels if c.companion_of and c.companion_of not in inputs]:
            self.channels.remove(orphan)
            changed = True
        for mic in inputs.values():
            companion = self.companion(mic)
            if companion is None:
                slug = companion_slug_for(mic.slug)
                if any(c.slug == slug for c in self.channels):
                    slug = companion_slug_for(mic.slug[:34] + "_c")
                companion = Channel(slug=slug, name=mic.name, device=NOWHERE, kind=OUTPUT,
                                    recordable=True, companion_of=mic.slug)
                # Right after its mic channel, so the file stays readable.
                self.channels.insert(self.channels.index(mic) + 1, companion)
                changed = True
            device = f"{NODE_PREFIX}{mic.listen}" if mic.listen else NOWHERE
            wanted = {"name": mic.name, "enabled": mic.enabled, "device": device, "recordable": True}
            for key, value in wanted.items():
                if getattr(companion, key) != value:
                    setattr(companion, key, value)
                    changed = True
        return changed

    def update_solo(self) -> None:
        """Mark which channels a solo silences: the rest of the soloed kind.

        Outputs and inputs are separate desks - soloing the headphones must not
        cut the microphone a call is using - so a solo only reaches channels
        of its own kind. The groups a soloed channel plays through, and the
        members of a soloed group, are part of what is being soloed and are
        never cut. Call this after changing any channel's `solo`.
        """
        soloed = [c for c in self.channels if c.solo and c.enabled]
        heard: set[str] = set()
        for channel in soloed:
            heard.add(channel.slug)
            heard.update(g.slug for g in self.groups_below(channel))
            heard.update(m.slug for m in self.members_of(channel, nested=True))
        kinds = {c.kind for c in soloed}
        for channel in self.channels:
            if channel.companion_of:
                continue  # follows its mic channel, below
            channel.solo_cut = channel.kind in kinds and channel.slug not in heard
        for companion in self.channels:
            mic = self.mic_channel_of(companion)
            if mic is not None:
                # Never cut by an output's solo: that would silence a call's mic.
                companion.solo_cut = mic.solo_cut

    # -- groups -------------------------------------------------------------
    #
    # A group is an output channel that other output channels play into: a
    # member's `device` is the group's node name (`ar_<slug>`), so the conf
    # needs nothing special. The group's own effects and fader then act on
    # the members' sum, and the group plays to a real device.

    def group_of(self, channel: Channel) -> Channel | None:
        """The output channel this one plays into, if it plays into one."""
        if channel.is_input or not channel.device.startswith(NODE_PREFIX):
            return None
        return next((c for c in self.channels
                     if not c.is_input and c is not channel and c.node_name == channel.device), None)

    def groups_below(self, channel: Channel) -> list[Channel]:
        """The groups this channel plays through, nearest first (loops stop)."""
        chain: list[Channel] = []
        group = self.group_of(channel)
        while group is not None and group is not channel and group not in chain:
            chain.append(group)
            group = self.group_of(group)
        return chain

    def in_loop(self, channel: Channel) -> bool:
        """Does following this channel's groups lead back to it?"""
        group = self.group_of(channel)
        for _ in self.channels:
            if group is None:
                return False
            if group is channel:
                return True
            group = self.group_of(group)
        return False

    def members_of(self, group: Channel, nested: bool = False) -> list[Channel]:
        """The channels playing into a group (and into its member groups)."""
        direct = [c for c in self.channels if self.group_of(c) is group]
        if not nested:
            return direct
        found: list[Channel] = []
        pending = list(direct)
        while pending:
            member = pending.pop(0)
            if member in found or member is group:
                continue
            found.append(member)
            pending.extend(c for c in self.channels if self.group_of(c) is member)
        return found

    def is_group(self, channel: Channel) -> bool:
        return bool(self.members_of(channel))

    def group_choices(self, channel: Channel) -> list[Channel]:
        """Output channels this one may play into without making a loop."""
        if channel.is_input:
            return []
        return [c for c in self.channels
                if not c.is_input and c is not channel and channel not in self.groups_below(c)]

    def start_order(self) -> list[Channel]:
        """Channels with every group before the channels playing into it.

        A member started first would find no group to play into and follow
        the default output until the group appeared.
        """
        def depth(channel: Channel) -> int:
            # A mic channel plays into its companion, which must exist first.
            companion = self.companion(channel)
            if companion is not None:
                return len(self.groups_below(companion)) + 1
            return len(self.groups_below(channel))
        return sorted(self.channels, key=depth)

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
        self.ensure_companions()
        self.update_solo()
        return channel

    def remove_channel(self, slug: str) -> Channel:
        channel = self.channel(slug)
        if channel.companion_of:
            raise ConfigError(f"{channel.name}'s mix belongs to its mic channel; delete that instead")
        # An input's companion goes with it.
        gone = [channel, *([self.companion(channel)] if self.companion(channel) else [])]
        for each in gone:
            self.channels.remove(each)
        gone_slugs = {c.slug for c in gone}
        gone_nodes = {c.node_name for c in gone if not c.is_input}
        # Rules pointing at a channel that no longer exists would silently stop
        # working, so drop them with the channel.
        self.rules.rules = [r for r in self.rules.rules if r.channel not in gone_slugs]
        # Members of a deleted group go back to the default output rather
        # than pointing at a node that will never appear again.
        for member in self.channels:
            if not member.is_input and member.device in gone_nodes:
                member.device = ""
        self.update_solo()
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
            # An empty device is "Default output", a choice the window offers,
            # not a fault. (It once warned "has no output device", which read
            # as "nothing is plugged in" while sound was playing.)
            for reason in channel.missing_plugins():
                issues.append(f"channel {channel.slug!r}: {reason}")
        kinds = {c.slug: c.kind for c in self.channels}
        for channel in self.channels:
            if channel.listen and kinds.get(channel.listen) != "output":
                issues.append(
                    f"input channel {channel.slug!r} listens through {channel.listen!r}, "
                    "which is not an output channel"
                )
        for channel in self.channels:
            if self.in_loop(channel):
                issues.append(f"channel {channel.slug!r} plays into a loop of groups")
        for rule in self.rules.rules:
            if rule.channel not in seen:
                issues.append(f"rule {rule.pattern!r} points at unknown channel {rule.channel!r}")
            elif kinds.get(rule.channel) == "input" and not rule.record:
                issues.append(f"rule {rule.pattern!r} sends playback to input channel {rule.channel!r}")
        return issues

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "auto_route": self.auto_route,
            "bypass": self.bypass,
            "channels": [c.to_dict() for c in self.channels],
            "rules": self.rules.to_list(),
            "plugin_folders": list(self.plugin_folders),
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
            bypass=bool(data.get("bypass", False)),
            plugin_folders=[str(f) for f in (data.get("plugin_folders") or []) if f],
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
