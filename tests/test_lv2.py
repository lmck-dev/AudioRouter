"""The Turtle reader and the LV2 catalogue, against small hand-written bundles.

The fixtures copy the shapes real plugins use (LSP blank-node units and scale
points, ZAM sidechain inputs, Calf's empty prefix, MDA's long comments) so the
tests never depend on what happens to be installed.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import lv2, turtle

MANIFEST = """
@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://example.org/comp_stereo> a lv2:Plugin ; lv2:binary <x.so> ; rdfs:seeAlso <comp_stereo.ttl> .
<urn:zam:Mono> a lv2:Plugin ; lv2:binary <x.so> ; rdfs:seeAlso <mono.ttl> .
<http://example.org/synth> a lv2:Plugin ; lv2:binary <x.so> ; rdfs:seeAlso <synth.ttl> .
<http://example.org/needy> a lv2:Plugin ; lv2:binary <x.so> ; rdfs:seeAlso <needy.ttl> .
"""

COMP_STEREO = '''
@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix doap: <http://usefulinc.com/ns/doap#> .
@prefix units: <http://lv2plug.in/ns/extensions/units#> .
@prefix pp: <http://lv2plug.in/ns/ext/port-props#> .
@prefix atom: <http://lv2plug.in/ns/ext/atom#> .

<http://example.org/comp_stereo>
    a lv2:CompressorPlugin, doap:Project ;
    doap:name "Example Compressor Stereo" ;
    rdfs:comment """Two lines
of "description".""" ;
    lv2:requiredFeature <http://lv2plug.in/ns/ext/urid#map> ;
    lv2:port [ a lv2:InputPort, lv2:AudioPort ; lv2:index 0 ; lv2:symbol "in_l" ; lv2:name "In L" ; ] ,
             [ a lv2:InputPort, lv2:AudioPort ; lv2:index 1 ; lv2:symbol "in_r" ; lv2:name "In R" ; ] ,
             [ a lv2:OutputPort, lv2:AudioPort ; lv2:index 2 ; lv2:symbol "out_l" ; lv2:name "Out L" ; ] ,
             [ a lv2:OutputPort, lv2:AudioPort ; lv2:index 3 ; lv2:symbol "out_r" ; lv2:name "Out R" ; ] ,
             [ a lv2:InputPort, atom:AtomPort ; lv2:index 4 ; lv2:symbol "in_ui" ; lv2:name "UI" ; ] ,
             [ a lv2:InputPort, lv2:ControlPort ; lv2:index 5 ; lv2:symbol "enabled" ;
               lv2:name "Enabled" ; lv2:designation lv2:enabled ; lv2:portProperty lv2:toggled ;
               lv2:minimum 0 ; lv2:maximum 1 ; lv2:default 1 ; ] ,
             [ a lv2:InputPort, lv2:ControlPort ; lv2:index 6 ; lv2:symbol "al" ; lv2:name "Threshold" ;
               units:unit [ a units:Unit ; rdfs:label "gain" ; units:symbol "G" ; ] ;
               lv2:portProperty pp:logarithmic ; lv2:minimum 0.001 ; lv2:maximum 1.0 ; lv2:default 0.25 ; ] ,
             [ a lv2:InputPort, lv2:ControlPort ; lv2:index 7 ; lv2:symbol "cm" ; lv2:name "Mode" ;
               lv2:portProperty lv2:integer, lv2:enumeration ;
               lv2:scalePoint [ rdfs:label "Up"; rdf:value 1 ] , [ rdfs:label "Down"; rdf:value 0 ] ;
               lv2:minimum 0 ; lv2:maximum 1 ; lv2:default 0 ; ] ,
             [ a lv2:InputPort, lv2:ControlPort ; lv2:index 8 ; lv2:symbol "at" ; lv2:name "Attack" ;
               units:unit units:ms ; lv2:minimum 0 ; lv2:maximum 2000 ; lv2:default 20 ; ] ,
             [ a lv2:InputPort, lv2:ControlPort ; lv2:index 9 ; lv2:symbol "igv" ;
               lv2:name "Input graph visibility" ; lv2:minimum 0 ; lv2:maximum 1 ; lv2:default 1 ; ] ,
             [ a lv2:OutputPort, lv2:ControlPort ; lv2:index 10 ; lv2:symbol "meter" ; lv2:name "Meter" ; ] .
'''

MONO = """
@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix unit: <http://lv2plug.in/ns/extensions/units#> .
<urn:zam:Mono>
    a lv2:Plugin, lv2:CompressorPlugin ;
    lv2:port [ a lv2:InputPort, lv2:AudioPort ; lv2:index 0 ; lv2:symbol "lv2_audio_in_1" ; lv2:name "In" ; ] ,
    [ a lv2:InputPort, lv2:AudioPort ; lv2:index 1 ; lv2:symbol "lv2_sidechain_in" ;
      lv2:name "Sidechain" ; lv2:portProperty lv2:isSideChain; ] ;
    lv2:port [ a lv2:OutputPort, lv2:AudioPort ; lv2:index 2 ; lv2:symbol "lv2_audio_out_1" ; lv2:name "Out" ; ] ;
    lv2:port [ a lv2:InputPort, lv2:ControlPort ; lv2:index 3 ; lv2:name "Threshold" ; lv2:symbol "thr" ;
      lv2:default 0 ; lv2:minimum -80.0 ; lv2:maximum 0 ; unit:unit unit:db ; ] .
