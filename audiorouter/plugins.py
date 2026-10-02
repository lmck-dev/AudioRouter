"""What plugin backends this machine can actually load.

PipeWire's filter-chain loads each plugin type through a separate SPA plugin.
A default Fedora install ships the builtin, ebur128 and LADSPA loaders but NOT
the LV2 one, so an effect that needs LV2 fails at process start with nothing but
"No such file or directory" unless we check first. Everything here is a
capability probe, so that failure can be reported as an installable package
instead of a crash.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

_LOADER_DIRS = (
    Path("/usr/lib64/spa-0.2/filter-graph"),
    Path("/usr/lib/spa-0.2/filter-graph"),
    Path("/usr/lib/x86_64-linux-gnu/spa-0.2/filter-graph"),
)

_LADSPA_DIRS = (
    Path("/usr/lib64/ladspa"),
    Path("/usr/lib/ladspa"),
    Path("/usr/lib/x86_64-linux-gnu/ladspa"),
    Path.home() / ".ladspa",
)

#: Backends that need no loader plugin of their own.
_ALWAYS = frozenset({"builtin"})

#: Folders the user added (Config.plugin_folders), searched for LV2 bundles and
#: LADSPA files at any depth. Set by the Engine whenever it loads its config.
_extra_folders: tuple[Path, ...] = ()
#: How deep inside an added folder to look, and how many files at most: someone
#: adding their whole home folder must not freeze the window.
_MAX_DEPTH = 5
_MAX_FILES = 5000


def set_extra_folders(folders: list[str] | tuple[str, ...]) -> bool:
    """Use these plugin folders from now on. True if that changed anything."""
    global _extra_folders
    wanted = tuple(Path(f).expanduser() for f in folders)
    if wanted == _extra_folders:
        return False
    _extra_folders = wanted
    _scan_extra.cache_clear()
    reset_cache()
    from . import lv2  # imported late: lv2 has no dependency on this module

    lv2.reset_cache()
    return True


def extra_folders() -> tuple[Path, ...]:
    return _extra_folders


@functools.lru_cache(maxsize=1)
def _scan_extra() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """(folders holding LV2 bundles, LADSPA candidate files) in the user's folders.

    A `.so` inside an LV2 bundle is that bundle's code, never a LADSPA plugin.
    """
    lv2_roots: dict[Path, None] = {}
    files: list[Path] = []
    for folder in _extra_folders:
        if not folder.is_dir():
            continue
        base = len(folder.parts)
        for root, dirs, names in os.walk(folder, followlinks=True):
            here = Path(root)
            if len(here.parts) - base >= _MAX_DEPTH:
                dirs[:] = []
            bundles = [d for d in dirs if d.endswith(".lv2")]
            if bundles:
                lv2_roots[here] = None
            dirs[:] = sorted(d for d in dirs if not d.endswith(".lv2") and not d.startswith("."))
            files.extend(here / n for n in sorted(names) if n.endswith(".so"))
            if len(files) >= _MAX_FILES:
                break
    return tuple(lv2_roots), tuple(files[:_MAX_FILES])


def extra_lv2_roots() -> tuple[Path, ...]:
    """Folders holding LV2 bundles inside the user's plugin folders."""
    return _scan_extra()[0]


def ladspa_files() -> list[Path]:
    """Every LADSPA candidate: LADSPA_PATH (or else the standard folders, as
    hosts do), then the user's own folders."""
    configured = os.environ.get("LADSPA_PATH")
    if configured is not None:
        dirs = [Path(p).expanduser() for p in configured.split(":") if p]
    else:
        dirs = list(_LADSPA_DIRS)
    seen: dict[Path, None] = {}
    for directory in dirs:
        if directory.is_dir():
            for path in sorted(directory.glob("*.so")):
                seen[path] = None
    for path in _scan_extra()[1]:
        seen[path] = None
    return list(seen)


@dataclass(frozen=True)
class Requirement:
    """Something an effect needs that may not be installed."""

    backend: str
    identifier: str
    package_hint: str

    def satisfied(self) -> bool:
        return backend_available(self.backend) and plugin_installed(self.backend, self.identifier)

    def explain(self) -> str:
        if not backend_available(self.backend):
            return (
                f"PipeWire cannot load {self.backend} plugins on this system "
                f"(install {self.package_hint})"
            )
        return f"plugin {self.identifier} is not installed (install {self.package_hint})"


@dataclass(frozen=True)
class Unusable:
    """A plugin that is installed but cannot run in a channel, and why."""

    identifier: str
    reason: str

    def satisfied(self) -> bool:
        return False

    def explain(self) -> str:
        return f"{self.identifier} {self.reason}"


@functools.lru_cache(maxsize=1)
def available_loaders() -> frozenset[str]:
    """Backend names filter-chain can load here, e.g. {builtin, ladspa, lv2}."""
    found = set(_ALWAYS)
    for directory in _LOADER_DIRS:
        if not directory.is_dir():
            continue
        for path in directory.glob("libspa-filter-graph-plugin-*.so"):
            name = path.name
            found.add(name[len("libspa-filter-graph-plugin-") : -len(".so")])
    return frozenset(found)


def backend_available(backend: str) -> bool:
    return backend in available_loaders()


def lv2_installed(uri: str) -> bool:
    """Is this LV2 plugin installed? Answered by the parsed plugin catalogue.

    This used to search manifest text for the URI or its last path segment,
    which missed every bundle that abbreviates URIs with a prefix ending in '#'
    (Rubber Band) or lists plugins only in secondary files (SWH): both were
    reported "not installed" and their channels refused to start.
    """
    from . import lv2  # imported late: lv2 has no dependency on this module

    return lv2.catalogue().get(uri) is not None


@functools.lru_cache(maxsize=256)
def ladspa_installed(soname: str) -> bool:
    """Is a LADSPA shared object with this base name present?"""
    stem = soname[:-3] if soname.endswith(".so") else soname
    return any((d / f"{stem}.so").is_file() for d in _LADSPA_DIRS if d.is_dir())


def plugin_installed(backend: str, identifier: str) -> bool:
    if backend == "builtin":
        return True
    if backend == "lv2":
        return lv2_installed(identifier)
    if backend == "ladspa":
        if identifier.startswith("ladspa:"):  # a plugin effect: in the catalogue
            return lv2_installed(identifier)
        return ladspa_installed(identifier)
    return False


def reset_cache() -> None:
    """Forget probe results, for tests and after the user installs something."""
    available_loaders.cache_clear()
    ladspa_installed.cache_clear()
