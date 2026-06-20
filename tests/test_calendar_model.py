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
    Health,
    bucket_cell,
    cadence_interval_usec,
    compute_gaps,
    parse_projection,
    parse_run_journal,
    projection_iterations,
    summarize,
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


# -- F6: now-boundary ownership ----------------------------------------------
#
# A slot landing EXACTLY at `now` must be owned by the future side (silent),
# matching how _on_cal_projection splits projected events with the strict
# `s > now` test. Before F6 the gap walk used `slot > now` (so slot == now was
# JUDGED as a possible gap), while the projection split used `s > now` (so the
# same instant was NOT drawn as projected) — the instant fell through both,
# which is the off-by-one the QA disposition flagged. After F6 the gap walk
# skips on `slot >= now`, so slot == now is future-owned on both sides.


def test_slot_exactly_at_now_is_not_a_gap():
    base = 1_781_000_000_000_000
    # Three slots; the MIDDLE one ran (so it splits the region), and `now` lands
    # EXACTLY on the THIRD slot. The boundary slot must be future-owned (silent),
    # so the only gap is the first slot. The ran middle slot guarantees the third
    # slot can't merge into the first's region — making this discriminating:
    # before F6 (slot > now) the third slot was JUDGED and, having no run, became
    # a SECOND standalone gap at base+2*DAY; after F6 (slot >= now) it is skipped.
    slots = [base, base + DAY, base + 2*DAY]
    runs = [run(base + DAY)]                       # middle ran → splits regions
    gaps = compute_gaps(slots, runs, "d.timer", coverage_start=base,
                        now=base + 2*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    assert [g.when for g in gaps] == [base]        # boundary slot is NOT a gap


# -- F5: _collapse robustness (duplicate slots + split case) ------------------
#
# WATCH-1 (formerly "unreachable") is now LIVE: F4's multi-trigger union and DST
# fall-back both produce DUPLICATE µs instants in one slot list, and second-
# flooring (int(timestamp())) can coincide two near-identical instants. The old
# _collapse keyed adjacency by slot VALUE, so a duplicate `when` degenerated the
# region merge. F5 dedups with sorted(set(slots)) before walking, so duplicates
# collapse to one instant and the index-based adjacency stays correct.


def test_collapse_split_case_ran_slot_between_two_missed():
    # The split case the QA disposition calls out: a RAN slot sitting between two
    # MISSED slots must break the region — the misses are NOT adjacent in the
    # schedule (a kept slot separates them), so they must surface as TWO separate
    # gap events, not one collapsed region.
    base = 1_781_000_000_000_000
    slots = [base, base + DAY, base + 2*DAY]
    runs = [run(base + DAY)]                       # middle slot ran; ends ran
    gaps = compute_gaps(slots, runs, "d.timer", coverage_start=base,
                        now=base + 3*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    # Two distinct single-miss regions, one each side of the ran slot.
    assert [(g.when, g.count) for g in gaps] == [(base, 1), (base + 2*DAY, 1)]


def test_collapse_tolerates_duplicate_slots():
    # Duplicate µs instants in the slot list (F4 union / DST fall-back) must not
    # break the merge. Before F5 the value-keyed idx map collapsed the duplicate
    # to a single index, so a contiguous miss run could mis-count or raise. After
    # F5 sorted(set(...)) folds the duplicate first → ONE region of count 2.
    base = 1_781_000_000_000_000
    slots = [base, base, base + DAY]              # base appears twice (a duplicate)
    gaps = compute_gaps(slots, [], "d.timer", coverage_start=base,
                        now=base + 2*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    # Deduped to {base, base+DAY}; both missed and schedule-adjacent → one region.
    assert len(gaps) == 1
    assert gaps[0].when == base and gaps[0].count == 2


def test_dst_fall_back_duplicate_instants_do_not_break_collapse():
    # A DST fall-back makes systemd-analyze emit the SAME wall-clock hour twice;
    # projected to UTC the two instants are distinct, but a timer whose accuracy
    # floors to the second can yield two slots at the same µs. parse_projection
    # here returns a list with a literal duplicate (the realistic shape), and
    # compute_gaps must dedup-then-collapse without raising. This is the model
    # half of the F7 "DST projection-parse case".
    base = 1_781_000_000_000_000
    dst_dup = [base, base, base + DAY, base + DAY]  # two pairs of duplicates
    gaps = compute_gaps(dst_dup, [], "d.timer", coverage_start=base,
                        now=base + 2*DAY, tolerance_usec=GAP_TOLERANCE_USEC)
    assert len(gaps) == 1                          # {base, base+DAY} adjacent
    assert gaps[0].count == 2


def test_parse_projection_dst_fall_back_block_parses_all_lines():
    # A real DST fall-back `systemd-analyze` block: 01:30 occurs twice (PDT then
    # PST). Both "(in UTC):" lines are an hour apart in UTC and BOTH must parse —
    # the parser reads UTC lines only, so it is immune to the repeated wall-clock
    # hour. Pins that parse_projection handles the DST shape (F7 DST case).
    dst_block = (
        "  Original form: *-*-* 01:30:00\n"
        "    Next elapse: Sun 2026-11-01 01:30:00 PDT\n"
        "       (in UTC): Sun 2026-11-01 08:30:00 UTC\n"
        "   Iteration #2: Sun 2026-11-01 01:30:00 PST\n"
        "       (in UTC): Sun 2026-11-01 09:30:00 UTC\n"
    )
    out = parse_projection(dst_block)
    # 08:30 and 09:30 UTC are exactly one hour apart — both kept, sorted.
    assert len(out) == 2
    assert out[1] - out[0] == 3_600_000_000


# -- F2 (model half): parse_projection skips a malformed (in UTC) line --------
#
# The fan-in hang's root cause: parse_projection built datetime() straight from
# the regex, which admits an out-of-range date (2026-13-45) → ValueError → the
# whole fetch raises → the calendar barrier never releases. F2 wraps the
# datetime() construction in try/except ValueError + continue, exactly like
# parse_run_journal already does for a bad line, so one poison line is skipped
# and the rest of the block still parses.


def test_parse_projection_skips_malformed_utc_line():
    # Month 13 / day 45 matches the _UTC_TS regex shape but is not a real date —
    # datetime() would raise ValueError. The malformed line must be SKIPPED (not
    # raise), and the surrounding valid lines must still parse.
    text = (
        "       (in UTC): Mon 2026-06-22 13:00:00 UTC\n"
        "       (in UTC): Bad 2026-13-45 99:00:00 UTC\n"   # impossible date/time
        "       (in UTC): Tue 2026-06-23 13:00:00 UTC\n"
    )
    out = parse_projection(text)
    # The two valid instants survive; the malformed one is dropped silently.
    assert out == [1782133200_000000, 1782219600_000000]


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


# -- HEALTH summary (Task 11) ------------------------------------------------
#
# summarize() rolls a flat event list into the top-strip readout the view draws
# and the Phase-2 plasmoid reuses. It lives in the MODEL (not the view) so both
# consumers count outcomes the same way — counts per kind plus a human issues
# list (one "unit @ date" string per failure and per gap region). Pure, so it
# is pinned DIRECTLY here, no Qt needed.

# A real µs epoch with a known UTC date, so the issue-string date is assertable.
# 1781787600 s == 2026-06-18 13:00:00 UTC (verified with datetime — note the
# Task-1 fixture COMMENT calls this 2026-06-22, which is a 4-day-off typo: the
# numeric value parses to the 18th, and the parse test only asserts the integer,
# so the typo was latent. We anchor to the real date here.)
JUN18_USEC = 1781787600_000000


def test_summarize_counts_each_kind():
    # ok = successful runs, failed = failed runs, upcoming = projected/approx,
    # gaps = MISSED SLOTS (sum of each gap event's count, so a collapsed region
    # of 3 misses counts as 3 — a gap event with count>1 stands for that many
    # missed runs, and the health number must reflect reality, not the region
    # count). Mixed list exercises every branch.
    events = [
        CalendarEvent("a.timer", JUN18_USEC, "ran", "success"),
        CalendarEvent("a.timer", JUN18_USEC + 1, "ran", "success"),
        CalendarEvent("a.timer", JUN18_USEC + 2, "ran", "failure"),
        CalendarEvent("b.timer", JUN18_USEC + 3, "gap", count=3),  # 3 missed slots
        CalendarEvent("c.timer", JUN18_USEC + 4, "projected"),
        CalendarEvent("c.timer", JUN18_USEC + 5, "approx"),
    ]
    h = summarize(events)
    assert isinstance(h, Health)
    assert h.ok == 2
    assert h.failed == 1
    assert h.gaps == 3          # 1 gap region of count=3 → 3 missed slots
    assert h.upcoming == 2      # projected + approx both count as upcoming


def test_summarize_lists_issues_with_unit_and_date():
    # Each failure and each gap REGION yields one human issue string carrying the
    # unit and the UTC date of the event, so the strip can list "what went wrong
    # and when" without the view re-deriving it. Successes/upcoming are not issues.
    events = [
        CalendarEvent("ok.timer", JUN18_USEC, "ran", "success"),
        CalendarEvent("fail.timer", JUN18_USEC, "ran", "failure"),
        CalendarEvent("gap.timer", JUN18_USEC, "gap", count=2),
        CalendarEvent("soon.timer", JUN18_USEC, "projected"),
    ]
    h = summarize(events)
    assert len(h.issues) == 2  # one per failure + one per gap region; not ok/proj
    joined = " ".join(h.issues)
    assert "fail.timer" in joined and "gap.timer" in joined
    assert "ok.timer" not in joined and "soon.timer" not in joined
    # The UTC date of the events is embedded so the issue is time-anchored.
    assert "2026-06-18" in joined


def test_summarize_empty_is_all_zero_no_issues():
    # An empty window (no events fetched yet, or a fully clean scope) is the
    # all-zero, no-issues health — the strip then reads as "nothing wrong".
    h = summarize([])
    assert h == Health(ok=0, failed=0, gaps=0, upcoming=0, issues=[])


# -- F7: model purity (mechanically enforced) --------------------------------
#
# calendar_model is the headless, testable core: the Phase-2 plasmoid reuses it,
# so it must NOT import Qt widgets or spawn subprocesses. The module docstring
# and CLAUDE.md both promise this, but nothing enforced it — a future edit could
# reach for a QWidget or subprocess and the promise would rot silently. This
# scans the module's SOURCE for forbidden imports so the purity contract fails
# loudly at test time, not at plasmoid-reuse time.


def test_calendar_model_imports_no_qt_or_subprocess():
    import ast
    import pathlib

    import taskdeck.calendar_model as cm

    source = pathlib.Path(cm.__file__).read_text()
    tree = ast.parse(source)
    # Collect every imported top-level module name (both `import X` and
    # `from X import …`), walking the AST rather than regex so a commented-out or
    # string-literal "import subprocess" never false-positives.
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # Subprocess I/O lives ONLY in systemd_client; widgets ONLY in the view.
    forbidden = {"subprocess", "PySide6", "PyQt5", "PyQt6"}
    leaked = imported & forbidden
    assert not leaked, f"calendar_model must stay pure; leaked imports: {leaked}"
