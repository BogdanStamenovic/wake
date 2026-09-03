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
