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
