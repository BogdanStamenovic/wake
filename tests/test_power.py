"""The shutdown guard, and the alarm that brings the machine back.

These are the tests for the part that runs with nobody watching. The guard's
job is to say *no* -- to a person at the keyboard, to work still in flight --
so most of what is below is a case where powering off must not happen.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from wake import power
from wake.power import Presence


def _processes(*entries: tuple[int, str, str]) -> list[tuple[int, str, str]]:
    return list(entries)


# -- what counts as an agent ------------------------------------------------


def test_only_argv0_claude_counts_as_an_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this exists for: on this host, agent command lines *are* prompts.

    A shell running a script that merely mentions claude matched a
    command-line substring search -- including the script doing the search --
    so every shutdown was blocked by the checker itself.
    """
    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (10, "zsh", "/usr/bin/zsh -c 'echo claude --name pretend-agent'"),
        (11, "python3", "python3 -c 'print(\"claude --name also-not-real\")'"),
    ))
    assert power.foreign_agents(allow=set()) == []


def test_a_real_agent_is_found_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (20, "claude", "/opt/claude-code/bin/claude --model x --name track-dev do a thing"),
    ))
    assert power.foreign_agents(allow=set()) == ["track-dev"]
    assert power.foreign_agents(allow={"track-dev"}) == []


def test_an_agent_with_no_name_flag_is_still_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unnamed session must not vanish just because it is hard to label."""
    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (30, "claude", "/opt/claude-code/bin/claude --permission-mode bypassPermissions hello"),
    ))
    assert power.foreign_agents(allow=set()) == ["unnamed pid 30"]


def test_the_operator_is_excused_by_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """It has no --name to allow-list: the watchdog starts it bare and it
    adopts its identity from the registry at runtime. Since that watchdog
    respawns it two minutes after every boot, counting it as work means the
    machine can never power itself off."""
    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (40, "claude", "/opt/claude-code/bin/claude --permission-mode x You are hotline's OPERATOR."),
        (41, "claude", "/opt/claude-code/bin/claude --name track-dev real work"),
    ))
    assert power.foreign_agents(allow=set(), allow_match="hotline's OPERATOR") == ["track-dev"]


def test_the_checking_process_never_counts_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (os.getpid(), "claude", "/opt/claude-code/bin/claude --name me"),
    ))
    assert power.foreign_agents(allow=set()) == []


# -- what counts as a person ------------------------------------------------


def test_a_manager_session_is_not_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """The always-present logind row on this box is Class=manager, the user
    manager itself. Counting rows rather than reading Class blocks forever."""
    def fake_run(command: list[str], timeout: float = 10.0) -> str:
        if command[:2] == ["loginctl", "list-sessions"]:
            return "1 1000 bodas - 580 manager\n"
        if command[:2] == ["loginctl", "show-session"]:
            return "Class=manager\nType=unspecified\nRemote=no\n"
        return ""

    monkeypatch.setattr(power, "_run", fake_run)
    monkeypatch.setattr(power, "_processes", list)
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)
    assert power.human_signals() == []


def test_a_real_login_is_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], timeout: float = 10.0) -> str:
        if command[:2] == ["loginctl", "list-sessions"]:
            return "7 1000 bodas seat0 900 user\n"
        if command[:2] == ["loginctl", "show-session"]:
            return "Class=user\nType=tty\nRemote=no\n"
        return ""

    monkeypatch.setattr(power, "_run", fake_run)
    monkeypatch.setattr(power, "_processes", list)
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)
    assert power.human_signals() == ["logind session 7 (tty)"]


def test_an_attached_tmux_client_is_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """A detached session is how every agent here runs; an attached client is
    somebody actually looking at it."""
    def fake_run(command: list[str], timeout: float = 10.0) -> str:
        if command[:2] == ["tmux", "list-clients"]:
            return "/dev/pts/5: guardprobe [80x24] (attached)\n"
        return ""

    monkeypatch.setattr(power, "_run", fake_run)
    monkeypatch.setattr(power, "_processes", list)
    monkeypatch.setattr(power.shutil, "which", lambda _name: "/usr/bin/tmux")
    assert power.human_signals() == ["1 attached tmux client(s)"]


def test_an_ssh_login_is_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "_run", lambda *a, **k: "")
    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (50, "sshd", "sshd: bodas@pts/2"),
    ))
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)
    assert power.human_signals() == ["an ssh login"]


def test_sshd_listener_and_privsep_are_not_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "_run", lambda *a, **k: "")
    monkeypatch.setattr(power, "_processes", lambda: _processes(
        (60, "sshd", "sshd: /usr/bin/sshd -D [listener] 0 of 10-100 startups"),
        (61, "sshd", "sshd: bodas [priv]"),
    ))
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)
    assert power.human_signals() == []


# -- the verdict ------------------------------------------------------------


def test_presence_is_clear_only_when_both_halves_are() -> None:
    assert Presence().clear
    assert not Presence(humans=["someone"]).clear
    assert not Presence(agents=["track-dev"]).clear


def test_why_names_what_is_holding_the_machine_up() -> None:
    reason = Presence(humans=["an ssh login"], agents=["track-dev"]).why()
    assert "ssh login" in reason
    assert "track-dev" in reason


# -- the alarm --------------------------------------------------------------


def test_refuses_to_arm_a_wake_that_is_too_soon() -> None:
    """The shape that turns one failed run into a boot loop."""
    now = time.time()
    with pytest.raises(power.PowerError, match="boot loop"):
        power.arm_wakealarm(now + 30, now=now)


def test_the_minimum_lead_is_a_real_gap() -> None:
    assert power.MIN_WAKE_LEAD_SECONDS >= 300


def test_arming_writes_zero_first_then_the_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Zero first is required, not tidiness: the kernel refuses to set a new
    alarm over an armed one and the failure is a quiet EBUSY."""
    written: list[str] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **kwargs: Any) -> _Ok:
        written.append(command[-1])
        return _Ok()

    monkeypatch.setattr(power.subprocess, "run", fake_run)
    monkeypatch.setattr(power, "RTC_WAKEALARM", tmp_path / "wakealarm")
    (tmp_path / "wakealarm").write_text("0")
    now = time.time()
    power.arm_wakealarm(now + 3600, now=now)

    assert len(written) == 2
    assert written[0].startswith("echo 0 >")
    assert f"echo {int(now + 3600)} >" in written[1]
