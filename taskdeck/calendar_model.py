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
from datetime import UTC, datetime, timedelta

from taskdeck.models import _classify_calendar
from taskdeck.systemd_client import ScheduleInfo

# -- local-calendar window boundaries ----------------------------------------
#
# The model is µs-epoch (UTC-absolute) end to end, but the WINDOW the user
# navigates is a LOCAL calendar unit: "this Day", "this Week", "this Month" mean
# the user's wall-clock day/week/month, not a UTC one. A PDT user expects the Day
# view to span local midnight→midnight, not 17:00→17:00 split across two UTC days.
# So window boundaries — and ONLY window boundaries — are computed in LOCAL time,
# then converted back to UTC µs for [win_start, win_end]. Every subprocess @-arg
# and every model computation downstream stays UTC-absolute (the boundaries are
# just two absolute instants); only their DERIVATION touches the local calendar.
#
# This lives in the pure model (not the view) so the Phase-2 plasmoid computes
# the exact same windows — a window boundary forked into the view would silently
# diverge between the two consumers (Global Constraints). It is still pure: no Qt,
# no subprocess; the local timezone comes from the process clock via
# datetime.astimezone(), exactly as the display path reads it.

# Which calendar unit each mode's window spans. The matrix is a Month sub-view
# (it shares the Month window — spec §5), so it maps to "month" here; "week" and
# "day" map to themselves. Keyed by the mode strings set_mode accepts.
_MODE_WINDOW_UNIT: dict[str, str] = {
    "day": "day",
    "week": "week",
    "month": "month",
    "matrix": "month",
}


