# Task Deck — systemd task scheduler GUI

Native PySide6 (Qt Widgets) app: DSM-style table of systemd user+system
timers/services with logs and user-unit actions. No web code, no sudo, no daemon.

## Commands
- Run: `python3 -m taskdeck` (from repo root; needs python3-pyside6 RPM)
- Test: `python3 -m pytest`  (hermetic; offscreen Qt)
- Integration (read-only, real systemd): `python3 -m pytest -m realsystemd`
- Lint: `ruff check .`  Type check: `mypy taskdeck`

## Architecture (4 bounded modules)
- `taskdeck/systemd_client.py` — ALL subprocess I/O. Parsers are pure functions;
  SystemdClient is async (QProcess), 5s timeout, single-flight per request key.
  No widget imports here, ever (v2 tray reuses this module headless).
- `taskdeck/models.py` — table model + µs-epoch → human time rendering.
- `taskdeck/actions.py` — pure argv builder; raises on any non-user-scope action.
- `taskdeck/main_window.py` — the only module that builds widgets.

## Hard rules
- System scope is READ-ONLY by design — enforced in actions.py AND disabled in UI.
- Never add sudo/polkit paths. Never block the UI thread on subprocess I/O.
- Spec: docs/superpowers/specs/2026-06-10-systemd-task-gui-design.md
- Plan: docs/superpowers/plans/2026-06-10-taskdeck-v1.md (Status block = re-entry point)

## Review agents (QA phase)
code-reviewer + test-analyzer always; silent-failure-hunter (subprocess error paths)
and adversarial-tester (malformed systemd output) are the relevant extras.
