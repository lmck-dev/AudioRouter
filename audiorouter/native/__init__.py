"""Plugins we build ourselves, on the machine that runs them.

Today there is one: voice noise suppression around the system's librnnoise
(`rnnoise/`). The packaged RNNoise LV2 plugin misbehaves inside PipeWire's
filter-chain - half level, late, distorted - and keeps its settings where
filter-chain cannot reach them, so we wrap the library with a plain LV2 plugin
of our own.

It is compiled into the user's `~/.lv2`, where PipeWire's LV2 loader and our
catalogue both look, whenever the bundle is missing or older than its source.
Building needs a C compiler and `librnnoise.so.0`, and nothing else - no
headers. When either is missing, nothing is built and nothing breaks: the
effect simply does not appear in the browser.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

RNNOISE_URI = "urn:audiorouter:rnnoise"
RNNOISE_BUNDLE = "audiorouter-rnnoise.lv2"
_SOURCE = Path(__file__).resolve().parent / "rnnoise"
_C_FILE = "audiorouter_rnnoise.c"
_LIBRARY = "librnnoise.so.0"
_LIBRARY_DIRS = (Path("/usr/lib64"), Path("/usr/lib"), Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/local/lib"))
_TURTLE = ("manifest.ttl", "rnnoise.ttl")
_SYSTEM_LV2_DIRS = (Path("/usr/lib64/lv2"), Path("/usr/lib/lv2"))

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class BuildError(RuntimeError):
    pass


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def user_lv2_dir() -> Path:
    return Path.home() / ".lv2"


def rnnoise_bundle(root: Path | None = None) -> Path:
    return (root or user_lv2_dir()) / RNNOISE_BUNDLE


def compiler() -> str | None:
    return shutil.which("cc") or shutil.which("gcc")


def rnnoise_library_present() -> bool:
    return any((d / _LIBRARY).exists() for d in _LIBRARY_DIRS)


def missing_for_rnnoise() -> list[str]:
    """What this machine lacks to build the plugin, as package names."""
    missing = []
    if compiler() is None:
        missing.append("gcc")
    if not rnnoise_library_present():
        missing.append("rnnoise")
    return missing


def system_rnnoise_bundle() -> Path | None:
    """The copy the package installed, already built, if there is one."""
    for d in _SYSTEM_LV2_DIRS:
        bundle = d / RNNOISE_BUNDLE
        if (bundle / "audiorouter_rnnoise.so").is_file():
            return bundle
    return None


def rnnoise_up_to_date(root: Path | None = None) -> bool:
    bundle = rnnoise_bundle(root)
    built = bundle / "audiorouter_rnnoise.so"
    if not built.is_file():
        return False
    newest = max((_SOURCE / name).stat().st_mtime_ns for name in (_C_FILE, *_TURTLE))
    return all((bundle / name).is_file() for name in _TURTLE) and built.stat().st_mtime_ns >= newest


def build_rnnoise(root: Path | None = None, run: Runner = _run) -> Path:
    """Compile the bundle into `root` (default ~/.lv2), replacing any older one."""
    missing = missing_for_rnnoise()
    if missing:
        raise BuildError(f"cannot build noise suppression; install: {' '.join(missing)}")
    target = rnnoise_bundle(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the target and swap it in, so a channel starting meanwhile
    # never loads half a bundle.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".build-") as work:
        staged = Path(work) / RNNOISE_BUNDLE
        staged.mkdir()
        result = run([
            compiler() or "cc", "-O2", "-shared", "-fPIC", "-fvisibility=hidden",
            "-o", str(staged / "audiorouter_rnnoise.so"), str(_SOURCE / _C_FILE),
            f"-l:{_LIBRARY}", "-lm",
        ])
        if result.returncode != 0:
            raise BuildError(f"building noise suppression failed:\n{result.stderr.strip()}")
        for name in _TURTLE:
            shutil.copy2(_SOURCE / name, staged / name)
        old = Path(work) / "old"
        if target.exists():
            target.rename(old)
        staged.rename(target)
    return target


def ensure_rnnoise(root: Path | None = None, run: Runner = _run) -> Path | None:
    """Build the plugin if it is missing or stale and this machine can.

    Returns the bundle when one is in place, None when it cannot be built.
    Never raises: a missing noise suppressor must not stop the app starting.
    Installed from the package, the plugin is already built system-wide, and
    nothing is compiled unless this user already has a copy of their own
    (a checkout keeps that copy current with its source).
    """
    if root is None and not rnnoise_bundle().exists():
        system = system_rnnoise_bundle()
        if system is not None:
            return system
    if rnnoise_up_to_date(root):
        return rnnoise_bundle(root)
    if missing_for_rnnoise():
        return None
    try:
        bundle = build_rnnoise(root, run)
    except (BuildError, OSError):
        return None
    from .. import lv2, plugins

    lv2.reset_cache()
    plugins.reset_cache()
    return bundle
