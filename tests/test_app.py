"""app.build() wiring tests — hermetic (no app.exec(), no real subprocesses).

build() is the bootstrap split out of main() exactly so it's testable. The tray
branch is forced via the _tray_available indirection, and FailureMonitor.start
is stubbed so the tray path never spawns a real systemctl poll.
"""
import sys

import pytest
from PySide6.QtWidgets import QSystemTrayIcon  # noqa: F401 (kept for clarity of intent)

from taskdeck import app as appmod
from taskdeck.monitor import FailureMonitor


@pytest.fixture(autouse=True)
def _restore_excepthook():
    # build() installs a sys.excepthook bound to its window; restore the
    # original so it doesn't leak into other tests.
    original = sys.excepthook
    yield
    sys.excepthook = original


def test_build_without_tray_shows_window(qapp, qtbot, monkeypatch):
    monkeypatch.setattr(appmod, "_tray_available", lambda: False)
    window = appmod.build(qapp, start_in_tray=True, auto_refresh=False)  # no tray
    qtbot.addWidget(window)
    assert window.isVisible()
    assert window._hide_to_tray is False  # close still quits


def test_build_with_tray_enables_hide_and_starts_hidden_on_flag(qapp, qtbot, monkeypatch):
    monkeypatch.setattr(appmod, "_tray_available", lambda: True)
    monkeypatch.setattr(FailureMonitor, "start", lambda self: None)  # no real poll
    try:
        window = appmod.build(qapp, start_in_tray=True, auto_refresh=False)
        qtbot.addWidget(window)
        assert window._hide_to_tray is True
        assert not window.isVisible()             # --tray → start hidden in the tray
        assert hasattr(qapp, "_taskdeck_refs")    # monitor + tray kept alive
    finally:
        qapp.setQuitOnLastWindowClosed(True)      # undo the tray-branch global mutation


def test_build_with_tray_shows_window_without_flag(qapp, qtbot, monkeypatch):
    monkeypatch.setattr(appmod, "_tray_available", lambda: True)
    monkeypatch.setattr(FailureMonitor, "start", lambda self: None)
    try:
        window = appmod.build(qapp, start_in_tray=False, auto_refresh=False)
        qtbot.addWidget(window)
        assert window.isVisible()                 # no flag → normal show
    finally:
        qapp.setQuitOnLastWindowClosed(True)
