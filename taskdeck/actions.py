"""Action layer: builds systemctl argv for user-unit actions.

Pure functions on purpose — the scope guard is testable without Qt, and the
UI's disabled-buttons-in-System-scope is duplicated here as defense in depth:
if a future refactor forgets to disable a button, the action still refuses.
"""
from __future__ import annotations

from taskdeck.systemd_client import SCOPE_USER, TimerRow

# v1 verb whitelist. Deliberately excludes mask/unmask/edit/daemon-reload —
# anything that rewrites unit state on disk is v3 territory with its own
# safeguards. A whitelist (not a blacklist) so new verbs are opt-in.
ALLOWED_VERBS = frozenset({"start", "stop", "enable", "disable"})


class ActionNotAllowed(Exception):
    """Raised when an action violates the v1 policy (wrong scope or verb)."""


def action_argv(verb: str, scope: str, unit: str) -> list[str]:
    """Build the systemctl command for an action, enforcing v1 policy.

    Raises ActionNotAllowed for system scope (read-only by design — the app
    never escalates) and for verbs outside the whitelist.
    """
    if scope != SCOPE_USER:
        raise ActionNotAllowed(f"system units are read-only by design (got scope={scope!r})")
    if verb not in ALLOWED_VERBS:
        raise ActionNotAllowed(
            f"verb {verb!r} not allowed in v1 (allowed: {sorted(ALLOWED_VERBS)})"
        )
    # "--" stops flag parsing, same defense as fetch_calendar: unit names come
    # from parsed systemd output, and leading-dash names are legal in systemd
    # (the root slice is "-.slice"). Probed 2026-06-11: systemctl accepts it.
    return ["systemctl", "--user", verb, "--", unit]


def run_now_unit(row: TimerRow) -> str:
    """'Run now' on a timer row starts the SERVICE it activates, not the timer.

    Starting the .timer would merely (re)arm the schedule; starting the
    activated .service is what Task Scheduler users mean by "run now".
    """
    return row.activates
