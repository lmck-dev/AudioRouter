"""Effect definitions and their translation into filter-chain graph fragments.

Two families live here. The *curated* effects (volume trim, filters, tone bands,
delay, compressor, limiter) have friendly names and knobs in the units a person
thinks in. The *plugin* effect (`kind="lv2"`) is any usable LV2 plugin from
`lv2.catalogue()`, with one knob per control port in the plugin's own units.

Every chain is rendered as an explicit stereo graph. A mono effect is placed
twice, once per side; a stereo plugin is placed once and sees both sides, which
is what lets a compressor or limiter react to the pair together instead of
pulling the image sideways. (PipeWire can replicate a mono graph per channel by
itself, but then no stereo plugin could ever be used.)

Gain values of curated effects are exchanged in decibels because that is what a
user understands; conversion to the linear multipliers the plugins want happens
at render time. Plugin effects keep native values, and `ParamSpec.db` tells the
window to show a linear gain as decibels.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import lv2
from .plugins import Requirement, Unusable

LSP = "http://lsp-plug.in/plugins/lv2/"

#: Package that teaches PipeWire to load LV2 plugins. Not installed by default
#: on Fedora, and its absence is only reported as ENOENT at process start.
LV2_PACKAGE = "pipewire-module-filter-chain-lv2"

#: The plugin effect's kind. Its identity is the `plugin` URI on the Effect.
PLUGIN_KIND = "lv2"

SIDES = ("l", "r")


class EffectError(ValueError):
    """An effect kind or parameter value was not usable."""


def db_to_linear(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def linear_to_db(value: float, floor: float = -120.0) -> float:
    return 20.0 * math.log10(value) if value > 0 else floor


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
    toggled: bool = False
    integer: bool = False
    logarithmic: bool = False
    #: The value is a linear gain factor best shown to people as dB.
    db: bool = False
    #: (label, value) pairs for a list of named settings.
    choices: tuple[tuple[str, float], ...] = ()
    comment: str = ""
    #: Left at its default and not offered as a control.
    hidden: bool = False

    def clamp(self, value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise EffectError(f"{self.key}: {value!r} is not a number") from exc
        if math.isnan(number) or math.isinf(number):
            raise EffectError(f"{self.key}: {value!r} is not a finite number")
        number = max(self.minimum, min(self.maximum, number))
        if self.toggled:
            return 1.0 if number >= (self.minimum + self.maximum) / 2 else self.minimum
        if self.integer or self.choices:
            return float(round(number))
        return number


@dataclass(frozen=True)
class Fragment:
    """A rendered piece of a filter graph, with its stereo entry and exit ports."""

    nodes: list[dict[str, Any]]
    links: list[dict[str, str]]
    inputs: tuple[str, str]
    outputs: tuple[str, str]


@dataclass(frozen=True)
class Chain:
    """A whole channel's graph: nodes, links, and the ports the streams bind to.

    The ports are not a convenience. PipeWire derives a filter's inputs from
    every *unconnected* input port in the graph, so one `mixer` node - which has
    eight inputs and only ever uses one - makes the filter look like an 8-in
    1-out device. The graph then refuses to start with "invalid ports" and the
    channel is silent while still appearing in the graph, fully linked, as if it
    were working. Naming the entry and exit ports explicitly is what prevents
    that, and it matters even more now that plugins bring sidechain inputs.
    """

    nodes: list[dict[str, Any]]
    links: list[dict[str, str]]
    inputs: tuple[str, str]
    outputs: tuple[str, str]

    def controls(self) -> dict[str, float]:
        """Every control value, named as PipeWire names it: `node:control`."""
        return {
            f"{node['name']}:{key}": value
            for node in self.nodes
            for key, value in (node.get("control") or {}).items()
        }


@dataclass(frozen=True)
class EffectSpec:
    kind: str
    label: str
    summary: str
    params: tuple[ParamSpec, ...]
    #: Plugins this effect needs; empty when it is built into PipeWire.
    requires: tuple[Requirement, ...] = ()
    #: Set for plugin effects: the LV2 URI.
    plugin: str = ""
    #: Reasons this effect can never run here, beyond missing packages.
    problems: tuple[Unusable, ...] = ()
    #: Who makes it ("Built in", "LSP", "Calf", ...).
    group: str = "Built in"
    #: What it does - one of lv2.CATEGORIES - for the browser's grouping.
    category: str = "Utility"

    def unsatisfied(self) -> list[Requirement | Unusable]:
        missing: list[Requirement | Unusable] = [r for r in self.requires if not r.satisfied()]
        return missing + list(self.problems)

    @property
    def available(self) -> bool:
        return not self.unsatisfied()

    def param(self, key: str) -> ParamSpec:
        for spec in self.params:
            if spec.key == key:
                return spec
        raise EffectError(f"{self.label} ({self.kind}) has no parameter {key!r}")

    def visible_params(self) -> list[ParamSpec]:
        return [p for p in self.params if not p.hidden]

    def defaults(self) -> dict[str, float]:
        return {p.key: p.default for p in self.params if not p.hidden}

    def normalise(self, values: Mapping[str, Any] | None) -> dict[str, float]:
        """Fill in defaults and clamp everything into range.

        A plugin effect silently drops settings for ports the plugin no longer
        has: a plugin update must not make a saved channel impossible to load.
        """
        merged = self.defaults()
        known = {p.key for p in self.params}
        for key, value in (values or {}).items():
            if self.plugin and key not in known:
                continue
            merged[key] = self.param(key).clamp(value)
        return merged


def _biquad_stack(name: str, label: str, values: Mapping[str, float], stages: int) -> Fragment:
    """`stages` identical biquads in series per side; each stage is 12 dB/octave."""
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    for side in SIDES:
        for index in range(stages):
            stage = f"{name}_{index}_{side}"
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
                links.append({"output": f"{name}_{index - 1}_{side}:Out", "input": f"{stage}:In"})
    last = stages - 1
    return Fragment(
        nodes, links,
        (f"{name}_0_l:In", f"{name}_0_r:In"),
        (f"{name}_{last}_l:Out", f"{name}_{last}_r:Out"),
    )


def _per_side(node: dict[str, Any], in_port: str, out_port: str) -> Fragment:
    """The same mono node placed once for each side."""
    nodes = []
    for side in SIDES:
        copy = dict(node)
        copy["name"] = f"{node['name']}_{side}"
        if "control" in copy:
            copy["control"] = dict(copy["control"])
        nodes.append(copy)
    name = node["name"]
    return Fragment(
        nodes, [],
        (f"{name}_l:{in_port}", f"{name}_r:{in_port}"),
        (f"{name}_l:{out_port}", f"{name}_r:{out_port}"),
    )


def _lv2_node(
    name: str,
    uri: str,
    control: dict[str, float],
    audio_in: tuple[str, ...],
    audio_out: tuple[str, ...],
) -> Fragment:
    node = {"type": "lv2", "name": name, "plugin": uri, "control": control}
    if len(audio_in) == 2:
        return Fragment(
            [node], [],
            (f"{name}:{audio_in[0]}", f"{name}:{audio_in[1]}"),
            (f"{name}:{audio_out[0]}", f"{name}:{audio_out[1]}"),
        )
    return _per_side(node, audio_in[0], audio_out[0])


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
        category="Utility",
    )
)

LOWPASS = register(
    EffectSpec(
        kind="lowpass",
        label="Low-pass",
        summary="Removes treble above the cutoff.",
        params=(
            ParamSpec("frequency", "Cutoff", 2000.0, 20.0, 20000.0, "Hz", 10.0, logarithmic=True),
            ParamSpec("q", "Resonance", 0.707, 0.1, 4.0, "Q", 0.05),
            ParamSpec("poles", "Steepness", 2, 1, 4, "x12dB/oct", 1.0, integer=True),
        ),
    category="EQ & filters",
    )
)

HIGHPASS = register(
    EffectSpec(
        kind="highpass",
        label="High-pass",
        summary="Removes bass below the cutoff. Protects small speakers.",
        params=(
            ParamSpec("frequency", "Cutoff", 100.0, 20.0, 20000.0, "Hz", 10.0, logarithmic=True),
            ParamSpec("q", "Resonance", 0.707, 0.1, 4.0, "Q", 0.05),
            ParamSpec("poles", "Steepness", 2, 1, 4, "x12dB/oct", 1.0, integer=True),
        ),
    category="EQ & filters",
    )
)

PEAKING = register(
    EffectSpec(
        kind="peaking",
        label="Tone band",
        summary="Boost or cut a band. Stack several to build an equaliser.",
        params=(
            ParamSpec("frequency", "Frequency", 1000.0, 20.0, 20000.0, "Hz", 10.0, logarithmic=True),
            ParamSpec("gain_db", "Gain", 0.0, -24.0, 24.0, "dB", 0.5),
            ParamSpec("q", "Width", 1.0, 0.1, 10.0, "Q", 0.05),
        ),
    category="EQ & filters",
    )
)

LOWSHELF = register(
    EffectSpec(
        kind="lowshelf",
        label="Bass shelf",
        summary="Lifts or drops everything below the frequency.",
        params=(
            ParamSpec("frequency", "Frequency", 120.0, 20.0, 2000.0, "Hz", 10.0, logarithmic=True),
            ParamSpec("gain_db", "Gain", 0.0, -24.0, 24.0, "dB", 0.5),
            ParamSpec("q", "Width", 0.707, 0.1, 4.0, "Q", 0.05),
        ),
    category="EQ & filters",
    )
)

HIGHSHELF = register(
    EffectSpec(
        kind="highshelf",
        label="Treble shelf",
        summary="Lifts or drops everything above the frequency.",
        params=(
            ParamSpec("frequency", "Frequency", 6000.0, 500.0, 20000.0, "Hz", 100.0, logarithmic=True),
            ParamSpec("gain_db", "Gain", 0.0, -24.0, 24.0, "dB", 0.5),
            ParamSpec("q", "Width", 0.707, 0.1, 4.0, "Q", 0.05),
        ),
    category="EQ & filters",
    )
)

DELAY = register(
    EffectSpec(
        kind="delay",
        label="Delay",
        summary="Delays this channel, for aligning speakers in a room.",
        params=(ParamSpec("delay_ms", "Delay", 0.0, 0.0, 500.0, "ms", 1.0),),
        category="Reverb & delay",
    )
)

COMPRESSOR = register(
    EffectSpec(
        kind="compressor",
        label="Compressor",
        summary="Evens out loud and quiet passages. Both sides are compressed together.",
        params=(
            ParamSpec("threshold_db", "Threshold", -18.0, -60.0, 0.0, "dB", 0.5),
            ParamSpec("ratio", "Ratio", 4.0, 1.0, 20.0, ":1", 0.1),
            ParamSpec("attack_ms", "Attack", 20.0, 0.0, 2000.0, "ms", 1.0),
            ParamSpec("release_ms", "Release", 100.0, 0.0, 5000.0, "ms", 5.0),
            ParamSpec("makeup_db", "Makeup", 0.0, -24.0, 24.0, "dB", 0.5),
        ),
        requires=(Requirement("lv2", f"{LSP}compressor_stereo", LV2_PACKAGE),),
        category="Dynamics",
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
        requires=(Requirement("lv2", f"{LSP}limiter_stereo", LV2_PACKAGE),),
        category="Dynamics",
    )
)


def _param_from_control(control: lv2.Control) -> ParamSpec:
    if control.is_gain:
        step = 0.5  # in dB, which is how the window shows it
    elif control.integer or control.toggled or control.choices:
        step = 1.0
    else:
        span = control.maximum - control.minimum
        step = 10 ** math.floor(math.log10(span / 100)) if span > 0 else 0.01
    return ParamSpec(
        key=control.symbol,
        label=control.name,
        default=control.default,
        minimum=control.minimum,
        maximum=control.maximum,
        unit="dB" if control.is_gain else control.unit,
        step=step,
        toggled=control.toggled,
        integer=control.integer,
        logarithmic=control.logarithmic,
        db=control.is_gain,
        choices=control.choices,
        comment=control.comment,
        hidden=control.hidden,
    )


def plugin_spec(uri: str) -> EffectSpec:
    """The spec for one LV2 plugin, whether or not it is installed."""
    requirement = Requirement("lv2", uri, LV2_PACKAGE)
    plugin = lv2.catalogue().get(uri)
    if plugin is None:
        return EffectSpec(
            kind=PLUGIN_KIND,
            label=uri.rstrip("/").rsplit("/", 1)[-1] or uri,
            summary="This plugin is not installed.",
            params=(),
            requires=(requirement,),
            plugin=uri,
            problems=(Unusable(uri, "is not installed"),),
            group="Missing",
        )
    classes = ", ".join(c for c in plugin.classes if c) or "Effect"
    layout = "stereo" if plugin.stereo else "mono, run on each side"
    return EffectSpec(
        kind=PLUGIN_KIND,
        label=plugin.name,
        summary=f"{plugin.vendor} {classes.lower()} ({layout}).",
        params=tuple(_param_from_control(c) for c in plugin.controls),
        requires=(requirement,),
        plugin=uri,
        problems=tuple(Unusable(plugin.name, reason) for reason in plugin.problems),
        group=plugin.vendor,
        category=plugin.category,
    )


def spec_for(kind: str, plugin: str = "") -> EffectSpec:
    if kind == PLUGIN_KIND:
        if not plugin:
            raise EffectError("a plugin effect needs the plugin's URI")
        return plugin_spec(plugin)
    try:
        return _SPECS[kind]
    except KeyError:
        raise EffectError(
            f"unknown effect {kind!r}; known effects: "
            + ", ".join(sorted([*_SPECS, PLUGIN_KIND]))
        ) from None


def all_specs() -> list[EffectSpec]:
    """Every curated effect, in the order they are worth offering.

    Registration order, not alphabetical: the first thing a menu shows should be
    the thing most people want (a volume trim), not whichever name happens to
    sort first (a compressor that needs a plugin installed).
    """
    return list(_SPECS.values())


def plugin_specs(include_unusable: bool = False) -> list[EffectSpec]:
    """Every installed LV2 plugin as an effect, grouped by vendor then name."""
    source = lv2.catalogue().all() if include_unusable else lv2.catalogue().usable()
    return [plugin_spec(p.uri) for p in source]


@dataclass
class Effect:
    """One configured effect in a channel's chain."""

    kind: str
    params: dict[str, float] = field(default_factory=dict)
    enabled: bool = True
    #: The LV2 URI, for plugin effects only.
    plugin: str = ""

    @property
    def spec(self) -> EffectSpec:
        return spec_for(self.kind, self.plugin)

    @property
    def label(self) -> str:
        return self.spec.label

    def resolved(self) -> dict[str, float]:
        return self.spec.normalise(self.params)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "params": dict(self.params), "enabled": self.enabled}
        if self.plugin:
            data["plugin"] = self.plugin
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Effect:
        kind = str(data.get("kind", ""))
        plugin = str(data.get("plugin", ""))
        if kind == PLUGIN_KIND:
            if not plugin:
                raise EffectError("a plugin effect needs the plugin's URI")
        else:
            spec_for(kind)  # validate early, with a helpful message
        return cls(
            kind=kind,
            params={str(k): float(v) for k, v in (data.get("params") or {}).items()},
            enabled=bool(data.get("enabled", True)),
            plugin=plugin,
        )

    def render(self, index: int) -> Fragment:
        """Build this effect's graph fragment. `index` keeps node names unique."""
        values = self.resolved()
        name = f"fx{index}" if self.kind == PLUGIN_KIND else f"{self.kind}{index}"

        if self.kind == PLUGIN_KIND:
            plugin = lv2.catalogue().get(self.plugin)
            if plugin is None or not plugin.usable:
                raise EffectError(f"plugin {self.plugin} cannot be used here")
            control = {key: round(float(value), 6) for key, value in values.items()}
            if plugin.enable_port:
                control[plugin.enable_port] = 1.0
            return _lv2_node(name, plugin.uri, control, plugin.audio_in, plugin.audio_out)

        if self.kind == "gain":
            node = {
                "type": "builtin",
                "name": name,
                "label": "mixer",
                "control": {"Gain 1": round(db_to_linear(values["gain_db"]), 6)},
            }
            return _per_side(node, "In 1", "Out")

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
            return _per_side(node, "In", "Out")

        if self.kind == "delay":
            node = {
                "type": "builtin",
                "name": name,
                "label": "delay",
                # Sized for the knob's whole range, not the current setting, so
                # moving the knob is a live change rather than a new graph.
                "config": {"max-delay": self.spec.param("delay_ms").maximum / 1000.0},
                "control": {"Delay (s)": round(float(values["delay_ms"]) / 1000.0, 6)},
            }
            return _per_side(node, "In", "Out")

        if self.kind == "compressor":
            control = {
                # LSP takes thresholds and makeup as linear gain, not dB.
                "al": round(db_to_linear(values["threshold_db"]), 6),
                "cr": round(float(values["ratio"]), 4),
                "at": round(float(values["attack_ms"]), 4),
                "rt": round(float(values["release_ms"]), 4),
                "mk": round(db_to_linear(values["makeup_db"]), 6),
                "enabled": 1.0,
            }
            return _lv2_node(name, f"{LSP}compressor_stereo", control, ("in_l", "in_r"), ("out_l", "out_r"))

        if self.kind == "limiter":
            control = {
                "th": round(db_to_linear(values["threshold_db"]), 6),
                "lk": round(float(values["lookahead_ms"]), 4),
                "rt": round(float(values["release_ms"]), 4),
                # LSP's default "gain boost" turns the level back up by the
                # amount the ceiling took off, so the ceiling does nothing
                # audible. Measured: a -20 dB ceiling changed the level 0.5 dB.
                "boost": 0.0,
                # Automatic level regulation pulls the level down further than
                # the ceiling asks for: a -20 dB ceiling left peaks ~8 dB lower.
                "alr": 0.0,
                "enabled": 1.0,
            }
            return _lv2_node(name, f"{LSP}limiter_stereo", control, ("in_l", "in_r"), ("out_l", "out_r"))

        raise EffectError(f"no renderer for effect {self.kind!r}")


