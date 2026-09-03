"""Runtime settings, read from the environment or a ``KEY=value`` env file.

The env-file format mirrors ``profiler``'s ``read_env_file`` -- simple,
dependency-free, and already the house convention for a small daemon's
config on this box.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "wake" / "wake.env"
DEFAULT_PORT = 8788


class ConfigError(ValueError):
    """The environment does not describe a usable configuration."""


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ConfigError(f"{path}:{number}: expected KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass
class WakeConfig:
    role: str = "device"  # "device" or "server"
    db_path: Path = Path.home() / ".local" / "state" / "wake" / "wake.db"
    origin: str = ""  # this machine's name, as recorded on every task it creates
    server_url: str = ""  # e.g. http://100.x.x.x:8788, used by `wake sync` on a device
    api_key: str = ""  # shared secret between devices and the server's HTTP API
    bind: str = "0.0.0.0"  # the server's listen address
    port: int = DEFAULT_PORT
    poll_seconds: float = 5.0
    sync_seconds: float = 60.0  # how often `wake agent` reconciles with the server
    poweroff_allow_agents: str = ""  # agent --name values that never block a poweroff
    poweroff_allow_match: str = ""  # regex; agent command lines matching it are furniture
    wol_broadcast: str = ""  # override the 255.255.255.255 default for the wol backend
    hotline_ios_url: str = ""  # e.g. http://100.72.2.62:8789, used by the notify backend
    hotline_ios_key: str = ""

    def __post_init__(self) -> None:
        if not self.origin:
            self.origin = socket.gethostname()


def load_config(path: Path | None = None) -> WakeConfig:
    """Build the config: an optional file, then the environment on top."""
    values: dict[str, str] = {}
    config_path = path or Path(os.environ.get("WAKE_CONFIG", DEFAULT_CONFIG_PATH))
    if config_path.exists():
        values.update(read_env_file(config_path))
    for key, value in os.environ.items():
        if key.startswith("WAKE_") and key != "WAKE_CONFIG":
            values[key[len("WAKE_") :]] = value

    config = WakeConfig()
    if "ROLE" in values:
        role = values["ROLE"].strip().lower()
        if role not in ("device", "server"):
            raise ConfigError(f"ROLE must be 'device' or 'server', not {values['ROLE']!r}")
        config.role = role
    if "DB_PATH" in values:
        config.db_path = Path(values["DB_PATH"]).expanduser()
    if "ORIGIN" in values:
        config.origin = values["ORIGIN"]
    if "SERVER_URL" in values:
        config.server_url = values["SERVER_URL"].rstrip("/")
    if "API_KEY" in values:
        config.api_key = values["API_KEY"]
    if "BIND" in values:
        config.bind = values["BIND"]
    if "PORT" in values:
        try:
            config.port = int(values["PORT"])
        except ValueError as exc:
            raise ConfigError(f"PORT must be numeric, not {values['PORT']!r}") from exc
    if "POLL_SECONDS" in values:
        try:
            config.poll_seconds = float(values["POLL_SECONDS"])
        except ValueError as exc:
            raise ConfigError(
                f"POLL_SECONDS must be numeric, not {values['POLL_SECONDS']!r}"
            ) from exc
    if "SYNC_SECONDS" in values:
        try:
            config.sync_seconds = float(values["SYNC_SECONDS"])
        except ValueError as exc:
            raise ConfigError(
                f"SYNC_SECONDS must be numeric, not {values['SYNC_SECONDS']!r}"
            ) from exc
    if "POWEROFF_ALLOW_AGENTS" in values:
        config.poweroff_allow_agents = values["POWEROFF_ALLOW_AGENTS"]
    if "POWEROFF_ALLOW_MATCH" in values:
        try:
            re.compile(values["POWEROFF_ALLOW_MATCH"])
        except re.error as exc:
            raise ConfigError(f"POWEROFF_ALLOW_MATCH is not a valid regex: {exc}") from exc
        config.poweroff_allow_match = values["POWEROFF_ALLOW_MATCH"]
    if "WOL_BROADCAST" in values:
        config.wol_broadcast = values["WOL_BROADCAST"]
    if "HOTLINE_IOS_URL" in values:
        config.hotline_ios_url = values["HOTLINE_IOS_URL"].rstrip("/")
    if "HOTLINE_IOS_KEY" in values:
        config.hotline_ios_key = values["HOTLINE_IOS_KEY"]
    return config
