# Phase B — Adversarial-Tester Cross-Examination (Task Deck v1)

All claims re-verified against the working tree before commenting; line numbers checked, not recalled.

## Reply to code-reviewer

**P2 (tab staleness after actions) — agree problem (3-way convergence); the proposed fix is correct but incomplete, and I can break it: RACE B.** Select unit X (its `log:user:X` journalctl spawns), click ▶ Run now within a second or two, action completes (~200ms), refresh lands, `_reselect` re-fires `_on_selection`, dedup misses as designed — but `fetch_log` returns **False** (the pre-action journalctl is still in flight) and line 345 ignores the return. The pre-action process then exits, its id matches the freshly REBUILT `_expected_tab_ids` (same strings), and the Log tab fills with **pre-run journal content** — the exact symptom the fix targets, now intermittent. Worse: `_last_detail_unit` was re-set BEFORE the ignored-return fetches, so it never self-heals; subsequent cycles dedup-skip forever. High plausibility: the journal fetch only has to outlive ~500ms, and `-n 200` reverse-seek does that routinely (my F10).

**On the record: this P2 fix falsifies the same report's P3 rule-7 finding.** The ignored fetch returns were "behaviorally benign" only because the dedup guaranteed no same-id fetch in flight; clearing the dedup destroys that invariant and the ignores become load-bearing bugs. Reconciling diff: capture the three booleans; if ANY fetch was rejected, leave `_last_detail_unit = None` so the next cycle retries (bounded — watchdog caps in-flight life at 5s). That one change discharges CR-P2, CR-P3(rule 7), my F3, and the F10 interaction simultaneously. Mirror-the-comment is no longer the right rule-7 fix; checking the value is.

**P2 (show-inactive) — agree register-or-implement. Implementation trap if toggled:** the results dict is a local in `_apply_results` — a naive toggle wiring calls `refresh()`, and a flip during an in-flight refresh gets single-flight-rejected: the checkbox appears **dead for up to 10s**. Cache the last results dict for local re-render, or accept and comment the latency.

**P3 (`_result_units` scope tag) — agree problem (my F8); disagree on the variant.** Tag-and-verify DROPS a usable response in the flip-back-flip case; SFH's id-keyed dict parses it correctly against the argv that produced it — strictly more correct at the same line count. **Updating my own F8 recommendation to the id-keyed variant.**

