"""Config precedence: defaults, then the env file, then the environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from wake.config import ConfigError, load_config, read_env_file


@pytest.fixture(autouse=True)
def no_ambient_wake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("WAKE_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_when_nothing_is_configured(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.env")
    assert config.role == "device"
    assert config.port == 8788
    assert config.origin, "origin falls back to the hostname"


def test_reads_a_key_value_file(tmp_path: Path) -> None:
    path = tmp_path / "wake.env"
    path.write_text(
        "# a comment\n\n"
        "ROLE=server\n"
        "PORT = 9999\n"
        'API_KEY="quoted secret"\n'
        "SERVER_URL=http://example:8788/\n"
    )
    config = load_config(path)
    assert config.role == "server"
    assert config.port == 9999
    assert config.api_key == "quoted secret"
    assert config.server_url == "http://example:8788", "trailing slash is trimmed"


def test_the_environment_overrides_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wake.env"
    path.write_text("ROLE=server\nPORT=1111\n")
    monkeypatch.setenv("WAKE_PORT", "2222")
    config = load_config(path)
    assert config.role == "server"
    assert config.port == 2222


def test_a_line_without_an_equals_sign_names_the_line_number(tmp_path: Path) -> None:
    path = tmp_path / "wake.env"
    path.write_text("ROLE=server\nnonsense\n")
    with pytest.raises(ConfigError, match=":2:"):
        read_env_file(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [("ROLE", "wizard"), ("PORT", "eight"), ("POLL_SECONDS", "soon"), ("SYNC_SECONDS", "x")],
)
def test_unusable_values_are_refused(
    key: str, value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(f"WAKE_{key}", value)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.env")


def test_wake_config_itself_is_not_read_as_a_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAKE_CONFIG", str(tmp_path / "absent.env"))
    config = load_config()
    assert config.role == "device"


def test_db_path_expands_a_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAKE_DB_PATH", "~/somewhere/wake.db")
    config = load_config(tmp_path / "absent.env")
    assert "~" not in str(config.db_path)


# -- pinned defaults --------------------------------------------------------
# Each of these asserts the property the default exists for as well as its
# value: a mutation test showed that checking a config value against the
# constant that produced it passes no matter what that constant becomes.


def test_the_server_listens_on_all_interfaces_by_default(tmp_path: Path) -> None:
    """Loopback here would leave every device unable to reach the server."""
    config = load_config(tmp_path / "absent.env")
    assert config.bind == "0.0.0.0"
    assert config.bind not in ("127.0.0.1", "localhost", "::1")


def test_the_default_port_is_8788(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.env")
    assert config.port == 8788
    assert 1024 < config.port < 65536, "must be unprivileged and a real port"


def test_the_firing_period_is_positive(tmp_path: Path) -> None:
    """Zero is a busy loop that pegs a core, not a fast scheduler."""
    config = load_config(tmp_path / "absent.env")
    assert config.poll_seconds == 5.0
    assert config.poll_seconds > 0


def test_the_sync_period_is_positive_and_slower_than_firing(tmp_path: Path) -> None:
    """Reconciling is the network-bound half; it must not outpace firing."""
    config = load_config(tmp_path / "absent.env")
    assert config.sync_seconds == 60.0
    assert config.sync_seconds > 0
    assert config.sync_seconds > config.poll_seconds


def test_the_environment_beats_a_config_file_that_pins_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WAKE_DB_PATH is the supported way to point a command at a scratch DB."""
    config_file = tmp_path / "wake.env"
    config_file.write_text(f"ROLE=device\nDB_PATH={tmp_path / 'pinned.db'}\n")
    monkeypatch.setenv("WAKE_DB_PATH", str(tmp_path / "scratch.db"))
    assert load_config(config_file).db_path == tmp_path / "scratch.db"


def test_the_mac_is_read_from_both_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "wake.env"
    config_file.write_text("MAC=00:00:5e:00:53:2a\n")
    assert load_config(config_file).mac == "00:00:5e:00:53:2a"
    monkeypatch.setenv("WAKE_MAC", "00:00:5e:00:53:ff")
    assert load_config(config_file).mac == "00:00:5e:00:53:ff"
