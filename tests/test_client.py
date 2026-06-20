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


def test_fetch_schedules_emits_expected_request_id(qtbot):
    # PIN-3 (QA 2026-06-12): the window hand-types "schedules:{scope}" into its
    # FakeClient and gates responses on an exact-id match; this pins the REAL
    # client to the same string. A drift here would pass every window test
    # while freezing the production Cadence/Schedule path at "loading…".
    client = SystemdClient(systemctl=str(FAKEBIN / "fake_ok"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_schedules("user", ["a.timer"])
    assert blocker.args[0] == "schedules:user"


def test_fetch_tab_schedule_emits_expected_request_id(qtbot):
    # The schedtab id carries the unit name (the window's freshness gate keys
    # on the full id) — pin its exact shape too.
    client = SystemdClient(systemctl=str(FAKEBIN / "fake_ok"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_tab_schedule("user", "a.timer")
    assert blocker.args[0] == "schedtab:user:a.timer"


def test_fetch_calendar_emits_expected_id_and_flag_stop_argv(qtbot):
    # Pins both the "calendar:{expr}" id AND the "--" flag-stop: a normalized
    # expression is benign, but the guard must survive a future caller passing
    # unnormalized text starting with "-" (it must reach systemd-analyze as an
    # operand, never a flag). fake_echo_argv dumps the argv it received.
    client = SystemdClient(analyze=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_calendar("*-*-* 03:50:00")
    request_id, stdout = blocker.args
    assert request_id == "calendar:*-*-* 03:50:00"
    argv_lines = stdout.splitlines()
    assert argv_lines == ["calendar", "--iterations=5", "--", "*-*-* 03:50:00"]


def test_fetch_cal_projection_id_and_argv(qtbot):
    # Calendar view's future-run source: an exact-instant projection from a past
    # base time. Pins the "calproj:{scope}:{unit}" id (NEVER "calendar:" — that
    # kind is taken by the Schedule-tab elapse preview, and _dispatch_finished
    # routes only on the kind before the first ':') AND the exact argv, including
    # the "--" flag-stop before the expression. fake_echo_argv dumps the argv.
    client = SystemdClient(analyze=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_projection("user", "a.timer", "*-*-* 06:00:00", 1781000000, 8)
    rid, stdout = blocker.args
    assert rid == "calproj:user:a.timer"
    assert stdout.splitlines() == [
        "calendar", "--base-time=@1781000000", "--iterations=8", "--", "*-*-* 06:00:00",
    ]


def test_fetch_cal_journal_id_and_argv(qtbot):
    # Calendar view's past-runs source: ONE journalctl query covering all units
    # in the window, filtered to completion outcomes. Pins the "caljournal:{scope}"
    # id and that both JOB_RESULT filters + the since/until window land in argv.
    client = SystemdClient(journalctl=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_journal("user", 1781000000, 1781086400)
    rid, stdout = blocker.args
    assert rid == "caljournal:user"
    argv = stdout.splitlines()
    assert "JOB_RESULT=done" in argv and "JOB_RESULT=failed" in argv
    assert "--since=@1781000000" in argv and "--until=@1781086400" in argv


def test_list_failed_services_emits_expected_id_and_argv(qtbot):
    # The monitor's poll (Task 2). --state=failed narrows server-side so the
    # diff sees only failures. Pins both the request id and the exact argv.
    client = SystemdClient(systemctl=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.list_failed_services("user")
    request_id, stdout = blocker.args
    assert request_id == "failed:user"
    assert stdout.splitlines() == [
        "--user", "list-units", "--type=service", "--state=failed", "--all", "-o", "json",
    ]


def test_finished_processes_are_swept_not_leaked(qtbot):
    # DEF-T4-01 regression (the leak fix): finished QProcess objects must be
    # FREED, not accumulated for the client's lifetime. Each is parked on
    # completion and deleted at the top of the NEXT request — deferred OUT of
    # the finished emission, because deleting in-slot (or via the
    # finished.connect(deleteLater) idiom) segfaults PySide6 6.11.1/py3.14.
    #
    # Shiboken.isValid() reports whether the C++ object behind a wrapper is
    # alive, so it proves actual FREEING — not just that the bookkeeping list
    # was cleared. (Asserting only len(_finished) would be vacuous: .clear()
    # alone satisfies it even if the deleteLater were gutted — QA finding.)
    from shiboken6 import Shiboken

    client = make_client(qtbot, "fake_ok")
    seen = []
    client.finished.connect(lambda rid, out: seen.append(rid))

    client.request("free:1", [str(FAKEBIN / "fake_ok")])
    qtbot.waitUntil(lambda: len(seen) == 1, timeout=3000)
    assert len(client._finished) == 1, "a finished proc is parked for deferred deletion"
    parked = client._finished[0][0]
    assert Shiboken.isValid(parked), "parked, still alive — safe to read in-slot"

    # The next request sweeps the parked proc; drain DeferredDelete.
    client.request("free:2", [str(FAKEBIN / "fake_ok")])
    qtbot.waitUntil(lambda: len(seen) == 2, timeout=3000)
    qtbot.wait(50)
    assert not Shiboken.isValid(parked), "swept proc is actually deleted, not leaked"
    assert len(client._finished) <= 1  # bounded backlog, not lifetime accumulation
    assert client._inflight == {}


def test_flush_finished_frees_parked_procs_on_shutdown(qtbot):
    # The no-next-request path (closeEvent quit branch): a proc that finished
    # after the last request() must still be freed via flush_finished(), not
    # left to linger until GC (QA adversarial finding).
    from shiboken6 import Shiboken

    client = make_client(qtbot, "fake_ok")
    done = []
    client.finished.connect(lambda rid, out: done.append(rid))
    client.request("flush:1", [str(FAKEBIN / "fake_ok")])
    qtbot.waitUntil(lambda: len(done) == 1, timeout=3000)
    parked = client._finished[0][0]
    client.flush_finished()          # shutdown sweep — no further request()
    qtbot.wait(50)
    assert not Shiboken.isValid(parked)
    assert client._finished == []


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
