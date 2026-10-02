"""Installed LADSPA plugins, described like LV2 ones so every effect UI works.

A LADSPA plugin describes itself only from inside its shared object (the
`ladspa_descriptor` entry point), so reading one means loading it. That is done
in a CHILD process (`python -m audiorouter.ladspa FILE...`): loading runs the
library's own code, and a broken plugin must not take the window or the login
service down with it. A file that crashes the batch is retried on its own and
skipped if it crashes again.

Each plugin becomes an `lv2.Plugin` whose uri is `ladspa:<file>#<label>`, its
controls keyed by PORT NAME - that is how filter-chain addresses LADSPA controls.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PREFIX = "ladspa:"

# Port descriptor bits (ladspa.h).
_INPUT, _OUTPUT, _CONTROL, _AUDIO = 0x1, 0x2, 0x4, 0x8
# Range hint bits.
_BELOW, _ABOVE, _TOGGLED, _SAMPLE_RATE, _LOG, _INTEGER = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20
_DEFAULT_MASK = 0x3C0
#: The rate a "times the sample rate" bound is read at: the desktop's usual one.
SAMPLE_RATE = 48000.0


def uri(path: str | Path, label: str) -> str:
    return f"{PREFIX}{path}#{label}"


def split(plugin_uri: str) -> tuple[str, str]:
    """`ladspa:<file>#<label>` -> (file, label)."""
    body = plugin_uri[len(PREFIX):]
    path, _sep, label = body.rpartition("#")
    return path, label


def is_ladspa(plugin_uri: str) -> bool:
    return plugin_uri.startswith(PREFIX)


# --- reading one file (runs in the child) --------------------------------


class _Hint(ctypes.Structure):
    _fields_ = [("hint", ctypes.c_int), ("lower", ctypes.c_float), ("upper", ctypes.c_float)]


class _Descriptor(ctypes.Structure):
    _fields_ = [
        ("unique_id", ctypes.c_ulong),
        ("label", ctypes.c_char_p),
        ("properties", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("maker", ctypes.c_char_p),
        ("copyright", ctypes.c_char_p),
        ("port_count", ctypes.c_ulong),
        ("port_descriptors", ctypes.POINTER(ctypes.c_int)),
        ("port_names", ctypes.POINTER(ctypes.c_char_p)),
        ("port_hints", ctypes.POINTER(_Hint)),
    ]


def _text(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", "replace").strip()


def default_value(hint: int, lower: float, upper: float) -> float:
    """The default a LADSPA range hint asks for (ladspa.h's table)."""
    kind = hint & _DEFAULT_MASK
    log = bool(hint & _LOG) and lower > 0 and upper > 0

    def between(weight: float) -> float:
        if log:
            return math.exp(math.log(lower) * (1 - weight) + math.log(upper) * weight)
        return lower * (1 - weight) + upper * weight

    return {
        0x40: lower,
        0x80: between(0.25),
        0xC0: between(0.5),
        0x100: between(0.75),
        0x140: upper,
        0x200: 0.0,
        0x240: 1.0,
        0x280: 100.0,
        0x2C0: 440.0,
    }.get(kind, lower)


def _control(name: str, index: int, hint: int, lower: float, upper: float) -> dict[str, Any]:
    if hint & _SAMPLE_RATE:
        lower, upper = lower * SAMPLE_RATE, upper * SAMPLE_RATE
    toggled = bool(hint & _TOGGLED)
    if toggled:
        lower, upper = 0.0, 1.0
    if not hint & _BELOW:
        lower = 0.0
    if not hint & _ABOVE:
        # Unbounded: give the slider room above whatever it starts at.
        upper = max(1.0, lower + 1.0, default_value(hint & _DEFAULT_MASK, lower, lower) * 2)
    if upper < lower:
        lower, upper = upper, lower
    default = max(lower, min(upper, default_value(hint, lower, upper)))
    return {
        "symbol": name,
        "name": name,
        "index": index,
        "minimum": lower,
        "maximum": upper,
        "default": default,
        "toggled": toggled,
        "integer": bool(hint & _INTEGER),
        "logarithmic": bool(hint & _LOG) and lower > 0,
    }


def read_file(path: str) -> list[dict[str, Any]]:
    """Every plugin in one LADSPA file, as `lv2.Plugin` dicts. Loads the file."""
    library = ctypes.CDLL(path)
    try:
        entry = library.ladspa_descriptor
    except AttributeError:
        return []  # some other kind of shared object (a VST, a helper library)
    entry.restype = ctypes.POINTER(_Descriptor)
    entry.argtypes = [ctypes.c_ulong]
    found = []
    for number in range(1024):
        pointer = entry(number)
        if not pointer:
            break
        d = pointer.contents
        label = _text(d.label)
        audio_in: list[str] = []
        audio_out: list[str] = []
        controls: list[dict[str, Any]] = []
        for i in range(d.port_count):
            kind = d.port_descriptors[i]
            name = _text(d.port_names[i]) or f"port {i}"
            if kind & _AUDIO:
                (audio_in if kind & _INPUT else audio_out).append(name)
            elif kind & _CONTROL and kind & _INPUT:
                hint = d.port_hints[i]
                controls.append(_control(name, i, hint.hint, hint.lower, hint.upper))
        found.append({
            "uri": uri(path, label),
            "name": _text(d.name) or label,
            "bundle": path,
            "maker": _text(d.maker),
            "audio_in": audio_in,
            "audio_out": audio_out,
            "controls": controls,
            "problems": _problems(audio_in, audio_out),
        })
    return found


def _problems(audio_in: list[str], audio_out: list[str]) -> list[str]:
    layout = (len(audio_in), len(audio_out))
    if layout in ((1, 1), (2, 2)):
        return []
    if not audio_in:
        return ["makes sound rather than processing it"]
    if not audio_out:
        return ["has no audio output"]
    return [f"has {layout[0]} inputs and {layout[1]} outputs; only mono or stereo effects fit"]


# --- reading many files (runs here, starts the child) --------------------


def _probe(files: list[str], timeout: float) -> dict[str, list[dict[str, Any]]] | None:
    """Read `files` in a child process. None if the child died or hung."""
    try:
        done = subprocess.run(
            [sys.executable, "-m", "audiorouter.ladspa", *files],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def read_files(files: list[Path]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """(plugins, unreadable files with the reason), from many LADSPA files."""
    names = [str(f) for f in files]
    if not names:
        return [], {}
    result = _probe(names, timeout=60)
    unreadable: dict[str, str] = {}
    if result is None:
        # Something in the batch crashed: find it by reading one at a time.
        result = {}
        for name in names:
            one = _probe([name], timeout=10)
            if one is None:
                unreadable[name] = "crashed or hung while being read"
            else:
                result.update(one)
    plugins = [plugin for name in names for plugin in result.get(name, [])]
    for name, value in result.items():
        if isinstance(value, str):
            unreadable[name] = value
    return [p for p in plugins if isinstance(p, dict)], unreadable


def _main(argv: list[str]) -> int:
    out: dict[str, Any] = {}
    for path in argv:
        try:
            out[path] = read_file(path)
        except OSError as exc:
            out[path] = f"cannot be loaded: {exc}"
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
