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


# (HEALTH-strip + filter-strip tests for Task 11 are at the END of this file.)


# -- Month grid view (Task 9) ------------------------------------------------
#
# The Month paint lays the visible calendar month as a weeks-x-weekdays grid
# (NOT timer rows): each cell is one DAY, summarising all timers' events for
# that day into a worst-outcome glyph + count, drawn only on problem days. A
# week-row containing any gap/failure gets a heavy border (tracked as the
# testable set `_problem_weeks`), future cells are dimmed, and clean days stay
# quiet. Like the other views these pin BEHAVIOR (paints without raising; the
# problem-week state is computed; click still emits a unit) and the small
# `_day_cell_summary` helper is pinned DIRECTLY — not the pixel layout, which
# Dustin iterates by eye post-build. Variable month length (28 vs 31 days) is
# exercised with a real February window AND a real 31-day-March window so the
# grid math can't silently assume a fixed 30-day step.

# Real UTC month boundaries (computed once; both months happen to start on a
# Sunday in 2026, which exercises the leading-blank cells of a Monday-led grid).
FEB1_USEC = 1_769_904_000_000_000      # 2026-02-01 00:00 UTC (Feb has 28 days)
FEB10_USEC = 1_770_724_800_000_000     # 2026-02-10 12:00 UTC (mid-month, a Tue)
FEB28_USEC = 1_772_236_800_000_000     # 2026-02-28 00:00 UTC (last day of Feb)
MAR1_USEC = 1_772_323_200_000_000      # 2026-03-01 00:00 UTC (March has 31 days)
MAR15_USEC = 1_773_576_000_000_000     # 2026-03-15 12:00 UTC (mid-month)
MAR31_USEC = 1_774_915_200_000_000     # 2026-03-31 00:00 UTC (31st — only a
#                                        31-day month has this cell)
APR1_USEC = 1_775_001_600_000_000      # 2026-04-01 00:00 UTC (Feb/Mar window end)


