"""The LV2 plugins installed on this machine, and which of them we can host.

A channel's graph is stereo, so a plugin is usable when it has one audio input
and one output (it is run once per side) or two of each (it is run once, and
sees both sides - which is what makes a compressor react to the pair together).
Anything else - instruments, analysers with no output, 1-in 2-out wideners,
plugins needing a host feature PipeWire does not provide - is still listed, with
the reason it cannot be used, so the browser can explain instead of hiding it.

Reading every description takes a few seconds (LSP alone is 16 MB of Turtle),
so the result is cached and only rebuilt when a description file changes.
"""

from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import turtle
from .turtle import IRI, BNode, Store

LV2 = "http://lv2plug.in/ns/lv2core#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DOAP = "http://usefulinc.com/ns/doap#"
UNITS = "http://lv2plug.in/ns/extensions/units#"
PP = "http://lv2plug.in/ns/ext/port-props#"
ATOM = "http://lv2plug.in/ns/ext/atom#"

#: Bump when the cached catalogue's shape changes.
CACHE_VERSION = 2

#: Host features PipeWire's LV2 loader provides (from the strings in
#: libspa-filter-graph-plugin-lv2.so, PipeWire 1.6). A plugin that *requires*
#: anything else fails to instantiate, and the whole channel with it.
SUPPORTED_FEATURES = frozenset(
    {
        "http://lv2plug.in/ns/ext/urid#map",
        "http://lv2plug.in/ns/ext/urid#unmap",
        "http://lv2plug.in/ns/ext/options#options",
        "http://lv2plug.in/ns/ext/worker#schedule",
        "http://lv2plug.in/ns/ext/log#log",
        "http://lv2plug.in/ns/ext/buf-size#boundedBlockLength",
        "http://lv2plug.in/ns/ext/buf-size#fixedBlockLength",
        "http://lv2plug.in/ns/ext/buf-size#powerOf2BlockLength",
        LV2 + "hardRTCapable",
        LV2 + "inPlaceBroken",
        LV2 + "isLive",
    }
)

#: Unit IRIs from the LV2 units extension, as a person would write them.
_UNIT_SYMBOLS = {
    "db": "dB", "hz": "Hz", "khz": "kHz", "mhz": "MHz", "ms": "ms", "s": "s",
    "min": "min", "pc": "%", "coef": "", "cent": "ct", "semitone12TET": "st",
    "oct": "oct", "bpm": "BPM", "beat": "beats", "bar": "bars", "m": "m",
    "cm": "cm", "mm": "mm", "km": "km", "degree": "°", "frame": "frames",
    "sample": "samples", "midiNote": "note",
}

#: Control ports that do nothing in a channel: ones that only drive a plugin's
#: own window (graph toggles, meter freezes), LSP's "link" mixes, which need
#: shared memory between plugin instances that PipeWire's host does not give,
#: and bypass switches, which the effect's own on/off box already covers.
_UI_ONLY = re.compile(
    r"visib|overlay|\bgraph\b|\bshow\b|\bzoom\b|pause|\bclear\b|\bfreeze\b|"
    r"\bui\b|\bfft\b|spectrum|analy[sz]|\blink mix\b|\blink to\b|^bypass$",
    re.IGNORECASE,
)


_VENDORS = (
    ("lsp-plug.in", "LSP"), ("calf", "Calf"), ("zamaudio", "ZAM"),
    ("drobilla.net/plugins/mda", "MDA"), ("gareus.org", "x42"), ("x42", "x42"),
    ("plugin.org.uk/swh", "SWH"), ("guitarix", "Guitarix"), ("breakfastquay", "Rubber Band"),
    ("noise-suppression-for-voice", "RNNoise"), ("eq10q", "EQ10Q"), ("hippie.lt", "abGate"),
    ("dragonfly", "Dragonfly"), ("invada", "Invada"), ("tap-plugins", "TAP"),
    ("tomszilagyi", "TAP"),
)

#: What an effect does, in the order the browser offers them.
CATEGORIES = (
    "Noise & gates",
    "Dynamics",
    "EQ & filters",
    "Pitch & voice",
    "Modulation",
    "Reverb & delay",
    "Distortion & amps",
    "Stereo & space",
    "Utility",
)


