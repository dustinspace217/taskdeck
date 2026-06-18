# Tray + Failure Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. TDD throughout. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A system-tray presence that watches the user's systemd services in the background and pops a desktop notification when one enters the `failed` state — so failures surface without the window open.

**Architecture:** Two new modules plus wiring. `monitor.py` is HEADLESS (no widgets, its own SystemdClient) — it polls `list-units --state=failed` on a timer, diffs against the last-known failed set, and emits `unit_failed` for *newly* failed units. `tray.py` owns the `QSystemTrayIcon`, its menu, desktop notifications, and the autostart toggle. `app.py` wires window + monitor + tray and owns app lifetime; `main_window.py`'s `closeEvent` hides to tray instead of quitting.

**Tech Stack:** PySide6 (QSystemTrayIcon, QSystemTrayIcon.showMessage for notifications — no extra deps), the existing async SystemdClient.

## Status (updated 2026-06-13, feature COMPLETE + QA done)
Phase: All 6 tasks done. Tasks 1-5 built (list_failed_services, FailureMonitor,
Tray, closeEvent hide-to-tray, app.py wiring + --tray). Three-phase QA ran
(4 reviewers; docs/qa/2026-06-13-tray/qa.md) and found a P0 (monitor blindness)
+ several P1s — all fixed in a QA batch. 155 hermetic + 3 live, ruff + mypy clean.
Done: Tasks 1-6 + QA fix batch.
Next: push, verify CI, Dustin's verdict on the running feature (real-tray
rendering of the icon/notifications is the one thing only a live desktop can
confirm — the logic is all tested).
Blocked: nothing.

### Deviation summary (2026-06-13)
Built per the locked design. One structural choice during implementation: the
window and monitor use SEPARATE SystemdClient instances (the monitor's is
headless) rather than sharing one — cleaner isolation, no shared signal-bus
races (behavioral-change, additive). QA fix batch fixed two found bugs (P0
monitor-blindness, P1s) — behavioral fixes, not deferments. New deferments
registered below.

### Appendix: Deferments originated in the tray feature
- **DEF-TR-01** (LOW): the window's 10s refresh keeps running while hidden to
  tray (a few `systemctl` reads/10s). Found in design; fix direction: bind the
  refresh timer to window visibility (show/hide events) so a tray-resident app
  is fully idle. Obsolete if the window is ever always-visible.
- **DEF-TR-02** (LOW, code-reviewer): if the tray icon dies mid-session, the
  window is hidden + `setQuitOnLastWindowClosed(False)` leaves no reachable
  Quit (must `kill`). Accepted corner — robust mid-session tray-death detection
  is hard and a guard risks its own bugs. Fix direction: poll
  `tray.isVisible()` and fall back to quit-on-close if the icon vanishes.
- **DEF-TR-03** (LOW, silent-failure-hunter): the tray tooltip doesn't reflect
  the LIVE failure count (only a static string / the blind warning). Enhancement:
  set the tooltip to "N service(s) failed" when the monitor reports failures, so
  state is visible on hover even when the notification daemon swallows balloons.
- **DEF-TR-04** (LOW, silent-failure-hunter): no first-time "still running in the
  tray" hint when the user first closes the window. One-shot informational
  balloon would close the surprise.

### Appendix: Single-instance guard (taskdeck/single_instance.py, 2026-06-17)
The tray + autostart feature let the app launch twice (autostart at login + a
manual launch) → duplicate tray icons. Fixed with a QLocalServer/QLocalSocket
single-instance guard: a second launch pings the primary to show its window and
exits. Reviewed (code-reviewer + test-analyzer); P1s fixed (checked `listen()`
return → real fail-open; testable `_start` seam pinning "secondary skips build";
race-safe conditional `removeServer`; per-connection cleanup). Residual:
- **DEF-SI-01** (LOW): two TRULY simultaneous *first* launches can both probe-miss
  before either listens; the loser's `listen()` fails and it falls open (runs
  without the guard, no duplicate tray but its window won't raise on later pings).
  Not reachable via autostart-then-manual-launch (never simultaneous). Fix
  direction: a brief retry/backoff on `listen()` failure, or a file lock.
