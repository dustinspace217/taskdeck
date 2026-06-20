"""Pure calendar logic: turn fetched systemd data into CalendarEvents.

No Qt widgets, no subprocess here — this module is the headless, testable core
(it takes already-fetched text/records and returns events), mirroring how
systemd_client's parsers are pure. The Phase-2 plasmoid reuses this module, so
ALL thresholds and time math live here, never in calendar_view.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from taskdeck.models import _classify_calendar
from taskdeck.systemd_client import ScheduleInfo


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
        dt = datetime(y, mo, d, h, mi, s, tzinfo=UTC)
        out.append(int(dt.timestamp()) * 1_000_000)
    return sorted(out)


def parse_run_journal(text: str, service_to_timer: dict[str, str]) -> list[CalendarEvent]:
    """Parse `journalctl … JOB_RESULT=done JOB_RESULT=failed` JSON-lines into
    'ran' events, keyed back to the TIMER unit (so they align with projections
    and gaps, which are keyed by timer — not the activated service).

    `service_to_timer` maps an activated-service name → its timer unit name; the
    caller builds it from the live TimerRows. Records for services not in that
    map are dropped — one subprocess feeds this for ALL units (spec §2.3), so we
    bucket here, in pure code, rather than running one query per timer.

    Robustness contract (each guard maps to a real journal shape, not theory):
    - A single unparseable line never sinks the batch — journald can interleave
      a non-JSON banner line; we skip it and keep going.
    - The unit field has three spellings across journal versions/scopes
      (USER_UNIT, _SYSTEMD_USER_UNIT, UNIT) — we try them in that order.
    - MESSAGE may be a byte ARRAY (a JSON list) for non-UTF-8 log lines, not a
      string. We never touch MESSAGE here, so that shape is harmless — the test
      pins that it doesn't crash, guarding any future code that reaches for it.
    - __REALTIME_TIMESTAMP is a µs-epoch STRING in journal JSON; a missing or
      non-numeric value means we can't place the run, so we drop it.
    """
    out: list[CalendarEvent] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue  # a single bad line never sinks the batch
        svc = o.get("USER_UNIT") or o.get("_SYSTEMD_USER_UNIT") or o.get("UNIT")
        # .get("") keeps the lookup total even when svc is None (no unit field).
        timer = service_to_timer.get(svc or "")
        if timer is None:
            continue  # not a timer-activated service we're tracking
        jr = o.get("JOB_RESULT")
        # JOB_RESULT, not _SYSTEMD_INVOCATION_ID: the completion record carries
        # the outcome here ('done'/'failed'); the invocation id is absent from
        # these records (spec / Global Constraints).
        result = "success" if jr == "done" else "failure" if jr == "failed" else ""
        if not result:
            continue  # not an outcome record (e.g. 'skipped', or no JOB_RESULT)
        ts = o.get("__REALTIME_TIMESTAMP")
        try:
            when = int(ts)
        except (TypeError, ValueError):
            continue  # no usable timestamp → can't place it on the calendar
        out.append(CalendarEvent(unit=timer, when=when, kind="ran", result=result))
    return out


# A scheduled run that lands within this of a slot counts as "ran on time".
# Default covers normal timer jitter / AccuracySec (systemd defaults to 1min
# accuracy and adds randomized delay); 15min is a generous band so a slightly
# late real run is never mislabeled a miss. Lives in the model, not the view, so
# the Phase-2 plasmoid shares the same threshold (spec / Global Constraints).
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
    missed run.

    `slots` are the exact scheduled µs instants for this timer (from
    parse_projection over a PAST --base-time); `runs` are that timer's 'ran'
    events. Only slots in [coverage_start, now] are judged — outside the
    journal's coverage there is no run data, so 'no run' means 'no data', NOT a
    gap (spec §4.3); the view renders those as '—'. Future slots (> now) are
    likewise silent — a run can't have happened yet. Contiguous misses collapse
    into one event carrying a count so a long outage reads as one region.
    """
    # Pre-sort the run times once so _has_run_near scans a known order; runs
    # arrive unsorted (one per journal record, in log order).
    run_times = sorted(r.when for r in runs)
    missed: list[int] = []
    for slot in sorted(slots):
        if slot < coverage_start or slot > now:
            continue  # unjudgeable (before coverage = no data) or still future
        if not _has_run_near(run_times, slot, tolerance_usec):
            missed.append(slot)
    # Collapse runs of missed slots that were ADJACENT in the schedule into one
    # gap region. Adjacency is decided by position in the sorted slot list (see
    # _collapse), not by arithmetic on the timestamps — that stays correct for
    # irregular cadences (e.g. weekday-only) where a fixed interval wouldn't.
    return _collapse(missed, sorted(slots), unit)