**P3 (format_delta assert) — agree; no break found** (probed: format_when can't pass a negative; `-0.0` lands in the `>= 0` branch). Free fix.

**P3 (`if e.ts_usec` truthiness) — agree inconsistency; DISAGREE with the fix direction.** `is not None` would render corrupt journal timestamp-0 entries as Dec 31 1969 — importing the table's proven bug into the Log tab. The truthiness BEHAVIOR is right; the IMPLICITNESS is the defect. Fix: explicit commented helper (`ts_usec is None or ts_usec == 0`, citing the drkonqi fixture rows) shared by both render sites.

**P3 (stop-confirm text) — agree problem; option (b) is breakable, recommend (a).** (b) dispatches a button's meaning on a transient state: job finishes during the dialog → stop no-ops on a dead service and the timer stays armed ("ok" to a user who wanted to cancel the schedule); inverse for the mid-run canceller. Per-view dialog text (a) is the unambiguous smallest fix; two-target dialog is v2.

**P3 (enable/disable --now) — agree surprise; `--now` needs SFH F4's probe first** (`disable --now` relocates the surprise: the running service keeps going; `enable --now` hits the no-[Install] territory). Register-as-known-v1 is smallest sufficient.

**Minor items — agree all four.** The raw "action:X ok" string gains importance under F1's persistence fix — phrase it while touching that code.

**Insight:** the rule-5 praise of `data()`'s bounds check pairs with the under-appreciated fact that `QItemSelectionModel::reset()` emits NO signals — several Phase A fixes implicitly assume otherwise (detailed under SFH F7(b)).

## Reply to test-analyzer

**Finding 1 (`last:0` → 1969) — agree, P1 confirmed, all links verified.** Compounding detail: those rows render "○ inactive" + (under my F1) "✔ success" — the never-ran row is a *three-cell compound lie*: "inactive, last ran Dec 31 1969, succeeded." TA's fix and my F1's fix are complementary halves of honest rendering and should land together.

**The truthiness reconciliation (vs CR-P3):** orthogonal once split into semantics vs style. *Semantics:* TA is right — fixture proves 0 is systemd's "never" sentinel; genuine epoch-0 µs can't occur by accident in any rendered field; note the codebase currently has it BOTH ways (Log tab folds 0 into "—"; table renders 1969). *Style:* CR is right — bare falsiness smuggles a data-contract decision past the reader. *Reconciliation:* one explicit commented helper used by `format_when`, the log stamp, and the sort-key sentinel. CR's `is not None` must NOT be applied as written. My own F11 parenthetical was the weakest of the three positions; the helper supersedes it. Secondary: TA's rejection of parser-normalization (faithful transcription) is right; the end-to-end fixture→model→"—" test is the single highest-value test in the document.

**Finding 2 (tab pipeline coverage) — agree problem; the PRIMARY fix is breakable, the ALTERNATIVE is right.** `MainWindow(FakeClient(), auto_refresh=True)`: (i) constructor immediately calls refresh() → two phantom call-list entries taxing every assertion; (ii) the live 10s QTimer can absorb a spurious refresh during a generous waitSignal on a slow box — classic 1-in-50 flakiness. The gate-split (~3 lines) has neither edge. **Endorse the split as primary.** Once these tests exist, add the Race B regression — only this new test surface can pin it.

**Finding 3 (`_do_action` untested) — agree all four; verified the argv tuple; confirmed testable today** (not gated by auto_refresh). Additions: assert the "start a.service…" status message; add the Services-view variant (activates == unit, currently comment-enforced).

**Finding 4 — agree; add to the negative twin: assert the status bar still carries the error when the tab is untouched** (under F1's persistence fix it becomes the only channel for unexpected-id failures).

**Finding 5 — agree; also pin the vanished-unit variant** (reset emits no signals — second cycle missing the selected unit → selection empty, tabs stamped post-combined-fix).

**Finding 6 — agree; pin all THREE injectable binaries, not just systemctl.**

**Finding 7 — agree incl. the honest "unverified" framing; verified the comment line between shebang and sleep makes exec-optimization the deciding detail. `exec sleep 30` is unbreakable.**

**Finding 8 — agree all; verified each.** Fixture prerequisites need one more entry once Finding 1's test lands: "≥1 never-ran timer emitting last:0" — a freshly-imaged machine whose drkonqi timers have all fired would otherwise produce a valid-looking fixture failing the new test for non-bug reasons.

**Register — agree both.** DEF-T2-01: three-way convergence on re-targeting at the F4/F5 stderr cluster — re-target once, not three times. NOTE-T7(a): Finding 3 closes it outright, better than re-grading.

## Reply to silent-failure-hunter

**F1 (refresh overwrites errors) — agree (my F7); adopt SFH's P1-for-action-errors severity split over my flat P3.** Break in the fix: a persistent error with nothing routine to displace it **never clears after the condition resolves** — one transient timeout at 14:02 leaves "ERROR…" on screen at 15:30 (inverse lie: healthy app labeled broken, undatable). Hardening: timestamp the error text (`ERROR 14:02:11 […]`) + clear on next explicit user action. The error-survives-refresh test should also assert the timestamp.

**F2 (parse failure strands tabs) — agree; the auto-retry half is breakable.** Resetting the dedup on parse failure turns a *persistently* corrupt journal — SFH's own chosen trigger — into a 10s loop: every cycle re-flashes "loading…", spawns three subprocesses, fails again — loud, but doubles DEF-T4-01's accumulation rate and strobes the pane. Cleaner: write `(parse failed)` into the tab (keep the core insight) but LEAVE the dedup; retry rides the ⟳-clears-dedup path — user-triggered, no loop, error visible meanwhile. The detail-kinds-only scoping was correct; keep it explicit.

**F3 (exception-class gap, = my F9) — agree; layer-1 incomplete against F9(a)** (null field inside a valid list passes the isinstance check, explodes later in `sorted({None, str})` — TypeError outside the catch). Per-field validation is the necessary complement. **Layer-3 probe required:** PySide6 reports slot exceptions via PyErr_Print, which should invoke sys.excepthook — but that's exactly the claim-shape the workspace says to probe (30s: raise in a slot, confirm). If the probe fails, fall back to broad `except Exception` at the trust boundary.

**F4 (success-stderr) — agree problem + probe; the minimal fix produces FALSE ALARMS that neutralize the channel:** `systemctl enable/disable` print informational "Created symlink…"/"Removed…" to stderr ON SUCCESS — `ok — warning: {stderr}` would fire on every successful enable/disable, training the user to ignore it within a week. Don't pattern-filter (brittle, localized); present stderr verbatim as secondary text without "warning" framing, and stage the rollout: journalctl corruption lines first (no informational noise there), actions after the enable probe.

**F5 (cat failure → Schedule tab) — agree; fix survives my break attempts.** Document one adjacent property while in there: two units sharing an OnCalendar expression — the second's rejected `fetch_calendar` is filled by the first's in-flight response CORRECTLY because the id is content-keyed. That collision-is-benign property is load-bearing; a unit-keyed "improvement" would break it.

**F6 (cross-scope results) — agree; SFH's id-keyed dict WINS the three-way comparison** (tag-verify drops a perfectly-aligned response in flip-back-flip; the dict parses it against its own argv). Hygiene: pop the entry in `_on_failed` too; keep `test_rejected_results_fetch_keeps_old_unit_list`'s guarantee through the mechanics change.

**F7 + the COMBINED-fix stress test (with CR-P2 and my F3) — three new failure modes found:**
1. **Race B** (a × single-flight): post-action refetch rejected while the pre-action journalctl is in flight → late response passes the rebuilt freshness set → pre-run content presented as the fresh run, permanently. Mitigation: check the three fetch booleans, leave `_last_detail_unit = None` on any rejection (self-heals within one cycle; also discharges CR's rule-7 P3). Do NOT clear `_expected_tab_ids` in the action branch instead — that flips the failure to a permanent "loading…" freeze.
2. **(b) is wired to a signal that doesn't fire:** `QItemSelectionModel::reset()` emits no signals, so the vanished-unit case never calls `_on_selection`; clear-on-None only runs on explicit user deselection. The hook must live in `_apply_results`: `_reselect` returns a bool; clear+stamp when restoration fails. Pin with TA Finding 5's vanished-unit variant.
3. **(a) × (c) scope interleaving:** an `action:X` completion landing after a scope flip clears the dedup and refreshes the NEW scope — functionally fine, but "action:X ok" names a user unit under the System label, and under F1's persistence it lingers. Include the scope while rephrasing the message (CR's raw-id nit — same line).

With those three patches the combined fix held against everything else (double-action, mid-action flip-back, ⟳ during loading…, filter active during reselect). My F12 pane-header label remains the cheap structural complement.

**F8 — agree, mechanism verified; `_data_scope` needs one wiring addition:** enablement is re-evaluated only via selectionChanged, and a reset that fails to restore the selection re-evaluates NOTHING (no-signal trap) — call `_update_action_enablement()` explicitly at the end of `_apply_results`.

**F9 (`_pending`) — agree; accept-and-document.** Render-with-data-on-hand creates a mixed-generation table whose subtle wrongness is harder to spot than the one-cycle delay it removes.

**F10 (bootstrap) — agree; rule-7 nit: the ImportError catch must exit non-zero** (a zero-exit "PySide6 missing" is itself a small silent failure for launcher scripts).

**Register — agree both; consolidate DEF-T2-01's three views (pairing-upgrade, stale target, stderr-cluster home) into ONE re-target when Phase C touches it. NOTE-T7(b)'s failure-path half = my F8; resolve together.**
