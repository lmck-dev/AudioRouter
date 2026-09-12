"""Effect definitions and their translation into filter-chain graph fragments.

The graph is built mono and PipeWire replicates it across the channel's
channels, so every effect here has exactly one audio input and one audio
output. That keeps chain assembly uniform at the cost of stereo linking: a
compressor reacts to each side independently. Stereo-linked dynamics would need
an explicitly wired stereo graph, which is a later change, not a tweak.

Gain values are exchanged in decibels at this layer because that is what a user
understands; conversion to the linear multipliers the plugins want happens at
render time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .plugins import Requirement

LSP = "http://lsp-plug.in/plugins/lv2/"

#: Package that teaches PipeWire to load LV2 plugins. Not installed by default
#: on Fedora, and its absence is only reported as ENOENT at process start.
LV2_PACKAGE = "pipewire-module-filter-chain-lv2"


class EffectError(ValueError):
    """An effect kind or parameter value was not usable."""


def db_to_linear(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


@dataclass(frozen=True)
class ParamSpec:
    """One user-facing knob."""

    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    unit: str = ""
    step: float = 1.0

    def clamp(self, value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise EffectError(f"{self.key}: {value!r} is not a number") from exc
        if math.isnan(number) or math.isinf(number):
            raise EffectError(f"{self.key}: {value!r} is not a finite number")
        return max(self.minimum, min(self.maximum, number))


@dataclass(frozen=True)
class Fragment:
    """A rendered piece of a filter graph, with its entry and exit ports."""

    nodes: list[dict[str, Any]]
    links: list[dict[str, str]]
    input_port: str
    output_port: str


@dataclass(frozen=True)
class Chain:
    """A whole channel's graph: nodes, links, and the ports the stream binds to.

    The ports are not a convenience. PipeWire derives a filter's inputs from
    every *unconnected* input port in the graph, so one `mixer` node - which has
    eight inputs and only ever uses one - makes the filter look like an 8-in
    1-out device. The graph then refuses to start with "invalid ports" and the
    channel is silent while still appearing in the graph, fully linked, as if it
    were working. Naming the entry and exit ports explicitly is what prevents
    that, and it must stay for any effect whose plugin has spare ports.
    """

    nodes: list[dict[str, Any]]
    links: list[dict[str, str]]
    input_port: str
    output_port: str


@dataclass(frozen=True)
class EffectSpec:
    kind: str
    label: str
    summary: str
    params: tuple[ParamSpec, ...]
    #: Plugins this effect needs; empty when it is built into PipeWire.
    requires: tuple[Requirement, ...] = ()

    def unsatisfied(self) -> list[Requirement]:
        return [r for r in self.requires if not r.satisfied()]

    @property
    def available(self) -> bool:
        return not self.unsatisfied()

    def param(self, key: str) -> ParamSpec:
        for spec in self.params:
            if spec.key == key:
                return spec
        raise EffectError(f"{self.kind} has no parameter {key!r}")

    def defaults(self) -> dict[str, float]:
        return {p.key: p.default for p in self.params}

    def normalise(self, values: Mapping[str, Any] | None) -> dict[str, float]:
        """Fill in defaults and clamp everything into range."""
        merged = self.defaults()
        for key, value in (values or {}).items():
            merged[key] = self.param(key).clamp(value)
        return merged


def _biquad_stack(name: str, label: str, values: Mapping[str, float], stages: int) -> Fragment:
    """`stages` identical biquads in series; each stage is 12 dB/octave."""
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    for index in range(stages):
        stage = f"{name}_{index}"
        nodes.append(
            {
                "type": "builtin",
                "name": stage,
                "label": label,
                "control": {
                    "Freq": round(float(values["frequency"]), 4),
                    "Q": round(float(values.get("q", 0.707)), 4),
                    "Gain": 0.0,
                },
            }
        )
        if index:
            links.append({"output": f"{name}_{index - 1}:Out", "input": f"{stage}:In"})
    return Fragment(nodes, links, f"{name}_0:In", f"{name}_{stages - 1}:Out")


# --- registry ------------------------------------------------------------

_SPECS: dict[str, EffectSpec] = {}


def register(spec: EffectSpec) -> EffectSpec:
    _SPECS[spec.kind] = spec
    return spec


GAIN = register(
    EffectSpec(
        kind="gain",
        label="Volume trim",
        summary="Level offset for this channel, so channels can be matched by ear.",
        params=(ParamSpec("gain_db", "Gain", 0.0, -40.0, 20.0, "dB", 0.5),),
    )
)

LOWPASS = register(
    EffectSpec(
        kind="lowpass",
        label="Low-pass",
        summary="Removes treble above the cutoff.",
        params=(
            ParamSpec("frequency", "Cutoff", 2000.0, 20.0, 20000.0, "Hz", 10.0),
            ParamSpec("q", "Resonance", 0.707, 0.1, 4.0, "Q", 0.05),
            ParamSpec("poles", "Steepness", 2, 1, 4, "x12dB/oct", 1.0),
        ),
    )
)

HIGHPASS = register(
    EffectSpec(
        kind="highpass",
        label="High-pass",
        summary="Removes bass below the cutoff. Protects small speakers.",
        params=(
            ParamSpec("frequency", "Cutoff", 100.0, 20.0, 20000.0, "Hz", 10.0),
            ParamSpec("q", "Resonance", 0.707, 0.1, 4.0, "Q", 0.05),
            ParamSpec("poles", "Steepness", 2, 1, 4, "x12dB/oct", 1.0),
        ),
    )
)

PEAKING = register(
    EffectSpec(
        kind="peaking",
        label="Tone band",
        summary="Boost or cut a band. Stack several to build an equaliser.",
        params=(
            ParamSpec("frequency", "Frequency", 1000.0, 20.0, 20000.0, "Hz", 10.0),
            ParamSpec("gain_db", "Gain", 0.0, -24.0, 24.0, "dB", 0.5),
            ParamSpec("q", "Width", 1.0, 0.1, 10.0, "Q", 0.05),
        ),
    )
)

LOWSHELF = register(
    EffectSpec(
        kind="lowshelf",
        label="Bass shelf",
        summary="Lifts or drops everything below the frequency.",
        params=(
            ParamSpec("frequency", "Frequency", 120.0, 20.0, 2000.0, "Hz", 10.0),
            ParamSpec("gain_db", "Gain", 0.0, -24.0, 24.0, "dB", 0.5),
            ParamSpec("q", "Width", 0.707, 0.1, 4.0, "Q", 0.05),
        ),
    )
)

HIGHSHELF = register(
    EffectSpec(
        kind="highshelf",
        label="Treble shelf",
        summary="Lifts or drops everything above the frequency.",
        params=(
            ParamSpec("frequency", "Frequency", 6000.0, 500.0, 20000.0, "Hz", 100.0),
            ParamSpec("gain_db", "Gain", 0.0, -24.0, 24.0, "dB", 0.5),
            ParamSpec("q", "Width", 0.707, 0.1, 4.0, "Q", 0.05),
        ),
    )
)

DELAY = register(
    EffectSpec(
        kind="delay",
        label="Delay",
        summary="Delays this channel, for aligning speakers in a room.",
        params=(ParamSpec("delay_ms", "Delay", 0.0, 0.0, 500.0, "ms", 1.0),),
    )
)

COMPRESSOR = register(
    EffectSpec(
        kind="compressor",
        label="Compressor",
        summary="Evens out loud and quiet passages.",
        params=(
            ParamSpec("threshold_db", "Threshold", -18.0, -60.0, 0.0, "dB", 0.5),
            ParamSpec("ratio", "Ratio", 4.0, 1.0, 20.0, ":1", 0.1),
            ParamSpec("attack_ms", "Attack", 20.0, 0.0, 2000.0, "ms", 1.0),
            ParamSpec("release_ms", "Release", 100.0, 0.0, 5000.0, "ms", 5.0),
            ParamSpec("makeup_db", "Makeup", 0.0, -24.0, 24.0, "dB", 0.5),
        ),
        requires=(Requirement("lv2", f"{LSP}compressor_mono", LV2_PACKAGE),),
    )
)

LIMITER = register(
    EffectSpec(
        kind="limiter",
        label="Limiter",
        summary="Hard ceiling on peaks. Stops sudden loud sounds in headphones.",
        params=(
            ParamSpec("threshold_db", "Ceiling", -1.0, -40.0, 0.0, "dB", 0.5),
            ParamSpec("lookahead_ms", "Lookahead", 5.0, 0.1, 20.0, "ms", 0.1),
            ParamSpec("release_ms", "Release", 5.0, 0.25, 20.0, "ms", 0.25),
        ),
        requires=(Requirement("lv2", f"{LSP}limiter_mono", LV2_PACKAGE),),
    )
)


def spec_for(kind: str) -> EffectSpec:
    try:
        return _SPECS[kind]
    except KeyError:
        raise EffectError(
            f"unknown effect {kind!r}; known effects: " + ", ".join(sorted(_SPECS))
        ) from None


def all_specs() -> list[EffectSpec]:
    """Every effect, in the order they are worth offering.

    Registration order, not alphabetical: the first thing a menu shows should be
    the thing most people want (a volume trim), not whichever name happens to
    sort first (a compressor that needs a plugin installed).
    """
    return list(_SPECS.values())


@dataclass
class Effect:
    """One configured effect in a channel's chain."""

    kind: str
    params: dict[str, float] = field(default_factory=dict)
    enabled: bool = True

    @property
    def spec(self) -> EffectSpec:
        return spec_for(self.kind)

    def resolved(self) -> dict[str, float]:
        return self.spec.normalise(self.params)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": dict(self.params), "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Effect:
        kind = str(data.get("kind", ""))
        spec_for(kind)  # validate early, with a helpful message
        return cls(
            kind=kind,
            params={str(k): float(v) for k, v in (data.get("params") or {}).items()},
            enabled=bool(data.get("enabled", True)),
        )

    def render(self, index: int) -> Fragment:
        """Build this effect's graph fragment. `index` keeps node names unique."""
        values = self.resolved()
        name = f"{self.kind}{index}"

        if self.kind == "gain":
            node = {
                "type": "builtin",
                "name": name,
                "label": "mixer",
                "control": {"Gain 1": round(db_to_linear(values["gain_db"]), 6)},
            }
            return Fragment([node], [], f"{name}:In 1", f"{name}:Out")

        if self.kind in ("lowpass", "highpass"):
            label = "bq_lowpass" if self.kind == "lowpass" else "bq_highpass"
            return _biquad_stack(name, label, values, int(values["poles"]))

        if self.kind in ("peaking", "lowshelf", "highshelf"):
            label = {"peaking": "bq_peaking", "lowshelf": "bq_lowshelf", "highshelf": "bq_highshelf"}[
                self.kind
            ]
            node = {
                "type": "builtin",
                "name": name,
                "label": label,
                "control": {
                    "Freq": round(float(values["frequency"]), 4),
                    "Q": round(float(values["q"]), 4),
                    "Gain": round(float(values["gain_db"]), 4),
                },
            }
            return Fragment([node], [], f"{name}:In", f"{name}:Out")

        if self.kind == "delay":
            seconds = float(values["delay_ms"]) / 1000.0
            node = {
                "type": "builtin",
                "name": name,
                "label": "delay",
                "config": {"max-delay": max(seconds, 0.001)},
                "control": {"Delay (s)": round(seconds, 6)},
            }
            return Fragment([node], [], f"{name}:In", f"{name}:Out")

        if self.kind == "compressor":
            node = {
                "type": "lv2",
                "name": name,
                "plugin": f"{LSP}compressor_mono",
                "control": {
                    # LSP takes thresholds and makeup as linear gain, not dB.
                    "al": round(db_to_linear(values["threshold_db"]), 6),
                    "cr": round(float(values["ratio"]), 4),
                    "at": round(float(values["attack_ms"]), 4),
                    "rt": round(float(values["release_ms"]), 4),
                    "mk": round(db_to_linear(values["makeup_db"]), 6),
                },
            }
            return Fragment([node], [], f"{name}:in", f"{name}:out")

        if self.kind == "limiter":
            node = {
                "type": "lv2",
                "name": name,
                "plugin": f"{LSP}limiter_mono",
                "control": {
                    "th": round(db_to_linear(values["threshold_db"]), 6),
                    "lk": round(float(values["lookahead_ms"]), 4),
                    "rt": round(float(values["release_ms"]), 4),
                },
            }
            return Fragment([node], [], f"{name}:in", f"{name}:out")

        raise EffectError(f"no renderer for effect {self.kind!r}")


def render_chain(effects: list[Effect]) -> Chain:
    """Render effects into one series graph.

    Disabled effects are dropped. An empty chain yields a single `copy` node so
    the channel still exists and passes audio through untouched.
    """
    active = [e for e in effects if e.enabled]
    if not active:
        return Chain(
            [{"type": "builtin", "name": "passthrough", "label": "copy"}],
            [],
            "passthrough:In",
            "passthrough:Out",
        )

    nodes: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    first_in: str | None = None
    previous_out: str | None = None
    for index, effect in enumerate(active):
        fragment = effect.render(index)
        nodes.extend(fragment.nodes)
        links.extend(fragment.links)
        if previous_out is None:
            first_in = fragment.input_port
        else:
            links.append({"output": previous_out, "input": fragment.input_port})
        previous_out = fragment.output_port
    assert first_in is not None and previous_out is not None
    return Chain(nodes, links, first_in, previous_out)


def unsatisfied_requirements(effects: list[Effect]) -> list[Requirement]:
    """Requirements of the enabled effects that this machine cannot meet."""
    missing: list[Requirement] = []
    for effect in effects:
        if not effect.enabled:
            continue
        for requirement in effect.spec.unsatisfied():
            if requirement not in missing:
                missing.append(requirement)
    return missing
