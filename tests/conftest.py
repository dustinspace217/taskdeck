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
