"""Action layer: builds systemctl argv for user-unit actions.

Pure functions on purpose — the scope guard is testable without Qt, and the
UI's disabled-buttons-in-System-scope is duplicated here as defense in depth:
if a future refactor forgets to disable a button, the action still refuses.
"""
from __future__ import annotations

from taskdeck.systemd_client import SCOPE_USER

# v1 verb whitelist. Deliberately excludes mask/unmask/edit/daemon-reload —
# anything that rewrites unit state on disk is v3 territory with its own
# safeguards. A whitelist (not a blacklist) so new verbs are opt-in.
ALLOWED_VERBS = frozenset({"start", "stop", "enable", "disable"})


class ActionNotAllowed(Exception):
    """Raised when an action violates the v1 policy (wrong scope or verb)."""


def action_argv(verb: str, scope: str, unit: str, systemctl: str = "systemctl") -> list[str]:
    """Build the systemctl command for an action, enforcing v1 policy.

    Raises ActionNotAllowed for system scope (read-only by design — the app
    never escalates) and for verbs outside the whitelist. `systemctl` is
    injectable so tests with a fakebin client never spawn the real binary.
    """
    if scope != SCOPE_USER:
        raise ActionNotAllowed(f"system units are read-only by design (got scope={scope!r})")
    if verb not in ALLOWED_VERBS:
        raise ActionNotAllowed(
            f"verb {verb!r} not allowed in v1 (allowed: {sorted(ALLOWED_VERBS)})"
        )
    # start/stop enqueue systemd JOBS that block until completion — and a
    # Type=oneshot start blocks for the service's WHOLE run (user-reported
    # 2026-06-11: a ~22s fetch job blew the 5s watchdog, which killed the
    # systemctl MESSENGER while the job ran on to success, reporting a false
    # timeout on the app's headline button). --no-block returns once the job
    # is queued; the table's status/result columns are the real feedback.
    # enable/disable are filesystem operations — no job, nothing to unblock.
    job_flags = ["--no-block"] if verb in ("start", "stop") else []
    # "--" stops flag parsing, same defense as fetch_calendar: unit names come
    # from parsed systemd output, and leading-dash names are legal in systemd
    # (the root slice is "-.slice"). Probed 2026-06-11: systemctl accepts it.
    return [systemctl, "--user", verb, *job_flags, "--", unit]
