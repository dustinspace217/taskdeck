# QA — Tray + Failure Notifications (2026-06-13)

Three-phase review of the tray feature (commits for Tasks 1–5) plus the bundled
DEF-T4-01 leak fix. Four independent Phase A reviewers (code-reviewer,
test-analyzer, silent-failure-hunter, adversarial-tester), all Opus-tier
(matching the Opus-authored code). **Phase B note:** the four reports converged
with no genuine conflict (the one apparent tension — code-reviewer "leak fix is
clean" vs test-analyzer "leak *test* is vacuous" — is not a contradiction:
correct code, weak test). Given that, the head agent adjudicated directly rather
than running a full cross-examination matrix.

## Phase A findings (condensed, faithful)

### code-reviewer — verdict FIX-FIRST
- **P1** Unquittable if the tray icon dies mid-session: `isSystemTrayAvailable()`
  true at build enables close-to-tray + `setQuitOnLastWindowClosed(False)`, but
  the icon can later fail to display, leaving no reachable Quit. → **DEFERRED**
  (DEF-TR-02): robustly detecting a mid-session tray death is hard and a fix
  risks its own bugs; accepted corner, documented.
- **P1** Monitor request-id namespace diverged from the plan (`failed:` vs
  planned `monitor-failed:`), and the docstring credited the id prefix for an
  isolation actually provided by the separate client. → **FIXED** (comment): the
  monitor's `_on_finished` comment now says the isolation is the separate
  client; the `failed:` id is kept (no real collision — minimal-diff).
- **P2** notification_text "trails an empty ' — '" comment phrasing; startup
  count unbounded-but-harmless; `--tray`-no-tray silent (see SFH). → addressed
  under SFH items / left as benign.
- **Verified CLEAN:** the leak fix's three hazards (sweep never touches an
  in-flight proc; no double-retire; on_finished reads a parked-not-freed proc);
  edge-triggered diff; two-client isolation; reference-keeping; close-to-tray
  lifetime; autostart path safety; `list_failed_services`.

### test-analyzer — verdict GAPS-FOUND
- **P1 (vacuous)** `test_finished_processes_are_swept_not_leaked` asserted
  `len(_finished) <= 1`, satisfied by `.clear()` alone — did not prove the
  QProcess is freed. → **FIXED**: now uses `Shiboken.isValid()` to prove the
  C++ object is deleted after the sweep.
- **P1** Monitor QTimer `start()/stop()`/periodic path untested. → **FIXED**.
- **P1** Tray Quit path (`real_quit` producer of `_quitting`) untested. → **FIXED**.
- **P2** `_autostart_exec_path` resolution untested; no-tray negative assertion;
  multi-unit `unit_failed` ordering; notification duration. → exec-path +
  no-tray negative **FIXED**; ordering/duration left (cosmetic).
- **Verified CLEAN:** baseline-None, wrong-scope guard, edge-trigger diff,
  closeEvent conditions all well-pinned; full hermeticity.

### silent-failure-hunter — verdict FIX-FIRST
- **P0** Monitor goes blind silently: only `client.finished` wired, and an
  unguarded `parse_list_units` raise escapes the slot to a hidden status bar AND
  re-poisons every 60s. "No notification" falsely reads as "healthy." →
  **FIXED**: `client.failed` + parse-guard feed a consecutive-failure counter;
  `monitor_blind` after 3 → Critical notification + persistent tooltip.
- **P1** `set_autostart` write failure desyncs the checkmark silently. →
  **FIXED**: revert to ground truth + surface the error (blockSignals guards the
  revert from looping).
- **P1** `--tray` with no tray silently downgrades. → **FIXED**: status-bar note.
- **P1** `showMessage` no-ops when DE notifications are off. → **FIXED** (partial):
  `supportsMessages()` reflected in the tooltip. Live failure-count tooltip
  deferred (DEF-TR-03).
- **P2** First close-to-tray gives no "still running" hint. → **DEFERRED**
  (DEF-TR-04, nice-to-have).
- **Verified loud:** window `_on_finished` parse handling; client `failed` path;
  the `_sweep_finished` disconnect-swallow (correct, narrow).

### adversarial-tester — verdict ISSUES-FOUND
- **P1** Flap within one 60s window is missed; docstring overpromised "ENTERING
  failed." → **FIXED** (docstring reworded to be honest about level-poll limits;
  behavior is a documented, accepted property of a level poll).
- **P1** `_finished` not swept when no further `request()` runs (quit / last
  action in flight). → **FIXED**: `flush_finished()` called from closeEvent's
  quit branch.
- **P1** Autostart `Exec=` breaks on a path with spaces. → **FIXED**: Desktop-
  Entry quoting.
- **P2** Notification body unbounded for long/multi-line descriptions. → **FIXED**:
  flatten + 120-char bound. Per-name length cap in the summary → minor, deferred.
- **Probed ROBUST:** QProcess free-in-slot avoidance; request-id reuse identity
  guards; kill-echo-vs-sweep race; scope isolation; off-by-one on the 5-name cap;
  `\x2d`/colon unit names; no-tray; quit with a poll in flight.

## Phase C — head-agent synthesis

**Fixed in this batch (commit after this doc):** the P0 (monitor blindness +
parse guard + `monitor_blind` → tray) and every P1 above except DEF-TR-02, plus
the vacuous-test fix, autostart Exec quoting + write-failure handling, `--tray`
degradation message, `supportsMessages` tooltip, `flush_finished`, notification
bounding, and the named test gaps. 155 hermetic + 3 live, ruff + mypy clean.

**Deferments registered** (see the tray plan doc appendix): DEF-TR-02
(unquittable-if-tray-dies, LOW, accepted corner), DEF-TR-03 (live failure-count
tooltip, LOW enhancement), DEF-TR-04 (first close-to-tray hint, LOW), plus the
pre-existing DEF-TR-01 (bind refresh to window visibility). The sub-minute-flap
limitation is documented in the monitor docstring (a property of a level poll,
not a deferment).
