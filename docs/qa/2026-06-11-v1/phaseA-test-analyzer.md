# Test-Analyzer Review — Task Deck v1 (QA Phase A)

## Overall assessment

This is a well-architected suite for its size. The strongest properties: parsers are tested against **real captured systemd output** (not hand-invented JSON), with the contract quirks probed and documented at capture time (empty `show` blocks, journal byte-array messages, empty journal stdout); the `parse_show_results` alignment tests use **distinguishable per-unit values** and explicitly note why the real fixture alone can't catch swaps — that's rare discipline; the client tests exercise genuinely hard async paths (timeout kill, spawn failure, the echo-guard regression test `test_timeout_emits_exactly_one_terminal_signal` is exactly the test that keeps the identity-guard design honest); `now` is injected everywhere so time rendering is deterministic forever; `format_delta` boundaries are pinned exactly; and the opt-in `realsystemd` pair is a sensible drift canary for Fedora updates. **Hermeticity: verified PASS.** I walked every default-run test: parsers/format/actions are pure, client tests inject `tests/fakebin/` stubs, window tests inject `FakeClient` with `auto_refresh=False`, the smoke test's real `SystemdClient` is never asked to run anything, `conftest.py` hard-assigns `QT_QPA_PLATFORM=offscreen`, and `pyproject.toml` excludes `realsystemd` by default with `--strict-markers`. No dev-scaffold-only tests found — everything exercises shipped code.

The headline problems are on the other side of the fixture boundary: the captured fixture **contains evidence of a real rendering bug** (`"last":0`) that no test reads, and the window's two most user-facing flows — the detail-tab pipeline and the action buttons — have zero hermetic coverage, partly because the `auto_refresh=False` test gate structurally excludes them.

---

## Finding 1 — `last: 0` from real systemd renders as a 1969 timestamp; the fixture proves it, no test covers it

**Severity: P1**

**Problem.** The captured fixture `tests/fixtures/list_timers.json` shows what systemd 258 actually emits for a never-ran timer:

```json
{"next":null,"left":null,"last":0,"passed":0,"unit":"drkonqi-coredump-cleanup.timer",...}
```

`last` is **0, not null** (two such timers in the fixture; `next` does use null). `parse_list_timers` passes `0` through faithfully (`item.get("last")` → `0`), and `format_when` in `taskdeck/models.py` only special-cases `None`:

```python
if ts_usec is None:
    return "—"
dt = datetime.fromtimestamp(ts_usec / 1_000_000)   # 0 → Dec 31 1969 16:00 PST
```

So every never-ran timer renders a "Last run" of `Dec 31 16:00 (20614d ago)` instead of `—`. The sort key also becomes `0` instead of `_SORT_NO_LAST` (cosmetically harmless — both sort oldest — but inconsistent with the documented sentinel design).

**Why the suite misses it.** Three tests circle this exact spot and none lands on it:
- `test_parse_list_timers_returns_rows_with_microsecond_epochs` asserts only on the cherry-picked happy row (`astrowidget-fetch.timer`).
- `test_parse_list_timers_tolerates_null_next_and_last` hand-writes `"last":null` — a shape the captured fixture shows systemd does **not** emit for never-ran timers. The test covers the invented shape and misses the real one.
- The model tests in `tests/test_models.py` hand-build `TimerRow("b.timer", "b.service", None, None)` — the hand-built rows diverge from what the parser actually produces from real input. The "same fixtures drive both layers" claim in models.py's docstring is aspirational: the model tests never consume parsed fixture output.

**Proposed solution.** (1) Decide the normalization point — given the client's "faithful transcription" principle, the cheapest correct fix is treating falsy as missing at render (`if not ts_usec:` in `format_when`) plus the matching sentinel in `set_timer_rows`'s sort key; normalizing `0 → None` in the parser is the alternative if "0 means never" is considered part of the data contract. (2) Add a parser test asserting the drkonqi rows from the real fixture, and a model/format test for `last_usec=0`. (3) Consider one end-to-end test: `parse_list_timers(fixture)` → `set_timer_rows` → assert the never-ran row renders `—` — that single test makes the fixture drive both layers for real and would have caught this.

**Context.** Not in the Deferments register — this is a new finding. It's user-visible wrong data in the main table, on the dev machine, today.

---

## Finding 2 — Detail-tab pipeline has zero hermetic coverage, and the test gate makes it structurally untestable as written

**Severity: P2**

**Problem.** In `taskdeck/main_window.py`, the entire selection → tabs flow is uncovered:
- `_on_selection`'s fetch branch (freshness-set construction, `loading…` placeholders, `_last_detail_unit` dedup) — 0%, because `auto_refresh=False` gates it and every hermetic window test uses that flag.
- `_fill_tab`, all four branches — 0%. That includes the **calendar chain**, the most intricate orchestration in the window: `cat` response → `OnCalendar=` extraction → add `calendar:{expr}` to `_expected_tab_ids` *before* requesting → `(calculating…)` replacement on response. Also untested: the log render (priority ≤ 3 `✘` marker, scroll-to-bottom, `(no journal entries)`), `(unit file not found)`, and the no-OnCalendar message.