"""

SYNTH = """
@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
<http://example.org/synth> a lv2:InstrumentPlugin ;
    lv2:port [ a lv2:OutputPort, lv2:AudioPort ; lv2:index 0 ; lv2:symbol "out" ; lv2:name "Out" ] .
"""

NEEDY = """
@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
<http://example.org/needy> a lv2:Plugin ;
    lv2:requiredFeature <http://lv2plug.in/ns/ext/instance-access> ;
    lv2:port [ a lv2:InputPort, lv2:AudioPort ; lv2:index 0 ; lv2:symbol "in" ; lv2:name "In" ] ,
             [ a lv2:OutputPort, lv2:AudioPort ; lv2:index 1 ; lv2:symbol "out" ; lv2:name "Out" ] .
"""


def write_bundle(root: Path) -> Path:
    bundle = root / "example.lv2"
    bundle.mkdir(parents=True)
    for name, text in (
        ("manifest.ttl", MANIFEST), ("comp_stereo.ttl", COMP_STEREO), ("mono.ttl", MONO),
        ("synth.ttl", SYNTH), ("needy.ttl", NEEDY),
    ):
        (bundle / name).write_text(text)
    return bundle


class TurtleTest(unittest.TestCase):
    def test_prefixes_blank_nodes_and_literals(self):
        triples = turtle.parse(
            '@prefix ex: <http://e/> .\n'
            'ex:a ex:n 3 ; ex:f -0.5 ; ex:e 1.0E3 ; ex:s "hi" ; ex:b true ;\n'
            '     ex:x [ ex:y "z"@en ] , ex:c ; .\n'
        )
        store = turtle.Store()
        store.add(triples)
        self.assertEqual(store.value("http://e/a", "http://e/n"), 3)
        self.assertEqual(store.value("http://e/a", "http://e/f"), -0.5)
        self.assertEqual(store.value("http://e/a", "http://e/e"), 1000.0)
        self.assertIs(store.value("http://e/a", "http://e/b"), True)
        blank, iri = store.objects("http://e/a", "http://e/x")
        self.assertEqual(store.value(blank, "http://e/y"), "z")
        self.assertEqual(iri, "http://e/c")

    def test_an_integer_before_the_final_dot_is_not_read_as_a_decimal(self):
        store = turtle.Store()
        store.add(turtle.parse("@prefix ex: <http://e/> .\nex:a ex:n 1.\n"))
        self.assertEqual(store.value("http://e/a", "http://e/n"), 1)

    def test_the_empty_prefix_and_relative_iris(self):
        store = turtle.Store()
        store.add(turtle.parse("@prefix : <http://e/p#> .\n:a :b <x.ttl> .", "file:///b/manifest.ttl"))
        self.assertEqual(store.value("http://e/p#a", "http://e/p#b"), "file:///b/x.ttl")

    def test_long_strings_keep_quotes_and_newlines(self):
        store = turtle.Store()
        store.add(turtle.parse('<http://e/a> <http://e/c> """one\n"two" """ .'))
        self.assertEqual(store.value("http://e/a", "http://e/c"), 'one\n"two" ')

    def test_blank_nodes_from_separate_files_never_merge(self):
        # Regression: tagging parses with id() reused tags once a parser was
        # freed, and every plugin in LSP got every other plugin's ports.
        store = turtle.Store()
        for n in range(20):
            store.add(turtle.parse(f'<http://e/p{n}> <http://e/port> [ <http://e/sym> "s{n}" ] .'))
        for n in range(20):
            (port,) = store.objects(f"http://e/p{n}", "http://e/port")
            self.assertEqual(store.objects(port, "http://e/sym"), [f"s{n}"])

    def test_garbage_is_refused_with_a_line_number(self):
        with self.assertRaises(turtle.TurtleError) as caught:
            turtle.parse("@prefix ex: <http://e/> .\n\nex:a ex:b ex:c ex:d .")
        self.assertIn("line 3", str(caught.exception))

    def test_an_undeclared_prefix_is_an_error(self):
        with self.assertRaises(turtle.TurtleError):
            turtle.parse("nope:a nope:b nope:c .")


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lv2"
        write_bundle(self.root)
        env = mock.patch.dict(os.environ, {
            "LV2_PATH": str(self.root), "XDG_CACHE_HOME": str(Path(self.tmp.name) / "cache"),
            "LADSPA_PATH": str(Path(self.tmp.name) / "ladspa"),  # none of this machine's
        })
        env.start()
        self.addCleanup(env.stop)
        lv2.reset_cache()
        self.addCleanup(lv2.reset_cache)

    def test_a_stereo_plugin_is_usable_with_its_ports_in_order(self):
        plugin = lv2.catalogue().get("http://example.org/comp_stereo")
        self.assertTrue(plugin.usable, plugin.problems)
        self.assertTrue(plugin.stereo)
        self.assertEqual(plugin.audio_in, ("in_l", "in_r"))
        self.assertEqual(plugin.audio_out, ("out_l", "out_r"))
        self.assertEqual(plugin.name, "Example Compressor Stereo")
        self.assertEqual(plugin.classes, ("Compressor",))

    def test_the_enable_port_is_ours_to_drive_not_a_control(self):
        plugin = lv2.catalogue().get("http://example.org/comp_stereo")
        self.assertEqual(plugin.enable_port, "enabled")
        self.assertNotIn("enabled", [c.symbol for c in plugin.controls])

    def test_meters_are_not_controls_and_ui_toggles_are_hidden(self):
        plugin = lv2.catalogue().get("http://example.org/comp_stereo")
        self.assertNotIn("meter", [c.symbol for c in plugin.controls])
        self.assertTrue(plugin.control("igv").hidden)
        self.assertFalse(plugin.control("at").hidden)

    def test_units_choices_and_gain(self):
        plugin = lv2.catalogue().get("http://example.org/comp_stereo")
        self.assertTrue(plugin.control("al").is_gain)
        self.assertEqual(plugin.control("at").unit, "ms")
        self.assertEqual(plugin.control("cm").choices, (("Down", 0.0), ("Up", 1.0)))

    def test_a_mono_plugin_with_a_sidechain_is_usable_and_the_sidechain_left_spare(self):
        plugin = lv2.catalogue().get("urn:zam:Mono")
        self.assertTrue(plugin.usable, plugin.problems)
        self.assertEqual(plugin.audio_in, ("lv2_audio_in_1",))
        self.assertEqual(plugin.spare_inputs, ("lv2_sidechain_in",))
        self.assertEqual(plugin.control("thr").unit, "dB")
        self.assertEqual(plugin.vendor, "zam")

    def test_instruments_and_unsupported_features_are_listed_with_a_reason(self):
        synth = lv2.catalogue().get("http://example.org/synth")
        needy = lv2.catalogue().get("http://example.org/needy")
        self.assertIn("makes sound rather than processing it", synth.problems)
        self.assertTrue(any("instance-access" in p for p in needy.problems))
        self.assertEqual({p.uri for p in lv2.catalogue().usable()},
                         {"http://example.org/comp_stereo", "urn:zam:Mono"})

    def test_the_catalogue_is_cached_and_rebuilt_when_a_description_changes(self):
        lv2.catalogue()
        cached = json.loads(lv2.cache_path().read_text())
        self.assertEqual(len(cached["plugins"]), 4)
        lv2.reset_cache()
        with mock.patch.object(lv2, "build", side_effect=AssertionError("should use cache")):
            self.assertIsNotNone(lv2.catalogue().get("urn:zam:Mono"))
        ttl = self.root / "example.lv2" / "mono.ttl"
        ttl.write_text(MONO.replace('"Threshold"', '"Level"'))
        os.utime(ttl, ns=(1, 1))
        lv2.reset_cache()
        self.assertEqual(lv2.catalogue().get("urn:zam:Mono").control("thr").name, "Level")

    def test_an_unreadable_bundle_is_reported_not_fatal(self):
        broken = self.root / "broken.lv2"
        broken.mkdir()
        (broken / "manifest.ttl").write_text("this is not turtle")
        lv2.reset_cache()
        catalogue = lv2.catalogue()
        self.assertIn(str(broken), catalogue.unreadable)
        self.assertIsNotNone(catalogue.get("urn:zam:Mono"))


if __name__ == "__main__":
    unittest.main()


class CategoryTest(unittest.TestCase):
    def cat(self, name, *classes):
        return lv2.categorise(name, classes)

    def test_declared_classes_decide(self):
        self.assertEqual(self.cat("Whatever", "Compressor"), "Dynamics")
        self.assertEqual(self.cat("Whatever", "Flanger"), "Modulation")
        self.assertEqual(self.cat("Whatever", "ParaEQ"), "EQ & filters")
        self.assertEqual(self.cat("Whatever", "Pitch"), "Pitch & voice")

    def test_plugins_without_a_class_are_sorted_by_name(self):
        self.assertEqual(self.cat("RNNoise suppression for voice"), "Noise & gates")
        self.assertEqual(self.cat("GxWah"), "Modulation")

    def test_a_gate_is_a_gate_before_it_is_dynamics_and_a_noise_generator_is_not_noise_removal(self):
        self.assertEqual(self.cat("Calf Multiband Gate", "Gate", "Dynamics"), "Noise & gates")
        self.assertEqual(self.cat("LSP Noise Generator x1", "Generator"), "Utility")

    def test_anything_unrecognised_is_utility(self):
        self.assertEqual(self.cat("Mystery box"), "Utility")


class VendorTest(unittest.TestCase):
    def vendor(self, uri):
        return lv2.Plugin(uri=uri, name="x", bundle="/x").vendor

    def test_makers_are_named_as_people_know_them(self):
        self.assertEqual(self.vendor("http://plugin.org.uk/swh-plugins/flanger"), "SWH")
        self.assertEqual(self.vendor("http://gareus.org/oss/lv2/fat1"), "x42")
        self.assertEqual(self.vendor("http://breakfastquay.com/rdf/lv2-rubberband#livestereo"), "Rubber Band")
        self.assertEqual(self.vendor("https://github.com/werman/noise-suppression-for-voice#stereo"), "RNNoise")

    def test_a_short_needle_does_not_match_inside_another_word(self):
        # Regression: "tap" matched SWH's tapeDelay.
        self.assertEqual(self.vendor("http://plugin.org.uk/swh-plugins/tapeDelay"), "SWH")
