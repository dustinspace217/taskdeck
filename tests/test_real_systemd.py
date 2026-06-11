"""Read-only tests against the REAL user systemd instance.

Opt-in (-m realsystemd) because they depend on machine state. They never run
actions — they prove the live data contracts still match the parsers, which
is the early-warning system for systemd version drift on Fedora updates.
"""
import subprocess

import pytest

from taskdeck.systemd_client import parse_list_timers, parse_list_units

pytestmark = pytest.mark.realsystemd


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True).stdout


def test_live_list_timers_parses():
    rows = parse_list_timers(run(["systemctl", "--user", "list-timers", "--all", "-o", "json"]))
    assert rows, "this machine has user timers; zero rows = contract drift"


def test_live_list_units_parses():
    rows = parse_list_units(
        run(["systemctl", "--user", "list-units", "--type=service", "-o", "json"])
    )
    assert rows
