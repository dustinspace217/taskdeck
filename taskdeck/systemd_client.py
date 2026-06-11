"""Data layer: systemd output types, parsers, and the async client.

This module is the ONLY place subprocesses are spawned, and it never imports
widgets — the planned v2 tray notifier reuses it headless. Parsers are pure
functions (text in → dataclasses out) so they're testable without Qt at all.

Data contracts were probed live on systemd 258 / Fedora 44 (2026-06-10); see
docs/superpowers/plans/2026-06-10-taskdeck-v1.md "Verified data contracts".
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


@dataclass(frozen=True)
class TimerRow:
    """One row of `systemctl list-timers -o json`.

    next_usec/last_usec are MICROSECOND epochs exactly as systemd emits them
    (or None — a disabled timer has no next elapse). Conversion to datetimes
    happens at render time in models.py; the client stays a faithful
    transcription so fixtures and live data are interchangeable.
    """

    unit: str
    activates: str
    next_usec: int | None
    last_usec: int | None


@dataclass(frozen=True)
class ServiceRow:
    """One row of `systemctl list-units --type=service -o json`."""

    unit: str
    load: str
    active: str
    sub: str
    description: str


@dataclass(frozen=True)
class LastResult:
    """Last-run outcome of a service, from `systemctl show -p Result,ExecMainStatus`.

    result is systemd's Result property ("success", "exit-code", "timeout", …);
    exec_status is ExecMainStatus as an int. NOTE: a loaded-but-never-run
    service still reports ExecMainStatus=0 — None here means the KEY WAS
    ABSENT from the unit's block (non-service unit, truncated output), not
    "never ran".
    """

    result: str
    exec_status: int | None


@dataclass(frozen=True)
class LogEntry:
    """One journal line from `journalctl -o json`."""

    ts_usec: int | None
    identifier: str
    message: str
    priority: int  # syslog level 0-7; 7 (debug) when the field is absent


def parse_list_timers(text: str) -> list[TimerRow]:
    """Parse `systemctl list-timers --all -o json` output.

    Raises ValueError (from json) on malformed input — callers surface that as
    an error state, never an empty table (no-silent-failure rule).
    Records missing required fields (unit/activates) raise KeyError — same policy:
    surface, never swallow.
    """
    raw = json.loads(text)
    return [
        TimerRow(
            unit=item["unit"],
            activates=item["activates"],
            next_usec=item.get("next"),
            last_usec=item.get("last"),
        )
        for item in raw
    ]


def parse_list_units(text: str) -> list[ServiceRow]:
    """Parse `systemctl list-units --type=service -o json` output.

    Records missing the unit field raise KeyError — surfaced, never swallowed.
    """
    raw = json.loads(text)
    return [
        ServiceRow(
            unit=item["unit"],
            load=item.get("load", ""),
            active=item.get("active", ""),
            sub=item.get("sub", ""),
            description=item.get("description", ""),
        )
        for item in raw
    ]


def parse_show_results(text: str, units: list[str]) -> dict[str, LastResult]:
    """Parse batched `systemctl show U1 U2… -p Result,ExecMainStatus,…` output.

    systemctl prints one Key=Value block per requested unit, in ARGUMENT
    ORDER, separated by blank lines. A unit that has NONE of the requested
    properties (a .target, a dangling `activates` name) contributes an EMPTY
    block — probed 2026-06-10: `show basic.target X.service -p Result,…`
    emits a leading blank line. A naive strip()+split("\\n\\n") silently
    shifts every later block onto the wrong unit (a failed unit could render
    as success on the wrong row), so this walks lines and advances the unit
    index at each blank line, preserving alignment under empty blocks.

    Units with empty blocks (or beyond a truncated output) get no entry —
    absence means "unknown", which the UI renders as "—", never as success.
    Raises ValueError if systemctl emits MORE non-empty blocks than units
    were given: that means the argv and this parser disagree on the contract.
    """
    results: dict[str, LastResult] = {}
    idx = 0
    props: dict[str, str] = {}

    def flush_block() -> None:
        """Record the accumulated block for units[idx]; empty blocks no-op."""
        nonlocal props
        if not props:
            return
        if idx >= len(units):
            raise ValueError(
                f"systemctl show returned more blocks than the {len(units)} requested units"
            )
        status_text = props.get("ExecMainStatus", "")
        # EAFP int parsing: .isdigit() guards accept strings int() rejects
        # ("--1", unicode digits); try/except is both shorter and correct.
        try:
            exec_status: int | None = int(status_text)
        except ValueError:
            exec_status = None
        results[units[idx]] = LastResult(
            result=props.get("Result", ""), exec_status=exec_status
        )
        props = {}

    for line in text.splitlines():
        if not line.strip():
            flush_block()
            idx += 1
            continue
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    flush_block()
    return results


def parse_journal(text: str) -> list[LogEntry]:
    """Parse `journalctl -o json` output (one JSON object per line).

    Tolerates individual bad lines (journal corruption happens — this machine
    has the "corrupted or uncleanly shut down" markers to prove it) but raises
    ValueError if NO line parses: an entirely unparseable journal means the
    call itself was wrong, and returning [] would silently hide that.
    """
    entries: list[LogEntry] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        message = obj.get("MESSAGE", "")
        if not isinstance(message, str):
            # journald stores non-UTF8 payloads as byte arrays; render their
            # repr rather than dropping the line (visibility over beauty).
            message = str(message)
        ts_text = str(obj.get("__REALTIME_TIMESTAMP", ""))
        try:
            ts_usec: int | None = int(ts_text)
        except ValueError:
            ts_usec = None
        priority_text = str(obj.get("PRIORITY", "7"))
        try:
            priority = int(priority_text)
        except ValueError:
            priority = 7
        entries.append(
            LogEntry(
                ts_usec=ts_usec,
                identifier=str(obj.get("SYSLOG_IDENTIFIER", "")),
                message=message,
                priority=priority,
            )
        )
    if bad and not entries:
        raise ValueError(f"journal output entirely unparseable ({bad} bad lines)")
    return entries


# Scope constants used across modules; "user" gets --user injected, "system"
# doesn't. Strings (not an Enum) because they appear verbatim in request ids
# and error messages — YAGNI until a third scope exists.
SCOPE_USER = "user"
SCOPE_SYSTEM = "system"

# Curated Details-tab properties (spec): enough to answer "what is this unit
# and what is it doing" without the full ~200-key `show` dump.
DETAIL_PROPS = (
    "Description,FragmentPath,ActiveState,SubState,MainPID,"
    "MemoryCurrent,MemoryPeak,CPUUsageNSec,TriggeredBy,Triggers"
)
RESULT_PROPS = "Result,ExecMainStatus,ExecMainExitTimestamp"


class SystemdClient(QObject):  # type: ignore[misc]
    """Runs systemctl/journalctl asynchronously and emits results as signals.

    Why QProcess and not subprocess: QProcess integrates with the Qt event
    loop, so the UI thread never blocks — subprocess.run() would freeze the
    window for the duration of every call.

    finished(request_id, stdout) — command exited 0.
    failed(request_id, message)  — non-zero exit, spawn failure, or timeout.
                                   message carries stderr verbatim: the user
                                   sees systemd's own words, not a paraphrase.

    Single-flight: a request whose id is already in flight is rejected
    (returns False). Refresh timers + manual refresh + action-triggered
    refresh can otherwise stack arbitrarily many identical subprocesses.
    """

    finished = Signal(str, str)
    failed = Signal(str, str)

    def __init__(
        self,
        parent: QObject | None = None,
        systemctl: str = "systemctl",
        journalctl: str = "journalctl",
        analyze: str = "systemd-analyze",
        timeout_ms: int = 5000,
    ) -> None:
        super().__init__(parent)
        self._systemctl = systemctl
        self._journalctl = journalctl
        self._analyze = analyze
        self._timeout_ms = timeout_ms
        # request_id -> QProcess; doubles as the single-flight registry.
        # The dict itself is bounded by distinct request kinds (~8). Dead
        # QProcess objects accumulate as children of this client — see the
        # DEF-T4-01 comment in request() for why they are not freed.
        self._inflight: dict[str, QProcess] = {}

    # -- generic runner ----------------------------------------------------

    def request(self, request_id: str, argv: list[str]) -> bool:
        """Start argv asynchronously; results arrive via finished/failed.

        Returns False (and runs nothing) if request_id is already in flight.
        """
        if request_id in self._inflight:
            return False
        proc = QProcess(self)
        # KNOWN BOUNDED LEAK (DEF-T4-01): dead QProcess objects stay parented
        # to this client until it is destroyed. Freeing them is deliberately
        # NOT done — both deleteLater() inside a Python slot AND the canonical
        # `proc.finished.connect(proc.deleteLater)` idiom segfault PySide6
        # 6.11.1 / Python 3.14 during event processing (probed 2026-06-11,
        # twice, different mechanisms — see plan deferments). Exposure is
        # bounded by the on-demand window's lifetime (~3 small objects per
        # 10s refresh). Revisit when PySide6 updates.
        self._inflight[request_id] = proc

        # QTimer (not QProcess.waitForFinished) keeps everything event-driven.
        watchdog = QTimer(proc)
        watchdog.setSingleShot(True)
        watchdog.setInterval(self._timeout_ms)

        # All three handlers guard on IDENTITY (`is proc`), not key presence:
        # a killed process's death echoes arrive on a LATER event-loop turn,
        # and if the same request_id was re-requested in that window, a bare
        # key check would let the stale echo steal the successor's registry
        # entry (spurious failed + the successor's real result swallowed).

        def on_timeout() -> None:
            if self._inflight.get(request_id) is not proc:
                return  # already terminal (finished / spawn-failed / successor owns id)
            proc.kill()  # triggers on_finished with CrashExit, which does the cleanup
            self._inflight.pop(request_id, None)
            self.failed.emit(request_id, f"{argv[0]} timed out after {self._timeout_ms} ms")

        def on_finished(exit_code: int, exit_status: QProcess.ExitStatus) -> None:
            if self._inflight.get(request_id) is not proc:
                return  # timeout or on_error already reported; ignore the echo
            self._inflight.pop(request_id, None)
            watchdog.stop()
            if exit_status != QProcess.ExitStatus.NormalExit:
                stderr = proc.readAllStandardError().data().decode(errors="replace").strip()
                self.failed.emit(request_id, f"{argv[0]} crashed: {stderr}")
                return
            if exit_code != 0:
                stderr = proc.readAllStandardError().data().decode(errors="replace").strip()
                self.failed.emit(request_id, f"{argv[0]} exit {exit_code}: {stderr}")
                return
            stdout = proc.readAllStandardOutput().data().decode(errors="replace")
            self.finished.emit(request_id, stdout)

        def on_error(err: QProcess.ProcessError) -> None:
            # ONLY spawn failures (binary missing, fd exhaustion) — for those,
            # finished() never fires, so cleanup must happen here. Crashes also
            # fire errorOccurred, but they fall through to on_finished, which
            # reads the real stderr instead of Qt's generic "Process crashed".
            if err != QProcess.ProcessError.FailedToStart:
                return
            if self._inflight.get(request_id) is not proc:
                return
            self._inflight.pop(request_id, None)
            watchdog.stop()
            self.failed.emit(request_id, f"failed to start {argv[0]}: {proc.errorString()}")

        watchdog.timeout.connect(on_timeout)
        proc.finished.connect(on_finished)
        proc.errorOccurred.connect(on_error)
        proc.start(argv[0], argv[1:])
        watchdog.start()
        return True

    # -- typed conveniences (argv assembly in ONE place) --------------------

    def _scope_args(self, scope: str) -> list[str]:
        """Return the scope flag list for scope: ["--user"] for user, [] for system."""
        return ["--user"] if scope == SCOPE_USER else []

    def list_timers(self, scope: str) -> bool:
        return self.request(
            f"timers:{scope}",
            [self._systemctl, *self._scope_args(scope), "list-timers", "--all", "-o", "json"],
        )

    def list_services(self, scope: str) -> bool:
        return self.request(
            f"services:{scope}",
            [
                self._systemctl, *self._scope_args(scope),
                "list-units", "--type=service", "-o", "json",
            ],
        )

    def fetch_results(self, scope: str, units: list[str]) -> bool:
        return self.request(
            f"results:{scope}",
            [self._systemctl, *self._scope_args(scope), "show", *units, "-p", RESULT_PROPS],
        )

    def fetch_log(self, scope: str, unit: str) -> bool:
        return self.request(
            f"log:{scope}:{unit}",
            [
                self._journalctl, *self._scope_args(scope),
                "-u", unit, "-o", "json", "-n", "200", "--no-pager",
            ],
        )

    def fetch_cat(self, scope: str, unit: str) -> bool:
        return self.request(
            f"cat:{scope}:{unit}",
            [self._systemctl, *self._scope_args(scope), "cat", unit, "--no-pager"],
        )

    def fetch_details(self, scope: str, unit: str) -> bool:
        return self.request(
            f"details:{scope}:{unit}",
            [self._systemctl, *self._scope_args(scope), "show", unit, "-p", DETAIL_PROPS],
        )

    def fetch_calendar(self, expr: str) -> bool:
        # systemd-analyze calendar needs no scope; expression comes from the
        # unit file's OnCalendar= line. --iterations verified on systemd 258.
        # "--" stops flag parsing: a malformed expression starting with "-"
        # (this app exists to inspect broken units) must not become a flag.
        return self.request(
            f"calendar:{expr}",
            [self._analyze, "calendar", "--iterations=5", "--", expr],
        )

    def run_action(self, argv: list[str], unit: str) -> bool:
        return self.request(f"action:{unit}", argv)
