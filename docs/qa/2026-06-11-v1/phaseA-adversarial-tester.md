# Task Deck v1 — Adversarial QA Review (Phase A, independent)

**Reviewer lens:** real-world chaos on a single-user localhost desktop; findings filtered through "could this happen by accident?"

## Overall assessment

The data layer is the strongest part of this codebase: the `parse_show_results` empty-block alignment work, the identity-guarded QProcess lifecycle, and the full-id freshness gating for tab writes all show that the genuinely nasty races were found and handled deliberately, with regression tests. I found **no P0s and no crashes reachable through normal use**. The findings cluster in two places: (1) *semantic* trust in systemd's output — `Result=success` is a default, not evidence, so the UI affirmatively claims success for units that never ran; and (2) *lifecycle invalidation* — several caches are set correctly but never expired correctly, so the app degrades under exactly the usage pattern it's built for: leaving it open and watching things change. One spec deviation (the missing "show inactive" toggle) creates a genuine functional trap. Everything below is fixable in small, local diffs.

## F1 — Never-run units render "✔ success" in green (P2; plausibility: VERY HIGH — every timer, after every login)

**Trigger:** log in (fresh user manager) or install a new timer; open Task Deck before first firing. Worse: a job FAILED last night; re-login resets the service's `Result` to its default `success`.
**What breaks:** RESULT_PROPS fetches `ExecMainExitTimestamp` but `parse_show_results` discards it. systemd's `Result` defaults to `success` for loaded-never-run services. The row reads "never ran, succeeded" — and in the failed-then-relogin variant, last night's failure evidence is REPLACED by an affirmative success claim. For a "did my scheduled jobs work?" tool, the most damaging wrong answer it can give.
**Mitigation:** use the already-fetched property: when `ExecMainExitTimestamp` is empty/absent, emit no LastResult (renders "—"). Small parser change + one fixture.

## F2 — A service stopped from the GUI vanishes and can never be started again (P2; plausibility: HIGH)

Stop a running service → refresh → `active=inactive` → hidden unconditionally by the view filter. The spec's "show inactive" toggle was never implemented and is NOT in the conscious-cuts list. The GUI offers Stop but the inverse is unreachable — drop to a terminal, the tool's failure condition.
**Mitigation:** implement the specced toggle (checkable QAction); or at minimum register the cut with the trap named.

## F3 — Detail tabs never refresh while a unit stays selected; "Run now" produces an invisible run (P2; plausibility: HIGH — core loop)

`_last_detail_unit` dedup has NO invalidation path except scope change — neither post-action refresh nor manual ⟳ refetches tabs. The Log tab shows the pre-run journal forever. Second face: a vanished unit's stale name blocks refetch when it reappears.
**Mitigation:** clear `_last_detail_unit` (and refetch) on action completion and manual ⟳; clear on empty selection. Keep the dedup for the 10s path.

## F4 — Viewport scroll position jumps to the top every 10 seconds (P2; plausibility: HIGH in system scope)

Model reset yanks QTableView to the top each refresh; `_reselect` restores selection but not scroll. Browsing any list longer than one screen = yanked every 10s.
**Mitigation:** capture/restore `verticalScrollBar().value()` or `scrollTo(reselected_index)`. Two lines.

## F5 — Unreadable system journal renders as "(no journal entries)" (P2 general / P3 this machine; plausibility: HIGH outside wheel/adm)

`journalctl` exits 0 with EMPTY stdout and prints its "not seeing system messages" hint to stderr — which is only read on failure. The tab asserts "(no journal entries)" — a confident false statement. Works on Dustin's wheel account, which is exactly why it'll never be noticed until it lies.
**Mitigation:** on zero entries in system scope, surface captured stderr / render a membership hint.

## F6 — Schedule tab computes the wrong schedule under drop-in overrides; misses legal `OnCalendar =` spacing (P2; plausibility: MEDIUM)

`cal_lines[0]` takes the SUPERSEDED base value under the canonical reset-then-set drop-in pair; whitespace around `=` (legal) fails the startswith test; multiple OnCalendar lines only get elapses for the first.
**Mitigation:** stop re-deriving from text — `systemctl show -p TimersCalendar,NextElapseUSecRealtime` gives the EFFECTIVE schedule. If text stays: implement reset semantics + tolerant matching.

## F7 — Error and confirmation messages wiped by the refresh status line (P3; occurrence HIGH)

