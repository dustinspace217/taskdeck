# Calendar View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third top-level **Calendar** view to Task Deck that shows systemd timer schedules — journal-mined past runs (with success/failure), exactly-projected future runs, and exact missed-run gaps — as Day / Week / Month layouts plus a by-timer matrix.

**Architecture:** A pure, headless `calendar_model.py` turns three live data sources (`list-timers`, `systemd-analyze calendar --base-time`, one `journalctl JOB_RESULT=…` query) into a flat list of `CalendarEvent`s; a custom-painted `calendar_view.py` QWidget draws them four ways; the widget rides in a new `QStackedWidget` page selected from the existing `View:` dropdown, reusing the existing detail tabs via a small selection adapter.

**Tech Stack:** Python 3.14, PySide6 6.11 (Qt Widgets, `QPainter`), pytest + pytest-qt (offscreen), ruff + mypy. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-20-calendar-view-design.md` (read it for the verified data contracts and the adversarial-review findings this plan implements).

## Global Constraints
- **No web, no new heavy deps.** Native Qt Widgets only.
- **`calendar_model.py` is pure** — no Qt *widgets*, no subprocess. Qt core types (`QObject`/`Signal`) are fine in the view, not the model. All thresholds (`GAP_TOLERANCE_USEC`, `CELL_DRAW_MAX`) live in the model, never the view (Phase-2 plasmoid reuse).
- **All times are µs epochs, UTC** (matches `TimerRow.next_usec`). Parse `systemd-analyze`'s `(in UTC):` line, never the local line.
- **Outcome key is the `JOB_RESULT` field** (`done`=success, `failed`=failure). NOT `_SYSTEMD_INVOCATION_ID` (absent from completion records).
- **New request-id kinds are `calproj:{scope}:{unit}` and `caljournal:{scope}`.** NEVER reuse `calendar:` (taken by the Schedule-tab elapse preview).
- **Gap detection is clamped to `[oldest-journal-entry, now]`** — never emit a gap where there is no run data; that renders as `—` (no data), not `⛌`.
- **`MESSAGE` may be a byte array** (list) in journal JSON — guard `isinstance(str)` before string ops.
- Gates: `python3 -m pytest` (offscreen, hermetic), `python3 -m pytest -m realsystemd` (opt-in), `ruff check .`, `mypy taskdeck`. CI runs ruff + pytest.
- Commenting: comment WHAT and WHY (Dustin learns by reading code; comments are also rigor-evidence to peers). Match the density of the existing modules.

---

## File structure

| File | Responsibility |
|---|---|
| `taskdeck/calendar_model.py` | NEW, pure. `CalendarEvent`; `parse_projection`; `parse_run_journal`; `compute_gaps`; `bucket_cell`; `cadence_interval_usec`; `projection_iterations`. |
| `taskdeck/calendar_view.py` | NEW, widget. `CalendarView(QWidget)`: `set_events`, Day/Week/Month/matrix paint, nav + view toggle, `selected = Signal(str)`. |
| `taskdeck/systemd_client.py` | MODIFY. `fetch_cal_projection`, `fetch_cal_journal` (≥15s timeout). |
| `taskdeck/main_window.py` | MODIFY. `QStackedWidget` central; "Calendar" in `view_box`; `_calendar_selection_adapter`; route `calproj:`/`caljournal:` in `_dispatch_finished`. |
| `taskdeck/models.py` | REUSE `classify_cadence`/`ScheduleInfo`. No structural change. |
| `tests/test_calendar_model.py` | NEW. Pure-function tests (the bulk). |
| `tests/test_calendar_view.py` | NEW. Offscreen widget smoke + selection. |
| `tests/test_calendar_integration.py` | NEW. Stacked-swap + adapter in `main_window`. |
| `tests/fixtures/cal_journal.json`, `cal_projection.txt` | NEW. Re-captured live. |

Build order = task order below. Each task ends green (pytest + ruff + mypy) and is committed.

---

## Task 1: `CalendarEvent` + projection parsing

**Files:**
- Create: `taskdeck/calendar_model.py`
- Test: `tests/test_calendar_model.py`

**Interfaces:**
- Produces: `CalendarEvent(unit:str, when:int, kind:str, result:str="", count:int=1, exit_status:int|None=None)` (frozen dataclass); `parse_projection(text:str) -> list[int]` (returns sorted µs-epoch UTC instants parsed from a `systemd-analyze calendar` block).

- [ ] **Step 1: Write the failing test** — a real `systemd-analyze` block; assert the `(in UTC):` lines parse to µs epochs (NOT the local lines).

```python
# tests/test_calendar_model.py
from taskdeck.calendar_model import CalendarEvent, parse_projection

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
    # 2026-06-22 13:00:00 UTC == 1781787600 s == 1781787600_000000 µs
    assert out == [1781787600_000000, 1781874000_000000]

