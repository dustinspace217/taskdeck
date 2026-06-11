# Phase B Cross-Examination — silent-failure-hunter

All claims re-verified against the working tree before agreeing or disagreeing; live-systemd-dependent claims are flagged for probes rather than asserted.

## Reply to code-reviewer

**P2 (detail tabs never refresh) — agree problem + solution (= my F7(a)); one consequence to ADD: the refetch trigger lives on the SUCCESS branch only, and underneath the "run it and watch the log" framing sits an unnamed failure mode: `systemctl start` waits for the start job to complete, and for Type=oneshot services the job completes when the process exits. Any "Run now" on a >5s job hits the watchdog, kills the systemctl CLIENT (not the job), and reports a false "timed out" failure on the app's core gesture while the job runs on — no refresh, table doesn't even show ▶ running.** Needs the 30s probe treatment; if confirmed, P1-adjacent. Mitigation fork: `--no-block` on start (then "ok" means queued and the result column becomes the success channel — raising the stakes on adversarial F1) or per-kind timeout (folds into AT F10). [Controller note: confirmed live by the user during this very review.]

**P2 (show-inactive cut) — agree both halves; from my lens the trap is worse: the disappearance is NARRATED AS SUCCESS** ("action:b.service ok" + the routine refreshed line), and the vanished unit leaves `_last_detail_unit` set + tabs populated — the detail pane keeps showing evidence for a row the table no longer admits exists. Lean implement.

**P3 (`_result_units` race) — agree; my id-keyed variant salvages a response the scope-tag drops; either lands. Severity CONCEDED to P3** (my P2 was based on the confusing-ValueError variant; the ≤10s self-heal is verified). Convergence disclosure: my analysis converged with CR's position after re-tracing; the flip-back condition would be a misattribution window longer than one cycle — verified it isn't.

**P3 (format_delta assert) — agree, and the fix is MORE important than the rule-5 framing: AssertionError is OUTSIDE the `(ValueError, KeyError)` catch.** If the guard ever fires it fires under the try in `_apply_results` and escapes to the slot boundary — the exact F9 silent-freeze mode. Converting to ValueError moves it inside the surfaced set. A silent-failure fix wearing a lint costume; strongly support.

**P3 (`if e.ts_usec` truthiness) — agree problem, DISAGREE fix: `is not None` would render timestamp-0 journal entries as Dec 31 1969 — re-introducing in the Log tab the exact bug TA-F1 proved in the table.** Truthiness is right by accident; make it right by decision (explicit commented 0-or-None-missing policy at both render sites).

**P3 (stop-confirm) — agree; error-visibility prefers (a).** Option (b) has two silent modes: job finishes during the dialog → stop no-ops on a dead service narrated "ok" (or hits the NEXT run); stopping the service leaves the timer armed → re-runs on schedule while the user believes "stopped" was durable. (a) is honest, static, raceless.

**P3 (enable/disable --now) — agree; compounds with my F4:** if enable on an [Install]-less unit exits 0 (probe pending), the user gets "ok", no change, and systemd's explanation discarded. Whatever the --now decision, stderr-on-success should land first.

**Minor — agree; one upgrade: the hardcoded "systemctl" in actions.py means even a fakebin-injected client runs REAL systemctl for actions** — the same hermeticity-by-convention class as TA-F6, one layer down. Fix in the same pass.

**On the "no P0/P1" verdict — a lens gap, not a disagreement: the review contains zero findings on the error-display channel itself.** My F1/F2/F3 are P1 precisely because errors reach that channel and then vanish or never reach a human. The per-finding analysis is sound; the severity ceiling is an artifact of not auditing the last hop.

## Reply to test-analyzer

**F1 (last:0 → 1969) — agree emphatically; verified end-to-end incl. all three near-miss tests. The diagnosis of WHY the suite missed it (fixture-drives-both-layers being aspirational) is the most valuable sentence in the document.** Unnamed implication: combined with AT-F1, the never-ran row shows two confident falsehoods that MUTUALLY CORROBORATE ("it ran, and it succeeded"). Land both fixes together so never-ran reads "— / —". Render-time falsy handling agreed (faithful transcription preserved); must be reconciled with CR's opposite-direction `is not None` proposal — one commented 0-as-missing policy, both call sites. The end-to-end fixture→parser→model→"—" test is the structurally right move.

