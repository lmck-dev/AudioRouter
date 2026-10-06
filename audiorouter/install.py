"""Putting AudioRouter into the desktop: a menu launcher and a login service.

Nothing here touches audio. It writes two small files the desktop already knows
how to read - a freedesktop `.desktop` entry and a systemd user unit - and asks
systemd to run the unit.

Run from a checkout, both files name the exact interpreter and source directory
in use rather than a bare `audiorouter` command: an entry that said
`audiorouter` would find nothing, and a non-technical user would see an icon
that does nothing when clicked.

Installed from the RPM, the package owns both files (`packaging/`), so this
module only enables or disables the packaged unit, and on the first start for
each user switches routing from login on (`set_up_for_user`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

APP_ID = "audiorouter"
SERVICE = f"{APP_ID}.service"

#: Where the RPM puts the login unit and the menu entry.
SYSTEM_UNIT = Path("/usr/lib/systemd/user") / SERVICE
SYSTEM_LAUNCHER = Path("/usr/share/applications") / f"{APP_ID}.desktop"
_SYSTEM_PACKAGE_DIRS = (Path("/usr/lib"), Path("/usr/lib64"))

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class InstallError(RuntimeError):
    pass


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def source_root() -> Path:
    """The directory holding the `audiorouter` package, installed or not."""
    return Path(__file__).resolve().parents[1]


def packaged() -> bool:
    """True when this code is the copy the RPM installed.

    A checkout on a machine that also has the package is not: its launcher and
    unit must keep pointing at the checkout. (Fedora's `sysconfig` names
    /usr/local for the system site-packages, so the location is checked
    directly.)
    """
    root = source_root()
    return SYSTEM_UNIT.exists() and any(root.is_relative_to(d) for d in _SYSTEM_PACKAGE_DIRS)


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def launcher_path() -> Path:
    return _data_home() / "applications" / f"{APP_ID}.desktop"


def unit_path() -> Path:
    return _config_home() / "systemd" / "user" / SERVICE


# -- quoting ----------------------------------------------------------------


def _desktop_arg(arg: str) -> str:
    """One Exec argument, quoted per the Desktop Entry spec when it needs it."""
    arg = arg.replace("%", "%%")
    if not any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`"):
        return arg
    inner = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    # The whole value is itself a string value, whose own escape is a backslash.
    return '"' + inner.replace("\\", "\\\\") + '"'


def _unit_arg(arg: str) -> str:
    """One argument for a systemd command line or Environment= assignment."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if not any(c in arg for c in " \t\"'\\"):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- file contents ----------------------------------------------------------


def desktop_entry(python: str | None = None, root: Path | None = None) -> str:
    python = python or sys.executable
    root = root or source_root()
    exec_line = " ".join(
        _desktop_arg(a)
        for a in ("env", f"PYTHONPATH={root}", python, "-m", "audiorouter.gui.main")
    )
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Audio Router",
            "GenericName=Audio Channels",
            "Comment=Send each app's sound to its own channel, with its own effects",
            f"Exec={exec_line}",
            # The RPM puts this file in the icon theme as `audiorouter`; from the
            # source tree there is no theme entry, so name the file itself.
            f"Icon={root / 'audiorouter' / 'gui' / 'audiorouter.svg'}",
            "Terminal=false",
            "Categories=AudioVideo;Audio;Mixer;",
            "Keywords=audio;sound;routing;pipewire;effects;equaliser;equalizer;",
            "StartupNotify=true",
            # Matches QGuiApplication.setDesktopFileName, so the running window
            # is grouped under this entry's icon instead of a generic one.
            f"StartupWMClass={APP_ID}",
            "",
        ]
    )


