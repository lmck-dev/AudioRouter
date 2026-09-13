"""Plugin effects, and changing knobs on a running channel without a restart.

The catalogue is replaced with a hand-built one, so nothing here depends on the
plugins installed on the machine running the tests.
"""

import json
import subprocess
import unittest
from unittest import mock

from audiorouter import lv2
from audiorouter.channels import Channel, ChannelError
from audiorouter.config import Config
from audiorouter.effects import (
    Effect,
    EffectError,
    linear_to_db,
    make_effect,
    plugin_specs,
    render_chain,
    spec_for,
    unsatisfied_requirements,
)

from .test_engine import EngineTestCase

STEREO = "http://example.org/comp_stereo"
MONO = "urn:zam:Mono"
SYNTH = "http://example.org/synth"


def fake_catalogue() -> lv2.Catalogue:
    stereo = lv2.Plugin(
        uri=STEREO, name="Example Compressor", bundle="/x", classes=("Compressor",),
        audio_in=("in_l", "in_r"), audio_out=("out_l", "out_r"), enable_port="enabled",
        controls=(
            lv2.Control("al", "Threshold", 6, 0.001, 1.0, 0.25, unit="G", logarithmic=True),
            lv2.Control("cm", "Mode", 7, 0, 2, 0, integer=True, choices=(("Down", 0), ("Up", 1), ("Boot", 2))),
            lv2.Control("sw", "Switch", 8, 0, 1, 0, toggled=True),
            lv2.Control("igv", "Input graph visibility", 9, 0, 1, 1, hidden=True),
        ),
    )
    mono = lv2.Plugin(
        uri=MONO, name="Mono Comp", bundle="/x", audio_in=("in",), audio_out=("out",),
        spare_inputs=("sc",), controls=(lv2.Control("thr", "Threshold", 3, -80, 0, 0, unit="dB"),),
    )
    synth = lv2.Plugin(uri=SYNTH, name="Synth", bundle="/x", audio_out=("out",),
                       problems=("makes sound rather than processing it",))
    return lv2.Catalogue(plugins={p.uri: p for p in (stereo, mono, synth)})


class PluginTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("audiorouter.lv2.catalogue", side_effect=fake_catalogue)
        patcher.start()
        self.addCleanup(patcher.stop)
        loaders = mock.patch("audiorouter.plugins.available_loaders",
                             return_value=frozenset({"builtin", "lv2"}))
        loaders.start()
        self.addCleanup(loaders.stop)
        installed = mock.patch("audiorouter.plugins.lv2_installed", return_value=True)
        installed.start()
        self.addCleanup(installed.stop)


class PluginSpecTest(PluginTestCase):
    def test_controls_become_knobs_with_gain_shown_as_db(self):
        spec = spec_for("lv2", STEREO)
        self.assertEqual(spec.label, "Example Compressor")
        threshold = spec.param("al")
        self.assertTrue(threshold.db)
        self.assertEqual(threshold.unit, "dB")
        self.assertEqual(spec.param("cm").choices[2], ("Boot", 2))

    def test_hidden_controls_are_not_stored_or_offered(self):
        spec = spec_for("lv2", STEREO)
        self.assertNotIn("igv", spec.defaults())
        self.assertNotIn("igv", [p.key for p in spec.visible_params()])

    def test_toggles_and_choices_snap_to_legal_values(self):
        spec = spec_for("lv2", STEREO)
        self.assertEqual(spec.param("sw").clamp(0.7), 1.0)
        self.assertEqual(spec.param("sw").clamp(0.2), 0.0)
        self.assertEqual(spec.param("cm").clamp(1.4), 1.0)

    def test_a_setting_for_a_port_the_plugin_lost_is_dropped_not_fatal(self):
        effect = Effect("lv2", {"al": 0.5, "gone": 3}, plugin=STEREO)
        self.assertEqual(effect.resolved()["al"], 0.5)
        self.assertNotIn("gone", effect.resolved())

    def test_the_browser_lists_only_usable_plugins_unless_asked(self):
        self.assertEqual({s.plugin for s in plugin_specs()}, {STEREO, MONO})
        self.assertIn(SYNTH, {s.plugin for s in plugin_specs(include_unusable=True)})

    def test_an_unusable_or_uninstalled_plugin_explains_itself(self):
        (problem,) = unsatisfied_requirements([Effect("lv2", plugin=SYNTH)])
        self.assertIn("makes sound", problem.explain())
        (missing,) = unsatisfied_requirements([Effect("lv2", plugin="http://example.org/uninstalled")])
        self.assertIn("not installed", missing.explain())

    def test_a_plugin_effect_without_a_uri_is_refused(self):
        with self.assertRaises(EffectError):
            make_effect("lv2")
        with self.assertRaises(EffectError):
            Effect.from_dict({"kind": "lv2"})

    def test_serialisation_keeps_the_plugin(self):
        effect = make_effect("lv2", {"al": 0.1}, plugin=STEREO)
        again = Effect.from_dict(json.loads(json.dumps(effect.to_dict())))
        self.assertEqual((again.kind, again.plugin, again.params["al"]), ("lv2", STEREO, 0.1))

    def test_curated_effects_do_not_save_a_plugin_field(self):
        self.assertNotIn("plugin", make_effect("gain").to_dict())

    def test_linear_to_db(self):
        self.assertAlmostEqual(linear_to_db(0.5), -6.0206, places=3)
        self.assertEqual(linear_to_db(0), -120.0)


