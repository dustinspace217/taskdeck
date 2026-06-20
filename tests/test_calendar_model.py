"""Tests for the pure calendar logic (taskdeck.calendar_model).

Task 1 covers CalendarEvent (the frozen dataclass the view draws) and
parse_projection (turning a `systemd-analyze calendar` block into µs epochs).
Task 2 adds parse_run_journal (turning a JOB_RESULT-filtered journal dump into
'ran' events, bucketed back to the timer unit). Both are pure functions over
already-fetched text — no Qt, no subprocess — so these tests run offscreen; the
only fixture is a captured `journalctl -o json` dump.
"""
import json
from pathlib import Path

from taskdeck.calendar_model import CalendarEvent, parse_projection, parse_run_journal

# A real journalctl JOB_RESULT=done JOB_RESULT=failed dump (re-captured live).
# parse_run_journal reads these JSON lines; tests below pick a service known to
# be present and assert it buckets back to its (fake) timer name.
FIX = Path(__file__).parent / "fixtures" / "cal_journal.json"

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


# -- Task 2: parse_run_journal ----------------------------------------------


def test_parse_run_journal_buckets_known_services_to_their_timers():
    text = FIX.read_text()
    # Pick a service present in the fixture; map it to a fake timer name.
    # (Replace 'project-board-scan.service' if re-captured on another machine.)
    s2t = {"project-board-scan.service": "project-board-scan.timer"}
    events = parse_run_journal(text, s2t)
    assert events, "fixture has runs for this service"
    assert all(e.kind == "ran" for e in events)
    assert all(e.unit == "project-board-scan.timer" for e in events)
    assert all(e.result in ("success", "failure") for e in events)
    assert all(e.when > 1_700_000_000_000_000 for e in events)  # µs, ~2023+


def test_parse_run_journal_ignores_unknown_units():
    # A record whose unit isn't in the map is dropped, not emitted.
    line = json.dumps({"USER_UNIT": "other.service", "JOB_RESULT": "done",
                       "__REALTIME_TIMESTAMP": "1781787600000000"})
    assert parse_run_journal(line, {"x.service": "x.timer"}) == []


def test_parse_run_journal_maps_failed_and_bytes_message():
    line = json.dumps({"USER_UNIT": "x.service", "JOB_RESULT": "failed",
                       "__REALTIME_TIMESTAMP": "1781787600000000",
                       "MESSAGE": [72, 105]})  # byte-array MESSAGE must not crash
    out = parse_run_journal(line, {"x.service": "x.timer"})
    assert out == [CalendarEvent(
        unit="x.timer", when=1781787600000000, kind="ran", result="failure")]
