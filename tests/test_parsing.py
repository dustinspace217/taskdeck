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
    # A block can lack ExecMainStatus entirely (non-service units); a unit
    # list longer than the output must not raise — absent units get no entry.
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


def test_parse_show_results_leading_empty_block_keeps_alignment():
    # Probed 2026-06-10: a unit with none of the requested properties (e.g. a
    # .target) emits an EMPTY block — `show basic.target a.service -p …`
    # output begins with a blank line. The result must land on a.service;
    # basic.target gets no entry, and nothing is misattributed.
    text = "\nResult=success\nExecMainExitTimestamp=Wed\nExecMainStatus=0\n"
    results = parse_show_results(text, ["basic.target", "a.service"])
    assert "basic.target" not in results
    assert results["a.service"] == LastResult("success", 0)


def test_parse_show_results_middle_empty_block_keeps_alignment():
    text = "Result=success\nExecMainStatus=0\n\n\nResult=exit-code\nExecMainStatus=1\n"
    results = parse_show_results(text, ["a.service", "b.target", "c.service"])
    assert results["a.service"] == LastResult("success", 0)
    assert "b.target" not in results
    assert results["c.service"] == LastResult("exit-code", 1)


def test_parse_show_results_distinct_values_land_on_their_units():
    # Guards the argument-order contract with DISTINGUISHABLE per-unit values
    # (the real fixture's two blocks are identical, so it can't catch swaps).
    text = "Result=success\nExecMainStatus=0\n\nResult=exit-code\nExecMainStatus=1\n"
    results = parse_show_results(text, ["ok.service", "bad.service"])
    assert results["ok.service"] == LastResult("success", 0)
    assert results["bad.service"] == LastResult("exit-code", 1)


def test_parse_show_results_more_blocks_than_units_raises():
    text = "Result=success\n\nResult=exit-code\n"
    with pytest.raises(ValueError):
        parse_show_results(text, ["only.service"])


def test_parse_journal_empty_output_is_empty_not_error():
    # Probed 2026-06-10: `journalctl -o json` for a unit with no entries emits
    # NOTHING on stdout (the "-- No entries --" marker is human-format only).
    # Empty input is therefore a legitimate empty log, not a broken call.
    assert parse_journal("") == []


def test_parse_journal_bytearray_message_renders_as_repr():
    # journald stores non-UTF8 payloads as byte arrays (the one documented
    # contract quirk); they must render as their repr, not vanish.
    line = '{"MESSAGE":[72,105],"__REALTIME_TIMESTAMP":"1781136604691295","PRIORITY":"6"}'
    entries = parse_journal(line)
    assert entries[0].message == "[72, 105]"


def test_parse_list_units_preserves_escaped_unit_names():
    # \x2d escapes in unit names are displayed AS-IS in v1 (spec decision);
    # pin the passthrough so a future "helpful" unescape shows up as a diff.
    rows = parse_list_units((FIXTURES / "list_units.json").read_text())
    escaped = [r.unit for r in rows if "\\x2d" in r.unit]
    assert escaped, "fixture contains escaped names; passthrough must preserve them"
