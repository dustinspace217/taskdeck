"""Pure calendar logic: turn fetched systemd data into CalendarEvents.

No Qt widgets, no subprocess here — this module is the headless, testable core
(it takes already-fetched text/records and returns events), mirroring how
systemd_client's parsers are pure. The Phase-2 plasmoid reuses this module, so
ALL thresholds and time math live here, never in calendar_view.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime


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
