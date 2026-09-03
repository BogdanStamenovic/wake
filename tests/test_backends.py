"""Each backend, fired for real against something that can observe it.

The wol test opens an actual UDP socket and reads the packet back; the
hotline-ios tests run a throwaway HTTP server and assert on the JSON that
arrives. Nothing here asserts that wake *called* a function -- only that the
bytes on the wire are the ones the other side documents.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest

from wake.backends import (
    BackendError,
    fire,
    fire_call,
    fire_notify,
    fire_shell,
    fire_wol,
    magic_packet,
)
from wake.config import WakeConfig
from wake.models import Task


def make_task(**overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": "t1", "task": "true", "at": 1.0, "backend": "shell", "target": None,
        "status": "pending", "origin": "testbox", "created_at": 1.0,
        "updated_at": 1.0, "rev": 1,
    }
    fields.update(overrides)
    return Task(**fields)


# -- shell ------------------------------------------------------------------


def test_shell_runs_the_command(tmp_path: Any) -> None:
    marker = tmp_path / "ran"
    fire_shell(make_task(task=f"touch {marker}"), WakeConfig())
    assert marker.exists()


def test_shell_reports_the_exit_code_and_stderr() -> None:
    with pytest.raises(BackendError, match="exited 3"):
        fire_shell(make_task(task="echo boom >&2; exit 3"), WakeConfig())


def test_shell_surfaces_the_error_text() -> None:
    with pytest.raises(BackendError, match="boom"):
        fire_shell(make_task(task="echo boom >&2; exit 1"), WakeConfig())


# -- wake-on-lan ------------------------------------------------------------


def test_magic_packet_is_six_ff_bytes_then_the_mac_sixteen_times() -> None:
    packet = magic_packet("a8:a1:59:fd:4d:13")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("a8a159fd4d13") * 16


@pytest.mark.parametrize("form", ["a8:a1:59:fd:4d:13", "a8-a1-59-fd-4d-13", "a8a159fd4d13"])
def test_magic_packet_accepts_the_usual_mac_spellings(form: str) -> None:
    assert magic_packet(form) == magic_packet("a8:a1:59:fd:4d:13")


@pytest.mark.parametrize("bad", ["", "not-a-mac", "a8:a1:59:fd:4d", "a8:a1:59:fd:4d:13:99"])
def test_magic_packet_refuses_anything_that_is_not_a_mac(bad: str) -> None:
    with pytest.raises(BackendError, match="not a MAC address"):
        magic_packet(bad)


def test_wol_sends_the_packet_over_a_real_socket() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    port = listener.getsockname()[1]
    try:
        # The port is fixed at 9 in the backend, so aim the datagram at the
        # listener by monkeypatching the constant rather than the socket.
        from wake import backends

        original = backends.WOL_PORT
        backends.WOL_PORT = port
        try:
            fire_wol(make_task(target="a8:a1:59:fd:4d:13@127.0.0.1"), WakeConfig())
            data, _ = listener.recvfrom(200)
        finally:
            backends.WOL_PORT = original
    finally:
        listener.close()
    assert data == magic_packet("a8:a1:59:fd:4d:13")


def test_wol_without_a_target_is_an_error() -> None:
    with pytest.raises(BackendError, match="requires --target"):
        fire_wol(make_task(backend="wol", target=None), WakeConfig())


# -- hotline-ios ------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    # Class-level on purpose: BaseHTTPRequestHandler is instantiated fresh per
    # request, so there is nowhere else for a test to read the calls back from.
    received: ClassVar[list[tuple[str, dict[str, Any], dict[str, str]]]] = []
    status: ClassVar[int] = 200

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        headers = {k.lower(): v for k, v in self.headers.items()}
        _Recorder.received.append((self.path, body, headers))
        payload = json.dumps({"ok": True}).encode()
        self.send_response(_Recorder.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_hotline() -> Iterator[WakeConfig]:
    _Recorder.received = []
    _Recorder.status = 200
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield WakeConfig(
            hotline_ios_url=f"http://127.0.0.1:{httpd.server_address[1]}",
            hotline_ios_key="secret",
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_notify_posts_to_say_with_the_api_key(fake_hotline: WakeConfig) -> None:
    fire_notify(make_task(task="check the deploy", target="hotline-80"), fake_hotline)
    path, body, headers = _Recorder.received[0]
    assert path == "/api/v1/say"
    assert body["text"] == "wake: check the deploy"
    assert body["agent"] == "hotline-80"
    assert headers["x-hotline-key"] == "secret"


def test_notify_carries_a_stable_idempotency_token(fake_hotline: WakeConfig) -> None:
    fire_notify(make_task(id="abc123"), fake_hotline)
    fire_notify(make_task(id="abc123"), fake_hotline)
    tokens = {body["client_token"] for _, body, _ in _Recorder.received}
    assert tokens == {"wake:abc123"}


def test_call_rings_without_waiting_for_an_answer(fake_hotline: WakeConfig) -> None:
    """wait=false is load-bearing: hotline-ios blocks for 900s by default."""
    fire_call(make_task(task="time to get up"), fake_hotline)
    path, body, _ = _Recorder.received[0]
    assert path == "/api/v1/call"
    assert body["reason"] == "time to get up"
    assert body["wait"] is False
    assert body["source"] == "wake"


def test_a_hotline_error_becomes_a_backend_error(fake_hotline: WakeConfig) -> None:
    _Recorder.status = 500
    with pytest.raises(BackendError, match="returned 500"):
        fire_notify(make_task(), fake_hotline)


def test_unconfigured_hotline_is_an_error_not_a_silent_no_op() -> None:
    with pytest.raises(BackendError, match="WAKE_HOTLINE_IOS_URL"):
        fire_notify(make_task(), WakeConfig())


def test_unreachable_hotline_is_an_error() -> None:
    config = WakeConfig(hotline_ios_url="http://127.0.0.1:1")
    with pytest.raises(BackendError, match="could not reach"):
        fire_call(make_task(), config)


# -- dispatch ---------------------------------------------------------------


def test_rtcwake_is_never_dispatched_remotely() -> None:
    with pytest.raises(BackendError, match="armed locally"):
        fire(make_task(backend="rtcwake"), WakeConfig())


def test_an_unknown_backend_is_an_error() -> None:
    with pytest.raises(BackendError, match="unknown backend"):
        fire(make_task(backend="telepathy"), WakeConfig())
