# Calendar View — Design Spec (Task Deck)

## Status (2026-06-20, IMPLEMENTED v1 on branch `calendar-view`)
Phase: BUILT. Design was hardened by a 4-critic adversarial pass (P0/P1 folded in
below), then all 12 plan tasks were executed via phased workflows (TDD impl +
independent adversarial verify per task). Day / Week / Month / by-timer-matrix all
render correctly; 211 hermetic + 5 realsystemd tests; ruff + mypy clean. One real
layout bug found by the by-eye visual pass (a hidden filter strip's stale geometry
stranded the Week/Matrix grids) and fixed (630da67) — invisible to the headless
verifiers. REMAINING before "done": (1) the three-phase QA Review (workspace
Post-Coding Process — posts to GitHub Discussions, keeps Dustin in the synthesis
loop); (2) a visual de-noise pass to Dustin's taste (contrast/whitespace — the
views are functional + correctly laid out, polish pending); (3) merge
`calendar-view` → main + push (Dustin's go). Probed live on systemd 259 / Fedora 44.

**Goal:** A third top-level **Calendar** view in Task Deck that visualizes systemd
**timer** schedules — past actual runs (mined from the journal, with success/
failure) and projected future runs — as Day / Week / Month layouts, so the user
can see *when jobs execute* and, diagnostically, *when they failed or were
missed*.

**Architecture (one sentence):** A pure, headless `calendar_model.py`
(projection + journal-history merge + exact gap detection → a list of
`CalendarEvent`s for a date range) feeds a custom-painted `calendar_view.py`
QWidget hosted in a `QStackedWidget` page, selected from the existing `View:`
dropdown. No web, no new heavy deps; reuses the async-QProcess client and pure-
parser discipline.

> **What the adversarial pass changed (vs. the first draft):** (1) runs are NOT
> keyed by `_SYSTEMD_INVOCATION_ID` — that field is absent from completion records
> (0/1293); outcome is the `JOB_RESULT` field. (2) Gap detection is now **exact**
> via `systemd-analyze calendar --base-time=@<epoch>` (projects from any past
> anchor) — the fuzzy rhythm heuristic is **deleted** (real data proved it would
> fabricate thousands of false gaps). (3) Past runs come from **one** manager-
> scoped journal query (not N per-timer). (4) The 2-year range envelope is cut.
> (5) New integration machinery is named: stacked-widget swap, distinct request-
> id kinds, a fan-in barrier, a selection adapter.

---

## 1. Scope

**In scope (v1):**
- **Timers only** (and the services they activate). On-demand-only services are
  out — covered by the existing Timers/Services views.
- **Past runs**, mined from the journal, each with outcome (success / failure /
  still-running).
- **Future runs**, projected forward (exact for calendar timers; a single
  approximate marker for monotonic timers, which cannot be wall-clock projected).
- **Gap detection** — a run was *due* and did not happen — first-class, and now
  **exact** for calendar timers (§4.3). The core diagnostic.
- **Three sub-views** Day / Week / Month, switched by a toggle in the view.
- **Month "by-timer matrix" toggle** — rows = timers, cols = days.
- **Both scopes** (user / system) via the existing scope toggle (read-only view).

**Out of scope (v1):** editing schedules; non-timer service activity; reading
archived/compressed journals to extend history past live retention (a
`journal-archiver` tie-in, Phase 2+); the KDE **plasmoid** (Phase 2 — §11;
`calendar_model` is built pure so the plasmoid reuses it). Gap detection for
**monotonic-only** timers (no `OnCalendar`) is **deliberately not done** — they
have no wall-clock schedule to be "missed" against; honest "no gaps" beats a
heuristic (an early heuristic was cut for fabricating false gaps on real data).

---

## 2. Verified data contracts (probed live 2026-06-20, systemd 259 / Fedora 44)

REAL probe results, re-verified by the adversarial pass. The plan re-captures
these as fixtures.

1. **Future projection / past slot expansion — `systemd-analyze calendar
   --base-time="@<epoch>" --iterations=N "<expr>"`.** Projects an `OnCalendar`
   expression forward **from any anchor epoch, including a past one** (verified
   recovering exact past slots days before now). Emits `Next elapse:` (local) +
   `(in UTC):` lines per iteration. **Parse the `(in UTC):` line and convert
   UTC→µs epoch** — never re-parse the local line as naive-local (±1h DST hazard
   on the projected-vs-actual overlay). **No end-time bound exists** — only
   `--iterations=N`; the model computes N from the cadence interval to cover the
   window (§4.2).
2. **Monotonic timers are NOT projectable.** `systemd-analyze calendar "boot+12h"`
   → exit 1, "Failed to parse calendar specification" on stderr (the client
   routes that to `failed` → per-timer isolation, §8). Their only known future
   point is the single `next_elapse` from `list-timers` → one `◇` marker.
3. **Past runs + outcome — ONE manager-scoped query:**
   `journalctl --user -o json --since <start> --until <end> JOB_RESULT=done JOB_RESULT=failed`
   (the two `JOB_RESULT` matches are OR'd server-side). **Verified:** returns
   every unit's run-completion events in one call (1344 records across units; the
   3 real failures distinguishable), each carrying `__REALTIME_TIMESTAMP` (µs,
   UTC) + `JOB_RESULT` (`done`=success / `failed`=failure) + the unit
   (`USER_UNIT` / `_SYSTEMD_USER_UNIT`). **One subprocess, not N** — fits the
   single-flight client; the pure parser buckets by unit and keeps the timer-
   activated services. **`JOB_RESULT` is the outcome key** (`_SYSTEMD_INVOCATION_ID`
   is ABSENT from these records — 0/1293 — do NOT key on it).
   - **Start time (optional, for slot placement):** the `Starting <svc>` line
     (`MESSAGE_ID=7d4958e8…`, 290 seen) precedes each run; pair it to the next
     `JOB_RESULT` record for the same unit (ordered, counts ≈1:1) if placing a run
     at its *start* rather than completion. v1 may place at completion time and
     defer start-pairing (§4.2) — completion is always present, start is a refinement.
   - **Exit code (detail only):** the `<svc>: Main process exited…` line carries
     `EXIT_STATUS`/`EXIT_CODE`; success/failure for the calendar itself comes from
     `JOB_RESULT`, not the exit line.
   - **MESSAGE may be a byte array** (journal quirk the existing parser already
     handles) — guard `isinstance(str)` before string ops.
4. **Timer rows — `systemctl --user list-timers --all -o json`** → `unit`,
   `activates`, `next` (µs, null=disabled), `last` (µs, 0/null=never). Already
   parsed into `TimerRow`.
5. **Backward range = journal coverage.** The journal keeps only what
   `SystemMaxUse` retains; on this machine the *user* journal currently reaches
   back only **~7 days**. Gap detection is therefore valid **only within
   [oldest-journal-entry, now]** — outside that window there's no run data, so a
   scheduled slot with "no run" means "no data," NOT a gap (§4.3 clamp). The past
   calendar renders back to the oldest available entry; **no fixed year floor**
   (cut as decorative — retention always binds first).
6. **Compatibility floor unchanged:** `systemctl list-* -o json` (~v246/2020) +
   `systemd-analyze calendar`. → Fedora 33+, Ubuntu 22.04+, Debian 11+, RHEL 9+.

---

## 3. Architecture & files

```
taskdeck/calendar_model.py   NEW — PURE, no widgets, headless-testable. Holds:
                                 CalendarEvent; projection parse (UTC line→µs);
                                 journal-outcome parse (bucket by unit); exact
                                 gap computation; the cell-bucketing/aggregation
                                 HELPER + all thresholds (GAP_TOLERANCE,
                                 CELL_DRAW_MAX). Plasmoid (Phase 2) reuses this.
taskdeck/calendar_view.py    NEW — the custom-painted QWidget (Day/Week/Month +
                                 by-timer matrix), internal nav/view state,
                                 set_events(events) test entry point, and a
                                 selection Signal(unit:str). ONLY new widget code.
taskdeck/systemd_client.py   MODIFY — add fetch_cal_projection(scope,unit,expr,
                                 base_epoch,iterations) → id "calproj:{scope}:{unit}";
                                 fetch_cal_journal(scope,since,until) → id
                                 "caljournal:{scope}" (the single manager-scoped
                                 query). Explicit ≥15s timeout on the journal read.
taskdeck/main_window.py      MODIFY — central widget becomes a QStackedWidget
                                 (page0 = existing table+tabs splitter; page1 =
                                 calendar); "Calendar" added to the View dropdown;
                                 a selection adapter wires calendar_view's
                                 Signal(unit) into the existing detail-tab flow.
taskdeck/models.py           REUSE — classify_cadence/ScheduleInfo give each
                                 calendar timer its cadence→interval (for N + slot
                                 step). No structural change.
tests/test_calendar_model.py NEW — pure tests (bulk of coverage).
tests/test_calendar_view.py  NEW — offscreen widget smoke + selection signal.
```

**Boundary discipline:** `calendar_model.py` is pure (no Qt widgets, no
subprocess) — it takes already-fetched text/records and returns `CalendarEvent`s
+ provides pure cell-bucketing. All I/O stays async in `systemd_client.py`. **All
thresholds live in the model, never the view** (so the Phase-2 plasmoid reuses the
aggregation). This mirrors the existing `systemd_client`(pure)/`main_window`
(widgets) split and `monitor.py`'s headless client reuse.

---

## 4. Data model & core logic

### 4.1 `CalendarEvent` (µs-epoch convention, matching `TimerRow.next_usec`)
```
@dataclass(frozen=True)
class CalendarEvent:
    unit: str          # timer unit name
    when: int          # µs epoch, UTC (run instant / projected instant / missed slot)
    kind: str          # "ran" | "running" | "projected" | "gap" | "approx"
    result: str        # "success" | "failure" | ""   (only for kind="ran")
    count: int = 1      # >1 for a contiguous gap region (missed slots collapsed)
    exit_status: int | None = None  # detail only; from the exit line when paired
```
- `kind="running"` = a `Starting` with no terminal `JOB_RESULT` yet (in-progress
  at the window edge) → render `▶`, never a completed outcome.
- `kind="gap"` with `count=k` = a contiguous run of `k` missed scheduled slots
  collapsed to ONE event (region + count), NOT k separate events (§4.3). `when` =
  the first missed slot.
- **No `AggBand` event type.** A cell with too many runs to draw is aggregated by
  the view calling the model's pure `bucket_cell(events)` helper (count + any-
  failure tint) at paint time — keeps the model a plain event list.

