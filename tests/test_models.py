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