def make_effect(kind: str, params: Mapping[str, Any] | None = None, plugin: str = "") -> Effect:
    """A new effect with its settings filled in and clamped."""
    spec = spec_for(kind, plugin)
    return Effect(kind=kind, params=spec.normalise(params), plugin=plugin if kind == PLUGIN_KIND else "")


#: How long switching an effect on or off crossfades between it and the dry
#: sound. Long enough not to click on anything sustained, short enough to feel
#: instant.
SWITCH_FADE_S = 0.05


def _with_bypass(fragment: Fragment, name: str, enabled: bool, clock: str) -> Fragment:
    """Wrap an effect in a switch that crossfades on the running channel.

    Each side's input is fanned out to the effect and to a dry path; the two
    are multiplied by opposite gains and summed:

        wet = effect * fade          dry = input * (1 - fade)

    `fade` is a builtin `ramp`, which is AUDIO rate: it moves toward `Stop` a
    little every sample, from wherever it is now, and holds there. `Start` only
    sets the slope and the limits. The fade therefore runs inside the audio
    thread, and flipping the switch back mid-fade reverses from the current
    point with no jump. The dry gain is `linear(fade * -1 + 1)`, so the two
    gains always add to exactly 1.

    **A ramp's controls cannot be set directly.** Its ports declare no range,
    and filter-graph clamps every value it sets to the port's range, so a conf
    or a live `Props` change reads back as 0 (PipeWire 1.6.8, measured). Control
    LINKS bypass that clamp, so each ramp control is fed from a `linear` node's
    `Notify` output (`Control * Mult + Add`; `Add` has a real range):

        target: Add = 1 on / 0 off  -> ramp Stop, and -> from
        from:   1 - target          -> ramp Start
        clock   (see `_fade_clock`) -> ramp Duration

    Switching is then ONE live control change, `<name>_target:Add`. One ramp
    serves both sides, so left and right can never drift apart.

    A ramp starts at 0, which for an effect that is on means a fade from dry
    to wet whenever a host starts. Measured: a restart's handover moves the
    stream in under 50 ms and 4 ms of +6 dB got through. So the duration comes
    from `clock`, which is 0 when the host starts - the switch snaps to its
    state within one sample - and reaches `SWITCH_FADE_S` 50 ms later.
    """
    nodes = list(fragment.nodes)
    links = list(fragment.links)
    fade = f"{name}_fade"
    dry_gain = f"{name}_drygain"
    target = f"{name}_target"
    origin = f"{name}_from"
    nodes.extend([
        {"type": "builtin", "name": target, "label": "linear",
         "control": {"Mult": 0.0, "Add": 1.0 if enabled else 0.0}},
        {"type": "builtin", "name": origin, "label": "linear",
         "control": {"Mult": -1.0, "Add": 1.0}},
        {"type": "builtin", "name": fade, "label": "ramp"},
        {"type": "builtin", "name": dry_gain, "label": "linear",
         "control": {"Mult": -1.0, "Add": 1.0}},
    ])
    links.extend([
        {"output": f"{target}:Notify", "input": f"{fade}:Stop"},
        {"output": f"{target}:Notify", "input": f"{origin}:Control"},
        {"output": f"{origin}:Notify", "input": f"{fade}:Start"},
        {"output": clock, "input": f"{fade}:Duration (s)"},
        {"output": f"{fade}:Out", "input": f"{dry_gain}:In"},
    ])
    inputs: list[str] = []
    outputs: list[str] = []
    for side, effect_in, effect_out in zip(SIDES, fragment.inputs, fragment.outputs):
        entry = f"{name}_in_{side}"
        wet = f"{name}_wet_{side}"
        dry = f"{name}_dry_{side}"
        switch = f"{name}_switch_{side}"
        nodes.append({"type": "builtin", "name": entry, "label": "copy"})
        nodes.append({"type": "builtin", "name": wet, "label": "mult"})
        nodes.append({"type": "builtin", "name": dry, "label": "mult"})
        nodes.append({"type": "builtin", "name": switch, "label": "mixer"})
        links.append({"output": f"{entry}:Out", "input": effect_in})
        links.append({"output": effect_out, "input": f"{wet}:In 1"})
        links.append({"output": f"{fade}:Out", "input": f"{wet}:In 2"})
        links.append({"output": f"{entry}:Out", "input": f"{dry}:In 1"})
        links.append({"output": f"{dry_gain}:Out", "input": f"{dry}:In 2"})
        links.append({"output": f"{wet}:Out", "input": f"{switch}:In 1"})
        links.append({"output": f"{dry}:Out", "input": f"{switch}:In 2"})
        inputs.append(f"{entry}:In")
        outputs.append(f"{switch}:Out")
    return Fragment(nodes, links, (inputs[0], inputs[1]), (outputs[0], outputs[1]))


