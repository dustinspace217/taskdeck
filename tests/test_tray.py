"""Tray helper tests — pure file/text logic, no QSystemTrayIcon needed.

The QSystemTrayIcon itself needs a platform tray (absent under offscreen QPA),
so these tests cover the parts that DON'T: the autostart .desktop read/write and
the notification text. monkeypatch points XDG_CONFIG_HOME at a tmp dir so the
real ~/.config is never touched.
"""
import pytest
from PySide6.QtWidgets import QSystemTrayIcon, QWidget

from taskdeck.monitor import FailureMonitor
from taskdeck.systemd_client import SystemdClient
from taskdeck.tray import (
    Tray,
    autostart_desktop_path,
    is_autostart_enabled,
    notification_text,
    set_autostart,
)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    return cfg


def make_tray(qtbot, exec_path="/x/taskdeck"):
    # QSystemTrayIcon constructs fine under offscreen QPA even though
    # isSystemTrayAvailable() is False — show()/showMessage() just no-op — so
    # the wiring is testable headlessly (probed 2026-06-13).
    window = QWidget()
    qtbot.addWidget(window)
    monitor = FailureMonitor(SystemdClient(), scope="user")
    tray = Tray(window, monitor, exec_path, on_quit=lambda: None)
    return tray, window, monitor


def test_autostart_path_under_xdg_config(tmp_config):
    assert autostart_desktop_path() == tmp_config / "autostart" / "taskdeck.desktop"


def test_set_autostart_true_writes_tray_launcher(tmp_config):
    set_autostart(True, "/home/dustin/.local/bin/taskdeck")
    assert is_autostart_enabled()
    text = autostart_desktop_path().read_text()
    # Launches to the tray (--tray), and is enabled for autostart-aware DEs.
    assert "Exec=/home/dustin/.local/bin/taskdeck --tray" in text
    assert "X-GNOME-Autostart-enabled=true" in text


def test_set_autostart_false_removes_and_is_idempotent(tmp_config):
    set_autostart(True, "/x/taskdeck")
    assert is_autostart_enabled()
    set_autostart(False, "/x/taskdeck")
    assert not is_autostart_enabled()
    set_autostart(False, "/x/taskdeck")  # absent already — must not raise
    assert not is_autostart_enabled()


def test_is_autostart_enabled_false_when_absent(tmp_config):
    assert not is_autostart_enabled()


def test_notification_text_formats_title_and_body():
    title, body = notification_text("backup.service", "Nightly backup")
    assert title == "Task Deck"
    assert body == "backup.service failed — Nightly backup"


def test_notification_text_omits_dash_when_no_description():
    _, body = notification_text("x.service", "")
    assert body == "x.service failed"


# -- Tray wiring (construct the real Tray; QSystemTrayIcon no-ops offscreen) ----


def test_tray_autostart_action_reflects_state_and_writes(tmp_config, qtbot):
    tray, _, _ = make_tray(qtbot)
    assert tray._autostart_action.isChecked() is False  # no entry yet
    tray._autostart_action.setChecked(True)             # fires toggled → writes
    assert is_autostart_enabled()
    assert "Exec=/x/taskdeck --tray" in autostart_desktop_path().read_text()
    tray._autostart_action.setChecked(False)
    assert not is_autostart_enabled()


def test_tray_left_click_toggles_window(tmp_config, qtbot):
    tray, window, _ = make_tray(qtbot)
    window.show()
    qtbot.waitExposed(window)
    assert window.isVisible()
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert not window.isVisible()                       # hide
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert window.isVisible()                           # show again


def test_failure_signal_routes_to_critical_notification(tmp_config, qtbot, monkeypatch):
    tray, _, monitor = make_tray(qtbot)
    calls = []
    monkeypatch.setattr(tray._tray, "showMessage", lambda *a: calls.append(a))
    monitor.unit_failed.emit("backup.service", "Nightly backup")
    assert calls, "a failure must produce a notification"
    title, body, icon, _msecs = calls[0]
    assert title == "Task Deck"
    assert body == "backup.service failed — Nightly backup"
    assert icon == QSystemTrayIcon.MessageIcon.Critical


def test_startup_failures_route_to_one_summary(tmp_config, qtbot, monkeypatch):
    tray, _, monitor = make_tray(qtbot)
    calls = []
    monkeypatch.setattr(tray._tray, "showMessage", lambda *a: calls.append(a))
    monitor.startup_failures.emit([("a.service", "A"), ("b.service", "B")])
    assert len(calls) == 1
    body = calls[0][1]
    assert "2 services currently failed" in body
    assert "a.service" in body and "b.service" in body
