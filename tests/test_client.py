"""SystemdClient behavior tests against stub executables (tests/fakebin/).

Each test injects a fake binary path, so no real systemctl runs and the suite
stays hermetic. qtbot.waitSignal spins the Qt event loop until the async
QProcess completes — that's the pytest-qt idiom for testing signal-emitting
async code without sleeps.
"""
from pathlib import Path

from taskdeck.systemd_client import (
    CALPROJ_CONCURRENCY_CAP,
    SystemdClient,
    _at_epoch,
    parse_list_timers,
)

FAKEBIN = Path(__file__).parent / "fakebin"

# A realistic MICROSECOND epoch as the host actually passes it (16-17 digits) —
# the bug report's value. Its //1_000_000 floor is the SECONDS form the two
# subprocess tools accept. Using a true µs magnitude here (not a seconds-sized
# stand-in) is what makes the argv tests NON-VACUOUS: the pre-fix code
# interpolated this raw, producing @1781914546656007 which systemd rejects.
CAL_US = 1781914546656007
CAL_SEC = "1781914546"  # CAL_US // 1_000_000, the form that parses (rc 0)


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


def test_at_epoch_floors_microseconds_to_seconds():
    # The µs→seconds boundary helper in isolation. systemd-analyze/journalctl
    # parse `@N` as SECONDS, but the calendar model is microseconds throughout;
    # _at_epoch is the one place that converts. Floor (// not round) so the
    # window's left edge is included. A regression that dropped the //1_000_000
    # would surface here AND in the three argv tests below.
    assert _at_epoch(CAL_US) == "@" + CAL_SEC
    assert _at_epoch(0) == "@0"
    assert _at_epoch(1_999_999) == "@1"  # sub-second tail floored away, not rounded


