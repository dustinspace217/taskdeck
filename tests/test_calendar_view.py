"""Offscreen widget tests for CalendarView.

These pin BEHAVIOR, not pixels (per Dustin: the visual layout is iterated by eye
after the build). The contract under test: the Day paint runs without raising,
a click on a timer row emits `selected(unit)`, and the nav chrome updates the
window and emits `rebuild(start, end)`. Events are injected via `set_events`
(no fetch), mirroring how test_smoke.py injects rows directly — the offscreen
QPA from conftest renders headlessly so grab() rasterizes without a display.
"""
from PySide6.QtCore import Qt

from taskdeck.calendar_model import CalendarEvent
from taskdeck.calendar_view import CalendarView

DAY_USEC = 86_400_000_000  # µs in a day; the Day view's window span


def test_day_view_renders_and_grabs(qtbot):
    # A Day paint with a successful run and a gap must rasterize without raising
    # — the whole point of the offscreen smoke is to catch a paint that throws.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("day")
    base = 1_781_000_000_000_000
    w.set_events(
        [
            CalendarEvent("a.timer", base, "ran", "success"),
            CalendarEvent("a.timer", base + 7_200_000_000, "gap"),
        ],
        units=["a.timer"],
        window_start=base,
        window_end=base + DAY_USEC,
        now=base + DAY_USEC,
    )
    w.resize(1000, 400)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0  # painted without raising


def test_click_emits_selected_unit(qtbot):
    # Clicking a timer's row hit-point must emit selected(unit) so the host can
    # feed the existing detail tabs — the calendar's only outward selection path.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("day")
    base = 1_781_000_000_000_000
    w.set_events(
        [CalendarEvent("a.timer", base, "ran", "success")],
        units=["a.timer"],
        window_start=base,
        window_end=base + DAY_USEC,
        now=base + DAY_USEC,
    )
    w.resize(1000, 400)
    w.show()
    qtbot.waitExposed(w)
    seen: list[str] = []
    w.selected.connect(seen.append)
    # Click the a.timer row (row 0) at its hit-point in the painted area:
    qtbot.mouseClick(w, Qt.MouseButton.LeftButton, pos=w.row_hit_point(0))
    assert seen == ["a.timer"]


def test_mode_and_window_are_readable(qtbot):
    # The host reads mode/window_start/window_end to drive fetches; pin them as
    # read-only props reflecting what set_mode/set_events last stored.
    w = CalendarView()
    qtbot.addWidget(w)
    base = 1_781_000_000_000_000
    w.set_mode("day")
    w.set_events(
        [],
        units=[],
        window_start=base,
        window_end=base + DAY_USEC,
        now=base + DAY_USEC,
    )
    assert w.mode == "day"
    assert w.window_start == base
    assert w.window_end == base + DAY_USEC


def test_next_nav_shifts_window_and_emits_rebuild(qtbot):
    # The ▸ button advances the window by one Day span and asks the host to
    # refetch for the new range via rebuild(start, end). Owning nav state in the
    # widget keeps it off the table's selection-restore path (spec §7).
    w = CalendarView()
    qtbot.addWidget(w)
    base = 1_781_000_000_000_000
    w.set_mode("day")
    w.set_events([], units=[], window_start=base, window_end=base + DAY_USEC,
                 now=base + DAY_USEC)
    rebuilds: list[tuple[int, int]] = []
    w.rebuild.connect(lambda s, e: rebuilds.append((s, e)))
    w.nav_next()
    assert w.window_start == base + DAY_USEC
    assert w.window_end == base + 2 * DAY_USEC
    assert rebuilds == [(base + DAY_USEC, base + 2 * DAY_USEC)]


def test_prev_nav_shifts_window_back(qtbot):
    # ◂ moves the window one span earlier (bounded by journal coverage at the
    # host level — the widget just shifts; the host decides how far back is data).
    w = CalendarView()
    qtbot.addWidget(w)
    base = 1_781_000_000_000_000
    w.set_mode("day")
    w.set_events([], units=[], window_start=base, window_end=base + DAY_USEC,
                 now=base + DAY_USEC)
    rebuilds: list[tuple[int, int]] = []
    w.rebuild.connect(lambda s, e: rebuilds.append((s, e)))
    w.nav_prev()
    assert w.window_start == base - DAY_USEC
    assert w.window_end == base
    assert rebuilds == [(base - DAY_USEC, base)]


