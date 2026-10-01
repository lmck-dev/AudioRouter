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
        try:
            chosen = subprocess.run(
                ["notify-send", "--app-name=Audio Router", "--icon=audio-card", "--urgency=critical",
                 "--action=fix=Restart the sound system", "Echo cancellation stopped working", MESSAGE],
                capture_output=True, text=True,
            ).stdout.strip()
        except OSError:
            return
        if chosen == "fix":
            restart_sound_system()