The only tab test (`test_stale_tab_response_is_dropped`) covers the drop path, not any write path.

**Proposed solution.** The architecture already supports this cheaply — `FakeClient` records calls instead of spawning, so it is safe to construct `MainWindow(FakeClient(), auto_refresh=True)` (the 10s QTimer never fires within a test). Alternatively, split the gate into `auto_refresh` (timer) and the selection-fetch suppression, so tests can enable one without the other. Then: populate via `_on_finished`, `selectRow`, assert the three fetch calls and the `_expected_tab_ids` contents; drive `_on_finished("cat:user:…", unit_file_with_OnCalendar)` and assert `fetch_calendar` was called and the schedule tab's text lifecycle. A second test feeding the journal fixture through `_on_finished("log:…")` covers the log render. Roughly four tests close the whole gap.

**Context.** The freshness/staleness design carries multiple load-bearing comments ("response can never append under another unit's schedule") — those invariants are currently enforced by prose, not tests. A refactor that reorders the `_expected_tab_ids.add` and `fetch_calendar` lines would regress silently.

---

## Finding 3 — Action dispatch (`_do_action`) has zero coverage; `FakeClient.run_action` exists but no test ever calls it

**Severity: P2**

**Problem.** No test exercises `_do_action` for any verb. Untested, in descending importance:
1. **Run-now targets `ROLE_ACTIVATES`, not the timer unit** — the semantic heart of the "Run now" button ("starting a .timer merely re-arms its schedule"). A regression here makes the headline button silently do the wrong thing while every existing test passes.
2. Single-flight rejection surfacing (`accept = False` → "previous action still running") — `FakeClient.accept` was built for exactly this and is used only by the results-fetch test.
3. The `ActionNotAllowed` belt-and-suspenders path (status bar "refused: …").
4. The `action:` finished branch in `_dispatch_finished` (status message + triggered refresh).

The smoke test covers button *enablement* (good — the read-only contract is asserted), but never a button *firing*.

**Proposed solution.** With the Finding-2 testability fix, these are four short tests: select a timer row, call `window._do_action("start", run_now=True)`, assert `("run_action", ["systemctl","--user","start","--","a.service"], "a.service")` in `client.calls` — that one assertion pins the activates-targeting AND re-verifies the argv through the real `action_argv`. NOTE-T7(a)'s modal-blocking hazard applies **only to `stop`** — start/enable/disable have no dialog and are testable today; stop needs one `monkeypatch.setattr(QMessageBox, "question", ...)`.

**Context.** `test_actions.py` thoroughly covers the pure `action_argv` policy (exact argv per verb, both refusal paths, message contracts — well done), but policy ≠ wiring; the wiring is where `run_now` lives.

---

## Finding 4 — `_on_failed`'s tab-write path untested

**Severity: P2**

**Problem.** `test_failed_signal_surfaces_verbatim` asserts the status bar only. The second half of `_on_failed` — writing `(fetch failed)\n{message}` into the correct tab when `request_id in self._expected_tab_ids`, and *not* writing when it isn't — is uncovered. This path exists precisely to prevent a tab frozen at `loading…` hiding a failure; that is a silent-failure guard with no regression protection. The kind→tab mapping dict is also unchecked (a typo'd key would no-op silently — exactly the failure class the app's own no-silent-failure rule targets).

**Proposed solution.** Two tests: seed `_expected_tab_ids = {"log:user:a.service"}`, call `_on_failed("log:user:a.service", "boom")`, assert tab text contains `(fetch failed)` and `boom`; then the negative twin asserting an unexpected id leaves the tab untouched.

---

## Finding 5 — Selection capture/restore across refresh untested

**Severity: P3**

