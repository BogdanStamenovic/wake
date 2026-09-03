"""The one record type wake moves around: a task to fire at a time."""

from __future__ import annotations

from dataclasses import dataclass

BACKENDS = ("shell", "wol", "rtcwake", "notify", "call")
THEN_ACTIONS = ("", "poweroff")
STATUSES = ("pending", "armed", "fired", "failed", "cancelled")


@dataclass
class Task:
    """A single wake-up task.

    ``rev`` is this row's position in the database's monotonic revision
    counter -- the sync cursor. It has nothing to do with wall-clock time,
    which is why it is separate from ``updated_at``.

    ``owner`` decides which machine fires it: the empty string means "the
    server", anything else is an ``origin`` name. One sentence, and it is the
    whole scheduling rule -- see ``server.run_once``.
    """

    id: str
    task: str
    at: float
    backend: str
    target: str | None
    status: str
    origin: str
    created_at: float
    updated_at: float
    rev: int
    owner: str = ""
    then_do: str = ""  # "" or "poweroff": what to do to the machine afterwards
    timeout_seconds: float | None = None  # per-task override for the shell timeout
    fired_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "task": self.task,
            "at": self.at,
            "backend": self.backend,
            "target": self.target,
            "status": self.status,
            "origin": self.origin,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "rev": self.rev,
            "owner": self.owner,
            "then_do": self.then_do,
            "timeout_seconds": self.timeout_seconds,
            "fired_at": self.fired_at,
            "error": self.error,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> Task:
        """Rebuild a task from a peer's JSON.

        ``owner`` is tolerated as missing so a device still running 0.1.0 can
        push to an upgraded server; everything else is required, because a row
        without it is not a task.
        """
        return Task(
            id=str(data["id"]),
            task=str(data["task"]),
            at=_as_float(data["at"]),
            backend=str(data["backend"]),
            target=str(data["target"]) if data.get("target") is not None else None,
            status=str(data["status"]),
            origin=str(data["origin"]),
            created_at=_as_float(data["created_at"]),
            updated_at=_as_float(data["updated_at"]),
            rev=int(_as_float(data["rev"])),
            owner=str(data.get("owner") or ""),
            then_do=str(data.get("then_do") or ""),
            timeout_seconds=(
                _as_float(data["timeout_seconds"])
                if data.get("timeout_seconds") is not None
                else None
            ),
            fired_at=_as_float(data["fired_at"]) if data.get("fired_at") is not None else None,
            error=str(data["error"]) if data.get("error") is not None else None,
        )


def _as_float(value: object) -> float:
    """Narrow one JSON scalar to a float, so ``from_dict`` needs no ``type: ignore``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"expected a number, got {value!r}")
    return float(value)
