# QA Review — Calendar View (2026-06-20)

Three-phase QA per the workspace Post-Coding Process, run AFTER the per-task
workflow verifies (which it does not replace). Four independent Phase-A reviewers
(code-reviewer, test-analyzer, silent-failure-hunter, adversarial-tester, all
Opus-tier), Phase-B cross-examination, Phase-C synthesis by the head agent.

**Headline:** the per-task adversarial verifies passed every task, yet this review
found **three P0s** — proof of why the workspace mandates this layer separately.
All three were independently surfaced and cross-confirmed.

## Phase A — findings (by lens)

**code-reviewer (FIX-FIRST):** P0 gap-clamp-uses-win_start; P2 Day-window/Today
direction inconsistency; P2 `_month_cell_rect` recomputes `monthdatescalendar`
50+×/paint; P2 slot-at-exactly-`now` judged as gap; P2 no end-to-end pin of the
count>1 gap-region collapse.

**test-analyzer (FIX-FIRST):** P1 DST asserted in comments, never tested; P1
`compute_gaps` never tested with `coverage_start != win_start` (masks the P0); P1
byte-array MESSAGE guard test is vacuous; P1 fan-in barrier multi-timer/out-of-
order untested; P2 `_collapse` split-case (a ran slot between two missed) untested;
P2 several view seams reached only via paint-smoke; P2 model purity not mechanically
enforced; P2 weekday/weekly intervals + a JUN18 fixture date-typo.

**silent-failure-hunter (FIX-FIRST):** P0 gap coverage clamped to window not journal
(false gaps on short retention); P0 stale projection slots from a superseded window
silently accepted on rapid nav; P1 multi-trigger timers project only the first
`OnCalendar` (secondary-trigger misses never gap-detected); P1 a calproj/caljournal
failure for a left scope can wedge the fan-in / late failure-echo finalizes a stale
build; P1 `parse_run_journal` silently drops unmapped-unit records; P2 sub-second
truncation (benign); P2 `summarize`/`_glyph_color` silently bucket unknown kinds.

**adversarial-tester (FIX-FIRST):** P0 malformed `systemd-analyze` date hangs the
fan-in forever (no barrier release on parse error); P1 minutely timers lose gap
coverage past the projection cap; P1 gap `coverage_start` = win_start; P1 `_collapse`
breaks on duplicate slots (WATCH-1 is REACHABLE via overlapping exprs + DST fall-
back); P2 monotonic reaching `fetch_cal_projection` fails the whole fetch; P2
empty-`activates` row click; P2 hundreds of timers → hundreds of concurrent
projection subprocesses, no cap.

## Phase B — convergence (cross-examined, all verified against code)

- **Gap-clamp P0**: 3-way convergence (code-reviewer + silent-failure-hunter +
  adversarial-tester; test-analyzer frames it as the masking test gap). Confirmed
  `main_window.py:651 coverage_start=win_start` contradicts the `_finalize_calendar`
  docstring's "[journal-coverage, now]" claim and `compute_gaps`'s contract. On this
  machine's ~2-day journald retention, clicking ◂ blooms amber ⛌ across the
  pre-coverage region. **Solution consensus:** an unfiltered oldest-journal-entry
  probe → `coverage_start = max(win_start, oldest_entry)`; and an explicit
  **zero-records → coverage_start = now (suppress all gaps)** branch, because an
  empty result can't distinguish "nothing ran" from "all rotated out." The
  `min(run.when)` approximation under-reports (safe direction) but blinds the
  boundary when a timer legitimately never ran in-window — so the probe is correct.
- **Parse-error fan-in hang P0**: all four agree, "sharpest finding in the batch."
  `_on_finished` except (≈399-427) releases `_pending_enrich` for results/schedules
  and writes tab kinds, but `calproj`/`caljournal` fall through → `_cal_pending`
  never discarded → `_finalize_calendar` never runs → permanently blank/stale
  calendar. `parse_projection` constructs `datetime()` from a regex that admits
  `2026-13-45` → `ValueError`. **Solution:** harden `parse_projection` to skip bad
  lines (consistent with its sibling `parse_run_journal`, which already does) AND
  release the calendar barrier on a calproj/caljournal parse error / failure
  (finalize-partial). Both halves — the status-bar error is ephemeral.
