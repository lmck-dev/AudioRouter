"""Plugins we build ourselves, on the machine that runs them.

- **Voice noise suppression** around the system's librnnoise (`rnnoise/`). The
  packaged RNNoise LV2 plugin misbehaves inside PipeWire's filter-chain - half
  level, late, distorted - and keeps its settings where filter-chain cannot
  reach them, so we wrap the library with a plain LV2 plugin of our own.
- **The level tap** (`meter/`), placed between effects so the window can show
  what each effect receives and puts out. PipeWire lists a plugin's output
  controls but never refreshes them, so the tap publishes through a file.

Each is compiled into the user's `~/.lv2`, where PipeWire's LV2 loader and our
catalogue both look, whenever its bundle is missing or older than its source.
Building needs a C compiler (and `librnnoise.so.0` for noise suppression), and
nothing else - no headers. When something is missing, nothing is built and
nothing breaks: the effect, or the per-effect meters, simply are not offered.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_LIBRARY_DIRS = (Path("/usr/lib64"), Path("/usr/lib"), Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/local/lib"))
_SYSTEM_LV2_DIRS = (Path("/usr/lib64/lv2"), Path("/usr/lib/lv2"))
_HERE = Path(__file__).resolve().parent

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class NativePlugin:
    """One plugin we compile: where its source is and what it links against."""

    uri: str
    title: str          # for messages: "noise suppression"
    source: str         # directory under native/
    c_file: str
    binary: str
    bundle: str
    turtle: tuple[str, ...]
    library: str = ""   # a shared library it needs, linked by file name

    @property
    def source_dir(self) -> Path:
        return _HERE / self.source


RNNOISE = NativePlugin(
    uri="urn:audiorouter:rnnoise",
    title="noise suppression",
    source="rnnoise",
    c_file="audiorouter_rnnoise.c",
    binary="audiorouter_rnnoise.so",
    bundle="audiorouter-rnnoise.lv2",
    turtle=("manifest.ttl", "rnnoise.ttl"),
    library="librnnoise.so.0",
)
METER = NativePlugin(
    uri="urn:audiorouter:meter",
    title="effect level meters",
    source="meter",
    c_file="audiorouter_meter.c",
    binary="audiorouter_meter.so",
    bundle="audiorouter-meter.lv2",
    turtle=("manifest.ttl", "meter.ttl"),
)
ALL = (RNNOISE, METER)

# The names the rest of the code and the tests have always used.
RNNOISE_URI = RNNOISE.uri
RNNOISE_BUNDLE = RNNOISE.bundle
METER_URI = METER.uri
_SOURCE = RNNOISE.source_dir
_C_FILE = RNNOISE.c_file
_LIBRARY = RNNOISE.library
_TURTLE = RNNOISE.turtle


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def user_lv2_dir() -> Path:
    return Path.home() / ".lv2"


def compiler() -> str | None:
    return shutil.which("cc") or shutil.which("gcc")


def library_present(name: str) -> bool:
    return any((d / name).exists() for d in _LIBRARY_DIRS)


def missing_for(plugin: NativePlugin) -> list[str]:
    """What this machine lacks to build `plugin`, as package names."""
    missing = []
    if compiler() is None:
        missing.append("gcc")
    if plugin.library and not library_present(plugin.library):
        missing.append(plugin.library.split(".so")[0].removeprefix("lib"))
    return missing


def bundle_path(plugin: NativePlugin, root: Path | None = None) -> Path:
    return (root or user_lv2_dir()) / plugin.bundle


def system_bundle(plugin: NativePlugin) -> Path | None:
    """The copy the package installed, already built, if there is one."""
    for d in _SYSTEM_LV2_DIRS:
        bundle = d / plugin.bundle
        if (bundle / plugin.binary).is_file():
            return bundle
    return None


def up_to_date(plugin: NativePlugin, root: Path | None = None) -> bool:
    bundle = bundle_path(plugin, root)
    built = bundle / plugin.binary
    if not built.is_file():
        return False
    src = plugin.source_dir
    newest = max((src / name).stat().st_mtime_ns for name in (plugin.c_file, *plugin.turtle))
    return all((bundle / name).is_file() for name in plugin.turtle) and built.stat().st_mtime_ns >= newest


def build(plugin: NativePlugin, root: Path | None = None, run: Runner = _run) -> Path:
    """Compile the bundle into `root` (default ~/.lv2), replacing any older one."""
    missing = missing_for(plugin)
    if missing:
        raise BuildError(f"cannot build {plugin.title}; install: {' '.join(missing)}")
    target = bundle_path(plugin, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the target and swap it in, so a channel starting meanwhile
    # never loads half a bundle.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".build-") as work:
        staged = Path(work) / plugin.bundle
        staged.mkdir()
        command = [
            compiler() or "cc", "-O2", "-shared", "-fPIC", "-fvisibility=hidden",
            "-o", str(staged / plugin.binary), str(plugin.source_dir / plugin.c_file),
        ]
        if plugin.library:
            command.append(f"-l:{plugin.library}")
        result = run([*command, "-lm"])
        if result.returncode != 0:
            raise BuildError(f"building {plugin.title} failed:\n{result.stderr.strip()}")
        for name in plugin.turtle:
            shutil.copy2(plugin.source_dir / name, staged / name)
        old = Path(work) / "old"
        if target.exists():
            target.rename(old)
        staged.rename(target)
    return target


def ensure(plugin: NativePlugin, root: Path | None = None, run: Runner = _run) -> Path | None:
    """Build `plugin` if it is missing or stale and this machine can.

    Returns the bundle when one is in place, None when it cannot be built.
    Never raises: a missing plugin must not stop the app starting. Installed
    from the package, the plugin is already built system-wide, and nothing is
    compiled unless this user already has a copy of their own (a checkout
    keeps that copy current with its source).
    """
    if root is None and not bundle_path(plugin).exists():
        system = system_bundle(plugin)
        if system is not None:
            return system
    if up_to_date(plugin, root):
        return bundle_path(plugin, root)
    if missing_for(plugin):
        return None
    try:
        bundle = build(plugin, root, run)
    except (BuildError, OSError):
        return None
    from .. import lv2, plugins

    lv2.reset_cache()
    plugins.reset_cache()
    return bundle


def ensure_all(root: Path | None = None, run: Runner = _run) -> None:
    """Every plugin we build: at window start and before the daemon applies."""
    for plugin in ALL:
        ensure(plugin, root, run)


# -- noise suppression, by its long-standing names ---------------------------

def rnnoise_bundle(root: Path | None = None) -> Path:
    return bundle_path(RNNOISE, root)


def rnnoise_library_present() -> bool:
    return library_present(RNNOISE.library)


def missing_for_rnnoise() -> list[str]:
    return missing_for(RNNOISE)


def system_rnnoise_bundle() -> Path | None:
    return system_bundle(RNNOISE)


def rnnoise_up_to_date(root: Path | None = None) -> bool:
    return up_to_date(RNNOISE, root)


def build_rnnoise(root: Path | None = None, run: Runner = _run) -> Path:
    return build(RNNOISE, root, run)


def ensure_rnnoise(root: Path | None = None, run: Runner = _run) -> Path | None:
    return ensure(RNNOISE, root, run)
