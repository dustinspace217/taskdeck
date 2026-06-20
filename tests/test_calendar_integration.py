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
from datetime import UTC, datetime

from PySide6.QtCore import QObject, Signal

from taskdeck.main_window import MainWindow
from taskdeck.systemd_client import ScheduleInfo, TimerRow


def _usec(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()) * 1_000_000


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


# -- build fan-IN (responses → set_events) ------------------------------------


def test_calendar_fan_in_assembles_runs_and_gaps_into_set_events(qtbot):
    # The fan-IN half: once BOTH the single journal query and the per-timer
    # projection land, _finalize_calendar must compute gaps and push the
    # assembled events into the view. This pins the host wiring (journal→ran,
    # projection→slots, finalize→gap+set_events) that the model unit tests can't
    # reach — a regression in the dispatch routing or finalize would slip past
    # the fan-OUT tests above. (Found as a coverage gap in Phase-2 review.)
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    slot1 = _usec(2026, 6, 15, 6)           # has a run → "ran"
    slot2 = _usec(2026, 6, 16, 6)           # no run, in coverage → "gap"
    win_start = _usec(2026, 6, 14, 0)
    win_end = now = _usec(2026, 6, 17, 0)

    # Capture what the view is ultimately handed.
    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append((a, k))  # type: ignore[method-assign]

    window._cal_now = now  # finalize clamps gaps to [win_start, _cal_now]
    window._build_calendar(win_start, win_end)
    window._cal_now = now  # _build_calendar recomputes now from the clock; pin it

    # Projection: parse_projection reads only the "(in UTC):" lines → two slots.
    proj = (
        "  Original form: *-*-* 06:00:00\n"
        "    Next elapse: Mon 2026-06-15 06:00:00 PDT\n"
        "       (in UTC): Mon 2026-06-15 06:00:00 UTC\n"
        "   Iteration #2: Tue 2026-06-16 06:00:00 PDT\n"
        "       (in UTC): Tue 2026-06-16 06:00:00 UTC\n"
    )
    # Journal: one FAILED run at slot1 for the activated service.
    journal = (
        '{"USER_UNIT":"new.service","JOB_RESULT":"failed",'
        f'"__REALTIME_TIMESTAMP":"{slot1}"}}\n'
    )
    window._on_finished("calproj:user:new.timer", proj)
    window._on_finished("caljournal:user", journal)

    assert len(handed) == 1, "set_events fires exactly once, after BOTH land"
    events = handed[0][0][0]
    ran = [e for e in events if e.kind == "ran"]
    gaps = [e for e in events if e.kind == "gap"]
    assert any(e.unit == "new.timer" and e.result == "failure" and e.when == slot1
               for e in ran), "journal failure parsed + bucketed to its timer"
    assert any(e.unit == "new.timer" and e.when == slot2 for e in gaps), \
        "slot2 had no run within coverage → a gap (projection→slots→finalize wired)"