def test_fetch_cal_projection_id_and_argv(qtbot):
    # Calendar view's future-run source: an exact-instant projection from a past
    # base time. Pins the "calproj:{scope}:{unit}" id (NEVER "calendar:" — that
    # kind is taken by the Schedule-tab elapse preview, and _dispatch_finished
    # routes only on the kind before the first ':') AND the exact argv, including
    # the "--" flag-stop before the expression. fake_echo_argv dumps the argv.
    #
    # NON-VACUOUS guard for the µs→seconds bug: the host passes a MICROSECOND
    # win_start (CAL_US, 16 digits); --base-time must carry the SECONDS floor
    # (@CAL_SEC), NOT the raw µs value. Reverting _at_epoch's //1_000_000 makes
    # this assert @1781914546656007 and fail — which is exactly what systemd
    # rejects in production ("Failed to parse --base-time= parameter").
    client = SystemdClient(analyze=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_projection("user", "a.timer", "*-*-* 06:00:00", CAL_US, 8)
    rid, stdout = blocker.args
    assert rid == "calproj:user:a.timer"
    assert stdout.splitlines() == [
        "calendar", f"--base-time=@{CAL_SEC}", "--iterations=8", "--", "*-*-* 06:00:00",
    ]


def test_fetch_cal_journal_id_and_argv(qtbot):
    # Calendar view's past-runs source: ONE journalctl query covering all units
    # in the window, filtered to completion outcomes. Pins the "caljournal:{scope}"
    # id and that both JOB_RESULT filters + the since/until window land in argv.
    #
    # NON-VACUOUS guard: since/until arrive in MICROSECONDS and must land as the
    # SECONDS floor (@CAL_SEC), not @<µs> — the raw µs value is what journalctl
    # rejected with "Failed to parse timestamp" in production. until uses a
    # DIFFERENT µs value (CAL_US + 1 day in µs) so a swapped since/until or a
    # dropped conversion on either bound is caught distinctly.
    until_us = CAL_US + 86_400 * 1_000_000
    until_sec = until_us // 1_000_000
    client = SystemdClient(journalctl=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_journal("user", CAL_US, until_us)
    rid, stdout = blocker.args
    assert rid == "caljournal:user"
    argv = stdout.splitlines()
    assert "JOB_RESULT=done" in argv and "JOB_RESULT=failed" in argv
    assert f"--since=@{CAL_SEC}" in argv and f"--until=@{until_sec}" in argv
    # And explicitly: the raw µs value must not appear EMBEDDED in any arg (the
    # bug put it inside `--since=@<µs>`, so substring-check the whole line — list
    # membership would miss it).
    assert str(CAL_US) not in " ".join(argv)


def test_fetch_cal_coverage_id_and_argv(qtbot):
    # Calendar view's journal-coverage floor probe (F1): the UNFILTERED oldest-
    # entry query that distinguishes "no runs" from "journal doesn't reach back".
    # Pins the "calcover:{scope}" id and — the same µs→seconds boundary — that
    # --since carries the SECONDS floor, never the raw µs value. This site had
    # the identical bug; without this test it was the one fetch with no argv
    # coverage at all.
    client = SystemdClient(journalctl=str(FAKEBIN / "fake_echo_argv"))
    with qtbot.waitSignal(client.finished, timeout=3000) as blocker:
        client.fetch_cal_coverage("user", CAL_US)
    rid, stdout = blocker.args
    assert rid == "calcover:user"
    argv = stdout.splitlines()
    assert f"--since=@{CAL_SEC}" in argv
    # Raw µs must not appear embedded in `--since=@<µs>` (substring, not list).
    assert str(CAL_US) not in " ".join(argv)


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


# -- calproj concurrency pool (DEF-CAL-02) ------------------------------------
# SAFETY-CRITICAL test design: every test below MOCKS _spawn, so NO real process
# is ever created. The DEF-CAL-02 incident (2026-06-21) was a test that fired real
# blocking processes and leaked them, OOM-crashing the machine. Mocking the spawn
# exercises the pool's ACCOUNTING (cap / queue / drain / single-flight) with zero
# subprocesses — the failure mode is impossible by construction.


def test_calproj_cap_admits_cap_and_queues_rest(qtbot):
    client = SystemdClient()
    spawned: list = []
    client._spawn = lambda rid, argv, t: spawned.append(rid)  # type: ignore[method-assign]
    cap = CALPROJ_CONCURRENCY_CAP
    for i in range(cap + 5):
        assert client.request(f"calproj:user:t{i}#0@1", ["x"]) is True
    assert len(client._calproj_active) == cap        # only cap-many run
    assert len(client._calproj_queue) == 5           # the rest wait
    assert spawned == [f"calproj:user:t{i}#0@1" for i in range(cap)]  # only cap spawned


def test_calproj_pump_drains_queue_as_slots_free(qtbot):
    client = SystemdClient()
    spawned: list = []
    client._spawn = lambda rid, argv, t: spawned.append(rid)  # type: ignore[method-assign]
    cap = CALPROJ_CONCURRENCY_CAP
    for i in range(cap + 3):
        client.request(f"calproj:user:t{i}#0@1", ["x"])
    freed = sorted(client._calproj_active)[0]        # simulate one finishing
    client._calproj_active.discard(freed)
    client._pump_calproj()
    assert len(client._calproj_active) == cap        # a queued one took the slot
    assert len(client._calproj_queue) == 2           # one drained from the 3
    assert len(spawned) == cap + 1                   # the pumped one spawned too


def test_calproj_single_flight_spans_pool(qtbot):
    client = SystemdClient()
    client._spawn = lambda rid, argv, t: None  # type: ignore[method-assign]
    cap = CALPROJ_CONCURRENCY_CAP
    rid = "calproj:user:t0#0@1"
    assert client.request(rid, ["x"]) is True        # admitted (now running)
    assert client.request(rid, ["x"]) is False       # duplicate of a RUNNING one
    for i in range(1, cap):                          # fill to the cap
        client.request(f"calproj:user:t{i}#0@1", ["x"])
    qrid = "calproj:user:tq#0@1"
    assert client.request(qrid, ["x"]) is True        # queued (cap reached)
    assert client.request(qrid, ["x"]) is False       # duplicate of a QUEUED one


def test_calproj_queue_max_refuses_loudly(qtbot, monkeypatch):
    import taskdeck.systemd_client as sc
    monkeypatch.setattr(sc, "CALPROJ_QUEUE_MAX", 3)
    client = sc.SystemdClient()
    client._spawn = lambda rid, argv, t: None  # type: ignore[method-assign]
    cap = sc.CALPROJ_CONCURRENCY_CAP
    for i in range(cap + 3):                          # cap running + 3 queued: all OK
        assert client.request(f"calproj:user:t{i}#0@1", ["x"]) is True
    assert client.request("calproj:user:over#0@1", ["x"]) is False  # queue full → refused
    assert len(client._calproj_queue) == 3            # not ballooned past the bound


def test_non_calproj_requests_are_not_pooled(qtbot):
    client = SystemdClient()
    spawned: list = []
    client._spawn = lambda rid, argv, t: spawned.append(rid)  # type: ignore[method-assign]
    for i in range(CALPROJ_CONCURRENCY_CAP + 10):     # far more than the cap
        assert client.request(f"caljournal:user@{i}", ["x"]) is True
    assert client._calproj_active == set()            # nothing pooled
    assert client._calproj_queue == []
    assert len(spawned) == CALPROJ_CONCURRENCY_CAP + 10  # all spawned immediately
