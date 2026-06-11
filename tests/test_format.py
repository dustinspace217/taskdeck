"""Tests for µs-epoch → human time rendering.

`now` is injected everywhere — never read from the wall clock inside the
function — so these tests are deterministic forever.
"""
from datetime import datetime

from taskdeck.models import format_delta, format_when

NOW = datetime(2026, 6, 10, 19, 0, 0)  # fixed reference: Wed Jun 10 2026 19:00 local


def usec(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def test_none_renders_dash():
    assert format_when(None, NOW) == "—"


def test_future_same_day():
    ts = usec(datetime(2026, 6, 10, 23, 10, 0))
    assert format_when(ts, NOW) == "today 23:10 (in 4h 10m)"


def test_future_this_week_uses_weekday():
    ts = usec(datetime(2026, 6, 15, 6, 1, 0))
    assert format_when(ts, NOW) == "Mon 06:01 (in 4d 11h)"


def test_past_renders_ago():
    ts = usec(datetime(2026, 6, 10, 17, 11, 25))
    assert format_when(ts, NOW) == "today 17:11 (1h 48m ago)"


def test_far_dates_use_month_day():
    ts = usec(datetime(2026, 7, 20, 12, 0, 0))
    assert format_when(ts, NOW) == "Jul 20 12:00 (in 39d)"


def test_past_this_week_uses_weekday():
    # Past-direction twin of the future weekday test: kills the mutant where
    # abs(delta) silently becomes delta and all past dates render month-day.
    ts = usec(datetime(2026, 6, 7, 9, 0, 0))
    assert format_when(ts, NOW) == "Sun 09:00 (3d 10h ago)"


def test_far_past_uses_month_day():
    ts = usec(datetime(2026, 4, 1, 12, 0, 0))
    assert format_when(ts, NOW) == "Apr 01 12:00 (70d ago)"


def test_six_day_boundary_falls_to_month_day():
    # Exactly 6 days is OUTSIDE the weekday window (strict <), by design.
    ts = usec(datetime(2026, 6, 16, 19, 0, 0))
    assert format_when(ts, NOW) == "Jun 16 19:00 (in 6d 0h)"


def test_format_delta_units():
    assert format_delta(45) == "45s"
    assert format_delta(60 * 5 + 30) == "5m"
    assert format_delta(3600 * 4 + 60 * 10) == "4h 10m"
    assert format_delta(86400 * 4 + 3600 * 11) == "4d 11h"
    assert format_delta(86400 * 39 + 3600 * 5) == "39d"


def test_format_delta_threshold_boundaries():
    # format_delta's whole job is its breakpoints — pin each one exactly.
    assert format_delta(59) == "59s"
    assert format_delta(60) == "1m"
    assert format_delta(3599) == "59m"
    assert format_delta(3600) == "1h 0m"
    assert format_delta(86399) == "23h 59m"
    assert format_delta(86400) == "1d 0h"
    assert format_delta(86400 * 35 - 1) == "34d 23h"
    assert format_delta(86400 * 35) == "35d"
