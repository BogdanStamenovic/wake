"""Turn a ``--at`` string into a Unix epoch timestamp, and ``--every`` into a period."""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime

_RELATIVE = re.compile(r"^\+(\d+)(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

_EVERY = re.compile(r"^(\d+)(s|m|h|d|w)?$")
_EVERY_ALIASES = {"hourly": 3600.0, "daily": 86400.0, "weekly": 604800.0}

# A recurrence shorter than this is a busy loop wearing a scheduler's clothes:
# the firing loop polls every 5s by default and each fire runs a real command
# with a 300s timeout, so anything sub-minute would overlap itself. cron's own
# floor is a minute for the same reason. Sub-minute work belongs in a loop
# inside the task's command, where it costs one process instead of one row
# rewrite and one sync push per iteration.
MIN_REPEAT_SECONDS = 60.0


class WhenError(Exception):
    """Raised when a ``--at`` or ``--every`` value cannot be parsed."""


def parse_when(text: str, *, now: float | None = None) -> float:
    """Accept epoch seconds, ISO 8601, or a ``+<N><s|m|h|d>`` relative offset."""
    text = text.strip()
    base = time.time() if now is None else now

    relative = _RELATIVE.match(text)
    if relative:
        amount, unit = relative.groups()
        return base + int(amount) * _UNIT_SECONDS[unit]
    if text.startswith(("+", "-")):
        # Bail out before float() gets it. `+5` is a plausible typo for `+5m`,
        # and float("+5") is a valid epoch second -- five past midnight in
        # 1970 -- so without this the task is accepted and fires immediately.
        raise WhenError(
            f"{text!r} looks like a relative offset but has no unit; "
            "use +<N> with s, m, h or d"
        )

    try:
        return float(text)
    except ValueError:
        pass

    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise WhenError(
            f"could not parse {text!r} as epoch seconds, ISO 8601, or +<N>[s|m|h|d]"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC).timestamp()


def parse_every(text: str) -> float:
    """Accept ``<N>[s|m|h|d|w]``, bare seconds, or hourly/daily/weekly.

    A period, not a calendar rule. "Every day at 06:00" is expressed as an
    anchor (``--at``) plus a 24-hour period, which is exact in UTC and drifts
    by an hour across a DST boundary in a local timezone -- see the
    Limitations section of the README, and ``next_occurrence`` for why the
    anchor's phase is what survives.
    """
    text = text.strip().lower()
    if not text:
        raise WhenError("--every needs a period, e.g. 1d, 12h, 30m or daily")

    seconds = _EVERY_ALIASES.get(text)
    if seconds is None:
        match = _EVERY.match(text)
        if match is None:
            raise WhenError(
                f"could not parse {text!r} as a period; "
                "use <N>[s|m|h|d|w], bare seconds, or hourly/daily/weekly"
            )
        amount, unit = match.groups()
        seconds = float(int(amount) * _UNIT_SECONDS[unit or "s"])

    if seconds < MIN_REPEAT_SECONDS:
        raise WhenError(
            f"--every {text!r} is {seconds:.0f}s; the shortest period wake will "
            f"schedule is {MIN_REPEAT_SECONDS:.0f}s"
        )
    return seconds


def next_occurrence(at: float, repeat_seconds: float, *, now: float) -> float:
    """The first occurrence strictly after ``now``, keeping the anchor's phase.

    Whole periods are added to the original ``at`` rather than to ``now``, so a
    task anchored at 06:00 stays at 06:00. That is also the whole missed-run
    policy, and it falls out of the arithmetic rather than being a separate
    rule: a machine that was off for three days comes up with one occurrence
    already overdue, fires it once, and this then skips the other two. One
    catch-up run and back on schedule -- the same choice systemd's
    ``Persistent=true`` makes, and for the same reason. Adding the period to
    ``now`` instead would let every outage permanently shift the schedule;
    firing every missed occurrence would turn a long weekend into a burst of
    back-to-back runs at the worst possible moment.
    """
    if repeat_seconds <= 0:
        raise WhenError(f"a period must be positive, not {repeat_seconds}")
    if at > now:
        return at
    return at + (math.floor((now - at) / repeat_seconds) + 1) * repeat_seconds


def format_every(seconds: float | None) -> str:
    """Render a period back as the shortest exact spelling, for ``wake list``."""
    if not seconds:
        return "-"
    for unit in ("w", "d", "h", "m"):
        size = _UNIT_SECONDS[unit]
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:.0f}s"
