"""The HTTP surface and the firing loop, at the service layer and over a socket."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wake import power
from wake.config import WakeConfig
from wake.db import WakeDB
from wake.server import ApiError, WakeServer, WakeService, owned_by, run_once


@pytest.fixture
def service(db: WakeDB) -> WakeService:
    return WakeService(db, WakeConfig(role="server", origin="server"))


# The firing loop under test is the *server's*, so it gets a server config: a
# server owns both "" and its own ORIGIN, and running these against the default
# device role would exercise a machine that owns neither.
SERVER = WakeConfig(role="server", origin="hub")

# -- run_once ---------------------------------------------------------------


def test_run_once_fires_a_due_task_and_marks_it(db: WakeDB, tmp_path: Path) -> None:
    marker = tmp_path / "fired"
    task = db.add(
        task=f"touch {marker}", at=100.0, backend="shell", target=None, origin="server"
    )
    handled = run_once(db, SERVER, now=500.0)

    assert [t.id for t in handled] == [task.id]
    assert marker.exists()
    stored = db.get(task.id)
    assert stored is not None
    assert stored.status == "fired"
    assert stored.fired_at is not None


def test_run_once_leaves_a_task_whose_time_has_not_come(db: WakeDB) -> None:
    db.add(task="true", at=9999.0, backend="shell", target=None, origin="server")
    assert run_once(db, SERVER, now=500.0) == []


def test_run_once_records_a_failure_instead_of_raising(db: WakeDB) -> None:
    task = db.add(task="exit 9", at=1.0, backend="shell", target=None, origin="server")
    run_once(db, SERVER, now=500.0)
    stored = db.get(task.id)
    assert stored is not None
    assert stored.status == "failed"
    assert "exited 9" in (stored.error or "")


def test_one_failing_task_does_not_stop_the_others(db: WakeDB, tmp_path: Path) -> None:
    marker = tmp_path / "later"
    db.add(task="exit 1", at=1.0, backend="shell", target=None, origin="server")
    db.add(task=f"touch {marker}", at=2.0, backend="shell", target=None, origin="server")
    run_once(db, SERVER, now=500.0)
    assert marker.exists()


def test_a_fired_task_is_not_fired_again(db: WakeDB, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    db.add(
        task=f"echo x >> {counter}", at=1.0, backend="shell", target=None, origin="server"
    )
    run_once(db, SERVER, now=500.0)
    run_once(db, SERVER, now=500.0)
    assert counter.read_text() == "x\n"


def test_the_server_ignores_device_owned_tasks(db: WakeDB, tmp_path: Path) -> None:
    marker = tmp_path / "laptops"
    db.add(
        task=f"touch {marker}", at=1.0, backend="shell", target=None,
        origin="laptop", owner="laptop",
    )
    assert run_once(db, SERVER, now=500.0) == []
    assert not marker.exists()


# -- the service layer ------------------------------------------------------


def test_add_requires_a_task_and_a_time(service: WakeService) -> None:
    with pytest.raises(ApiError) as info:
        service.add({"at": 1.0})
    assert info.value.status == 400
    with pytest.raises(ApiError):
        service.add({"task": "x"})


def test_add_rejects_an_unknown_backend_as_a_400(service: WakeService) -> None:
    with pytest.raises(ApiError) as info:
        service.add({"task": "x", "at": 1.0, "backend": "telepathy"})
    assert info.value.status == 400


def test_add_with_an_id_is_idempotent(service: WakeService) -> None:
    first = service.add({"task": "x", "at": 1.0, "id": "fixed"})
    second = service.add({"task": "x", "at": 1.0, "id": "fixed"})
    assert first["id"] == second["id"] == "fixed"


def test_a_stale_push_cannot_resurrect_a_fired_task(
    service: WakeService, db: WakeDB
) -> None:
    """The reason the server trusts the pushed timestamp instead of stamping now."""
    landed = service.add({"task": "x", "at": 1.0, "id": "fixed", "updated_at": 100.0})
    db.mark_fired("fixed")
    fired_at_rev = db.get("fixed")

    service.add(
        {"task": "x", "at": 1.0, "id": "fixed", "status": "pending", "updated_at": 50.0}
    )
    still = db.get("fixed")
    assert still is not None and fired_at_rev is not None
    assert still.status == "fired"
    assert landed["id"] == "fixed"


def test_cancel_of_a_missing_id_is_a_404(service: WakeService) -> None:
    with pytest.raises(ApiError) as info:
        service.cancel({"id": "nope"})
    assert info.value.status == 404


def test_list_since_rejects_a_non_integer_cursor(service: WakeService) -> None:
    with pytest.raises(ApiError) as info:
        service.list_since({"since": "yesterday"})
    assert info.value.status == 400


def test_authorise_only_checks_when_a_key_is_configured(db: WakeDB) -> None:
    open_service = WakeService(db, WakeConfig())
    open_service.authorise({})

    closed = WakeService(db, WakeConfig(api_key="s3cret"))
    with pytest.raises(ApiError) as info:
        closed.authorise({})
    assert info.value.status == 401
    closed.authorise({"x-wake-key": "s3cret"})


# -- over a socket ----------------------------------------------------------


@pytest.fixture
def running(db: WakeDB) -> Iterator[str]:
    config = WakeConfig(role="server", bind="127.0.0.1", port=0, poll_seconds=3600)
    server = WakeServer(db, config)
    thread = threading.Thread(target=server.httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.httpd.shutdown()
        thread.join(timeout=5)
        server.close()


def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_reports_the_revision(running: str, db: WakeDB) -> None:
    db.add(task="x", at=1.0, backend="shell", target=None, origin="server")
    with urllib.request.urlopen(f"{running}/health", timeout=5) as response:
        payload = json.loads(response.read())
    assert payload["ok"] is True
    assert payload["revision"] == 1
    assert payload["role"] == "server"


def test_an_unknown_path_is_a_404(running: str) -> None:
    status, payload = _post(f"{running}/api/v1/nope", {})
    assert status == 404
    assert payload["error"] == "not found"


def test_malformed_json_is_a_400_not_a_crash(running: str) -> None:
    request = urllib.request.Request(
        f"{running}/api/v1/tasks", data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 400


def test_a_json_array_body_is_a_400(running: str) -> None:
    request = urllib.request.Request(
        f"{running}/api/v1/tasks", data=b"[1,2,3]",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 400


def test_an_external_caller_can_schedule_a_task(running: str, db: WakeDB) -> None:
    """The phone -> wake direction: no wake install needed, just this POST."""
    status, payload = _post(
        f"{running}/api/v1/tasks",
        {"task": "wake me", "at": time.time() + 3600, "backend": "call"},
    )
    assert status == 200
    stored = db.get(payload["id"])
    assert stored is not None
    assert stored.backend == "call"


def test_the_scheduler_thread_survives_a_backend_blowing_up(
    db: WakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-BackendError must not silently kill the loop and every later task."""
    from wake import backends, server

    calls: list[str] = []

    def exploding(task: Any, config: Any) -> None:
        calls.append(task.id)
        raise RuntimeError("not a BackendError")

    monkeypatch.setitem(backends.FIRE_BACKENDS, "shell", exploding)
    db.add(task="x", at=1.0, backend="shell", target=None, origin="server")

    config = WakeConfig(role="server", bind="127.0.0.1", port=0, poll_seconds=0.01)
    instance = server.WakeServer(db, config)
    thread = threading.Thread(target=instance._scheduler_loop, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    instance._stop.set()
    thread.join(timeout=5)
    instance.close()

    assert len(calls) >= 2, "the loop stopped after the first exception"


def test_an_external_re_add_rearms_the_same_id(service: WakeService, db: WakeDB) -> None:
    """No updated_at in the body means an instruction, not a stale sync push."""
    service.add({"task": "track run abc", "at": 100.0, "id": "track-abc"})
    db.mark_fired("track-abc")
    again = service.add({"task": "track run abc", "at": 900.0, "id": "track-abc"})

    assert again["at"] == 900.0
    assert again["status"] == "pending"
    assert len(db.tasks(include_all=True)) == 1


# -- the self-re-arming task ------------------------------------------------


def test_a_task_that_rearms_itself_stays_scheduled(
    db: WakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command reschedules its own id before returning; wake must not
    stamp `fired` over the `pending` it just wrote."""
    from wake import backends

    def rearming(task: Any, config: Any) -> None:
        db.add(
            task=task.task, at=9999.0, backend="shell", target=None,
            origin=task.origin, id=task.id,
        )

    monkeypatch.setitem(backends.FIRE_BACKENDS, "shell", rearming)
    db.add(task="track run abc", at=1.0, backend="shell", target=None, origin="server")

    run_once(db, SERVER, now=500.0)
    stored = db.get(db.tasks(include_all=True)[0].id)
    assert stored is not None
    assert stored.status == "pending", "the assignment must survive its own run"
    assert stored.at == 9999.0
    assert stored.fired_at is None


def test_a_task_that_rearms_a_different_id_is_still_marked_fired(
    db: WakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only self-re-arming hits the race; normal firing must be unaffected."""
    from wake import backends

    def schedules_another(task: Any, config: Any) -> None:
        db.add(task="next", at=9999.0, backend="shell", target=None, origin="server")

    monkeypatch.setitem(backends.FIRE_BACKENDS, "shell", schedules_another)
    original = db.add(
        task="x", at=1.0, backend="shell", target=None, origin="server", id="first"
    )

    run_once(db, SERVER, now=500.0)
    stored = db.get(original.id)
    assert stored is not None
    assert stored.status == "fired"


def test_a_real_subprocess_rearming_the_same_database_is_respected(
    db: WakeDB, tmp_path: Path
) -> None:
    """The race for real: a separate process writes the row mid-fire.

    Uses the shell backend and a genuine child process rather than a patched
    callable, because the thing being trusted here is that the parent's
    connection sees the child's commit through WAL at all -- an in-process
    fake would prove nothing about that.
    """
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "rearm.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wake.db import WakeDB
            with WakeDB({str(db.path)!r}) as handle:
                handle.add(
                    task="next occurrence", at=9999.0, backend="shell",
                    target=None, origin="server", id="recurring",
                )
            """
        )
    )
    db.add(
        task=f"{sys.executable} {script}", at=1.0, backend="shell",
        target=None, origin="server", id="recurring",
    )

    proof = subprocess.run(
        [sys.executable, "-c", "import wake"], capture_output=True, check=False
    )
    assert proof.returncode == 0, "the child needs wake importable"

    run_once(db, SERVER, now=500.0)
    stored = db.get("recurring")
    assert stored is not None
    assert stored.status == "pending"
    assert stored.at == 9999.0


def test_a_thin_re_add_beats_a_row_stamped_in_the_future(
    service: WakeService, db: WakeDB
) -> None:
    """Why the thin-add branch exists at all, rather than always merging.

    A device whose clock runs fast pushes a row stamped ahead of this
    machine's wall clock. A later re-add stamps `now`, which is *older* than
    what is stored -- so routed through merge's last-write-wins it would be
    discarded, silently, and the caller's timer would sit at the old time
    reporting success. A re-add is an instruction and is not up for a vote.
    """
    from dataclasses import replace

    service.add({"task": "track run abc", "at": 100.0, "id": "track-abc"})
    stored = db.get("track-abc")
    assert stored is not None
    db.merge(replace(stored, updated_at=time.time() + 3600))

    again = service.add({"task": "track run abc", "at": 900.0, "id": "track-abc"})
    assert again["at"] == 900.0, "the re-arm must not lose to a future timestamp"
    assert again["status"] == "pending"


# -- recurrence -------------------------------------------------------------


def test_a_recurring_task_goes_back_to_pending_at_its_next_occurrence(
    db: WakeDB, tmp_path: Path
) -> None:
    counter = tmp_path / "count"
    db.add(
        task=f"echo x >> {counter}", at=100.0, backend="shell", target=None,
        origin="server", id="daily", repeat_seconds=60.0,
    )
    run_once(db, SERVER, now=500.0)

    stored = db.get("daily")
    assert stored is not None
    assert stored.status == "pending", "a recurrence has no terminal state"
    assert stored.at == 520.0
    assert stored.fired_at == 500.0
    assert counter.read_text() == "x\n"
    assert len(db.tasks(include_all=True)) == 1, "never a second row racing the first"


def test_a_recurring_task_fires_again_on_the_next_pass(db: WakeDB, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    db.add(
        task=f"echo x >> {counter}", at=100.0, backend="shell", target=None,
        origin="server", id="daily", repeat_seconds=60.0,
    )
    run_once(db, SERVER, now=500.0)
    run_once(db, SERVER, now=500.0)  # the next occurrence is not due yet
    run_once(db, SERVER, now=600.0)
    assert counter.read_text() == "x\nx\n"


def test_three_days_off_is_one_catch_up_run_not_three(db: WakeDB, tmp_path: Path) -> None:
    """The documented missed-occurrence policy, at the loop rather than the maths."""
    counter = tmp_path / "count"
    db.add(
        task=f"echo x >> {counter}", at=1000.0, backend="shell", target=None,
        origin="server", id="daily", repeat_seconds=86400.0,
    )
    back_up = 1000.0 + 3 * 86400.0 + 3600.0
    run_once(db, SERVER, now=back_up)
    run_once(db, SERVER, now=back_up)

    assert counter.read_text() == "x\n"
    stored = db.get("daily")
    assert stored is not None
    assert stored.at == 1000.0 + 4 * 86400.0, "back on the anchor's phase"


def test_a_failing_recurring_task_still_runs_tomorrow(db: WakeDB) -> None:
    """Stopping the schedule on the first bad night is the silent failure to avoid."""
    db.add(
        task="exit 9", at=100.0, backend="shell", target=None, origin="server",
        id="daily", repeat_seconds=60.0,
    )
    run_once(db, SERVER, now=500.0)
    stored = db.get("daily")
    assert stored is not None
    assert stored.status == "pending"
    assert stored.at == 520.0
    assert "exited 9" in (stored.error or ""), "the bad run is recorded, not discarded"


def test_a_cancelled_recurring_task_stops_for_good(db: WakeDB, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    db.add(
        task=f"echo x >> {counter}", at=100.0, backend="shell", target=None,
        origin="server", id="daily", repeat_seconds=60.0,
    )
    db.cancel("daily")
    assert run_once(db, SERVER, now=99999.0) == []
    assert not counter.exists()


def test_a_recurring_task_cancelled_mid_run_is_not_revived(
    db: WakeDB, tmp_path: Path
) -> None:
    """The command cancels its own row while wake is still holding it open."""
    import sys
    import textwrap

    marker = tmp_path / "cancelled"
    script = tmp_path / "cancel.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wake.db import WakeDB
            with WakeDB({str(db.path)!r}) as handle:
                handle.cancel("daily")
            open({str(marker)!r}, "w").close()
            """
        )
    )
    db.add(
        task=f"{sys.executable} {script}", at=100.0, backend="shell", target=None,
        origin="server", id="daily", repeat_seconds=60.0,
    )
    run_once(db, SERVER, now=500.0)

    assert marker.exists(), "the command has to have actually run"
    stored = db.get("daily")
    assert stored is not None
    assert stored.status == "cancelled"


