"""Pluggable wake actions. One task, one backend, one way to fire it.

``rtcwake`` is deliberately not in ``FIRE_BACKENDS``: a suspended machine
cannot run the scheduler loop that would fire it, so that backend is armed
locally at ``add`` time instead (see ``arm_rtcwake``) and never sits in
``pending`` waiting on a poll -- ``WakeDB.due()`` already excludes it.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any

from .config import WakeConfig
from .models import Task

REQUEST_TIMEOUT = 10.0
SHELL_TIMEOUT = 300.0
WOL_PORT = 9
DEFAULT_BROADCAST = "255.255.255.255"

_NON_HEX = re.compile(r"[^0-9a-fA-F]")


class BackendError(Exception):
    """Raised when a backend cannot fire (or arm) a task."""


def fire_shell(task: Task, config: WakeConfig) -> None:
    # A per-task timeout, because the default is sized for a quick command and
    # the jobs worth waking a machine for are not quick. It is a hard ceiling
    # either way: a hung task must not be able to hold the machine up forever.
    limit = task.timeout_seconds or SHELL_TIMEOUT
    try:
        result = subprocess.run(
            ["sh", "-c", task.task],
            capture_output=True,
            text=True,
            timeout=limit,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"command did not finish within {limit:.0f}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BackendError(f"command exited {result.returncode}: {detail or 'no output'}")


# -- wake-on-lan -------------------------------------------------------------
# The magic packet is built and sent here rather than shelled out to the
# `wakeonlan` binary, which is not installed on every box wake runs on (it is
# absent on archserver) and would be an unnecessary system-wide package for
# six 0xff bytes and sixteen copies of a MAC. Unprivileged: a broadcast UDP
# datagram needs SO_BROADCAST, not root.


def magic_packet(mac: str) -> bytes:
    cleaned = _NON_HEX.sub("", mac)
    if len(cleaned) != 12:
        raise BackendError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(cleaned) * 16


def fire_wol(task: Task, config: WakeConfig) -> None:
    if not task.target:
        raise BackendError("wol backend requires --target <mac-address>[@broadcast]")
    # `aa:bb:..@10.0.0.255` addresses a machine on another subnet by directed
    # broadcast; a bare MAC goes to the local broadcast address.
    mac, _, broadcast = task.target.partition("@")
    packet = magic_packet(mac)
    destination = broadcast or config.wol_broadcast or DEFAULT_BROADCAST
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(REQUEST_TIMEOUT)
            sock.sendto(packet, (destination, WOL_PORT))
    except OSError as exc:
        raise BackendError(f"could not send magic packet to {destination}: {exc}") from exc


# -- hotline-ios -------------------------------------------------------------


def _post_hotline(config: WakeConfig, path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not config.hotline_ios_url:
        raise BackendError("this backend needs WAKE_HOTLINE_IOS_URL configured")
    headers = {"Content-Type": "application/json"}
    if config.hotline_ios_key:
        headers["X-Hotline-Key"] = config.hotline_ios_key
    request = urllib.request.Request(
        f"{config.hotline_ios_url}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise BackendError(f"hotline-ios returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BackendError(f"could not reach hotline-ios: {exc.reason}") from exc
    return dict(payload) if isinstance(payload, dict) else {}


def fire_notify(task: Task, config: WakeConfig) -> None:
    """Leave a note in a hotline-ios channel. Does not ring anything."""
    _post_hotline(
        config,
        "/api/v1/say",
        {
            "text": f"wake: {task.task}",
            "agent": task.target or None,
            # hotline-ios stores client_token on the event row, so a task that
            # is retried after a timeout lands as the same note rather than two.
            "client_token": f"wake:{task.id}",
        },
    )


def fire_call(task: Task, config: WakeConfig) -> None:
    """Ring Bogdan's phone for real, and do not wait for him to answer.

    ``wait: false`` is the whole point. hotline-ios's ``/api/v1/call`` defaults
    to blocking for up to 900s while it waits for a typed reply in the app --
    which is right for `hotline-call`, and wrong here: the scheduler thread
    would sit on one alarm while every later task went unfired. A wake task
    rings and returns; the reply, if he sends one, lands in the conversation
    hotline-ios opened.
    """
    _post_hotline(
        config,
        "/api/v1/call",
        {
            "reason": task.task,
            "agent": task.target or None,
            "source": "wake",
            "wait": False,
            "client_token": f"wake:{task.id}",
        },
    )


FIRE_BACKENDS = {
    "shell": fire_shell,
    "wol": fire_wol,
    "notify": fire_notify,
    "call": fire_call,
}


def fire(task: Task, config: WakeConfig) -> None:
    handler = FIRE_BACKENDS.get(task.backend)
    if handler is None:
        if task.backend == "rtcwake":
            raise BackendError("rtcwake tasks are armed locally at add-time, not fired remotely")
        raise BackendError(f"unknown backend {task.backend!r}")
    handler(task, config)


def arm_rtcwake(task: Task) -> None:
    """Arm this machine's hardware RTC alarm for ``task.at``.

    Must run on the device that needs to wake itself -- there is no way for
    a remote server to reach into a suspended machine's hardware clock.
    """
    binary = shutil.which("rtcwake")
    if binary is None:
        raise BackendError("rtcwake is not installed")
    result = subprocess.run(
        [binary, "-m", "no", "-t", str(int(task.at))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BackendError(f"rtcwake exited {result.returncode}: {detail or 'no output'}")