FADE_CLOCK = "fadeclock"


def _fade_clock() -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    """A control that is 0 when the host starts and `SWITCH_FADE_S` from 50 ms on.

    It is itself a ramp (0 -> SWITCH_FADE_S over SWITCH_FADE_S), whose
    `Current` control output feeds every switch's fade duration. Its own
    controls come through `linear` nodes for the same reason as the switches'.
    With a duration of 0 a ramp's step is +-infinity and it clamps straight to
    `Stop`; Start and Stop always differ, so the step is never 0/0.
    """
    zero, length = f"{FADE_CLOCK}_zero", f"{FADE_CLOCK}_len"
    nodes = [
        {"type": "builtin", "name": zero, "label": "linear", "control": {"Mult": 0.0, "Add": 0.0}},
        {"type": "builtin", "name": length, "label": "linear",
         "control": {"Mult": 0.0, "Add": SWITCH_FADE_S}},
        {"type": "builtin", "name": FADE_CLOCK, "label": "ramp"},
    ]
    links = [
        {"output": f"{zero}:Notify", "input": f"{FADE_CLOCK}:Start"},
        {"output": f"{length}:Notify", "input": f"{FADE_CLOCK}:Stop"},
        {"output": f"{length}:Notify", "input": f"{FADE_CLOCK}:Duration (s)"},
    ]
    return nodes, links, f"{FADE_CLOCK}:Current"


