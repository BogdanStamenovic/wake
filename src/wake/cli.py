"""Command-line interface for wake.

Keeps stdout clean: `add` prints only the new task id, `list --json` prints
only the JSON array, `list` prints only the table. Everything else -- progress,
warnings, errors -- goes to stderr. Exit codes: 0 success, 1 one or more
operations failed, 2 usage error / user aborted.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import NoReturn

from . import __version__, backends
from .backends import BackendError
from .config import ConfigError, WakeConfig, load_config
from .db import WakeDB, WakeError
from .models import BACKENDS, Task
from .server import WakeServer, finish_power, run_once
from .syncclient import SyncError
from .syncclient import sync as sync_now
from .whenspec import WhenError, parse_when

Logger = Callable[[str], None]


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="wake", description="Schedule and fire wake-up tasks.")
    parser.add_argument("-v", "--verbose", action="store_true", help="print detailed progress")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="path to a wake.env config file")

    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="schedule a new wake task")
    add.add_argument("--at", required=True, help="epoch seconds, ISO 8601, or +<N>[s|m|h|d]")
    add.add_argument("--task", required=True, help="what to run when it fires")
    add.add_argument("--backend", default="shell", choices=BACKENDS)
    add.add_argument("--target", help="backend-specific target (MAC for wol, agent for notify)")
    add.add_argument("--on", dest="owner", help="origin name of the machine that fires it "
                                                "(default: the server)")
    add.add_argument(
        "--then", dest="then_do", default="", choices=("poweroff",),
        help="what to do to this machine once the task finishes",
    )
    add.add_argument(
        "--timeout", type=float,
        help="seconds before the command is killed (default 300); a hung task must "
             "not be able to hold the machine up forever",
    )
    add.add_argument("--id", help="explicit task id, instead of a generated one")

    listing = sub.add_parser("list", help="list wake tasks")
    listing.add_argument("--all", action="store_true", help="include fired/cancelled/failed")
    listing.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    cancel = sub.add_parser("cancel", help="cancel a pending task")
    cancel.add_argument("id")

    sub.add_parser("sync", help="push local tasks up and pull the server's view down, once")

    agent = sub.add_parser(
        "agent", help="device daemon: sync on a period, fire this machine's own tasks"
    )
    agent.add_argument("--once", action="store_true", help="run a single pass and exit")

    serve = sub.add_parser("serve", help="run the server: HTTP API + the firing loop")
    serve.add_argument("--port", type=int, help="override the configured port")
    serve.add_argument("--bind", help="override the configured listen address")

    fire = sub.add_parser("fire", help="fire one task now, bypassing its schedule")
    fire.add_argument("id")

    return parser


def _open_db(config: WakeConfig) -> WakeDB:
    return WakeDB(config.db_path)


def _format_table(rows: list[Task]) -> str:
    if not rows:
        return "no tasks"
    widths = {"id": 8, "at": 24, "status": 9, "backend": 8}
    header = (
        f"{'id':<{widths['id']}}  {'at':<{widths['at']}}  {'status':<{widths['status']}}  "
        f"{'backend':<{widths['backend']}}  task"
    )
    lines = [header]
    for row in rows:
        at_str = datetime.fromtimestamp(row.at, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(
            f"{row.id[:8]:<{widths['id']}}  {at_str:<{widths['at']}}  "
            f"{row.status:<{widths['status']}}  {row.backend:<{widths['backend']}}  {row.task}"
        )
    return "\n".join(lines)


def _stop_event() -> threading.Event:
    """An event set by SIGINT/SIGTERM, so a loop exits between passes, not mid-write."""
    stop = threading.Event()

    def _handle(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    return stop


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"wake: error: {exc}", file=sys.stderr)
        return 2

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    def vlog(message: str) -> None:
        if args.verbose and not args.quiet:
            print(message, file=sys.stderr)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    config_path = Path(args.config) if args.config else None
    try:
        config = load_config(config_path)
    except (ConfigError, OSError) as exc:
        print(f"wake: error: {exc}", file=sys.stderr)
        return 2

    handlers: dict[str, Callable[[], int]] = {
        "add": lambda: _cmd_add(args, config, vlog),
        "list": lambda: _cmd_list(args, config),
        "cancel": lambda: _cmd_cancel(args, config, log),
        "sync": lambda: _cmd_sync(config, log),
        "agent": lambda: _cmd_agent(args, config, log),
        "serve": lambda: _cmd_serve(args, config, log),
        "fire": lambda: _cmd_fire(args, config, log),
    }
    handler = handlers.get(args.command)
    if handler is None:
        print(f"wake: error: unknown command {args.command!r}", file=sys.stderr)
        return 2
    try:
        return handler()
    except (WakeError, WhenError, SyncError, BackendError) as exc:
        print(f"wake: error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # A database fault is a failed operation, not a bug to dump a stack
        # for at someone driving this from a shell script.
        print(f"wake: error: database: {exc}", file=sys.stderr)
        return 1


def _autosync(config: WakeConfig, report: Logger) -> None:
    """Best-effort sync after a local write. A device offline is not an error."""
    if config.role != "device" or not config.server_url:
        return
    try:
        with _open_db(config) as db:
            sync_now(db, config)
    except SyncError as exc:
        report(f"wake: warning: could not sync immediately: {exc}")


def _cmd_add(args: argparse.Namespace, config: WakeConfig, vlog: Logger) -> int:
    at = parse_when(args.at)
    with _open_db(config) as db:
        task = db.add(
            task=args.task, at=at, backend=args.backend, target=args.target,
            origin=config.origin, owner=args.owner or "", id=args.id,
            then_do=args.then_do, timeout_seconds=args.timeout,
        )
        if args.backend == "rtcwake":
            # Armed here and now, on this machine: nothing else can reach a
            # suspended box's hardware clock. Marked armed so no scheduler
            # anywhere later mistakes it for work to pick up.
            backends.arm_rtcwake(task)
            db.mark_armed(task.id)
        vlog(f"scheduled {task.backend} task for {task.at:.0f}")
    print(task.id)
    _autosync(config, lambda message: print(message, file=sys.stderr))
    return 0


def _cmd_list(args: argparse.Namespace, config: WakeConfig) -> int:
    with _open_db(config) as db:
        rows = db.tasks(include_all=args.all)
    if args.json:
        print(json.dumps([t.to_dict() for t in rows]))
    else:
        print(_format_table(rows))
    return 0


def _cmd_cancel(args: argparse.Namespace, config: WakeConfig, log: Logger) -> int:
    with _open_db(config) as db:
        task = db.cancel(args.id)
    log(f"cancelled {task.id}")
    _autosync(config, log)
    return 0


def _cmd_sync(config: WakeConfig, log: Logger) -> int:
    if config.role == "server":
        log("this is the server; it is the source of truth, nothing to sync")
        return 0
    with _open_db(config) as db:
        pushed, pulled = sync_now(db, config)
    log(f"pushed {pushed}, pulled {pulled}")
    return 0


def _cmd_agent(args: argparse.Namespace, config: WakeConfig, log: Logger) -> int:
    """Sync on a slow period, fire this machine's own tasks on a fast one.

    Two periods rather than one: firing wants to be prompt (it is an alarm),
    reconciling does not want to hammer the server every few seconds.
    """
    stop = _stop_event()
    with _open_db(config) as db:
        log(
            f"wake agent: origin {config.origin}, firing every {config.poll_seconds}s, "
            f"syncing every {config.sync_seconds}s"
        )
        next_sync = 0.0
        while not stop.is_set():
            if config.server_url and time.monotonic() >= next_sync:
                try:
                    pushed, pulled = sync_now(db, config)
                except SyncError as exc:
                    log(f"wake agent: sync failed, will retry: {exc}")
                else:
                    if pushed or pulled:
                        log(f"wake agent: pushed {pushed}, pulled {pulled}")
                next_sync = time.monotonic() + config.sync_seconds
            for task in run_once(db, config, owner=config.origin):
                log(f"wake agent: {task.id[:8]} ({task.backend}) fired")
            if args.once:
                break
            stop.wait(config.poll_seconds)
    return 0


def _cmd_serve(args: argparse.Namespace, config: WakeConfig, log: Logger) -> int:
    if args.port:
        config.port = args.port
    if args.bind:
        config.bind = args.bind
    if not config.api_key and config.bind not in ("127.0.0.1", "localhost", "::1"):
        log(
            f"wake serve: warning: listening on {config.bind} with no API_KEY set; "
            "anything that can reach this port can schedule a shell command"
        )
    with _open_db(config) as db:
        server = WakeServer(db, config)
        log(
            f"wake serve: listening on {config.bind}:{server.port}, "
            f"polling every {config.poll_seconds}s"
        )

        def _stop(signum: int, frame: FrameType | None) -> None:
            log("wake serve: shutting down")
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        server.serve_forever()
        server.close()
    return 0


def _cmd_fire(args: argparse.Namespace, config: WakeConfig, log: Logger) -> int:
    with _open_db(config) as db:
        task = db.get(args.id)
        if task is None:
            print(f"wake: error: no such task: {args.id}", file=sys.stderr)
            return 1
        if task.backend == "rtcwake":
            backends.arm_rtcwake(task)
            db.mark_armed(task.id)
            log(f"armed rtcwake for {task.id}")
            return 0
        before = task.rev
        try:
            backends.fire(task, config)
        except BackendError as exc:
            if db.mark_failed(task.id, str(exc), expect_rev=before) is None:
                log(f"wake: {task.id} re-armed itself while failing; leaving it scheduled")
            print(f"wake: error: {exc}", file=sys.stderr)
            return 1
        if db.mark_fired(task.id, expect_rev=before) is None:
            log(f"fired {task.id}; it re-armed itself, leaving it scheduled")
        else:
            log(f"fired {task.id}")
        if task.then_do:
            # Deliberately the same call the scheduler makes. A manual
            # `wake fire` that skipped the power sequence would prove nothing
            # about the unattended run it is meant to rehearse.
            log(f"wake: {finish_power(db, config, task, succeeded=True)}")
    _autosync(config, log)
    return 0


__all__ = ["main"]
