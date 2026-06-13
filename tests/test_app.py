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
    if hasattr(qapp, "_taskdeck_refs"):
        delattr(qapp, "_taskdeck_refs")  # clear a prior tray test's leftover
    window = appmod.build(qapp, start_in_tray=False, auto_refresh=False)
    qtbot.addWidget(window)
    assert window.isVisible()
    assert window._hide_to_tray is False           # close still quits
    assert not hasattr(qapp, "_taskdeck_refs")     # no monitor/tray was created


def test_build_no_tray_with_flag_announces_monitoring_off(qapp, qtbot, monkeypatch):
    # --tray on a machine with no tray must not silently drop the background
    # half the user asked for (QA SFH P1).
    monkeypatch.setattr(appmod, "_tray_available", lambda: False)
    window = appmod.build(qapp, start_in_tray=True, auto_refresh=False)
    qtbot.addWidget(window)
    msg = window.statusBar().currentMessage()
    assert "No system tray" in msg and "monitoring is off" in msg


def test_tray_quit_sets_quitting_flag(qapp, qtbot, monkeypatch):
    # The real_quit producer of _quitting (the closeEvent tests pin the
    # consumer; this pins the producer the only real exit path uses).
    monkeypatch.setattr(appmod, "_tray_available", lambda: True)
    monkeypatch.setattr(FailureMonitor, "start", lambda self: None)
    try:
        window = appmod.build(qapp, start_in_tray=True, auto_refresh=False)
        qtbot.addWidget(window)
        _monitor, tray = qapp._taskdeck_refs
        tray._menu.actions()[-1].trigger()   # last menu entry is Quit → real_quit
        assert window._quitting is True
    finally:
        qapp.setQuitOnLastWindowClosed(True)


def test_autostart_exec_path_prefers_which_then_falls_back(monkeypatch):
    monkeypatch.setattr(appmod.shutil, "which", lambda name: "/usr/bin/taskdeck")
    assert appmod._autostart_exec_path() == "/usr/bin/taskdeck"
    monkeypatch.setattr(appmod.shutil, "which", lambda name: None)
    assert appmod._autostart_exec_path().endswith("/.local/bin/taskdeck")


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