### 4.2 Building the event set for a [win_start, win_end] window (per scope)
1. **Past (`ran`/`running`)** — ONE `fetch_cal_journal(scope, since=win_start,
   until=min(now,win_end))`; the pure parser buckets `JOB_RESULT` records by unit,
   keeps records whose unit is a timer-activated service (from `TimerRow.activates`),
   emits `kind="ran"` (`result` from `JOB_RESULT`). A `Starting` with no following
   `JOB_RESULT` in-window → `kind="running"`.
2. **Future (`projected`)** — per calendar timer, `fetch_cal_projection` with
   `base_epoch=now`, `N = ceil(window_span / cadence_interval) + small_margin`
   (cadence_interval from `classify_cadence`/normalized `OnCalendar`; capped so a
   minutely timer over a month aggregates rather than emitting tens of thousands —
   see §4.4). Parse `(in UTC):` lines in (now, win_end] → `kind="projected"`.
   Monotonic timers → one `kind="approx"` from `list-timers` `next`.
3. **Gaps (`gap`)** — §4.3.
4. **Merge.** No empty range beyond the oldest journal entry / past win_end.

### 4.3 Gap detection (EXACT, via `--base-time`) — the core diagnostic
Replaces the deleted rhythm heuristic. For each **calendar** timer:
1. Expand its exact scheduled slots in the window: `fetch_cal_projection` with
   `base_epoch = max(win_start, oldest_journal_entry)` and N sized to reach
   `min(now, win_end)`. These are the times the timer *should* have fired.
