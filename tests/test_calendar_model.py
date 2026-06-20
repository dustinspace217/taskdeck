"""Tests for the pure calendar logic (taskdeck.calendar_model).

Task 1 covers CalendarEvent (the frozen dataclass the view draws) and
parse_projection (turning a `systemd-analyze calendar` block into µs epochs).
Both are pure functions over already-fetched text — no Qt, no subprocess — so
these tests run with no fixtures and no offscreen widget setup.
"""
from taskdeck.calendar_model import CalendarEvent, parse_projection

# A real `systemd-analyze calendar --iterations=N` block. Each iteration prints
# a localtime line (PDT here) followed by an indented "(in UTC):" line. The
# parser must read the UTC line ONLY — re-parsing the PDT line as naive-local
# would drift the epoch by the offset (here -7h), corrupting the overlay.
PROJ = """  Original form: *-*-* 06:00:00
Normalized form: *-*-* 06:00:00
    Next elapse: Mon 2026-06-22 06:00:00 PDT
       (in UTC): Mon 2026-06-22 13:00:00 UTC
       From now: ...
   Iteration #2: Tue 2026-06-23 06:00:00 PDT
       (in UTC): Tue 2026-06-23 13:00:00 UTC
"""


def test_parse_projection_uses_utc_line_to_usec():
    out = parse_projection(PROJ)
    # 2026-06-22 13:00:00 UTC == 1782133200 s == 1782133200_000000 µs;
    # 2026-06-23 13:00:00 UTC == 1782219600 s (both verified via `date -u`).
    assert out == [1782133200_000000, 1782219600_000000]


def test_parse_projection_empty_block_is_empty():
    assert parse_projection("Original form: x\nNormalized form: x\n") == []


def test_calendar_event_is_frozen_with_defaults():
    e = CalendarEvent(unit="a.timer", when=1, kind="projected")
    assert e.result == "" and e.count == 1 and e.exit_status is None
