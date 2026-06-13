"""Read-only tests against the REAL user systemd instance.

Opt-in (-m realsystemd) because they depend on machine state. They never run
actions — they prove the live data contracts still match the parsers, which
is the early-warning system for systemd version drift on Fedora updates.
"""
import subprocess

import pytest

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