# -- Week view (Task 8) ------------------------------------------------------
#
# The Week paint lays timer rows against 7 day-columns. Like the Day tests these
# pin BEHAVIOR — the week renders without raising, a day-cell holding a failure
# AND a gap paints (showing the worst outcome), and clicks still hit-test rows —
# not the pixel layout (iterated by eye post-build per Dustin).

WEEK_USEC = 7 * DAY_USEC  # the Week view's window span: 7 days


def test_week_view_renders_seven_days(qtbot):
    # A Week paint with runs spread across multiple day-columns must rasterize
    # without raising — events on day 0, day 3, and day 6 exercise the column
    # placement math (window_start..+7d mapped to 7 columns).
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("week")
    base = 1_781_000_000_000_000
    w.set_events(
        [
            CalendarEvent("a.timer", base, "ran", "success"),
            CalendarEvent("a.timer", base + 3 * DAY_USEC, "ran", "success"),
            CalendarEvent("a.timer", base + 6 * DAY_USEC, "projected"),
        ],
        units=["a.timer"],
        window_start=base,
        window_end=base + WEEK_USEC,
        now=base + 4 * DAY_USEC,
    )
    w.resize(1100, 400)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0  # painted without raising
    assert w.mode == "week"


def test_week_cell_with_failure_and_gap_paints(qtbot):
    # One day-cell holding BOTH a failed run and a missed slot (gap) must paint —
    # the cell shows the WORST outcome, and the sibling gap on the same day must
    # not crash the per-cell summary. This is the de-noising case: a failure in a
    # day must surface, never be hidden behind a tidy success.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("week")
    base = 1_781_000_000_000_000
    day2 = base + 2 * DAY_USEC
    w.set_events(
        [
            CalendarEvent("a.timer", day2, "ran", "failure"),
            CalendarEvent("a.timer", day2 + 3_600_000_000, "gap"),
            CalendarEvent("a.timer", base, "ran", "success"),
        ],
        units=["a.timer"],
        window_start=base,
        window_end=base + WEEK_USEC,
        now=base + WEEK_USEC,
    )
    w.resize(1100, 400)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0  # painted both the failure and the gap day


def test_week_day_cell_worst_picks_failure_over_success(qtbot):
    # The per-cell worst-outcome picker is the cell's core semantic and the seam
    # the de-noising relies on: a failed run must win over a success in the same
    # day so the cell glyph is the dominant ✘, never the receding ✔. Pinning the
    # helper directly (not pixels) keeps the contract stable across the visual
    # pass. Ordering: failure > gap > success > projected/approx > (empty None).
    w = CalendarView()
    qtbot.addWidget(w)
    ran_ok = CalendarEvent("a.timer", 1, "ran", "success")
    ran_fail = CalendarEvent("a.timer", 2, "ran", "failure")
    gap = CalendarEvent("a.timer", 3, "gap")
    proj = CalendarEvent("a.timer", 4, "projected")
    assert w._day_cell_worst([ran_ok, ran_fail, gap]) is ran_fail
    assert w._day_cell_worst([ran_ok, gap]) is gap
    assert w._day_cell_worst([ran_ok, proj]) is ran_ok
    assert w._day_cell_worst([proj]) is proj
    assert w._day_cell_worst([]) is None


def test_week_view_click_still_emits_selected_unit(qtbot):
    # Row hit-testing is mode-agnostic (shared _TOP_PAD/_ROW_H geometry), so a
    # click in Week mode must emit selected(unit) exactly like Day — the host
    # wires it to the same detail-tab fetches regardless of the active sub-view.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("week")
    base = 1_781_000_000_000_000
    w.set_events(
        [CalendarEvent("a.timer", base, "ran", "success")],
        units=["a.timer"],
        window_start=base,
        window_end=base + WEEK_USEC,
        now=base + WEEK_USEC,
    )
    w.resize(1100, 400)
    w.show()
    qtbot.waitExposed(w)
    seen: list[str] = []
    w.selected.connect(seen.append)
    qtbot.mouseClick(w, Qt.MouseButton.LeftButton, pos=w.row_hit_point(0))
    assert seen == ["a.timer"]