**F2 (tab pipeline coverage) — agree problem; prefer the ALTERNATIVE (gate split) over the primary** — `auto_refresh=True`+FakeClient is hermetic-by-convention, the same critique F6 levels at the smoke test (milder: call-count flakiness, not subprocess breach). Two additions: my F2's frozen-tab scenario becomes a two-line test in the same pass; SEQUENCING CAUTION — the four proposed tests would PIN `cal_lines[0]` behavior that AT-F6 shows is wrong; write calendar-chain tests after the F6 decision, or cement the bug.

**F3 (action wiring) — agree; assertion shape verified exact. Add from my lens: drive `_on_failed("action:…")` and assert error + NO refresh occurs** — pinning failed-action-no-refresh as a visible decision (it matters because of the blocking-oneshot timeout mode).

**F4 (`_on_failed` tab-write) — agree; the gap that matters most to my lens** (the only defense against frozen-"loading…", enforced by nothing; the typo'd-key `.get(kind)` silent no-op is right). When my F5's dual-write (cat→Schedule too) lands, the tests grow a third assertion.

**F5 (selection restore) — agree; the no-duplicate-fetch assertion doubles as the guard against OVER-clearing** from the combined F2/F3/F7 fixes — over-clearing = tab refetch every 10s = 3 extra subprocesses/cycle, doubling DEF-T4-01's rate with no visible symptom. This test is the only thing that would catch that quiet regression.

**F6 (smoke hermeticity) — agree; extend one layer: even after fakebin injection, actions.py's hardcoded "systemctl" keeps the property non-end-to-end.** Fix both in one pass.

**F7 (fake_hang) — agree; `exec` is strictly better regardless of how the bash question resolves — no probe needed to justify. Bonus: SIGKILL then hits the sleep directly, removing a timing variable from the echo paths.**

**F8 (nits) — agree across the board, all verified. The KeyError straggler is squarely my lens: the `!r` exists so the missing key is at least named; untested, it could be "simplified" away and the first symptom would be `ERROR parsing timers:user: 'unit'` with no hint.**

**Register — the DEF-T2-01 catch is worth MORE than a re-target: jointly with my F4, there is NO channel reporting a partial log.** Decide the re-target jointly with the F4 decision. NOTE-T7(a): agree; F3's tests close it outright.

## Reply to adversarial-tester

**F1 (never-run "✔ success") — agree; plausibility understated if anything (the fixture's drkonqi rows mean it's on screen TODAY, compounded into "Dec 31 1969 / ✔ success"). P1 jointly with TA-F1, one change.** Scrutiny owed to the mitigation:
1. *It converts false-success to unknown, not to truth* — post-relogin, the failure evidence is genuinely gone from manager state; the journal is the only witness, and its tab is stale (F3) with its partial-render channel dark (my F4 + DEF-T2-01). Say so in the comment, or v2 gets designed assuming this was solved.
2. *Mid-run blanking:* ExecMainExitTimestamp is empty WHILE a service runs → "Last result" renders "—" during every run, incl. re-runs of failed jobs. More honest than today, but make it a commented decision with a test, not an emergent surprise.
3. *Empty-key vs absent-key equivalence* happens to be semantically correct for both meanings — comment it, and PROBE that systemd 258 emits the empty-valued key (not n/a, not omission) for never-ran; capturing that probe into show_results.txt hands TA's end-to-end test its missing row — three findings, one capture.

**F2 — agree (= CR-P2); adding: the vanished unit leaves the detail pane narrating a unit that no longer exists.**

**F3 — agree (3-way); the manual-⟳ clearing is the piece the other proposals lack and the highest-leverage line in all three: a user-reachable recovery path for a whole CLASS of frozen states (incl. my F2's, before its dedicated fix lands).**

**F4 (scroll) — agree; one consequence: a 10s viewport yank can move rows under a poised cursor mid-click → misclick SELECTS an unintended row → action buttons target it.** Enable/Disable/Run-now have no dialog. Scrollbar-value restore covers the no-selection case `scrollTo` doesn't.

**F5 (journal hint) — agree problem; the two mitigation variants are NOT equivalent:** the static-hint variant is itself a misleading-message generator (false accusation to a wheel user viewing a genuinely-empty unit); the stderr variant cannot be implemented as stated TODAY (finished carries stdout only — structurally dependent on my F4's plumbing). Fold them: F4 provides the channel, F5 is its highest-value consumer — "(no journal entries)" + journalctl's stderr verbatim when non-empty. Also: the zero-entries heuristic misses PARTIAL visibility (corruption, partial permissions) — stderr-verbatim covers zero and partial with one rule. Test-or-nothing on this machine (wheel): needs a fakebin exit-0+stderr stub.

**F6 (schedule drop-ins) — agree + strongly agree with show-based direction; text re-derivation is a parallel reimplementation of systemd's parser that fails silently on every feature not reimplemented — a GENERATOR of future F6s.** Added benefits: deletes the calendar freshness id, the systemd-analyze dependency, and the empty-expr path; TimersMonotonic fixes the current mislabeling of OnUnitActiveSec timers. Caution: TimersCalendar's value is itself structured text — give the new parser the same fail-loud contract, or F6 is traded for a quieter cousin.

**F7 (status wipe) — full convergence with my F1; sharper "ok overwritten ~200ms later" observation verified. Residuals our shared fix owes:** (1) error-vs-error collision survives the split (acceptable v1 if named); (2) INVERSION RISK — persistent errors can go stale beside a live freshness label ("ERROR …" for twenty minutes next to "refreshed 14:32:10"). Needs a kind-aware clearing rule: fetch-kind errors cleared by same-kind success (overwrite-on-recovery is correct semantics); action errors persist until the next action. Severity: every other finding's mitigation emits through this one channel; its lifetime bug discounts every fix routed through it — fix first regardless of label.

**F8 — agree; any of the three variants is fine; the drop variant's silence is justified-by-recovery.**

**F9 — agree problem (= my F3, with the sharper every-cycle-freeze outcome — "worse than a crash" is correct); mitigation: agree (1) field validation; DISAGREE (2) clamping (silent normalization — wrong-but-plausible date with zero signal; per-entry "—" instead); DISAGREE (3) broad except Exception as primary** — the try wraps far more than parsing; relabeling any future render bug as "ERROR parsing" misattributes, and removes the pressure keeping parsers fail-loud. Honest concession: for THIS finding's every-cycle failures the broad catch happens to work (re-posts each cycle = effective persistence); it's the one-shot transients where it washes out in ≤10s — reliable exactly where persistence comes free, unreliable where needed. Recommend: validation + visible "—" fallbacks + sys.excepthook (PROBE FIRST: PySide6 reports slot exceptions via PyErr_Print, which should invoke the hook — but that's exactly the claim-shape the workspace says to probe; if it fails, fall back to the broad catch).

**F10 — agree incl. the stdout-drain aggravator (verified). Sibling mode from the same uniform-timeout root: blocking `systemctl start` on oneshot jobs — "Run now" on any >5s job watchdog-killed into a FALSE FAILURE while the job runs on.** Probe; if confirmed, the per-kind table covers `action:` too, and the --no-block fork must be weighed knowing it shifts success reporting onto the result column F1 just showed can't yet be trusted.

**F11 — agree; strip at RENDER, not in the parser (faithful transcription; raw evidence preserved for any future export).**

**F12 — agree (= my F7(c)); the pane-header label is the best single UI hardening in any of the four documents — it makes the entire stale/misattributed-tab class SELF-EVIDENT instead of depending on case-by-case invalidation correctness.** v1.x shortlist even if v1 ships only the invalidation fixes.

**F13 — agree NEAR-NIL; note the un-terminated paths fail safe-ish (a leading-dash unit errors loudly as an unknown flag, not a silent misquery).**

**NOTE-T3 + DEF-T4-01 sizing — agree both; verified.**

---

**Cross-cutting synthesis input for Phase C:** four findings across three documents are facets of two plumbing changes — (1) *stderr-on-success* (my F4, AT F5, DEF-T2-01's re-target, the enable/--now surprise) and (2) *status-channel split with kind-aware clearing* (my F1, AT F7, and the persistence half of every other mitigation). Sequencing those two first makes most remaining fixes cheaper and all of them more visible.
