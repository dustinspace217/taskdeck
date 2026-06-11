"""Parser tests against REAL captured systemd output (tests/fixtures/).

Fixtures were captured from the dev machine on 2026-06-10 (systemd 258).
Using real output — not hand-written JSON — means a parser that passes here
parses the actual contract, escapes and all.
"""
from pathlib import Path

import pytest

from taskdeck.systemd_client import (
    LastResult,
    LogEntry,
    ServiceRow,
    TimerRow,
    parse_journal,
    parse_list_timers,
    parse_list_units,
    parse_show_results,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_list_timers_returns_rows_with_microsecond_epochs():
    rows = parse_list_timers((FIXTURES / "list_timers.json").read_text())
    assert rows, "fixture machine has timers; empty result = parser bug"
    assert all(isinstance(r, TimerRow) for r in rows)
    fetch = next(r for r in rows if r.unit == "astrowidget-fetch.timer")
    assert fetch.activates == "astrowidget-fetch.service"
    # µs epoch sanity: 2026 epochs are ~1.78e15 µs. A value ~1.78e9 would mean
    # we mistook seconds for microseconds.
    assert fetch.next_usec is None or fetch.next_usec > 1e15


def test_parse_list_timers_tolerates_null_next_and_last():
    text = (
        '[{"next":null,"left":null,"last":null,"passed":null,'
        '"unit":"x.timer","activates":"x.service"}]'
    )
    rows = parse_list_timers(text)
    assert rows[0].next_usec is None and rows[0].last_usec is None


def test_parse_list_units_fields():
    rows = parse_list_units((FIXTURES / "list_units.json").read_text())
    assert rows and all(isinstance(r, ServiceRow) for r in rows)
    assert all(r.unit.endswith(".service") for r in rows)
    allowed = {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
    assert {r.active for r in rows} <= allowed


def test_parse_show_results_blank_line_separated_blocks_in_arg_order():
    text = (FIXTURES / "show_results.txt").read_text()
    units = ["astrowidget-fetch.service", "boothang-update-check.service"]
    results = parse_show_results(text, units)
    assert set(results) <= set(units)
    first = results[units[0]]
    assert isinstance(first, LastResult)
    assert first.result == "success"
    assert first.exec_status == 0


def test_parse_show_results_tolerates_missing_keys_and_short_output():
    # A never-run unit can produce an empty block; a unit list longer than the
    # output must not raise — absent units simply have no entry.
    results = parse_show_results("Result=success\n", ["a.service", "b.service"])
    assert results["a.service"].result == "success"
    assert results["a.service"].exec_status is None
    assert "b.service" not in results


def test_parse_journal_lines():
    entries = parse_journal((FIXTURES / "journal.jsonl").read_text())
    assert entries and all(isinstance(e, LogEntry) for e in entries)
    assert all(e.message for e in entries)
    assert all(0 <= e.priority <= 7 for e in entries)


def test_parse_journal_skips_garbage_lines_loudly():
    # One bad line must not lose the good ones — but it raises if EVERYTHING
    # is garbage (a wholly unparseable journal means a broken call, and
    # pretending it was empty would be a silent failure).
    good = '{"MESSAGE":"ok","__REALTIME_TIMESTAMP":"1781136604691295","PRIORITY":"6"}'
    entries = parse_journal(good + "\nnot json\n")
    assert len(entries) == 1
    with pytest.raises(ValueError):
        parse_journal("not json at all\n")
