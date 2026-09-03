"""Device <-> server sync, exercised over a real socket.

These run an actual ``WakeServer`` on a loopback port rather than stubbing
``urllib``: the thing most likely to be wrong here is the wire contract
between ``syncclient`` and ``server``, and a stub is exactly the layer that
would agree with itself while the real pair disagreed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

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