2. For each expanded slot, check the journal-mined `ran` events for that unit for
   a run within a tolerance window (`GAP_TOLERANCE`, e.g. the timer's
   `AccuracySec` or a default like 2× the cadence's accuracy, min a few minutes).
   A slot with **no** run in tolerance → a missed slot.
3. Collapse contiguous missed slots into one `kind="gap"` event with `count`.
**Hard clamp (completeness):** only evaluate slots within **[oldest-journal-entry,
now]**. Outside the journal's actual coverage there is no run data, so "no run"
means "no data," not a gap — never paint `⛌` where we simply can't know. **Never**
emit gaps in the future or for monotonic-only timers. This is exact (no median/
rhythm guessing) and needs zero run-history, so it works for weekly/monthly timers
even when the journal holds <2 of their runs.

### 4.4 Aggregation (`▦`) — a pure model helper, applied by the view
`bucket_cell(events) -> (count, failures)` collapses a cell's events; the view
renders `▦ <count>✔` (or `<ok>✔ <fail>✘`, tinted red if `failures>0`). The
projection N-cap (§4.2) prevents emitting tens of thousands of `projected` events
for a minutely timer over a month — above a per-timer-per-window cap, emit a
single `projected` "band" event with a count instead of individual slots.

---

## 5. Views (layouts approved 2026-06-20 — see brainstorming mockups)

Shared chrome: a `[ Day │ Week │ Month ]` toggle, `◂ <range> ▸` + `[Today]`, a top
**HEALTH** strip (`✔ ✘ ⛌ ⏲` tallies + issues callout), and selection feeding the
existing detail tabs (§8).

- **Day** — timer rows × hourly axis, `▲ now`, aggregate band per high-freq row,
  single `◇` for monotonic. "What's happening today."
- **Week** — timer rows × 7 day-columns; cell = `HH <glyph>`; row-health column.
  The diagnostic workhorse: failure + gap side by side.
- **Month** — calendar grid (weeks × weekdays); day-cell summary (`✔▦`, worst-
  outcome + counts on problem days); **heavy border on a week with a problem**;
  future cells dim; a filter strip isolates a class. **Highest-effort view**
  (variable month length, borders, filter, hit-testing) — build it **last**.
- **Month → by-timer matrix (toggle)** — rows = timers × day-cols; `metrics`
  solid `▦` band, lone `✘` vs lone `⛌` read as failure vs absence per timer.

## 6. Visual grammar (matches the app's existing table language)
```
✔ ran ok (muted green)   ✘ ran FAILED (muted red)   ⛌ GAP: due but missed (amber)
⏲ upcoming/projected (dim) ▶ running (in progress)    ○ idle, nothing due
· empty slot   — no data   ◇ monotonic "approx" — single next-run, never a series
▦ aggregate band + count (tints red if any failed)
▸ today/now   heavy border = week with a problem   future cells dimmed
```
Glyph **and** color both carry meaning → colorblind-safe; glyphs match the
Timers/Services table. **Density note (Dustin):** the ASCII mockups read "noisy"
because ASCII lacks color weight/spacing; the real Qt rendering must let the
healthy majority recede (low-contrast `▦`/`✔`) so `✘`/`⛌` draw the eye — visual
de-noising is an **expected post-build iteration**, not a v1 blocker.

## 7. Range / navigation
- The visible **sub-view** (Day/Week/Month) is the unit of work. Nav arrows move
  the window; `[Today]` recenters. **Fetches are scoped to the visible window plus
  one adjacent window of prefetch** so the next-arrow isn't empty — there is **no
  multi-year envelope** (cut: it had no v1 consumer).
- **Backward** is bounded by journal coverage (discovered: oldest available
  entry). **Forward** projection covers the visible+prefetch window only.
- Calendar window/nav state is owned by `calendar_view` and does **not** go
  through the table's `_render_rows`/selection-restore path.

## 8. Integration mechanics (named, per the architecture review)
- **Widget swap:** the central widget becomes a **`QStackedWidget`** — page 0 is
  the existing `table`+`tabs` `QSplitter` (untouched), page 1 is `calendar_view`.
  Selecting "Calendar" in the View dropdown switches the stack page and triggers a
  calendar build; selecting Timers/Services switches back to page 0 and the normal
  table refresh. The View dropdown's `currentIndexChanged` dispatches on the new
  index instead of unconditionally calling the table `refresh()`.
- **Refresh timer:** while page 1 (Calendar) is active, the 10s `QTimer` drives a
  **calendar rebuild of the visible window** (not a table refresh); while page 0
  is active it behaves as today. (Or is suppressed on Calendar and rebuild is
  manual + on-nav — plan decides; default: rebuild-on-nav + on the 10s tick.)
- **Request-id kinds are NEW and distinct:** `calproj:{scope}:{unit}` and
  `caljournal:{scope}`. **Do NOT reuse `calendar:`** — it's already taken by the
  Schedule-tab elapse preview (`fetch_calendar`→`calendar:{expr}`, routed to
  `tab_schedule`); reusing it would paint projections into the Schedule tab.
- **Fan-in barrier (new):** the calendar dispatches 1 journal fetch + M projection
  fetches (M = calendar timers) per build; it paints when all expected ids have
  landed. This is a **per-build fan-in counter** (new), not the existing 2-kind
  `_pending_enrich` set — budget for it in the plan. (The single journal query
  keeps the count to 1 + M, far below a per-timer-journal fan-out.)
- **Selection → detail (adapter):** `calendar_view` has no `QItemSelectionModel`;
  it emits `Signal(unit:str)` on click. A small adapter in `main_window` does what
  `_on_selection` does for a table row — set `_expected_tab_ids` for the unit+scope
  and fire `fetch_log/details/cat/schedtab` — reusing the existing detail tabs and
  their freshness gating.
- **Error handling (no silent failures):** a projection fetch failing for one
  timer → that timer shows no future dots + a note; others unaffected (per-timer
  isolation, like the existing calendar-tab fetch). The journal query failing →
  the past layer shows "—" with a loud HEALTH-strip error, never a silently empty
  calendar. Unparseable output → fail loud (mirrors `parse_show_schedules`). Scope
  flip / nav mid-fetch → existing by-request-id freshness gating drops stale
  responses. **"No data" (`—`/`·`) and "missed run" (`⛌`) stay visually distinct
  everywhere** (§4.3 clamp guarantees we never render a gap where we lack data).

## 9. Completeness / edge cases (author-covered; the rate-limited critic's lens)
- **Never-ran timer** (`last`=0/null): no past runs; show `⏲` future only; no gaps
  (no baseline to miss). **Disabled timer** (`next`=null): past only, no projection.
- **Service name not derivable** (`activates` empty): render the timer row with
  `—`, skip its journal/projection — graceful, never a crash.
- **In-progress run** at the window edge: `kind="running"` → `▶`.
- **Gap vs no-data:** the §4.3 clamp to journal coverage is the load-bearing
  guard — outside coverage we render `—`, never `⛌`.
- **Aggregate hiding a failure:** `bucket_cell` returns `failures>0` → the band
  tints red and shows the fail count; a single `✘` in 96 runs still surfaces.
- **DST/clock change** in-window: all placement is UTC-µs (journal native; `(in
  UTC):` for projections) → no ±1h drift.
- **Many timers, system scope:** past layer is still ONE query; projection is M
  fetches — if M is large the fan-in barrier + single-flight handle it; the plan
  caps concurrent projection fetches if needed.

## 10. Testing strategy
- **`calendar_model` (pure, headless — the bulk):** projection parse (`(in UTC):`
  →µs, DST date); journal-outcome parse (bucket by unit, success/failure/running,
  byte-array MESSAGE); **exact gap detection** (calendar timer with one missing
  slot → one `⛌`; contiguous misses → one gap+count; slot present→no gap; **gap
  clamp**: no `⛌` outside journal coverage; monotonic→never a gap; never-ran→no
  gap); aggregation `bucket_cell`; N-derivation; merge; monotonic single-marker.
  Table-driven against re-captured fixtures (`journalctl … JOB_RESULT=…` +
  `systemd-analyze … --base-time`).
- **`calendar_view` (offscreen Qt):** `set_events()` injects canned events (no
  fetch); smoke-render Day/Week/Month + matrix; a click emits `Signal(unit)`; the
  view toggle switches layout; `auto_refresh=False` → zero subprocess.
- **`main_window`:** the stacked-widget swap selects the calendar page; the
  selection adapter fires the detail fetches for the clicked unit.
- **realsystemd (opt-in):** the single journal query + a `--base-time` projection
  parse against live data.
- Gates unchanged: ruff + mypy(local) + offscreen pytest; CI = ruff + tests.

## 11. Phase 2 (not v1)
KDE Plasma 6 plasmoid surfacing at-a-glance health (next runs + recent failures/
gaps), reusing the pure `calendar_model`. Possible `journal-archiver` tie-in to
extend history past live retention. Separate specs.

## 12. Suggested build order (for writing-plans)
1. `calendar_model` pure core + tests (journal parse, projection parse, exact gap
   detection, bucketing) — no UI. 2. `systemd_client` fetch methods + their
   parses. 3. `calendar_view` **Day** + the stacked-widget swap + selection
   adapter (smallest end-to-end slice). 4. **Week**. 5. **Month** grid (highest
   effort). 6. **Month by-timer matrix** toggle. 7. HEALTH strip + filter strip +
   visual de-noising pass. Each phase ships working + tested; the QA Phase
   (code-reviewer + test-analyzer always; silent-failure-hunter for the fetch/gap
   paths; adversarial-tester for malformed journal/projection) runs per the
   workspace Post-Coding Process.

## 13. Open items the plan must resolve (not blockers)
- Exact `GAP_TOLERANCE` default (AccuracySec vs a fixed floor) — pick, then tune.
- The 10s-timer behavior on the Calendar page (rebuild-on-tick vs manual) — default
  rebuild-on-nav + on-tick; confirm it doesn't stomp the user's nav window.
- Projection-fetch concurrency cap for large M (system scope with many timers).
- Whether v1 places runs at completion time (simpler) or pairs `Starting` for
  start-time placement (refinement) — default completion, upgrade later.
