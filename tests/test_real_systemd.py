"""Read-only tests against the REAL user systemd instance.

Opt-in (-m realsystemd) because they depend on machine state. They never run
actions — they prove the live data contracts still match the parsers, which
is the early-warning system for systemd version drift on Fedora updates.
"""
import subprocess
import time

import pytest

from taskdeck.calendar_model import parse_projection, parse_run_journal
from taskdeck.systemd_client import (
    SCHEDULE_PROPS,
    parse_list_timers,
    parse_list_units,
    parse_show_schedules,
)

pytestmark = pytest.mark.realsystemd


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


def test_live_cal_projection_base_time_parses():
    # The calendar's gap/projection engine depends on `systemd-analyze calendar
    # --base-time=@<epoch>` projecting an OnCalendar from a PAST anchor and the
    # parser reading the `(in UTC):` lines. A systemd change to that human-format
    # output would silently break projections + gap detection — pin it live.
    base = int(time.time()) - 7 * 86_400  # a week ago
    text = run(["systemd-analyze", "calendar", f"--base-time=@{base}",
                "--iterations=3", "--", "*-*-* 06:00:00"])
    slots = parse_projection(text)
    assert len(slots) == 3, "three daily slots projected from a past anchor"
    assert slots == sorted(slots) and slots[0] > base * 1_000_000


def test_live_cal_journal_outcomes_parse():
    # The single manager-scoped run-outcome query (JOB_RESULT=done|failed) is the
    # calendar's past layer. Confirm the live JSON parses into 'ran' events
    # bucketed by the activated service → timer map. Tolerates an empty window
    # (short retention) — a parse error, not emptiness, is the failure here.
    timers = parse_list_timers(
        run(["systemctl", "--user", "list-timers", "--all", "-o", "json"])
    )
    s2t = {t.activates: t.unit for t in timers if t.activates}
    text = run(["journalctl", "--user", "-o", "json", "--since", "7 days ago",
                "JOB_RESULT=done", "JOB_RESULT=failed", "--no-pager"])
    events = parse_run_journal(text, s2t)  # must not raise on real journal JSON
    assert all(e.kind == "ran" and e.result in ("success", "failure") for e in events)
