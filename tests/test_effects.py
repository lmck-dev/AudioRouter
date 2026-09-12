import unittest

from audiorouter.effects import (
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
        chain = render_chain([Effect("gain", {"gain_db": -6.0206})])
        (node,) = chain.nodes
        self.assertEqual(node["label"], "mixer")
        self.assertAlmostEqual(node["control"]["Gain 1"], 0.5, places=4)

    def test_gain_entry_port_is_the_connected_mixer_input(self):
        # Regression: a mixer has eight inputs. Leaving the graph's ports
        # implicit makes PipeWire see an 8-in 1-out filter, refuse to start it,
        # and leave a silent channel that still looks healthy in the graph.
        chain = render_chain([Effect("gain")])
        self.assertEqual(chain.input_port, "gain0:In 1")
        self.assertEqual(chain.output_port, "gain0:Out")

    def test_biquad_poles_become_stages_in_series(self):
        chain = render_chain([Effect("highpass", {"frequency": 1000, "poles": 3})])
        self.assertEqual(len(chain.nodes), 3)
        self.assertEqual(len(chain.links), 2)
        self.assertEqual(chain.input_port, "highpass0_0:In")
        self.assertEqual(chain.output_port, "highpass0_2:Out")
        self.assertTrue(all(n["label"] == "bq_highpass" for n in chain.nodes))

    def test_empty_chain_passes_audio_through(self):
        chain = render_chain([])
        self.assertEqual(chain.nodes[0]["label"], "copy")
        self.assertEqual(chain.input_port, "passthrough:In")
        self.assertEqual(chain.output_port, "passthrough:Out")

    def test_disabled_effects_are_dropped(self):
        chain = render_chain([Effect("gain", enabled=False)])
        self.assertEqual(chain.nodes[0]["label"], "copy")

    def test_effects_are_chained_in_order(self):
        chain = render_chain([Effect("highpass", {"poles": 1}), Effect("gain")])
        self.assertIn({"output": "highpass0_0:Out", "input": "gain1:In 1"}, chain.links)
        self.assertEqual(chain.input_port, "highpass0_0:In")
        self.assertEqual(chain.output_port, "gain1:Out")

    def test_delay_declares_enough_buffer_for_its_setting(self):
        (node,) = render_chain([Effect("delay", {"delay_ms": 200})]).nodes
        self.assertGreaterEqual(node["config"]["max-delay"], 0.2)

    def test_lsp_thresholds_are_sent_as_linear_gain(self):
        (node,) = render_chain([Effect("compressor", {"threshold_db": -6.0206})]).nodes
        self.assertEqual(node["type"], "lv2")
        self.assertAlmostEqual(node["control"]["al"], 0.5, places=4)

    def test_peaking_keeps_gain_in_decibels(self):
        (node,) = render_chain([Effect("peaking", {"gain_db": 6})]).nodes
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
