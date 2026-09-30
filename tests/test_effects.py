import unittest
from unittest import mock

from audiorouter.effects import (
    SWITCH_FADE_S,
    Effect,
    EffectError,
    db_to_linear,
    render_chain,
    spec_for,
    unsatisfied_requirements,
)


class DecibelTest(unittest.TestCase):
    def test_zero_db_is_unity(self):
        self.assertAlmostEqual(db_to_linear(0), 1.0)

    def test_minus_six_db_is_about_half(self):
        self.assertAlmostEqual(db_to_linear(-6.0206), 0.5, places=4)


class ParameterTest(unittest.TestCase):
    def test_defaults_fill_in_missing_values(self):
        values = spec_for("peaking").normalise({"frequency": 500})
        self.assertEqual(values["frequency"], 500)
        self.assertEqual(values["gain_db"], 0.0)

    def test_values_are_clamped_not_rejected(self):
        values = spec_for("gain").normalise({"gain_db": 999})
        self.assertEqual(values["gain_db"], 20.0)

    def test_unknown_parameter_names_the_effect(self):
        with self.assertRaises(EffectError) as caught:
            spec_for("gain").normalise({"cutoff": 1})
        self.assertIn("gain", str(caught.exception))

    def test_non_finite_values_are_refused(self):
        with self.assertRaises(EffectError):
            spec_for("gain").normalise({"gain_db": float("nan")})

    def test_unknown_effect_lists_the_known_ones(self):
        with self.assertRaises(EffectError) as caught:
            spec_for("reverb")
        self.assertIn("limiter", str(caught.exception))


