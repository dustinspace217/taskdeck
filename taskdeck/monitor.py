"""Headless background monitor: watches user services for failures.

No widgets here (mirrors systemd_client's discipline) so the monitor is pure,
testable logic. It owns its OWN SystemdClient and request-id namespace
(`failed:{scope}`), so it never races the window's client over shared signal
state — the two poll independently. The window shows the full picture on demand;
the monitor's single job is to notice a service ENTERING the failed state and
announce it, even with the window closed.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from taskdeck.systemd_client import SystemdClient, parse_list_units


class FailureMonitor(QObject):  # type: ignore[misc]
    """Polls `list-units --state=failed` and emits on NEW failures.

    This is a LEVEL poll at ~60s boundaries, not an event stream: it reports
    services that are failed at a sample boundary and were not at the previous
    one. A unit that fails and recovers entirely between two samples is not
    individually reported (a level poll cannot see sub-minute flaps) — the
    window shows the full picture on demand; the monitor's job is unattended
    surfacing of failures that persist to a sample.

    Signals:
      unit_failed(unit, description) — a service NOT failed last sample is
                                       failed now (edge-triggered).
      startup_failures(list)         — emitted once, on the first successful
                                       poll, with [(unit, description), …] for
                                       anything already failed at startup (one
                                       summary, not per-unit login spam).
      monitor_blind(message)         — emitted once when polling has FAILED
                                       BLIND_THRESHOLD times in a row (systemctl
                                       gone, broken bus, unparseable output). A
                                       blind monitor produces no unit_failed, so
                                       silence would falsely read as "healthy";
                                       this makes the blindness loud. Resets on
                                       the next successful poll.
    """

    unit_failed = Signal(str, str)
    startup_failures = Signal(list)
    monitor_blind = Signal(str)

    # 60s: failure detection is not second-critical, and this runs even while
    # the window's 10s refresh is idle/closed. A bounded, intentionally periodic
    # timer (Power of Ten rule 2 — the only "loop" here is the event-driven tick).
    POLL_MS = 60_000
    # Consecutive failed polls before declaring the monitor blind (~3 min). One
    # or two blips stay silent (transient systemctl/bus errors self-heal); a
    # persistent failure means the watcher is broken and must say so.
    BLIND_THRESHOLD = 3

    def __init__(
        self, client: SystemdClient, scope: str = "user", parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._scope = scope
        # None until the first SUCCESSFUL poll establishes the baseline; the
        # None-vs-set distinction is what separates "startup summary" from
        # "new failure". A set of unit names (the failed set last seen).
        self._previous: set[str] | None = None
        # Latest unit -> description, refreshed each poll, so a newly-failed
        # unit can be announced with its human description.
        self._descriptions: dict[str, str] = {}
        # Consecutive poll-failure count; crosses BLIND_THRESHOLD → monitor_blind.
        self._consecutive_failures = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._poll)
        # BOTH outcomes are handled: finished advances the diff; failed feeds the
        # blind counter so a persistently broken poll can't fail invisibly.
        client.finished.connect(self._on_finished)
        client.failed.connect(self._on_failed)

    def start(self) -> None:
        """Begin monitoring: poll immediately, then every POLL_MS."""
        self._poll()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        # Single-flight in the client coalesces an overlapping poll, so a slow
        # systemctl can't stack ticks.
        self._client.list_failed_services(self._scope)

    def _on_finished(self, request_id: str, stdout: str) -> None:
        if request_id != f"failed:{self._scope}":
            return  # the monitor's own client, but guard the id defensively
        try:
            rows = parse_list_units(stdout)
        except ValueError as exc:
            # Malformed output is a broken poll, not a crash: count it toward
            # blindness and DON'T advance the baseline (so a good poll later
            # still diffs correctly). Without this the ValueError would escape
            # the Qt slot to the excepthook and re-poison every tick.
            self._note_poll_failure(f"unparseable list-units output: {exc}")
            return
        self._consecutive_failures = 0  # a good poll → not blind
        current = {r.unit for r in rows}
        self._descriptions = {r.unit: r.description for r in rows}
        if self._previous is None:
            # First successful poll → baseline. Surface anything already failed
            # ONCE as a summary; do not fire unit_failed per unit at login.
            if current:
                self.startup_failures.emit(
                    [(u, self._descriptions[u]) for u in sorted(current)]
                )
            self._previous = current
            return
        # Edge-triggered: only units that appeared in the failed set since the
        # last poll. Sorted for deterministic notification order.
        for unit in sorted(current - self._previous):
            self.unit_failed.emit(unit, self._descriptions.get(unit, ""))
        self._previous = current

    def _on_failed(self, request_id: str, message: str) -> None:
        if request_id != f"failed:{self._scope}":
            return
        self._note_poll_failure(message)

    def _note_poll_failure(self, message: str) -> None:
        # Escalate ONCE at the threshold; staying blind past it is silent (no
        # re-spam). The baseline is left untouched so recovery diffs cleanly.
        self._consecutive_failures += 1
        if self._consecutive_failures == self.BLIND_THRESHOLD:
            self.monitor_blind.emit(message)
