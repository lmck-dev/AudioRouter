import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiorouter import install


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(Path(self.tmp.name) / "data"),
            "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config"),
        })
        env.start()
        self.addCleanup(env.stop)
        which = mock.patch("audiorouter.install.shutil.which", return_value="/usr/bin/x")
        which.start()
        self.addCleanup(which.stop)
        self.calls = []

    def run_ok(self, args):
        self.calls.append(list(args))
        return done(out="enabled\n")


class FileContentsTest(InstallTestCase):
    def test_the_launcher_runs_this_python_on_this_checkout(self):
        # Nothing is pip-installed; a bare `audiorouter` would find nothing.
        text = install.desktop_entry("/usr/bin/python3", Path("/src/AudioRouter"))
        self.assertIn(
            "Exec=env PYTHONPATH=/src/AudioRouter /usr/bin/python3 -m audiorouter.gui.main", text
        )
        self.assertIn("StartupWMClass=audiorouter", text)

    def test_a_path_with_spaces_is_quoted_in_the_launcher(self):
        text = install.desktop_entry("/usr/bin/python3", Path("/my stuff/AR"))
        self.assertIn('"PYTHONPATH=/my stuff/AR"', text)

    def test_the_service_runs_the_daemon_and_leaves_channels_alone_on_restart(self):
        text = install.service_unit("/usr/bin/python3", Path("/src/AudioRouter"))
        self.assertIn("ExecStart=/usr/bin/python3 -m audiorouter watch", text)
        self.assertIn("Environment=PYTHONPATH=/src/AudioRouter", text)
        self.assertIn("KillMode=process", text)
        self.assertIn("PartOf=pipewire.service", text)
        self.assertIn("WantedBy=default.target", text)

    def test_percent_signs_are_not_read_as_specifiers(self):
        text = install.service_unit("/usr/bin/python3", Path("/src/100%"))
        self.assertIn("PYTHONPATH=/src/100%%", text)


class LauncherTest(InstallTestCase):
    def test_install_and_remove(self):
        path = install.install_launcher(run=self.run_ok)
        self.assertTrue(path.exists())
        self.assertTrue(install.launcher_installed())
        self.assertTrue(install.remove_launcher(run=self.run_ok))
        self.assertFalse(path.exists())
        self.assertFalse(install.remove_launcher(run=self.run_ok))


class LoginServiceTest(InstallTestCase):
    def test_enabling_writes_the_unit_then_starts_it(self):
        path = install.enable_login_service(run=self.run_ok)
        self.assertTrue(path.exists())
        self.assertEqual(self.calls, [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "audiorouter.service"],
        ])
        self.assertTrue(install.login_service_enabled(run=self.run_ok))

    def test_disabling_stops_it_and_removes_the_unit(self):
        install.enable_login_service(run=self.run_ok)
        self.calls.clear()
        self.assertTrue(install.disable_login_service(run=self.run_ok))
        self.assertFalse(install.unit_path().exists())
        self.assertEqual(self.calls[0], ["systemctl", "--user", "disable", "--now",
                                         "audiorouter.service"])

    def test_not_enabled_without_a_unit_file(self):
        self.assertFalse(install.login_service_enabled(run=self.run_ok))
        self.assertEqual(self.calls, [])

    def test_a_systemctl_failure_is_reported_with_its_reason(self):
        with self.assertRaises(install.InstallError) as caught:
            install.enable_login_service(run=lambda args: done(1, err="Access denied"))
        self.assertIn("Access denied", str(caught.exception))

    def test_no_systemd_says_so(self):
        with mock.patch("audiorouter.install.shutil.which", return_value=None):
            with self.assertRaises(install.InstallError):
                install.enable_login_service(run=self.run_ok)


class PackageFilesTest(unittest.TestCase):
    """The RPM's own unit and menu entry must not drift from the generated ones."""

    PACKAGING = Path(__file__).resolve().parents[1] / "packaging"

    @staticmethod
    def _settings(text, skip):
        return [line for line in text.splitlines()
                if line and not line.startswith("#") and not line.startswith(skip)]

    def test_the_package_unit_matches_the_generated_one_but_runs_the_installed_command(self):
        packaged = (self.PACKAGING / "audiorouter.service").read_text()
        generated = install.service_unit("/usr/bin/python3", Path("/src"))
        skip = ("ExecStart=", "Environment=PYTHONPATH")
        self.assertEqual(self._settings(packaged, skip), self._settings(generated, skip))
        self.assertIn("ExecStart=/usr/bin/audiorouter watch", packaged)

    def test_the_package_menu_entry_matches_the_generated_one(self):
        packaged = (self.PACKAGING / "audiorouter.desktop").read_text()
        generated = install.desktop_entry("/usr/bin/python3", Path("/src"))
        self.assertEqual(self._settings(packaged, "Exec="), self._settings(generated, "Exec="))
        self.assertIn("Exec=audiorouter-gui", packaged)


