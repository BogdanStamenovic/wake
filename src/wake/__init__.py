"""wake: schedule and fire wake-up tasks across a server/device pair."""

from __future__ import annotations

__version__ = "0.1.0"

from .backends import BackendError
from .cli import main
from .db import WakeError

__all__ = [
    "BackendError",
    "WakeError",
    "__version__",
    "main",
]
