"""Turn a ``--at`` string into a Unix epoch timestamp."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

_RELATIVE = re.compile(r"^\+(\d+)(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class WhenError(Exception):
    """Raised when a ``--at`` value cannot be parsed."""


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
