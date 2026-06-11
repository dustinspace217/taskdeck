"""SystemdClient behavior tests against stub executables (tests/fakebin/).

Each test injects a fake binary path, so no real systemctl runs and the suite
stays hermetic. qtbot.waitSignal spins the Qt event loop until the async
QProcess completes — that's the pytest-qt idiom for testing signal-emitting
async code without sleeps.
"""
from pathlib import Path

from taskdeck.systemd_client import SystemdClient, parse_list_timers

FAKEBIN = Path(__file__).parent / "fakebin"


def make_client(qtbot, binary: str, timeout_ms: int = 5000) -> SystemdClient:
    client = SystemdClient(systemctl=str(FAKEBIN / binary), timeout_ms=timeout_ms)
    return client


def test_success_emits_finished_with_stdout(qtbot):
    client = make_client(qtbot, "fake_ok")
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.request("timers:user", [str(FAKEBIN / "fake_ok"), "ignored"])
    request_id, stdout = blocker.args
    assert request_id == "timers:user"
    assert parse_list_timers(stdout)[0].unit == "a.timer"


def test_failure_emits_failed_with_stderr_text(qtbot):
    client = make_client(qtbot, "fake_fail")
    with qtbot.waitSignal(client.failed, timeout=3000) as blocker:
        client.request("timers:user", [str(FAKEBIN / "fake_fail")])
    request_id, message = blocker.args
    assert request_id == "timers:user"
    assert "No medium found" in message
    assert "exit 1" in message


def test_timeout_kills_and_reports(qtbot):
    client = make_client(qtbot, "fake_hang", timeout_ms=300)
    with qtbot.waitSignal(client.failed, timeout=5000) as blocker:
        client.request("hang:test", [str(FAKEBIN / "fake_hang")])
    _, message = blocker.args
    assert "timed out" in message


def test_single_flight_coalesces_duplicate_request_ids(qtbot):
    # Two requests with the same id while the first is in flight: the second
    # is dropped (refresh storms must not stack subprocesses — bounded
    # resource use, Power of Ten rule 3).
    client = make_client(qtbot, "fake_ok")
    seen = []
    client.finished.connect(lambda rid, out: seen.append(rid))
    client.request("dup:test", [str(FAKEBIN / "fake_ok")])
    accepted_second = client.request("dup:test", [str(FAKEBIN / "fake_ok")])
    assert accepted_second is False
    qtbot.waitUntil(lambda: len(seen) == 1, timeout=3000)
    # After completion the id frees up again:
    assert client.request("dup:test", [str(FAKEBIN / "fake_ok")]) is True
    qtbot.waitUntil(lambda: len(seen) == 2, timeout=3000)


def test_spawn_failure_emits_failed(qtbot):
    # Spawn failure (binary doesn't exist): finished() never fires, so the
    # on_error path must report — and clean up — on its own. Closes DEF-A-02.
    client = make_client(qtbot, "fake_ok")
    with qtbot.waitSignal(client.failed, timeout=3000) as blocker:
        client.request("spawn:fail", [str(FAKEBIN / "does_not_exist")])
    rid, message = blocker.args
    assert rid == "spawn:fail"
    assert "failed to start" in message


def test_timeout_emits_exactly_one_terminal_signal(qtbot):
    # Regression test for the echo guards: after the watchdog reports a
    # timeout, the killed process's death echoes (errorOccurred(Crashed) and
    # finished(CrashExit)) arrive on later event-loop turns and must be
    # swallowed — the regression is a second failed/finished for one request.
    client = make_client(qtbot, "fake_hang", timeout_ms=300)
    outcomes = []
    client.finished.connect(lambda rid, out: outcomes.append(("finished", rid)))
    client.failed.connect(lambda rid, msg: outcomes.append(("failed", rid)))
    client.request("hang:once", [str(FAKEBIN / "fake_hang")])
    qtbot.waitUntil(lambda: len(outcomes) >= 1, timeout=5000)
    qtbot.wait(400)  # let the kill's echoes arrive and be (not) handled
    assert outcomes == [("failed", "hang:once")]
