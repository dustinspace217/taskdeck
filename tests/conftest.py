"""Shared pytest configuration.

QT_QPA_PLATFORM=offscreen makes Qt render without a display server, so the
whole suite runs headless (CI, SSH sessions, subagents). Set here — before
any test imports a Qt module — because Qt reads it at QApplication creation.
pytest-qt provides the `qtbot` fixture and the shared QApplication.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