class PackagedTest(InstallTestCase):
    """Installed from the RPM: the package owns the unit and the menu entry."""

    def setUp(self):
        super().setUp()
        system = Path(self.tmp.name) / "usr"
        unit = system / "lib/systemd/user/audiorouter.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n")
        for name, value in (("SYSTEM_UNIT", unit),
                            ("SYSTEM_LAUNCHER", system / "share/applications/audiorouter.desktop"),
                            ("_SYSTEM_PACKAGE_DIRS", (system / "lib",)),
                            ("source_root", lambda: system / "lib/python3.14/site-packages")):
            patch = mock.patch.object(install, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_it_knows_it_is_the_package_and_a_checkout_does_not(self):
        self.assertTrue(install.packaged())
        with mock.patch.object(install, "source_root", lambda: Path("/home/me/AudioRouter")):
            self.assertFalse(install.packaged())

    def test_enabling_uses_the_package_unit_and_writes_nothing(self):
        path = install.enable_login_service(run=self.run_ok)
        self.assertEqual(path, install.SYSTEM_UNIT)
        self.assertFalse(install.unit_path().exists())
        self.assertEqual(self.calls, [["systemctl", "--user", "enable", "--now",
                                       "audiorouter.service"]])

    def test_disabling_leaves_the_package_file_alone(self):
        self.assertTrue(install.disable_login_service(run=self.run_ok))
        self.assertTrue(install.SYSTEM_UNIT.exists())
        self.assertEqual(self.calls[-1], ["systemctl", "--user", "disable", "--now",
                                          "audiorouter.service"])

    def test_the_menu_entry_is_the_package_one(self):
        self.assertEqual(install.install_launcher(run=self.run_ok), install.SYSTEM_LAUNCHER)
        self.assertFalse(install.launcher_path().exists())
        with self.assertRaises(install.InstallError):
            install.remove_launcher(run=self.run_ok)

    def test_first_start_switches_routing_on_once(self):
        self.assertTrue(install.set_up_for_user(run=self.run_ok))
        self.assertIn(["systemctl", "--user", "enable", "--now", "audiorouter.service"],
                      self.calls)
        self.calls.clear()
        # Switched off later, it stays off.
        self.assertFalse(install.set_up_for_user(run=self.run_ok))
        self.assertEqual(self.calls, [])

    def test_a_failed_first_start_is_tried_again(self):
        with self.assertRaises(install.InstallError):
            install.set_up_for_user(run=lambda args: done(1, err="no session bus"))
        self.assertFalse(install.setup_marker().exists())
        self.assertTrue(install.set_up_for_user(run=self.run_ok))

    def test_a_checkout_s_unit_and_launcher_are_moved_out_of_the_way(self):
        # Written by the checkout, they would hide the package's copies and
        # keep running the checkout.
        with mock.patch.object(install, "packaged", return_value=False):
            install.enable_login_service(run=self.run_ok)
            install.install_launcher(run=self.run_ok)
        self.calls.clear()
        install.set_up_for_user(run=self.run_ok)
        self.assertFalse(install.unit_path().exists())
        self.assertFalse(install.launcher_path().exists())
        stop = ["systemctl", "--user", "disable", "--now", "audiorouter.service"]
        start = ["systemctl", "--user", "enable", "--now", "audiorouter.service"]
        self.assertLess(self.calls.index(stop), self.calls.index(start))

    def test_someone_else_s_unit_of_that_name_is_left_alone(self):
        install.unit_path().parent.mkdir(parents=True)
        install.unit_path().write_text("[Unit]\nDescription=Mine\n")
        install.set_up_for_user(run=self.run_ok)
        self.assertTrue(install.unit_path().exists())

    def test_a_checkout_does_nothing_at_first_start(self):
        with mock.patch.object(install, "packaged", return_value=False):
            self.assertFalse(install.set_up_for_user(run=self.run_ok))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
