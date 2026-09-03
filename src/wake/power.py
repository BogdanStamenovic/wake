"""Powering this machine off after a task, and arming its way back on.

wake already owns getting a machine *up* at a time -- wol, rtcwake. This is the
symmetric half: putting it back down once the work it woke for is done, and
arming its own alarm before it goes, so it can return without anyone's help.

The presence guard is the delicate part. Its job is to stop the box powering
off while a person is using it or while real work is in flight -- not to notice
the permanent background furniture of an agent host. On this machine that
distinction is not academic: ``who`` reports four logged-in pts at all times,
every one of them a detached tmux pane belonging to an agent, so the obvious
check is true forever and the box would never power off. Furniture is not work.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

RTC_WAKEALARM = Path("/sys/class/rtc/rtc0/wakealarm")
# A wake armed closer than this is treated as a mistake rather than an
# instruction: it is the shape that turns one failed run into a boot loop,
# waking the machine every few minutes all night.
MIN_WAKE_LEAD_SECONDS = 600.0
# How far before a task the RTC is armed. Waking *at* the task's time means it
# fires on a machine that is still bringing up its network, which is exactly
# the state a job doing real web work should not start in. The primary path
# has the same margin by construction: the server's magic packet is scheduled
# ahead of the task rather than alongside it.
WAKE_LEAD_SECONDS = 300.0
AGENT_NAME_FLAG = re.compile(r"--name[= ]+(\S+)")


class PowerError(Exception):
    """Raised when a power action cannot be carried out."""


@dataclass
class Presence:
    """Why the machine may or may not be powered off right now."""

    humans: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)

    @property
    def clear(self) -> bool:
        return not self.humans and not self.agents

    def why(self) -> str:
        parts = []
        if self.humans:
            parts.append("someone is using this machine: " + ", ".join(self.humans))
        if self.agents:
            parts.append("work in flight: " + ", ".join(self.agents))
        return "; ".join(parts) or "nothing is holding the machine up"


def _processes() -> list[tuple[int, str, str]]:
    """(pid, argv0 basename, full command line) for every process.

    Matching on the *whole* command line is not usable on this host. Agent
    sessions carry their entire tasking prompt in argv, so any shell running a
    script that merely mentions claude, or sshd, matches a substring search --
    including the script doing the matching. Every check below keys on argv[0]
    instead, and only then reads the rest.
    """
    found = []
    for line in _run(["ps", "-eo", "pid=,args="]).splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, args = line.partition(" ")
        if not pid_text.isdigit() or not args.strip():
            continue
        found.append((int(pid_text), os.path.basename(args.split()[0]), args))
    return found


def _run(command: list[str], timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def human_signals() -> list[str]:
    """Evidence that a person, not an agent, is at or connected to this box.

    Deliberately not ``who``/``w``: see the module docstring. Each signal below
    was checked to be *false* on this host while four agents were running, so
    each one can actually say no.
    """
    found = []

    # A real login -- console, X, wayland, or remote -- gets a logind session
    # of Class=user. The always-present row is Class=manager, the user manager
    # itself, which is not a person.
    for line in _run(["loginctl", "list-sessions", "--no-legend"]).splitlines():
        session = line.split()[0] if line.split() else ""
        if not session:
            continue
        detail = dict(
            item.split("=", 1)
            for item in _run(
                ["loginctl", "show-session", session, "-p", "Class", "-p", "Type", "-p", "Remote"]
            ).splitlines()
            if "=" in item
        )
        if detail.get("Class") != "user":
            continue
        if detail.get("Type") in ("tty", "x11", "wayland", "mir") or detail.get("Remote") == "yes":
            found.append(f"logind session {session} ({detail.get('Type', '?')})")

    # An attached tmux client means a person is looking at a pane right now.
    # Detached sessions are how every agent here runs, and are not presence.
    if shutil.which("tmux"):
        clients = [line for line in _run(["tmux", "list-clients"]).splitlines() if line.strip()]
        if clients:
            found.append(f"{len(clients)} attached tmux client(s)")

    # An interactive ssh login, identified by the session process sshd forks
    # per connection ("sshd: bodas@pts/2"), not by counting pts rows -- tmux
    # produces those too.
    for _pid, argv0, args in _processes():
        if argv0 != "sshd" and not argv0.startswith("sshd:"):
            continue
        if "[priv]" in args or "listener" in args or "@notty" in args:
            continue
        if "@" in args and "pts/" in args:
            found.append("an ssh login")
            break

    return found


def foreign_agents(*, allow: set[str], allow_match: str = "") -> list[str]:
    """Agent sessions other than the ones this machine always has anyway.

    Two ways to be furniture rather than work, because one is not enough here.

    ``allow`` matches on the session's ``--name``. That covers ordinary agents.
    It does *not* cover the operator: hotline-watchdog.timer starts it with no
    ``--name`` at all, because its identity is adopted at runtime from the
    hotline registry rather than passed in argv. Since the watchdog respawns it
    within two minutes of every boot, an operator counted as work means the
    machine can never power itself off -- so ``allow_match``, a regex against
    the whole command line, exists to name it.

    Both are configuration rather than built-in knowledge: wake schedules
    things and should not carry a list of which agent framework's processes are
    load-bearing on someone's desktop.
    """
    mine = os.getpid()
    pattern = re.compile(allow_match) if allow_match else None
    names = []
    for pid, argv0, args in _processes():
        if argv0 != "claude" or pid == mine:
            continue
        if pattern is not None and pattern.search(args):
            continue
        match = AGENT_NAME_FLAG.search(args)
        name = match.group(1) if match else f"unnamed pid {pid}"
        if name not in allow:
            names.append(name)
    return sorted(set(names))


def presence(*, allow_agents: set[str] | None = None, allow_match: str = "") -> Presence:
    return Presence(
        humans=human_signals(),
        agents=foreign_agents(allow=set(allow_agents or set()), allow_match=allow_match),
    )


# -- the way back on ---------------------------------------------------------


def arm_wakealarm(at: float, *, now: float | None = None) -> None:
    """Arm the hardware RTC to power this machine on at ``at``.

    This is the backup path, and it is the one that works when the network
    does not: it needs neither the wake server nor the LAN, only the clock on
    this motherboard. The primary path is a magic packet from the server.

    Writing 0 first is required, not tidiness -- the kernel refuses to set a
    new alarm over an armed one, and the failure is a quiet EBUSY.
    """
    moment = time.time() if now is None else now
    if at - moment < MIN_WAKE_LEAD_SECONDS:
        raise PowerError(
            f"refusing to arm a wake {at - moment:.0f}s out; "
            f"less than {MIN_WAKE_LEAD_SECONDS:.0f}s is how a boot loop starts"
        )
    if not RTC_WAKEALARM.exists():
        raise PowerError(f"{RTC_WAKEALARM} does not exist on this machine")
    for value in ("0", str(int(at))):
        result = subprocess.run(
            ["sudo", "-n", "sh", "-c", f"echo {value} > {RTC_WAKEALARM}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PowerError(f"could not write {value} to the wakealarm: {detail or 'no output'}")


def clear_wakealarm() -> bool:
    """Disarm the RTC. Called when the machine starts, not when it stops.

    Tomorrow both wake paths are armed at once -- a magic packet from the
    server and this machine's own alarm -- because either can fail. Whichever
    fires first, the machine is up and the other is now a leftover: an alarm
    still set in hardware will power the box on again after the *next*
    shutdown, at a time nobody chose. Measured here: a WoL wake at 20:44:58
    left an alarm armed for 20:49:15 with nothing to clear it.
    """
    if not RTC_WAKEALARM.exists():
        return False
    result = subprocess.run(
        ["sudo", "-n", "sh", "-c", f"echo 0 > {RTC_WAKEALARM}"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    return result.returncode == 0


def read_wakealarm() -> int | None:
    try:
        raw = RTC_WAKEALARM.read_text().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


# -- going down --------------------------------------------------------------


def suppress_watchdog() -> bool:
    """Stop the timer that respawns the operator, for the shutdown window only.

    It stays *enabled*, so it comes back on the next boot; this only prevents it
    firing between the guard check and the actual poweroff, where a fresh
    session would otherwise appear and there would be nothing to notice it.
    """
    result = subprocess.run(
        ["systemctl", "--user", "stop", "hotline-watchdog.timer"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result.returncode == 0


def power_off() -> None:
    result = subprocess.run(
        ["systemctl", "poweroff"], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PowerError(f"poweroff failed: {detail or 'no output'}")


def this_session_name() -> str:
    """The agent name of the session running this process, if it has one."""
    return os.environ.get("CLAUDE_AGENT_NAME", "")
