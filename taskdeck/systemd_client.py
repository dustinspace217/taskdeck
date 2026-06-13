"""Data layer: systemd output types, parsers, and the async client.

This module is the ONLY place subprocesses are spawned, and it never imports
widgets — the planned v2 tray notifier reuses it headless. Parsers are pure
functions (text in → dataclasses out) so they're testable without Qt at all.

Data contracts were probed live on systemd 258 / Fedora 44 (2026-06-10); see
docs/superpowers/plans/2026-06-10-taskdeck-v1.md "Verified data contracts".
"""
from __future__ import annotations

import json
import re
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
class ScheduleInfo:
    """A timer's EFFECTIVE triggers, from `systemctl show` — never re-derived
    from unit-file text (drop-in overrides, `OnCalendar =` spacing, and
    multi-line schedules all made text-scraping lie; QA AT-F6 / DEF-V11-02).

    calendar holds normalized OnCalendar expressions (may carry a timezone
    suffix like "UTC" or "America/Los_Angeles"); monotonic holds spec strings
    like "OnBootUSec=12h" — note systemd's USec-spelled property names, not
    the unit-file *Sec forms; next_elapse is systemd's human-readable
    NextElapseUSecRealtime ("" when empty/none).
    """

    calendar: tuple[str, ...]
    monotonic: tuple[str, ...]
    next_elapse: str = ""


@dataclass(frozen=True)
class LogEntry:
    """One journal line from `journalctl -o json`."""

    ts_usec: int | None
    identifier: str
    message: str
    priority: int  # syslog level 0-7; 7 (debug) when the field is absent


def _require_str(item: dict[str, object], key: str, context: str) -> str:
    """Field-type gate for required string fields.

    systemd's table-to-JSON layer emits null for empty cells, and a None
    smuggled into a dataclass explodes far from its source (e.g. inside
    sorted() in the refresh path) with a TypeError — OUTSIDE the window's
    surfaced exception classes, producing the silent-frozen-table failure
    mode. Raising ValueError here keeps the failure loud and attributed.
    """
    value = item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{context}: field {key!r} is {type(value).__name__}, expected str")
    return value


def _int_or_none(item: dict[str, object], key: str, context: str) -> int | None:
    """Field-type gate for optional µs-epoch fields (int or null)."""
    value = item.get(key)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise ValueError(f"{context}: field {key!r} is {type(value).__name__}, expected int or null")


def parse_list_timers(text: str) -> list[TimerRow]:
    """Parse `systemctl list-timers --all -o json` output.

    Raises ValueError on malformed input OR wrong-shape/wrong-type payloads
    (a JSON object instead of an array, null required fields) — callers
    surface that as an error state, never an empty table (no-silent-failure
    rule). NOTE: `last` is passed through faithfully, INCLUDING the literal 0
    systemd emits for never-ran timers — rendering treats 0 as missing
    (models.ts_missing); the client does not editorialize.
    """
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError(f"list-timers: expected JSON array, got {type(raw).__name__}")
    return [
        TimerRow(
            unit=_require_str(item, "unit", "list-timers"),
            activates=_require_str(item, "activates", "list-timers"),
            next_usec=_int_or_none(item, "next", "list-timers"),
            last_usec=_int_or_none(item, "last", "list-timers"),
        )
        for item in raw
    ]


