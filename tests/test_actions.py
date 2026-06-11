"""Action-layer tests: pure functions, no Qt needed."""
import pytest

from taskdeck.actions import ActionNotAllowed, action_argv
from taskdeck.systemd_client import SCOPE_SYSTEM, SCOPE_USER, TimerRow


def test_action_argv_builds_user_command():
    assert action_argv("start", SCOPE_USER, "x.service") == [
        "systemctl", "--user", "start", "--", "x.service",
    ]


@pytest.mark.parametrize("verb", ["start", "stop", "enable", "disable"])
def test_all_four_verbs_build_exact_argv(verb):
    # Pin the FULL argv per verb, not just membership — a regression that
    # special-cases one verb (drops --user or --) must fail loudly.
    assert action_argv(verb, SCOPE_USER, "x.service") == [
        "systemctl", "--user", verb, "--", "x.service",
    ]


def test_system_scope_raises():
    # match= pins the user-facing message — it's the status-bar contract.
    with pytest.raises(ActionNotAllowed, match="read-only"):
        action_argv("start", SCOPE_SYSTEM, "x.service")


def test_unknown_verb_raises():
    with pytest.raises(ActionNotAllowed, match="not allowed"):
        action_argv("mask", SCOPE_USER, "x.service")  # mask is destructive; not in v1


def test_run_now_targets_the_service_not_the_timer():
    from taskdeck.actions import run_now_unit

    row = TimerRow(unit="a.timer", activates="a.service", next_usec=None, last_usec=None)
    assert run_now_unit(row) == "a.service"
