"""Application bootstrap: QApplication + window + (tray + monitor when available).

build() does all the wiring and is separated from main() so tests can exercise
it without entering app.exec(). The tray + background monitor are created ONLY
when a system tray is available — without one there is nowhere to live in the
background, so the app degrades to a plain window (close quits).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from taskdeck.main_window import MainWindow
from taskdeck.monitor import FailureMonitor
from taskdeck.single_instance import SingleInstance
from taskdeck.systemd_client import SystemdClient
from taskdeck.tray import Tray


def _tray_available() -> bool:
    """Indirection over QSystemTrayIcon.isSystemTrayAvailable() so tests can
    force either branch without patching the C++ class."""
    # bool(): the Qt call is Any-typed (no py.typed marker), so narrow it.
    return bool(QSystemTrayIcon.isSystemTrayAvailable())


def _autostart_exec_path() -> str:
    """Launcher path the autostart entry should run. Prefer the installed shim
    on PATH; otherwise point at where install.sh puts it, so enabling autostart
    before running install.sh still resolves once it is run."""
    found = shutil.which("taskdeck")
    return found if found else str(Path.home() / ".local" / "bin" / "taskdeck")


def _install_excepthook(window: MainWindow) -> None:
    def crash_hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        # Backstop for exception classes nobody anticipated: PySide6 routes slot
        # exceptions through sys.excepthook (probed 2026-06-11 — HOOK_FIRED), so
        # this is the last line of the no-silent-failure rule. Without it, an
        # escaped exception prints to a stderr nobody watches in a GUI launch
        # while the table silently freezes. stderr still gets the full traceback.
        sys.__excepthook__(exc_type, exc, tb)  # type: ignore[arg-type]
        window.statusBar().showMessage(f"UNEXPECTED ERROR: {exc_type.__name__}: {exc}", 0)

    sys.excepthook = crash_hook


def build(
    app: QApplication, start_in_tray: bool = False, auto_refresh: bool = True
) -> MainWindow:
    """Wire the app and return the window. With a tray available, also create
    the background monitor + tray and enable close-to-tray. start_in_tray (the
    --tray flag) starts hidden — but only honored when a tray actually exists.
    auto_refresh is forwarded to MainWindow; tests pass False so build() spawns
    no subprocesses."""
    app.setApplicationName("Task Deck")
    # Associates the Wayland window (app_id) with taskdeck.desktop so the
    # taskbar/switcher show the launcher's icon and group correctly.
    app.setDesktopFileName("taskdeck")

    client = SystemdClient()
    window = MainWindow(client, auto_refresh=auto_refresh)
    _install_excepthook(window)
    window.resize(1100, 700)

    if _tray_available():
        # Hiding the window must not exit the app — only tray Quit does.
        app.setQuitOnLastWindowClosed(False)
        window._hide_to_tray = True
        # The monitor gets its OWN client (the client is widget-free precisely
        # so it can run headless), with app as parent so Qt owns its lifetime.
        monitor = FailureMonitor(SystemdClient(), scope="user", parent=app)

        def real_quit() -> None:
            window._quitting = True
            app.quit()

        tray = Tray(window, monitor, _autostart_exec_path(), on_quit=real_quit)
        monitor.start()
        # Keep strong refs for the app's lifetime: the monitor's parent is app,
        # but the Python Tray wrapper holds the notification signal connections
        # and has no other owner — GC'ing it would silently drop them.
        app._taskdeck_refs = (monitor, tray)  # bootstrap keep-alive
        if not start_in_tray:
            window.show()
    else:
        # No tray → nowhere to live in the background; behave as a plain window.
        window.show()
        if start_in_tray:
            # The user asked for hidden/background operation (--tray, e.g. from
            # the autostart entry) but there's no tray — don't silently drop the
            # half they wanted. The window is visible here, so the status bar is
            # the right channel.
            window.statusBar().showMessage(
                "No system tray available — running as a normal window; "
                "background monitoring is off, and closing will quit.",
                0,
            )

    return window


def _raise_window(window: MainWindow) -> None:
    """Bring the window to the foreground — called when a second launch pings the
    primary. showNormal restores from BOTH minimized and hidden-to-tray states."""
    window.showNormal()
    window.raise_()
    window.activateWindow()


def main() -> int:
    """Build the app and run the Qt event loop; returns the exit code."""
    app = QApplication(sys.argv)
    # Single-instance guard FIRST — before build() spawns a window/tray/monitor —
    # so a second launch (e.g. autostart at login + a manual launch) surfaces the
    # running copy and exits instead of stacking a duplicate tray icon.
    instance = SingleInstance()
    if instance.is_secondary():
        instance.ping_primary()
        return 0
    window = build(app, start_in_tray="--tray" in sys.argv[1:])
    instance.set_window_shower(lambda: _raise_window(window))
    # The instance owns the listening QLocalServer — keep it alive for the run.
    app._taskdeck_instance = instance
    # QApplication.exec() returns Any under PySide6's Any-typed stubs (the RPM
    # ships no py.typed marker — see pyproject.toml). Cast to int so the -> int
    # return type holds; Qt always returns an integer exit code.
    return int(app.exec())