def parse_list_units(text: str) -> list[ServiceRow]:
    """Parse `systemctl list-units --type=service -o json` output.

    Same validation policy as parse_list_timers: wrong shape or a null unit
    name raises ValueError. The descriptive fields tolerate null (rendered
    as empty) — only fields that flow into request argv must be strings.
    """
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError(f"list-units: expected JSON array, got {type(raw).__name__}")
    return [
        ServiceRow(
            unit=_require_str(item, "unit", "list-units"),
            load=item.get("load") or "",
            active=item.get("active") or "",
            sub=item.get("sub") or "",
            description=item.get("description") or "",
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
    for unit, lines in _walk_show_blocks(text, units):
        props: dict[str, str] = {}
        for line in lines:
            key, sep, value = line.partition("=")
            if sep:
                props[key] = value
        # NEVER-RAN GATE (probed 2026-06-11): a loaded-but-never-run service
        # reports Result=success and ExecMainStatus=0 — those are DEFAULTS,
        # not evidence; rendering them green told the user "it ran and it
        # succeeded" about a job that never fired (and after a re-login, a
        # FAILED job's evidence resets to those same defaults). The
        # disambiguator is ExecMainExitTimestamp: present-but-EMPTY for
        # never-ran (probe: `show drkonqi-coredump-cleanup.service`), and
        # equally empty while a main process is STILL RUNNING (the status
        # column shows ▶ running for that case) or when the key is absent
        # entirely (non-service unit) — all three correctly mean "no result
        # to claim". No entry → renders "—". LIMITATION, by design: this
        # converts false-success to unknown, not to truth — post-relogin the
        # only surviving failure evidence is the journal.
        if not props.get("ExecMainExitTimestamp", ""):
            continue
        # EAFP int parsing: .isdigit() guards accept strings int() rejects
        # ("--1", unicode digits); try/except is both shorter and correct.
        try:
            exec_status: int | None = int(props.get("ExecMainStatus", ""))
        except ValueError:
            exec_status = None
        results[unit] = LastResult(result=props.get("Result", ""), exec_status=exec_status)
    return results


def _walk_show_blocks(text: str, units: list[str]) -> list[tuple[str, list[str]]]:
    """Split batched `systemctl show` output into (unit, lines) blocks.

    The alignment contract parse_show_results documents lives HERE so every
    show-parser shares one implementation: blocks arrive in ARGUMENT ORDER
    separated by blank lines, and a property-less unit emits an EMPTY block
    (probed 2026-06-10) — so the walk advances the unit index at each blank
    line; empty blocks yield nothing; more NON-EMPTY blocks than units raises
    (the argv and the parser disagree about the contract).
    """
    out: list[tuple[str, list[str]]] = []
    idx = 0
    acc: list[str] = []

    def flush() -> None:
        nonlocal acc
        if not acc:
            return
        if idx >= len(units):
            raise ValueError(
                f"systemctl show returned more blocks than the {len(units)} requested units"
            )
        out.append((units[idx], acc))
        acc = []

    for line in text.splitlines():
        if not line.strip():
            flush()
            idx += 1
            continue
        acc.append(line)
    flush()
    return out


# One trigger per line: `TimersCalendar={ OnCalendar=<expr> ; next_elapse=<ts> }`
# with the key REPEATED for multi-trigger timers (probed 2026-06-11). The
# next_elapse timestamp is brace-free ([^{}]*) and the pattern is anchored to a
# single closing brace at end-of-line, so trailing content — a future systemd
# packing two `{…} {…}` entries on one line — fails to match and raises the loud
# ValueError below instead of being silently dropped (QA 2026-06-12 P2-7).
_TRIGGER_RE = re.compile(r"\{ (\w+)=([^{}]+?) ; next_elapse=[^{}]* \}$")


def parse_show_schedules(text: str, units: list[str]) -> dict[str, ScheduleInfo]:
    """Parse batched `show -p TimersCalendar,TimersMonotonic,NextElapse…`.

    Fail-loud contract: an unrecognized trigger-line shape raises ValueError —
    a half-parsed schedule rendered confidently is exactly the bug class this
    parser replaced (text-scraped unit files, QA AT-F6).
    """
    out: dict[str, ScheduleInfo] = {}
    for unit, lines in _walk_show_blocks(text, units):
        calendar: list[str] = []
        monotonic: list[str] = []
        next_elapse = ""
        for line in lines:
            key, _, value = line.partition("=")
            if key in ("TimersCalendar", "TimersMonotonic"):
                if not value:
                    continue  # property present but empty — no trigger of this kind
                match = _TRIGGER_RE.match(value)
                if match is None:
                    raise ValueError(f"schedules: unrecognized trigger line for {unit}: {line!r}")
                spec_key, spec_value = match.group(1), match.group(2)
                if key == "TimersCalendar":
                    # spec_key is always OnCalendar here; the expression alone
                    # is what classification and systemd-analyze consume.
                    calendar.append(spec_value)
                else:
                    monotonic.append(f"{spec_key}={spec_value}")
            elif key == "NextElapseUSecRealtime":
                next_elapse = value
        out[unit] = ScheduleInfo(tuple(calendar), tuple(monotonic), next_elapse)
    return out


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
# Effective trigger data for the Cadence column AND the Schedule tab — one
# property set so both consumers share fixtures and parsing (DEF-V11-02).
SCHEDULE_PROPS = "TimersCalendar,TimersMonotonic,NextElapseUSecRealtime"


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
        # request_id -> QProcess; doubles as the single-flight registry,
        # bounded by distinct request kinds (~8).
        self._inflight: dict[str, QProcess] = {}
        # Finished processes awaiting deletion. They are NOT freed in their own
        # finished/timeout slot: deleting a QProcess (even via deleteLater)
        # during its own signal emission segfaults PySide6 6.11.1 / py3.14
        # (DEF-T4-01, probed twice — both in-slot deleteLater and the
        # `finished.connect(deleteLater)` idiom). Instead each is parked here
        # and freed at the top of the NEXT request() (see _sweep_finished),
        # which runs entirely outside any emission of these objects. Bounded by
        # how many can finish between two requests (~8).
        self._finished: list[tuple[QProcess, QTimer]] = []

    @property
    def systemctl_path(self) -> str:
        """The injected systemctl binary path.

        Exposed so action argv (built in actions.py) honors the same
        injection the fetches do — otherwise a fakebin-injected test client
        would still spawn the REAL systemctl for actions.
        """
        return self._systemctl

    # -- generic runner ----------------------------------------------------

    def request(self, request_id: str, argv: list[str], timeout_ms: int | None = None) -> bool:
        """Start argv asynchronously; results arrive via finished/failed.

        Returns False (and runs nothing) if request_id is already in flight.
        timeout_ms overrides the client default per request kind — journal
        reads legitimately take longer than list queries (reverse-seeking a
        multi-GB rotated journal is a known multi-second operation).
        """
        if request_id in self._inflight:
            return False
        # Free the previous cycle's finished processes now — safe here because
        # we are at the top of a fresh request(), outside any of their signal
        # emissions (the DEF-T4-01 fix). Any late death-echoes they had were
        # already processed and dropped by the identity guards below.
        self._sweep_finished()
        effective_timeout = timeout_ms if timeout_ms is not None else self._timeout_ms
        proc = QProcess(self)
        self._inflight[request_id] = proc

        # QTimer (not QProcess.waitForFinished) keeps everything event-driven.
        watchdog = QTimer(proc)
        watchdog.setSingleShot(True)
        watchdog.setInterval(effective_timeout)

        # All three handlers guard on IDENTITY (`is proc`), not key presence:
        # a killed process's death echoes arrive on a LATER event-loop turn,
        # and if the same request_id was re-requested in that window, a bare
        # key check would let the stale echo steal the successor's registry
        # entry (spurious failed + the successor's real result swallowed).

        def on_timeout() -> None:
            if self._inflight.get(request_id) is not proc:
                return  # already terminal (finished / spawn-failed / successor owns id)
            proc.kill()  # the CrashExit echo is swallowed by on_finished's identity guard
            self._retire(request_id, proc, watchdog)
            self.failed.emit(request_id, f"{argv[0]} timed out after {effective_timeout} ms")

        def on_finished(exit_code: int, exit_status: QProcess.ExitStatus) -> None:
            if self._inflight.get(request_id) is not proc:
                return  # timeout or on_error already reported; ignore the echo
            # proc is parked, not deleted — still safe to read its buffers below.
            self._retire(request_id, proc, watchdog)
            if exit_status != QProcess.ExitStatus.NormalExit:
                stderr = proc.readAllStandardError().data().decode(errors="replace").strip()
                self.failed.emit(request_id, f"{argv[0]} crashed: {stderr}")
                return
            if exit_code != 0:
                stderr = proc.readAllStandardError().data().decode(errors="replace").strip()
                self.failed.emit(request_id, f"{argv[0]} exit {exit_code}: {stderr}")
                return
            if request_id.startswith("action:"):
                # Actions print nothing useful to stdout, but systemd DOES
                # explain itself on stderr even at exit 0 — probed 2026-06-11:
                # `enable` on an [Install]-less unit exits 0 with its whole
                # explanation on stderr. Deliver stderr as the payload so the
                # window shows systemd's own words instead of a bare "ok"
                # that misleads ("ok" + nothing changed + reason discarded).
                stderr = proc.readAllStandardError().data().decode(errors="replace").strip()
                self.finished.emit(request_id, stderr)
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
            self._retire(request_id, proc, watchdog)
            self.failed.emit(request_id, f"failed to start {argv[0]}: {proc.errorString()}")

        watchdog.timeout.connect(on_timeout)
        proc.finished.connect(on_finished)
        proc.errorOccurred.connect(on_error)
        proc.start(argv[0], argv[1:])
        watchdog.start()
        return True

    def _retire(self, request_id: str, proc: QProcess, watchdog: QTimer) -> None:
        """Mark a request terminal: stop its watchdog, drop it from the
        single-flight registry, and PARK the QProcess for deletion at the next
        request(). Deletion is deferred (not done here) because freeing a
        QProcess during its own finished/timeout emission segfaults PySide6
        6.11.1 / py3.14 (DEF-T4-01). proc stays valid to read after this — it is
        only parked, not deleted."""
        watchdog.stop()
        self._inflight.pop(request_id, None)
        self._finished.append((proc, watchdog))

    def _sweep_finished(self) -> None:
        """Delete processes retired in a PRIOR request cycle. Called only at the
        top of request(), so it runs outside any signal emission of these
        objects — the condition that makes deleteLater safe here (DEF-T4-01).
        Each proc's slots are disconnected first, both to break the
        proc<->closure reference cycle (so Python can GC the wrappers) and so a
        stray queued echo can never reach a closure mid-teardown."""
        for proc, watchdog in self._finished:
            for signal in (proc.finished, proc.errorOccurred, watchdog.timeout):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass  # no remaining connection — already clean
            proc.deleteLater()
        self._finished.clear()

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

    def list_failed_services(self, scope: str) -> bool:
        """Failed services only — the background monitor's poll. --state=failed
        narrows the query server-side so the monitor's diff compares just the
        failures, not the full unit list. Same JSON shape as list_services, so
        parse_list_units reads it unchanged."""
        return self.request(
            f"failed:{scope}",
            [
                self._systemctl, *self._scope_args(scope),
                "list-units", "--type=service", "--state=failed", "--all", "-o", "json",
            ],
        )

    def fetch_results(self, scope: str, units: list[str]) -> bool:
        return self.request(
            f"results:{scope}",
            [self._systemctl, *self._scope_args(scope), "show", *units, "-p", RESULT_PROPS],
        )

    def fetch_schedules(self, scope: str, units: list[str]) -> bool:
        """Batched effective-trigger fetch for the Cadence column."""
        return self.request(
            f"schedules:{scope}",
            [self._systemctl, *self._scope_args(scope), "show", *units, "-p", SCHEDULE_PROPS],
        )

    def fetch_tab_schedule(self, scope: str, unit: str) -> bool:
        """Single-unit effective-trigger fetch for the Schedule tab."""
        return self.request(
            f"schedtab:{scope}:{unit}",
            [self._systemctl, *self._scope_args(scope), "show", unit, "-p", SCHEDULE_PROPS],
        )

    def fetch_log(self, scope: str, unit: str) -> bool:
        return self.request(
            f"log:{scope}:{unit}",
            [
                self._journalctl, *self._scope_args(scope),
                "-u", unit, "-o", "json", "-n", "200", "--no-pager",
            ],
            # Journal reverse-seek on big rotated journals routinely exceeds
            # the 5s default — killing it exactly at investigate-this-unit
            # moments. 15s per QA AT-F10.
            timeout_ms=15_000,
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
        # systemd-analyze calendar needs no scope; since the Task 10 redesign
        # the expression is a NORMALIZED OnCalendar form from `systemctl show`
        # (not raw unit-file text), so it is well-formed by construction.
        # "--" stops flag parsing anyway — belt-and-suspenders now rather than
        # load-bearing, kept because it costs nothing and survives a future
        # caller passing unnormalized text. --iterations verified on 258.
        return self.request(
            f"calendar:{expr}",
            [self._analyze, "calendar", "--iterations=5", "--", expr],
        )

    def run_action(self, argv: list[str], unit: str) -> bool:
        return self.request(f"action:{unit}", argv)