- **Stale-window fan-in P0** (silent-failure-hunter; adversarial-tester agrees):
  `_cal_pending` reset wholesale + single-flight id-equality lets Build A's response
  satisfy Build B's barrier with Build A's slots on a nav-mid-flight. **Solution:** a
  per-build **generation stamp**; drop responses whose generation is stale. Compose
  with the coverage fix (stash `_cal_coverage_start` in the same stamped build).
- **Multi-trigger P1** (silent-failure-hunter + adversarial-tester; code-reviewer
  agrees): `main_window.py:611 info.calendar[0]` projects only the first expr while
  `cadence_interval_usec` sizes from the smallest of ALL — so secondary-trigger
  misses are never slots (never gaps) while secondary successes show via the
  trigger-agnostic journal → reads as a coherent-but-false pattern. **Solution:**
  project each `OnCalendar` expr (per-expr fan-out reusing `_cal_pending`); union the
  slots. Feeds duplicates → needs the `_collapse` dedup below.
- **`_collapse` duplicate-slot P1** (adversarial-tester; all agree, defeats WATCH-1's
  "unreachable"): `idx = {s: i …}` drops duplicate µs instants; DST fall-back and
  `int(timestamp())` second-flooring both produce duplicates. **Solution:**
  `sorted(set(slots))` before collapse + a split-case test.

No finding was contradicted in cross-exam; one was upgraded (WATCH-1 → live).

## Phase C — disposition (head-agent synthesis)

**FIX before merge (consensus P0/P1 + the tests that mask them):**
| # | Finding | Fix |
|---|---|---|
| F1 | P0 gap clamp = win_start | Probe oldest journal entry per build → `coverage_start = max(win_start, oldest)`; zero-records → `now` (suppress all); size projection base from the same floor; fix the false docstring. |
| F2 | P0 parse-error fan-in hang | `parse_projection` skips malformed `(in UTC)` lines (try/except like `parse_run_journal`); `_on_finished` except + `_on_failed` release `_cal_pending` for calproj/caljournal and finalize-partial. |
| F3 | P0 stale-window acceptance | Per-build generation int; tag calproj/caljournal handling; drop stale-generation responses. Stash `_cal_coverage_start` on the stamped build. |
| F4 | P1 multi-trigger projection | Fire one `fetch_cal_projection` per `OnCalendar` expr; union slots per timer. |
| F5 | P1 `_collapse` duplicate slots | `sorted(set(slots))` in `compute_gaps` before `_collapse`. |
| F6 | P2 now-boundary off-by-one | `compute_gaps` future-skip uses `slot >= now` (match the projected `> now`). |
| F7 | test gaps masking F1–F5 | Host test: oldest-run/coverage strictly newer than win_start → no pre-coverage gaps; malformed-projection → barrier releases, partial renders; stale-generation drop; multi-trigger second-slot miss → gap; `_collapse` split-case + duplicate-slot; DST projection-parse case; non-vacuous MESSAGE byte-array test; model-purity import-scan test. |

**DEFER (real, lower-priority — GitHub Issues filed):**
- DEF-CAL-01 (P1): minutely/high-freq timers lose gap coverage past the projection
  cap (~8h). Cap protects rendering; gap coverage is the cost. Issue + a code comment.
- DEF-CAL-02 (P2): no concurrency cap on projection fan-out (hundreds of timers →
  hundreds of subprocesses). Single-flight serializes per-id, not across ids.
- DEF-CAL-03 (P2): `parse_run_journal` can't distinguish "no runs" from "all records
  unmapped" — add an observability counter later.
- DEF-CAL-04 (P2): Day initial-window vs `[Today]` direction inconsistency — fold
  into the visual de-noise pass (spec §7 "recenters" is the target).
- `_month_weeks` recompute (perf smell) — fold into the visual pass; no correctness
  impact.

**Single-agent P2s judged non-issues / crash-guards (kept as-is):** `summarize`/
`_glyph_color` neutral-bucketing of unknown kinds (correct crash-guard); monotonic
never reaches `fetch_cal_projection` (build skips it at `iterations<=0`); sub-second
truncation (benign, all systemd timestamps are whole-µs).

Fix batch F1–F7 implemented next (workflow), then merge `calendar-view` → main.
