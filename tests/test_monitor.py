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


def test_start_polls_immediately_and_arms_timer(qtbot, monkeypatch):
    # start() must poll at once (so the login summary fires immediately) AND
    # arm the periodic timer; stop() disarms it.
    mon, _ = make_monitor(qtbot)
    polls = []
    monkeypatch.setattr(mon, "_poll", lambda: polls.append(1))
    mon.start()
    assert polls == [1]            # immediate first poll
    assert mon._timer.isActive()
    assert mon._timer.interval() == FailureMonitor.POLL_MS
    mon.stop()
    assert not mon._timer.isActive()


def collect_blind(mon):
    blind: list[str] = []
    mon.monitor_blind.connect(lambda msg: blind.append(msg))
    return blind


def test_transient_failure_below_threshold_is_silent(qtbot):
    # A blip or two must NOT cry wolf — only persistent blindness escalates.
    mon, client = make_monitor(qtbot)
    blind = collect_blind(mon)
    for _ in range(FailureMonitor.BLIND_THRESHOLD - 1):
        mon._poll()
        client.fail("failed:user", "transient")
    assert blind == []


def test_persistent_failures_emit_monitor_blind_once(qtbot):
    # P0 fix: a monitor that has silently stopped working is the worst failure
    # for this feature (no notification reads as "all healthy"). After
    # BLIND_THRESHOLD consecutive failed polls it announces once, carrying
    # systemd's own words.
    mon, client = make_monitor(qtbot)
    blind = collect_blind(mon)
    for _ in range(FailureMonitor.BLIND_THRESHOLD):
        mon._poll()
        client.fail("failed:user", "Failed to connect to user bus")
    assert blind == ["Failed to connect to user bus"]
    # Stays blind silently (no re-spam) until it recovers.
    mon._poll()
    client.fail("failed:user", "Failed to connect to user bus")
    assert blind == ["Failed to connect to user bus"]


def test_recovery_resets_the_blind_counter(qtbot):
    mon, client = make_monitor(qtbot)
    blind = collect_blind(mon)
    for _ in range(FailureMonitor.BLIND_THRESHOLD - 1):
        mon._poll()
        client.fail("failed:user", "transient")
    mon._poll()
    client.deliver("failed:user", payload([]))   # a good poll resets the count
    for _ in range(FailureMonitor.BLIND_THRESHOLD - 1):
        mon._poll()
        client.fail("failed:user", "transient")
    assert blind == []  # never reached the threshold consecutively


def test_unparseable_output_counts_as_a_failed_poll_not_a_crash(qtbot):
    # parse_list_units raises ValueError on malformed JSON. Inside a Qt slot
    # that would escape to the excepthook AND leave the baseline un-advanced,
    # re-poisoning every tick. It must instead count as a failed poll, silently
    # until the threshold, then escalate — never crash, never re-raise forever.
    mon, client = make_monitor(qtbot)
    blind = collect_blind(mon)
    failures, _ = collect(mon)
    for _ in range(FailureMonitor.BLIND_THRESHOLD):
        mon._poll()
        client.deliver("failed:user", "not json at all")
    assert failures == []
    assert len(blind) == 1 and "unparseable" in blind[0].lower()
