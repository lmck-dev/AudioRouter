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


if __name__ == "__main__":
    unittest.main()