def _has_run_near(run_times: list[int], slot: int, tol: int) -> bool:
    """True if any run in `run_times` falls within ±`tol` of `slot`.

    Linear scan is deliberate: a timer has at most tens of runs in a visible
    window, so a bisect would add complexity for no measurable gain. `run_times`
    is pre-sorted by the caller, but order is irrelevant to this `any()` check.
    """
    return any(abs(t - slot) <= tol for t in run_times)


def _collapse(missed: list[int], all_slots: list[int], unit: str) -> list[CalendarEvent]:
    """Fold a sorted list of missed slot instants into 'gap' events, merging
    schedule-adjacent misses into one region whose `count` is the run length.

    `all_slots` is the full sorted schedule; a missed slot is "adjacent" to the
    previous one when their indices in `all_slots` differ by exactly 1 — i.e.
    no kept (ran) slot sits between them. Using slot-position rather than a time
    delta keeps the merge correct for non-uniform cadences.
    """
    if not missed:
        return []
    # Position of each slot in the schedule, so adjacency is an index check.
    idx = {s: i for i, s in enumerate(all_slots)}
    out: list[CalendarEvent] = []
    start = prev = missed[0]
    count = 1
    for s in missed[1:]:
        if idx[s] == idx[prev] + 1:        # next slot in the schedule → same region
            count += 1
        else:
            out.append(CalendarEvent(unit=unit, when=start, kind="gap", count=count))
            start = s
            count = 1
        prev = s
    out.append(CalendarEvent(unit=unit, when=start, kind="gap", count=count))
    return out


# -- Cadence interval, projection sizing, cell aggregation -------------------
#
# These size the projection fan-out and collapse over-full cells. They live in
# the model (not the view) so the Phase-2 plasmoid shares one set of thresholds
# (spec / Global Constraints). The interval is derived from the SAME normalized
# OnCalendar families classify_cadence recognizes — one classifier, two
# consumers — so the human "daily/weekly/…" label and the numeric interval can
# never disagree about which shapes are understood.

_DAY_USEC = 86_400_000_000  # µs in a day; the anchor for every calendar interval

# Map a classify_cadence WORD (the output of _classify_calendar) to the timer's
# firing interval in µs. We reuse the existing classifier rather than re-parse
# OnCalendar here: it already encodes every shape the app claims to understand,
# with the raw-fallback contract for the rest. Variable-length cadences use a
# nominal interval — what matters downstream is sizing --iterations to cover a
# window, so "monthly" ≈ 30d and "yearly" ≈ 365d are deliberately approximate.
_CADENCE_INTERVAL_USEC: dict[str, int] = {
    "minutely": 60 * 1_000_000,
    "hourly": 3_600 * 1_000_000,
    "daily": _DAY_USEC,
    "weekdays": _DAY_USEC,        # fires Mon–Fri; the SHORTEST gap between runs is 1 day
    "monthly": 30 * _DAY_USEC,    # nominal (months vary) — only used to size projection N
    "yearly": 365 * _DAY_USEC,    # nominal — same reason
}

