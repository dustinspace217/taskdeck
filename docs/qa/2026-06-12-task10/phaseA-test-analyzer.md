# Phase A — Test-Coverage Analysis: Task 10 (cadence column + show-based Schedule tab)

*Agent: test-analyzer (Fable). Reviewed commit 39c145e independently — no other review seen.*

No failing tests — this is a gap-and-quality analysis of a green suite. I verified each claim below by walking the mutation against every test that touches the affected path.

---

## 1. Are the new behaviors pinned?

### parse_show_schedules failure modes — mostly pinned, alignment is not

**Pinned and solid:** empty property (`tests/test_parsing.py:217` — removing the `if not value: continue` guard at `systemd_client.py:262` makes the regex raise on `""`, so the test fails), unrecognized line shape (`test_parsing.py:210`), repeated `TimersMonotonic` keys, tz suffix passthrough, and empty `NextElapseUSecRealtime` (all via the exact-tuple assertions in `test_parsing.py:186-208`).

**P2 — schedules-side block alignment is pinned only by code-sharing, not by tests.** I checked this concretely: rewrite `parse_show_schedules` (`taskdeck/systemd_client.py:255`) to use a naive `text.strip().split("\n\n")` instead of `_walk_show_blocks`, and every schedules test passes — the fixture has three non-empty blocks for three units, and the two synthetic tests are single-block. Empty blocks are reachable here (a timer that vanishes between `list-timers` and the `show` call loads as not-found and emits a property-less block), and misalignment means the *wrong timer gets another timer's cadence* — the exact misattribution class `_walk_show_blocks`'s docstring exists to prevent. The overflow-raises path is likewise tested only through `parse_show_results` (`test_parsing.py:152`). Mitigation: the helper is shared and well-tested through the results parser, so only an independent rewrite regresses this. Proposed fix: two small tests mirroring `test_parse_show_results_leading_empty_block_keeps_alignment` and the overflow test, driven through `parse_show_schedules`.

### classify_cadence buckets — table is good, two real holes

The `CADENCE_CASES` table (`tests/test_models.py:186-212`) covers daily/monthly/yearly/hourly/N×day/weekdays/weekly/raw-fallback/mixed/dedup/empty/None. I traced each branch of `_classify_calendar`; the reachable-but-untested ones:

**P1 — repetition syntax (`/`) is entirely absent from the table, and the classifier misbuckets it — contradicting its own contract.** `systemctl show` emits e.g. `*-*-* *:00/15:00` for an every-15-minutes timer; an every-6-hours timer (`*-*-* 00/6:00:00`) gets a confident wrong bucket. The docstring at `models.py:67-68` states the contract: "an honest raw string beats a wrong bucket" — these are confident wrong buckets, and no test exercises any `/` expression, so the contradiction is invisible. Proposed fix: add `/`-repetition cases to `CADENCE_CASES` with whatever label is decided (raw fallback, or "every 15min"), and adjust `_classify_calendar` to detect `/` before bucketing. This is the one finding where the test gap hides a live production-contract violation rather than a hypothetical regression. *(Phase B note: my "hourly" bucket claim for the minute-step case was wrong — see Phase B reply; the tz-strip heuristic intercepts first and produces "daily".)*

**P2 — two `_MONOTONIC_LABELS` entries are dead weight to the suite.** Deleting `OnUnitInactiveUSec` and `OnActiveUSec` from `models.py:57-58` survives all tests (the table covers only Boot/Startup/UnitActive). Regression cost is low (raw spec shown — still honest), but they're two table rows to add. Likewise the `"Mon,Tue,Wed,Thu,Fri"` alternative at `models.py:86` is untested — and possibly unreachable, since systemd normalizes weekday lists to `Mon..Fri` range form; worth a probe and either a test or removal.

P2 (acceptable): the defensive branches at `models.py:76` (`if not parts`) and the date/time defaults at `models.py:82-83` are untested but unreachable for normalized `show` output.

### _pending_enrich barrier — pinned on one side only

**P1 — the results-side of the render barrier is unpinned; deleting it survives the entire suite.** Concretely: delete `self._pending_enrich.add("results")` at `taskdeck/main_window.py:386`. `test_render_waits_for_both_enrichments` (`test_window_logic.py:252`) delivers **results first**, so it only proves "pending schedules blocks render." Every other test uses `run_full_cycle`, which also delivers results before schedules. With the mutation, a schedules-first delivery renders the table with a blank Last-result column that pops in a beat later — precisely the flicker the barrier comment says it prevents. Proposed fix: a mirror test delivering `schedules:user` first, asserting `rowCount() == 0`, then results, asserting render.