The status bar is the only error channel; every refresh overwrites it. Action success "ok" is overwritten ~200ms later by the post-action refresh. Failures survive ≤1 cycle.
**Mitigation:** permanent right-side QLabel for the freshness counter; `showMessage` reserved for errors/confirmations.

## F8 — Scope flailing can parse one scope's `show` response against the other's unit list (P3; plausibility: LOW)

`_result_units` is scope-unqualified; the same-scope race was handled and tested, the cross-scope variant slipped. User/system share unit NAMES (dbus.service…) so wrong-row green/red is possible.
**Mitigation:** store `(scope, units)` and verify before parsing.

## F9 — Two uncaught exception classes turn refresh into a silent permanent freeze (P3 individually; worst failure mode in the app)

(a) `"activates": null` (systemd table-to-JSON emits null for empty cells) → stored None → later `sorted({None, str})` → TypeError. (b) absurd µs epochs → `fromtimestamp` OverflowError/OSError. Both escape the `(ValueError, KeyError)` catch; PySide6 prints to unwatched stderr and continues; because the bad unit persists, EVERY 10s cycle dies at the same line — table + "refreshed" stamp freeze at last good state with no visible error. Worse than a crash.
**Mitigation:** type-validate parser fields (raise ValueError); clamp fromtimestamp inputs; optionally broaden the catch to Exception at this external-program trust boundary.

## F10 — Uniform 5s timeout makes the Log tab unusable on machines with huge journals (P3; plausibility: MEDIUM)

`journalctl -n 200` reverse-seek through multi-GB rotated journals is a known multi-second operation → watchdog kills at 5s → "(fetch failed) journalctl timed out" exactly at investigate-this-unit moments. Aggravator: the timeout path never drains stdout → killed journalctl pins megabytes in the never-freed QProcess (DEF-T4-01 interaction).
**Mitigation:** per-kind timeout (journal 15-30s) and/or `--since=-14d` bound.

## F11 — ANSI escape sequences in journal messages render as gibberish (P3; plausibility: HIGH)

Colorized daemon output passes through `-o json` raw → literal control characters in the Log tab.
**Mitigation:** strip ECMA-48 CSI/OSC at render. (Related: `if e.ts_usec` folds epoch-0 into "—" — worth a comment.)

## F12 — Scope flip doesn't invalidate `_expected_tab_ids`; tabs can show another scope's unit with nothing selected (P3; plausibility: LOW)

In-flight tab fetch at flip time lands after the flip, passes the freshness gate, fills the tabs — and the pane has NO unit-name label to contradict the misreading.
**Mitigation:** clear `_expected_tab_ids` + blank tabs in `set_scope`; add a pane header label ("foo.timer — fetched 14:02:11") which also makes F3's staleness self-evident.

## F13 — `--` flag-termination applied inconsistently across read paths (P3; plausibility: NEAR-NIL)

actions + fetch_calendar terminate flags; fetch_cat/details/log/results don't. Only `-.slice`/`-.mount` exist organically and neither appears in these views — consistency hardening only.

## Deferments register — severity/framing checks

- **NOTE-T3 understates reality:** test_client.py uses qtbot and collects ALPHABETICALLY BEFORE test_format.py, so QApplication (and its setlocale) is constructed before the format assertions run TODAY. On any non-English LC_TIME machine the suite fails deterministically, not hypothetically. LOW stays right for this machine; the one-line conftest fix is cheap enough to just do.
- **DEF-T4-01 sizing optimistic:** the timeout path never drains stdout — a repeatedly-timing-out journalctl pins megabytes per kill, not kilobytes. Still session-bounded; note the interaction.
- **DEF-A-01, DEF-T2-01 (LOW):** agreed. DEF-T2-01's fix is a natural home for F5's stderr-hint surfacing.

## Handled Well

`parse_show_results` empty-block alignment (probed, index-walked, regression-tested both positions); QProcess identity guards incl. spawn failure; freshness gating by full-id equality (calendar-under-wrong-unit genuinely closed); no shell anywhere (injection-proof argv, escaped names pinned); stop-confirm captures target before the modal (race degrades safely); double-launch safe (stateless, read-mostly); clock steps bounded to one cycle (single `now` per refresh); filter box regex-safe (setFilterFixedString); same-scope results/argv alignment handled AND tested.
