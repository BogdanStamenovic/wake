"""SQLite-backed task store, shared by the device and the server.

Every write bumps a single monotonic revision counter and stamps the row with
it. That ``rev`` is the sync cursor: "everything after revision N" is an exact,
gap-free query (``WHERE rev > ?``) with no clock involved, the same shape
hotline-ios's event feed uses its global ``seq`` for. Wall-clock ``updated_at``
is kept too, but only to break ties when a device and the server have each
written the same task id independently (last write wins).

The push cursor is deliberately *per row* (``pushed_rev``) rather than one
global "last pushed" watermark. A global watermark is wrong here because
``pull`` writes rows too, and those writes take fresh local revisions: any
watermark advanced past them either re-pushes everything the server just sent
back, or -- if it is advanced to the post-pull revision -- silently skips a
local task created while the pull was running. Per row, the invariant is
simply ``pushed_rev < rev`` means "the server has not seen this content", which
survives any interleaving.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Self

from .models import BACKENDS, STATUSES, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    at REAL NOT NULL,
    backend TEXT NOT NULL,
    target TEXT,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    rev INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    pushed_rev INTEGER NOT NULL DEFAULT 0,
    fired_at REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_rev ON tasks(rev);
CREATE INDEX IF NOT EXISTS tasks_status_at ON tasks(status, at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

# Columns added after 0.1.0. Applied one at a time, ignoring "duplicate column",
# which is the same shape hotline-ios's store uses for its own late columns --
# a table of one-line ALTERs beats a migration framework for a file this small.
_MIGRATIONS = {
    "owner": "ALTER TABLE tasks ADD COLUMN owner TEXT NOT NULL DEFAULT ''",
    "pushed_rev": "ALTER TABLE tasks ADD COLUMN pushed_rev INTEGER NOT NULL DEFAULT 0",
}


class WakeError(Exception):
    """Raised when wake cannot complete an operation."""


class WakeDB:
    """A task table plus the revision counter that makes it syncable.

    Every public method takes ``self._lock`` -- the server shares one
    ``WakeDB`` across its HTTP worker threads and the scheduler thread, so
    this serializes access rather than trusting sqlite3's own threading mode.
    An ``RLock`` because ``merge``/``_update_status`` call ``get`` internally.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', 0)")
        self._conn.commit()
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        present = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")}
        for column, statement in _MIGRATIONS.items():
            if column not in present:
                self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes --------------------------------------------------------

    def _next_rev(self) -> int:
        cursor = self._conn.execute(
            "UPDATE meta SET value = value + 1 WHERE key = 'revision' RETURNING value"
        )
        row = cursor.fetchone()
        return int(row[0])

    def add(
        self,
        *,
        task: str,
        at: float,
        backend: str,
        target: str | None,
        origin: str,
        owner: str = "",
        id: str | None = None,
        status: str = "pending",
    ) -> Task:
        if backend not in BACKENDS:
            raise WakeError(f"unknown backend {backend!r}, expected one of {BACKENDS}")
        if status not in STATUSES:
            raise WakeError(f"unknown status {status!r}, expected one of {STATUSES}")
        with self._lock:
            now = time.time()
            row = Task(
                id=id or uuid.uuid4().hex,
                task=task,
                at=at,
                backend=backend,
                target=target,
                status=status,
                origin=origin,
                created_at=now,
                updated_at=now,
                rev=self._next_rev(),
                owner=owner,
            )
            self._conn.execute(
                "INSERT INTO tasks "
                "(id, task, at, backend, target, status, origin, created_at, updated_at, rev, "
                " owner, pushed_rev, fired_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*_columns(row), 0, row.fired_at, row.error),
            )
            self._conn.commit()
            return row

    def merge(self, incoming: Task) -> Task:
        """Insert or last-write-wins-update a task arriving from a sync peer.

        Always assigns a fresh local ``rev`` on any write so this side's own
        cursor stays gap-free -- the peer's ``rev`` is meaningless here, it
        only made sense in *their* database.

        ``pushed_rev`` is set equal to that fresh ``rev``: the content being
        written came from the peer, so by definition the peer already has it
        and it must not be pushed back. When the incoming row loses the
        last-write-wins comparison nothing is written at all, which leaves the
        local row's ``pushed_rev`` behind its ``rev`` -- exactly right, since
        the peer is the one that is out of date.
        """
        with self._lock:
            existing = self.get(incoming.id)
            if existing is not None and existing.updated_at >= incoming.updated_at:
                return existing
            rev = self._next_rev()
            merged = replace(
                incoming,
                rev=rev,
                created_at=existing.created_at if existing else incoming.created_at,
            )
            self._conn.execute(
                "INSERT INTO tasks "
                "(id, task, at, backend, target, status, origin, created_at, updated_at, rev, "
                " owner, pushed_rev, fired_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "task=excluded.task, at=excluded.at, backend=excluded.backend, "
                "target=excluded.target, status=excluded.status, updated_at=excluded.updated_at, "
                "rev=excluded.rev, owner=excluded.owner, pushed_rev=excluded.pushed_rev, "
                "fired_at=excluded.fired_at, error=excluded.error",
                (*_columns(merged), rev, merged.fired_at, merged.error),
            )
            self._conn.commit()
            stored = self.get(incoming.id)
            assert stored is not None
            return stored

    def _update_status(
        self, id: str, *, status: str, fired_at: float | None = None, error: str | None = None
    ) -> Task:
        with self._lock:
            existing = self.get(id)
            if existing is None:
                raise WakeError(f"no such task: {id}")
            rev = self._next_rev()
            now = time.time()
            self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=?, rev=?, fired_at=?, error=? WHERE id=?",
                (status, now, rev, fired_at, error, id),
            )
            self._conn.commit()
            updated = self.get(id)
            assert updated is not None
            return updated

    def cancel(self, id: str) -> Task:
        return self._update_status(id, status="cancelled")

    def mark_armed(self, id: str) -> Task:
        return self._update_status(id, status="armed")

    def mark_fired(self, id: str) -> Task:
        return self._update_status(id, status="fired", fired_at=time.time())

    def mark_failed(self, id: str, error: str) -> Task:
        return self._update_status(id, status="failed", error=error)

    def mark_pushed(self, id: str, rev: int) -> None:
        """Record that the server has seen this row as of local revision ``rev``.

        Does not touch ``rev`` or ``updated_at``: acknowledging a push is
        bookkeeping, not an edit, and bumping the revision here would make the
        row look freshly changed and push itself again forever.
        """
        with self._lock:
            self._conn.execute("UPDATE tasks SET pushed_rev = ? WHERE id = ?", (rev, id))
            self._conn.commit()

    # -- reads -----------------------------------------------------------

    def get(self, id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
            return _row_to_task(row) if row else None

    # Not named `list`: a method called `list` shadows the builtin for every
    # annotation evaluated later in the class body, and mypy then rejects
    # `-> list[Task]` on `list_since`, `due` and `unpushed` as "not iterable".
    def tasks(self, *, include_all: bool = False) -> list[Task]:
        with self._lock:
            if include_all:
                rows = self._conn.execute("SELECT * FROM tasks ORDER BY at").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status IN ('pending', 'armed') ORDER BY at"
                ).fetchall()
            return [_row_to_task(row) for row in rows]

    def list_since(self, rev: int) -> tuple[list[Task], int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE rev > ? ORDER BY rev", (rev,)
            ).fetchall()
            found = [_row_to_task(row) for row in rows]
            newest = found[-1].rev if found else rev
            return found, newest

    def unpushed(self, origin: str) -> list[Task]:
        """Rows this machine authored whose current content the server lacks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE origin = ? AND pushed_rev < rev ORDER BY rev",
                (origin,),
            ).fetchall()
            return [_row_to_task(row) for row in rows]

    def due(self, owner: str = "", *, now: float | None = None) -> list[Task]:
        """Pending tasks owned by ``owner`` whose time has come.

        ``rtcwake`` is excluded: it is armed on its own device at ``add`` time
        (see backends.py) and a suspended machine cannot run this loop anyway,
        so a pending ``rtcwake`` row is a failed arming, not work to pick up.
        """
        with self._lock:
            cutoff = time.time() if now is None else now
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'pending' AND at <= ? "
                "AND backend != 'rtcwake' AND owner = ? ORDER BY at",
                (cutoff, owner),
            ).fetchall()
            return [_row_to_task(row) for row in rows]

    def revision(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
            return int(row[0])

    # -- sync bookkeeping --------------------------------------------------
    # The pull cursor: how far into the *server's* revision sequence this
    # device has read. Meaningless on a server install, harmless to store.

    def get_meta(self, key: str) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return int(row[0]) if row else None

    def set_meta(self, key: str, value: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()


def _columns(row: Task) -> tuple[object, ...]:
    """The first eleven INSERT parameters, in schema order."""
    return (
        row.id,
        row.task,
        row.at,
        row.backend,
        row.target,
        row.status,
        row.origin,
        row.created_at,
        row.updated_at,
        row.rev,
        row.owner,
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        task=row["task"],
        at=row["at"],
        backend=row["backend"],
        target=row["target"],
        status=row["status"],
        origin=row["origin"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        rev=row["rev"],
        owner=row["owner"],
        fired_at=row["fired_at"],
        error=row["error"],
    )