- **DEF-SI-02** (LOW, test gap): the true-crash-orphan reclaim (stale socket FILE
  with no listener → `removeServer` then re-listen) is covered by the standard
  pattern + the conditional-removeServer logic, but not faithfully unit-tested
  (faithful simulation needs a subprocess that listens then is `kill -9`'d). The
  clean-shutdown reclaim path IS tested. Fix direction: a subprocess-based test.

## Design decisions (from Dustin, 2026-06-13)
1. **Close behavior:** the window's close button hides to tray and keeps monitoring. Quit lives in the tray menu (and is the ONLY thing that exits).
2. **Autostart:** off by default; a checkable in-app action toggles a `~/.config/autostart/taskdeck.desktop` entry (file presence IS the persisted state). The autostart entry launches the app to the tray (`--tray`).
3. **Notifications:** fire only when a unit ENTERS `failed` (edge-triggered via set-diff). A unit that stays failed is not re-notified. Recovery then re-failure notifies again.

## Open design choices resolved by judgment (not Dustin-facing forks)
- **Poll interval:** 60s. Failure detection isn't second-critical, and the monitor runs even when the 10s window refresh is idle. Bounded loop (QTimer).
- **Startup-failed units:** on the first poll, surface anything already failed in ONE summary notification ("N service(s) currently failed: …"), then baseline. Avoids per-unit spam at login while still surfacing overnight breakage. Subsequent new failures notify individually.
- **No system tray available** (`QSystemTrayIcon.isSystemTrayAvailable()` false — e.g. a bare WM): degrade to a plain window (close = quit, no background monitor). The tray is the whole background mechanism; without it there's nowhere to live.
- **Separate SystemdClient for the monitor:** the client is widget-free precisely so it can be reused headless (CLAUDE.md). The monitor owns its own instance with its own request-id namespace (`monitor-failed:*`), so it never races the window's client over shared signal state. Redundant polling when both run is cheap (`list-units` is fast, 60s cadence).

## File structure
```
taskdeck/monitor.py    — NEW. FailureMonitor(QObject): poll + diff + signals. No widgets.
taskdeck/tray.py       — NEW. Tray(QObject): QSystemTrayIcon, menu, notify, autostart toggle.
taskdeck/systemd_client.py — MODIFY. Add list_failed_services(scope).
taskdeck/main_window.py    — MODIFY. closeEvent hides to tray; type the event param.
taskdeck/app.py            — MODIFY. Wire window+monitor+tray; --tray flag; lifetime.
tests/test_monitor.py  — NEW. Diff logic, baseline, re-fail, scope, headless via FakeClient.
tests/test_tray.py     — NEW. Autostart file write/remove; notification text; toggle state.
tests/test_window_logic.py — MODIFY. closeEvent hide-vs-quit.
```

---

### Task 1: `list_failed_services` client method

**Files:** Modify `taskdeck/systemd_client.py`; Test `tests/test_client.py`.

- [ ] **Step 1: Failing test** — `list_failed_services("user")` emits a `finished` with request id `failed:user` and runs `list-units --type=service --state=failed --all -o json` against the injected systemctl.

```python
def test_list_failed_services_emits_expected_id_and_argv(qtbot):
    client = SystemdClient(systemctl=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.list_failed_services("user")
    request_id, stdout = blocker.args
    assert request_id == "failed:user"
    argv = stdout.splitlines()
    assert argv == ["--user", "list-units", "--type=service", "--state=failed", "--all", "-o", "json"]
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**

```python
def list_failed_services(self, scope: str) -> bool:
    """Failed services only — the monitor's poll. --state=failed narrows the
    query server-side so the diff sees just the failures, not the full list."""
    return self.request(
        f"failed:{scope}",
        [self._systemctl, *self._scope_args(scope),
         "list-units", "--type=service", "--state=failed", "--all", "-o", "json"],
    )
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.**

### Task 2: `FailureMonitor` — headless poll + diff

**Files:** Create `taskdeck/monitor.py`; Test `tests/test_monitor.py`.

The monitor holds its own client, a QTimer, and the last-known failed set. On each `failed:{scope}` response it parses, diffs, and emits. `parse_list_units` tolerates the `--state=failed` output (same JSON shape).

- [ ] **Step 1: Failing tests** (FakeClient mirrors tests/test_window_logic.py — records calls, has `finished`/`failed` signals, `list_failed_services`). Cover:
  - New failure after baseline → `unit_failed` emitted once with (unit, description).
  - Unit still failed next poll → NOT re-emitted.
  - Unit recovers then fails again → re-emitted.
  - First poll with pre-existing failures → ONE `startup_failures` emission (list), no per-unit `unit_failed`; baseline set.
  - First poll clean → no emissions; baseline empty.
  - A `failed` signal (poll error) → monitor does not crash, keeps last baseline, no spurious emission.

```python
# Sketch of the core assertion:
def test_new_failure_notifies_once(qtbot):
    client = FakeMonitorClient()
    mon = FailureMonitor(client, scope="user")
    events = []
    mon.unit_failed.connect(lambda u, d: events.append((u, d)))
    mon._poll()                                   # first poll: baseline
    client.deliver("failed:user", payload([]))    # nothing failed
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    assert events == [("backup.service", "Nightly backup")]
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    assert events == [("backup.service", "Nightly backup")]  # not repeated
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `FailureMonitor`.** Signals `unit_failed(str, str)`, `startup_failures(list)`. `start()` starts the QTimer (POLL_MS=60000) and fires an immediate first poll; `stop()` stops it. `_poll()` calls `client.list_failed_services(scope)`. `_on_finished` filters by `failed:{scope}` id, parses, diffs (`current - previous`), emits, updates `previous`. `previous` is `None` until the first successful poll (distinguishes baseline). Bound the timer; comment it intentionally periodic.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.**

### Task 3: `Tray` — icon, menu, notifications, autostart toggle

**Files:** Create `taskdeck/tray.py`; Test `tests/test_tray.py`.

Split the file-touching and text logic into pure helpers so they test without a real tray (offscreen QPA has no system tray):
- `autostart_desktop_path() -> Path` (`~/.config/autostart/taskdeck.desktop`).
- `set_autostart(enabled: bool, exec_path: str) -> None` — write or remove the file.
- `is_autostart_enabled() -> bool` — file exists.
- `notification_text(unit: str, description: str) -> tuple[str, str]` — ("Task Deck", "{unit} failed — {description}").

- [ ] **Step 1: Failing tests** for the four helpers (use a tmp HOME via monkeypatch so the real `~/.config` is never touched):
  - `set_autostart(True, ...)` creates the file with `Exec=… --tray` and `X-GNOME-Autostart-enabled=true`; `is_autostart_enabled()` True.
  - `set_autostart(False, ...)` removes it; idempotent when already absent.
  - `notification_text` formats title/body, and omits the dash when description is empty.

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement helpers + `Tray(QObject)`.** Constructor takes the window and the monitor; builds `QSystemTrayIcon` (guarded by `isSystemTrayAvailable`), a `QMenu` (Open, a checkable "Start at login", Quit), connects `activated` (Trigger → toggle window), connects `monitor.unit_failed` → `showMessage(... Critical ...)`, and `monitor.startup_failures` → one summary `showMessage`. The autostart action reflects `is_autostart_enabled()` and calls `set_autostart` on toggle. Quit calls the app's real-quit path.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.**

### Task 4: `closeEvent` hides to tray

**Files:** Modify `taskdeck/main_window.py`; Test `tests/test_window_logic.py`.

The window must hide (not quit) on close when a tray is present, and the refresh timer must keep running (the table should be current when re-shown). A `_quitting` flag on the app, set by the tray's Quit, lets a real quit through.

- [ ] **Step 1: Failing test** — with a tray active, `closeEvent` ignores the event and hides the window; with `_quitting` true, it accepts.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Type the param `event: QCloseEvent` (import from QtGui). Keep the existing `self._timer.stop()` ONLY on real quit.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.**

### Task 5: `app.py` wiring + `--tray` + lifetime

**Files:** Modify `taskdeck/app.py`; Test `tests/test_smoke.py` (construct-with-tray smoke).

- [ ] **Step 1: Failing/again-green smoke** — the app constructs window + monitor + tray without raising under offscreen QPA (tray degrades gracefully when unavailable), and `--tray` starts with the window hidden.
- [ ] **Step 2-4:** Implement. `setQuitOnLastWindowClosed(False)` so hiding the window doesn't exit. Parse `--tray` (start hidden). Create SystemdClient(s), window, FailureMonitor, Tray; `monitor.start()`. Wire Quit → set `_quitting` + `app.quit()`. Excepthook already in place.
- [ ] **Step 5: Commit.**

### Task 6: Close-out — gates, README, autostart entry, QA

- [ ] Full suite + ruff + mypy + realsystemd green.
- [ ] Regenerate screenshot if the window chrome changed (it shouldn't).
- [ ] README: a short "Background monitoring" subsection.
- [ ] `install.sh`: no change needed (the in-app toggle writes the autostart entry itself, pointing at the same shim with `--tray`).
- [ ] Three-phase QA (code-reviewer + test-analyzer always; silent-failure-hunter for the monitor's error path; adversarial-tester for the diff edge cases). Bundle the DEF-T4-01 leak-fix review (commit 6e4c3c2) into this pass — same client/lifecycle surface.
- [ ] Update this Status block + the v1 plan's deferment register if anything is deferred.
