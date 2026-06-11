"""TaskTableModel tests — fixed `now`, hand-built rows, no subprocesses."""
from datetime import datetime

from PySide6.QtCore import Qt

from taskdeck.models import COLUMNS, ROLE_ACTIVATES, ROLE_UNIT, TaskTableModel, classify_cadence
from taskdeck.systemd_client import LastResult, ScheduleInfo, ServiceRow, TimerRow

NOW = datetime(2026, 6, 10, 19, 0, 0)


def usec(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


TIMERS = [
    TimerRow(
        "a.timer", "a.service",
        usec(datetime(2026, 6, 10, 23, 10)), usec(datetime(2026, 6, 10, 17, 11)),
    ),
    TimerRow("b.timer", "b.service", None, None),
]
SERVICES = [
    ServiceRow("a.service", "loaded", "active", "running", "A job"),
    ServiceRow("b.service", "loaded", "inactive", "dead", "B job"),
]
RESULTS = {
    "a.service": LastResult("success", 0),
    "b.service": LastResult("exit-code", 1),
}
# b.timer deliberately ABSENT: a timer whose schedule fetch hasn't landed (or
# failed) must render cadence "—", same honesty policy as missing results.
SCHEDULES = {
    "a.timer": ScheduleInfo(calendar=("*-*-* 03:50:00",), monotonic=()),
}


def cell(model, row, col):
    return model.index(row, col).data(Qt.ItemDataRole.DisplayRole)


def test_timer_view_columns_and_rows(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, RESULTS, SCHEDULES, now=NOW)
    assert model.columnCount() == len(COLUMNS)
    assert model.rowCount() == 2
    assert cell(model, 0, 0) == "a.timer"
    assert cell(model, 0, 1) == "▶ running"      # activated service is active
    assert cell(model, 0, 2) == "daily"           # cadence from SCHEDULES
    assert "today 23:10" in cell(model, 0, 3)
    assert cell(model, 0, 5) == "✔ success"
    assert cell(model, 1, 1) == "○ inactive"      # no next elapse
    assert cell(model, 1, 2) == "—"               # b.timer absent from SCHEDULES
    assert cell(model, 1, 3) == "—"
    assert cell(model, 1, 5) == "✘ exit-code (1)"


def test_timer_view_unknown_result_renders_dash(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, {}, SCHEDULES, now=NOW)
    assert cell(model, 0, 5) == "—"


def test_service_view(qtbot):
    model = TaskTableModel()
    model.set_service_rows(SERVICES, RESULTS, now=NOW)
    assert cell(model, 0, 0) == "a.service"
    assert cell(model, 0, 1) == "▶ running"
    assert cell(model, 0, 2) == "—"               # services have no cadence
    assert cell(model, 1, 1) == "○ dead"
    assert cell(model, 1, 5) == "✘ exit-code (1)"


def test_unit_roles_for_action_wiring(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, RESULTS, SCHEDULES, now=NOW)
    idx = model.index(0, 0)
    assert idx.data(ROLE_UNIT) == "a.timer"
    assert idx.data(ROLE_ACTIVATES) == "a.service"


def test_waiting_status_when_next_set_and_service_idle(qtbot):
    timers = [TimerRow("c.timer", "c.service", usec(datetime(2026, 6, 11, 8, 0)), None)]
    services = [ServiceRow("c.service", "loaded", "inactive", "dead", "C job")]
    model = TaskTableModel()
    model.set_timer_rows(timers, services, {}, {}, now=NOW)
    assert cell(model, 0, 1) == "⏲ waiting"


def test_activating_oneshot_shows_running(qtbot):
    # Type=oneshot services (the normal shape for timer jobs) report
    # ActiveState=activating for their whole ExecStart run — that is
    # "running" to a human watching the table.
    timers = [TimerRow("c.timer", "c.service", None, None)]
    services = [ServiceRow("c.service", "loaded", "activating", "start", "C job")]
    model = TaskTableModel()
    model.set_timer_rows(timers, services, {}, {}, now=NOW)
    assert cell(model, 0, 1) == "▶ running"


def test_failed_service_status(qtbot):
    services = [ServiceRow("f.service", "loaded", "failed", "failed", "F job")]
    model = TaskTableModel()
    model.set_service_rows(services, {}, now=NOW)
    assert cell(model, 0, 1) == "✘ failed"


def test_result_column_foreground_tinting(qtbot):
    # Relational assertions only — models.py declares the exact shades
    # cosmetic (the information is also in the ✔/✘ glyphs), so the contract
    # is: result column tinted iff result known, success ≠ failure brush,
    # other columns untinted.
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, RESULTS, SCHEDULES, now=NOW)
    role = Qt.ItemDataRole.ForegroundRole
    success_brush = model.index(0, 5).data(role)
    failure_brush = model.index(1, 5).data(role)
    assert success_brush is not None and failure_brush is not None
    assert success_brush.color() != failure_brush.color()
    assert model.index(0, 0).data(role) is None  # only the result column tints
    model.set_timer_rows(TIMERS, SERVICES, {}, SCHEDULES, now=NOW)
    assert model.index(0, 5).data(role) is None  # unknown result → no tint


def test_header_data(qtbot):
    model = TaskTableModel()
    horizontal = Qt.Orientation.Horizontal
    display = Qt.ItemDataRole.DisplayRole
    assert model.headerData(0, horizontal, display) == "Task"
    assert model.headerData(0, Qt.Orientation.Vertical, display) is None