def test_month_view_renders_february_with_problem_days(qtbot):
    # A February (28-day) month with a failure day and a gap day must rasterize
    # without raising, and the week-rows holding those days must be flagged in
    # _problem_weeks (the testable seam for the heavy-border draw). Feb10 and
    # Feb28 fall in different week-rows, so two distinct weeks light up.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_events(
        [
            CalendarEvent("a.timer", FEB10_USEC, "ran", "failure"),
            CalendarEvent("b.timer", FEB28_USEC, "gap"),
            CalendarEvent("a.timer", FEB1_USEC, "ran", "success"),  # clean day
        ],
        units=["a.timer", "b.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=MAR1_USEC,  # whole month is in the past → nothing dimmed
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0  # painted the month grid without raising
    assert w.mode == "month"
    # Two problem days in two different week-rows → two flagged weeks.
    assert len(w._problem_weeks) == 2


def test_month_view_clean_month_has_no_problem_weeks(qtbot):
    # A month whose only events are successes/projections must leave
    # _problem_weeks EMPTY — the heavy border is reserved for gaps/failures, so
    # a healthy month stays quiet (the de-noising the spec asks for).
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_events(
        [
            CalendarEvent("a.timer", FEB10_USEC, "ran", "success"),
            CalendarEvent("a.timer", FEB28_USEC, "projected"),
        ],
        units=["a.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=FEB1_USEC,
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0
    assert w._problem_weeks == set()


def test_month_view_renders_thirty_one_day_month(qtbot):
    # The 31st of March only exists in a 31-day month — placing a failure there
    # proves the grid extends to day 31 (a fixed 30-day step would drop it). The
    # March window must render and flag the final week as a problem week.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_events(
        [
            CalendarEvent("a.timer", MAR15_USEC, "ran", "success"),
            CalendarEvent("a.timer", MAR31_USEC, "ran", "failure"),  # day 31
        ],
        units=["a.timer"],
        window_start=MAR1_USEC,
        window_end=APR1_USEC,
        now=APR1_USEC,
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0
    # The day-31 failure must land in some week-row → at least one problem week.
    assert len(w._problem_weeks) >= 1


def test_month_day_cell_summary_picks_worst_and_counts(qtbot):
    # The per-day summary is the Month cell's core semantic and the seam the
    # heavy-border + glyph draw both read. It returns (glyphs, worst, counts):
    # `worst` is the worst CalendarEvent (failure > gap > success > upcoming),
    # `counts` is (total, failures) from the model's bucket_cell. Pinned directly
    # so the contract survives the visual pass. Mixed-timer day: a failed run
    # must win the worst slot, and the failure count must be exact.
    w = CalendarView()
    qtbot.addWidget(w)
    ran_ok = CalendarEvent("a.timer", 1, "ran", "success")
    ran_fail = CalendarEvent("b.timer", 2, "ran", "failure")
    gap = CalendarEvent("c.timer", 3, "gap")
    glyphs, worst, counts = w._day_cell_summary([ran_ok, ran_fail, gap])
    assert worst is ran_fail            # failure dominates the day
    assert counts == (3, 1)             # 3 events, 1 of them a failure
    assert isinstance(glyphs, str) and glyphs  # a non-empty readout for the cell
    # An empty day → no worst, zero counts, empty readout (a clean day stays
    # quiet: the painter draws nothing for it).
    glyphs0, worst0, counts0 = w._day_cell_summary([])
    assert worst0 is None and counts0 == (0, 0) and glyphs0 == ""


def test_month_view_click_emits_worst_outcome_unit(qtbot):
    # Clicking a day-cell emits the unit of that day's WORST-outcome event so the
    # host opens the detail tabs for the timer that actually failed/missed (not a
    # healthy sibling). The Month grid is day-cells, not timer-rows, so the
    # hit-test maps the click to a day and resolves the unit via _day_cell_summary
    # — distinct from Day/Week's row hit-test, but the same outward `selected`
    # contract. We click the cell of the failure day via its testable hit-point.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_events(
        [
            CalendarEvent("a.timer", FEB10_USEC, "ran", "success"),
            CalendarEvent("b.timer", FEB10_USEC, "ran", "failure"),  # worst on day 10
        ],
        units=["a.timer", "b.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=MAR1_USEC,
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    seen: list[str] = []
    w.selected.connect(seen.append)
    qtbot.mouseClick(
        w, Qt.MouseButton.LeftButton, pos=w.day_hit_point(FEB10_USEC)
    )
    assert seen == ["b.timer"]  # the failing timer, not the healthy a.timer


# -- Month by-timer matrix toggle (Task 10) ----------------------------------
#
# The matrix is a Month SUB-VIEW (grid <-> matrix toggle, NOT a new top-level
# View entry): rows = timers, cols = days, each cell the timer's worst outcome
# for that day. Its diagnostic point is that a lone failure and a lone gap must
# read DIFFERENTLY per timer (the spec's "lone ✘ vs lone ⛌"), and a high-volume
# (aggregate) timer collapses to a solid ▦ band instead of a glyph storm — the
# same over-full rule Day/Week use via the model's bucket_cell. Like every other
# view these pin BEHAVIOR — paints without raising, the per-row cells are
# distinguishable, the band fires for an over-full row, the sub-toggle is
# Month-only, and clicks still emit the row's unit — not the pixel layout, which
# Dustin iterates by eye post-build. The matrix shares the Month 30-day window,
# so events are placed on real February day boundaries (reusing the FEB*
# constants above) to exercise the per-day bucketing.


def test_matrix_view_renders_rows_by_timer(qtbot):
    # A matrix paint with two timers, each with one event on a distinct day, must
    # rasterize without raising — this exercises the rows=timers × cols=days
    # placement for the Month window.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("matrix")
    w.set_events(
        [
            CalendarEvent("a.timer", FEB10_USEC, "ran", "failure"),
            CalendarEvent("b.timer", FEB28_USEC, "gap"),
        ],
        units=["a.timer", "b.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=MAR1_USEC,
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0  # painted the by-timer matrix without raising
    assert w.mode == "matrix"


def test_matrix_row_cells_distinguish_failure_from_gap(qtbot):
    # THE core matrix contract: a timer with one failure and a timer with one gap
    # must produce DIFFERENT row content. We pin it through the testable per-row
    # summary _matrix_row_cells(unit) -> list[str]: one cell-readout per day, so
    # the failure row carries the ✘ glyph on its day and the gap row the ⛌ glyph —
    # the "lone ✘ vs lone ⛌" the spec wants legible per timer.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("matrix")
    w.set_events(
        [
            CalendarEvent("fail.timer", FEB10_USEC, "ran", "failure"),
            CalendarEvent("gap.timer", FEB10_USEC, "gap"),
        ],
        units=["fail.timer", "gap.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=MAR1_USEC,
    )
    fail_cells = w._matrix_row_cells("fail.timer")
    gap_cells = w._matrix_row_cells("gap.timer")
    # Both rows span the same number of day-columns (the month's days)...
    assert len(fail_cells) == len(gap_cells)
    # ...but their CONTENT differs: the failure glyph appears in one, the gap
    # glyph in the other, and neither row is the other's content.
    assert any("✘" in c for c in fail_cells)
    assert any("⛌" in c for c in gap_cells)
    assert fail_cells != gap_cells
    # The non-event days are quiet in both (no glyph), so a row's signal is only
    # its real outcome day(s).
    assert "✘" not in "".join(gap_cells)
    assert "⛌" not in "".join(fail_cells)


def test_matrix_aggregate_timer_renders_as_band(qtbot):
    # A high-volume ("aggregate") timer — more events than CELL_DRAW_MAX in a
    # single day — collapses to ONE solid ▦ band row rather than per-day glyphs,
    # the same over-full rule Day/Week apply via the model's bucket_cell. The band
    # readout is a single cell carrying the ▦ count, distinct from the per-day
    # cell list a normal timer produces.
    from taskdeck.calendar_model import CELL_DRAW_MAX

    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("matrix")
    # CELL_DRAW_MAX + 1 runs all on the same day → over-full → aggregate band.
    busy = [
        CalendarEvent("busy.timer", FEB10_USEC + i * 60_000_000, "ran", "success")
        for i in range(CELL_DRAW_MAX + 1)
    ]
    w.set_events(
        busy,
        units=["busy.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=MAR1_USEC,
    )
    cells = w._matrix_row_cells("busy.timer")
    # The whole over-full row collapses to a single ▦ band cell, not a per-day
    # list — that is how the aggregate timer reads as a band.
    assert len(cells) == 1
    assert "▦" in cells[0]
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    assert w.grab().width() > 0  # the band row paints without raising


def test_matrix_subtoggle_is_month_only(qtbot):
    # The grid<->matrix sub-toggle is a MONTH affordance, never a top-level View
    # entry: it is visible only when the active mode is month/matrix and hidden in
    # Day/Week. This keeps the top-level Day/Week/Month toggle (and the host's
    # view_box) free of a fourth entry — matrix is reached only from within Month.
    w = CalendarView()
    qtbot.addWidget(w)
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    w.set_mode("day")
    assert not w._matrix_toggle.isVisible()  # hidden outside Month
    w.set_mode("month")
    assert w._matrix_toggle.isVisible()      # visible in the Month family
    w.set_mode("matrix")
    assert w._matrix_toggle.isVisible()      # still the Month family
    w.set_mode("week")
    assert not w._matrix_toggle.isVisible()  # hidden again


def test_matrix_click_emits_row_unit(qtbot):
    # The matrix is a timer-row layout (like Day/Week), so a click maps to a row
    # → that timer's unit via the shared row hit-test — the same outward selected
    # contract, distinct from Month's day-cell hit-test.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("matrix")
    w.set_events(
        [CalendarEvent("a.timer", FEB10_USEC, "ran", "success")],
        units=["a.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=MAR1_USEC,
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    seen: list[str] = []
    w.selected.connect(seen.append)
    qtbot.mouseClick(w, Qt.MouseButton.LeftButton, pos=w.row_hit_point(0))
    assert seen == ["a.timer"]


# -- HEALTH strip + Month filter strip (Task 11) -----------------------------
#
# The HEALTH strip is a top readout summarising the visible window (counts +
# issues) computed by the model's summarize(); it appears above the canvas in
# every mode. The Month filter strip is a Month-grid-only set of toggles that set
# `_filter` (None/"fail"/"gap"/"upcoming") so the paint DIMS non-matching cells,
# letting the user isolate "show me only the failures". Like the other view
# tests these pin BEHAVIOR — the strip text reflects summarize, setting a filter
# updates `_filter`, and a filtered Month paint runs without raising — not the
# pixel layout (iterated by eye post-build per Dustin).


def test_health_strip_reflects_summarize(qtbot):
    # set_events must roll the events through the model's summarize() and surface
    # the counts in the strip — a failure and a gap in the window mean the strip
    # reads non-clean. We assert on the strip's text (its outward state) and the
    # cached Health, not pixels: a failed run and a gap region both register.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("day")
    base = 1_781_000_000_000_000
    w.set_events(
        [
            CalendarEvent("a.timer", base, "ran", "success"),
            CalendarEvent("a.timer", base + 3_600_000_000, "ran", "failure"),
            CalendarEvent("b.timer", base + 7_200_000_000, "gap"),
        ],
        units=["a.timer", "b.timer"],
        window_start=base,
        window_end=base + DAY_USEC,
        now=base + DAY_USEC,
    )
    text = w._health_label.text()
    # The strip shows the failed + gap counts so a problem window is readable at a
    # glance; exact formatting is the visual pass, the counts are the contract.
    assert "1" in text          # the failure count surfaces in the strip text
    assert w._health.failed == 1 and w._health.gaps == 1


def test_month_filter_sets_filter_and_paints(qtbot):
    # Selecting a Month filter must set `_filter` to the chosen kind and trigger a
    # filtered paint that DOESN'T raise (dimming non-matching cells is a colour
    # change inside paintEvent, so the smoke is "still rasterizes"). We drive the
    # public set_filter API the strip buttons call, across each filter value.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_events(
        [
            CalendarEvent("a.timer", FEB10_USEC, "ran", "failure"),
            CalendarEvent("b.timer", FEB28_USEC, "gap"),
            CalendarEvent("c.timer", FEB1_USEC, "projected"),
        ],
        units=["a.timer", "b.timer", "c.timer"],
        window_start=FEB1_USEC,
        window_end=MAR1_USEC,
        now=FEB10_USEC,
    )
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    for kind in ("fail", "gap", "upcoming", None):
        w.set_filter(kind)
        assert w._filter == kind
        assert w.grab().width() > 0  # filtered month paint rasterizes, no raise


def test_filter_strip_is_month_only(qtbot):
    # The filter strip is a Month-GRID affordance (it dims Month grid cells); it
    # makes no sense in Day/Week/matrix and must hide there so the chrome stays
    # clean — the same Month-only discipline as the grid<->matrix sub-toggle.
    w = CalendarView()
    qtbot.addWidget(w)
    w.resize(1100, 600)
    w.show()
    qtbot.waitExposed(w)
    w.set_mode("day")
    assert not w._filter_strip.isVisible()   # hidden outside Month grid
    w.set_mode("month")
    assert w._filter_strip.isVisible()       # visible in the Month grid
    w.set_mode("matrix")
    assert not w._filter_strip.isVisible()   # the matrix has no per-cell dim
    w.set_mode("week")
    assert not w._filter_strip.isVisible()


def test_set_filter_ignores_unknown_value(qtbot):
    # A bad filter string must be ignored (keep the current filter), never raise —
    # mirrors set_mode's tolerance of unknown modes. Guards a future caller from
    # wedging the paint with a typo'd kind.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_filter("fail")
    w.set_filter("bogus")          # unrecognized → no change
    assert w._filter == "fail"


def test_set_mode_away_from_month_clears_filter(qtbot):
    # Leaving the Month grid clears any active filter so it can't silently dim a
    # later return to Month (the filter is a Month-grid-only state). Switching to
    # Day after filtering by "fail" must reset `_filter` to None.
    w = CalendarView()
    qtbot.addWidget(w)
    w.set_mode("month")
    w.set_filter("fail")
    assert w._filter == "fail"
    w.set_mode("day")
    assert w._filter is None