**P1 — the schedules wrong-scope guard is unpinned; its results twin is tested.** Delete `main_window.py:341-342` (`if request_id != f"schedules:{self.scope}": return`) and no test fails: `test_other_scope_results_are_consumed_but_not_rendered` covers only the results branch, and `test_schedules_response_without_recorded_units_is_dropped` returns earlier at the units-None check. Consequence of regression: flip scope mid-flight and the other scope's schedule data lands in `_last_schedules`, then renders cadence for same-named timers under the wrong scope label — QA synthesis #6's bug class, half-guarded. Proposed fix: mirror the results test (record `schedules:system` entry, deliver, assert popped and `_last_schedules == {}`).

**One-fetch-rejected:** the results side is pinned (`test_rejected_results_fetch_keeps_recorded_unit_list`); the schedules twin of the accept-guarded recording at `main_window.py:396-397` is not. P2 — same one-test mirror.

**Failure mid-cycle:** P2 — no test fails a `results:` or `schedules:` request mid-cycle. The documented behavior (error posted, no render this cycle, next cycle's wholesale `_pending_enrich = set()` rebuild self-heals) is structurally robust, but the visible contract — last-good table + error, recovery next cycle — is asserted nowhere. *(Phase B note: upgraded to P1 agreement after the code-reviewer traced the deterministic parse-raise variant — see their P1-2.)*

**P2 — the empty-timer-list guard is only accidentally pinned.** `test_empty_scope_renders_zero_rows_without_results_fetch` asserts no `fetch_results` call but never asserts no `fetch_schedules` call. A mutation that fires `fetch_schedules(scope, [])` while keeping the render path survives, and in production that's the manager-properties dump → `_walk_show_blocks` overflow ValueError → a spurious error toast every 10s in any timer-less scope. One added line — `assert not calls_of(client, "fetch_schedules")` — pins it.

### schedtab flow and freshness — gate is well covered, one path has zero tests

The stale-after-reselect case is **clean**: the gate at `main_window.py:368` is one shared membership check for all five tab kinds, pinned by `test_stale_tab_response_is_dropped` and `test_set_scope_stamps_tabs_and_clears_expected_ids`; schedtab's membership in the dispatch tuple is pinned because removing it strands the fill and `test_schedtab_fill_renders_triggers_cadence_and_chains_calendar` fails.

**P1 — the non-timer selection path is completely untested.** Delete the else branch at `main_window.py:529-530` (`self.tab_schedule.setPlainText("(not a timer — no schedule)")`) and the suite stays green: no test selects a non-`.timer` row with fetches enabled. The regression leaves the Schedule tab stranded at "loading…" for every service — the exact stranded-tab failure class the suite names elsewhere (SFH-F5). Proposed fix: a services-view selection test asserting the stamp, no `fetch_tab_schedule` call, and no `schedtab:` id in `_expected_tab_ids`.

### Calendar chain — pinned

`test_schedtab_fill_renders_triggers_cadence_and_chains_calendar` pins the placeholder, the `fetch_calendar` call, the freshness-set admission, and the append-and-replace; the no-triggers and monotonic-only tests pin the no-chain branches. **Clean**, with one P2: a multi-calendar timer (only `info.calendar[0]` is chained; two `OnCalendar=` lines rendered) has no test, and no fixture block carries two calendar triggers.

---

## 2. Vacuous-test check — named survivors

Two specific mutations survive tests that nominally cover them; both detailed above:

- `test_render_waits_for_both_enrichments` survives deletion of `_pending_enrich.add("results")` — it pins only the schedules-side of a two-sided barrier.
- All three schedules-parser tests survive replacing `_walk_show_blocks` with a naive `split("\n\n")` in `parse_show_schedules` — none of their inputs contains an empty block or an overflow.

Everything else I mutation-walked holds: the empty-property guard, the fail-loud regex raise, the cadence column index, the dedup, the calendar-chain wiring, and the cat-failure/Schedule-tab decoupling (`test_on_failed_writes_expected_tab_only` genuinely fails if the old stamping returns).

**P1 — the client↔window request-id seam has zero coverage, and this commit added three new ids to it.** The window's expected-id strings (`main_window.py` — `schedtab:{scope}:{unit}`, `calendar:{expr}`, `schedules:{scope}`) must match what the real client emits (`systemd_client.py:511/519/553`). Window tests hand-type the ids into a FakeClient; `tests/test_client.py` exercises only the generic `request()` machinery and never calls `fetch_schedules`/`fetch_tab_schedule`/`fetch_calendar`; `test_smoke.py` builds the real client but never triggers a fetch. A client-side format drift keeps all 102 tests green while production's Schedule tab freezes at "loading…" forever — the silent-failure mode this app exists to avoid. The seam predates Task 10 for log/details/cat, but Task 10 tripled the new surface. Proposed fix: one fakebin-driven test per new method asserting the emitted `finished` request_id (the `fetch_calendar` test also pins the `--` flag-stop argv, currently untested).

---

## 3. Fixture honesty — clean

`tests/fixtures/show_schedules.txt` genuinely exercises every claimed shape: multi-trigger monotonic via a repeated `TimersMonotonic` key (lines 4-5, asserted as an ordered two-tuple), IANA timezone (line 8), abbreviated `UTC` suffix (line 1), and empty next-elapse (line 6) — each backed by an exact assertion in `test_parse_show_schedules_fixture_round_trip`, not a weak `in` check. The 27-second skew between line 1's trigger `next_elapse` and `NextElapseUSecRealtime` is consistent with a faithful live capture (AccuracySec). Gaps worth noting, not honesty problems: no empty block and no multi-calendar timer in the capture.

**P1 — no realsystemd contract test for the new parser.** `tests/test_real_systemd.py` declares itself "the early-warning system for systemd version drift," yet `parse_show_schedules` — the only parser regex-matching a *human-formatted* line rather than JSON, and explicitly fail-loud on shape drift — has no live test. A systemd format change would surface as a ValueError toast every 10 seconds for users; a five-line live test (fetch `SCHEDULE_PROPS` for the machine's timers, assert parse succeeds and each timer has an entry) catches it on Fedora update day instead.

---

## 4. Tests contradicting the production contract

One genuine contradiction, surfaced under the cadence finding above: the code's "honest raw string beats a wrong bucket" contract is violated by `/`-repetition expressions, and the test table's silence ratifies it.

One deliberate-but-worth-confirming encoding: `test_classify_cadence_dedupes_identical_buckets` pins `("*-*-* 03:00:00", "*-*-* 15:00:00")` → `"daily"` — a twice-daily timer labeled "daily". This *matches* the documented dedup semantics (dedup is for identical buckets), so it is not a contradiction, but the test cements frequency-understating UX as the contract. P2: confirm this is intended, since the same suite labels a four-trigger calendar "4×/day".

---

## Summary

| Sev | Finding | Where |
|---|---|---|
| P1 | Render barrier pinned one-sided; deleting the results-side add survives the suite | main_window.py:386 |
| P1 | Schedules wrong-scope guard unpinned (results twin is tested) | main_window.py:341-342 |
| P1 | `/`-repetition cadences untested and misbucketed — contradicts stated contract | models.py:89-102 |
| P1 | Client↔window request-id seam for schedules/schedtab/calendar has zero coverage | systemd_client.py:511,519,553 |
| P1 | Non-timer selection branch untested; regression strands Schedule tab | main_window.py:529-530 |
| P1 | No realsystemd test for the most drift-fragile parser | tests/test_real_systemd.py |
| P2 | Schedules-side block alignment pinned only via shared helper | systemd_client.py:255 |
| P2 | Empty-timer-list `fetch_schedules` guard only accidentally pinned | test_window_logic.py |
| P2 | Schedules twin of rejected-fetch recording; failure-mid-cycle; `_on_failed` pops | main_window.py:396-397, 478-479 |
| P2 | Two monotonic labels + comma-weekday form untested; multi-calendar tab untested; dedup-understates-frequency confirmation | models.py:57-58,86 |

**Verdict: GAPS-FOUND** — the happy paths and fail-loud parser contracts are well pinned with honest fixtures, but two halves of new two-sided guards (render barrier, scope guard), the `/`-repetition cadence class, the non-timer tab path, and the cross-module id seam would all regress silently today.
