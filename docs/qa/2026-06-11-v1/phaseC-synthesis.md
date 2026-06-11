# QA Phase C — Head-Agent Synthesis (Task Deck v1)

Inputs: 4 independent Phase A reports, 4 Phase B cross-examinations (12 threaded
replies), 1 live user bug report (Dustin, 2026-06-11), and 5 controller probes run
during the review. Every disposition below cites its evidence.

## Probes that gated decisions (all run 2026-06-11)

| Probe | Result | Decides |
|---|---|---|
| `show` on a never-ran service | `ExecMainExitTimestamp=` — key PRESENT, value EMPTY (with Result=success, ExecMainStatus=0) | AT-F1 fix gates on empty-valued key; SFH's emission-shape question answered |
| `systemctl --user enable` on an [Install]-less unit | **exit 0**, explanation on **stderr** | SFH-F4's worst case CONFIRMED for actions — "ok" + no change + explanation discarded |
| sys.excepthook from a PySide6 slot | **HOOK_FIRED** | Catch-design fork → SFH's architecture (narrow catch + validation + excepthook), not CR's broad catch |
| `systemctl start` on a >5s oneshot (live user report + Result probe) | job succeeded at 11:08:01 while UI reported timeout | USER-P1 confirmed; `--no-block` fix path |
| `--no-block` flag acceptance | accepted | fix implementable |

## Disposition matrix

### IMPLEMENT NOW — v1 fix batch (multi-agent consensus or user-reported)

