# Phase B Cross-Examination — test-analyzer replies

All claims re-verified against the source, suite, fixtures, spec (line 107), and the deferments register. Own Phase A findings cited as TA-1…TA-8.

## Reply to code-reviewer

**CR-P2-1 (detail tabs never refresh, incl. after Run now) — problem AGREE, verified.** The 3-way convergent finding (= SFH F7a, AT F3). **Solution AGREE — of the three proposed fixes, CR's one-liner is the one to implement** (`_last_detail_unit = None` in the action branch before `refresh()`); SFH F7(b) and the manual-⟳ variant are separable follow-ups. **Testability caveat: not currently testable hermetically** — `_on_selection` returns before any fetch when `auto_refresh=False`, so TA-2's enabler must land FIRST (`MainWindow(FakeClient(), auto_refresh=True)` is safe — FakeClient records, the 10s QTimer can't fire within a test — or split the gate). **The single locking test:** populate via `_on_finished` → `selectRow` (one `fetch_log` recorded) → `_on_finished("action:a.service", "")` → complete the triggered refresh → assert a SECOND `fetch_log` in `client.calls`. Fails on current code, passes with the one-liner, pins all three reports at once.

**CR-P2-2 (show-inactive toggle cut) — AGREE, verified** (spec line 107 vs the unconditional filter). Convergent with AT F2 (the sharper consequence). **Implement, don't just register.** Locking test: toggled twin of `test_services_view_hides_inactive` — testable today, no gate fix needed.

**CR-P3 (`_result_units` not scope-tagged) — AGREE, verified; 3-way convergent (= SFH F6, AT F8).** Severity: side with CR's P3 over SFH's P2 (self-heals within one cycle either way). **Solution: CR's scope-tag over SFH's by-request-id dict** — functionally equivalent under single-flight, smaller diff, doesn't rewrite the seam `test_rejected_results_fetch_keeps_old_unit_list` seeds. Locking test is hermetic today (scope-flip sequence through `_on_finished`); TA-8's stale-`results:system` straggler lives at the same seam.

**CR-P3 (`if e.ts_usec` → `is not None`) — problem agree (inconsistency), solution DISAGree — REJECT this fix.** `is not None` would make `__REALTIME_TIMESTAMP=0` render as Dec 31 1969 in the Log tab — importing exactly TA-1's table bug. The captured data shows systemd uses 0 as falsy "never/missing"; epoch-0 is never a legitimate journal timestamp. Right resolution is the OPPOSITE: keep truthiness, add the why-comment (AT F11's aside), and align `format_when` to the same falsy-is-missing policy (TA-1's fix). One policy, one test pair: `format_when(0, NOW) == "—"` + a log-render `ts_usec=0` → `—` assertion. This Phase B disagreement is itself the evidence the two call sites would keep drifting without the policy pin.

**CR-P3 (format_delta assert under `-O`) — agree both halves.** One-line regression test (`pytest.raises(ValueError)` on `format_delta(-1)`).

**CR-P3 (stop-dialog text wrong for timer rows) — agree; design fork is real and is Dustin's call.** Whichever branch: the test needs the QMessageBox monkeypatch; if option (b) (Stop targets the running service), the locking test is a `client.calls` argv assertion reusing TA-3's scaffolding — write TA-3's run-now test first.

**CR-P3 (enable/disable without `--now`) — agree, register-don't-change for v1.** The exact-argv tests pin the current shape; adopting `--now` later is a deliberate two-file change — which is what exact-argv pinning is for.

**Insight:** CR's Power-of-Ten pass and my hermeticity pass were the two explicit full-walks and didn't overlap — good independence signal. CR's rule-7 unmarked-ignores nit sits on the same lines TA-2 wants tests for; same commit.

## Reply to silent-failure-hunter

**F1 (status washout) — AGREE, verified; identical fix proposed by AT F7 → implement the permanent-widget split.** Qt claim checks out (permanent widgets are never displaced by showMessage). Locking test: `_on_failed` → full successful refresh cycle → error still in `currentMessage()` AND freshness in the new label. Collateral: two existing tests assert freshness via `currentMessage()` — they move to the label; plan for it.

**F2 (parse failure freezes tabs, retry blocked) — AGREE, traced end-to-end. Solution AGREE — and it's testable TODAY** (seed `_expected_tab_ids` + `_last_detail_unit`, deliver "not json", assert `(parse failed)` in tab AND `_last_detail_unit is None`). The dedup-reset half is the part most likely to be forgotten — make it an explicit assertion.

**F3 (catch-gap) — AGREE, both escape routes verified. Solution: agree with layers 1-2, one gap:** the `isinstance(raw, list)` shape check does NOT cover AT F9(a) — `"activates": null` is inside a valid list; field-level validation is needed too. Implement the union: shape check + field check + render-time `except (OverflowError, OSError, ValueError)` → "—". Boundary-level locking test (null-activates payload → status bar ERROR) survives either implementation choice. Probe flag: whether PySide6 routes slot exceptions through `sys.excepthook` needs a 30-second probe before relying on the layer-3 backstop; its test must EMIT the signal, not call `_on_finished` directly.

**F4 (stderr discarded on success) — AGREE, verified; the enable-no-Install probe gates severity.** Contract cost: `finished` is `Signal(str, str)` — carrying warnings needs a third param; `test_success_emits_finished_with_stdout` unpacks a 2-tuple. Budget the churn. **AT F5 needs this exact plumbing — fix F4 first, F5 becomes a two-line consumer.**

**F5 (cat failure strands Schedule tab) — AGREE both halves, verified; same seam as TA-4.** Fold the fix and TA-4's negative-twin tests into one pass. Hermetic today.

**F6 — see CR reply:** prefer CR's scope-tag.

**F7 (staleness family) — agree all three; (a) is the 3-way pick (see CR reply); (c) = AT F12, one-line fix, testable today** (sibling of `test_stale_tab_response_is_dropped`); (b) needs TA-2's enabler.

**F8 (transition window, wrong-scope rows interactive) — agree; `_data_scope` fix testable with the existing harness.** Also agree with re-grading NOTE-T7(b)'s failure-path half — interactive-wrong-rows is not polish.

**F9 (`_pending` on failure) — agree; accept-and-document is right.**

**Insight:** SFH's test-gaps list and TA-2/TA-4 are the same map drawn from opposite directions. Sequencing consequence: **TA-2's gate fix is the enabler for most of this report's regression tests — land it first in the fix pass.**

## Reply to adversarial-tester

**F1 (never-run "✔ success") — AGREE; the codebase corroborates the mechanism** (RESULT_PROPS fetches ExecMainExitTimestamp; flush_block discards it; the LastResult docstring documents the SIBLING trap for ExecMainStatus=0 — the same skepticism was never applied to Result). The fixture contains only ran-units, so no test could have caught it. **Solution AGREE with one behavior decision to pin deliberately:** ExecMainExitTimestamp is also empty for a CURRENTLY RUNNING main process, so the fix renders a running service's result cell "—" mid-run — defensible (Status column says ▶ running) but make it an explicit test assertion. **Convergence with TA-1: sibling bugs** — systemd's falsy defaults (`last:0`, default `Result=success`) rendered as affirmative claims. TA-1's end-to-end test extends to pin BOTH: Last run "—" AND Last result "—" on the same never-ran row. One test, two Phase A reports closed.

**F2 — same as CR-P2-2 with the better consequence statement; implement the toggle.**

**F3 — the 3-way convergent finding; CR's one-liner + the post-action second-fetch_log test.** Manual-⟳ and empty-selection clears are worthwhile seconds, each needing its own assertion.

**F4 (scroll yank) — plausible, but uniquely among the P2s it has no code-reading proof (view-layer behavior, unprobed). The test IS the probe:** populate ~50 rows, set scrollbar value, run a second cycle, assert survival. Write the failing test first; if "before" doesn't reproduce, the finding self-retires.

**F5 (journal hint on stderr) — agree problem; CANNOT be implemented standalone — gated on SFH F4's stderr-on-success plumbing.** Sequence F4 first. Natural home for DEF-T2-01's surfacing too — and that register row is STALE (target "Task 7" passed unresolved); re-target it at this fix.

**F6 (drop-in override schedule) — agree, verified. Agree with the `systemctl show -p TimersCalendar,NextElapseUSecRealtime` direction; trade-off: show yields only the NEXT elapse, analyze yields five.** Hybrid (effective expression from TimersCalendar → feed analyze) keeps both. **Test-strategy consequence: decide F6 BEFORE writing TA-2's schedule-tab tests** — otherwise the tests pin the wrong implementation.

**F7 — identical to SFH F1; permanent-widget split.**

**F8 — see CR reply (scope-tag).**

**F9 — see SFH reply; AT's null-activates case defeats the list-shape check alone, so field validation is non-optional.** The frozen-table-labeled-fresh framing is the correct severity argument even at LOW plausibility.

**F10 (5s timeout vs huge journals) — agree; per-kind timeout testable trivially** (interval assertion against the existing fake_hang harness; no real waiting). The undrained-stdout aggravator sharpens DEF-T4-01's sizing — add the note to that register row.

**F11 (ANSI escapes) — agree; implement the strip as a pure helper (`clean_message`) unit-tested with CSI/OSC samples,** not buried in the untested render loop. The epoch-0 aside contradicts CR's `is not None` proposal — falsy-is-missing is the right policy (see CR reply).

**F12 — agree, verified; same fix as SFH F7(c), testable today.** The pane-header label ("foo.timer — fetched 14:02:11") is the best UX idea in any report for making staleness self-evident — but v2 scope by smallest-sufficient.

**F13 (`--` consistency) — agree near-nil; if hardened, one parametrized argv test.**

**NOTE-T3 sharpening — AGREE, a genuinely good catch on my own blind spot.** Verified: test_client.py (qtbot) collects alphabetically before test_format.py, so QApplication's `setlocale(LC_ALL, "")` exists before the %a/%b assertions TODAY — deterministic failure on any non-English LC_TIME machine, not hypothetical. My hermeticity walk checked subprocess and display isolation but not LOCALE isolation — a third axis I missed. Just do the one-line `LC_TIME=C` in conftest.

**Insight:** AT's report is the only one that attacked the SEMANTIC layer (what systemd's values mean) vs the mechanical layer (how responses are routed) — F1/F6 are findings no routing coverage would catch, because tests would faithfully pin the wrong interpretation. TA-1/TA-2 describe where tests are missing; AT F1/F6 describe why some must be written against RE-PROBED contracts, not current assumptions.
