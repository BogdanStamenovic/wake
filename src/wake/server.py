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

from . import backends
from .config import WakeConfig
from .db import WakeDB
from .models import Task

LOG = logging.getLogger("wake.server")
MAX_BODY = 1 << 20  # 1 MiB


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
                    fired_at=body.get("fired_at"),
                    error=body.get("error"),
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


def run_once(
    db: WakeDB, config: WakeConfig, *, owner: str = "", now: float | None = None
) -> list[Task]:
    """Fire every due task belonging to ``owner`` once. Returns the tasks it acted on.

    ``owner`` is the whole scheduling rule: the server runs with ``""`` and
    fires unassigned tasks, a device runs with its own origin and fires only
    the tasks addressed to it. No task is therefore claimed twice, without any
    lease or lock crossing the network.
    """
    handled = []
    for task in db.due(owner, now=now):
        # Captured before the command runs: a self-re-arming task rewrites this
        # row while we are still holding it, and the bookkeeping below must
        # give way rather than stamp `fired` over the new `pending`.
        before = task.rev
        try:
            backends.fire(task, config)
        except backends.BackendError as exc:
            LOG.error("task %s (%s) failed: %s", task.id, task.backend, exc)
            if db.mark_failed(task.id, str(exc), expect_rev=before) is None:
                LOG.warning(
                    "task %s re-armed itself while failing; leaving it scheduled", task.id
                )
        else:
            if db.mark_fired(task.id, expect_rev=before) is None:
                LOG.info(
                    "task %s (%s) fired and re-armed itself; leaving it scheduled",
                    task.id, task.backend,
                )
            else:
                LOG.info("task %s (%s) fired", task.id, task.backend)
        handled.append(task)
    return handled


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
