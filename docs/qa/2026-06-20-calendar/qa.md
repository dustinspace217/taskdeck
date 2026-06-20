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

Fix batch F1–F7 implemented in commit 140d856 (workflow), all gates green.

---

# Round 2 — QA of the F1–F7 fix batch (commit 140d856, 2026-06-20)

A second, fully-independent three-phase QA, run on the fix batch itself per the
workspace Post-Coding Process (a workflow's internal verify does NOT satisfy this).
Same four Opus reviewers (code-reviewer [CR], test-analyzer [TA], silent-failure-
hunter [SF], adversarial-tester [AT]), blind Phase A, then Phase-B cross-examination,
Phase-C synthesis here.

**Headline:** the batch's *barrier and failure-routing plumbing is genuinely solid* —
SF and AT both went hunting for an F3 stale-acceptance or a fan-in wedge (the class
the first QA called "sharpest") and both came back unable to break it; AT explicitly
withdrew its initial wedge after exhaustive tracing. All residual findings are in the
**gap-detection arithmetic** and **degradation signalling**, independent of the fan-in
machinery. One P1, three P2s, no P0.

## Phase A — findings (by lens)
- **CR:** SHIP, no P0/P1 regressions. CR-P2 (P2) F4's multi-trigger union has no dedup
  on the *projected-event* path (only the gap path got F5) → duplicate `projected`
  events (double glyph + `summarize().upcoming` overcount). CR-P3a/b (P3) two docstrings
  say `[coverage_start, now]` but the code is half-open. (CR also initially cleared F6.)
- **TA:** tests solid/non-vacuous, the `pending_id()` helper closes the F3 hardcoded-id
  vacuity trap. TA-P1 (P1-test) out-of-order fan-in (journal before coverage) untested;
  TA-P2a (P3) the F3 stale-drop test delivers via a `generation=` kwarg production never
  passes; TA-P2b/c (P2-test) coverage-fails and empty-scope branches untested; TA-P3
  (P2-test) the F4-union→F5-dedup seam never exercised end-to-end.
- **SF:** sound; SF-P1 (P1) finalize-partial renders a *degraded* calendar identically
  to a complete one — a coverage failure suppresses gaps → "all clear" on a window with
  real misses; only signal is the ephemeral status bar. SF-P2/P3 (P2/P3, defer) probe
  unparseable-vs-empty + skip-counter observability.
- **AT:** F3 barrier integrity SOLID. AT-P2a (P2→P3) a no-run slot exactly at `now` is
  excluded by *both* projection (`s > now`) and gap (`slot >= now`-skip) → drawn nowhere;
  AT-P2b (P2) `_has_run_near` is a stateless membership test, so one run satisfies *two*
  slots within `GAP_TOLERANCE` → a genuine miss on a sub-15-min multi-trigger timer is
  invisible; AT-P3a (P3) the `_cal_coverage_start = 0` sentinel is a latent epoch-floor
  trap; AT-P3b (P3, defer) F4 multiplies the DEF-CAL-01/02 blast radius per expression.

## Phase B — convergence (cross-examined)
- **F6 now-boundary (CR vs AT-P2a) — resolved unanimously for AT.** CR retracted on
  re-trace; SF and TA confirmed from their lenses. TA gave the clinching evidence: the
  only existing boundary test (`test_slot_exactly_at_now_is_not_a_gap`) is a `compute_gaps`
  unit test with no `projected` output, so it passes identically under both readings and
  is structurally blind to the drop — that is why F6 shipped the bug, and its inline
  comment "describes the bug as the fix." Correct partition: gap judges `slot <= now`
  (skip `slot > now`), projection keeps `s > now` — exactly one owner, tolerance handles
  the just-ran/about-to-run fuzz. AT itself revised severity to **P3** (test-blindness,
  not live data-loss: live `now` has sub-second precision), but the fix is one operator,
  so fix it.
- **The F4-union family (CR-P2 + AT-P2b + TA-P3) — the batch's real soft spot.** Three
  reviewers hit one seam from three angles (duplicate projected glyph / hidden miss /
  untested). CR's proposed `parse_projection → sorted(set())` is **necessary but not
  sufficient** (TA + AT): it dedups within one expression's response, not *across* the
  two per-expression fetches whose union creates the duplicate. AT's convergent fix:
  emit `projected` events in `_finalize_calendar` from the already-deduped `_cal_slots`
  (the same source the gap path reads), not per-fetch — which *also* collapses the
  AT-P2a boundary into a single site.
- **SF-P1 + AT-P3a — two halves of one fix.** Suppressing gaps on an unset floor (AT-P3a,
  the data-safety half) without flagging it is silent (SF's complaint); flagging without
  suppressing risks `coverage_start=0` blooming gaps to epoch (AT's complaint). Implement
  together. TA's non-vacuity guard: the locking test must assert `degraded is False` on
  the happy path, or a hardcoded `degraded=True` passes the positive test and trains the
  user to ignore the warning.
- **AT-P2b fix refinement (TA + SF):** run-consumption must be **nearest-unclaimed**, not
  first-within-tolerance, or it can mis-assign a run and false-gap the wrong slot.

No finding was contradicted in cross-exam except the F6 disagreement, which resolved
cleanly on the test-existence evidence.

## Phase C — disposition (head-agent synthesis)

**FIX before merge (Round-2 batch):**
| # | Finding(s) | Sev | Fix |
|---|---|---|---|
| R2-1 | CR-P2 dup projected events + AT-P2a now-boundary drop | P2 | Emit `projected` events in `_finalize_calendar` from `sorted(set(_cal_slots[unit]))` (drop the per-fetch emission in `_on_cal_projection`), so projected + gap share one deduped source; `compute_gaps` judges `slot <= now` (skip `slot > now`); rewrite the inverted F6 comment; the two CR-P3a/b docstrings become correct-as-written once the interval is closed. |
| R2-2 | AT-P2b one run hides a second nearby miss | P2 | In `compute_gaps`, consume runs — each run satisfies at most one slot, **nearest-unclaimed** within tolerance. |
| R2-3 | SF-P1 degraded render + AT-P3a unset-floor sentinel | P1 | `_cal_coverage_start` sentinel → `None`; `_finalize_calendar` suppresses gaps when the floor is unset AND sets `degraded=True`; the flag flows to `set_events` → health strip shows "⚠ partial — some data failed to load (⟳ to retry)". Boolean only (per-layer qualifier → DEF-CAL-08). |
| R2-4 | TA-P1/P2a/P2b/P2c/P3 test gaps | — | Out-of-order fan-in (journal before coverage); F3 stale-drop via the production `_on_finished` path (no `generation` kwarg); coverage-fails → `degraded` True AND happy-path `degraded` False; empty-scope build; F4 coincident-slot end-to-end (one gap + one projected + upcoming once); replace `test_slot_exactly_at_now_is_not_a_gap` with a now-instant-IS-judged test. |

**DEFER (real, lower-priority — register + file):**
- DEF-CAL-05 (P2, SF-P2): coverage probe can't distinguish an unparseable stream from a
  genuinely-empty journal (both → suppress, the safe direction). Twin of DEF-CAL-03; add
  a "got output, parsed nothing" tripwire when DEF-CAL-03's observability counter lands.
- DEF-CAL-06 (P3, SF-P3): `parse_projection`'s malformed-line skip has no diagnostic counter.
- DEF-CAL-07 (P3, AT-P3b): F4's per-expression fan-out multiplies DEF-CAL-01's cap blind
  spot and DEF-CAL-02's subprocess count by exprs-per-timer — annotate both existing issues.
- DEF-CAL-08 (P3, CR/AT): the `degraded` flag could name *which* layer failed (coverage →
  under-report / journal → phantom gaps / one projection → scoped under-report); v1 ships
  the boolean.

R2 implemented in commit 52e06f4 (workflow: impl → adversarial verify), gates green.

## Stabilization pass (commit 52e06f4) — code-reviewer + test-analyzer

Per the follow-up-pass rule (slim to code-reviewer + test-analyzer), an independent
Opus pair reviewed the R2 diff. **Both returned SHIP** — no P0/P1/P2 regressions; the
projected-emission move, the run-consumption rewrite, the None-sentinel suppression,
and the degraded flag all confirmed correct and the R2 tests confirmed non-vacuous
(including the discriminator that rejects a first-within-tolerance greedy).

One P3 finding from test-analyzer was acted on rather than deferred: the `_cal_degraded`
assignment in the `_on_finished` **parse-error** except branch was unpinned (its sibling
`_on_failed` path is tested). Writing that test (`test_parse_error_in_calendar_handler_
marks_degraded_and_releases`) **revealed a real latent defect both the R2 verifier and
both stabilization reviewers missed by reasoning rather than executing:** the journal and
projection handlers discarded the id from `_cal_pending` *before* the parse, so a parse
error left the except branch's `request_id in _cal_pending` guard False — the documented
finalize-partial + degraded behavior was dead code (the branch's comment promised behavior
the code couldn't deliver). The critical no-wedge property still held via the early
discard, so the live impact was nil (P3 — and the parsers are hardened not to raise today),
but the defense was a lie. **Fixed** (commit pending): both handlers now discard *after*
the parse, so a parse error leaves the id pending and the except branch finalize-partials
+ marks degraded as documented. Gates green: 235 hermetic + 5 realsystemd, ruff, mypy.

Remaining P3 observations (non-blocking, no action): the dead-in-production `generation`
kwarg on `_on_cal_projection` (documented belt-and-suspenders; the F3 test exercises the
real path without it); the by-design double-iteration of `_cal_slots` in `_finalize_calendar`.

DEF register additions filed this round: DEF-CAL-05 (probe unparseable-vs-empty), DEF-CAL-06
(parse-skip counter), DEF-CAL-07 (F4 multiplies DEF-CAL-01/02), DEF-CAL-08 (per-layer degraded
qualifier). Merge `calendar-view` → main next.