#: The level tap (`native/meter/`), placed at every boundary of a chain.
TAP_URI = "urn:audiorouter:meter"


def taps_available() -> bool:
    """Can a channel host the level tap here? Without it, no per-effect meters."""
    from . import plugins

    return plugins.backend_available(PLUGIN_KIND) and plugins.lv2_installed(TAP_URI)


def _active(effects: list[Effect]) -> list[Effect]:
    """The effects that are rendered: all but a switched-off one that cannot run."""
    return [e for e in effects if e.enabled or not e.spec.unsatisfied()]


def tap_slots(effects: list[Effect]) -> list[tuple[int, int] | None]:
    """For each configured effect, the (before, after) tap slots around it.

    Slot k is the boundary before the k-th *rendered* effect; slot n (n
    rendered effects) is the chain's output. An effect that is not rendered
    has no taps (None), which is why the slots are not simply its index.
    """
    active = _active(effects)
    slots: list[tuple[int, int] | None] = []
    for effect in effects:
        index = next((i for i, e in enumerate(active) if e is effect), None)
        slots.append(None if index is None else (index, index + 1))
    return slots


def _tap(slot: int, ports: tuple[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    name = f"tap{slot}"
    node = {"type": "lv2", "name": name, "plugin": TAP_URI, "control": {"slot": float(slot)}}
    links = [{"output": port, "input": f"{name}:in_{side}"} for side, port in zip(SIDES, ports)]
    return node, links


def render_chain(effects: list[Effect], taps: bool = False) -> Chain:
    """Render effects into one series stereo graph.

    Every effect sits behind a bypass switch (see `_with_bypass`), so a
    switched-off effect stays in the graph and switching it back on is heard
    at once. The one exception is a switched-off effect that cannot run here
    (its plugin was uninstalled): it is left out rather than breaking the
    channel. An empty chain yields a `copy` node per side so the channel still
    exists and passes audio through untouched.

    With `taps`, a level tap listens at every boundary - before the first
    effect and after each one - so the window can show what any effect
    receives and puts out (`tap_slots`). Taps only listen, and are always in
    the graph, so choosing which one to show never restarts the channel.
    **The chain's last output cannot also feed a tap**: filter-graph refuses
    ("already used by link, use copy"), the graph fails to start and the
    channel passes silence. A `copy` per side therefore ends a tapped chain.
    """
    active = _active(effects)
    if not active:
        return Chain(
            [
                {"type": "builtin", "name": "passthrough_l", "label": "copy"},
                {"type": "builtin", "name": "passthrough_r", "label": "copy"},
            ],
            [],
            ("passthrough_l:In", "passthrough_r:In"),
            ("passthrough_l:Out", "passthrough_r:Out"),
        )

    nodes, links, clock = _fade_clock()
    first_in: tuple[str, str] | None = None
    previous_out: tuple[str, str] | None = None
    for index, effect in enumerate(active):
        fragment = _with_bypass(effect.render(index), f"sw{index}", effect.enabled, clock)
        nodes.extend(fragment.nodes)
        links.extend(fragment.links)
        if previous_out is None:
            first_in = fragment.inputs
            if taps:
                # The switch's entry copy already fans out; one more listener
                # there hears exactly what the first effect receives.
                node, tap_links = _tap(0, (f"sw{index}_in_l:Out", f"sw{index}_in_r:Out"))
                nodes.append(node)
                links.extend(tap_links)
        else:
            for out_port, in_port in zip(previous_out, fragment.inputs):
                links.append({"output": out_port, "input": in_port})
        previous_out = fragment.outputs
        if taps:
            node, tap_links = _tap(index + 1, fragment.outputs)
            nodes.append(node)
            links.extend(tap_links)
    assert first_in is not None and previous_out is not None
    if taps:
        tail = []
        for side, port in zip(SIDES, previous_out):
            nodes.append({"type": "builtin", "name": f"tail_{side}", "label": "copy"})
            links.append({"output": port, "input": f"tail_{side}:In"})
            tail.append(f"tail_{side}:Out")
        previous_out = (tail[0], tail[1])
    return Chain(nodes, links, first_in, previous_out)


def unsatisfied_requirements(effects: list[Effect]) -> list[Requirement | Unusable]:
    """Requirements of the enabled effects that this machine cannot meet."""
    missing: list[Requirement | Unusable] = []
    for effect in effects:
        if not effect.enabled:
            continue
        for requirement in effect.spec.unsatisfied():
            if requirement not in missing:
                missing.append(requirement)
    return missing
