"""Device <-> server sync, exercised over a real socket.

These run an actual ``WakeServer`` on a loopback port rather than stubbing
``urllib``: the thing most likely to be wrong here is the wire contract
between ``syncclient`` and ``server``, and a stub is exactly the layer that
would agree with itself while the real pair disagreed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wake.config import WakeConfig
from wake.db import WakeDB
from wake.server import WakeServer
from wake.syncclient import SyncError, pull, push, sync


@pytest.fixture
def server_db(tmp_path: Path) -> Iterator[WakeDB]:
    with WakeDB(tmp_path / "server.db") as handle:
        yield handle


@pytest.fixture
def live_server(server_db: WakeDB) -> Iterator[WakeConfig]:
    """A running server, and the device-side config that points at it."""
    server_config = WakeConfig(
        role="server", origin="server", bind="127.0.0.1", port=0, poll_seconds=3600
    )
    server = WakeServer(server_db, server_config)
    thread = threading.Thread(target=server.httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield WakeConfig(
            role="device",
            origin="laptop",
            server_url=f"http://127.0.0.1:{server.port}",
        )
    finally:
        server.httpd.shutdown()
        thread.join(timeout=5)
        server.close()


@pytest.fixture
def device_db(tmp_path: Path) -> Iterator[WakeDB]:
    with WakeDB(tmp_path / "device.db") as handle:
        yield handle


def test_a_task_written_on_the_device_reaches_the_server(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    task = device_db.add(
        task="ring me", at=42.0, backend="call", target=None, origin="laptop"
    )
    assert push(device_db, live_server) == 1
    landed = server_db.get(task.id)
    assert landed is not None
    assert landed.task == "ring me"
    assert landed.origin == "laptop"


def test_pushing_twice_sends_the_row_once(
    device_db: WakeDB, live_server: WakeConfig
) -> None:
    device_db.add(task="once", at=1.0, backend="shell", target=None, origin="laptop")
    assert push(device_db, live_server) == 1
    assert push(device_db, live_server) == 0


def test_the_server_firing_a_task_comes_back_on_the_next_pull(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    task = device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
    sync(device_db, live_server)
    server_db.mark_fired(task.id)

    assert pull(device_db, live_server) == 1
    local = device_db.get(task.id)
    assert local is not None
    assert local.status == "fired"


def test_a_status_pulled_from_the_server_is_not_pushed_back(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    """The echo bug a global push watermark cannot avoid."""
    task = device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
    sync(device_db, live_server)
    server_db.mark_fired(task.id)
    sync(device_db, live_server)

    pushed, pulled = sync(device_db, live_server)
    assert (pushed, pulled) == (0, 0), "sync must reach a fixed point"


def test_a_task_added_during_a_sync_is_not_skipped(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    """The lost-write bug a global push watermark cannot avoid.

    The row is created after the push half has run, so a watermark advanced to
    "the revision the pull left behind" would step straight over it.
    """
    device_db.add(task="first", at=1.0, backend="shell", target=None, origin="laptop")
    push(device_db, live_server)
    racer = device_db.add(task="raced", at=1.0, backend="shell", target=None, origin="laptop")
    pull(device_db, live_server)

    assert push(device_db, live_server) == 1
    assert server_db.get(racer.id) is not None


def test_cancelling_locally_propagates_up(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    task = device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
    sync(device_db, live_server)
    device_db.cancel(task.id)
    sync(device_db, live_server)

    landed = server_db.get(task.id)
    assert landed is not None
    assert landed.status == "cancelled"


def test_a_second_device_sees_the_first_ones_tasks(
    device_db: WakeDB, tmp_path: Path, live_server: WakeConfig
) -> None:
    device_db.add(task="from laptop", at=1.0, backend="shell", target=None, origin="laptop")
    push(device_db, live_server)

    other_config = WakeConfig(
        role="device", origin="phone", server_url=live_server.server_url
    )
    with WakeDB(tmp_path / "phone.db") as phone:
        assert pull(phone, other_config) == 1
        assert [t.task for t in phone.tasks()] == ["from laptop"]


def test_the_second_device_does_not_claim_the_first_ones_row(
    device_db: WakeDB, tmp_path: Path, live_server: WakeConfig
) -> None:
    device_db.add(task="from laptop", at=1.0, backend="shell", target=None, origin="laptop")
    push(device_db, live_server)
    other_config = WakeConfig(
        role="device", origin="phone", server_url=live_server.server_url
    )
    with WakeDB(tmp_path / "phone.db") as phone:
        pull(phone, other_config)
        assert phone.unpushed("phone") == [], "a relayed row is not the phone's to push"


def test_owner_survives_the_round_trip(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    task = device_db.add(
        task="local job", at=1.0, backend="shell", target=None, origin="laptop", owner="laptop"
    )
    push(device_db, live_server)
    landed = server_db.get(task.id)
    assert landed is not None
    assert landed.owner == "laptop"


def test_the_pull_cursor_only_moves_forward(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    server_db.add(task="a", at=1.0, backend="shell", target=None, origin="server")
    assert pull(device_db, live_server) == 1
    assert pull(device_db, live_server) == 0
    server_db.add(task="b", at=1.0, backend="shell", target=None, origin="server")
    assert pull(device_db, live_server) == 1


# -- failure modes ----------------------------------------------------------


def test_no_server_configured_is_an_error_not_a_crash(device_db: WakeDB) -> None:
    device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
    with pytest.raises(SyncError, match="no server configured"):
        sync(device_db, WakeConfig(role="device", origin="laptop"))


def test_an_unreachable_server_raises_syncerror(device_db: WakeDB) -> None:
    config = WakeConfig(role="device", origin="laptop", server_url="http://127.0.0.1:1")
    device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
    with pytest.raises(SyncError, match="could not reach"):
        push(device_db, config)


def test_a_failed_push_leaves_the_row_unpushed(device_db: WakeDB) -> None:
    config = WakeConfig(role="device", origin="laptop", server_url="http://127.0.0.1:1")
    device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
    with pytest.raises(SyncError):
        push(device_db, config)
    assert len(device_db.unpushed("laptop")) == 1, "the next run must retry it"


def test_a_wrong_api_key_is_refused(
    device_db: WakeDB, server_db: WakeDB, tmp_path: Path
) -> None:
    server_config = WakeConfig(
        role="server", origin="server", bind="127.0.0.1", port=0,
        poll_seconds=3600, api_key="correct-horse",
    )
    server = WakeServer(server_db, server_config)
    thread = threading.Thread(target=server.httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        device_db.add(task="x", at=1.0, backend="shell", target=None, origin="laptop")
        bad = WakeConfig(
            role="device", origin="laptop",
            server_url=f"http://127.0.0.1:{server.port}", api_key="wrong",
        )
        with pytest.raises(SyncError, match="401"):
            push(device_db, bad)

        good = WakeConfig(
            role="device", origin="laptop",
            server_url=f"http://127.0.0.1:{server.port}", api_key="correct-horse",
        )
        assert push(device_db, good) == 1
    finally:
        server.httpd.shutdown()
        thread.join(timeout=5)
        server.close()


# -- claims the docstrings make, which nothing was asserting -----------------


def test_sync_pushes_before_it_pulls(
    device_db: WakeDB, live_server: WakeConfig
) -> None:
    """Ordering, pinned.

    Push first so a row written a moment ago is on the server before this
    device asks what the server knows. Pulling first leaves the just-created
    row outside the pulled revision, so the row comes back on the *following*
    cycle and sync takes two cycles to settle instead of one.

    The first cycle returns (1, 1), not (1, 0): the pull legitimately hands
    back the row this device just pushed, because pushing advanced the
    server's revision past the device's pull cursor. merge discards it as a
    tie and nothing is written, so the count is the only trace.
    """
    device_db.add(task="just written", at=1.0, backend="shell", target=None, origin="laptop")

    pushed, _ = sync(device_db, live_server)
    assert pushed == 1, "the local row must go up on this cycle, before the pull"
    assert sync(device_db, live_server) == (0, 0), "one cycle must reach a fixed point"


class _FlakyHandler(BaseHTTPRequestHandler):
    """Accepts `accept_first` task posts, then fails everything after."""

    accept_first: ClassVar[int] = 1
    accepted: ClassVar[list[str]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if len(_FlakyHandler.accepted) < _FlakyHandler.accept_first:
            _FlakyHandler.accepted.append(str(body.get("id", "")))
            payload, status = json.dumps(body).encode(), 200
        else:
            payload, status = b'{"error": "gone away"}', 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_a_push_that_dies_halfway_resumes_rather_than_restarts(
    device_db: WakeDB,
) -> None:
    """Each row is acknowledged individually, so a server that goes away
    mid-push leaves what it accepted marked pushed and the rest pending."""
    _FlakyHandler.accepted = []
    _FlakyHandler.accept_first = 1
    httpd = HTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        config = WakeConfig(
            role="device", origin="laptop",
            server_url=f"http://127.0.0.1:{httpd.server_address[1]}",
        )
        for name in ("a", "b", "c"):
            device_db.add(
                task=name, at=1.0, backend="shell", target=None, origin="laptop", id=name
            )

        with pytest.raises(SyncError, match="500"):
            push(device_db, config)

        remaining = [t.id for t in device_db.unpushed("laptop")]
        assert _FlakyHandler.accepted == ["a"]
        assert remaining == ["b", "c"], "only the unacknowledged rows may retry"

        # The server comes back: the next run sends two, not three.
        _FlakyHandler.accept_first = 99
        assert push(device_db, config) == 2
        assert _FlakyHandler.accepted == ["a", "b", "c"]
        assert device_db.unpushed("laptop") == []
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_a_recurring_task_pushed_up_keeps_its_period(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    """The add route rebuilds the row by hand; every field it forgot was dropped."""
    task = device_db.add(
        task="morning", at=1.0, backend="shell", target=None, origin="laptop",
        repeat_seconds=86400.0, then_do="poweroff", timeout_seconds=1200.0,
    )
    assert push(device_db, live_server) == 1
    landed = server_db.get(task.id)
    assert landed is not None
    assert landed.repeat_seconds == 86400.0
    assert landed.then_do == "poweroff", "a device's --then reached the server as nothing"
    assert landed.timeout_seconds == 1200.0


def test_a_recurrence_the_server_advanced_comes_back_down(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    task = device_db.add(
        task="morning", at=1.0, backend="shell", target=None, origin="laptop",
        repeat_seconds=86400.0,
    )
    sync(device_db, live_server)
    server_db.mark_recurred(task.id, at=86401.0, fired_at=1.0)

    assert pull(device_db, live_server) == 1
    local = device_db.get(task.id)
    assert local is not None
    assert local.at == 86401.0
    assert local.status == "pending"
    assert sync(device_db, live_server) == (0, 0), "and does not bounce straight back up"


def test_a_cancel_is_not_undone_by_a_late_re_arm(
    device_db: WakeDB, server_db: WakeDB, live_server: WakeConfig
) -> None:
    """The device stops a recurrence; the server's own advance must not revive it."""
    task = device_db.add(
        task="morning", at=1.0, backend="shell", target=None, origin="laptop",
        repeat_seconds=86400.0,
    )
    sync(device_db, live_server)
    before = server_db.get(task.id)
    assert before is not None

    device_db.cancel(task.id)
    sync(device_db, live_server)
    # The server was mid-run when the cancel landed: its bookkeeping is a
    # compare-and-set against the revision it read before starting.
    assert server_db.mark_recurred(
        task.id, at=86401.0, fired_at=1.0, expect_rev=before.rev
    ) is None

    landed = server_db.get(task.id)
    assert landed is not None and landed.status == "cancelled"
