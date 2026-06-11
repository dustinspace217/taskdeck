# Task Deck

A native Linux desktop GUI for systemd timers and services — the missing
"Task Scheduler" panel: what's scheduled, when it runs next, when it last ran,
whether it worked, and its logs. Built with PySide6/Qt Widgets on Fedora KDE.

- User instance: full view + run-now / enable / disable / stop
- System instance: read-only view (by design — the app never asks for root)

## Run
    sudo dnf install python3-pyside6
    python3 -m taskdeck

## Develop
    sudo dnf install python3-pytest-qt ruff  # mypy optional
    python3 -m pytest          # hermetic suite (offscreen Qt)
    ruff check . && mypy taskdeck