def test_a_recurring_task_arms_the_rtc_for_its_own_next_occurrence(
    db: WakeDB, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recurrence and `--then poweroff` compose: tomorrow's run is the next wake."""
    armed: list[float] = []
    monkeypatch.setattr("wake.power.presence", lambda **_: _Clear())
    monkeypatch.setattr("wake.power.arm_wakealarm", lambda when: armed.append(when))
    monkeypatch.setattr("wake.power.suppress_watchdog", lambda: False)
    monkeypatch.setattr("wake.power.power_off", lambda: None)

    at = time.time() + 10
    db.add(
        task="true", at=at, backend="shell", target=None, origin="server",
        id="daily", repeat_seconds=86400.0, then_do="poweroff",
    )
    run_once(db, SERVER, now=at + 1)

    stored = db.get("daily")
    assert stored is not None and stored.at == at + 86400.0
    assert armed == [stored.at - power.WAKE_LEAD_SECONDS]


class _Clear:
    clear = True

    def why(self) -> str:
        return ""


# -- who fires it -----------------------------------------------------------


def test_the_server_fires_a_task_addressed_to_it_by_name(
    db: WakeDB, tmp_path: Path
) -> None:
    """`--on <the server>` used to produce a row nothing would ever fire."""
    marker = tmp_path / "by-name"
    db.add(
        task=f"touch {marker}", at=1.0, backend="shell", target=None,
        origin="hub", owner="hub",
    )
    assert len(run_once(db, SERVER, now=500.0)) == 1
    assert marker.exists()


def test_a_device_fires_only_its_own_tasks(db: WakeDB, tmp_path: Path) -> None:
    """And in particular not the server's, which is why the sets stay disjoint."""
    laptop = WakeConfig(role="device", origin="laptop")
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    db.add(
        task=f"touch {mine}", at=1.0, backend="shell", target=None,
        origin="laptop", owner="laptop",
    )
    db.add(task=f"touch {theirs}", at=1.0, backend="shell", target=None, origin="hub")

    assert len(run_once(db, laptop, now=500.0)) == 1
    assert mine.exists()
    assert not theirs.exists()


def test_owned_by_is_the_whole_rule() -> None:
    assert owned_by(WakeConfig(role="server", origin="hub")) == ("", "hub")
    assert owned_by(WakeConfig(role="device", origin="laptop")) == ("laptop",)
