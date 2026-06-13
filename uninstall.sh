#!/usr/bin/env bash
# Removes everything install.sh created under $HOME. Leaves the repo untouched.
set -euo pipefail

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
SHIM="$BIN_DIR/taskdeck"
ICON="$DATA_DIR/icons/hicolor/scalable/apps/taskdeck.svg"
DESKTOP="$DATA_DIR/applications/taskdeck.desktop"

# rm -f: a missing file is success (idempotent uninstall), not an error.
rm -f "$SHIM" "$ICON" "$DESKTOP"

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$DATA_DIR/applications" 2>/dev/null || true

echo "Task Deck uninstalled (removed shim, icon, and menu entry). Repo left intact."
