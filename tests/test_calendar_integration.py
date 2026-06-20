"""Integration tests for the Calendar page wired into MainWindow.

These pin the host-side behavior of Task 7's end-to-end Day slice: selecting
"Calendar" in the View dropdown swaps the central QStackedWidget to the
CalendarView page; a calendar-row click fires the SAME detail-tab fetches the
table selection uses (the selection adapter); and a calendar build fires exactly
ONE journal query plus a projection per ELIGIBLE timer (disabled timers get no
projection — spec §9).

A local FakeClient (records calls, no subprocess) is used rather than the real
SystemdClient so the build/select/swap logic is exercised hermetically — the
same injection idiom test_window_logic.py uses, extended with the two calendar
fetch methods. MainWindow is built with auto_refresh=False so construction fires
zero subprocesses; tests that need the selection-fetch path flip the private
_auto_refresh flag afterwards (the timer was never started).
"""
from PySide6.QtCore import QObject, Signal

from taskdeck.main_window import MainWindow
from taskdeck.systemd_client import ScheduleInfo, TimerRow


class FakeClient(QObject):
    """Signal-compatible stand-in for SystemdClient that records every call.

    Mirrors test_window_logic.FakeClient (kept local rather than imported so the
    two test modules stay independent), with the two calendar fetch methods
    Task 7 adds: fetch_cal_journal (the single manager-scoped run query) and
    fetch_cal_projection (one per eligible timer). Both record and return
    `accept` so single-flight-rejection handling can be exercised by flipping it.
    """

    finished = Signal(str, str)
    failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.accept = True

    @property
    def systemctl_path(self) -> str:
        return "systemctl"

    def _record(self, *call: object) -> bool:
        self.calls.append(call)
        return self.accept

    def list_timers(self, scope: str) -> bool:
        return self._record("list_timers", scope)

    def list_services(self, scope: str) -> bool:
        return self._record("list_services", scope)

    def fetch_results(self, scope: str, units: list) -> bool:
        return self._record("fetch_results", scope, list(units))

    def fetch_schedules(self, scope: str, units: list) -> bool:
        return self._record("fetch_schedules", scope, list(units))

    def fetch_tab_schedule(self, scope: str, unit: str) -> bool:
        return self._record("fetch_tab_schedule", scope, unit)

    def fetch_log(self, scope: str, unit: str) -> bool:
        return self._record("fetch_log", scope, unit)

    def fetch_details(self, scope: str, unit: str) -> bool:
        return self._record("fetch_details", scope, unit)

    def fetch_cat(self, scope: str, unit: str) -> bool:
        return self._record("fetch_cat", scope, unit)

    def fetch_calendar(self, expr: str) -> bool:
        return self._record("fetch_calendar", expr)

    def fetch_cal_projection(
        self, scope: str, unit: str, expr: str, base_epoch: int, iterations: int
    ) -> bool:
        return self._record(
            "fetch_cal_projection", scope, unit, expr, base_epoch, iterations
        )

    def fetch_cal_journal(self, scope: str, since_epoch: int, until_epoch: int) -> bool:
        return self._record("fetch_cal_journal", scope, since_epoch, until_epoch)

    def run_action(self, argv: list, unit: str) -> bool:
        return self._record("run_action", list(argv), unit)

    def flush_finished(self) -> None:
        self.calls.append(("flush_finished",))


def make_window(qtbot):
    client = FakeClient()
    window = MainWindow(client, auto_refresh=False)
    qtbot.addWidget(window)
    return window, client


def calls_of(client, name):
    return [c for c in client.calls if c[0] == name]


# -- stacked-widget swap ------------------------------------------------------


def test_selecting_calendar_shows_calendar_page(qtbot):
    # The View dropdown gains a "Calendar" entry that swaps the central
    # QStackedWidget to the CalendarView page (page 1) instead of refreshing
    # the table.
    window, client = make_window(qtbot)
    idx = window.view_box.findText("Calendar")
    assert idx >= 0
    window.view_box.setCurrentIndex(idx)
    assert window._stack.currentWidget() is window.calendar_view


def test_switching_back_to_timers_shows_table_page(qtbot):
    # Selecting Timers/Services after Calendar must return to the table page
    # (page 0) — the two views are independent pages, and the table path must
    # not be reachable while Calendar is showing nor stranded after leaving it.
    window, client = make_window(qtbot)
    cal_idx = window.view_box.findText("Calendar")
    window.view_box.setCurrentIndex(cal_idx)
    assert window._stack.currentWidget() is window.calendar_view
    window.view_box.setCurrentIndex(window.view_box.findText("Timers"))
    assert window._stack.currentIndex() == 0


# -- selection adapter --------------------------------------------------------


def test_calendar_selection_fires_detail_fetches(qtbot):
    # A click on a calendar row emits selected(unit); the adapter fires the same
    # detail-tab fetches the table selection uses, and sets the freshness id set
    # so only those responses may write tabs.
    window, client = make_window(qtbot)
    window._auto_refresh = True
    window._on_calendar_selected("a.timer")
    assert ("fetch_log", "user", "a.timer") in client.calls
    assert window._expected_tab_ids == {
        f"{k}:user:a.timer" for k in ("log", "details", "cat", "schedtab")
    }


# -- build fan-out (eligibility) ----------------------------------------------


def test_build_skips_projection_for_disabled_timer(qtbot):
    # A disabled timer (next=None) gets no projection fetch; a never-ran timer
    # (last=0) still gets one. The single past-runs query fires once regardless
    # (spec §9 / §4.2).
    window, client = make_window(qtbot)
    window._timers = [
        TimerRow("off.timer", "off.service", None, 123),   # disabled
        TimerRow("new.timer", "new.service", 999, 0),       # never ran
    ]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}
    window._build_calendar(1_781_000_000_000_000, 1_781_086_400_000_000)
    projs = [c for c in client.calls if c[0] == "fetch_cal_projection"]
    assert not any(c[2] == "off.timer" for c in projs)   # disabled → no projection
    assert any(c[2] == "new.timer" for c in projs)        # enabled → projection
    assert len([c for c in client.calls if c[0] == "fetch_cal_journal"]) == 1
