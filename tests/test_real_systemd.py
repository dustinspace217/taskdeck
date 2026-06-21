"""Read-only tests against the REAL user systemd instance.

Opt-in (-m realsystemd) because they depend on machine state. They never run
actions — they prove the live data contracts still match the parsers, which
is the early-warning system for systemd version drift on Fedora updates.
"""
import subprocess
import time

import pytest

from taskdeck.calendar_model import (
    FWD_PROJECTION_ITERATIONS,
    parse_projection,
    parse_run_journal,
)
from taskdeck.systemd_client import (
    SCHEDULE_PROPS,
    cal_coverage_argv,
    cal_journal_argv,
    cal_projection_argv,
    parse_journal,
    parse_list_timers,
    parse_list_units,
    parse_show_schedules,
)

pytestmark = pytest.mark.realsystemd


def first_line(argv):
    # Run argv and read only the FIRST stdout line, then terminate — exactly how
    # the production coverage handler consumes fetch_cal_coverage's stream (it
    # reads line 1 = the oldest entry and ignores the rest). subprocess.run()
    # would instead DRAIN the whole stream: the unfiltered week-wide query
    # returns millions of lines (~32s on this machine), which production never
    # waits for and which would bust any sane test timeout. So this both runs the
    # REAL argv (catching @-epoch/flag drift) AND matches real consumption.
    # Returns the first line ("" if the stream was empty). Raises on spawn error.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return line


def run(argv):
    # check=False + explicit assert so a failure shows systemctl's actual
    # stderr (CalledProcessError's default message omits it).
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    assert proc.returncode == 0, f"{argv} failed: {proc.stderr}"
    return proc.stdout


def test_live_list_timers_parses():
    rows = parse_list_timers(run(["systemctl", "--user", "list-timers", "--all", "-o", "json"]))
    assert rows, "this machine has user timers; zero rows = contract drift"


def test_live_list_units_parses():
    rows = parse_list_units(
        run(["systemctl", "--user", "list-units", "--type=service", "-o", "json"])
    )
    assert rows, "a live user session always has loaded services; zero = contract drift"


def test_live_show_schedules_parses():
    # parse_show_schedules is the ONLY parser matching systemd's human-format
    # property text (the `{ K=V ; next_elapse=… }` shape) rather than JSON, and
    # it is fail-loud on shape drift — so it is the most fragile to a Fedora
    # systemd update. This drives the real `show` output for every user timer
    # through it; a format change surfaces here on update day instead of as a
    # ValueError toast every 10s in front of the user. (QA 2026-06-12 LIVE-1.)
    timers = parse_list_timers(
        run(["systemctl", "--user", "list-timers", "--all", "-o", "json"])
    )
    units = sorted({r.unit for r in timers})
    assert units, "this machine has user timers; zero = contract drift"
    text = run(["systemctl", "--user", "show", *units, "-p", SCHEDULE_PROPS])
    schedules = parse_show_schedules(text, units)  # raises on any shape drift
    assert set(schedules) == set(units), "every requested timer must get a block"


# A realistic MICROSECOND base — a week ago, in µs, exactly as the host fans
# win_start into the calendar fetches (the µs model). The whole point of these
# rewrites: the OLD versions hand-built argv with `--since "7 days ago"` (human
# format) and a manual `@<seconds>` base, so they NEVER exercised the @-epoch the
# client actually emits — which is how the µs→seconds boundary bug shipped
# invisible to every test (Dustin caught it on live retest). These now build argv
# through the SAME pure functions the production fetches use, so a units/format
# drift at the subprocess boundary fails here on update day.
WEEK_AGO_USEC = (int(time.time()) - 7 * 86_400) * 1_000_000


def test_live_cal_projection_base_time_parses():
    # Run the EXACT argv fetch_cal_projection builds (cal_projection_argv with a
    # µs base), not a hand-typed approximation. Proves the production
    # `systemd-analyze calendar --base-time=@<seconds>` argv exits 0 AND that the
    # projection parser reads its human-format `(in UTC):` lines. A regression
    # that passed µs straight through would make systemd reject the arg → run()'s
    # rc!=0 assert fires here instead of blanking the calendar in front of Dustin.
    argv = cal_projection_argv("systemd-analyze", "*-*-* 06:00:00", WEEK_AGO_USEC, 3)
    slots = parse_projection(run(argv))
    assert len(slots) == 3, "three daily slots projected from a past anchor"
    assert slots == sorted(slots) and slots[0] > WEEK_AGO_USEC


def test_live_cal_forward_projection_from_now_yields_future_slots():
    # B (forward projection): the EXACT argv the host fires for the now-based
    # forward projection — cal_projection_argv with base_epoch == NOW (µs) and the
    # small FWD_PROJECTION_ITERATIONS budget. This is what makes "upcoming" show
    # for a fast cadence whose win_start projection burned its cap on past slots.
    # Proves (a) the now-base @<seconds> is accepted (raw µs would make systemd
    # reject --base-time → run()'s rc!=0 fires here, the exact production blank-out)
    # and (b) every projected slot is strictly in the FUTURE — which is why
    # _finalize_calendar emits them as `projected` (the s > now branch). Uses a
    # minutely expression so the cap-vs-now distinction is the real one.
    now_usec = int(time.time()) * 1_000_000
    argv = cal_projection_argv(
        "systemd-analyze", "*:0/1", now_usec, FWD_PROJECTION_ITERATIONS
    )
    slots = parse_projection(run(argv))
    assert len(slots) == FWD_PROJECTION_ITERATIONS, (
        "the forward budget projects that many upcoming minutely slots"
    )
    assert all(s > now_usec for s in slots), "every forward slot is in the future"
    assert slots == sorted(slots)


def test_live_cal_journal_outcomes_parse():
    # Run the EXACT argv fetch_cal_journal builds (cal_journal_argv with µs
    # since/until), not `--since "7 days ago"`. Confirms the production journalctl
    # query exits 0 and its JSON parses into 'ran' events bucketed by the
    # activated-service → timer map. Tolerates an empty window (short retention) —
    # a parse error or a non-zero exit, not emptiness, is the failure here.
    timers = parse_list_timers(
        run(["systemctl", "--user", "list-timers", "--all", "-o", "json"])
    )
    s2t = {t.activates: t.unit for t in timers if t.activates}
    now_usec = int(time.time()) * 1_000_000
    argv = cal_journal_argv("journalctl", "user", WEEK_AGO_USEC, now_usec)
    events = parse_run_journal(run(argv), s2t)  # rc 0 (run asserts) + must not raise
    assert all(e.kind == "ran" and e.result in ("success", "failure") for e in events)


def test_live_cal_coverage_probe_parses():
    # The coverage-floor probe (F1) had the SAME µs→seconds bug and previously had
    # NO live test at all. Run the EXACT argv fetch_cal_coverage builds — proving
    # the @-epoch is accepted (a raw-µs since is rejected with rc 1, blanking the
    # coverage floor). Consume it like production does: read only the FIRST line
    # (the oldest entry) via first_line(), since the unfiltered week-wide stream
    # is millions of lines the handler never drains. An empty window (the µs base
    # predates retention) is allowed; a populated one must parse — that first line
    # carries the __REALTIME_TIMESTAMP the handler reads.
    line = first_line(cal_coverage_argv("journalctl", "user", WEEK_AGO_USEC))
    if line.strip():
        entries = parse_journal(line)  # must not raise on a real journal line
        assert entries and entries[0].ts_usec is not None, (
            "the first coverage line must carry a usable __REALTIME_TIMESTAMP"
        )
