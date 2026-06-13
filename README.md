# Task Deck

[![ci](https://github.com/dustinspace217/taskdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/dustinspace217/taskdeck/actions/workflows/ci.yml)

A native Linux desktop GUI for systemd timers and services — the missing
"Task Scheduler" panel: what's scheduled, how often, when it runs next, when it
last ran, whether it worked, and its logs. Built with PySide6/Qt Widgets.

![Task Deck showing user timers with a cadence column, run states, and last results](docs/screenshot.png)

- **At a glance:** status, cadence (daily / weekly / 2×/day / boot+12h …), next
  run, last run, and last result for every timer and service.
- **User instance:** full view plus run-now / enable / disable / stop.
- **System instance:** read-only view — by design, the app never asks for root.
- **Per-unit detail:** journal log, `systemctl show` properties, effective
  schedule (drop-ins included), and the unit file.

No web stack, no daemon, no polkit — just `systemctl`/`journalctl` shelled out
asynchronously and a Qt table.

## Run

    sudo dnf install python3-pyside6
    python3 -m taskdeck

## Install (Start menu, KRunner search, taskbar pinning)

    ./install.sh

A user-local install (no root): it drops a launcher at `~/.local/bin/taskdeck`,
the icon under the hicolor theme, and a `.desktop` entry in
`~/.local/share/applications/`. The app still runs from this repo in place — the
launcher just records where the repo is, so the menu entry keeps working even if
you move the checkout (re-run `./install.sh` after a move). `./uninstall.sh`
removes all three and leaves the repo untouched.

## Develop

    sudo dnf install python3-pytest-qt ruff python3-mypy
    python3 -m pytest                  # hermetic suite (offscreen Qt, no real systemd)
    python3 -m pytest -m realsystemd   # read-only checks against the live user instance
    ruff check . && mypy taskdeck

`python3 tools/screenshot.py` regenerates the image above from the real widget.
