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

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Same never-inherit rationale for LOCALE: QApplication calls
# setlocale(LC_ALL, "") at construction on Unix, and test_client.py (which
# uses qtbot) collects alphabetically BEFORE test_format.py — so on any
# non-English LC_TIME machine, the %a/%b weekday/month assertions would fail
# deterministically (QA NOTE-T3, escalated and verified in Phase B).
os.environ["LC_TIME"] = "C"
