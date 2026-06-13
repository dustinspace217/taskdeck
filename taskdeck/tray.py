"""System-tray presence: background-failure notifications + show/hide + autostart.

The Tray wraps QSystemTrayIcon and is the app's persistent face while the window
is hidden. It turns the headless FailureMonitor's signals into desktop
notifications and offers Open / Start-at-login / Quit.

The file- and text-handling pieces are module-level pure functions so they test
without a real tray (offscreen Qt has none): autostart .desktop read/write and
the notification text.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from taskdeck.monitor import FailureMonitor

_AUTOSTART_RELPATH = ("autostart", "taskdeck.desktop")

# Autostart entry launches the SAME installed launcher with --tray (start
# hidden, monitoring). Icon=taskdeck resolves from the hicolor theme that
# install.sh populates. X-GNOME-Autostart-enabled is honoured by KDE too.
_AUTOSTART_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Task Deck
Comment=Watch systemd timers and services
Exec={exec_path} --tray
Icon=taskdeck
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _config_home() -> Path:
    """XDG config base — $XDG_CONFIG_HOME or ~/.config (the spec default)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def autostart_desktop_path() -> Path:
    """Path of the autostart entry this app manages."""
    return _config_home().joinpath(*_AUTOSTART_RELPATH)


def is_autostart_enabled() -> bool:
    """Autostart state IS the file's presence — no separate persisted flag."""
    return autostart_desktop_path().exists()


def set_autostart(enabled: bool, exec_path: str) -> None:
    """Create or remove the autostart entry. Idempotent both ways."""
    path = autostart_desktop_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_AUTOSTART_TEMPLATE.format(exec_path=exec_path))
    else:
        # missing_ok: removing an already-absent entry is success, not an error.
        path.unlink(missing_ok=True)


def notification_text(unit: str, description: str) -> tuple[str, str]:
    """(title, body) for a failure notification; drop the dash when there's no
    description so the body never trails an empty ' — '."""
    body = f"{unit} failed — {description}" if description else f"{unit} failed"
    return "Task Deck", body


class Tray:
    """Owns the QSystemTrayIcon and routes monitor signals to notifications.

    Constructed only when a system tray is available (the caller checks
    QSystemTrayIcon.isSystemTrayAvailable() first). `on_quit` is the app's real
    quit path — the ONLY thing that exits, since closing the window only hides.
    `exec_path` is the installed launcher the autostart entry should run.
    """

    def __init__(
        self,
        window: QWidget,
        monitor: FailureMonitor,
        exec_path: str,
        on_quit: Callable[[], None],
    ) -> None:
        self._window = window
        self._exec_path = exec_path
        self._tray = QSystemTrayIcon(QIcon.fromTheme("taskdeck"))
        self._tray.setToolTip("Task Deck — watching systemd services")

        menu = QMenu()
        open_action = menu.addAction("Open Task Deck")
        open_action.triggered.connect(self._show_window)
        self._autostart_action = menu.addAction("Start at login")
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(is_autostart_enabled())
        self._autostart_action.toggled.connect(self._on_autostart_toggled)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(on_quit)
        self._menu = menu  # keep a ref — a QMenu with no owner is GC'd away
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_activated)
        monitor.unit_failed.connect(self._notify_failure)
        monitor.startup_failures.connect(self._notify_startup)
        self._tray.show()

    def _show_window(self) -> None:
        # showNormal (not show) restores a minimized window; raise_+activate
        # pull it above other windows and give it focus.
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left click toggles the window; right click is the context menu (Qt
        # handles that itself). Trigger == primary activation.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self._window.isVisible():
                self._window.hide()
            else:
                self._show_window()

    def _on_autostart_toggled(self, checked: bool) -> None:
        set_autostart(checked, self._exec_path)

    def _notify_failure(self, unit: str, description: str) -> None:
        title, body = notification_text(unit, description)
        self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Critical, 10_000)

    def _notify_startup(self, items: list[tuple[str, str]]) -> None:
        # One summary for whatever was already failed at login. Cap the names so
        # a pathological failure storm can't produce a wall-of-text balloon.
        names = ", ".join(unit for unit, _ in items[:5])
        more = "" if len(items) <= 5 else f" (+{len(items) - 5} more)"
        plural = "s" if len(items) != 1 else ""
        self._tray.showMessage(
            "Task Deck",
            f"{len(items)} service{plural} currently failed: {names}{more}",
            QSystemTrayIcon.MessageIcon.Warning,
            10_000,
        )