def test_never_ran_timer_renders_honest_dashes_end_to_end(qtbot):
    # THE compound-lie regression (QA TA-F1 + AT-F1): drive the REAL fixture
    # through parser → model and assert a never-ran row shows "—" for Last
    # run (not Dec 31 1969 from last:0) and "—" for Last result (no entry =
    # no false ✔ from systemd's Result=success DEFAULT). This is the test
    # that makes "the same fixtures drive both layers" true.
    from pathlib import Path

    from taskdeck.systemd_client import parse_list_timers

    fixture = Path(__file__).parent / "fixtures" / "list_timers.json"
    timers = parse_list_timers(fixture.read_text())
    never = next(r for r in timers if r.last_usec == 0)
    model = TaskTableModel()
    model.set_timer_rows(timers, [], {}, {}, now=NOW)
    row = next(i for i in range(model.rowCount()) if cell(model, i, 0) == never.unit)
    assert cell(model, row, 4) == "—"
    assert cell(model, row, 5) == "—"


def test_last_zero_sorts_as_oldest_sentinel(qtbot):
    from taskdeck.models import _SORT_NO_LAST, ROLE_SORT

    timers = [TimerRow("z.timer", "z.service", None, 0)]
    model = TaskTableModel()
    model.set_timer_rows(timers, [], {}, {}, now=NOW)
    assert model.index(0, 4).data(ROLE_SORT) == _SORT_NO_LAST


def test_sort_role_is_chronological_not_alphabetical(qtbot):
    # "Jun 12..." < "today..." alphabetically but not chronologically; the
    # proxy sorts the time columns by ROLE_SORT's raw epoch ints instead.
    from taskdeck.models import ROLE_SORT

    early = usec(datetime(2026, 6, 12, 9, 0))
    late = usec(datetime(2026, 7, 20, 12, 0))
    timers = [
        TimerRow("late.timer", "late.service", late, None),
        TimerRow("early.timer", "early.service", early, None),
        TimerRow("never.timer", "never.service", None, None),
    ]
    model = TaskTableModel()
    model.set_timer_rows(timers, [], {}, {}, now=NOW)
    keys = [model.index(r, 3).data(ROLE_SORT) for r in range(3)]
    assert keys[1] < keys[0]  # early < late, compared as ints
    assert keys[2] > keys[0]  # no-next sentinel sorts last ascending


# -- classify_cadence ---------------------------------------------------------
# Inputs are NORMALIZED expressions as `systemctl show` emits them (probed
# 2026-06-11: "daily" in a unit file arrives as "*-*-* 00:00:00"), so the
# table tests the wire shapes the classifier actually receives.

CADENCE_CASES = [
    # (calendar triggers, monotonic triggers, expected label)
    (("*-*-* 03:50:00",), (), "daily"),
    (("*-*-* 00:00:00",), (), "daily"),
    # Dustin's real timers (from the captured fixture):
    (("*-*-* 00,06,12,18:10:00 UTC",), (), "4×/day"),
    (("Mon *-*-* 06:00:00 America/Los_Angeles",), (), "weekly (Mon)"),
    (("Mon..Fri *-*-* 09:00:00",), (), "weekdays"),
    (("Sat,Sun *-*-* 10:00:00",), (), "weekly (Sat,Sun)"),
    (("*-*-01 04:00:00",), (), "monthly"),
    (("*-01-01 00:00:00",), (), "yearly"),
    (("*-*-* *:15:00",), (), "hourly"),
    # Unrecognized calendar shape falls back to the raw expression — an
    # honest raw string beats a wrong bucket.
    (("2026-06-15 12:00:00",), (), "2026-06-15 12:00:00"),
    # Monotonic triggers, including the real two-trigger boothang timer:
    ((), ("OnUnitActiveUSec=1d",), "every 1d"),
    ((), ("OnBootUSec=12h",), "boot+12h"),
    ((), ("OnStartupUSec=5min",), "login+5min"),
    ((), ("OnUnitActiveUSec=1d", "OnBootUSec=12h"), "every 1d + boot+12h"),
    # Unrecognized monotonic kind: show it raw, never guess.
    ((), ("OnClockChangeUSec=0",), "OnClockChangeUSec=0"),
    # Mixed calendar + monotonic joins both buckets.
    (("*-*-* 03:50:00",), ("OnBootUSec=15min",), "daily + boot+15min"),
    # A loaded timer with no triggers at all (transient/stopped) is honest "—".
    ((), (), "—"),
]


def test_classify_cadence_table():
    for calendar, monotonic, expected in CADENCE_CASES:
        info = ScheduleInfo(calendar=calendar, monotonic=monotonic)
        assert classify_cadence(info) == expected, (calendar, monotonic)


def test_classify_cadence_none_means_unknown():
    # None = "schedule fetch didn't land", distinct from "no triggers" only
    # in meaning — both render "—" because neither supports a cadence claim.
    assert classify_cadence(None) == "—"


def test_classify_cadence_dedupes_identical_buckets():
    # Two triggers that bucket identically read once ("daily"), not
    # "daily + daily" — the join is for DIFFERENT cadences.
    info = ScheduleInfo(calendar=("*-*-* 03:00:00", "*-*-* 15:00:00"), monotonic=())
    assert classify_cadence(info) == "daily"
