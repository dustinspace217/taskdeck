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
    client = SystemdClient()
    window = MainWindow(client)
    window.resize(1100, 700)
    window.show()
    # QApplication.exec() returns Any under PySide6's Any-typed stubs (the RPM
    # ships no py.typed marker — see pyproject.toml mypy override). Cast to int
    # so the declared -> int return type satisfies mypy without changing behavior:
    # Qt always returns an integer exit code.
    return int(app.exec())