def test_parse_projection_empty_block_is_empty():
    assert parse_projection("Original form: x\nNormalized form: x\n") == []

def test_calendar_event_is_frozen_with_defaults():
    e = CalendarEvent(unit="a.timer", when=1, kind="projected")
    assert e.result == "" and e.count == 1 and e.exit_status is None
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/test_calendar_model.py -v` → FAIL (module/func missing).
- [ ] **Step 3: Implement** — the dataclass + `(in UTC):`-line parser.

```python
# taskdeck/calendar_model.py
"""Pure calendar logic: turn fetched systemd data into CalendarEvents.

No Qt widgets, no subprocess here — this module is the headless, testable core
(it takes already-fetched text/records and returns events), mirroring how
systemd_client's parsers are pure. The Phase-2 plasmoid reuses this module, so
ALL thresholds and time math live here, never in calendar_view.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class CalendarEvent:
    """One thing the calendar draws. `when` is a µs epoch in UTC (matching
    TimerRow.next_usec). `kind`: 'ran' (actual run; `result` is success/failure),
    'projected' (future scheduled), 'gap' (a missed scheduled slot; `count`>1 = a
    contiguous region of misses), or 'approx' (a monotonic timer's single next
    run — never a series). 'running' is reserved, not emitted in v1 (see below)."""
    unit: str
    when: int
    kind: str               # 'ran' | 'projected' | 'gap' | 'approx'
    result: str = ""        # 'success' | 'failure' | '' (only meaningful for 'ran')
    count: int = 1          # >1 collapses a contiguous gap region
    exit_status: int | None = None  # detail only; from the exit line when paired
    # NOTE: 'running' (in-progress) is intentionally NOT a v1 kind — the single
    # JOB_RESULT-filtered journal query (Task 2) only returns COMPLETED runs, so
    # an in-progress run simply appears on completion. Reserved for a future
    # variant that also reads 'Starting' records.


# systemd-analyze prints, per iteration, a localtime line then an indented
# "(in UTC): <ts> UTC" line. We parse the UTC line ONLY — re-parsing the local
# line as naive-local would drift ±1h across a DST boundary on the
# projected-vs-actual overlay (spec §2.1).
_UTC_LINE = re.compile(r"\(in UTC\):\s+(.+?)\s+UTC\s*$", re.MULTILINE)
# Example payload: "Mon 2026-06-22 13:00:00" (weekday prefix, then ISO-ish).
_UTC_TS = re.compile(r"\w+\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")


def parse_projection(text: str) -> list[int]:
    """Parse a `systemd-analyze calendar` block into sorted µs-epoch (UTC)
    instants. Reads only the '(in UTC):' lines. Returns [] for a block with no
    elapses (e.g. a never-firing expression)."""
    out: list[int] = []
    for m in _UTC_LINE.finditer(text):
        ts = _UTC_TS.search(m.group(1))
        if ts is None:
            continue  # unexpected shape — skip this line, never guess a time
        y, mo, d, h, mi, s = (int(g) for g in ts.groups())
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
        out.append(int(dt.timestamp()) * 1_000_000)
    return sorted(out)
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_calendar_model.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add taskdeck/calendar_model.py tests/test_calendar_model.py && git commit -m "feat(calendar): CalendarEvent + UTC projection parsing"`

---

## Task 2: Journal run-outcome parsing (bucket by unit)

**Files:**
- Modify: `taskdeck/calendar_model.py`
- Test: `tests/test_calendar_model.py`; Create fixture `tests/fixtures/cal_journal.json`

**Interfaces:**
- Consumes: `CalendarEvent`.
- Produces: `parse_run_journal(text:str, services:set[str]) -> list[CalendarEvent]` — parses the JSON-lines output of `journalctl … JOB_RESULT=done JOB_RESULT=failed`, keeps records whose unit is in `services` (the timer-activated service names), emits one `kind="ran"` event per record (`result` from `JOB_RESULT`, `when` from `__REALTIME_TIMESTAMP`). Maps the run back to its **timer** unit via the caller-supplied `service_to_timer` — see signature below.

Refined signature (use this): `parse_run_journal(text:str, service_to_timer:dict[str,str]) -> list[CalendarEvent]` — `service_to_timer` maps an activated-service name → its timer unit name (built from `TimerRow`). Events carry the **timer** unit name (so they align with projections/gaps, which are keyed by timer).

- [ ] **Step 1: Write the failing test** — capture a real fixture first:

```bash
journalctl --user -o json --since "3 days ago" JOB_RESULT=done JOB_RESULT=failed --no-pager > tests/fixtures/cal_journal.json
```

```python
import json
from pathlib import Path
from taskdeck.calendar_model import parse_run_journal

FIX = Path(__file__).parent / "fixtures" / "cal_journal.json"

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
    assert out == [__import__("taskdeck.calendar_model", fromlist=["CalendarEvent"]).CalendarEvent(
        unit="x.timer", when=1781787600000000, kind="ran", result="failure")]
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement.**

```python
import json

def parse_run_journal(text: str, service_to_timer: dict[str, str]) -> list[CalendarEvent]:
    """Parse `journalctl … JOB_RESULT=done JOB_RESULT=failed` JSON-lines into
    'ran' events, keyed back to the TIMER unit (so they align with projections
    and gaps). Records for services not in the map are dropped. One subprocess
    feeds this for ALL units (spec §2.3) — we bucket here, in pure code."""
    out: list[CalendarEvent] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue  # a single bad line never sinks the batch
        svc = o.get("USER_UNIT") or o.get("_SYSTEMD_USER_UNIT") or o.get("UNIT")
        timer = service_to_timer.get(svc or "")
        if timer is None:
            continue
        jr = o.get("JOB_RESULT")
        result = "success" if jr == "done" else "failure" if jr == "failed" else ""
        if not result:
            continue  # not an outcome record
        ts = o.get("__REALTIME_TIMESTAMP")
        try:
            when = int(ts)
        except (TypeError, ValueError):
            continue  # no usable timestamp → can't place it
        out.append(CalendarEvent(unit=timer, when=when, kind="ran", result=result))
    return out
```

- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): journal run-outcome parsing, bucketed to timers"`

---

## Task 3: Exact gap detection (clamped to journal coverage)

**Files:**
- Modify: `taskdeck/calendar_model.py`
- Test: `tests/test_calendar_model.py`

**Interfaces:**
- Consumes: `CalendarEvent`.
- Produces: `compute_gaps(slots:list[int], runs:list[CalendarEvent], unit:str, coverage_start:int, now:int, tolerance_usec:int) -> list[CalendarEvent]`. `slots` = exact scheduled µs instants (from `parse_projection` with a past `--base-time`); `runs` = that timer's `ran` events. Emits `kind="gap"` events (contiguous misses collapsed, `count`>1) ONLY for slots in `[coverage_start, now]` with no run within `±tolerance_usec`. Module constant `GAP_TOLERANCE_USEC` for the default.

- [ ] **Step 1: Write the failing test.**

```python
from taskdeck.calendar_model import compute_gaps, CalendarEvent, GAP_TOLERANCE_USEC

DAY = 86_400_000_000  # µs in a day
def run(t): return CalendarEvent(unit="d.timer", when=t, kind="ran", result="success")

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
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement.**

```python
# A scheduled run that lands within this of a slot counts as "ran on time".
# Default covers normal timer jitter / AccuracySec; the plan may tune it.
GAP_TOLERANCE_USEC = 15 * 60 * 1_000_000  # 15 minutes

def compute_gaps(
    slots: list[int],
    runs: list[CalendarEvent],
    unit: str,
    coverage_start: int,
    now: int,
    tolerance_usec: int,
) -> list[CalendarEvent]:
    """Exact gap detection: a scheduled slot with no actual run nearby is a
    missed run. Only slots in [coverage_start, now] are judged — outside the
    journal's coverage there is no run data, so 'no run' means 'no data', NOT a
    gap (spec §4.3). Contiguous misses collapse into one event carrying a count."""
    run_times = sorted(r.when for r in runs)
    missed: list[int] = []
    for slot in sorted(slots):
        if slot < coverage_start or slot > now:
            continue  # unjudgeable (no data) or future
        if not _has_run_near(run_times, slot, tolerance_usec):
            missed.append(slot)
    # Collapse contiguous missed slots (adjacent in the sorted slot list) into
    # regions. "Contiguous" = consecutive entries of `missed` whose gap matches
    # the slot cadence; simplest robust rule: group runs of missed slots that
    # were adjacent in `slots`.
    return _collapse(missed, sorted(slots), unit)


def _has_run_near(run_times: list[int], slot: int, tol: int) -> bool:
    # Linear scan is fine (a timer has tens of runs in a window). Returns True
    # if any run falls within ±tol of the slot.
    return any(abs(t - slot) <= tol for t in run_times)


def _collapse(missed: list[int], all_slots: list[int], unit: str) -> list[CalendarEvent]:
    if not missed:
        return []
    idx = {s: i for i, s in enumerate(all_slots)}
    out: list[CalendarEvent] = []
    start = prev = missed[0]
    count = 1
    for s in missed[1:]:
        if idx[s] == idx[prev] + 1:        # adjacent slot → same region
            count += 1
        else:
            out.append(CalendarEvent(unit=unit, when=start, kind="gap", count=count))
            start = s
            count = 1
        prev = s
    out.append(CalendarEvent(unit=unit, when=start, kind="gap", count=count))
    return out
```

- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): exact gap detection clamped to journal coverage"`

---

## Task 4: Cadence→interval, projection-iterations, cell bucketing

**Files:**
- Modify: `taskdeck/calendar_model.py`
- Test: `tests/test_calendar_model.py`

**Interfaces:**
- Consumes: `CalendarEvent`, `ScheduleInfo`/`classify_cadence` from `taskdeck.models`.
- Produces:
  - `cadence_interval_usec(info: ScheduleInfo | None) -> int | None` — the timer's interval (daily→86_400e6, weekly→7d, hourly→1h, N×/day→day/N, etc.); `None` for monotonic-only/unclassifiable.
  - `projection_iterations(interval_usec:int|None, span_usec:int, cap:int=CELL_DRAW_MAX_PER_WINDOW) -> int` — N for `systemd-analyze --iterations`, capped.
  - `bucket_cell(events: list[CalendarEvent]) -> tuple[int,int]` — `(count, failures)` for a cell the view decides is over-full.
  - Constants `CELL_DRAW_MAX` (per-cell draw threshold) and `CELL_DRAW_MAX_PER_WINDOW` (projection cap).

- [ ] **Step 1: Write the failing test** (table-driven for `cadence_interval_usec`; direct for the others).

```python
from taskdeck.models import ScheduleInfo
from taskdeck.calendar_model import (cadence_interval_usec, projection_iterations,
                                     bucket_cell, CalendarEvent, CELL_DRAW_MAX_PER_WINDOW)

def test_cadence_interval_common_shapes():
    daily = ScheduleInfo(calendar=("*-*-* 00:00:00",), monotonic=())
    weekly = ScheduleInfo(calendar=("Mon *-*-* 06:00:00",), monotonic=())
    quarter = ScheduleInfo(calendar=("*-*-* 00,06,12,18:00:00",), monotonic=())
    mono = ScheduleInfo(calendar=(), monotonic=("OnBootUSec=12h",))
    assert cadence_interval_usec(daily) == 86_400_000_000
    assert cadence_interval_usec(weekly) == 7 * 86_400_000_000
    assert cadence_interval_usec(quarter) == 86_400_000_000 // 4
    assert cadence_interval_usec(mono) is None      # monotonic → no calendar interval

def test_projection_iterations_caps_high_frequency():
    span = 30 * 86_400_000_000                       # a month
    assert projection_iterations(86_400_000_000, span) in (31, 32)  # ~daily
    assert projection_iterations(60_000_000, span) <= CELL_DRAW_MAX_PER_WINDOW  # minutely → capped
    assert projection_iterations(None, span) == 0   # monotonic → no projection

def test_bucket_cell_counts_and_flags_failure():
    evs = [CalendarEvent("t", 1, "ran", "success"), CalendarEvent("t", 2, "ran", "failure")]
    assert bucket_cell(evs) == (2, 1)
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — map `classify_cadence` buckets / normalized `OnCalendar` to intervals; cap iterations; count cell events. (Reuse `classify_cadence` for the human label; derive the interval from the same normalized expression families it recognizes. For multi-trigger timers use the *smallest* interval.)
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): cadence interval, projection-N cap, cell bucketing"`

---

## Task 5: Client fetch methods (projection + single journal query)

**Files:**
- Modify: `taskdeck/systemd_client.py`
- Test: `tests/test_client.py` (extend), fixture `tests/fixtures/cal_projection.txt`

**Interfaces:**
- Consumes: existing `SystemdClient.request`, `_scope_args`, `systemctl_path`, `fakebin`.
- Produces:
  - `fetch_cal_projection(self, scope:str, unit:str, expr:str, base_epoch:int, iterations:int) -> bool` → request id `f"calproj:{scope}:{unit}"`, runs `[analyze, "calendar", f"--base-time=@{base_epoch}", f"--iterations={iterations}", "--", expr]`.
  - `fetch_cal_journal(self, scope:str, since_epoch:int, until_epoch:int) -> bool` → request id `f"caljournal:{scope}"`, runs `[journalctl, *scope, "-o","json", f"--since=@{since_epoch}", f"--until=@{until_epoch}", "JOB_RESULT=done","JOB_RESULT=failed","--no-pager"]`, `timeout_ms=15_000`.

- [ ] **Step 1: Write the failing test** — assert request ids + exact argv via the `fake_echo_argv` stub (the pattern already in `tests/test_client.py`).

```python
def test_fetch_cal_projection_id_and_argv(qtbot):
    client = SystemdClient(analyze=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_projection("user", "a.timer", "*-*-* 06:00:00", 1781000000, 8)
    rid, stdout = blocker.args
    assert rid == "calproj:user:a.timer"
    assert stdout.splitlines() == ["calendar", "--base-time=@1781000000",
                                   "--iterations=8", "--", "*-*-* 06:00:00"]

def test_fetch_cal_journal_id_and_argv(qtbot):
    client = SystemdClient(journalctl=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_journal("user", 1781000000, 1781086400)
    rid, stdout = blocker.args
    assert rid == "caljournal:user"
    argv = stdout.splitlines()
    assert "JOB_RESULT=done" in argv and "JOB_RESULT=failed" in argv
    assert "--since=@1781000000" in argv and "--until=@1781086400" in argv
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** the two methods next to the existing `fetch_*` methods (mirror their structure; `fetch_cal_journal` passes `timeout_ms=15_000` like `fetch_log`).
- [ ] **Step 4: Run to verify pass** + capture the realsystemd fixture: `systemd-analyze calendar --base-time=@$(date +%s) --iterations=5 "*-*-* 06:00:00" > tests/fixtures/cal_projection.txt`.
- [ ] **Step 5: Commit** — `git commit -m "feat(client): calendar projection + single journal-outcome fetch"`

---

## Task 6: `CalendarView` widget — skeleton, `set_events`, Day paint, selection signal

**Files:**
- Create: `taskdeck/calendar_view.py`
- Test: `tests/test_calendar_view.py`

**Interfaces:**
- Consumes: `CalendarEvent`.
- Produces: `CalendarView(QWidget)` with: `set_events(self, events:list[CalendarEvent], units:list[str], window_start:int, window_end:int, now:int) -> None`; `set_mode(self, mode:str)` where mode ∈ {"day","week","month","matrix"}; `selected = Signal(str)` (timer unit on click); a **`rebuild = Signal(int,int)`** the host connects to fetch a new window when the user navigates; read-only props `mode`, `window_start`, `window_end`. Nav chrome (Day/Week/Month toggle + `◂ ▸ [Today]`) is part of the widget. Day paint first.

- [ ] **Step 1: Write the failing test** — offscreen smoke + the selection signal. Inject events via `set_events` (no fetch), like the existing `test_smoke.py` injects rows.

```python
from PySide6.QtCore import Qt
from taskdeck.calendar_view import CalendarView
from taskdeck.calendar_model import CalendarEvent

def test_day_view_renders_and_grabs(qtbot):
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("day")
    base = 1_781_000_000_000_000
    w.set_events([CalendarEvent("a.timer", base, "ran", "success"),
                 CalendarEvent("a.timer", base + 7_200_000_000, "gap")],
                 units=["a.timer"], window_start=base, window_end=base + 86_400_000_000,
                 now=base + 86_400_000_000)
    w.resize(1000, 400); w.show(); qtbot.waitExposed(w)
    assert w.grab().width() > 0           # painted without raising

def test_click_emits_selected_unit(qtbot):
    w = CalendarView(); qtbot.addWidget(w); w.set_mode("day")
    base = 1_781_000_000_000_000
    w.set_events([CalendarEvent("a.timer", base, "ran", "success")],
                 units=["a.timer"], window_start=base, window_end=base + 86_400_000_000,
                 now=base + 86_400_000_000)
    w.resize(1000, 400); w.show(); qtbot.waitExposed(w)
    seen = []
    w.selected.connect(seen.append)
    # Click the a.timer row label area (left gutter), row 0:
    qtbot.mouseClick(w, Qt.MouseButton.LeftButton, pos=w.row_hit_point(0))
    assert seen == ["a.timer"]
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — `CalendarView(QWidget)`: a top chrome row (a `QComboBox`/segmented Day/Week/Month toggle calling `set_mode`, and `◂ ▸ [Today]` buttons that shift/recenter `window_start/window_end` by the current mode's span and emit `rebuild(window_start, window_end)`), above a custom-painted area. `set_events` stores events/units/window and calls `update()`; `paintEvent` dispatches on `self._mode`; implement `_paint_day` (timer rows × hourly axis; glyph+color per `CalendarEvent.kind`/`result`; `▲ now`; aggregate band when a row's cell exceeds `CELL_DRAW_MAX` via `bucket_cell`); `mousePressEvent` hit-tests rows → `selected.emit(unit)`; expose `row_hit_point(i)` for tests. Calendar owns its nav state — it never touches the table's `_render_rows` path. Keep ALL thresholds imported from `calendar_model`. Glyph/color table per spec §6. (Pixel layout is iterated visually post-build per Dustin; tests pin behavior — paints without error, click → signal, nav updates the window — not exact pixels.)
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): CalendarView widget + Day view + selection signal"`

---

## Task 7: Wire into `main_window` (QStackedWidget swap + selection adapter) — end-to-end Day slice

**Files:**
- Modify: `taskdeck/main_window.py`
- Test: `tests/test_calendar_integration.py`

**Interfaces:**
- Consumes: `CalendarView`, the client fetch methods, the existing `view_box`, `_dispatch_finished`, `_on_selection`, `_expected_tab_ids`.
- Produces: a `QStackedWidget` central (page 0 = existing splitter, page 1 = `CalendarView`); "Calendar" appended to `view_box`; `_dispatch_finished` branches for kinds `"calproj"`/`"caljournal"` that feed `calendar_model` and call `calendar_view.set_events`; a `_on_calendar_selected(unit)` adapter that sets `_expected_tab_ids` + fires `fetch_log/fetch_details/fetch_cat/fetch_tab_schedule` for the unit (reusing the detail tabs).

- [ ] **Step 1: Write the failing test** — selecting "Calendar" shows page 1; a calendar selection fires the detail fetches (FakeClient records calls).

```python
def test_selecting_calendar_shows_calendar_page(qtbot):
    window, client = make_window(qtbot)         # existing helper, auto_refresh=False
    idx = window.view_box.findText("Calendar")
    assert idx >= 0
    window.view_box.setCurrentIndex(idx)
    assert window._stack.currentWidget() is window.calendar_view

def test_calendar_selection_fires_detail_fetches(qtbot):
    window, client = make_window(qtbot)
    window._auto_refresh = True
    window._on_calendar_selected("a.timer")
    assert ("fetch_log", "user", "a.timer") in client.calls
    assert window._expected_tab_ids == {f"{k}:user:a.timer" for k in
                                       ("log","details","cat","schedtab")}

def test_build_skips_projection_for_disabled_timer(qtbot):
    # A disabled timer (next=None) gets no projection fetch; a never-ran timer
    # (last=0) still gets one. Past-runs query fires once regardless (spec §9).
    window, client = make_window(qtbot)
    window._timers = [TimerRow("off.timer", "off.service", None, 123),   # disabled
                      TimerRow("new.timer", "new.service", 999, 0)]      # never ran
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}
    window._build_calendar(1_781_000_000_000_000, 1_781_086_400_000_000)
    projs = [c for c in client.calls if c[0] == "fetch_cal_projection"]
    assert not any(c[2] == "off.timer" for c in projs)   # disabled → no projection
    assert len([c for c in client.calls if c[0] == "fetch_cal_journal"]) == 1
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — wrap the existing `table`+`tabs` splitter as page 0 of a `QStackedWidget`; add `CalendarView` as page 1; `view_box` "Calendar" switches the stack page (and triggers a calendar build for the visible window) instead of a table refresh; on Timers/Services switch back to page 0; connect `calendar_view.rebuild` to a `_build_calendar(win_start, win_end)` that fires `fetch_cal_journal(scope, win_start, now)` once + `fetch_cal_projection` per **enabled** calendar timer; add the two `_dispatch_finished` branches (parse projection/journal via `calendar_model`, accumulate per-build fan-in, call `set_events` when all expected ids land — a per-build counter, NOT the table's `_pending_enrich`); add `_on_calendar_selected` adapter connected to `calendar_view.selected`. **Timer eligibility (spec §9):** a **disabled** timer (`next` is None) contributes past runs + gaps but no projection; a **never-ran** timer (`last` 0/None) contributes projection but no past/gaps; a timer with empty `activates` is shown with `—` and skipped for journal/projection. Update `FakeClient` in the test helper with `fetch_cal_projection`/`fetch_cal_journal` no-ops.
- [ ] **Step 4: Run to verify pass.** Then `python3 -m taskdeck` manual smoke: select Calendar → Day view paints the live week. Update README "Calendar view" line.
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): stacked-widget swap, dispatch routing, selection adapter (Day slice end-to-end)"`

---

## Task 8: Week view

**Files:** Modify `taskdeck/calendar_view.py`; Test `tests/test_calendar_view.py`.

**Interfaces:** adds `_paint_week`; `set_mode("week")`.

- [ ] **Step 1: Failing test** — `set_mode("week")` with events spanning 7 days renders; a cell with a `failure` and a sibling `gap` both paint; `row_hit_point`/click still emits the unit.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** `_paint_week` — timer rows × 7 day-columns; each cell renders `HH glyph` (time-of-day + worst outcome), the row-health summary column, aggregate band per high-frequency cell via `bucket_cell`. Reuse the glyph/color helpers from Task 6 (extract them to module-level `_GLYPH`/`_brush` if not already).
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): Week view"`

---

## Task 9: Month grid view (highest-effort view)

**Files:** Modify `taskdeck/calendar_view.py`; Test `tests/test_calendar_view.py`.

**Interfaces:** adds `_paint_month`; `set_mode("month")`; a per-day `_day_cell_summary(events) -> (glyphs, worst, counts)` helper (pure-ish, in the view but using `bucket_cell`).

- [ ] **Step 1: Failing test** — `set_mode("month")` over a month with a failure day + a gap day renders; the week containing a problem is drawn distinctly (assert a flag/state the paint sets, e.g. `w._problem_weeks` is computed and non-empty), clean days stay quiet; click on a day emits the unit (or day — decide: emit the unit of the worst-outcome event in the day). Variable month length (28/30/31) handled — test Feb and a 31-day month.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** `_paint_month` — weeks × weekdays grid for the visible month; per-day summary (worst-outcome glyph + counts on problem days only); heavy border on weeks containing a `gap`/`failure`; dim future cells; compute `_problem_weeks` as testable state. This is the most layout logic — keep `_day_cell_summary` small and test it directly.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): Month grid view"`

---

## Task 10: Month by-timer matrix toggle

**Files:** Modify `taskdeck/calendar_view.py`; Test `tests/test_calendar_view.py`.

**Interfaces:** adds `_paint_matrix`; `set_mode("matrix")`; the view exposes a Month sub-toggle (grid ⇄ matrix) — a small `QToolButton`/segmented control emitting an internal mode change, NOT a new top-level View entry.

- [ ] **Step 1: Failing test** — `set_mode("matrix")` renders rows=timers × cols=days; a timer with one `failure` and a timer with one `gap` produce distinguishable row content (assert via a testable per-row summary method `_matrix_row_cells(unit) -> list[str]`); the aggregate timer renders as a band.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** `_paint_matrix` + the grid⇄matrix sub-toggle (visible only in Month). Reuse glyph helpers.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): Month by-timer matrix toggle"`

---

## Task 11: HEALTH strip + Month filter strip

**Files:** Modify `taskdeck/calendar_view.py`, `taskdeck/calendar_model.py` (a pure `summarize(events) -> Health` helper); Test both test files.

**Interfaces:** `calendar_model.summarize(events:list[CalendarEvent]) -> Health` where `Health(ok:int, failed:int, gaps:int, upcoming:int, issues:list[str])`; the view renders it as the top strip; Month gains a filter strip that sets a `_filter` (one of None/"fail"/"gap"/"upcoming") dimming non-matching cells.

- [ ] **Step 1: Failing test** — `summarize` counts each kind and lists issues (unit + date for each failure/gap); the view's filter dims non-matching (assert `w._filter` state + that a filtered paint runs without error).
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** `summarize` (pure, tested directly) + the strips.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): HEALTH strip + Month filter strip"`

---

## Task 12: Close-out — realsystemd test, visual de-noise pass, QA

**Files:** `tests/test_real_systemd.py` (extend), `calendar_view.py` (visual tuning), docs.

- [ ] **Step 1: realsystemd test** — `fetch_cal_journal` + a `--base-time` projection parse against live data (opt-in marker); assert parse succeeds and events are well-formed.
- [ ] **Step 2: Visual de-noise pass** — tune contrast/spacing so healthy `✔`/`▦` recede and `✘`/`⛌` dominate (Dustin: ASCII looked noisy; real rendering must de-noise). Re-grab `docs/screenshot.png`-style artifact for the calendar if useful. This step is iteration, not new behavior — no test change required beyond "still paints."
- [ ] **Step 3: Full gates** — `python3 -m pytest`, `-m realsystemd`, `ruff check .`, `mypy taskdeck` all green.
- [ ] **Step 4: Plan-doc Status update + deviation summary** (per workspace Post-Coding Process). Update the spec Status to "implemented".
- [ ] **Step 5: Commit** — `git commit -m "feat(calendar): realsystemd test + visual de-noise + close-out"`. Then the **three-phase QA Review** (code-reviewer + test-analyzer always; silent-failure-hunter for the fetch/gap/journal paths; adversarial-tester for malformed journal/projection) per the workspace Post-Coding Process.

---

## Appendix: open items the implementer resolves in-task (from spec §13)
- `GAP_TOLERANCE_USEC` default (15 min) — tune against real timers in Task 12.
- 10s-timer behavior on the Calendar page — default: rebuild the visible window on tick + on nav; confirm it doesn't stomp the user's nav position (own the nav state in `calendar_view`, Task 7).
- Projection-fetch concurrency for large M (system scope, many timers) — if the fan-in is large, cap concurrent `calproj:` fetches; the single-flight client already serializes per-id. Decide in Task 7; note if deferred.
- Run placement at completion time (v1) vs `Starting`-paired start time (later) — v1 uses completion (`JOB_RESULT` record timestamp); start-pairing is a documented future refinement.
