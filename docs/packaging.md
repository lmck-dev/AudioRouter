# Launcher, login service, the RPM and the Arch package

Moved out of `CLAUDE.md` on 25 Sep 2026 so it loads only when the work touches it.
Read this file before changing the code it describes.

## Launcher and login service

`python -m audiorouter launcher` writes `~/.local/share/applications/audiorouter.desktop`;
`python -m audiorouter login on|off|status` (or the window's "Keep routing with
this window closed" box) writes `~/.config/systemd/user/audiorouter.service`,
which runs `watch`. **Nothing is pip-installed**, so both files pin
`sys.executable` and `PYTHONPATH=<checkout>` - moving the checkout means
re-running `launcher` and toggling login off/on.

- **The daemon's record is `routing.daemon`, NOT `daemon.pid`.** Every `*.pid`
  in the runtime dir is taken to be a channel's, so `orphan_slugs()` (called by
  every `status()`) deleted `daemon.pid` within a second of the GUI refreshing.
  Found only by enabling the real service with the real GUI open.
- **`KillMode=process`** - channels are children of whoever started them and
  must survive a daemon restart. Verified: `systemctl --user restart` left both
  channel pids unchanged.
- **The daemon reloads the config on each graph event** (`follow_config=True`),
  because the GUI edits it from another process. The GUI's own router never
  reloads - its panels hold the objects a reload would replace. The GUI does
  not start its own router while `daemon_pid()` finds one.
- **`watch` exits 1 when `pw-dump` dies** (`GraphMonitor.ended`) so systemd
  restarts it. Before this it sat forever routing nothing. Verified by killing
  only the daemon's own `pw-dump` child: restart in 3s, channels untouched.
- **`apply()` restarts a running channel whose sink is missing from the graph.**
  A host process can outlive the PipeWire it was attached to; conf unchanged
  plus pid alive used to count as healthy. Unit-tested only - verifying it live
  means restarting the user's PipeWire.


## The RPM (15 Sep 2026)

`packaging/`: `audiorouter.spec`, the packaged `audiorouter.service` (runs
`/usr/bin/audiorouter watch`) and `audiorouter.desktop` (runs `audiorouter-gui`),
and `build-rpm.sh`, which tars the working tree (committed or not) and runs
`rpmbuild` in a `fedora:44` podman container, `%check` included. Our noise
plugin and level tap are compiled at build time into `/usr/lib64/lv2/audiorouter-rnnoise.lv2`
and `audiorouter-meter.lv2`, so users need no gcc.
The launcher icon is `audiorouter/gui/audiorouter.svg` (the phone app's icon on a
rounded tile, made by AudioRouterRemote's `tools/icon.py` - regenerate it there);
the RPM installs it as hicolor `audiorouter`, a source install names the file. Bump the version in BOTH `pyproject.toml` and the spec
(the script refuses a mismatch).

- **`install.packaged()` is decided by location, not `sysconfig`.** Fedora's
  `sysconfig.get_path("purelib")` says `/usr/local/lib/python3.x/site-packages`,
  but the RPM installs to `/usr/lib/...`. Packaged = the system unit exists AND
  this code lives under `/usr/lib*`, so a checkout on a machine that also has
  the package still writes its own launcher and unit.
- **A package cannot enable a user service in each session**, so
  `install.set_up_for_user()` does it at the window's first start and writes
  `~/.config/audiorouter/package-set-up`; later starts change nothing (switched
  off stays off). It also removes a checkout's `~/.config/systemd/user` unit and
  `~/.local` launcher, which would otherwise HIDE the package's copies and keep
  running the checkout. The window then waits up to 3 s for the daemon record,
  or it would start a second router.
- **On a box that already ran the checkout, the menu still opens the CHECKOUT
  after installing** - a `~/.local/share/applications` entry of the same name
  wins over the system one, and the checkout's window never runs the set-up.
  Start `/usr/bin/audiorouter-gui` once by hand (hit on the owner's box 15 Sep).
- **`systemctl --user enable --now` without a running user manager** (e.g. a
  container) creates the link, skips `--now`, and exits 0 - not a failure.
- `PackageFilesTest` keeps the packaged unit and menu entry identical to the
  generated ones apart from the command. Change both together.
- Test the package with a fresh `fedora:44` container (`dnf install` the rpm,
  `useradd`, `runuser` - the base image has no `su`), not by installing it on
  the dev box: that swaps the owner's login service over to the packaged copy.


## The Arch package (4 Oct 2026)

`packaging/arch/PKGBUILD` (+ `.SRCINFO` for the AUR) mirrors the spec: same
wheel, same two LV2 bundles, but in `/usr/lib/lv2` (Arch has no lib64), unit in
`/usr/lib/systemd/user`. `packaging/build-arch.sh` tars the working tree under
GitHub's tag-archive name, so makepkg uses it instead of downloading
(`--skipchecksums`), and builds + `check()`s in `archlinux:latest`.

- **The PKGBUILD downloads the release TAG**, with a pinned sha256. Bump
  `pkgver` with `pyproject.toml` (the script refuses a mismatch), then re-pin the
  sum and regenerate `.SRCINFO` (`makepkg --printsrcinfo`) after tagging.
- **Arch package names**: the LV2 loader is in `pipewire-audio` (it brings
  lilv), `pactl`/`parec` in `libpulse`. The app's "install ..." hints still name
  the Fedora packages (`effects.LV2_PACKAGE`); the PKGBUILD depends on all of
  them, so an Arch user only sees a hint after removing one.
- **makepkg cannot build in a rootless podman mount**: files its user writes
  are owned by a sub-uid the host cannot delete. The script copies the source
  into the container and copies only the package out.
- **A bare container has almost no fonts**, so strip labels elide differently
  from the dev box. Assert a shortened label's tooltip (the full text), never
  its visible text. v1.2.1's tag still has one such test, so building 1.2.1 from
  the tag fails `check()` in a font-poor container: the AUR needs 1.2.2+ (released 4 Oct 2026).
- `namcap` on the package lists every PyQt6 import as an "uninstalled
  dependency" when run before installing the deps; it is noise.
