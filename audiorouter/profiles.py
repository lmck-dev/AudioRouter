"""Saved profiles and saved effect chains (owner, 16 Oct 2026).

A **profile** is the whole desk under a name ("Gaming", "Recording"): every
channel with its effects, outputs, fader and pan, the remembered apps, and the
auto-route switch. Switching to one replaces all of that. Bypass and the plugin
folders belong to this computer, not to a way of working, and are never part
of a profile.

A **chain** is one channel's effects under a name, with its fader and pan,
loadable onto any channel ("Streaming voice" reused across mics).

Each lives in its own JSON file beside config.json, written atomically like
it. A profile naming a device that is not plugged in still loads: that
channel plays to the default output until the device appears (the same as a
channel whose device is unplugged today).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .channels import Channel, ChannelError
from .config import Config, ConfigError
from .effects import Effect, EffectError

PROFILE_VERSION = 1
NAME_MAX = 60

#: Config keys a profile holds. Everything else stays as it is on a switch.
PROFILE_KEYS = ("auto_route", "channels", "rules")


class ProfileError(ConfigError):
    pass


def _file_stem(name: str) -> str:
    """A file name for a display name: same letters, nothing a path can trip on."""
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return stem or "unnamed"


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ProfileError("give it a name")
    if len(name) > NAME_MAX:
        raise ProfileError(f"names are at most {NAME_MAX} characters")
    return name


def _write(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)
    return path


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path.name} is not a saved profile or chain")
    if int(data.get("version", PROFILE_VERSION)) > PROFILE_VERSION:
        raise ProfileError(f"{path.name} was saved by a newer Audio Router")
    return data


class Store:
    """The named files of one kind (profiles or chains) in one folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def path_for(self, name: str) -> Path:
        return self.folder / f"{_file_stem(clean_name(name))}.json"

    def names(self) -> list[str]:
        """Saved names, A to Z. Unreadable files are skipped, not fatal."""
        found: list[str] = []
        if self.folder.is_dir():
            for path in self.folder.glob("*.json"):
                try:
                    found.append(str(_read(path).get("name") or path.stem))
                except ProfileError:
                    continue
        return sorted(found, key=str.casefold)

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def read(self, name: str) -> dict[str, Any]:
        path = self.path_for(name)
        if not path.exists():
            known = ", ".join(self.names()) or "none saved yet"
            raise ProfileError(f"nothing saved as {name!r} (saved: {known})")
        return _read(path)

    def write(self, name: str, body: dict[str, Any]) -> Path:
        name = clean_name(name)
        data = {"version": PROFILE_VERSION, "name": name,
                "saved": datetime.now().isoformat(timespec="seconds"), **body}
        return _write(self.path_for(name), data)

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if not path.exists():
            raise ProfileError(f"nothing saved as {name!r}")
        path.unlink()

    def rename(self, old: str, new: str) -> None:
        data = self.read(old)
        new = clean_name(new)
        if self.path_for(new) != self.path_for(old) and self.exists(new):
            raise ProfileError(f"{new!r} is already saved; delete it first")
        data["name"] = new
        _write(self.path_for(new), data)
        if self.path_for(new) != self.path_for(old):
            self.path_for(old).unlink()


def profiles(config_file: Path) -> Store:
    return Store(config_file.parent / "profiles")


def chains(config_file: Path) -> Store:
    return Store(config_file.parent / "chains")


# -- profiles ----------------------------------------------------------------


def profile_body(config: Config) -> dict[str, Any]:
    whole = config.to_dict()
    return {key: whole[key] for key in PROFILE_KEYS}


def config_from_profile(data: dict[str, Any], current: Config, name: str) -> Config:
    """The current config with the profile's part swapped in."""
    whole = current.to_dict()
    for key in PROFILE_KEYS:
        if key in data:
            whole[key] = data[key]
    try:
        config = Config.from_dict(whole)
    except ConfigError as exc:
        raise ProfileError(f"profile {name!r} cannot be loaded: {exc}") from exc
    config.profile = name
    return config


def matches_profile(config: Config, data: dict[str, Any]) -> bool:
    """Is the desk exactly as the profile saved it?"""
    try:
        saved = Config.from_dict({**Config().to_dict(), **{k: data[k] for k in PROFILE_KEYS if k in data}})
    except ConfigError:
        return False
    return profile_body(saved) == profile_body(config)


# -- chains ------------------------------------------------------------------


def chain_body(channel: Channel) -> dict[str, Any]:
    return {
        "kind": channel.kind,
        "effects": [e.to_dict() for e in channel.effects],
        "fader_db": channel.fader_db,
        "pan": channel.pan,
    }


def chain_effects(data: dict[str, Any], name: str) -> list[Effect]:
    try:
        return [Effect.from_dict(e) for e in (data.get("effects") or [])]
    except (EffectError, ChannelError, ValueError) as exc:
        raise ProfileError(f"chain {name!r} cannot be loaded: {exc}") from exc