class RenderTest(unittest.TestCase):
    def test_gain_becomes_a_mixer_with_linear_gain(self):
        chain = Effect("gain", {"gain_db": -6.0206}).render(0)
        for node in chain.nodes:
            self.assertEqual(node["label"], "mixer")
            self.assertAlmostEqual(node["control"]["Gain 1"], 0.5, places=4)
        self.assertEqual([n["name"] for n in chain.nodes], ["gain0_l", "gain0_r"])

    def test_gain_entry_port_is_the_connected_mixer_input(self):
        # Regression: a mixer has eight inputs. Leaving the graph's ports
        # implicit makes PipeWire see an 8-in 1-out filter, refuse to start it,
        # and leave a silent channel that still looks healthy in the graph.
        chain = Effect("gain").render(0)
        self.assertEqual(chain.inputs, ("gain0_l:In 1", "gain0_r:In 1"))
        self.assertEqual(chain.outputs, ("gain0_l:Out", "gain0_r:Out"))

    def test_biquad_poles_become_stages_in_series(self):
        chain = Effect("highpass", {"frequency": 1000, "poles": 3}).render(0)
        self.assertEqual(len(chain.nodes), 6)  # three stages on each side
        self.assertEqual(len(chain.links), 4)
        self.assertIn({"output": "highpass0_1_r:Out", "input": "highpass0_2_r:In"}, chain.links)
        self.assertEqual(chain.inputs, ("highpass0_0_l:In", "highpass0_0_r:In"))
        self.assertEqual(chain.outputs, ("highpass0_2_l:Out", "highpass0_2_r:Out"))
        self.assertTrue(all(n["label"] == "bq_highpass" for n in chain.nodes))

    def test_empty_chain_is_just_the_fader(self):
        chain = render_chain([])
        self.assertEqual([n["name"] for n in chain.nodes], ["fader_l", "fader_r"])
        self.assertEqual(chain.inputs, ("fader_l:In 1", "fader_r:In 1"))
        self.assertEqual(chain.outputs, ("fader_l:Out", "fader_r:Out"))
        self.assertEqual(chain.controls()["fader_l:Gain 1"], 1.0)

    def test_the_fader_follows_the_last_effect_and_goes_fully_off(self):
        from audiorouter.effects import FADER_MAX_DB

        chain = render_chain([Effect("gain")], fader_db=-6.0)
        self.assertIn({"output": "sw0_switch_l:Out", "input": "fader_l:In 1"}, chain.links)
        self.assertEqual(chain.outputs, ("fader_l:Out", "fader_r:Out"))
        self.assertAlmostEqual(chain.controls()["fader_r:Gain 1"], 0.501187, places=5)
        self.assertEqual(render_chain([], fader_db=-200).controls()["fader_l:Gain 1"], 0.0)
        self.assertAlmostEqual(render_chain([], fader_db=99).controls()["fader_l:Gain 1"],
                               10 ** (FADER_MAX_DB / 20), places=4)

    def test_moving_the_fader_keeps_the_graph_shape(self):
        # So a fader move is a live control change, never a restart.
        a, b = render_chain([Effect("gain")], fader_db=0), render_chain([Effect("gain")], fader_db=-20)
        self.assertEqual(a.links, b.links)
        self.assertEqual([n["name"] for n in a.nodes], [n["name"] for n in b.nodes])

    def test_every_effect_sits_behind_a_switch_that_feeds_it_and_the_dry_path(self):
        chain = render_chain([Effect("gain")])
        self.assertEqual(chain.inputs, ("sw0_in_l:In", "sw0_in_r:In"))
        for side in "lr":
            self.assertIn({"output": f"sw0_in_{side}:Out", "input": f"gain0_{side}:In 1"}, chain.links)
            self.assertIn({"output": f"gain0_{side}:Out", "input": f"sw0_wet_{side}:In 1"}, chain.links)
            self.assertIn({"output": f"sw0_in_{side}:Out", "input": f"sw0_dry_{side}:In 1"}, chain.links)
            self.assertIn({"output": f"sw0_wet_{side}:Out", "input": f"sw0_switch_{side}:In 1"}, chain.links)
            self.assertIn({"output": f"sw0_dry_{side}:Out", "input": f"sw0_switch_{side}:In 2"}, chain.links)

    def test_one_audio_rate_fade_drives_both_sides_and_the_dry_gain_is_its_complement(self):
        # The dry gain is 1 - fade, so the two always add to exactly 1, and one
        # ramp for both sides means left and right cannot drift apart.
        chain = render_chain([Effect("gain")])
        nodes = {n["name"]: n for n in chain.nodes}
        self.assertEqual(nodes["sw0_fade"]["label"], "ramp")
        self.assertEqual(nodes["sw0_drygain"]["label"], "linear")
        self.assertEqual(nodes["sw0_drygain"]["control"], {"Mult": -1.0, "Add": 1.0})
        self.assertIn({"output": "sw0_fade:Out", "input": "sw0_drygain:In"}, chain.links)
        for side in "lr":
            self.assertIn({"output": "sw0_fade:Out", "input": f"sw0_wet_{side}:In 2"}, chain.links)
            self.assertIn({"output": "sw0_drygain:Out", "input": f"sw0_dry_{side}:In 2"}, chain.links)

    def test_no_ramp_has_controls_of_its_own_because_they_would_be_clamped_to_zero(self):
        # A ramp's ports declare no range and filter-graph clamps set values to
        # it: measured, Start/Stop/Duration all read 0 and the fade never ran.
        chain = render_chain([Effect("gain"), Effect("gain", enabled=False)])
        ramps = [n for n in chain.nodes if n["label"] == "ramp"]
        self.assertEqual(len(ramps), 3)  # one per switch, one clock
        for ramp in ramps:
            self.assertNotIn("control", ramp)
            for port in ("Start", "Stop", "Duration (s)"):
                feeds = [l for l in chain.links if l["input"] == f"{ramp['name']}:{port}"]
                self.assertEqual(len(feeds), 1, f"{ramp['name']}:{port}")

    def test_a_switch_fades_toward_its_target_from_the_opposite_end(self):
        chain = render_chain([Effect("gain")])
        nodes = {n["name"]: n for n in chain.nodes}
        self.assertEqual(nodes["sw0_target"]["control"], {"Mult": 0.0, "Add": 1.0})
        self.assertEqual(nodes["sw0_from"]["control"], {"Mult": -1.0, "Add": 1.0})
        self.assertIn({"output": "sw0_target:Notify", "input": "sw0_fade:Stop"}, chain.links)
        self.assertIn({"output": "sw0_target:Notify", "input": "sw0_from:Control"}, chain.links)
        self.assertIn({"output": "sw0_from:Notify", "input": "sw0_fade:Start"}, chain.links)

    def test_one_clock_starts_every_fade_at_zero_length_so_a_new_host_is_already_settled(self):
        # A ramp starts at 0: without this, a restart let 4 ms of +6 dB through.
        chain = render_chain([Effect("gain"), Effect("highpass", {"poles": 1})])
        nodes = {n["name"]: n for n in chain.nodes}
        self.assertEqual([n["name"] for n in chain.nodes].count("fadeclock"), 1)
        self.assertEqual(nodes["fadeclock"]["label"], "ramp")
        self.assertEqual(nodes["fadeclock_zero"]["control"], {"Mult": 0.0, "Add": 0.0})
        self.assertEqual(nodes["fadeclock_len"]["control"], {"Mult": 0.0, "Add": SWITCH_FADE_S})
        self.assertGreater(SWITCH_FADE_S, 0)
        self.assertIn({"output": "fadeclock_zero:Notify", "input": "fadeclock:Start"}, chain.links)
        self.assertIn({"output": "fadeclock_len:Notify", "input": "fadeclock:Stop"}, chain.links)
        self.assertIn({"output": "fadeclock_len:Notify", "input": "fadeclock:Duration (s)"}, chain.links)
        for index in (0, 1):
            self.assertIn({"output": "fadeclock:Current", "input": f"sw{index}_fade:Duration (s)"}, chain.links)

    def test_an_empty_chain_has_no_clock(self):
        self.assertFalse(any(n["label"] == "ramp" for n in render_chain([]).nodes))

    def test_an_effect_that_is_on_targets_the_processed_sound(self):
        self.assertEqual(render_chain([Effect("gain")]).controls()["sw0_target:Add"], 1.0)

    def test_a_switched_off_effect_stays_in_the_graph_passing_only_the_dry_sound(self):
        on = render_chain([Effect("gain")])
        off = render_chain([Effect("gain", enabled=False)])
        self.assertEqual([n["name"] for n in on.nodes], [n["name"] for n in off.nodes])
        self.assertEqual(off.controls()["sw0_target:Add"], 0.0)

    def test_a_switched_off_effect_that_cannot_run_is_left_out(self):
        with mock.patch("audiorouter.plugins.available_loaders", return_value=frozenset({"builtin"})):
            chain = render_chain([Effect("limiter", enabled=False)])
        self.assertEqual([n["name"] for n in chain.nodes], ["fader_l", "fader_r"])

    def test_effects_are_chained_in_order(self):
        chain = render_chain([Effect("highpass", {"poles": 1}), Effect("gain")])
        self.assertIn({"output": "sw0_switch_l:Out", "input": "sw1_in_l:In"}, chain.links)
        self.assertIn({"output": "sw0_switch_r:Out", "input": "sw1_in_r:In"}, chain.links)
        self.assertIn({"output": "sw1_in_l:Out", "input": "gain1_l:In 1"}, chain.links)
        self.assertEqual(chain.inputs, ("sw0_in_l:In", "sw0_in_r:In"))
        self.assertIn({"output": "sw1_switch_r:Out", "input": "fader_r:In 1"}, chain.links)

    def test_delay_declares_enough_buffer_for_its_setting(self):
        for node in Effect("delay", {"delay_ms": 200}).render(0).nodes:
            self.assertGreaterEqual(node["config"]["max-delay"], 0.2)

    def test_delay_buffer_covers_the_whole_knob_so_moving_it_stays_live(self):
        short, long = (Effect("delay", {"delay_ms": ms}).render(0).nodes[0] for ms in (10, 400))
        self.assertEqual(short["config"], long["config"])

    def test_lsp_thresholds_are_sent_as_linear_gain(self):
        (node,) = Effect("compressor", {"threshold_db": -6.0206}).render(0).nodes
        self.assertEqual(node["type"], "lv2")
        self.assertTrue(node["plugin"].endswith("compressor_stereo"))
        self.assertAlmostEqual(node["control"]["al"], 0.5, places=4)

    def test_the_compressor_is_one_stereo_instance_so_both_sides_are_linked(self):
        chain = Effect("compressor").render(0)
        self.assertEqual(len(chain.nodes), 1)
        self.assertEqual(chain.inputs, ("compressor0:in_l", "compressor0:in_r"))
        self.assertEqual(chain.outputs, ("compressor0:out_l", "compressor0:out_r"))

    def test_the_limiter_switches_off_lsp_gain_boost(self):
        # With boost on, a -20 dB ceiling changed the measured level by 0.5 dB.
        (node,) = Effect("limiter").render(0).nodes
        self.assertEqual(node["control"]["boost"], 0.0)

    def test_peaking_keeps_gain_in_decibels(self):
        for node in Effect("peaking", {"gain_db": 6}).render(0).nodes:
            self.assertEqual(node["control"]["Gain"], 6.0)


class RequirementTest(unittest.TestCase):
    def test_builtin_effects_need_nothing(self):
        self.assertEqual(unsatisfied_requirements([Effect("gain")]), [])

    def test_disabled_effects_do_not_demand_plugins(self):
        self.assertEqual(unsatisfied_requirements([Effect("limiter", enabled=False)]), [])

    def test_serialisation_round_trip(self):
        effect = Effect("peaking", {"frequency": 800.0}, enabled=False)
        again = Effect.from_dict(effect.to_dict())
        self.assertEqual((again.kind, again.params, again.enabled), ("peaking", {"frequency": 800.0}, False))

    def test_deserialising_an_unknown_effect_fails_loudly(self):
        with self.assertRaises(EffectError):
            Effect.from_dict({"kind": "chorus"})


if __name__ == "__main__":
    unittest.main()