1. **Honest never-ran rendering** (TA-F1 + AT-F1; 4-way Phase B consensus; "the same row lying in three cells"). Render-side 0-or-None-means-missing via ONE explicit commented helper (Phase B reconciliation — CR's `is not None` proposal REJECTED and withdrawn by CR itself; parser normalization rejected to preserve faithful transcription); `parse_show_results` emits no LastResult when `ExecMainExitTimestamp` is empty/absent (probe-gated), with SFH's three design notes honored (comment the unknown-not-truth limitation; mid-run "—" is a commented, tested decision; empty-key/absent-key equivalence commented). Never-ran block added to the show fixture; end-to-end fixture→parser→model test pins both columns.
2. **User-P1 timeout on Run-now** (Dustin's report + SFH Phase B's independent derivation). `--no-block` added to start/stop argv; action status rephrased as "command accepted" semantics; the result/status columns are the success channel (made trustworthy by #1).
3. **Action-stderr passthrough, narrow** (probe-confirmed exit-0+stderr on enable). For `action:` request ids only, the client's success path reads stderr and delivers it as the finished payload; the window surfaces it persistently. AT's false-alarm break ("Created symlink…" noise) is accepted as GOOD feedback when phrased without "warning". The FULL stderr-on-success plumbing (journal hints/corruption) stays deferred — see DEF-V11-01.
4. **Tab-lifecycle invalidation family** (CR-P2 + SFH-F2/F7 + AT-F3; Phase B-refined design): clear dedup on action success and manual ⟳ (new wrapper); **Race B guard** — the three selection-fetch booleans are checked, any rejection leaves the dedup unset so the next cycle retries (simultaneously discharges CR's rule-7 P3 — AT proved mirror-the-comment was no longer the right fix); vanished-unit handling moved to `_apply_results`/`_reselect`-returns-bool (the clear-on-None proposals were REFUTED by Qt's signal-free selection reset — CR + AT, independently); `set_scope` clears `_expected_tab_ids` and stamps the tabs; parse failures write `(parse failed)` into their tab via a shared kind→tab map WITHOUT resetting the dedup (AT's strobe-loop break accepted — retry rides the ⟳ path).
5. **Status-channel split with kind-aware clearing** (SFH-F1 + AT-F7, unanimous fix; severity settled at P1-for-action-errors per CR's arbitration): freshness line moves to a permanent right-side QLabel; `showMessage` reserved for errors/action feedback; error text timestamped (AT's stale-error inversion break); fetch-kind errors cleared by same-kind success, action messages persist until the next action (SFH's clearing rule).
6. **Results alignment by request id** (CR-P3/SFH-F6/AT-F8 → Phase B consensus on SFH's `_result_units_by_id`, CR and AT both switched to it): bounded dict, popped on consume and in `_on_failed`; the rejected-fetch guarantee preserved through the mechanics change.
7. **`_data_scope` enablement gate** (SFH-F8, CR-endorsed) + explicit `_update_action_enablement()` at the end of `_apply_results` (AT's no-signal wiring fix).
8. **Catch hardening, SFH architecture** (probe-decided): field-type validation in both list parsers (raises ValueError — covers AT-F9(a)'s null-activates, which defeats the shape check alone); per-entry `fromtimestamp` guard rendering "—" (AT's clamping proposal REJECTED as silent normalization); `format_delta` assert → ValueError (CR + SFH: moves the guard inside the surfaced exception set); `sys.excepthook` backstop in app.py posting UNEXPECTED ERROR (probe: HOOK_FIRED).
9. **Scroll restore** (AT-F4; failing-test-first per TA — the test is the probe).
10. **Show-inactive toggle** (CR-P2 + AT-F2; consensus implement over register — the Stop-is-a-one-way-door trap): checkable toolbar action; last results dict cached for local re-render (AT's dead-checkbox break).
11. **Per-kind timeout** (AT-F10): journal fetches get 15s (request() override param); actions stay 5s (now non-blocking via #2).
12. **ANSI strip at render** (AT-F11; render-side per SFH's faithful-transcription argument) as a unit-tested pure helper.
13. **Stop dialog per-view text — option (a)** (CR's design fork, decided by AT's race analysis of option (b): no-op-narrated-as-ok and kill-job-keep-schedule both fail silently; (a) is static and raceless). Not escalated to Dustin: the race evidence makes this not a genuine fork.
14. **Hermeticity hardening** (TA-F6 + SFH's extension): smoke test injects all three fakebin paths; `action_argv` gains an injectable systemctl path wired from the client.
15. **Small items batch**: `exec sleep 30` (TA-F7, CR strengthened); `LC_TIME=C` in conftest (NOTE-T3, AT escalation verified by CR+TA — deterministic today); `__main__` ImportError catch with dnf hint, exit non-zero (SFH-F10 + AT's rule-7 nit); redundant start-verb test deleted; color assertions made relational (TA-F8, contradiction with models.py's cosmetic-freedom comment); `_pending`-on-failure comment (consensus accept+document); the `is None` fixture-tolerance arm KEPT (CR's pushback accepted over TA's nit); fixture re-capture prerequisites note in the plan (TA-F8 + AT's addition).
16. **Test batch** (~20 tests; TA Findings 2-5 enablement): the gate question resolved as a TEST IDIOM — `window._auto_refresh = True` set post-construction (timer never started, constructor refresh never fired) — satisfying CR's no-production-churn objection AND AT's flakiness objection simultaneously; Race B regression; error-persistence; KeyError dispatch; action wiring (run-now targets activates; rejection surfacing; refusal; failed-action-no-refresh pinned per SFH); `_on_failed` tab writes + negative twin; vanished-unit selection variant; stale-`results:system` drop.

### DEFER — registered with fix directions

- **DEF-V11-01 (NEW, MEDIUM)**: full stderr-on-success plumbing — journal corruption/permission hints (SFH-F4 journalctl half + AT-F5 + DEF-T2-01, three-way convergent re-target). Staged design from Phase B recorded: 3-arg finished signal or parallel channel; journalctl first; verbatim-secondary-text framing; fakebin exit-0+stderr stub test mandatory (unreachable on this wheel account). DEF-T2-01 is RESOLVED-INTO this entry (its skip-count surfacing belongs to the same channel).
- **DEF-V11-02 (NEW, MEDIUM)**: Schedule-tab source redesign (AT-F6, all-hands agreement on the show-based direction; CR's minimal-diff hold). FOLDED INTO TASK 10 (cadence column): one batched `show -p TimersCalendar,TimersMonotonic,NextElapseUSecRealtime` feeds BOTH the cadence classifier and the effective-schedule tab, deleting the cat→extract→analyze chain (negative complexity per TA). Fail-loud parsing contract per SFH. Calendar-chain pin tests deliberately NOT written now (TA's sequencing warning).
- **DEF-V11-03 (NEW, LOW)**: pane-header label ("unit — fetched HH:MM:SS") — SFH: "the best single UI hardening in any of the four documents"; v1.1 by smallest-sufficient.
- **Enable/disable `--now`**: registered as known-v1 behavior (consensus); revisit after v1.1 stderr work, knowing `disable --now` relocates rather than removes the surprise (AT).
- **DEF-T4-01 updated**: + AT's megabytes-per-kill stdout-drain note, + CR's per-selection-fetch rate note.
- **F13 (`--` on read paths)**: not done — NEAR-NIL by-accident; CR's flags-first argv caveat recorded for any future hardening.
- **`_pending` discard variant, F9-render-with-partial**: rejected (CR+AT: mixed-generation table worse than one-cycle delay).

### CLOSED / RESOLVED BY THIS SYNTHESIS

- NOTE-T3 → closed by LC_TIME=C. NOTE-T7(a) → closed by the action-wiring tests. NOTE-T7(b) failure-path half → resolved by `_data_scope` (CR's arbitration: view-switch half stays LOW, scope-switch risk was the real one and is fixed). DEF-T2-01 → resolved into DEF-V11-01. DEF-A-02 was already resolved (c323ce8).

### SINGLE-AGENT FINDINGS — analyzed, dispositioned

- CR's "refresh only while focused" spec micro-deviation: accepted behavior, spec amended (refresh-while-open is better UX; one sentence).
- AT-F13, SFH-F9, SFH-F10 bootstrap items: as above (small-items batch / register).
- TA-F7 fake_hang: implemented (CR strengthened it from "may" to "likely occurs").

### PROCESS FINDINGS

- Phase B *changed outcomes* in five places: one Phase A fix withdrawn by its own author (CR's `is not None`), two fixes refuted with Qt-documentation evidence (clear-on-None), one new race found in the consensus fix (Race B), one fix shown to create a strobe loop (F2 auto-retry). The cross-examination phase is earning its cost on this codebase.
- The manual-launch step caught a P1 (USER-P1) that three review layers and 58 tests had not. It stays in every future plan.
