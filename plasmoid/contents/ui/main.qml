/*
 * Task Deck plasmoid — a glanceable, panel/desktop companion to the Task Deck
 * app. It reads systemd USER state itself (via systemctl), so it works with or
 * without the app running.
 *
 * For a reader new to Plasma 6 QML, the load-bearing idioms here (verified against
 * the installed reference plasmoids on this machine):
 *  - The root is a `PlasmoidItem` (from `import org.kde.plasmoid`). `compact-
 *    Representation` and `fullRepresentation` are PLAIN item-valued properties on
 *    it — NOT `Plasmoid.`-prefixed and NOT wrapped in `Component {}` (that was a
 *    Plasma 5 idiom).
 *  - The bare lowercase `plasmoid` context global was REMOVED in Plasma 6. The
 *    popup is toggled via the root PlasmoidItem's own `expanded` property.
 *  - `pragma ComponentBehavior: Bound` lets the inline Repeater delegates below
 *    reference outer ids (like `root`) without unqualified-access warnings.
 *  - Data comes from a Plasma5Support `executable` DataSource: each connected
 *    "source" string is run THROUGH A SHELL and its stdout returned in onNewData.
 *    We connect a command, read it once, then disconnect (one-shot polling driven
 *    by the Timer below).
 */
pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import org.kde.plasmoid
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    // --- State, refreshed from systemctl on a timer ---
    property int failedCount: 0
    property var failedUnits: []   // string[]: names of currently-failed user services
    property var upcomingRuns: []  // [{ unit, next }] sorted soonest-first (next = µs epoch)
    property int refreshSec: 30    // poll cadence

    // The executable engine runs each connectedSource string through `sh`, so env
    // vars expand and we can call systemctl directly. We connect a command, parse
    // its stdout in onNewData, then disconnect so it doesn't re-run on its own —
    // the Timer below drives the cadence.
    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: function (source, data) {
            const stdout = data["stdout"] || ""
            if (source.indexOf("list-units") !== -1) {
                root._parseFailed(stdout)
            } else if (source.indexOf("list-timers") !== -1) {
                root._parseTimers(stdout)
            }
            exec.disconnectSource(source) // one-shot; the Timer re-issues it
        }
        function run(cmd) { connectSource(cmd) }
    }

    function refresh() {
        // -o json: the same machine-readable form the app parses. Field names
        // verified against taskdeck's parsers: list-units -> "unit"; list-timers
        // -> "unit"/"activates"/"next" (µs epoch, 0/absent when not scheduled).
        exec.run("systemctl --user list-units --type=service --state=failed -o json")
        exec.run("systemctl --user list-timers --all -o json --no-pager")
    }

    function _parseFailed(stdout) {
        let units = []
        try {
            const arr = JSON.parse(stdout)
            for (const row of arr) {
                if (row.unit) units.push(row.unit)
            }
        } catch (e) {
            // Empty stream or a systemctl JSON-shape change: leave the list empty
            // (a glance widget must never crash the panel on bad input).
        }
        root.failedUnits = units
        root.failedCount = units.length
    }

    function _parseTimers(stdout) {
        let runs = []
        try {
            const arr = JSON.parse(stdout)
            for (const row of arr) {
                const next = Number(row.next) || 0 // µs epoch; 0 = not scheduled
                if (next > 0) {
                    runs.push({ unit: row.unit, next: next })
                }
            }
            runs.sort(function (a, b) { return a.next - b.next })
            runs = runs.slice(0, 5) // the soonest few — this is a glance, not a list
        } catch (e) {
            // see _parseFailed
        }
        root.upcomingRuns = runs
    }

    function _relTime(usec) {
        // µs epoch -> a short "in 5m" / "in 2h" relative string (LOCAL clock via
        // Date.now()). usec/1000 = ms to compare against Date.now()'s ms.
        const deltaSec = Math.round((usec / 1000 - Date.now()) / 1000)
        if (deltaSec < 0) return "due"
        if (deltaSec < 60) return "in " + deltaSec + "s"
        if (deltaSec < 3600) return "in " + Math.round(deltaSec / 60) + "m"
        if (deltaSec < 86400) return "in " + Math.round(deltaSec / 3600) + "h"
        return "in " + Math.round(deltaSec / 86400) + "d"
    }

    Timer {
        interval: root.refreshSec * 1000
        running: true
        repeat: true
        triggeredOnStart: true // populate immediately on load, not after one cadence
        onTriggered: root.refresh()
    }

    // COMPACT (panel) view: the clock icon plus a red badge with the failure count.
    // A click toggles the popup (the full view). Plain item property, no Component.
    compactRepresentation: MouseArea {
        onClicked: root.expanded = !root.expanded
        Kirigami.Icon {
            anchors.fill: parent
            source: "clock"
        }
        Rectangle {
            visible: root.failedCount > 0
            anchors.top: parent.top
            anchors.right: parent.right
            height: parent.height * 0.5
            width: Math.max(height, badgeLabel.implicitWidth + 4)
            radius: height / 2
            color: Kirigami.Theme.negativeBackgroundColor
            PlasmaComponents.Label {
                id: badgeLabel
                anchors.centerIn: parent
                text: root.failedCount
                color: Kirigami.Theme.negativeTextColor
                font.pixelSize: Math.max(8, parent.height * 0.7)
                font.bold: true
            }
        }
    }

    // FULL (popup) view: a summary line + a Failures section + an Upcoming section.
    fullRepresentation: ColumnLayout {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 16
        Layout.minimumHeight: Kirigami.Units.gridUnit * 12
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading {
            level: 2
            text: "Task Deck"
        }

        PlasmaComponents.Label {
            text: root.failedCount > 0 ? (root.failedCount + " failed") : "All clear"
            color: root.failedCount > 0
                ? Kirigami.Theme.negativeTextColor
                : Kirigami.Theme.positiveTextColor
        }

        Kirigami.Heading {
            level: 4
            text: "Failures"
            visible: root.failedCount > 0
        }
        Repeater {
            model: root.failedUnits
            delegate: PlasmaComponents.Label {
                required property string modelData
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: "✘ " + modelData
            }
        }

        Kirigami.Heading {
            level: 4
            text: "Upcoming"
        }
        Repeater {
            model: root.upcomingRuns
            delegate: PlasmaComponents.Label {
                required property var modelData
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: "⏲ " + modelData.unit + "  —  " + root._relTime(modelData.next)
            }
        }
        PlasmaComponents.Label {
            visible: root.upcomingRuns.length === 0
            opacity: 0.6
            text: "no scheduled runs"
        }

        Item { Layout.fillHeight: true } // soak extra height so content stays top-aligned
    }
}