# Per-timer-per-window projection cap. A minutely timer over a month would
# otherwise demand ~43k --iterations (and emit that many `projected` events);
# above this the model returns the cap and the view draws ONE aggregate band
# (spec §4.4). 500 comfortably covers a daily/weekly timer across any view span
# while bounding the worst case (Power of Ten rule 3: bound the fan-out).
CELL_DRAW_MAX_PER_WINDOW = 500

# Per-CELL draw threshold: above this many events in a single calendar cell the
# view stops drawing individual glyphs and renders an aggregate band via
# bucket_cell. Distinct from the projection cap above — that bounds how many
# slots we ASK systemd for; this bounds how many we DRAW in one cell. Owned here
# so the plasmoid uses the same threshold (spec line 122).
CELL_DRAW_MAX = 12


def cadence_interval_usec(info: ScheduleInfo | None) -> int | None:
    """The timer's firing interval in µs, or None when there is no calendar
    cadence to project from (monotonic-only, unclassifiable, or no info).

    `info` is the timer's effective triggers (from `systemctl show`, via
    ScheduleInfo). Calendar expressions are classified with the shared
    _classify_calendar so the interval matches the human cadence label exactly;
    monotonic triggers are ignored here — a monotonic timer's next run comes
    from list-timers as a single `approx` event, never a projected series
    (spec §4.2), so it has no calendar interval.

    Multi-trigger timers return the SMALLEST interval among their calendar
    expressions: that is the tightest cadence the window must be sized to cover,
    so projecting at the smallest interval never under-counts the others.
    """
    if info is None:
        return None
    intervals: list[int] = []
    for expr in info.calendar:
        word = _classify_calendar(expr)
        interval = _CADENCE_INTERVAL_USEC.get(word)
        if interval is not None:
            intervals.append(interval)
            continue
        # "N×/day" carries its own count in the word (e.g. "4×/day") — the gap
        # between fires is one day divided by N. classify_cadence builds this
        # from a comma hour-list, so the count is always a positive integer.
        if word.endswith("×/day"):
            n = int(word[: -len("×/day")])
            intervals.append(_DAY_USEC // n)
            continue
        # "weekly (Mon)" / "weekly (Mon,Wed)": a weekday-qualified calendar. The
        # nominal interval is one week — sizing the projection to a 7-day cadence
        # covers any single weekday slot; the exact per-week slot times come from
        # systemd's own expansion, not from this estimate.
        if word.startswith("weekly"):
            intervals.append(7 * _DAY_USEC)
            continue
        # Anything else is the raw-fallback case (_classify_calendar returned the
        # expression unchanged because it didn't recognize the shape) — we have
        # no honest interval, so it contributes nothing.
    return min(intervals) if intervals else None


def projection_iterations(
    interval_usec: int | None,
    span_usec: int,
    cap: int = CELL_DRAW_MAX_PER_WINDOW,
) -> int:
    """How many `--iterations` to request to cover `span_usec` at `interval_usec`,
    capped at `cap`.

    Returns 0 when `interval_usec` is None — a monotonic-only timer has no
    calendar cadence to project (its single next run is fetched separately as an
    `approx` event), so it gets no projection fetch at all.

    N = ceil(span / interval) + 1: the ceil reaches the far edge of the window
    and the +1 covers the slot that may sit just past it (a partial cell still
    needs its boundary run). The cap bounds a pathologically fast timer
    (minutely over a month would want ~43k) so the fetch and the event list stay
    bounded (Power of Ten rule 3); above the cap the view aggregates (spec §4.4).
    """
    if interval_usec is None:
        return 0
    # interval is derived from a recognized cadence, so it is always positive;
    # guard anyway since a future caller could pass a hand-built value.
    if interval_usec <= 0:
        return 0
    n = math.ceil(span_usec / interval_usec) + 1
    return min(n, cap)


def bucket_cell(events: list[CalendarEvent]) -> tuple[int, int]:
    """Collapse one cell's events into `(count, failures)` for the aggregate
    band the view draws when a cell exceeds CELL_DRAW_MAX.

    `count` is the total number of events in the cell; `failures` is how many of
    them are failed runs. The view tints the band red when failures > 0 — that
    is the whole point of aggregating in the MODEL rather than the view: a
    summarized cell must never hide a failure behind a tidy count (spec §4.4).
    A 'ran' event with result 'failure' is the only failing kind; gaps and
    projections are not run outcomes and so never count as failures here.
    """
    count = len(events)
    failures = sum(1 for e in events if e.kind == "ran" and e.result == "failure")
    return (count, failures)


# -- HEALTH summary ----------------------------------------------------------
#
# summarize() rolls the whole visible window into one health readout: how many
# runs succeeded / failed, how many scheduled slots were missed, how many runs
# are still upcoming, plus a human list of the things that went wrong. The view
# draws this as the top strip and the Phase-2 plasmoid reuses it, so — like
# every other count and threshold — it lives in the MODEL, never the view. A
# single source of truth means the strip and the plasmoid can never disagree
# about what "1 failed" means.


@dataclass(frozen=True)
class Health:
    """The summary the top strip shows for the visible window.

    `ok`/`failed` are successful/failed run counts; `upcoming` is projected +
    approx (runs that haven't happened yet); `gaps` is the number of MISSED
    SLOTS — the sum of each gap event's `count`, so a collapsed region of 3
    misses contributes 3, because the user cares how many runs were skipped, not
    how many contiguous regions they formed. `issues` is one human "unit @ date"
    string per failure and per gap region, the strip's at-a-glance "what's wrong".

    Frozen (immutable) like CalendarEvent: a Health is a computed snapshot, never
    mutated after summarize() returns it; the view caches and re-reads it. The
    `issues` list uses field(default_factory=list) — a bare `[]` default would be
    SHARED across all instances (Python's classic mutable-default trap), so the
    factory gives each Health its own list.
    """
    ok: int = 0
    failed: int = 0
    gaps: int = 0
    upcoming: int = 0
    issues: list[str] = field(default_factory=list)


def summarize(events: list[CalendarEvent]) -> Health:
    """Roll a flat event list into a Health summary (counts + issue strings).

    `events` is the same flat list the view paints (from parse_run_journal,
    compute_gaps, and projections combined). One pass classifies each event by
    kind — Power of Ten rule 2: bounded by the event count, itself bounded by the
    projection cap. Failures and gaps also append a "unit @ YYYY-MM-DD" issue so
    the strip can list what went wrong without re-deriving it. Gaps add their
    `count` to the missed-slot total (a count>1 region stands for that many
    skipped runs) but contribute ONE issue line for the region — the user reads
    "this timer had an outage starting <date>", not three identical lines.
    """
    ok = failed = gaps = upcoming = 0
    issues: list[str] = []
    for ev in events:
        if ev.kind == "ran":
            if ev.result == "failure":
                failed += 1
                issues.append(_issue_line(ev))
            else:
                ok += 1
        elif ev.kind == "gap":
            gaps += ev.count  # a collapsed region of N misses counts as N
            issues.append(_issue_line(ev))
        elif ev.kind in ("projected", "approx"):
            upcoming += 1
        # Any unrecognized kind is silently ignored — it is neither a known
        # outcome nor an upcoming run, so it can't honestly land in any bucket.
    return Health(ok=ok, failed=failed, gaps=gaps, upcoming=upcoming, issues=issues)


def _issue_line(event: CalendarEvent) -> str:
    """Format one failure/gap as a human "unit @ YYYY-MM-DD" string.

    The date is the event's UTC calendar date (matching the µs-epoch convention
    everywhere else in this module), so an issue is anchored in time. Kept tiny
    and separate so summarize() reads as pure bucketing and the date format has
    one place to change in the visual pass.
    """
    day = datetime.fromtimestamp(event.when / 1_000_000, UTC).date()
    return f"{event.unit} @ {day.isoformat()}"
