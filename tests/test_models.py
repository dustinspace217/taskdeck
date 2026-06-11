"""TaskTableModel tests — fixed `now`, hand-built rows, no subprocesses."""
from datetime import datetime

from PySide6.QtCore import Qt

from taskdeck.models import COLUMNS, ROLE_ACTIVATES, ROLE_UNIT, TaskTableModel
from taskdeck.systemd_client import LastResult, ServiceRow, TimerRow

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


def cell(model, row, col):
    return model.index(row, col).data(Qt.ItemDataRole.DisplayRole)


def test_timer_view_columns_and_rows(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, RESULTS, now=NOW)
    assert model.columnCount() == len(COLUMNS)
    assert model.rowCount() == 2
    assert cell(model, 0, 0) == "a.timer"
    assert cell(model, 0, 1) == "▶ running"      # activated service is active
    assert "today 23:10" in cell(model, 0, 2)
    assert cell(model, 0, 4) == "✔ success"
    assert cell(model, 1, 1) == "○ inactive"      # no next elapse
    assert cell(model, 1, 2) == "—"
    assert cell(model, 1, 4) == "✘ exit-code (1)"


def test_timer_view_unknown_result_renders_dash(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, {}, now=NOW)
    assert cell(model, 0, 4) == "—"


def test_service_view(qtbot):
    model = TaskTableModel()
    model.set_service_rows(SERVICES, RESULTS, now=NOW)
    assert cell(model, 0, 0) == "a.service"
    assert cell(model, 0, 1) == "▶ running"
    assert cell(model, 1, 1) == "○ dead"
    assert cell(model, 1, 4) == "✘ exit-code (1)"


def test_unit_roles_for_action_wiring(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, RESULTS, now=NOW)
    idx = model.index(0, 0)
    assert idx.data(ROLE_UNIT) == "a.timer"
    assert idx.data(ROLE_ACTIVATES) == "a.service"


def test_waiting_status_when_next_set_and_service_idle(qtbot):
    timers = [TimerRow("c.timer", "c.service", usec(datetime(2026, 6, 11, 8, 0)), None)]
    services = [ServiceRow("c.service", "loaded", "inactive", "dead", "C job")]
    model = TaskTableModel()
    model.set_timer_rows(timers, services, {}, now=NOW)
    assert cell(model, 0, 1) == "⏲ waiting"


def test_activating_oneshot_shows_running(qtbot):
    # Type=oneshot services (the normal shape for timer jobs) report
    # ActiveState=activating for their whole ExecStart run — that is
    # "running" to a human watching the table.
    timers = [TimerRow("c.timer", "c.service", None, None)]
    services = [ServiceRow("c.service", "loaded", "activating", "start", "C job")]
    model = TaskTableModel()
    model.set_timer_rows(timers, services, {}, now=NOW)
    assert cell(model, 0, 1) == "▶ running"


def test_failed_service_status(qtbot):
    services = [ServiceRow("f.service", "loaded", "failed", "failed", "F job")]
    model = TaskTableModel()
    model.set_service_rows(services, {}, now=NOW)
    assert cell(model, 0, 1) == "✘ failed"


def test_result_column_foreground_colors(qtbot):
    model = TaskTableModel()
    model.set_timer_rows(TIMERS, SERVICES, RESULTS, now=NOW)
    role = Qt.ItemDataRole.ForegroundRole
    assert model.index(0, 4).data(role).color().name() == "#4caf50"  # success → green
    assert model.index(1, 4).data(role).color().name() == "#e57373"  # failure → red
    assert model.index(0, 0).data(role) is None  # only the result column tints
    model.set_timer_rows(TIMERS, SERVICES, {}, now=NOW)
    assert model.index(0, 4).data(role) is None  # unknown result → no tint


def test_header_data(qtbot):
    model = TaskTableModel()
    horizontal = Qt.Orientation.Horizontal
    display = Qt.ItemDataRole.DisplayRole
    assert model.headerData(0, horizontal, display) == "Task"
    assert model.headerData(0, Qt.Orientation.Vertical, display) is None


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
    model.set_timer_rows(timers, [], {}, now=NOW)
    keys = [model.index(r, 2).data(ROLE_SORT) for r in range(3)]
    assert keys[1] < keys[0]  # early < late, compared as ints
    assert keys[2] > keys[0]  # no-next sentinel sorts last ascending
