"""The sound system around us: noticing when it needs a restart, and doing it.

**Restarting WirePlumber on its own breaks echo cancellation** (measured on the
owner's desk, 1 Oct 2026): from then on the PipeWire daemon schedules the mic
one cycle late (pw-top WAIT 5.3 ms instead of ~0.4 ms) and every echo
canceller counts ~190 errors a second. Restarting the channel, suspending the
card and re-creating its nodes all left it broken; only restarting PipeWire
itself cures it. A private PipeWire + WirePlumber with null devices did NOT
reproduce it, so it needs real hardware to test.

So Audio Router cannot quietly recover. It notices instead - WirePlumber
started well after PipeWire means someone restarted it alone, which needs no
state and so works whenever the window or the service starts - and offers a
fix the user chooses to run (owner ruling: a notification with a Fix button,
never an automatic restart, which could cut a call without warning).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from .channels import Channel
from .pwgraph import Graph

#: At login PipeWire and WirePlumber start within a second of each other; a
#: WirePlumber that started this much later was restarted on its own.
LONE_RESTART_S = 10.0

MESSAGE = (
    "Echo cancellation stopped working: the sound system's session manager "
    "(WirePlumber) was restarted on its own. Restarting the whole sound system "
    "fixes it - every sound stops for a few seconds."
)


def started_at(pid: int) -> float | None:
    """When a process started, in seconds since boot (None if it is gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name is in parentheses and may contain spaces.
    fields = stat.rsplit(")", 1)[1].split()
    return int(fields[19]) / os.sysconf("SC_CLK_TCK")


def wireplumber_restarted_alone(graph: Graph) -> int | None:
    """WirePlumber's pid if it was restarted without PipeWire, else None."""
    daemon, session = graph.daemon_pid(), graph.session_manager_pid()
    if daemon is None or session is None:
        return None
    daemon_start, session_start = started_at(daemon), started_at(session)
    if daemon_start is None or session_start is None:
        return None
    return session if session_start - daemon_start > LONE_RESTART_S else None


def echo_cancel_broken(channels: Iterable[Channel], graph: Graph) -> int | None:
    """WirePlumber's pid if an echo-cancelling channel is broken by its restart."""
    if not any(c.is_input and c.enabled and c.echo_cancel for c in channels):
        return None
    return wireplumber_restarted_alone(graph)


def restart_sound_system() -> None:
    """Restart PipeWire, its Pulse server and WirePlumber, then bring channels back.

    Runs as its own transient unit: our login service is `PartOf=pipewire`,
    so a restart started from inside it would be killed half way. When the
    service is running it re-applies the channels itself as it comes back;
    otherwise this applies them.
    """
    package_parent = str(Path(__file__).resolve().parent.parent)
    apply = f"{sys.executable} -m audiorouter apply"
    script = ("systemctl --user restart pipewire pipewire-pulse wireplumber && sleep 3 && "
              f"(systemctl --user -q is-active audiorouter || {apply})")
    if shutil.which("systemd-run"):
        command = ["systemd-run", "--user", "--collect", "--quiet",
                   f"--unit=audiorouter-sound-restart-{int(time.time())}",
                   f"--setenv=PYTHONPATH={package_parent}", "sh", "-c", script]
        env = None
    else:  # pragma: no cover - every systemd desktop has systemd-run
        command = ["sh", "-c", script]
        env = {**os.environ, "PYTHONPATH": package_parent}
    subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


class EchoCancelNotifier:
    """For the login service: one desktop notification per broken WirePlumber.

    The notification's Fix button runs `restart_sound_system`. `notify-send`
    waits for the click on its own thread, so the graph monitor never blocks.
    """

    def __init__(self) -> None:
        self._told: set[int] = set()

    def check(self, channels: Iterable[Channel], graph: Graph) -> None:
        session = echo_cancel_broken(channels, graph)
        if session is None or session in self._told or not shutil.which("notify-send"):
            return
        self._told.add(session)
        threading.Thread(target=self._notify, name="echo-cancel-notice", daemon=True).start()

    @staticmethod
    def _notify() -> None:
        notify_with_fix("Echo cancellation stopped working", MESSAGE)


def notify_with_fix(title: str, message: str) -> None:
    """A critical desktop notification whose button restarts the sound system.

    Blocks until the notification closes, so call it from its own thread.
    """
    try:
        chosen = subprocess.run(
            ["notify-send", "--app-name=Audio Router", "--icon=audio-card", "--urgency=critical",
             "--action=fix=Restart the sound system", title, message],
            capture_output=True, text=True,
        ).stdout.strip()
    except OSError:
        return
    if chosen == "fix":
        restart_sound_system()


# PipeWire running out of open files (31 Oct 2026): the daemon hit its 1024
# soft limit 2 s after OBS started, ~18 h into uptime, and from then on could
# not set up links ("Too many open files", "can't import mem"). It kept
# running, silent, until a reboot. So the login service watches the count and
# warns well before the limit, with the same Fix button.

#: Warn when the daemon holds this share of its open-file limit.
HANDLES_WARN_SHARE = 0.8

#: How often the login service counts the daemon's open files.
HANDLES_CHECK_S = 60.0


def open_files(pid: int) -> list[str] | None:
    """What each of a process's open files points at (None if we cannot look)."""
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    targets = []
    for name in names:
        try:
            targets.append(os.readlink(fd_dir / name))
        except OSError:
            continue  # closed while we looked
    return targets


def open_file_limit(pid: int) -> int | None:
    """A process's soft limit on open files (None if unknown or unlimited)."""
    try:
        limits = Path(f"/proc/{pid}/limits").read_text()
    except OSError:
        return None
    for line in limits.splitlines():
        if line.startswith("Max open files"):
            soft = line[len("Max open files"):].split()[0]
            return int(soft) if soft.isdigit() else None
    return None


def handle_kinds(targets: Iterable[str]) -> dict[str, int]:
    """Open files counted by kind, so a log line shows which kind is growing."""
    kinds = {"memory": 0, "wakeup": 0, "socket": 0, "other": 0}
    for target in targets:
        if target.startswith("/memfd:") or target.endswith("(deleted)"):
            kinds["memory"] += 1
        elif target in ("anon_inode:[eventfd]", "anon_inode:[timerfd]"):
            kinds["wakeup"] += 1
        elif target.startswith("socket:"):
            kinds["socket"] += 1
        else:
            kinds["other"] += 1
    return kinds


class HandleNotifier:
    """For the login service: one warning per PipeWire daemon nearing its file limit.

    Call `check` every second; it counts at most every `HANDLES_CHECK_S`.
    """

    def __init__(self, log=print, clock=time.monotonic) -> None:
        self._log, self._clock = log, clock
        self._pid: int | None = None
        self._next = 0.0
        self._told: set[int] = set()

    def use_graph(self, graph: Graph) -> None:
        self._pid = graph.daemon_pid()

    def check(self) -> None:
        now = self._clock()
        if self._pid is None or now < self._next:
            return
        self._next = now + HANDLES_CHECK_S
        pid = self._pid
        targets, limit = open_files(pid), open_file_limit(pid)
        if targets is None or limit is None or pid in self._told:
            return
        if len(targets) < limit * HANDLES_WARN_SHARE:
            return
        self._told.add(pid)
        kinds = ", ".join(f"{k} {n}" for k, n in handle_kinds(targets).items())
        self._log(f"PipeWire holds {len(targets)} of {limit} open files ({kinds})")
        if shutil.which("notify-send"):
            message = (f"PipeWire has {len(targets)} of its {limit} open files in use. At the limit "
                       "every sound stops until it restarts. Restarting the sound system now "
                       "stops every sound for a few seconds.")
            threading.Thread(target=notify_with_fix, args=("Sound system nearly out of files", message),
                             name="handles-notice", daemon=True).start()


# -- self-check: every channel still wired (owner, 11 Nov 2026) ---------------
#
# The owner logged out and back in at 10:30 on 11 Nov with the sound system
# left running. WirePlumber re-created the sound card's nodes for the new
# login, and the Listener channel's echo canceller (its mic capture is
# dont-fallback) was left with nothing on its mic end: the wake word heard flat
# silence until a fader move restarted the channel at 17:19. A channel restart
# is make-before-break and measured gapless, so this one IS done automatically
# (unlike the whole-sound-system restart above, which stops every sound).

#: How often the login service looks, and how long a channel must stay broken
#: (two looks in a row) before it is restarted.
SELF_CHECK_S = 30.0
#: At most one repair per channel in this long, so something a restart cannot
#: fix (no default output for an echo canceller's reference) never loops.
SELF_CHECK_COOLDOWN_S = 600.0


def unwired_streams(slug: str, pid: int | None, graph: Graph) -> list[str]:
    """A channel's own streams that have nothing on their other end.

    Every stream a channel host creates (its capture from the mic, its
    playback into a device or another channel, an echo canceller's mic and
    reference) is linked while the channel works; one with no link at all
    passes no sound. Only the running host's nodes count: during a restart the
    old host's copies are on their way out.
    """
    linked: set[int] = set()
    for link in graph.links():
        linked.add(link.output_node)
        linked.add(link.input_node)
    found = []
    for node in graph.nodes:
        if node.owned_channel != slug or node.is_meter:
            continue
        if not (node.is_input_stream or node.is_output_stream):
            continue
        if pid is not None and graph.client_pid(node.client_id) != pid:
            continue
        if node.id not in linked:
            found.append(node.name)
    return sorted(found)


class SelfCheck:
    """For the login service: restart a channel whose own wiring has come undone.

    Call `use_graph` with every graph update and `check` every second; it looks
    every `SELF_CHECK_S`. `restart(slug)` does the restart (under whatever lock
    the caller's other config changes use).
    """

    def __init__(self, channels, restart, is_paused=lambda: False, log=print,
                 clock=time.monotonic, pid_of=None) -> None:
        self._channels = channels          # callable: the current channel list
        self._restart, self._paused, self._log, self._clock = restart, is_paused, log, clock
        self._pid_of = pid_of or (lambda channel: channel.pid())
        self._graph: Graph | None = None
        self._next = clock() + SELF_CHECK_S
        self._suspect: dict[str, list[str]] = {}
        self._repaired: dict[str, float] = {}

    def use_graph(self, graph: Graph) -> None:
        self._graph = graph

    def check(self) -> None:
        now = self._clock()
        if self._graph is None or now < self._next:
            return
        self._next = now + SELF_CHECK_S
        if self._paused():
            self._suspect.clear()
            return
        graph, suspect = self._graph, {}
        for channel in self._channels():
            if not channel.enabled:
                continue
            pid = self._pid_of(channel)
            if pid is None:
                continue
            loose = unwired_streams(channel.slug, pid, graph)
            if not loose:
                continue
            suspect[channel.slug] = loose
            if self._suspect.get(channel.slug) != loose:
                continue  # first sighting: links may be mid-change, look again next time
            if now - self._repaired.get(channel.slug, -1e12) < SELF_CHECK_COOLDOWN_S:
                continue
            self._repaired[channel.slug] = now
            self._log(f"self-check: {channel.name}: {', '.join(loose)} not connected; restarting it")
            try:
                self._restart(channel.slug)
            except Exception as exc:  # noqa: BLE001 - the service must keep running
                self._log(f"self-check: restarting {channel.name} failed: {exc}")
        self._suspect = suspect
