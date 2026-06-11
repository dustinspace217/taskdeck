"""Application bootstrap: QApplication + MainWindow + SystemdClient wiring."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from taskdeck.main_window import MainWindow
from taskdeck.systemd_client import SystemdClient


def main() -> int:
    """Build the app and run the Qt event loop; returns the exit code."""
    app = QApplication(sys.argv)
    app.setApplicationName("Task Deck")
    # Associates the Wayland window (app_id) with taskdeck.desktop, so the
    # taskbar/switcher show the launcher's icon and group correctly when
    # started from the Kickoff menu or KRunner.
    app.setDesktopFileName("taskdeck")
    client = SystemdClient()
    window = MainWindow(client)

    def crash_hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        # Backstop for exception classes nobody anticipated: PySide6 routes
        # slot exceptions through sys.excepthook (probed 2026-06-11 —
        # HOOK_FIRED), so this is the last line of the no-silent-failure
        # rule. Without it, an escaped exception prints to a stderr nobody
        # watches in a GUI launch while the table silently freezes at its
        # last good state. stderr still gets the full traceback.
        sys.__excepthook__(exc_type, exc, tb)  # type: ignore[arg-type]
        window.statusBar().showMessage(f"UNEXPECTED ERROR: {exc_type.__name__}: {exc}", 0)

    sys.excepthook = crash_hook
    window.resize(1100, 700)
    window.show()
    # QApplication.exec() returns Any under PySide6's Any-typed stubs (the RPM
    # ships no py.typed marker — see pyproject.toml mypy override). Cast to int
    # so the declared -> int return type satisfies mypy without changing behavior:
    # Qt always returns an integer exit code.
    return int(app.exec())