def categorise(name: str, classes: tuple[str, ...] | list[str]) -> str:
    """Sort an effect into one of CATEGORIES from its LV2 classes and name.

    Plugins declare classes (Compressor, Flanger, ParaEQ...), but some declare
    none - RNNoise among them - and a few declare something too general to
    help, so the name is consulted too. Order matters: a multiband gate is a
    gate before it is dynamics, and a noise *generator* is not noise removal.
    """
    text = name.lower()
    kinds = set(classes)
    if kinds & {"Generator", "Oscillator", "Instrument"} or "generator" in text:
        return "Utility"
    if kinds & {"Gate", "Expander"} or re.search(r"noise|denois|\bgate\b", text):
        return "Noise & gates"
    if kinds & {"Pitch"} or re.search(r"pitch|tune|vocoder|formant|shifter|detune", text):
        return "Pitch & voice"
    if kinds & {"Compressor", "Limiter", "Dynamics", "Envelope"} or re.search(r"compress|limit|de-?ess", text):
        return "Dynamics"
    if kinds & {"Chorus", "Flanger", "Phaser", "Modulator"} or re.search(
        r"chorus|flang|phaser|wah|tremolo|vibrato|rotary|ring ?mod|leslie", text
    ):
        return "Modulation"
    if kinds & {"Reverb", "Delay"} or re.search(r"reverb|delay|echo|room|ambience", text):
        return "Reverb & delay"
    if kinds & {"Distortion", "Waveshaper", "Simulator", "Amplifier"} or re.search(
        r"distort|overdrive|fuzz|saturat|crush|tube|\bamp\b|clipper|bandisto", text
    ):
        return "Distortion & amps"
    if kinds & {"EQ", "ParaEQ", "MultiEQ", "Filter", "Highpass", "Lowpass", "Bandpass", "Allpass", "Comb"} or re.search(
        r"equali[sz]|\beq\b|filter", text
    ):
        return "EQ & filters"
    if kinds & {"Spatial"} or re.search(r"stereo (tools|width|enhanc)|haas|crossfeed|binaural|panner", text):
        return "Stereo & space"
    return "Utility"


def search_path() -> list[Path]:
    """Where LV2 bundles live, honouring LV2_PATH the way hosts do."""
    configured = os.environ.get("LV2_PATH")
    if configured:
        return [Path(p).expanduser() for p in configured.split(":") if p]
    return [
        Path.home() / ".lv2",
        Path("/usr/local/lib64/lv2"),
        Path("/usr/local/lib/lv2"),
        Path("/usr/lib64/lv2"),
        Path("/usr/lib/lv2"),
        Path("/usr/lib/x86_64-linux-gnu/lv2"),
    ]


@dataclass(frozen=True)
class Control:
    """One adjustable input on a plugin, in the plugin's own units."""

    symbol: str
    name: str
    index: int
    minimum: float
    maximum: float
    default: float
    unit: str = ""
    toggled: bool = False
    integer: bool = False
    logarithmic: bool = False
    #: (label, value) pairs when the port is a list of named choices.
    choices: tuple[tuple[str, float], ...] = ()
    comment: str = ""
    #: Not worth a control of its own - see _UI_ONLY - but still set to default.
    hidden: bool = False

    @property
    def is_gain(self) -> bool:
        """A linear gain factor, which people read far better as dB."""
        return self.unit == "G" and self.minimum >= 0 and self.maximum > 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Control:
        data = dict(data)
        data["choices"] = tuple((str(label), float(value)) for label, value in data.get("choices", ()))
        return cls(**data)


