"""Shared fixtures. Every test gets its own database and its own config."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from wake.config import WakeConfig
from wake.db import WakeDB


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WakeDB]:
    with WakeDB(tmp_path / "wake.db") as handle:
        yield handle


@pytest.fixture
def config(tmp_path: Path) -> WakeConfig:
    return WakeConfig(role="device", db_path=tmp_path / "wake.db", origin="testbox")
