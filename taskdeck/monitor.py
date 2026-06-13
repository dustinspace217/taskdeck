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

    Signals:
      unit_failed(unit, description) — a service that was NOT failed last poll
                                       is failed now (edge-triggered).
      startup_failures(list)         — emitted once, on the first successful
                                       poll, with [(unit, description), …] for
                                       anything already failed at startup. A
                                       single summary instead of per-unit spam
                                       at login; subsequent failures use
                                       unit_failed.
    """

    unit_failed = Signal(str, str)
    startup_failures = Signal(list)

    # 60s: failure detection is not second-critical, and this runs even while
    # the window's 10s refresh is idle/closed. A bounded, intentionally periodic
    # timer (Power of Ten rule 2 — the only "loop" here is the event-driven tick).
    POLL_MS = 60_000

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
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._poll)
        # Only finished is handled: a FAILED poll (transient systemctl error) is
        # deliberately ignored — the baseline is kept and the next tick retries,
        # so a blip never produces a spurious "recovered/failed" notification.
        client.finished.connect(self._on_finished)

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
            return  # another consumer's response on a shared bus / wrong scope
        rows = parse_list_units(stdout)
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
