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

from taskdeck.calendar_model import (
    CELL_DRAW_MAX_PER_WINDOW,
    GAP_TOLERANCE_USEC,
    CalendarEvent,
    bucket_cell,
    cadence_interval_usec,
    compute_gaps,
    parse_projection,
    parse_run_journal,
    projection_iterations,
)
from taskdeck.systemd_client import ScheduleInfo

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


# -- Task 3: compute_gaps ----------------------------------------------------
#
# A "gap" is a scheduled slot (from parse_projection over a PAST --base-time)
# that has no actual run nearby. The contract is exact, not heuristic: only
# slots inside the journal's coverage window [coverage_start, now] are judged —
# outside it there is simply no run data, which the view renders as "—" (no
# data), never as a miss. Contiguous misses collapse into one event carrying a
# count so a long outage reads as one region, not N separate glyphs.

DAY = 86_400_000_000  # µs in a day


def run(t):
    """Build a successful 'ran' event at µs-epoch `t` (test helper)."""
    return CalendarEvent(unit="d.timer", when=t, kind="ran", result="success")


def test_gap_when_a_scheduled_slot_has_no_run():
    base = 1_781_000_000_000_000
    slots = [base, base + DAY, base + 2*DAY]          # three daily slots
    runs = [run(base), run(base + 2*DAY)]             # middle one missing
    gaps = compute_gaps(slots, runs, "d.timer", coverage_start=base,
                        now=base + 3*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    assert len(gaps) == 1
    assert gaps[0].kind == "gap" and gaps[0].when == base + DAY and gaps[0].count == 1


def test_contiguous_misses_collapse_to_one_region_with_count():
    base = 1_781_000_000_000_000
    slots = [base + k*DAY for k in range(5)]
    runs = [run(base), run(base + 4*DAY)]             # 3 in a row missing
    gaps = compute_gaps(slots, runs, "d.timer", coverage_start=base,
                        now=base + 5*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    assert len(gaps) == 1 and gaps[0].count == 3 and gaps[0].when == base + DAY


def test_no_gap_outside_journal_coverage():
    # Slots before coverage_start are "no data", never a gap.
    base = 1_781_000_000_000_000
    slots = [base, base + DAY, base + 2*DAY]
    gaps = compute_gaps(slots, [], "d.timer", coverage_start=base + DAY,
                        now=base + 3*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    assert all(g.when >= base + DAY for g in gaps)    # the pre-coverage slot is silent


def test_no_gap_in_the_future():
    base = 1_781_000_000_000_000
    slots = [base, base + DAY]
    gaps = compute_gaps(slots, [run(base)], "d.timer", coverage_start=base,
                        now=base + DAY // 2, tolerance_usec=GAP_TOLERANCE_USEC)
    assert gaps == []                                 # base+DAY is in the future


def test_run_within_tolerance_is_not_a_gap():
    base = 1_781_000_000_000_000
    slots = [base]
    runs = [run(base + GAP_TOLERANCE_USEC - 1)]
    assert compute_gaps(slots, runs, "d.timer", base, base + DAY, GAP_TOLERANCE_USEC) == []


# -- Task 4: cadence interval, projection-N cap, cell bucketing ---------------
#
# These three pure helpers size and aggregate the projection fan-out. The view
# never owns thresholds (Phase-2 plasmoid reuses this module), so the interval
# math, the iteration cap, and the cell collapse all live here. The interval is
# derived from the SAME normalized OnCalendar families classify_cadence already
# recognizes — one classifier, two consumers (the human label and the interval).


def test_cadence_interval_common_shapes():
    daily = ScheduleInfo(calendar=("*-*-* 00:00:00",), monotonic=())
    weekly = ScheduleInfo(calendar=("Mon *-*-* 06:00:00",), monotonic=())
    quarter = ScheduleInfo(calendar=("*-*-* 00,06,12,18:00:00",), monotonic=())
    mono = ScheduleInfo(calendar=(), monotonic=("OnBootUSec=12h",))
    assert cadence_interval_usec(daily) == 86_400_000_000
    assert cadence_interval_usec(weekly) == 7 * 86_400_000_000
    assert cadence_interval_usec(quarter) == 86_400_000_000 // 4
    assert cadence_interval_usec(mono) is None      # monotonic → no calendar interval

    # Multi-trigger timer: the docstring (calendar_model.py:247-249) promises the
    # SMALLEST calendar interval, since projecting at the tightest cadence never
    # under-counts the others. Daily (86.4e9 µs) + minutely (60e6 µs) → 60e6.
    # Pins min() so a regression to max()/first-wins is caught.
    multi = ScheduleInfo(calendar=("*-*-* 00:00:00", "*-*-* *:*:00"), monotonic=())
    assert cadence_interval_usec(multi) == 60_000_000

    # Raw-fallback: an OnCalendar expression _classify_calendar can't recognize
    # (bare "HH:MM" comes back unchanged, not as a named cadence) contributes no
    # honest interval, so a lone unclassifiable trigger yields None.
    raw_only = ScheduleInfo(calendar=("13:17",), monotonic=())
    assert cadence_interval_usec(raw_only) is None

    # Empty ScheduleInfo (no triggers at all) → None, same no-cadence-to-project
    # path as monotonic-only.
    empty = ScheduleInfo(calendar=(), monotonic=())
    assert cadence_interval_usec(empty) is None


def test_projection_iterations_caps_high_frequency():
    span = 30 * 86_400_000_000                       # a month
    assert projection_iterations(86_400_000_000, span) in (31, 32)  # ~daily
    assert projection_iterations(60_000_000, span) <= CELL_DRAW_MAX_PER_WINDOW  # minutely → capped
    assert projection_iterations(None, span) == 0   # monotonic → no projection


def test_bucket_cell_counts_and_flags_failure():
    evs = [CalendarEvent("t", 1, "ran", "success"), CalendarEvent("t", 2, "ran", "failure")]
    assert bucket_cell(evs) == (2, 1)
    # Pin the load-bearing `kind == "ran"` guard (calendar_model.py:320): only a
    # 'ran' event is a run outcome, so gaps/projections never count as failures.
    # Without these, dropping the kind check from the predicate still passes the
    # assertion above — verified the mutation slips through, so this is what
    # makes the guard non-vacuous. The 'gap'/'projected' events carry
    # result='failure' on purpose: the guard must reject them on KIND, not on a
    # blank result, so a future predicate regression to `result == "failure"`
    # fails here loudly.
    non_runs = [
        CalendarEvent("t", 1, "gap", "failure"),
        CalendarEvent("t", 2, "projected", "failure"),
    ]
    assert bucket_cell(non_runs) == (2, 0)
