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