def local_calendar_window(mode: str, anchor_usec: int) -> tuple[int, int]:
    """The LOCAL-calendar window (UTC µs bounds) for `mode` CONTAINING `anchor`.

    `anchor_usec` is any µs epoch inside the wanted unit (e.g. `now` for the
    first-show window, or a nav step's probe instant). Returns
    (win_start_usec, win_end_usec) — both UTC µs epochs, half-open [start, end):

    - day:   local 00:00 of the anchor's day        → next local 00:00 (+1 day)
    - week:  local Monday 00:00 of the anchor's week → +7 days (Mon-start, so it
             matches the Month grid's Mon..Sun columns and weekday header)
    - month: local 1st 00:00 of the anchor's month  → next month's 1st 00:00

    Why derive every boundary from the LOCAL calendar rather than µs arithmetic:
    a fixed +7×86400s "week" or +30d "month" drifts across a DST transition and
    can't express variable month length. Re-flooring an anchor to a local-calendar
    boundary (via .replace on the naive-local datetime, then .astimezone() to read
    the correct UTC instant) is DST- and month-length-correct by construction —
    confirmed by an empirical probe (2026-06-20) across a forced America/Los_Angeles
    tz: a 06:00-local anchor floors to 00:00 local and round-trips to local 00:00.

    Unknown modes fall back to the Day unit so a future caller never gets an empty
    or raising window — mirrors the view's tolerant set_mode/unknown-mode guard.
    """
    unit = _MODE_WINDOW_UNIT.get(mode, "day")
    # fromtimestamp WITHOUT a tz argument yields a NAIVE datetime in LOCAL time —
    # this is the same conversion the display path uses, so the window the user
    # navigates and the times drawn inside it share one timezone.
    local = datetime.fromtimestamp(anchor_usec / 1_000_000)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "day":
        start_local = day_start
        end_local = day_start + timedelta(days=1)
    elif unit == "week":
        # weekday(): Monday=0 … Sunday=6. Subtracting it lands on this week's
        # Monday 00:00 — the Mon-start the Month grid header (Mon..Sun) expects.
        start_local = day_start - timedelta(days=day_start.weekday())
        end_local = start_local + timedelta(days=7)
    else:  # month
        start_local = day_start.replace(day=1)
        # Next month's 1st: bump the year at December rather than month=13.
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    # .astimezone() attaches the LOCAL tzinfo to the naive boundary, so
    # .timestamp() returns the correct UTC epoch for that local wall-clock instant
    # (a DST-aware conversion — the only place tz enters). Floor to whole seconds
    # then ×1e6 to keep the µs-epoch convention used everywhere else.
    start_usec = int(start_local.astimezone().timestamp()) * 1_000_000
    end_usec = int(end_local.astimezone().timestamp()) * 1_000_000
    return (start_usec, end_usec)


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
        # The regex admits digit groups that are NOT a valid date/time — e.g. a
        # corrupt `systemd-analyze` line with month 13 or hour 99 — so datetime()
        # can raise ValueError. Skip that one line and keep going, exactly as
        # parse_run_journal does for a bad JSON line: one poison line must never
        # sink the whole projection. Letting it raise here was the root of the
        # fan-in hang (the calendar barrier never released — QA F2): one malformed
        # date wedged the entire build. We only swallow ValueError (the
        # out-of-range-field signal); anything else still propagates.
        try:
            dt = datetime(y, mo, d, h, mi, s, tzinfo=UTC)
        except ValueError:
            continue  # impossible date/time on this line — skip, never guess
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
    events. Only slots in [coverage_start, now] are judged — the interval is
    CLOSED on BOTH ends: a slot at exactly coverage_start is judged (skip is
    `slot < coverage_start`), and a slot at exactly `now` is judged too (the
    ±tolerance band absorbs a run that just fired or is about to). Outside the
    journal's coverage there is no run data, so 'no run' means 'no data', NOT a
    gap (spec §4.3); the view renders those as '—'. Future slots (strictly > now)
    are silent — they belong to the projection path (_finalize_calendar emits
    `projected` events for `s > now`), so the boundary is partitioned as: this
    function owns (-∞, now] via NOT(slot > now); projection owns (now, ∞) via
    s > now. Exactly one owner, no hole, no double-draw — a slot at exactly now
    is gap-judged only, the next slot at now+interval is projection-only (they
    are different instants). Contiguous misses collapse into one event carrying a
    count so a long outage reads as one region.

    Run consumption (R2-2): each run satisfies at MOST ONE slot — the slot it is
    NEAREST to. A stateless "any run within tolerance" test let one run silence
    EVERY slot within ±tolerance, so on a multi-trigger timer whose two
    OnCalendar expressions fire less than GAP_TOLERANCE apart, a single run could
    mask a genuine miss of the other trigger. Assignment is RUN-CENTRIC: each run
    claims the nearest still-unclaimed slot in range, so a run lands on the slot
    it actually matches and the other slot stays unsatisfied → a gap. Run-centric
    (vs. walking slots and letting each grab its nearest run) is what prevents the
    mis-assignment the QA flagged: if slots grabbed runs left-to-right, an earlier
    slot could claim a run that sits closer to a LATER slot, then false-gap that
    later slot. Sending the run to ITS nearest slot is the assignment that can't
    false-gap the wrong one.
    """
    # Pre-sort the run times once (runs arrive unsorted — one per journal record,
    # in log order). Time order makes the assignment deterministic; correctness
    # comes from each run picking its NEAREST slot, not from the order.
    run_times = sorted(r.when for r in runs)
    # Dedup the slot list BEFORE walking (F5). Duplicate µs instants are now
    # reachable: F4's multi-trigger union concatenates several expressions' slots
    # into one list, and a DST fall-back makes systemd-analyze emit the same
    # wall-clock instant twice (second-flooring can coincide two near-identical
    # instants too). _collapse keys schedule-adjacency by a slot→index map, so a
    # duplicate `when` would overwrite an index and degenerate the region merge.
    # sorted(set(...)) folds duplicates to one instant first, which keeps the
    # index map total and the merge correct. (Was WATCH-1, "unreachable"; F4 made
    # it live, so the dedup is now load-bearing, not defensive.)
    unique_slots = sorted(set(slots))
    # Judgeable slots only: partition the now-boundary so each instant has exactly
    # one owner. Keep slots in [coverage_start, now] — closed on both ends. A slot
    # < coverage_start is "no data" (before the journal's reach); a slot STRICTLY
    # > now is future and belongs to the projection path (_finalize_calendar draws
    # `projected` for s > now). A slot exactly AT now IS judged here — the
    # ±tolerance band covers a run that just fired / is about to. (The prior code
    # skipped `slot >= now`, double-excluding the now-instant from BOTH the gap
    # walk and projection → drawn nowhere; the R2-1 boundary fix closes the now
    # end.) Only these slots can be claimed by a run or become a gap.
    judgeable = [s for s in unique_slots if coverage_start <= s <= now]
    # Run-centric nearest assignment (R2-2): each run claims the nearest unclaimed
    # judgeable slot within tolerance; a claimed slot ran on time. Slots left
    # unclaimed after every run is placed are the misses.
    claimed: set[int] = set()
    for t in run_times:
        nearest = _nearest_unclaimed_slot(judgeable, claimed, t, tolerance_usec)
        if nearest is not None:
            claimed.add(nearest)
    missed = [s for s in judgeable if s not in claimed]
    # Collapse runs of missed slots that were ADJACENT in the schedule into one
    # gap region. Adjacency is decided by position in the deduped sorted slot
    # list (see _collapse), not by arithmetic on the timestamps — that stays
    # correct for irregular cadences (e.g. weekday-only) where a fixed interval
    # wouldn't. _collapse is handed the SAME deduped list so its index map agrees
    # with the missed instants picked above.
    return _collapse(missed, unique_slots, unit)


def _nearest_unclaimed_slot(
    slots: list[int], claimed: set[int], run_time: int, tol: int
) -> int | None:
    """The unclaimed slot in `slots` nearest to `run_time` within ±`tol`, or
    None if every in-tolerance slot is already claimed (or none is in range).

    `slots` is the sorted list of judgeable slots; `claimed` holds the slot
    instants already matched by an earlier run, so a run never double-claims one
    another run already satisfied (R2-2: each run satisfies at most one slot, and
    each slot is satisfied by at most one run). Among unclaimed slots within
    tolerance we return the one with the SMALLEST |slot - run_time| — sending the
    run to the slot it actually matches, which is what prevents false-gapping a
    slot a different run was the real match for.

    Linear scan is deliberate: a timer has at most tens of runs/slots in a
    visible window, so the overall O(runs × slots) is trivially bounded (Power of
    Ten rule 2) and a bisect would add complexity for no measurable gain.
    """
    best: int | None = None
    best_dist = tol + 1  # any in-tolerance slot beats this sentinel
    for s in slots:
        if s in claimed:
            continue  # already matched by an earlier (nearer-or-equal) run
        dist = abs(s - run_time)
        if dist <= tol and dist < best_dist:
            best_dist = dist
            best = s
    return best


def _collapse(missed: list[int], all_slots: list[int], unit: str) -> list[CalendarEvent]:
    """Fold a sorted list of missed slot instants into 'gap' events, merging
    schedule-adjacent misses into one region whose `count` is the run length.

    `all_slots` is the full sorted schedule; a missed slot is "adjacent" to the
    previous one when their indices in `all_slots` differ by exactly 1 — i.e.
    no kept (ran) slot sits between them. Using slot-position rather than a time
    delta keeps the merge correct for non-uniform cadences.

    `all_slots` MUST be deduplicated before it reaches here (compute_gaps does it
    with sorted(set(...))) — the idx map below keys by slot value, so a duplicate
    `when` would overwrite an index and corrupt the adjacency walk. The dedup at
    the call site is the single guard, so this stays a simple positional walk.
    """
    if not missed:
        return []
    # Position of each slot in the (deduplicated) schedule, so adjacency is an
    # index check. Safe to key by value because the caller guarantees no dupes.
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

# Forward-projection iteration budget per eligible timer×expression, based at
# NOW (not win_start). WHY a SEPARATE, small budget exists: the win_start-based
# projection (sized by projection_iterations, capped at CELL_DRAW_MAX_PER_WINDOW)
# fans out the PAST slots gap detection needs — but a fast cadence burns that cap
# entirely on slots BEFORE now, so a minutely timer (always) or an hourly timer
# (at Month scale) produces ZERO future slots and its "upcoming" ⏲ never shows
# (Dustin's live-retest symptom: "upcoming doesn't show for some timers"). A
# second projection based at NOW with this tiny fixed budget guarantees EVERY
# cadence yields a handful of upcoming slots regardless of how the win_start cap
# was spent. 16 is enough for several upcoming glyphs at any cadence while staying
# trivially bounded (Power of Ten rule 3) — the view aggregates above CELL_DRAW_MAX
# anyway, so more would only be drawn as a band. Owned here so the plasmoid shares
# it (Global Constraints).
FWD_PROJECTION_ITERATIONS = 16


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