**Problem.** The `keep_unit`/`_reselect` logic in `_apply_results` exists to prevent a specific regression (the 10s refresh silently deselecting the user's row and blanking the action buttons). No test drives two full refresh cycles with a selection held — so the exact bug this code prevents would not be caught if reintroduced. `_reselect`'s proxy-walk (filter/sort respected, vanished unit stays deselected) is also uncovered, as is the `_last_detail_unit` dedup that prevents tab re-flashing on restore.

**Proposed solution.** One test: full `_on_finished` cycle, `selectRow`, second cycle with the same payloads, assert the selection survived (and with Finding 2's fix, assert no duplicate `fetch_log` calls — covering the dedup).

---

## Finding 6 — Smoke test's hermeticity is by convention, not structure

**Severity: P3**

**Problem.** `tests/test_smoke.py` constructs `SystemdClient()` pointing at the **real** `systemctl`, relying on `auto_refresh=False` plus "never asked to run anything." A future edit (flipping the flag, adding a `refresh()` call to widen the screenshot) would silently start spawning real systemctl in the hermetic suite — and the test would still pass, so nothing flags the breach.

**Proposed solution.** Inject the fakebin path: `SystemdClient(systemctl=str(FAKEBIN / "fake_ok"), ...)`. Zero behavior change today; the hermetic property becomes structural. (Keeping the real client class — rather than `FakeClient` — is right for a smoke test; only the binary path needs pinning.)

---

## Finding 7 — `fake_hang` may orphan a 30-second `sleep` child past the test run

**Severity: P3**

**Problem.** `tests/fakebin/fake_hang` is `#!/bin/bash` + `sleep 30`. `QProcess.kill()` SIGKILLs the process it spawned. Whether the lingering `sleep` survives depends on whether bash applies its exec-the-last-command optimization to script files — version-dependent behavior I have not probed, so I flag it as a risk to verify rather than a confirmed leak. Two tests use `fake_hang`; worst case is two stray `sleep` processes for ~30s after the suite exits — harmless but unhygienic, and it would confuse anyone profiling test cleanup.

**Proposed solution.** Make it deterministic: `exec sleep 30`. One word; removes the bash layer from the kill path entirely.

---

## Finding 8 — Test-quality nits (brittleness / redundancy)

**Severity: P3**

- **Exact-hex color assertions vs. declared intent.** `test_result_column_foreground_colors` pins `#4caf50`/`#e57373` while `models.py` says "exact shades are cosmetic, the INFORMATION is also in the ✔/✘ glyphs." A cosmetic re-tint breaks the test. Assert the load-bearing facts instead: result column tinted iff result known, success-brush ≠ failure-brush, other columns untinted.
- **Redundant test.** `test_action_argv_builds_user_command` is exactly the `verb="start"` case of `test_all_four_verbs_build_exact_argv`. Delete one.
- **Dead assertion arm.** `assert fetch.next_usec is None or fetch.next_usec > 1e15` — the fixture is static and the row's `next` is non-null, so the `is None` arm can never matter; it reads like tolerance the test doesn't need. Assert `> 1e15` directly.
- **Fixture name-pinning.** Tests pin machine-specific unit names (`astrowidget-fetch.timer`, `boothang-update-check.service`) and require a `\x2d`-escaped name to exist. Re-capture commands are documented in the plan and the assertion messages are good, but the *content prerequisites* for a valid re-capture (≥1 timer, the two named units, ≥1 escaped name, ≥1 never-ran timer once Finding 1's test exists) are implicit. A one-paragraph note next to the capture commands prevents a future re-capture from producing a fixture set that fails for non-bug reasons.
- **Minor dispatch-coverage stragglers** (fold into any Finding 2/3 test pass): stale `results:system` drop while user scope is active; the `KeyError` branch of the `_on_finished` handler (e.g. `'[{"activates":"x"}]'` → status bar shows the missing-key error) — the handler's comment claims KeyError coverage, but only ValueError is tested.

---

## Deferments register — severity check

Mostly sound. Three notes:

- **DEF-T2-01 (parse_journal skip count) — register entry is stale.** Its defer target was "Task 7 log-tab wiring or v2"; Task 7 has shipped and `_fill_tab` does not surface the skip count. The target passed without resolution or re-targeting. Severity LOW is still defensible, but per the workspace deferment process the row needs a new target (v2) or an explicit re-defer.
- **NOTE-T7(a) understates its consequence.** Recorded as informational ("monkeypatch QMessageBox before testing act_stop"), but the practical effect is that the app's headline feature — the action buttons — shipped with zero wiring coverage (Finding 3). The modal hazard genuinely blocks only `stop`; the other three verbs were testable all along. I'd upgrade the note to an explicit coverage deferment or just close it with the Finding 3 tests.
- **DEF-T4-01 (MEDIUM, QProcess leak) and DEF-A-01 (LOW, empty argv)** — severity calls look right to me given the on-demand-window exposure bound and the conveniences-only call pattern respectively. NOTE-T3 (locale) is correctly informational.

---

## Summary table

| # | Finding | Severity |
|---|---------|----------|
| 1 | `last:0` renders 1969 epoch; fixture proves it, untested | P1 |
| 2 | Detail-tab pipeline (selection → `_fill_tab` → calendar chain) 0% hermetic coverage; test gate blocks it | P2 |
| 3 | `_do_action` wiring (run-now targeting, rejection, refusal) untested | P2 |
| 4 | `_on_failed` tab-write path untested | P2 |
| 5 | Selection restore across refresh untested | P3 |
| 6 | Smoke test hermetic by convention, not structure | P3 |
| 7 | `fake_hang` orphaned-sleep risk — verify, add `exec` | P3 |
| 8 | Brittleness/redundancy nits | P3 |
| — | DEF-T2-01 stale target; NOTE-T7(a) understated | register hygiene |
