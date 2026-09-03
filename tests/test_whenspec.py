from __future__ import annotations

import pytest

from wake.whenspec import WhenError, parse_when


def test_relative_offsets_are_added_to_now() -> None:
    assert parse_when("+30s", now=1000.0) == 1030.0
    assert parse_when("+5m", now=1000.0) == 1300.0
    assert parse_when("+2h", now=1000.0) == 8200.0
    assert parse_when("+1d", now=1000.0) == 87400.0


def test_epoch_seconds_pass_through() -> None:
    assert parse_when("1767225600", now=0.0) == 1767225600.0
    assert parse_when("1767225600.5", now=0.0) == 1767225600.5


def test_iso_with_explicit_zulu_is_utc() -> None:
    # 2026-09-04T07:00:00Z
    assert parse_when("2026-09-04T07:00:00Z") == 1788505200.0


def test_iso_with_offset_respects_it() -> None:
    assert parse_when("2026-09-04T09:00:00+02:00") == 1788505200.0


def test_naive_iso_is_read_as_local_time() -> None:
    naive = parse_when("2026-09-04T07:00:00")
    aware = parse_when("2026-09-04T07:00:00+00:00")
    # Only equal if the box happens to sit on UTC; either way it must parse
    # to *some* instant and stay within a day of the UTC reading.
    assert abs(naive - aware) <= 86400


def test_surrounding_whitespace_is_ignored() -> None:
    assert parse_when("  +5m  ", now=0.0) == 300.0


@pytest.mark.parametrize("text", ["", "tomorrow", "+5", "+5y", "5m", "++1h", "2026-13-45"])
def test_rejects_unparseable_input(text: str) -> None:
    with pytest.raises(WhenError):
        parse_when(text, now=0.0)
