# Task Deck — systemd Task Scheduler GUI — Design Spec

## Status (updated 2026-06-10)
Phase: Design (spec written, awaiting Dustin's review)
Done: brainstorm complete — layout, scope, v1 boundary, residency, data layer all decided with Dustin
Next: Dustin reviews this spec → writing-plans skill → implementation plan
Blocked: nothing

## What this is

A native PySide6 (Qt Widgets, Python) desktop application for Fedora KDE that does for
systemd what Synology DSM's Task Scheduler does for a NAS: one window showing every
scheduled task (timer) and service, when it runs next, when it last ran, whether it
succeeded, and its logs — with run-now/enable/disable/stop actions on user units.

**Why it exists:** nothing packaged for Fedora covers this. Zeit/KCron are cron-only
(systemd units are invisible to them); SystemdGenie and GTK Systemd Manager are
unit-file managers without scheduler ergonomics and aren't packaged for F44; Cockpit is
web-based (excluded by constraint); isd/systemctl-tui are TUIs. Survey done 2026-06-10.

**Working title:** "Task Deck" (window title), Python package `taskdeck`. Rename is a
one-line change; bikeshed freely.

## Decisions log (brainstorm, 2026-06-10)

| Decision | Choice | Why |
|---|---|---|
| Toolkit | PySide6 / Qt Widgets (RPM `python3-pyside6` 6.11.1) | Native Breeze look on KDE, no web surface, Python readability, strong LLM fluency; Widgets (not QML) because the UI is tables+forms |
| Unit scope | User instance + System instance **read-only** | Full "everything scheduled/running" visibility; actions stay user-only so the app never needs sudo/polkit |
| v1 boundary | View + basic actions (run-now/enable/disable/stop) on user units | Each action is one systemctl call; this is what makes it DSM, not a report. Create/edit = v3 |
| Residency | On-demand window, no tray | Like Task Scheduler/DSM; no always-running process. Tray/notifier = possible v2 |
| Data layer | Shell out to `systemctl`/`journalctl --output=json` via async QProcess | Same interface Dustin uses by hand (debuggable), stable contract, most maintainable. QtDBus push is the documented upgrade path if polling ever disappoints |
| Layout | "A — DSM-style stack" (browser mockup pick, single decisive click) + B's tabbed detail pane | Full-width table for long unit names; tabs separate Log / Details / Schedule / Unit file |

Constraints (hard): no sudo anywhere; no web/browser code; user-instance actions only;
system instance strictly read-only.

## Architecture

Single process, four bounded modules under `taskdeck/` (flat package — RPM-only deps mean no pip install step, so `python3 -m taskdeck` runs from the repo root). Each module answers: what
it does, how you use it, what it depends on. No module reaches around another.

### `systemd_client.py` — data layer (no Qt widgets; QProcess + QObject signals only)
- Async wrappers around exactly these commands (each per scope, `--user` or not):
  - `systemctl [--user] list-timers --all -o json`
    → verified shape: `[{"next":µs-epoch|null,"left":…,"last":µs-epoch|null,"passed":…,"unit":"x.timer","activates":"x.service"}]`
  - `systemctl [--user] list-units --type=service -o json` (unit/load/active/sub/description)
  - `journalctl [--user] -u <unit> -o json -n 200` (log tail, lazy per selection)
  - `systemctl [--user] cat <unit>` (unit file text, lazy)
  - `systemctl [--user] show <unit…> -p Result,ExecMainStatus,ExecMainExitTimestamp`
    (batched last-result lookup — **exact output shape to verify at plan time**, it is
    the one data contract not yet empirically probed)
  - `systemd-analyze calendar --iterations=5 "<OnCalendar expr>"` (Schedule tab's
    "next 5 elapses"; expression comes from the unit file — flag to verify at plan
    time whether `--iterations` exists on systemd 258 or the tab computes from
    repeated single calls)
- Actions (user scope only): `systemctl --user start|stop|enable|disable <unit>`.
  Run-now on a timer row starts its `activates` service, not the timer.
- Returns frozen dataclasses (`TimerRow`, `ServiceRow`, `LogEntry`, `ActionResult`).
  Emits Qt signals on completion; never blocks the UI thread.
- Every call: 5s timeout (kill + error), checked exit code, stderr captured.
  `SystemdClientError(command, exit_code, stderr)` — errors carry enough to act on.
- Concurrency guard: one in-flight refresh per scope; a refresh requested while one is
  running is coalesced, not stacked (bounded, Power-of-Ten rule 3).

### `models.py` — Qt models (depends on dataclasses only)
- `TaskTableModel(QAbstractTableModel)` over rows; `QSortFilterProxyModel` provides the
  filter box and column sorting for free (Qt-idiomatic; no hand-rolled sort/filter).
- Columns: Task | Status | Next run | Last run | Last result.
  Next/Last render relative + absolute ("today 23:10 (in 3h 48m)"); µs-epoch conversion
  happens here, not in the client (client stays a faithful transcription of systemd).

### `main_window.py` — UI shell (depends on models, actions)
- Layout A: toolbar → full-width QTableView → tabbed bottom pane.
- Toolbar: scope toggle (User / System🔒), view selector (Timers / Services), action
  buttons, ⟳ Refresh, filter box (right-aligned).
- Bottom pane tabs: **Log** (monospace tail, error lines tinted), **Details** (curated
  properties from `systemctl show`: Description, FragmentPath, ActiveState/SubState,
  MainPID, MemoryCurrent/Peak, CPUUsageNSec, TriggeredBy/Triggers — not the full
  200-key dump), **Schedule** (timer's OnCalendar lines + next 5 elapses), **Unit
  file** (read-only `systemctl cat` view).
- Interactions: row select → lazy-load tabs; clicking a "✘ exit N" result cell jumps to
  the Log tab; System scope hard-disables action buttons with tooltip "system units are
  read-only by design".
- Refresh model: QTimer every 10s while window focused + after every action + manual ⟳.

### `actions.py` — action layer (depends on systemd_client)
- Wraps the four actions; Stop shows a confirm dialog (it can interrupt a mid-run job —
  e.g. astrowidget-fetch mid-download). Run/enable/disable are unconfirmed (cheap,
  reversible). Failures surface systemctl's stderr verbatim in the status bar.

### Entry point: `taskdeck/app.py` (main()), launched as `python3 -m taskdeck`
via `taskdeck/__main__.py`. No console script — there is no pip packaging by
design. Desktop file + icon: v1 nice-to-have, not a gate.

## Data flow

```
open/⟳/QTimer ──▶ systemd_client (QProcess, async) ──▶ JSON ──▶ dataclasses
                                                                    │
row selected ──▶ lazy: journalctl tail / cat / show ──▶ detail tabs ◀┘
                                                                    │
                                            TaskTableModel ◀────────┘
action button ──▶ actions.py ──▶ systemctl --user … ──▶ refresh ──▶ (loop above)
```

Services view default filter: hide `inactive` units unless "show inactive" is toggled
(system scope has ~100+ services; the noise would bury the signal). Timers view always
shows all timers in scope.

## Error handling (Power of Ten 5/7)

- No silent failures: every subprocess error surfaces in the status bar with the actual
  stderr text. An empty table is only ever shown with an explicit "0 units" state —
  a failed fetch shows an error state, never an empty table pretending success.
- Malformed JSON (systemd version drift): caught, logged, surfaced as error state with
  the raw first 200 chars for diagnosis.
- Action failures: status bar + the row keeps its pre-action state (refresh re-syncs).

## Testing

- pytest + pytest-qt (`QT_QPA_PLATFORM=offscreen` in CI/headless).
- `systemd_client`: canned JSON fixtures (captured from this machine) — deterministic,
  no real systemctl, parallel-safe. Includes malformed-JSON and timeout cases.
- `models`: µs-epoch rendering, sort/filter behavior, null next/last handling.
- Smoke test: render MainWindow offscreen with fixture data, `grab()` → PNG artifact
  (Claude's self-verification loop; also catches layout regressions).
- Opt-in integration marker (`-m realsystemd`): read-only calls against the real user
  instance; never runs actions.
- ruff + mypy configured in the FIRST commit (Power of Ten rule 10); both already
  installed (RPM, 2026-06-10).

## Prerequisites (verify at plan time)

- `sudo dnf install python3-pyside6` — NOT yet installed (verified available, 6.11.1).
- pytest-qt availability as RPM (`python3-pytest-qt`) — else note the venv tradeoff:
  RPM PySide6 lives in system site-packages, so a venv would need
  `--system-site-packages`. Decide in plan, prefer all-RPM if available.
- Exact JSON field names of `list-units -o json` and `show -p Result,…` output.

## Roadmap (post-v1, not in scope now)

- **v2 — tray/notifier (optional):** separate thin process reusing `systemd_client`;
  notifies on task failure. The data layer's no-widgets rule exists partly for this.
- **v3 — create/edit (the DSM "Create task" form):** schedule builder generating
  `OnCalendar` with live plain-English preview, validated via
  `systemd-analyze calendar`; raw-expression field for advanced cases; writes
  `.timer`/`.service` pairs to `~/.config/systemd/user/` + `daemon-reload`. Edits only
  units it created (marker comment) — never rewrites hand-written units in place.

## References

- Brainstorm mockups: `.superpowers/brainstorm/1238015-1781143098/content/` (layout
  options + final consolidated mockup, `design-final.html`).
- Verified JSON probe outputs: see Decisions log; probes run 2026-06-10 in-session.
