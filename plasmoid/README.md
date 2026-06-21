# Task Deck plasmoid

A glanceable Plasma 6 panel/desktop widget for systemd **user** timers. Shows a red
failure badge; click for a popup of currently-failed services plus the next few
upcoming runs. Reads `systemctl --user` directly, so it works with or without the
Task Deck app running.

## Install
```
kpackagetool6 --type Plasma/Applet --install /home/dustin/Claude/systemd-task-gui/plasmoid
```
Then add **Task Deck** from the widget browser (right-click the desktop or a panel →
*Add Widgets…*), or run it standalone to test:
```
plasmawindowed space.dustin.taskdeck
```

## Update after edits
```
kpackagetool6 --type Plasma/Applet --upgrade /home/dustin/Claude/systemd-task-gui/plasmoid
```

## Uninstall
```
kpackagetool6 --type Plasma/Applet --remove space.dustin.taskdeck
```

## Status
v0.1 — the QML is qmllint-clean and follows the Plasma 6 idioms, but a plasmoid only
truly proves out by rendering, so this first install is the real test; QML/Plasma
quirks may need a round of iteration. Poll cadence is 30 s (`refreshSec` in
`contents/ui/main.qml`). The compact view is a clock icon + failure badge; the popup
lists failures (✘) and the soonest 5 upcoming runs (⏲) with relative times.
