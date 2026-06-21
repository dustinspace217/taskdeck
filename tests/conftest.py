"""Shared pytest configuration.

QT_QPA_PLATFORM=offscreen makes Qt render without a display server, so the
whole suite runs headless (CI, SSH sessions, subagents). Set here — before
any test imports a Qt module — because Qt reads it at QApplication creation.
Hard assignment (not setdefault): tests must NEVER inherit the session's
platform — a Plasma session can export QT_QPA_PLATFORM=wayland, and setdefault
would silently run the suite against the live compositor, making the Task-7
screenshot artifact non-deterministic. Hard assignment guarantees headless.
pytest-qt provides the `qtbot` fixture and the shared QApplication.
"""
import os
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Same never-inherit rationale for LOCALE: QApplication calls
# setlocale(LC_ALL, "") at construction on Unix, and test_client.py (which
# uses qtbot) collects alphabetically BEFORE test_format.py — so on any
# non-English LC_TIME machine, the %a/%b weekday/month assertions would fail
# deterministically (QA NOTE-T3, escalated and verified in Phase B).
os.environ["LC_TIME"] = "C"

# Pin the LOCAL timezone to a fixed NON-UTC zone for the whole suite. The
# calendar's window boundaries and EVERY displayed time are LOCAL (the model +
# subprocess @-args stay UTC-absolute; only display/window math is local — see
# calendar_view._local_dt and calendar_model.local_calendar_window). A test that
# asserts "a run at local 06:00 renders at the 06:00 position, not 13:00 UTC"
# is only MEANINGFUL against a non-UTC zone — on a UTC CI box local==UTC and the
# assertion would pass even if the code wrongly used UTC. America/Los_Angeles
# (PDT/PST, Dustin's zone, UTC-7/-8) gives a fixed, DST-aware offset so the
# local-vs-UTC tests would FAIL if any render site reverted to UTC. tzset()
# applies it to the C library that datetime.astimezone()/fromtimestamp() read.
os.environ["TZ"] = "America/Los_Angeles"
time.tzset()
