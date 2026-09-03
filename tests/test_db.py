"""The revision counter and the two cursors built on it."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from wake.db import WakeDB, WakeError
from wake.models import Task


def _add(db: WakeDB, **overrides: object) -> Task:
    fields: dict[str, object] = {
        "task": "echo hi",
        "at": 1000.0,
        "backend": "shell",
        "target": None,
        "origin": "testbox",
    }
    fields.update(overrides)
    return db.add(**fields)  # type: ignore[arg-type]


def test_add_assigns_an_id_and_a_revision(db: WakeDB) -> None:
    task = _add(db)
    assert task.id
    assert task.rev == 1
    assert db.revision() == 1
    assert db.get(task.id) == task


def test_revisions_are_monotonic_across_every_kind_of_write(db: WakeDB) -> None:
    first = _add(db)
    second = _add(db)
    cancelled = db.cancel(first.id)
    assert [first.rev, second.rev, cancelled.rev] == [1, 2, 3]


def test_unknown_backend_and_status_are_refused(db: WakeDB) -> None:
    with pytest.raises(WakeError):
        _add(db, backend="telepathy")
    with pytest.raises(WakeError):
        _add(db, status="maybe")


def test_cancelling_a_missing_task_raises(db: WakeDB) -> None:
    with pytest.raises(WakeError):
        db.cancel("nope")


def test_list_hides_finished_tasks_unless_asked(db: WakeDB) -> None:
    live = _add(db)
    done = _add(db)
    db.mark_fired(done.id)
    assert [t.id for t in db.tasks()] == [live.id]
    assert {t.id for t in db.tasks(include_all=True)} == {live.id, done.id}


def test_list_since_is_gap_free_and_reports_the_newest_revision(db: WakeDB) -> None:
    first = _add(db)
    second = _add(db)
    rows, newest = db.list_since(0)
    assert [r.id for r in rows] == [first.id, second.id]
    assert newest == 2
    rows, newest = db.list_since(1)
    assert [r.id for r in rows] == [second.id]
    assert newest == 2


def test_list_since_at_the_head_returns_the_cursor_unchanged(db: WakeDB) -> None:
    _add(db)
    rows, newest = db.list_since(1)
    assert rows == []
    assert newest == 1


# -- due() ------------------------------------------------------------------


def test_due_returns_only_pending_tasks_whose_time_has_come(db: WakeDB) -> None:
    past = _add(db, at=100.0)
    _add(db, at=9999.0)
    fired = _add(db, at=100.0)
    db.mark_fired(fired.id)
    assert [t.id for t in db.due(now=500.0)] == [past.id]


def test_due_never_returns_rtcwake(db: WakeDB) -> None:
    """A pending rtcwake row is a failed arming, not work for a scheduler."""
    _add(db, at=100.0, backend="rtcwake")
    assert db.due(now=500.0) == []


def test_due_is_scoped_to_one_owner(db: WakeDB) -> None:
    unassigned = _add(db, at=100.0)
    mine = _add(db, at=100.0, owner="laptop")
    _add(db, at=100.0, owner="other")
    assert [t.id for t in db.due(now=500.0)] == [unassigned.id]
    assert [t.id for t in db.due("laptop", now=500.0)] == [mine.id]


# -- merge() ----------------------------------------------------------------


def test_merge_inserts_an_unseen_task(db: WakeDB) -> None:
    incoming = Task(
        id="abc", task="ring", at=1.0, backend="call", target=None, status="pending",
        origin="phone", created_at=1.0, updated_at=1.0, rev=77,
    )
    stored = db.merge(incoming)
    assert stored.task == "ring"
    assert stored.rev == 1, "the peer's revision is meaningless in this database"


def test_merge_takes_the_newer_write(db: WakeDB) -> None:
    local = _add(db)
    newer = replace(local, task="changed", updated_at=local.updated_at + 10)
    assert db.merge(newer).task == "changed"


def test_merge_keeps_the_local_row_when_the_incoming_one_is_stale(db: WakeDB) -> None:
    local = _add(db)
    older = replace(local, task="stale", updated_at=local.updated_at - 10)
    assert db.merge(older).task == "echo hi"
    assert db.revision() == 1, "losing a merge must not burn a revision"


def test_merge_preserves_the_original_creation_time(db: WakeDB) -> None:
    local = _add(db)
    incoming = replace(local, created_at=0.0, updated_at=local.updated_at + 10)
    assert db.merge(incoming).created_at == local.created_at


# -- the per-row push cursor ------------------------------------------------


def test_a_new_local_task_is_unpushed(db: WakeDB) -> None:
    task = _add(db)
    assert [t.id for t in db.unpushed("testbox")] == [task.id]


def test_unpushed_ignores_other_machines_rows(db: WakeDB) -> None:
    _add(db, origin="somebody-else")
    assert db.unpushed("testbox") == []


def test_marking_pushed_clears_it_without_burning_a_revision(db: WakeDB) -> None:
    task = _add(db)
    db.mark_pushed(task.id, task.rev)
    assert db.unpushed("testbox") == []
    assert db.revision() == 1
    assert db.get(task.id) == task, "acknowledging a push is not an edit"


def test_editing_a_pushed_task_makes_it_unpushed_again(db: WakeDB) -> None:
    task = _add(db)
    db.mark_pushed(task.id, task.rev)
    db.cancel(task.id)
    assert [t.id for t in db.unpushed("testbox")] == [task.id]


def test_a_pulled_row_is_never_pushed_back(db: WakeDB) -> None:
    """merge() is how rows arrive from the server; they must not bounce."""
    incoming = Task(
        id="abc", task="ring", at=1.0, backend="call", target=None, status="pending",
        origin="testbox", created_at=1.0, updated_at=1.0, rev=99,
    )
    db.merge(incoming)
    assert db.unpushed("testbox") == []


def test_a_pulled_row_that_loses_the_merge_stays_unpushed(db: WakeDB) -> None:
    """The server is the one that is behind, so this device must push again."""
    local = _add(db)
    stale = replace(local, task="stale", updated_at=local.updated_at - 10)
    db.merge(stale)
    assert [t.id for t in db.unpushed("testbox")] == [local.id]


# -- migration --------------------------------------------------------------


def test_opens_a_pre_0_2_database_and_adds_the_new_columns(tmp_path: Path) -> None:
    """A 0.1.0 database has no owner or pushed_rev; opening it must not fail."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, task TEXT NOT NULL, at REAL NOT NULL,
            backend TEXT NOT NULL, target TEXT, status TEXT NOT NULL,
            origin TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            rev INTEGER NOT NULL, fired_at REAL, error TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        INSERT INTO meta VALUES ('revision', 1);
        INSERT INTO tasks VALUES
            ('old1', 'echo legacy', 5.0, 'shell', NULL, 'pending', 'testbox',
             1.0, 1.0, 1, NULL, NULL);
        """
    )
    old.commit()
    old.close()

    with WakeDB(path) as db:
        task = db.get("old1")
        assert task is not None
        assert task.owner == "", "existing rows default to server-owned"
        assert [t.id for t in db.due(now=500.0)] == ["old1"]
        assert [t.id for t in db.unpushed("testbox")] == ["old1"]


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    with WakeDB(path) as first:
        _add(first)
    with WakeDB(path) as second:
        assert len(second.tasks()) == 1
