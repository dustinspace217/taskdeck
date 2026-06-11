"""Action-layer tests: pure functions, no Qt needed."""
import pytest

from taskdeck.actions import ActionNotAllowed, action_argv
from taskdeck.systemd_client import SCOPE_SYSTEM, SCOPE_USER

# Expected job flags per verb: start/stop enqueue blocking systemd JOBS and
# need --no-block (a oneshot start otherwise blocks for the service's whole
# run — user-reported 2026-06-11); enable/disable are filesystem ops.
EXPECTED_JOB_FLAGS = {
    "start": ["--no-block"],
    "stop": ["--no-block"],
    "enable": [],
    "disable": [],
}


@pytest.mark.parametrize("verb", sorted(EXPECTED_JOB_FLAGS))
def test_all_four_verbs_build_exact_argv(verb):
    # Pin the FULL argv per verb — a regression that special-cases one verb
    # (drops --user, --no-block, or --) must fail loudly.
    assert action_argv(verb, SCOPE_USER, "x.service") == [
        "systemctl", "--user", verb, *EXPECTED_JOB_FLAGS[verb], "--", "x.service",
    ]


def test_systemctl_path_is_injectable():
    # Tests with a fakebin client must never spawn the real binary — the
    # injected path must reach argv[0] (QA hermeticity hardening).
    argv = action_argv("start", SCOPE_USER, "x.service", systemctl="/fake/systemctl")
    assert argv[0] == "/fake/systemctl"


def test_system_scope_raises():
    # match= pins the user-facing message — it's the status-bar contract.
    with pytest.raises(ActionNotAllowed, match="read-only"):
        action_argv("start", SCOPE_SYSTEM, "x.service")


def test_unknown_verb_raises():
    with pytest.raises(ActionNotAllowed, match="not allowed"):
        action_argv("mask", SCOPE_USER, "x.service")  # mask is destructive; not in v1
