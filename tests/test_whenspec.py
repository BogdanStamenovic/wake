from __future__ import annotations

import pytest

from wake.whenspec import (
    MIN_REPEAT_SECONDS,
    WhenError,
    format_every,
    next_occurrence,
    parse_every,
    parse_when,
)


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


# -- --every ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("60", 60.0),
        ("90s", 90.0),
        ("30m", 1800.0),
        ("12h", 43200.0),
        ("1d", 86400.0),
        ("2w", 1209600.0),
        ("hourly", 3600.0),
        ("daily", 86400.0),
        ("weekly", 604800.0),
        ("  DAILY  ", 86400.0),
    ],
)
def test_periods_parse(text: str, seconds: float) -> None:
    assert parse_every(text) == seconds


@pytest.mark.parametrize("text", ["", "0", "30s", "59", "-1d", "1y", "every day", "1.5h"])
def test_rejects_a_period_that_is_not_one(text: str) -> None:
    with pytest.raises(WhenError):
        parse_every(text)


def test_the_floor_is_a_minute() -> None:
    """Below it a recurrence is a busy loop: the firing loop polls every 5s."""
    assert parse_every("60") == MIN_REPEAT_SECONDS
    with pytest.raises(WhenError, match="60s"):
        parse_every("59")


def test_a_future_occurrence_is_left_alone() -> None:
    """Firing early -- `wake fire` -- must not consume the scheduled run."""
    assert next_occurrence(1000.0, 86400.0, now=500.0) == 1000.0


def test_the_next_occurrence_is_strictly_after_now() -> None:
    assert next_occurrence(1000.0, 60.0, now=1000.0) == 1060.0


def test_missed_occurrences_collapse_to_one_catch_up() -> None:
    """Off for three days: the overdue row fires once, then skips to today.

    The row is already `pending` with a past `at`, so the catch-up run happens
    whatever this returns. What this decides is that the *other* two missed
    days are dropped rather than fired back to back.
    """
    anchor = 1000.0  # a daily task
    three_days_late = anchor + 3 * 86400.0 + 3600.0
    assert next_occurrence(anchor, 86400.0, now=three_days_late) == anchor + 4 * 86400.0


def test_the_anchor_keeps_its_phase_across_an_outage() -> None:
    """Adding to `now` instead of the anchor would drift 06:00 to whenever."""
    anchor = parse_when("2026-09-04T06:00:00Z")
    late = parse_when("2026-09-07T14:37:11Z")
    assert next_occurrence(anchor, 86400.0, now=late) == parse_when("2026-09-08T06:00:00Z")


def test_a_non_positive_period_is_refused() -> None:
    with pytest.raises(WhenError):
        next_occurrence(1000.0, 0.0, now=2000.0)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(None, "-"), (0.0, "-"), (60.0, "1m"), (5400.0, "90m"), (86400.0, "1d"),
     (604800.0, "1w"), (43200.0, "12h"), (90.0, "90s")],
)
def test_periods_render_back(seconds: float | None, text: str) -> None:
    assert format_every(seconds) == text
