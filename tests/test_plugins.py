import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import plugins
from audiorouter.effects import LV2_PACKAGE, Effect, unsatisfied_requirements


class LoaderProbeTest(unittest.TestCase):
    def setUp(self):
        plugins.reset_cache()
        self.addCleanup(plugins.reset_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def loaders(self):
        with mock.patch.object(plugins, "_LOADER_DIRS", (self.dir,)):
            return plugins.available_loaders()

    def test_builtin_needs_no_loader_of_its_own(self):
        self.assertIn("builtin", self.loaders())

    def test_installed_loaders_are_found_by_file_name(self):
        (self.dir / "libspa-filter-graph-plugin-lv2.so").touch()
        (self.dir / "libspa-filter-graph-plugin-ladspa.so").touch()
        self.assertEqual(self.loaders(), frozenset({"builtin", "lv2", "ladspa"}))

    def test_an_absent_loader_directory_is_not_an_error(self):
        with mock.patch.object(plugins, "_LOADER_DIRS", (self.dir / "nope",)):
            self.assertEqual(plugins.available_loaders(), frozenset({"builtin"}))


class PluginProbeTest(unittest.TestCase):
    def setUp(self):
        plugins.reset_cache()
        self.addCleanup(plugins.reset_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_an_lv2_uri_is_found_in_a_manifest(self):
        bundle = self.dir / "lsp-plugins.lv2"
        bundle.mkdir()
        (bundle / "manifest.ttl").write_text(
            "<http://lsp-plug.in/plugins/lv2/limiter_mono> a lv2:Plugin ."
        )
        with mock.patch.object(plugins, "_LV2_DIRS", (self.dir,)):
            self.assertTrue(plugins.lv2_installed("http://lsp-plug.in/plugins/lv2/limiter_mono"))
            self.assertFalse(plugins.lv2_installed("http://example.org/nothing"))

    def test_a_ladspa_object_is_found_by_name(self):
        (self.dir / "lsp-plugins-ladspa.so").touch()
        with mock.patch.object(plugins, "_LADSPA_DIRS", (self.dir,)):
            self.assertTrue(plugins.ladspa_installed("lsp-plugins-ladspa.so"))
            self.assertTrue(plugins.ladspa_installed("lsp-plugins-ladspa"))
            self.assertFalse(plugins.ladspa_installed("not-here.so"))


class DegradationTest(unittest.TestCase):
    """Missing plugins must read as something the user can act on."""

    def setUp(self):
        plugins.reset_cache()
        self.addCleanup(plugins.reset_cache)

    def test_a_missing_loader_names_the_package_to_install(self):
        with mock.patch.object(plugins, "available_loaders", return_value=frozenset({"builtin"})):
            (missing,) = unsatisfied_requirements([Effect("limiter")])
            self.assertIn(LV2_PACKAGE, missing.explain())
            self.assertIn("lv2", missing.explain())

    def test_a_present_loader_with_no_plugin_says_which_plugin(self):
        with mock.patch.object(plugins, "available_loaders",
                               return_value=frozenset({"builtin", "lv2"})), \
             mock.patch.object(plugins, "lv2_installed", return_value=False):
            (missing,) = unsatisfied_requirements([Effect("limiter")])
            self.assertIn("limiter_stereo", missing.explain())

    def test_builtin_effects_never_demand_anything(self):
        with mock.patch.object(plugins, "available_loaders", return_value=frozenset({"builtin"})):
            self.assertEqual(unsatisfied_requirements([Effect("lowpass"), Effect("gain")]), [])


if __name__ == "__main__":
    unittest.main()
