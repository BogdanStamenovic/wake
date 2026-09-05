"""Device side of sync: push what this device wrote, pull what the server knows.

The two directions use different cursors on purpose. The *pull* cursor is a
single watermark into the server's revision sequence -- correct, because that
sequence is the server's and this device only ever reads it. The *push* cursor
is per row (``pushed_rev``, see db.py), because the local revision sequence is
written by both sides of this module and no single watermark over it can be
advanced without either re-sending or losing a row.

Timestamps are not compared at this layer at all. That happens once, per row,
inside ``WakeDB.merge``, as the last-write-wins tie-breaker for the rare case
where both sides edited the same task id.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import WakeConfig
from .db import WakeDB
from .models import Task

LOG = logging.getLogger("wake.sync")
REQUEST_TIMEOUT = 10.0


class SyncError(Exception):
    """Raised when a device cannot reach or was refused by the server."""


def _post(config: WakeConfig, path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not config.server_url:
        raise SyncError("no server configured (set WAKE_SERVER_URL)")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["X-Wake-Key"] = config.api_key
    request = urllib.request.Request(
        f"{config.server_url}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise SyncError(f"server returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"could not reach {config.server_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"server sent a non-JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncError(f"server sent {type(payload).__name__}, expected an object")
    return payload


def push(db: WakeDB, config: WakeConfig) -> int:
    """Push every row this device authored whose content the server lacks.

    Each row is acknowledged individually, so a server that goes away halfway
    through leaves the rows already accepted marked as pushed and the rest
    still pending -- the next run resumes rather than restarting.
    """
    pending = db.unpushed(config.origin)
    dropped = []
    for task in pending:
        stored = _post(config, "/api/v1/tasks", task.to_dict())
        # The server echoes back the row it stored, which costs nothing extra
        # and is the only way to notice a server too old to understand a field.
        # It matters for exactly one field so far: a server without
        # `repeat_seconds` drops it, fires the task once, marks it `fired`, and
        # the device then pulls that back and loses its own copy of the period.
        # A recurring task quietly becoming a one-shot is the failure this
        # feature exists to remove, so it must not happen quietly.
        if task.repeat_seconds and stored.get("repeat_seconds") is None:
            dropped.append(task.id)
        db.mark_pushed(task.id, task.rev)
    if dropped:
        LOG.warning(
            "the server did not store the repeat period on %d task(s) (%s) and will "
            "not recur them: it is running a wake too old for --every. Tasks this "
            "device owns (--on %s) still recur, because this device re-arms those "
            "itself; tasks the server fires will run once and stop.",
            len(dropped), ", ".join(t[:8] for t in dropped), config.origin,
        )
    return len(pending)


def pull(db: WakeDB, config: WakeConfig) -> int:
    """Pull down everything the server has recorded since this device last pulled."""
    last_pulled = db.get_meta("last_pulled_rev") or 0
    result = _post(config, "/api/v1/tasks/list", {"since": last_pulled})
    raw_tasks = result.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise SyncError("server sent a malformed task list")
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise SyncError("server sent a malformed task")
        try:
            db.merge(Task.from_dict(raw))
        except (KeyError, TypeError, ValueError) as exc:
            raise SyncError(f"server sent an unreadable task: {exc}") from exc
    revision = result.get("revision")
    if not isinstance(revision, int):
        raise SyncError("server did not return a revision cursor")
    db.set_meta("last_pulled_rev", revision)
    return len(raw_tasks)


def sync(db: WakeDB, config: WakeConfig) -> tuple[int, int]:
    """Push then pull. Returns (pushed, pulled).

    Push first so that a task added a moment ago is on the server before this
    device asks what the server knows -- otherwise the very first sync after
    an add reports it as unknown and the row waits a whole cycle.
    """
    pushed = push(db, config)
    pulled = pull(db, config)
    return pushed, pulled
