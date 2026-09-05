"""The server side: an HTTP API over the task DB, plus the firing loop.

Uses the standard library's ``http.server`` rather than a hand-rolled
protocol implementation or a framework -- this is a private-network,
low-traffic control plane, and ``http.server`` is both fully inspectable
and already in every Python install.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import backends, power
from .config import WakeConfig
from .db import WakeDB
from .models import Task
from .whenspec import next_occurrence

LOG = logging.getLogger("wake.server")
MAX_BODY = 1 << 20  # 1 MiB


def _optional_float(value: object) -> float | None:
    """Narrow one optional JSON scalar, so a sync push does not lose the field.

    Every field this route forgot to copy off the body was silently dropped on
    the way through: a device pushing `--then poweroff` reached the server as a
    task that would never power anything off.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class WakeService:
    """The request handlers, independent of any HTTP framework -- testable directly."""

    def __init__(self, db: WakeDB, config: WakeConfig) -> None:
        self.db = db
        self.config = config

    def authorise(self, headers: dict[str, str]) -> None:
        if self.config.api_key and headers.get("x-wake-key", "") != self.config.api_key:
            raise ApiError(401, "bad or missing X-Wake-Key")

    def add(self, body: dict[str, Any]) -> dict[str, Any]:
        task_text = str(body.get("task", "")).strip()
        if not task_text:
            raise ApiError(400, "task is required")
        try:
            at = float(body["at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError(400, "at (epoch seconds) is required") from exc
        backend = str(body.get("backend", "shell"))
        target = body.get("target")
        origin = str(body.get("origin") or self.config.origin)
        owner = str(body.get("owner") or "")
        task_id = body.get("id")
        try:
            # Two callers reach this route and they mean different things.
            # A sync push sends a full Task.to_dict(), so it carries
            # updated_at, and must go through merge's last-write-wins: it may
            # legitimately be stale. Anything else -- a Shortcut, a bot, track
            # re-arming a recurring id -- is an instruction, not a claim about
            # what happened when, so it re-arms unconditionally.
            if task_id and "updated_at" not in body:
                row = self.db.add(
                    task=task_text, at=at, backend=backend,
                    target=str(target) if target else None, origin=origin,
                    owner=owner, id=str(task_id),
                    status=str(body.get("status", "pending")),
                )
            elif task_id:
                existing = self.db.get(str(task_id))
                # A sync push carries the device's own created_at/updated_at (it
                # sent a full Task.to_dict()) -- that timestamp is what merge()'s
                # last-write-wins comparison must judge against. Stamping "now"
                # here instead would make every push look like the newest write
                # regardless of how stale its content actually is, letting a
                # delayed push resurrect a task the server already fired.
                created_at = body.get("created_at")
                updated_at = body.get("updated_at")
                incoming = Task(
                    id=str(task_id),
                    task=task_text,
                    at=at,
                    backend=backend,
                    target=str(target) if target else None,
                    status=str(body.get("status", "pending")),
                    origin=origin,
                    created_at=float(created_at) if created_at is not None
                    else (existing.created_at if existing else time.time()),
                    updated_at=float(updated_at) if updated_at is not None else time.time(),
                    rev=0,
                    owner=owner,
                    then_do=str(body.get("then_do") or ""),
                    timeout_seconds=_optional_float(body.get("timeout_seconds")),
                    repeat_seconds=_optional_float(body.get("repeat_seconds")),
                    fired_at=_optional_float(body.get("fired_at")),
                    error=str(body["error"]) if body.get("error") is not None else None,
                )
                row = self.db.merge(incoming)
            else:
                row = self.db.add(
                    task=task_text, at=at, backend=backend,
                    target=str(target) if target else None, origin=origin, owner=owner,
                )
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(400, str(exc)) from exc
        return row.to_dict()

    def list_since(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            since = int(body.get("since", 0))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "since must be an integer revision") from exc
        found, revision = self.db.list_since(since)
        return {"tasks": [t.to_dict() for t in found], "revision": revision}

    def cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(body.get("id", ""))
        if not task_id:
            raise ApiError(400, "id is required")
        try:
            row = self.db.cancel(task_id)
        except Exception as exc:
            raise ApiError(404, str(exc)) from exc
        return row.to_dict()

    def health(self) -> dict[str, Any]:
        return {"ok": True, "revision": self.db.revision(), "role": self.config.role}


def owned_by(config: WakeConfig) -> tuple[str, ...]:
    """Every ``owner`` value that means "this machine".

    A device answers to its own ORIGIN and nothing else. The server answers to
    two names: ``""``, which ``add`` writes when nobody passed ``--on``, and its
    own ORIGIN, which is exactly what ``--on <the server>`` writes.

    Treating those two as one machine is not a convenience. Without it,
    ``wake add --on <the server>`` produces a task nothing will ever fire: the
    server's loop queries for ``""`` and no device answers to the server's
    name, so the row sits ``pending`` forever and fails silently at the moment
    it was supposed to matter. The two sets stay disjoint from every device's,
    so nothing is claimed twice.
    """
    if config.role == "server":
        return ("", config.origin)
    return (config.origin,)


def record_run(
    db: WakeDB, task: Task, *, error: str | None, expect_rev: int, moment: float
) -> Task | None:
    """Write down what one run did. ``None`` means the row moved and this gave way.

    A recurring task never reaches ``fired`` or ``failed``; it goes back to
    ``pending`` at its next occurrence. Including when it failed -- a nightly
    job that errored once must still run tomorrow, and stopping the schedule on
    the first bad night is precisely the silent failure recurrence exists to
    remove. The error is logged by the caller and kept on the row, so a failed
    run is visible without being terminal.
    """
    if task.repeat_seconds:
        return db.mark_recurred(
            task.id,
            at=next_occurrence(task.at, task.repeat_seconds, now=moment),
            fired_at=moment,
            error=error,
            expect_rev=expect_rev,
        )
    if error is not None:
        return db.mark_failed(task.id, error, expect_rev=expect_rev)
    return db.mark_fired(task.id, expect_rev=expect_rev)


def run_once(
    db: WakeDB, config: WakeConfig, *, owner: str | None = None, now: float | None = None
) -> list[Task]:
    """Fire every due task this machine owns once. Returns the tasks it acted on.

    Ownership is the whole scheduling rule: the server fires unassigned tasks
    and tasks addressed to it by name, a device fires only the tasks addressed
    to it. No task is therefore claimed twice, without any lease or lock
    crossing the network. ``owner`` overrides that for a caller that wants one
    specific name; ``None`` means ``owned_by(config)``.
    """
    owners = owned_by(config) if owner is None else (owner,)
    moment = time.time() if now is None else now
    handled = []
    for task in db.due(*owners, now=now):
        # Captured before the command runs: a self-re-arming task rewrites this
        # row while we are still holding it, and the bookkeeping below must
        # give way rather than stamp `fired` over the new `pending`.
        before = task.rev
        try:
            backends.fire(task, config)
        except backends.BackendError as exc:
            LOG.error("task %s (%s) failed: %s", task.id, task.backend, exc)
            if record_run(db, task, error=str(exc), expect_rev=before, moment=moment) is None:
                LOG.warning(
                    "task %s re-armed itself while failing; leaving it scheduled", task.id
                )
            if task.then_do:
                LOG.info("task %s: %s", task.id, finish_power(db, config, task, succeeded=False))
        else:
            settled = record_run(db, task, error=None, expect_rev=before, moment=moment)
            if settled is None:
                LOG.info(
                    "task %s (%s) fired and re-armed itself; leaving it scheduled",
                    task.id, task.backend,
                )
            elif task.repeat_seconds:
                LOG.info(
                    "task %s (%s) fired; next occurrence at %.0f",
                    task.id, task.backend, settled.at,
                )
            else:
                LOG.info("task %s (%s) fired", task.id, task.backend)
            if task.then_do:
                LOG.info("task %s: %s", task.id, finish_power(db, config, task, succeeded=True))
        handled.append(task)
    return handled


def finish_power(db: WakeDB, config: WakeConfig, task: Task, *, succeeded: bool) -> str:
    """Carry out a task's ``--then`` action. Returns what happened, for the log.

    Order matters and each step is a guard in its own right:

    1. Refuse if someone is using the machine or other work is in flight.
    2. Arm the RTC for the next task this machine owns, *before* going down --
       that alarm is the backup path, the one that still works when the wake
       server or the LAN does not.
    3. Stop the timer that respawns the operator, so a session cannot appear
       between the check and the poweroff with nothing left to notice it.
    4. Power off.

    A failed task still powers the machine off: it finished, and leaving a
    desktop running all day because a research job exited non-zero is the
    wrong failure. What a failure does *not* do is arm the next wake -- that
    is the difference between a bad run and a machine waking every ten minutes
    all night to fail again.
    """
    if task.then_do != "poweroff":
        return "nothing to do"

    seen = power.presence(
        allow_agents={n for n in config.poweroff_allow_agents.split(",") if n.strip()},
        allow_match=config.poweroff_allow_match,
    )
    if not seen.clear:
        return f"staying up: {seen.why()}"

    armed = "no next task to arm"
    if succeeded:
        mine = owned_by(config)
        upcoming = [t for t in db.tasks() if t.at > time.time() and t.owner in mine]
        if upcoming:
            nxt = min(upcoming, key=lambda t: t.at)
            try:
                wake_at = nxt.at - power.WAKE_LEAD_SECONDS
                power.arm_wakealarm(wake_at)
                armed = f"rtc armed for {nxt.id[:8]} at {wake_at:.0f} (task at {nxt.at:.0f})"
            except power.PowerError as exc:
                armed = f"rtc NOT armed: {exc}"
                LOG.error("could not arm the rtc: %s", exc)
    else:
        armed = "rtc not armed: the task failed, and a failing wake loop is worse than a dark box"

    stopped = power.suppress_watchdog()
    LOG.warning("powering off (%s; watchdog suppressed: %s)", armed, stopped)
    try:
        power.power_off()
    except power.PowerError as exc:
        # A refused poweroff must not take the scheduler down with it, and must
        # not leave the operator's respawn timer stopped for a shutdown that
        # never happened. Both were real: NoNewPrivileges=true in the unit
        # blocked sudo, the PowerError propagated out and killed the daemon,
        # and the watchdog stayed off on a machine that was still running.
        if stopped:
            power.restore_watchdog()
        LOG.error("poweroff failed, machine stays up: %s", exc)
        return f"poweroff FAILED, machine stays up: {exc}"
    return f"powering off ({armed})"


def _make_handler(service: WakeService) -> type[BaseHTTPRequestHandler]:
    routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "/api/v1/tasks": service.add,
        "/api/v1/tasks/list": service.list_since,
        "/api/v1/tasks/cancel": service.cancel,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "wake/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, service.health())
                return
            self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            handler = routes.get(self.path)
            if handler is None:
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, {"error": "bad Content-Length"})
                return
            if length > MAX_BODY:
                self._reply(413, {"error": "payload too large"})
                return
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._reply(400, {"error": "invalid JSON"})
                return
            if not isinstance(body, dict):
                self._reply(400, {"error": "body must be a JSON object"})
                return
            headers = {key.lower(): value for key, value in self.headers.items()}
            try:
                service.authorise(headers)
                result = handler(body)
            except ApiError as exc:
                self._reply(exc.status, {"error": exc.message})
                return
            except Exception as exc:
                # Never let a bug hang the connection: a device blocked on a
                # dead socket stops syncing entirely, which is worse than a 500.
                LOG.exception("unhandled error in %s", self.path)
                self._reply(500, {"error": str(exc)})
                return
            self._reply(200, result)

    return Handler


class WakeServer:
    """Owns the HTTP listener and the background firing loop together."""

    def __init__(self, db: WakeDB, config: WakeConfig) -> None:
        self.db = db
        self.config = config
        self.service = WakeService(db, config)
        self.httpd = ThreadingHTTPServer(
            (config.bind, config.port), _make_handler(self.service)
        )
        self._stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_once(self.db, self.config)
            except Exception:
                # A backend raising something that is not a BackendError must
                # not take the loop down with it -- every later task would go
                # unfired with no signal but a dead thread.
                LOG.exception("scheduler pass failed")
            self._stop.wait(self.config.poll_seconds)

    def serve_forever(self) -> None:
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()
        try:
            self.httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=5)

    def close(self) -> None:
        self.httpd.server_close()
