"""Offscreen smoke test: build the real window with canned data, screenshot it.

This is the self-verification loop: Qt renders headlessly (offscreen QPA from
conftest), grab() rasterizes the window, and the PNG artifact under
tests/artifacts/ is inspectable after every run. It catches layout
catastrophes (zero-size table, missing pane) that widget-level asserts miss.
"""
from datetime import datetime
from pathlib import Path

from taskdeck.main_window import MainWindow
from taskdeck.systemd_client import LastResult, ServiceRow, SystemdClient, TimerRow

ARTIFACTS = Path(__file__).parent / "artifacts"
FAKEBIN = Path(__file__).parent / "fakebin"


def test_window_builds_and_renders(qtbot):
    # All three binary paths point at fakebin so hermeticity is STRUCTURAL —
    # a future edit that fires a fetch or an action cannot silently spawn
    # the real systemctl (QA TA-F6 + the action-path extension).
    client = SystemdClient(
        systemctl=str(FAKEBIN / "fake_ok"),
        journalctl=str(FAKEBIN / "fake_ok"),
        analyze=str(FAKEBIN / "fake_ok"),
    )
    window = MainWindow(client, auto_refresh=False)
    qtbot.addWidget(window)

    window.model.set_timer_rows(
        [TimerRow("a.timer", "a.service", 1781136604691295, 1781115051863104)],
        [ServiceRow("a.service", "loaded", "active", "running", "A")],
        {"a.service": LastResult("success", 0)},
        now=datetime(2026, 6, 10, 19, 0, 0),
    )
    # Enablement requires the rendered data's scope to match the active scope
    # (the model was populated directly here, bypassing _apply_results).
    window._data_scope = "user"
    window.resize(1100, 700)
    window.show()
    qtbot.waitExposed(window)

    assert window.table.model().rowCount() == 1
    # System scope must hard-disable every action button (read-only contract).
    window.set_scope("system")
    assert all(not a.isEnabled() for a in window.action_buttons)
    window.set_scope("user")
    # Action buttons need a selected row even in user scope:
    assert all(not a.isEnabled() for a in window.action_buttons)
    window.table.selectRow(0)
    assert all(a.isEnabled() for a in window.action_buttons)

    ARTIFACTS.mkdir(exist_ok=True)
    assert window.grab().save(str(ARTIFACTS / "smoke.png"))
