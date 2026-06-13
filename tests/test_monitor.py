"""FailureMonitor tests — headless, FakeClient, no real systemd.

The monitor owns its own client and connects to its finished signal; these
tests drive _poll() + the client's delivery directly and assert the diff
logic (edge-triggered: notify only on a unit ENTERING failed).
"""
import json

from PySide6.QtCore import QObject, Signal

from taskdeck.monitor import FailureMonitor


class FakeMonitorClient(QObject):
    """Signal-compatible stand-in: records polls; tests deliver responses."""

    finished = Signal(str, str)
    failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    def list_failed_services(self, scope: str) -> bool:
        self.calls.append(("list_failed_services", scope))
        return True

    def deliver(self, request_id: str, payload: str) -> None:
        self.finished.emit(request_id, payload)

    def fail(self, request_id: str, message: str) -> None:
        self.failed.emit(request_id, message)


def payload(units: list[tuple[str, str]]) -> str:
    """Build a list-units --state=failed JSON document from (unit, description)."""
    return json.dumps([
        {"unit": u, "load": "loaded", "active": "failed", "sub": "failed", "description": d}
        for u, d in units
    ])


def make_monitor(qtbot, scope: str = "user"):
    client = FakeMonitorClient()
    mon = FailureMonitor(client, scope=scope)
    return mon, client


def collect(mon):
    failures: list[tuple[str, str]] = []
    startup: list[list] = []
    mon.unit_failed.connect(lambda u, d: failures.append((u, d)))
    mon.startup_failures.connect(lambda items: startup.append(items))
    return failures, startup


def test_new_failure_after_clean_baseline_notifies_once(qtbot):
    mon, client = make_monitor(qtbot)
    failures, startup = collect(mon)
    mon._poll()
    client.deliver("failed:user", payload([]))        # baseline: nothing failed
    assert startup == []                               # clean start → no summary
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    assert failures == [("backup.service", "Nightly backup")]


def test_unit_staying_failed_is_not_re_notified(qtbot):
    mon, client = make_monitor(qtbot)
    failures, _ = collect(mon)
    mon._poll()
    client.deliver("failed:user", payload([]))
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    assert failures == [("backup.service", "Nightly backup")]  # exactly once


def test_recovery_then_refailure_re_notifies(qtbot):
    mon, client = make_monitor(qtbot)
    failures, _ = collect(mon)
    mon._poll()
    client.deliver("failed:user", payload([]))
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    mon._poll()
    client.deliver("failed:user", payload([]))         # recovered (left the set)
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    assert failures == [
        ("backup.service", "Nightly backup"),
        ("backup.service", "Nightly backup"),
    ]


def test_first_poll_with_preexisting_failures_summarizes_once(qtbot):
    mon, client = make_monitor(qtbot)
    failures, startup = collect(mon)
    mon._poll()
    client.deliver("failed:user", payload([
        ("backup.service", "Nightly backup"),
        ("sync.service", "Cloud sync"),
    ]))
    # One summary, sorted, NOT per-unit unit_failed spam at login.
    assert startup == [[("backup.service", "Nightly backup"), ("sync.service", "Cloud sync")]]
    assert failures == []
    # And a unit already failed at baseline is not re-notified next poll.
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    assert failures == []


def test_other_scope_response_is_ignored(qtbot):
    mon, client = make_monitor(qtbot, scope="user")
    failures, _ = collect(mon)
    mon._poll()
    client.deliver("failed:system", payload([("x.service", "X")]))  # wrong scope
    assert failures == []
    assert mon._previous is None  # baseline not even established by a foreign reply


def test_poll_error_keeps_baseline_and_does_not_emit(qtbot):
    mon, client = make_monitor(qtbot)
    failures, _ = collect(mon)
    mon._poll()
    client.deliver("failed:user", payload([("backup.service", "Nightly backup")]))
    # startup summary fired; baseline = {backup}. Now a poll fails:
    mon._poll()
    client.fail("failed:user", "systemctl exit 1: transient")
    assert failures == []
    assert mon._previous == {"backup.service"}  # unchanged
    # Next good poll with a NEW failure still works after the transient error.
    mon._poll()
    client.deliver("failed:user", payload([
        ("backup.service", "Nightly backup"), ("sync.service", "Cloud sync"),
    ]))
    assert failures == [("sync.service", "Cloud sync")]
