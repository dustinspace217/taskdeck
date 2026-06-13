#!/usr/bin/env python3
"""Render the README screenshot from the REAL widget with representative data.

Run from the repo root:  python3 tools/screenshot.py
Output:  docs/screenshot.png

This renders the actual MainWindow offscreen (no display needed) populated with
a handful of generic, widely-recognizable system timers — fstrim, logrotate,
certbot, a backup that failed — so the image is honest (it IS the shipping UI)
without exposing anyone's machine. `now` and all timestamps are fixed so the
output is byte-reproducible across runs.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# Force the offscreen Qt platform BEFORE any PySide6 import, and a fixed locale
# so the rendered times don't drift with the runner's locale.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LC_TIME", "C")

# Make `taskdeck` importable when run as a plain script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from taskdeck.main_window import MainWindow  # noqa: E402
from taskdeck.systemd_client import (  # noqa: E402
    LastResult,
    ScheduleInfo,
    ServiceRow,
    SystemdClient,
    TimerRow,
)

# Fixed reference instant so "in 4h" / "2h ago" render identically every run.
NOW = datetime(2026, 6, 12, 14, 0, 0)


def usec(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


# (unit, activates, next_run, last_run) — a realistic mix of cadences/states.
TIMERS = [
    TimerRow("fstrim.timer", "fstrim.service",
             usec(datetime(2026, 6, 15, 0, 0)), usec(datetime(2026, 6, 8, 0, 0))),
    TimerRow("logrotate.timer", "logrotate.service",
             usec(datetime(2026, 6, 13, 0, 0)), usec(datetime(2026, 6, 12, 0, 0))),
    TimerRow("backup.timer", "backup.service",
             usec(datetime(2026, 6, 13, 2, 0)), usec(datetime(2026, 6, 12, 2, 0))),
    TimerRow("certbot-renew.timer", "certbot-renew.service",
             usec(datetime(2026, 6, 12, 18, 0)), usec(datetime(2026, 6, 12, 6, 0))),
    TimerRow("man-db.timer", "man-db.service",
             usec(datetime(2026, 6, 12, 15, 30)), usec(datetime(2026, 6, 12, 9, 0))),
    TimerRow("reflector.timer", "reflector.service",
             usec(datetime(2026, 6, 16, 6, 0)), None),  # never ran
]

SERVICES = [
    ServiceRow("fstrim.service", "loaded", "inactive", "dead", "Discard unused blocks"),
    ServiceRow("logrotate.service", "loaded", "inactive", "dead", "Rotate log files"),
    ServiceRow("backup.service", "loaded", "failed", "failed", "Nightly backup"),
    ServiceRow("certbot-renew.service", "loaded", "activating", "start", "Renew TLS certs"),
    ServiceRow("man-db.service", "loaded", "inactive", "dead", "Update man-db cache"),
    ServiceRow("reflector.service", "loaded", "inactive", "dead", "Refresh mirrorlist"),
]

RESULTS = {
    "fstrim.service": LastResult("success", 0),
    "logrotate.service": LastResult("success", 0),
    "backup.service": LastResult("exit-code", 1),
    "certbot-renew.service": LastResult("success", 0),
    "man-db.service": LastResult("success", 0),
}

SCHEDULES = {
    "fstrim.timer": ScheduleInfo(("Mon *-*-* 00:00:00",), ()),
    "logrotate.timer": ScheduleInfo(("*-*-* 00:00:00",), ()),
    "backup.timer": ScheduleInfo(("*-*-* 02:00:00",), ()),
    "certbot-renew.timer": ScheduleInfo(("*-*-* 00,12:00:00",), ()),
    "man-db.timer": ScheduleInfo(("*-*-* 09,15:00:00",), ()),
    "reflector.timer": ScheduleInfo(("Mon *-*-* 06:00:00",), ()),
}


def main() -> int:
    app = QApplication(sys.argv)
    # No fakebin paths needed: auto_refresh=False means the window never spawns
    # a subprocess — we populate the model directly below.
    client = SystemdClient()
    window = MainWindow(client, auto_refresh=False)
    window.model.set_timer_rows(TIMERS, SERVICES, RESULTS, SCHEDULES, now=NOW)
    window._data_scope = "user"  # match the populated scope (we bypassed refresh)
    # We bypassed refresh(), so the permanent freshness label still reads its
    # initial "starting…" — set it to what a real refresh would show.
    window._freshness.setText(f"{len(TIMERS)} units · user scope · refreshed {NOW:%H:%M:%S}")
    window.resize(1000, 560)
    window.show()
    app.processEvents()  # let the offscreen layout settle before grabbing

    out = Path(__file__).resolve().parent.parent / "docs" / "screenshot.png"
    out.parent.mkdir(exist_ok=True)
    ok = window.grab().save(str(out))
    print(f"{'wrote' if ok else 'FAILED to write'} {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