def service_unit(python: str | None = None, root: Path | None = None) -> str:
    python = python or sys.executable
    root = root or source_root()
    command = " ".join(_unit_arg(a) for a in (python, "-m", "audiorouter", "watch"))
    return "\n".join(
        [
            "[Unit]",
            "Description=Audio Router - keeps each app on its channel",
            "After=pipewire.service wireplumber.service pipewire-pulse.service",
            "Wants=pipewire.service wireplumber.service",
            # A PipeWire restart takes every channel's sink with it; restarting
            # alongside it brings them back instead of routing into nothing.
            "PartOf=pipewire.service",
            "",
            "[Service]",
            "Type=simple",
            f"Environment={_unit_arg(f'PYTHONPATH={root}')}",
            "Environment=PYTHONUNBUFFERED=1",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=3",
            # Channels are separate processes that must outlive this one:
            # restarting the router must not cut the sound.
            "KillMode=process",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


# -- the launcher -----------------------------------------------------------


def launcher_installed() -> bool:
    if packaged():
        return SYSTEM_LAUNCHER.exists()
    return launcher_path().exists()


def _written_by_a_checkout(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text()
    except OSError:
        return False


def install_launcher(run: Runner = _run) -> Path:
    if packaged():
        return SYSTEM_LAUNCHER  # the package installed it
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry())
    if shutil.which("update-desktop-database"):
        run(["update-desktop-database", str(path.parent)])  # a cache; failure is harmless
    return path


def remove_launcher(run: Runner = _run) -> bool:
    if packaged():
        raise InstallError("the menu entry belongs to the package; uninstall the package instead")
    path = launcher_path()
    if not path.exists():
        return False
    path.unlink()
    if shutil.which("update-desktop-database"):
        run(["update-desktop-database", str(path.parent)])
    return True


# -- the login service ------------------------------------------------------


def _systemctl(run: Runner, *args: str) -> subprocess.CompletedProcess:
    if not shutil.which("systemctl"):
        raise InstallError("systemd is not available, so routing cannot start at login")
    return run(["systemctl", "--user", *args])


def _checked(run: Runner, *args: str) -> None:
    result = _systemctl(run, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"systemctl --user {' '.join(args)} failed: {detail}")


def login_service_enabled(run: Runner = _run) -> bool:
    if not (packaged() or unit_path().exists()):
        return False
    try:
        result = _systemctl(run, "is-enabled", SERVICE)
    except InstallError:
        return False
    return result.stdout.strip() == "enabled"


def enable_login_service(run: Runner = _run) -> Path:
    """Write the unit, then start it now and at every login.

    The unit is rewritten every time, so moving the checkout or changing Python
    is repaired by switching the option off and on again. A packaged app
    enables the package's unit instead.
    """
    if packaged():
        _retire_checkout_unit(run)
        _checked(run, "enable", "--now", SERVICE)
        return SYSTEM_UNIT
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(service_unit())
    _checked(run, "daemon-reload")
    _checked(run, "enable", "--now", SERVICE)
    return path


def disable_login_service(run: Runner = _run) -> bool:
    """Stop routing in the background. Running channels keep playing."""
    if packaged():
        if not login_service_enabled(run):
            return False
        _checked(run, "disable", "--now", SERVICE)
        return True
    path = unit_path()
    if not path.exists():
        return False
    _checked(run, "disable", "--now", SERVICE)
    path.unlink()
    _checked(run, "daemon-reload")
    return True


# -- the package, per user ----------------------------------------------------

#: Written by a checkout; a user unit or menu entry of the same name hides the
#: package's own copy, and would keep running the checkout.
_UNIT_MARK = "Description=Audio Router"
_LAUNCHER_MARK = "PYTHONPATH="


def _retire_checkout_unit(run: Runner) -> None:
    path = unit_path()
    if not _written_by_a_checkout(path, _UNIT_MARK):
        return
    _systemctl(run, "disable", "--now", SERVICE)  # the next step starts the package's
    path.unlink()
    _checked(run, "daemon-reload")


def _retire_checkout_launcher(run: Runner) -> None:
    path = launcher_path()
    if not _written_by_a_checkout(path, _LAUNCHER_MARK):
        return
    path.unlink()
    if shutil.which("update-desktop-database"):
        run(["update-desktop-database", str(path.parent)])


def setup_marker() -> Path:
    return _config_home() / APP_ID / "package-set-up"


def set_up_for_user(run: Runner = _run) -> bool:
    """The packaged app's first start for this user: routing from login on.

    Installing is one step because of this - a package cannot enable a service
    in each user's session, so the window does it the first time it opens.
    Returns True when the service was switched on just now. Later starts change
    nothing, so a user who switches it off keeps it off. A launcher or unit an
    earlier checkout wrote is taken out of the way first.
    """
    if not packaged() or setup_marker().exists():
        return False
    _retire_checkout_launcher(run)
    enable_login_service(run)  # raises before the marker: try again next start
    marker = setup_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    return True