class PluginRenderTest(PluginTestCase):
    def test_a_stereo_plugin_is_one_node_fed_both_sides(self):
        chain = make_effect("lv2", {"al": 0.1}, plugin=STEREO).render(0)
        (node,) = chain.nodes
        self.assertEqual(node["plugin"], STEREO)
        self.assertEqual(chain.inputs, ("fx0:in_l", "fx0:in_r"))
        self.assertEqual(chain.outputs, ("fx0:out_l", "fx0:out_r"))
        self.assertEqual(node["control"]["al"], 0.1)
        self.assertEqual(node["control"]["enabled"], 1.0)
        self.assertNotIn("igv", node["control"])

    def test_a_mono_plugin_runs_once_per_side_and_its_sidechain_is_never_a_graph_port(self):
        chain = make_effect("lv2", {"thr": -20}, plugin=MONO).render(0)
        self.assertEqual([n["name"] for n in chain.nodes], ["fx0_l", "fx0_r"])
        self.assertEqual(chain.inputs, ("fx0_l:in", "fx0_r:in"))
        self.assertTrue(all(n["control"]["thr"] == -20 for n in chain.nodes))
        self.assertFalse(any("sc" in port for port in chain.inputs + chain.outputs))

    def test_plugins_and_curated_effects_chain_side_by_side(self):
        chain = render_chain([make_effect("gain"), make_effect("lv2", plugin=STEREO)])
        self.assertIn({"output": "sw1_in_l:Out", "input": "fx1:in_l"}, chain.links)
        self.assertIn({"output": "fx1:out_r", "input": "sw1_switch_r:In 1"}, chain.links)

    def test_rendering_an_unusable_plugin_fails_loudly(self):
        with self.assertRaises(EffectError):
            render_chain([Effect("lv2", plugin=SYNTH)])


class ControlChangeTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.channel = Channel("x", "X", "dev", effects=[make_effect("gain"), make_effect("lv2", plugin=STEREO)])
        self.channel.config_path.write_text(self.channel.render_config_text())

    def test_nothing_changed_is_an_empty_set_of_changes(self):
        self.assertEqual(self.channel.control_changes(), {})

    def test_a_knob_is_a_live_change_naming_every_node_it_touches(self):
        self.channel.effects[0].params["gain_db"] = -6.0206
        changes = self.channel.control_changes()
        self.assertEqual(set(changes), {"gain0_l:Gain 1", "gain0_r:Gain 1"})
        self.assertAlmostEqual(changes["gain0_l:Gain 1"], 0.5, places=4)

    def test_adding_or_moving_an_effect_needs_a_restart(self):
        self.channel.effects.reverse()
        self.assertIsNone(self.channel.control_changes())
        self.channel.effects.reverse()
        self.channel.effects.append(make_effect("peaking"))
        self.assertIsNone(self.channel.control_changes())

    def test_switching_an_effect_off_or_on_is_a_live_change(self):
        self.channel.effects[1].enabled = False
        self.assertEqual(self.channel.control_changes(), {
            "sw1_switch_l:Gain 1": 0.0, "sw1_switch_l:Gain 2": 1.0,
            "sw1_switch_r:Gain 1": 0.0, "sw1_switch_r:Gain 2": 1.0,
        })

    def test_a_setting_that_reshapes_the_graph_needs_a_restart(self):
        channel = Channel("y", "Y", "dev", effects=[make_effect("highpass", {"poles": 2})])
        channel.config_path.write_text(channel.render_config_text())
        channel.effects[0].params["frequency"] = 300
        self.assertEqual(set(channel.control_changes()),
                         {f"highpass0_{i}_{s}:Freq" for i in (0, 1) for s in "lr"})
        channel.effects[0].params["poles"] = 3
        self.assertIsNone(channel.control_changes())

    def test_no_record_of_the_running_conf_means_a_restart(self):
        self.channel.config_path.unlink()
        self.assertIsNone(self.channel.control_changes())

    def test_set_controls_sends_one_props_update_and_records_it(self):
        from .test_engine import live_graph

        channel = Channel("speakers", "Speakers", "alsa_output.a", effects=[make_effect("gain")])
        channel.config_path.write_text(channel.render_config_text())
        channel.effects[0].params["gain_db"] = -6
        changes = channel.control_changes()
        with mock.patch("audiorouter.channels.require_tools"), \
             mock.patch("audiorouter.channels.subprocess.run") as run:
            channel.set_controls(changes, live_graph())
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["pw-cli", "set-param", "51", "Props"])
        params = json.loads(argv[4])["params"]
        self.assertEqual(params[0::2], ["gain0_l:Gain 1", "gain0_r:Gain 1"])
        self.assertEqual(channel.control_changes(), {})

    def test_switching_an_effect_off_fades_rather_than_jumps(self):
        from .test_engine import live_graph

        channel = Channel("speakers", "Speakers", "alsa_output.a", effects=[make_effect("gain")])
        channel.config_path.write_text(channel.render_config_text())
        channel.effects[0].enabled = False
        with mock.patch("audiorouter.channels.require_tools"), \
             mock.patch("audiorouter.channels.time.sleep"), \
             mock.patch("audiorouter.channels.subprocess.run") as run:
            channel.set_controls(channel.control_changes(), live_graph())
        wet = [dict(zip(p[0::2], p[1::2]))["sw0_switch_l:Gain 1"]
               for p in (json.loads(c.args[0][4])["params"] for c in run.call_args_list)]
        self.assertEqual(wet, [0.8, 0.6, 0.4, 0.2, 0.0])
        self.assertEqual(channel.control_changes(), {})

    def test_set_controls_without_a_sink_refuses(self):
        from audiorouter.pwgraph import Graph

        with mock.patch("audiorouter.channels.require_tools"), self.assertRaises(ChannelError):
            self.channel.set_controls({"gain0_l:Gain 1": 0.5}, Graph([]))


class EngineTuneTest(EngineTestCase):
    """apply() changes knobs in place and restarts only for new graphs."""

    def setUp(self):
        super().setUp()
        self.engine.add_effect("speakers", "gain", {"gain_db": 0})
        channel = self.engine.channel("speakers")
        channel.config_path.write_text(channel.render_config_text())

    def test_a_knob_change_is_applied_live_without_a_restart(self):
        self.engine.set_effect_params("speakers", 0, {"gain_db": -6})
        with mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "set_controls") as tune, \
             mock.patch.object(Channel, "start") as start:
            report = self.engine.apply()
        start.assert_not_called()
        tune.assert_called_once()
        self.assertEqual([a.kind for a in report.actions], ["tune"])
        self.assertFalse(report.restarted)

    def test_a_refused_live_change_falls_back_to_a_restart(self):
        self.engine.set_effect_params("speakers", 0, {"gain_db": -6})
        with mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "set_controls", side_effect=ChannelError("no")), \
             mock.patch.object(Channel, "start", return_value=1) as start:
            report = self.engine.apply()
        start.assert_called_once()
        self.assertTrue(report.restarted)

    def test_a_new_effect_restarts_the_channel(self):
        self.engine.add_effect("speakers", "peaking")
        with mock.patch.object(Channel, "is_running", return_value=True), \
             mock.patch.object(Channel, "set_controls") as tune, \
             mock.patch.object(Channel, "start", return_value=1) as start:
            self.engine.apply()
        tune.assert_not_called()
        start.assert_called_once()

    def test_a_plugin_effect_can_be_added_through_the_engine(self):
        with mock.patch("audiorouter.lv2.catalogue", side_effect=fake_catalogue):
            effect = self.engine.add_effect("speakers", "lv2", {"thr": -12}, plugin=MONO)
            self.assertEqual(effect.plugin, MONO)
            saved = Config.load(self.path).channel("speakers").effects[-1]
            self.assertEqual((saved.plugin, saved.params["thr"]), (MONO, -12))


if __name__ == "__main__":
    unittest.main()
