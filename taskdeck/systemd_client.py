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
    exec_status is the main process exit code, None if the unit never ran.
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
    """Parse `systemctl list-units --type=service -o json` output."""
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

    The output is one Key=Value block per unit, blank-line separated, in
    ARGUMENT ORDER (verified empirically — systemd prints requested units in
    the order given). `units` must therefore be the exact argv order. Units
    beyond the block count (or with empty blocks) get no entry — absence
    means "unknown", which the UI renders as "—", not as success.
    """
    blocks = text.strip().split("\n\n")
    results: dict[str, LastResult] = {}
    for unit, block in zip(units, blocks, strict=False):
        props: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key] = value
        if not props:
            continue
        status_text = props.get("ExecMainStatus", "")
        results[unit] = LastResult(
            result=props.get("Result", ""),
            exec_status=int(status_text) if status_text.lstrip("-").isdigit() else None,
        )
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
        priority_text = str(obj.get("PRIORITY", "7"))
        entries.append(
            LogEntry(
                ts_usec=int(ts_text) if ts_text.isdigit() else None,
                identifier=str(obj.get("SYSLOG_IDENTIFIER", "")),
                message=message,
                priority=int(priority_text) if priority_text.isdigit() else 7,
            )
        )
    if bad and not entries:
        raise ValueError(f"journal output entirely unparseable ({bad} bad lines)")
    return entries