@dataclass(frozen=True)
class Plugin:
    uri: str
    name: str
    bundle: str
    classes: tuple[str, ...] = ()
    audio_in: tuple[str, ...] = ()
    audio_out: tuple[str, ...] = ()
    controls: tuple[Control, ...] = ()
    #: Input ports we leave unconnected (sidechains, optional MIDI/atom).
    spare_inputs: tuple[str, ...] = ()
    #: Why this plugin cannot be used here; empty when it can.
    problems: tuple[str, ...] = ()
    #: Symbol of the port that switches the plugin on, which we pin to 1.
    enable_port: str = ""

    @property
    def usable(self) -> bool:
        return not self.problems

    @property
    def stereo(self) -> bool:
        return len(self.audio_in) == 2

    @property
    def vendor(self) -> str:
        """Who makes it, as a person would say it, from the plugin's URI."""
        uri = self.uri.lower()
        # Specific needles only: a bare "tap" matched SWH's tapeDelay.
        for needle, label in _VENDORS:
            if needle in uri:
                return label
        match = re.match(r"^(?:https?://|urn:)(?:www\.)?([^/:#]+)", self.uri)
        return match.group(1) if match else "Other"

    @property
    def category(self) -> str:
        return categorise(self.name, self.classes)

    def control(self, symbol: str) -> Control:
        for control in self.controls:
            if control.symbol == symbol:
                return control
        raise KeyError(symbol)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plugin:
        data = dict(data)
        data["controls"] = tuple(Control.from_dict(c) for c in data.get("controls", ()))
        for key in ("classes", "audio_in", "audio_out", "spare_inputs", "problems"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


# --- reading descriptions ------------------------------------------------


def _number(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _unit(store: Store, port: str) -> str:
    unit = store.value(port, UNITS + "unit")
    if unit is None:
        return ""
    if isinstance(unit, BNode):
        symbol = store.value(unit, UNITS + "symbol") or store.value(unit, RDFS + "label") or ""
        return str(symbol).strip()
    text = str(unit)
    if text.startswith(UNITS):
        return _UNIT_SYMBOLS.get(text[len(UNITS):], text[len(UNITS):])
    return ""


def _read_plugin(store: Store, uri: str, bundle: Path) -> Plugin:
    types = {str(t) for t in store.types(uri)}
    classes = tuple(
        sorted(t[len(LV2):].removesuffix("Plugin") for t in types if t.startswith(LV2) and t != LV2 + "Plugin")
    )
    name = store.value(uri, DOAP + "name") or store.value(uri, RDFS + "label") or uri.rsplit("/", 1)[-1]
    problems: list[str] = []

    for feature in store.objects(uri, LV2 + "requiredFeature"):
        if str(feature) not in SUPPORTED_FEATURES:
            problems.append(f"needs a host feature PipeWire lacks ({str(feature).rsplit('/', 1)[-1]})")

    ports = []
    for port in store.objects(uri, LV2 + "port"):
        ports.append((int(_number(store.value(port, LV2 + "index"), 0)), port))
    ports.sort(key=lambda item: item[0])

    audio_in: list[str] = []
    audio_out: list[str] = []
    spare: list[str] = []
    controls: list[Control] = []
    enable_port = ""
    for index, port in ports:
        port_types = {str(t) for t in store.types(port)}
        symbol = str(store.value(port, LV2 + "symbol", ""))
        props = {str(p) for p in store.objects(port, LV2 + "portProperty")}
        is_input = LV2 + "InputPort" in port_types
        optional = LV2 + "connectionOptional" in props
        if LV2 + "AudioPort" in port_types:
            if is_input and LV2 + "isSideChain" in props:
                spare.append(symbol)
            elif is_input:
                audio_in.append(symbol)
            else:
                audio_out.append(symbol)
        elif LV2 + "ControlPort" in port_types:
            if not is_input:
                continue  # meters and latency reports; nothing to set
            designation = str(store.value(port, LV2 + "designation", ""))
            if designation == LV2 + "enabled":
                enable_port = symbol
                continue
            if designation in (LV2 + "freeWheeling", LV2 + "latency"):
                continue
            minimum = _number(store.value(port, LV2 + "minimum"), 0.0)
            maximum = _number(store.value(port, LV2 + "maximum"), 1.0)
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            default = _number(store.value(port, LV2 + "default"), minimum)
            default = max(minimum, min(maximum, default))
            choices: list[tuple[str, float]] = []
            for point in store.objects(port, LV2 + "scalePoint"):
                label = store.value(point, RDFS + "label")
                value = store.value(point, RDF + "value")
                if label is not None and value is not None:
                    choices.append((str(label), _number(value, 0.0)))
            choices.sort(key=lambda c: c[1])
            enumeration = LV2 + "enumeration" in props and bool(choices)
            label = str(store.value(port, LV2 + "name", symbol))
            hidden = (
                PP + "notOnGUI" in props
                or PP + "trigger" in props
                or bool(_UI_ONLY.search(label))
            )
            controls.append(
                Control(
                    symbol=symbol,
                    name=label,
                    index=index,
                    minimum=minimum,
                    maximum=maximum,
                    default=default,
                    unit=_unit(store, port),
                    toggled=LV2 + "toggled" in props,
                    integer=LV2 + "integer" in props or enumeration,
                    logarithmic=PP + "logarithmic" in props,
                    choices=tuple(choices) if enumeration else (),
                    comment=str(store.value(port, RDFS + "comment", "")),
                    hidden=hidden,
                )
            )
        elif LV2 + "CVPort" in port_types:
            if is_input and not optional:
                problems.append("uses control-voltage ports")
        elif ATOM + "AtomPort" in port_types or not port_types - {LV2 + "InputPort", LV2 + "OutputPort"}:
            if is_input:
                spare.append(symbol)
        elif is_input and not optional:
            problems.append(f"has a port type we cannot connect ({symbol})")

    layout = (len(audio_in), len(audio_out))
    if layout not in ((1, 1), (2, 2)):
        if not audio_in:
            problems.append("makes sound rather than processing it")
        elif not audio_out:
            problems.append("has no audio output")
        else:
            problems.append(f"has {layout[0]} inputs and {layout[1]} outputs; only mono or stereo effects fit")

    return Plugin(
        uri=uri,
        name=str(name).strip(),
        bundle=str(bundle),
        classes=classes,
        audio_in=tuple(audio_in),
        audio_out=tuple(audio_out),
        controls=tuple(controls),
        spare_inputs=tuple(spare),
        problems=tuple(dict.fromkeys(problems)),
        enable_port=enable_port,
    )


def read_bundle(bundle: Path) -> list[Plugin]:
    """Every plugin a bundle declares. Raises TurtleError on unreadable Turtle."""
    manifest = bundle / "manifest.ttl"
    store = Store()
    store.add(turtle.parse_file(manifest))
    plugin_type = LV2 + "Plugin"
    uris = store.subjects_of_type(plugin_type)
    loaded = {manifest.resolve().as_uri()}
    for uri in uris:
        for see_also in store.objects(uri, RDFS + "seeAlso"):
            target = str(see_also)
            if target in loaded or not target.startswith("file://"):
                continue
            loaded.add(target)
            path = Path(target[len("file://"):])
            if path.is_file():
                store.add(turtle.parse_file(path))
    return [_read_plugin(store, uri, bundle) for uri in uris]


def _bundles() -> list[Path]:
    found: list[Path] = []
    for root in search_path():
        if not root.is_dir():
            continue
        for bundle in sorted(root.glob("*.lv2")):
            if (bundle / "manifest.ttl").is_file():
                found.append(bundle)
    return found


def _fingerprint(bundles: list[Path]) -> list[list[Any]]:
    marks: list[list[Any]] = []
    for bundle in bundles:
        for path in sorted(bundle.glob("*.ttl")):
            try:
                info = path.stat()
            except OSError:
                continue
            marks.append([str(path), info.st_mtime_ns, info.st_size])
    return marks


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "audiorouter" / "lv2-catalogue.json"


@dataclass
class Catalogue:
    plugins: dict[str, Plugin] = field(default_factory=dict)
    #: Bundles that could not be read, with the reason.
    unreadable: dict[str, str] = field(default_factory=dict)

    def get(self, uri: str) -> Plugin | None:
        return self.plugins.get(uri)

    def usable(self) -> list[Plugin]:
        return sorted((p for p in self.plugins.values() if p.usable), key=lambda p: (p.vendor, p.name.lower()))

    def all(self) -> list[Plugin]:
        return sorted(self.plugins.values(), key=lambda p: (p.vendor, p.name.lower()))


def build(bundles: list[Path] | None = None) -> Catalogue:
    catalogue = Catalogue()
    for bundle in _bundles() if bundles is None else bundles:
        try:
            plugins = read_bundle(bundle)
        except (turtle.TurtleError, OSError, UnicodeError) as exc:
            catalogue.unreadable[str(bundle)] = str(exc)
            continue
        for plugin in plugins:
            # First on the search path wins, as in every LV2 host.
            catalogue.plugins.setdefault(plugin.uri, plugin)
    return catalogue


@functools.lru_cache(maxsize=1)
def catalogue() -> Catalogue:
    """The installed plugins, from cache when nothing has changed."""
    bundles = _bundles()
    fingerprint = _fingerprint(bundles)
    path = cache_path()
    try:
        cached = json.loads(path.read_text())
        if cached.get("version") == CACHE_VERSION and cached.get("fingerprint") == fingerprint:
            return Catalogue(
                plugins={p["uri"]: Plugin.from_dict(p) for p in cached["plugins"]},
                unreadable=dict(cached.get("unreadable", {})),
            )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = build(bundles)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "version": CACHE_VERSION,
                    "fingerprint": fingerprint,
                    "plugins": [p.to_dict() for p in result.plugins.values()],
                    "unreadable": result.unreadable,
                }
            )
        )
        temp.replace(path)
    except OSError:
        pass  # a cache we cannot write only costs time
    return result


def reset_cache() -> None:
    catalogue.cache_clear()
