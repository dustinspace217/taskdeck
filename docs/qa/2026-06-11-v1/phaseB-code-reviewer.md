# Phase B Cross-Examination — code-reviewer replies

All claims verified against the code, not taken from the Phase A reports.

## Reply to test-analyzer

**Finding 1 (`last:0` → 1969) — AGREE, verified link by link; P1 agreed. The best catch in the whole Phase A set, and it refutes part of my own Phase A.** Two never-ran timers in the fixture have `"next":null,"last":0`; the parser passes 0 through; `format_when` special-cases only None; the sort key gets 0 instead of the sentinel. **Self-correction:** my Phase A P3 recommended `is not None` for `_fill_tab`'s `if e.ts_usec` — Finding 1's evidence shows the truthiness is *accidentally correct* (0 is systemd's "never" encoding) and my fix would have introduced the 1969 bug into the Log tab. **I withdraw that fix direction**; truthiness stays, with a 0-means-never comment.

**Solution: agree on decomposition, DISAGREE on normalization point.** Normalize in the PARSER: `last_usec=item.get("last") or None` — one expression fixes display AND sort key at one site, and it doesn't violate faithful transcription because 0 IS systemd's encoding of "never triggered" (fixture: `last:0` co-occurs with `passed:0`, `next:null`). The parser already maps JSON null→None; mapping systemd's other "no timestamp" encoding to the same None is the same job. TimerRow docstring gets a one-line amendment citing the probe. The proposed tests (drkonqi-row parser test + fixture→model end-to-end) are right regardless of side — the end-to-end test makes the "same fixtures drive both layers" docstring true instead of aspirational (verified currently false).

**Cross-link for Phase C:** this and adversarial F1 are the *same table row lying twice* — "Dec 31 16:00 (20614d ago)" AND green "✔ success". Both fixes live in the parse layer; one shared drkonqi-row regression test pins both.

**Finding 2 (tab pipeline 0% coverage) — AGREE; "structurally untestable" is overstated** (their own Finding 4 disproves it: `_fill_tab` + freshness set are reachable today by seeding `_expected_tab_ids` and calling `_on_finished`). Only `_on_selection`'s fetch branch is gated. **Solution: agree option 1 (`MainWindow(FakeClient(), auto_refresh=True)` — traced safe), REJECT option 2** (splitting the gate = production churn to serve tests; fails smallest-sufficient). The calendar-chain ordering invariant being enforced only by a comment is the strongest part of the finding.

**Finding 3 (`_do_action` untested) — AGREE; one dependency claim is WRONG:** `_do_action` is not gated by `_auto_refresh` at all — populate, selectRow, call it: writable today, zero production changes. The proposed argv assertion is correct. Modal-blocks-only-stop verified.

**Finding 4 (`_on_failed` tab-write untested) — AGREE both halves.**

**Finding 5 (selection restore untested) — AGREE, P3 fair.** Test-design note: the selection clears *silently* during reset (QItemSelectionModel::reset emits no signals) — assert no duplicate `fetch_log` to pin the dedup.

**Finding 6 (smoke hermetic by convention) — AGREE; fakebin injection is zero-behavior-change hardening.**

**Finding 7 (`fake_hang` orphan) — AGREE and strengthen: bash does NOT exec-optimize script files (only `-c` strings), so the orphan likely occurs today, not "may."** Still P3. `exec sleep 30` correct.

**Finding 8 (nits) — mostly agree, one pushback:** the "dead assertion arm" (`is None or > 1e15`) is NOT dead in intent — it's tolerance for a legitimate re-capture where that timer has no next elapse; removing it creates exactly the false-failure class their own re-capture nit warns about. Keep the arm; document it. Redundant start-verb test verified; KeyError-branch test gap verified; color-assertion nit fair.

**Register checks — agree on both** (DEF-T2-01 re-target to v2; NOTE-T7(a) reframe composes with Finding 3 — three of four verbs were never modal-blocked).

## Reply to silent-failure-hunter

**F1 (status washout) — AGREE, verified; nuance: recurring fetch errors re-post each cycle, so the wipe-out is permanent only for ONE-SHOT errors — precisely the action-failure case. Severity arbitration: P1-for-action-errors / P2-overall** (vs AT F7's P3 — the occurrence-HIGH framing supports upgrade). **Solution: implement this version (permanent widget split; AT's fix is identical, no conflict).** The error-survives-refresh test lands with the fix.

**F2 (parse failure → frozen loading…, retry blocked) — AGREE, mechanism verified end-to-end. Severity: P2, not P1** — the only realistic trigger is a wholly-unparseable journal tail; details/cat/calendar branches are pure string ops that can't raise. **Solution: agree** — shared kind→tab constant, `(parse failed)` tab write, `_last_detail_unit = None` reset. Composes with the action-branch reset (same idiom, second site).

**F3 (catch gap) — AGREE, verified. Precision: moderately absurd epochs raise plain ValueError (already caught); only extremes reach OverflowError/OSError. Fix fork settled: broaden `_on_finished`'s catch to `Exception`** (one line; the handler's response — loud status-bar error with id and `{exc!r}` — is correct for every exception class, so the narrow-catch argument doesn't apply at this external-program trust boundary) **plus SFH's isinstance checks for message quality.** Closes the entire class; makes the excepthook backstop optional rather than load-bearing.

**F4 (success-stderr discarded) — AGREE; the probe gate on enable-no-Install is exactly right (verify-behavior rule — don't assert it). Cross-link: AT F5 shares this root completely** — read-stderr-on-success is one fix closing both findings plus half of DEF-T2-01's pairing concern. The convergence raises the fix's value above its individual P2.

**F5 (cat failure strands Schedule tab) — AGREE, verified; one-line fix right.**

**F6 (cross-scope results race) — AGREE; three independent finds (my P3, AT F8). Fix fork: implement SFH's by-id dict** — it encodes the actual invariant ("this unit list belongs to the exact request whose argv built it") rather than approximating with a scope check, and the rejected-fetch special case falls out naturally (on rejection, don't store; the existing entry is the aligned one). Bounded at 2 keys. The regression test updates to the new field; that's the whole cost.

**F7 (staleness family) — (a) AGREE (convergent, same fix). (c) AGREE, verified. (b) REFUTED as proposed:** "clear on `_selected() is None`" never fires in the vanished-unit scenario — the model reset clears selection WITHOUT a signal (QItemSelectionModel::reset is documented signal-free; that's why `_reselect`-refires-`_on_selection` works at all). When the unit vanishes, `_reselect` finds no match and returns; `_on_selection` never sees None. **Correct fix site: `_apply_results`/`_reselect` — when the kept unit can't be re-found, clear `_last_detail_unit` there.** Problem real; patch as written wouldn't fix it.

**F8 (transition window) — AGREE, verified; `_data_scope` is the smallest surgical option — endorse over blanking the table (flicker). NOTE-T7(b) re-grade: partial disagree** — the dangerous half is the SCOPE transition (this F8); view-switch staleness keeps the same scope and actions target the actually-rendered row (self-consistent roles) — display lag, not wrong-target. LOW stands for T7(b); F8 carries the real risk separately.

**F9 (`_pending` on failure) — AGREE with accept+document.** Render-with-data-on-hand risks pairing fresh services with stale timers; last-good-plus-visible-error is arguably the better contract.

**F10 (bootstrap) — agree throughout;** ImportError catch matches the workspace tool-installation philosophy; the DEF-T4-01 "don't fix this into the segfault zone" comment is worth adding verbatim.

## Reply to adversarial-tester

**F1 (never-run "✔ success") — AGREE; with TA Finding 1, the most consequential pair in Phase A.** Verified: the disambiguating evidence (ExecMainExitTimestamp) is fetched and discarded; the codebase documents the default-not-evidence trap for ExecMainStatus and never applies it to Result. Failed-job-then-relogin = failure evidence replaced by affirmative green success — the worst wrong answer this tool can give. **Solution AGREE; edge cases land right** (first-run-in-progress → "—" correct; post-relogin → "—" correct). Same drkonqi fixture row drives regression tests for this AND `last:0` — the same row lying in two columns.

**F2 — identical to my P2; AT's trap framing is the sharper argument. Implement the toggle.**

**F3 — AGREE face one; face two's fix has the same flaw as SFH F7(b) — refuted (signal-free selection reset). Fix belongs in `_apply_results`/`_reselect`.** Action-completion and manual-⟳ clears right; ⟳ needs a small wrapper (QAction currently connects straight to `refresh`).

**F4 (scroll yank) — AGREE; unique find, well spotted. P2 right** — degrades the exact leave-it-open usage. Scrollbar-value capture/restore over `scrollTo` (works without a selection).

**F5 — AGREE; shares its root with SFH F4 (stderr unread on exit 0). One fix closes both; implement together.**

**F6 (Schedule tab vs drop-ins) — AGREE all three problems; solution needs a Phase C scope call.** The `show -p TimersCalendar,NextElapseUSecRealtime` approach is genuinely better — and would DELETE the cat→extract→analyze chain (the most intricate and least-tested orchestration per TA Finding 2: negative complexity). But it's a redesign of a working-common-case flow — under minimal-diff, register as deferment with the direction written down, or first follow-up; not mid-stabilization. Caveat for implementer: monotonic timers populate TimersMonotonic, not TimersCalendar — handle both.

**F7 — same root/fix as SFH F1. Severity: P3 understates the action-error subcase; P1-for-actions/P2-overall.**

**F8 — three-way convergent; implement SFH's by-id dict (the tuple verifies an approximation; the dict IS the invariant).**

**F9 — AGREE; the recurrence framing (every cycle dies identically, frozen table labeled fresh) is the real contribution. The "optionally broaden to Exception" aside should be the PRIMARY fix** — one line closing the class; isinstance checks layered for message quality. Despite "P3 individually," this is the highest-leverage small fix of the entire review.

**F10 — AGREE, P3 fair; per-kind timeout over `--since` (which silently changes what the Log tab means). Stdout-pinning aggravator verified — the timeout path never drains; register note warranted.**

**F11 — AGREE; the epoch-0 aside supersedes my own Phase A P3 (withdrawn).**

**F12 — AGREE; identical to SFH F7(c); clear set + blank tabs in `set_scope` (the latter also closes my "tabs lie after scope switch" P3). Pane header label = v1.1 scope creep.**

**F13 — AGREE NEAR-NIL; implementation caveat if hardened: argv must become flags-first (`show -p PROPS -- unit`) — appending `--` in the CURRENT order would break `-p`. Note `fetch_log` is safe by accident (`-u` consumes the next token).**

**Register: NOTE-T3 escalation is a refutation-grade catch, CONFIRMED** (alphabetical collection puts qtbot-using test_client before test_format; QApplication's setlocale runs first TODAY; the note's hypothetical framing is stale). Take the one-line LC_TIME=C conftest fix. DEF-T4-01 stdout-drain interaction verified — register line warranted.

---

## Cross-cutting summary for Phase C

1. The never-run row carries TWO independent lies (TA-F1 epoch-0 date, AT-F1 default-success) — fix both in the parse layer, pin both with the real drkonqi fixture row.
2. The catch-gap (SFH F3 + AT F9) resolves to broaden-to-`Exception` + isinstance checks — one line closes the class.
3. The status-bar fix is unanimous (permanent widget); only the severity label was contested (P1-for-actions/P2-overall).
4. The `_last_detail_unit` invalidation family (my P2, SFH F2/F7, AT F3) is ONE idiom at four sites — but the vanished-unit site must be `_apply_results`/`_reselect`; the clear-on-None patch proposed by both SFH and AT is REFUTED by Qt's signal-free selection reset.
5. `_result_units` goes by-request-id (SFH's version).
6. I withdraw my own Phase A `is not None` fix for `_fill_tab` — the fixture evidence reverses it.
