# Code Review — Task Deck v1 (Phase A, independent)

**Reviewer lens:** bugs, logic errors, edge cases, style/convention adherence (project CLAUDE.md + workspace Power of Ten).

## Overall assessment

This is a clean, disciplined codebase. The hard problems — async subprocess lifecycle with stale-echo suppression, block/unit alignment in `parse_show_results`, freshness gating of detail-tab writes — are solved carefully, commented with probe evidence, and pinned by targeted regression tests (the identity-guard scheme in `systemd_client.py` plus `test_timeout_emits_exactly_one_terminal_signal` is exactly how this should be done). The read-only system-scope contract is enforced in two independent layers as promised. I found **no P0/P1 issues**. What I did find: one functional gap in the core "run it and watch the log" loop (P2), one unregistered spec cut (P2), and a handful of P3 edge cases and convention nits. The registered deferments' severities all look fairly judged.

## P2 — Detail tabs never refresh while a unit stays selected, including after "Run now"

**Problem.** `main_window.py:334` — `_on_selection` dedups on `self._last_detail_unit`, which is only cleared on scope change. After a successful action, `_dispatch_finished`'s `"action"` branch refreshes the *table* but not the tabs: the post-refresh `_reselect` re-fires `_on_selection`, which hits the dedup and returns. Clicking the already-selected row doesn't re-fire `selectionChanged` either. Net effect: once a row is selected, its Log/Details/Schedule/Unit-file tabs are frozen until the user selects a *different* row and comes back. After "▶ Run now", the Log tab keeps showing the pre-run tail.

**Why it matters.** This is the app's core loop — run a job, watch its log. The plan's own acceptance criterion (Task 8 Step 4) expects "Run-now … with the log tab showing the fresh run." The implemented behavior cannot satisfy that check.

**Proposed solution.** Smallest fix: in the `"action"` branch of `_dispatch_finished`, set `self._last_detail_unit = None` before `self.refresh()`. The refresh's selection restore then refetches the tabs for the selected unit. (Optionally also clear it on the manual ⟳ Refresh action, so ⟳ becomes a "reload everything" gesture.) The dedup itself is correct and should stay.

## P2 — Spec's "show inactive" toggle silently cut; not in the deferments register

**Problem.** Spec: "Services view default filter: hide `inactive` units **unless 'show inactive' is toggled**." The hide is implemented (`main_window.py:272`), the toggle is not — there is no way to see inactive services at all. The plan's self-review registers the ✘→Log-tab jump as a conscious cut but says nothing about this one — an *unregistered* deviation.

**Why it matters.** A loaded-but-inactive user service is invisible, so it can't be started/enabled from the app (Enable/Start on stopped services is squarely in the v1 verb set). Process-wise: cuts get registered with severity and fix direction; this one slipped through.

**Proposed solution.** Either add the toggle (a checkable QAction feeding the list comprehension — small) or register the cut as a deferment with a defer target. Lean: register for v1, implement in the first follow-up.

## P3 — `_result_units` isn't scope-tagged: narrow cross-scope race can misattribute last-results

**Problem.** `_result_units` is a single field shared across scopes, while the single-flight keys are per-scope. A user→system→user flip inside one results-subprocess lifetime can parse a stale `results:user` response against the system unit list — silent wrong-row attribution. Requires the flip inside ~5s and self-heals next cycle — hence P3. **Proposed solution.** Store the scope with the list (or per-scope dict); `_apply_results` verifies before parsing. ~3 lines.

## P3 — Power of Ten rule 7: unmarked ignored return values in `_on_selection` / `_fill_tab`

The three selection fetches and `fetch_calendar` discard the bool single-flight result with no marker, while `refresh()` does this right with an explicit comment. The ignored cases are behaviorally benign (traced), but the reasoning is non-obvious enough that the comment is load-bearing. **Fix:** mirror the `refresh()` comment.

## P3 — `format_delta`'s assert is a check-without-recovery (rule 5) and vanishes under `python -O`

`models.py:26`. **Fix:** `if seconds < 0: raise ValueError(...)` — same loudness, survives `-O`.

## P3 — `if e.ts_usec` truthiness conflates `0` with `None`

`main_window.py:355-358` — convention inconsistency (everywhere else is scrupulous about `is None`). **Fix:** `is not None`.

## P3 — Stop confirmation text doesn't match what Stop does on a timer row

The dialog warns "Stopping can interrupt a job mid-run," but in the Timers view Stop targets the `.timer` (cancels future scheduling; never interrupts the running service). A user trying to kill a runaway job from the Timers view stops the wrong unit and the job keeps running. **Proposed solution:** (a) per-view dialog text, or (b) Stop on a timer row targets the activated service when running — a design fork for Dustin (the spec's own wording suggests intent (b)).

## P3 — Enable/Disable without `--now` won't change current state — likely user surprise

Plain `enable`/`disable`: on a timer, Enable doesn't start it (status stays "○ inactive" until next login); Disable leaves an armed timer firing until separately stopped. Spec-conformant (four bare verbs promised) but reads as a bug after refresh. **Proposed solution:** consider `--now`, or register as known v1 behavior.

## P3 — Assorted minor items

- Raw request-id leaks to the status bar: `"action:a.service ok"` — the only non-human-phrased message.
- `actions.py` hardcodes `"systemctl"` while SystemdClient takes injectable paths — inconsistent for future tests.
- Spec says refresh runs "while window focused"; implementation refreshes whenever open (incl. minimized). Negligible load, arguably better UX — unregistered micro-deviation; one sentence closes it.
- Scope switch leaves detail tabs showing the previous scope's unit with nothing selected — a brief lie in the UI; clear to a placeholder.

## Deferment severity check

All registered severities fairly judged. DEF-T4-01 (MEDIUM): appropriate; the "~3 per 10s" estimate omits per-selection fetches but doesn't change the order of magnitude. DEF-A-01/DEF-T2-01 (LOW): fine. NOTE-T7(c): confirmed accurate.

## Power of Ten compliance (explicit pass, rules 1-10)

Rules 1, 2, 6, 8, 9 clean. Rule 3: bounded except known DEF-T4-01. Rule 4: largest function ~45 code statements — within budget without trimming comments. Rule 5: good (the `data()` bounds check is a model example) except the `format_delta` assert. Rule 7: good except the unmarked ignores. Rule 10: ruff superset + mypy strict from first commit, PySide6 stub limitation honestly documented.

## What's genuinely good

- `parse_show_results`'s line-walking alignment under empty blocks, pinned by leading/middle/distinct-values tests.
- The identity-guard (`is proc`) scheme with its exact regression test.
- Freshness gating by full-id string equality with the colons rationale; content-keyed `calendar:{expr}` id.
- `--` argument terminators in both `action_argv` and `fetch_calendar`, with probe dates.
- Epoch-space delta with DST rationale; deterministic injected `now` throughout tests.
- The deferment register itself: severities, probe evidence, fix directions, obsolescence conditions.

**Verdict:** Ship-quality core with two P2s to address (or consciously register) before calling v1 closed — the post-action tab staleness directly collides with the pending Task 8 manual check.
