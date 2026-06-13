# Phase C — Head-agent synthesis (Task 10 review)

Roster: code-reviewer + test-analyzer (stabilization-slim per workspace rules).
Both Phase A verdicts negative (FIX-FIRST / GAPS-FOUND); Phase B produced full
agreement on every P1, one mechanism correction (test-analyzer's "hourly" claim
for minute-steps was wrong — the tz-strip intercepts first and yields "daily"),
and one solution refinement (naive `/`-detection would false-positive IANA
timezones into raw fallback).

A probe settled the comma-weekday question both agents raised:
`systemd-analyze calendar` normalizes contiguous lists (`Mon,Tue,Wed,Thu,Fri` →
`Mon..Fri`) but keeps non-contiguous lists (`Mon,Wed,Fri`) in comma form. So the
`"Mon,Tue,Wed,Thu,Fri"` literal is dead code, while comma lists in general are a
real input shape.

## Disposition — convergent findings (fix in this batch)

| ID | Finding | Decision |
|---|---|---|
| CADENCE-1 | Classifier misbuckets step/range/minutely; tz-strip eats `/`-bearing time fields (CR P1-1 ≡ TA P1-C) | Fix per Phase B refinement: tz-strip only letters-shaped tokens; require simple hour+minute subfields else raw fallback; weekday branch validates per-component against `_WEEKDAYS` (resolves dead-code P2-1); drop the dead comma-literal. CADENCE_CASES rows for every named shape, both tz-strip sides, negative weekday. |
| FREEZE-1 | One unparseable trigger line permanently freezes both views — parse raise escapes between by-id pop and barrier discard, recurs every cycle (CR P1-2; TA upgraded from P2) | Render-with-stale (test-analyzer's preferred variant: parser stays strict, all parser tests intact). The window's parse-error path now releases the enrichment barrier for results AND schedules — stale cadence beats a frozen table; the error stays loud. Tests: malformed schedules mid-cycle (delivered first), recovery next cycle. |
| PIN-1 | Render barrier pinned one-sided (TA; CR verified) | Mirror test, schedules-first ordering. |
| PIN-2 | Schedules wrong-scope guard unpinned (TA; CR sharpened: guard is load-bearing for the read-only contract via `_data_scope` stamping) | Mirror test + assert no render occurred. |
| PIN-3 | Client↔window request-id seam zero coverage; Task 10 tripled the surface (TA; CR agreed) | Fakebin-driven client tests asserting emitted request ids for fetch_schedules / fetch_tab_schedule / fetch_calendar; the calendar test also pins the `--` argv. |
| PIN-4 | Non-timer selection path untested — regression strands Schedule tab (TA; CR: run it in the Services view) | Services-view selection test: stamp asserted, no fetch, no schedtab id. |
| LIVE-1 | No realsystemd test for the drift-fragile human-format parser (TA; CR: complementary to FREEZE-1, not an alternative) | Live test: fetch SCHEDULE_PROPS for the machine's user timers, parse must succeed with an entry per timer. |

## Disposition — P2s (all accepted into this batch; each is small)

- Comment rot: "Five-column model" docstring, MainWindow refresh-cycle docstring,
  `fetch_calendar` provenance comment (+ documenting its now-belt-and-suspenders
  `--` guard), `_classify_monotonic` docstring. (CR P2-2/3/4/8)
- `fetch_calendar` unchecked return documented as a deliberate ignore with the
  three-step benignity argument. (CR P2-5)
- Calendar-chain failure replaces only "(calculating…)", preserving the
  already-rendered triggers. + test. (CR P2-6)
- `_TRIGGER_RE` anchored to end-of-line; trailing-junk-raises test. (CR P2-7)
- Schedules-side block-alignment tests (leading empty block, overflow) driven
  through `parse_show_schedules`. (TA P2)
- Empty-scope test additionally asserts no `fetch_schedules` call. (TA P2)
- Rejected-fetch schedules twin test. (TA P2)
- `OnUnitInactiveUSec` / `OnActiveUSec` rows added to CADENCE_CASES. (TA P2)
- Enrichment failure-mid-cycle test (error + last-good table, recovery next
  cycle). (TA P2)

## Head-agent decisions

1. **FREEZE-1 fix variant.** Render-with-stale over per-unit-degrade: smaller
   diff, parser contract (fail-loud, pinned by an existing deliberate test)
   untouched, and the loud error channel survives. Per-unit tolerance can be
   revisited if a real exotic-trigger unit ever shows up.
2. **Dedup understating frequency** (twice-daily → "daily", TA P2 confirm-intent):
   KEPT as designed. Dustin's feature request was explicitly "doesn't have to be
   too involved"; cross-trigger frequency aggregation is the involved version.
   The approximation is now documented at `classify_cadence`.
3. **Deferment** DEF-T10-01 (LOW): multi-calendar-trigger Schedule tab has no
   test (two `OnCalendar=` lines render, only `calendar[0]` chains to
   systemd-analyze). Behavior is correct by construction (list rendering +
   explicit `[0]`); pinning it is nice-to-have. Registered in the plan doc.

## Outcome

Fix batch implemented immediately after synthesis (same session), gated by the
full suite + ruff + mypy + realsystemd before commit.
