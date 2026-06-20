"""CalendarView: the custom-painted calendar widget (Qt Widgets, QPainter).

This is the ONLY new widget for the calendar feature. It owns its own nav/window
state (mode + visible window) so the calendar never touches the table's
`_render_rows`/selection-restore path (spec §7) — the two views are fully
independent pages of a QStackedWidget in main_window.

All thresholds and time math are IMPORTED from calendar_model, never redefined
here: calendar_model is the pure, headless core the Phase-2 plasmoid reuses, so a
threshold living in the view would silently fork between the two consumers
(Global Constraints). The view only knows how to DRAW events and turn clicks into
a `selected(unit)` signal; deciding what an over-full cell collapses to, or how
many slots to project, stays in the model (`bucket_cell`, the CELL_DRAW_* caps).

Why a custom-painted QWidget rather than a QTableWidget / QGraphicsView: the
layout is a timer-rows × time-axis grid with per-cell glyphs and an aggregate
band, which a cell-widget table can't express without one widget per glyph
(thousands of widgets for a busy month). A single paintEvent over a flat event
list is both lighter and the shape the plasmoid will mirror. Pixel positions are
iterated visually post-build (Dustin's call) — this v1 pins BEHAVIOR (paints
without raising, click→signal, nav→window) and leaves exact spacing to that pass.
"""
from __future__ import annotations

import calendar
from datetime import UTC, date, datetime

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from taskdeck.calendar_model import CELL_DRAW_MAX, CalendarEvent, bucket_cell

# The span (µs) the nav arrows shift by, per mode. Day/Week are exact; Month is
# a NOMINAL 30 days — the exact month boundary is the host's job when it sizes
# projections (cadence_interval_usec already treats "monthly" as ~30d), so the
# widget's job is only to move the window by a sensible step and ask the host to
# refetch. Keys match the mode strings accepted by set_mode.
_DAY_USEC = 86_400_000_000
_MODE_SPAN_USEC: dict[str, int] = {
    "day": _DAY_USEC,
    "week": 7 * _DAY_USEC,
    "month": 30 * _DAY_USEC,
    # "matrix" shares the Month window — it's a Month sub-toggle (spec §5), so it
    # navigates by the same 30-day step.
    "matrix": 30 * _DAY_USEC,
}

# Glyph + color per (kind, result), straight from spec §6's visual grammar.
# Both glyph AND color carry the meaning so the readout is colorblind-safe, and
# the healthy majority is deliberately LOW-CONTRAST (muted green ✔, dim ⏲) so a
# failure ✘ or gap ⛌ draws the eye — the de-noising the spec calls an expected
# post-build iteration. Stored as a table (not an if-ladder) so the Week/Month
# paints in later tasks reuse the exact same mapping via _glyph/_color.
_RAN_OK = ("✔", QColor(120, 160, 120))      # muted green — receding
_RAN_FAIL = ("✘", QColor(200, 90, 90))      # muted red — dominant
_GAP = ("⛌", QColor(210, 160, 70))          # amber — due but missed
_PROJECTED = ("⏲", QColor(140, 140, 150))   # dim — upcoming
_APPROX = ("◇", QColor(140, 140, 150))      # monotonic single next-run
_UNKNOWN = ("·", QColor(110, 110, 110))     # anything unrecognized — never crash


def _glyph_color(event: CalendarEvent) -> tuple[str, QColor]:
    """Map one CalendarEvent to its (glyph, color) per spec §6.

    `event` comes from calendar_model (parse_run_journal / compute_gaps /
    projection). The fall-through to `_UNKNOWN` is load-bearing: an unrecognized
    kind must render as a neutral dot, never raise inside paintEvent (a throwing
    paint blanks the whole widget). 'ran' splits on result so a failed run is the
    dominant red, not lumped with successes.
    """
    if event.kind == "ran":
        return _RAN_FAIL if event.result == "failure" else _RAN_OK
    if event.kind == "gap":
        return _GAP
    if event.kind == "projected":
        return _PROJECTED
    if event.kind == "approx":
        return _APPROX
    return _UNKNOWN


# Severity rank for picking the WORST event in a day-cell (Week/Month collapse
# several events into one glyph). Higher = worse, so a failed run dominates a
# success in the same day — the de-noising the spec asks for (✘ must never hide
# behind a tidy ✔). Order: failure > gap (missed slot) > success > upcoming
# (projected/approx). Anything unrecognized ranks lowest so it never masks a real
# outcome. Lives next to _glyph_color because both encode the same visual grammar
# (which signal wins the eye), and Week/Month/matrix all reuse this one ranking.
_SEVERITY: dict[tuple[str, str], int] = {
    ("ran", "failure"): 4,
    ("gap", ""): 3,
    ("ran", "success"): 2,
    ("projected", ""): 1,
    ("approx", ""): 1,
}


def _event_severity(event: CalendarEvent) -> int:
    """Severity rank of one event for worst-outcome selection (higher = worse).

    Keyed on (kind, result) so a failed run outranks a success. `gap` and the
    upcoming kinds ('projected'/'approx') carry no result, so they look up under
    the empty-string result. Unknown shapes fall through to 0 — never let an
    unrecognized event outrank or mask a real failure.
    """
    return _SEVERITY.get((event.kind, event.result), 0)


# Layout constants for the painted canvas. These are STARTING values for the
# visual-iteration pass, not load-bearing contract — the tests pin behavior, not
# these numbers. Named so the iteration pass has one place to tune.
_GUTTER_W = 160      # left column width for the timer-unit label
_ROW_H = 28          # height of one timer row in the canvas
_TOP_PAD = 24        # space above row 0 for the hour axis / header
_HEALTH_COL_W = 64   # right-edge column showing one row's worst-of-week summary

# Month-grid layout. Unlike Day/Week (timer-rows down a gutter), the Month view
# is a classic weeks-x-weekdays calendar: each CELL is one day, summarising ALL
# timers for that day. So it has its own coordinate space — these constants and
# _month_cell_rect are the single source of truth shared by the paint and the
# day hit-test (so a click always lands on the cell drawn). Starting values for
# the visual-iteration pass; behaviour is pinned by tests, not these numbers.
_MONTH_TOP_PAD = 24     # space above the grid for the weekday header (Mon..Sun)
_MONTH_GRID_PAD = 6     # left/right inset of the grid from the widget edges


class CalendarView(QWidget):  # type: ignore[misc]
    """The calendar page: nav chrome on top, a custom-painted grid below.

    Outward contract (consumed by main_window in Task 7):
    - `selected = Signal(str)` — the timer unit a click landed on; the host wires
      it to the same detail-tab fetches the table selection uses.
    - `rebuild = Signal(qlonglong, qlonglong)` — (window_start, window_end) after
      a nav move; the host refetches journal+projection for the new window.
      Emitting the window (not just "moved") lets the host fetch exactly what's
      visible. qlonglong (not int): the µs epochs exceed Qt's 32-bit `int`.
    - read-only props `mode`, `window_start`, `window_end` — what the host reads
      to scope its fetches.

    Data flows IN via `set_events` (no fetch here — the host does I/O and injects
    the parsed events), matching how the table is fed and keeping the widget
    testable offscreen with zero subprocesses.
    """

    selected = Signal(str)
    # qlonglong, NOT int: a µs epoch (~1.78e15) overflows Qt's 32-bit C `int`,
    # which libshiboken silently truncates on emit (the plan's `Signal(int,int)`
    # is a typo — verified: the truncated value reached the slot). qlonglong is
    # Qt's 64-bit integer and round-trips a µs epoch to Python int unchanged.
    rebuild = Signal("qlonglong", "qlonglong")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Nav/window state — OWNED here (spec §7) so a 10s tick or an arrow click
        # never disturbs the table. Defaults are harmless zeros until the host's
        # first set_events; _mode gates which _paint_* runs.
        self._mode = "day"
        self._window_start = 0
        self._window_end = 0
        self._now = 0
        self._events: list[CalendarEvent] = []
        self._units: list[str] = []
        # Week-row indices (0-based, top of the visible month down) that contain
        # at least one gap or failed run. Recomputed each Month paint and read by
        # tests as the testable seam for the heavy-border draw — it lets a test
        # assert "this month flagged N problem weeks" without inspecting pixels.
        # Empty for any non-Month paint and for a clean month.
        self._problem_weeks: set[int] = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._build_chrome(outer)

        # The painted canvas is THIS widget below the chrome row. Rather than a
        # separate child widget we paint directly in paintEvent and reserve the
        # chrome's height via _canvas_top(), so hit-testing and painting share one
        # coordinate space — a child canvas would need its own event forwarding.
        outer.addStretch(1)

    # -- construction ---------------------------------------------------------

    def _build_chrome(self, outer: QVBoxLayout) -> None:
        """Build the top nav row: Day/Week/Month toggle + ◂ <range> ▸ [Today].

        Buttons (not a QComboBox) for the mode toggle so all three modes are
        visible at once — the spec's `[ Day │ Week │ Month ]` segmented control.
        Checkable + autoExclusive makes them behave as one radio group without a
        QButtonGroup. The arrows/Today drive nav_prev/nav_next/nav_today, which
        own the window math and emit `rebuild`.
        """
        row = QHBoxLayout()
        row.setContentsMargins(6, 4, 6, 4)

        self._mode_buttons: dict[str, QPushButton] = {}
        for label, mode in (("Day", "day"), ("Week", "week"), ("Month", "month")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)  # one-of-N without a QButtonGroup
            # Default-arg binds `mode` per-iteration; a bare closure over the loop
            # var would capture the LAST mode for every button (classic late-bind).
            btn.clicked.connect(lambda _checked=False, m=mode: self.set_mode(m))
            self._mode_buttons[mode] = btn
            row.addWidget(btn)
        self._mode_buttons["day"].setChecked(True)

        # Grid⇄matrix sub-toggle — a MONTH-only affordance, NOT a fourth top-level
        # mode button (spec §5: the matrix is reached only from within Month, so
        # the host's view_box keeps just Day/Week/Month). Checkable: unchecked =
        # the calendar grid (mode "month"), checked = the by-timer matrix (mode
        # "matrix"); both share the same 30-day window so the toggle never
        # refetches, it only swaps which _paint_* runs. Hidden in Day/Week via
        # _sync_matrix_toggle, shown in the Month family. We keep a reference so
        # set_mode can flip its checked state when the mode changes programmatically
        # (e.g. clicking Month after being in matrix should land on the grid).
        self._matrix_toggle = QPushButton("Matrix")
        self._matrix_toggle.setCheckable(True)
        self._matrix_toggle.setVisible(False)  # Day is the default mode → hidden
        self._matrix_toggle.toggled.connect(self._on_matrix_toggled)
        row.addWidget(self._matrix_toggle)

        row.addSpacing(16)
        self._prev_btn = QPushButton("◂")
        self._prev_btn.clicked.connect(self.nav_prev)
        row.addWidget(self._prev_btn)

        # The range label is updated on every set_events/nav so the user can read
        # the visible window. Kept minimal (the visual pass will format it).
        self._range_label = QLabel("")
        row.addWidget(self._range_label)

        self._next_btn = QPushButton("▸")
        self._next_btn.clicked.connect(self.nav_next)
        row.addWidget(self._next_btn)

        self._today_btn = QPushButton("Today")
        self._today_btn.clicked.connect(self.nav_today)
        row.addWidget(self._today_btn)

        row.addStretch(1)
        outer.addLayout(row)

    # -- read-only props ------------------------------------------------------
    #
    # Exposed as properties (not bare attrs) so the host reads them without being
    # able to set them — the window is mutated ONLY through set_events/nav_*,
    # which keep the range label and a repaint in sync.

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def window_start(self) -> int:
        return self._window_start

    @property
    def window_end(self) -> int:
        return self._window_end

    # -- public API -----------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Switch the active sub-view (day/week/month/matrix) and repaint.

        Unknown modes are ignored rather than raising — a bad mode string from a
        future caller should degrade to the current view, not crash the widget.
        Updates the toggle button so a programmatic set_mode keeps the chrome in
        sync with a click-driven one.
        """
        if mode not in _MODE_SPAN_USEC:
            return  # unrecognized — keep the current mode, never raise
        self._mode = mode
        # The top-level toggle only has Day/Week/Month buttons; "matrix" is a
        # Month sub-mode, so it checks the MONTH button (the user is still "in
        # Month", just viewing the matrix). .get() returns None for "matrix".
        btn = self._mode_buttons.get("month" if mode == "matrix" else mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        self._sync_matrix_toggle()
        self.update()

    def _sync_matrix_toggle(self) -> None:
        """Show/hide and check the grid⇄matrix sub-toggle to match the mode.

        Visible only in the Month family (month or matrix) — a Day/Week mode hides
        it so the sub-toggle never implies a fourth top-level view. Its CHECKED
        state mirrors the mode (matrix→checked, month→unchecked) so a programmatic
        set_mode keeps the button in sync with a click-driven one. We block its
        signals while syncing: setChecked would otherwise re-enter set_mode via
        _on_matrix_toggled and double-fire the repaint (harmless but wasteful).
        """
        in_month_family = self._mode in ("month", "matrix")
        self._matrix_toggle.setVisible(in_month_family)
        want_checked = self._mode == "matrix"
        if self._matrix_toggle.isChecked() != want_checked:
            self._matrix_toggle.blockSignals(True)
            self._matrix_toggle.setChecked(want_checked)
            self._matrix_toggle.blockSignals(False)

    def _on_matrix_toggled(self, checked: bool) -> None:
        """Switch between the Month grid and the by-timer matrix on toggle.

        `checked` is the new button state (True=matrix, False=grid). Both are the
        Month window, so this only swaps the mode (and thus which _paint_* runs) —
        no nav, no rebuild. Routed through set_mode so visibility/checked stay in
        one place; set_mode's own _sync_matrix_toggle is a no-op here since the
        button is already in the wanted state.
        """
        self.set_mode("matrix" if checked else "month")

    def set_events(
        self,
        events: list[CalendarEvent],
        units: list[str],
        window_start: int,
        window_end: int,
        now: int,
    ) -> None:
        """Inject the parsed events + the window they cover, then repaint.

        Called by the host AFTER it has fetched and parsed (via calendar_model);
        the widget does no I/O. `units` is the row order (timer unit names);
        `events` are placed against `[window_start, window_end]`; `now` positions
        the ▲ now marker. Storing then update() defers the actual draw to
        paintEvent — Qt coalesces repaints, so a burst of set_events (10s tick +
        nav) costs one paint.
        """
        self._events = list(events)
        self._units = list(units)
        self._window_start = window_start
        self._window_end = window_end
        self._now = now
        self._update_range_label()
        self.update()

    def row_hit_point(self, index: int) -> QPoint:
        """The widget-local point at the centre of timer row `index`.

        Used by tests to click a row deterministically, and the same geometry
        paintEvent and mousePressEvent use — one source of truth for where a row
        lives, so a layout tweak can't desync the hit-test from the paint. Returns
        a QPoint in the gutter (left column) where the unit label is drawn.
        """
        y = self._canvas_top() + _TOP_PAD + index * _ROW_H + _ROW_H // 2
        x = _GUTTER_W // 2
        return QPoint(x, y)

    # -- navigation -----------------------------------------------------------
    #
    # Each nav move shifts the window by the current mode's span and emits
    # `rebuild(start, end)` so the host refetches exactly the new window. The
    # widget owns this state; it never calls the host directly (spec §7).

    def nav_next(self) -> None:
        """Advance the window by one mode-span and ask the host to refetch."""
        span = _MODE_SPAN_USEC[self._mode]
        self._shift_window(span)

    def nav_prev(self) -> None:
        """Move the window one mode-span earlier (host bounds how far back data
        exists — the widget just shifts; journal coverage is the host's limit)."""
        span = _MODE_SPAN_USEC[self._mode]
        self._shift_window(-span)

    def nav_today(self) -> None:
        """Recenter the window on `now`, keeping the current span.

        Anchors the window's START at now (so 'Today' shows from now forward for
        Day) rather than centring — the simplest recenter that the visual pass can
        refine; the host still gets the new window via rebuild.
        """
        span = self._window_end - self._window_start
        if span <= 0:
            span = _MODE_SPAN_USEC[self._mode]
        self._window_start = self._now
        self._window_end = self._now + span
        self._update_range_label()
        self.update()
        self.rebuild.emit(self._window_start, self._window_end)

    def _shift_window(self, delta: int) -> None:
        """Shift both window edges by `delta` µs, repaint, and emit rebuild.

        Centralized so nav_next/nav_prev share the exact same emit contract — the
        repaint shows the (now-empty) shifted window immediately for feedback,
        and rebuild tells the host to fill it.
        """
        self._window_start += delta
        self._window_end += delta
        self._update_range_label()
        self.update()
        self.rebuild.emit(self._window_start, self._window_end)

    def _update_range_label(self) -> None:
        """Refresh the chrome's range text from the current window.

        Minimal on purpose (epoch-derived, not richly formatted) — the date
        formatting is part of the post-build visual pass; this keeps the label
        non-empty and correct so the iteration has something to refine.
        """
        if self._window_end <= self._window_start:
            self._range_label.setText("")
            return
        start = datetime.fromtimestamp(self._window_start / 1_000_000, UTC)
        end = datetime.fromtimestamp(self._window_end / 1_000_000, UTC)
        self._range_label.setText(f"{start:%b %d} – {end:%b %d}")

    # -- painting -------------------------------------------------------------

    def _canvas_top(self) -> int:
        """Y of the painted canvas's top edge = below the chrome row.

        The chrome is laid out by the QVBoxLayout; its height is the first layout
        item's geometry. Reading it (rather than a magic constant) keeps the
        canvas directly under the chrome whatever the platform font height —
        important because row_hit_point and the paint both build on this.
        """
        layout = self.layout()
        if layout is None or layout.count() == 0:
            return 0
        item = layout.itemAt(0)
        if item is None:
            return 0
        # int() pins the type: PySide6's stubs type QRect.bottom() as Any, which
        # mypy --strict rejects; the value is always an int pixel coordinate.
        return int(item.geometry().bottom())

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt naming)
        """Dispatch to the active sub-view's painter.

        Day, Week, Month and the Month by-timer matrix are all implemented; any
        other (future) mode falls back to Day so the widget always draws something
        rather than a blank page. A QPainter that escapes paintEvent un-ended can
        wedge Qt, so each _paint_* opens and ends its own painter.
        """
        if self._mode == "week":
            self._paint_week()
        elif self._mode == "day":
            self._paint_day()
        elif self._mode == "month":
            self._paint_month()
        elif self._mode == "matrix":
            self._paint_matrix()
        else:
            # Unknown future mode — draw the Day grid so the page is never blank.
            self._paint_day()

    def _paint_day(self) -> None:
        """Paint the Day view: timer rows down the gutter, an hourly axis across
        the top, one glyph per event placed by its time-of-day, a ▲ now marker,
        and an aggregate band for any row whose visible events exceed
        CELL_DRAW_MAX (collapsed via the model's bucket_cell).

        Geometry is deliberately simple (linear time→x within the window) and
        shares _canvas_top/_TOP_PAD/_ROW_H with row_hit_point so clicks land on
        what's drawn. Exact spacing/labels are the visual-iteration pass; this
        proves the data renders and never raises.
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            top = self._canvas_top()
            width = self.width()
            axis_left = _GUTTER_W
            axis_right = max(axis_left + 1, width - 8)
            span = max(1, self._window_end - self._window_start)  # avoid /0

            self._paint_hour_axis(painter, top, axis_left, axis_right)

            # One row per unit, in caller order. Events are grouped by unit so an
            # over-full row collapses to a band via the model's bucket_cell — the
            # decision of WHAT a full cell shows stays in the model.
            for i, unit in enumerate(self._units):
                row_y = top + _TOP_PAD + i * _ROW_H
                self._paint_unit_label(painter, unit, row_y)
                row_events = [e for e in self._events if e.unit == unit]
                self._paint_row_events(
                    painter, row_events, row_y, axis_left, axis_right, span
                )

            self._paint_now_marker(painter, top, axis_left, axis_right, span)
        finally:
            # End the painter even if a draw raised — an un-ended QPainter on a
            # widget can corrupt the backing store on the next paint.
            painter.end()

    def _paint_hour_axis(
        self, painter: QPainter, top: int, axis_left: int, axis_right: int
    ) -> None:
        """Draw a thin baseline for the time axis at the top of the canvas.

        Kept minimal (a single line, no hour ticks yet) — the spec's hourly ticks
        are part of the visual pass. Its presence is what proves the axis region
        is reserved correctly above row 0.
        """
        painter.setPen(QColor(90, 90, 90))
        baseline = top + _TOP_PAD - 4
        painter.drawLine(axis_left, baseline, axis_right, baseline)

    def _paint_unit_label(self, painter: QPainter, unit: str, row_y: int) -> None:
        """Draw the timer unit name in the left gutter for one row."""
        painter.setPen(QColor(200, 200, 200))
        rect = QRect(6, row_y, _GUTTER_W - 10, _ROW_H)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, unit)

    def _paint_row_events(
        self,
        painter: QPainter,
        row_events: list[CalendarEvent],
        row_y: int,
        axis_left: int,
        axis_right: int,
        span: int,
    ) -> None:
        """Place one row's events along the time axis, or collapse to a band.

        Above CELL_DRAW_MAX events the row draws ONE aggregate band (count, with a
        red tint if any failed) instead of a glyph storm — the threshold and the
        (count, failures) math both come from the model (CELL_DRAW_MAX,
        bucket_cell), so the view never re-decides what "over-full" means.
        """
        if len(row_events) > CELL_DRAW_MAX:
            count, failures = bucket_cell(row_events)
            band_color = _RAN_FAIL[1] if failures > 0 else QColor(110, 110, 120)
            painter.setPen(band_color)
            painter.fillRect(
                QRect(axis_left, row_y + 6, axis_right - axis_left, _ROW_H - 12),
                QColor(band_color.red(), band_color.green(), band_color.blue(), 60),
            )
            label = f"▦ {count}" + (f" ({failures}✘)" if failures else "")
            painter.drawText(
                QRect(axis_left + 4, row_y, axis_right - axis_left, _ROW_H),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            return

        glyph_font = QFont(self.font())
        painter.setFont(glyph_font)
        for ev in row_events:
            glyph, color = _glyph_color(ev)
            # Linear time→x: position within the window scaled across the axis.
            # Clamp into [axis_left, axis_right] so an event just outside the
            # window (a prefetch slot) still lands at the edge, never off-canvas.
            frac = (ev.when - self._window_start) / span
            frac = min(1.0, max(0.0, frac))
            x = int(axis_left + frac * (axis_right - axis_left))
            painter.setPen(color)
            painter.drawText(
                QRect(x - 8, row_y, 16, _ROW_H),
                Qt.AlignmentFlag.AlignCenter,
                glyph,
            )

    def _paint_now_marker(
        self, painter: QPainter, top: int, axis_left: int, axis_right: int, span: int
    ) -> None:
        """Draw the ▲ now marker if `now` falls inside the visible window.

        Outside the window there is nothing to mark — drawing it clamped to an
        edge would lie about where 'now' is, so it's simply omitted then.
        """
        if not (self._window_start <= self._now <= self._window_end):
            return
        frac = (self._now - self._window_start) / span
        x = int(axis_left + frac * (axis_right - axis_left))
        painter.setPen(QColor(230, 230, 120))
        painter.drawText(
            QRect(x - 8, top, 16, _TOP_PAD), Qt.AlignmentFlag.AlignCenter, "▲"
        )

    # -- week paint -----------------------------------------------------------
    #
    # The Week view is the same timer-rows-down-the-gutter layout as Day, but the
    # axis is divided into 7 fixed day-COLUMNS (Day used a continuous time→x
    # scale). Each (row, day) cell collapses that day's events for the timer into
    # one "HH glyph" readout — the worst event's hour-of-day plus its glyph —
    # plus a trailing per-row health column. Over-full cells aggregate via the
    # model's bucket_cell, exactly like Day, so a busy day never glyph-storms.

    def _day_cell_worst(self, events: list[CalendarEvent]) -> CalendarEvent | None:
        """Pick the worst-outcome event from one day-cell, or None if empty.

        `events` are a single timer's events that fall on one day-column. The
        Week cell shows ONE glyph, so a failed run must win over a same-day
        success (de-noising — the eye should land on ✘, not ✔). Ranking is
        _event_severity (failure > gap > success > upcoming); ties keep the
        earliest by list order, which is harmless since the glyph is identical.
        Returns None for an empty day so the painter draws nothing there (a clean
        day stays quiet), rather than a placeholder that adds visual noise.
        """
        if not events:
            return None
        # max() with a key is the smallest correct expression here; events is a
        # handful of items per cell, so the linear scan is trivially bounded.
        return max(events, key=_event_severity)

    def _paint_week(self) -> None:
        """Paint the Week view: timer rows × 7 day-columns + a per-row health col.

        Each cell shows the day's worst event as "HH glyph" (the worst event's
        UTC hour-of-day, then its glyph) so a single readout captures both WHEN in
        the day it happened and the WORST thing that happened. A cell whose event
        count exceeds CELL_DRAW_MAX collapses to an aggregate band via the model's
        bucket_cell (same rule as Day) instead of a misleading single glyph. The
        trailing health column shows the row's worst-of-week glyph so a problem
        timer is spottable without scanning all 7 columns.

        Geometry shares _canvas_top/_TOP_PAD/_ROW_H/_GUTTER_W with row_hit_point
        and mousePressEvent, so clicks land on the painted rows in Week mode just
        as in Day. Exact spacing/labels are the post-build visual pass; this proves
        the data renders across 7 columns and never raises.
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            top = self._canvas_top()
            width = self.width()
            grid_left = _GUTTER_W
            # Reserve the health column at the right edge; the 7 day-columns fill
            # the space between the gutter and that column.
            grid_right = max(grid_left + 1, width - _HEALTH_COL_W - 8)
            col_w = max(1, (grid_right - grid_left) // 7)

            self._paint_week_header(painter, top, grid_left, col_w)

            for i, unit in enumerate(self._units):
                row_y = top + _TOP_PAD + i * _ROW_H
                self._paint_unit_label(painter, unit, row_y)
                row_events = [e for e in self._events if e.unit == unit]
                self._paint_week_row(
                    painter, row_events, row_y, grid_left, col_w, grid_right
                )
        finally:
            # End the painter even if a draw raised — an un-ended QPainter on a
            # widget can corrupt the backing store on the next paint.
            painter.end()

    def _paint_week_header(
        self, painter: QPainter, top: int, grid_left: int, col_w: int
    ) -> None:
        """Draw the 7 day-column dividers and date labels across the top.

        Labels come from window_start + N days (UTC, matching the event epochs);
        kept minimal (a thin tick + "MMM DD") — richer formatting is the visual
        pass. Drawing the dividers is what makes the 7-column structure legible.
        """
        painter.setPen(QColor(90, 90, 90))
        baseline = top + _TOP_PAD - 4
        for day in range(7):
            x = grid_left + day * col_w
            painter.drawLine(x, top + 2, x, baseline)
            # Label each column with its date so the week is anchored in time.
            day_start = datetime.fromtimestamp(
                (self._window_start + day * _DAY_USEC) / 1_000_000, UTC
            )
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(
                QRect(x + 2, top, col_w - 4, _TOP_PAD - 4),
                Qt.AlignmentFlag.AlignVCenter,
                f"{day_start:%b %d}",
            )
            painter.setPen(QColor(90, 90, 90))

    def _paint_week_row(
        self,
        painter: QPainter,
        row_events: list[CalendarEvent],
        row_y: int,
        grid_left: int,
        col_w: int,
        grid_right: int,
    ) -> None:
        """Paint one timer's 7 day-cells plus its trailing health summary.

        Events are bucketed into day-columns by their offset from window_start
        (integer-divided by one day). Each non-empty cell draws either an "HH
        glyph" worst-outcome readout or — above CELL_DRAW_MAX events — an
        aggregate band, the same over-full rule the Day view uses (the count and
        failure flag both come from the model's bucket_cell). The health column
        at the right shows the worst event across the whole week for this row.
        """
        # Bucket the row's events into 7 day-columns. A day index outside [0, 6]
        # (an event just outside the window from a prefetch) is dropped here — it
        # has no column to land in; the health summary still considers all events.
        columns: list[list[CalendarEvent]] = [[] for _ in range(7)]
        for ev in row_events:
            day = (ev.when - self._window_start) // _DAY_USEC
            if 0 <= day < 7:
                columns[day].append(ev)

        glyph_font = QFont(self.font())
        painter.setFont(glyph_font)
        for day, cell in enumerate(columns):
            if not cell:
                continue  # clean/empty day stays quiet — no placeholder noise
            cell_x = grid_left + day * col_w
            self._paint_week_cell(painter, cell, cell_x, row_y, col_w)

        # Row-health summary column: the single worst event across the whole week
        # so a failing timer is spottable at the right edge without reading all 7
        # cells. Empty week → nothing drawn (the row is silent, which IS healthy).
        worst = self._day_cell_worst(row_events)
        if worst is not None:
            glyph, color = _glyph_color(worst)
            painter.setPen(color)
            painter.drawText(
                QRect(grid_right + 4, row_y, _HEALTH_COL_W - 6, _ROW_H),
                Qt.AlignmentFlag.AlignVCenter,
                glyph,
            )

    def _paint_week_cell(
        self,
        painter: QPainter,
        cell: list[CalendarEvent],
        cell_x: int,
        row_y: int,
        col_w: int,
    ) -> None:
        """Draw one (row, day) cell: an "HH glyph" worst readout, or a band.

        Above CELL_DRAW_MAX events the cell collapses to an aggregate band (count
        + red tint if any failed) via the model's bucket_cell — the view never
        re-decides what "over-full" means. Otherwise it shows the day's worst
        event as its UTC hour-of-day then its glyph, so one readout carries both
        time-of-day and the worst outcome (spec §6's Week cell).
        """
        if len(cell) > CELL_DRAW_MAX:
            count, failures = bucket_cell(cell)
            band_color = _RAN_FAIL[1] if failures > 0 else QColor(110, 110, 120)
            painter.fillRect(
                QRect(cell_x + 1, row_y + 6, col_w - 3, _ROW_H - 12),
                QColor(band_color.red(), band_color.green(), band_color.blue(), 60),
            )
            painter.setPen(band_color)
            label = f"▦{count}" + (f" {failures}✘" if failures else "")
            painter.drawText(
                QRect(cell_x + 3, row_y, col_w - 5, _ROW_H),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            return

        worst = self._day_cell_worst(cell)
        if worst is None:
            return
        glyph, color = _glyph_color(worst)
        hour = datetime.fromtimestamp(worst.when / 1_000_000, UTC).hour
        painter.setPen(color)
        painter.drawText(
            QRect(cell_x + 3, row_y, col_w - 5, _ROW_H),
            Qt.AlignmentFlag.AlignVCenter,
            f"{hour:02d} {glyph}",
        )

    # -- month paint ----------------------------------------------------------
    #
    # The Month view abandons the timer-rows layout for a classic calendar grid:
    # weeks down, weekdays across, one CELL per day summarising ALL timers for
    # that day. The de-noising goal drives the design — a clean day is BLANK (no
    # glyph), only a problem day (gap/failure) shows its worst glyph + count, and
    # a week-row holding any problem gets a heavy border so the eye finds trouble
    # at week granularity before reading individual cells. Future cells are dimmed
    # because they can't have outcomes yet. Variable month length is handled by
    # building the grid from the stdlib `calendar` module (monthdatescalendar),
    # never a fixed 30-day step — Feb (28/29) and 31-day months both come out
    # right. _day_cell_summary is the small, directly-tested seam both the paint
    # and the click hit-test read.

    def _day_cell_summary(
        self, events: list[CalendarEvent]
    ) -> tuple[str, CalendarEvent | None, tuple[int, int]]:
        """Summarise one day's events into (glyphs, worst, counts).

        `events` are every timer's events that fall on one calendar day (already
        bucketed by the caller). Returns:
        - `glyphs`: the short readout drawn in the cell — the worst event's glyph
          plus the day's event count (e.g. "✘ 3"), or "" for an empty day so the
          painter draws nothing (a clean day stays quiet, the spec's de-noising).
        - `worst`: the worst-outcome CalendarEvent (failure > gap > success >
          upcoming) via the same _day_cell_worst ranking the Week cell uses, or
          None for an empty day. The click hit-test reads this to pick which
          timer's detail tabs to open.
        - `counts`: (total, failures) from the model's bucket_cell — the count is
          shown next to the glyph, and a future visual pass can tint on failures.
          Bucketing in the MODEL (not here) keeps the failure-never-hidden rule
          in one place, shared with Day/Week and the plasmoid.
        Kept small and pure-ish (no painter, no self-state mutation) precisely so
        Task 9's most-complex view has a directly testable core.
        """
        worst = self._day_cell_worst(events)
        if worst is None:
            return ("", None, (0, 0))
        count, failures = bucket_cell(events)
        glyph, _color = _glyph_color(worst)
        # "<glyph> <count>" only when there's more than one event, so a single
        # event reads as just its glyph (less noise); the count earns its place
        # only when it's summarising several.
        readout = f"{glyph} {count}" if count > 1 else glyph
        return (readout, worst, (count, failures))

    def _month_anchor(self) -> date:
        """The first day of the visible month, derived from window_start (UTC).

        The host sizes the Month window to roughly a month; we take its START
        instant, read its UTC year/month, and anchor on the 1st — so the grid
        always shows a WHOLE calendar month regardless of the exact window edges.
        Using the UTC date (matching the event epochs) keeps day-bucketing and
        the grid in the same timezone, avoiding an off-by-one at month ends.
        """
        start = datetime.fromtimestamp(self._window_start / 1_000_000, UTC)
        return date(start.year, start.month, 1)

    def _month_weeks(self) -> list[list[date]]:
        """The visible month as a list of week-rows of 7 UTC dates each.

        Built with the stdlib calendar.Calendar.monthdatescalendar, which returns
        full weeks padded with the trailing/leading days of the adjacent months —
        so a 28-day Feb yields 4-6 rows and a 31-day month yields 5-6, with no
        hand-rolled length math that could mis-handle leap years or month length.
        firstweekday defaults to Monday (calendar's default), matching the Mon..Sun
        header. Bounded: a month spans at most 6 week-rows.
        """
        anchor = self._month_anchor()
        cal = calendar.Calendar()  # firstweekday=0 (Monday)
        return cal.monthdatescalendar(anchor.year, anchor.month)

    def _events_by_date(self) -> dict[date, list[CalendarEvent]]:
        """Bucket all events by their UTC calendar date.

        One pass over the flat event list (Power of Ten rule 2: bounded by the
        event count, itself bounded by the projection cap). The Month grid then
        looks each cell's date up here, so a day with no events is simply absent
        from the dict and draws blank. UTC matches the grid dates from
        _month_weeks, so an event never lands a day off.
        """
        by_date: dict[date, list[CalendarEvent]] = {}
        for ev in self._events:
            d = datetime.fromtimestamp(ev.when / 1_000_000, UTC).date()
            by_date.setdefault(d, []).append(ev)
        return by_date

    def _month_cell_rect(self, week: int, weekday: int) -> QRect:
        """Geometry of the (week-row, weekday-col) day-cell — the SINGLE source
        of truth shared by _paint_month and day_hit_point.

        `week` is the 0-based row from the top of the visible month; `weekday` is
        0=Mon..6=Sun. The grid fills the canvas below the chrome and the weekday
        header, inset by _MONTH_GRID_PAD. Computing rects from the live widget
        size (not fixed cell sizes) means the grid scales with the window — and
        because the paint and the hit-test call THIS, a click can't desync from
        what's drawn even as the layout is tuned by eye.
        """
        weeks = self._month_weeks()
        n_rows = max(1, len(weeks))  # avoid /0 before the first set_events
        top = self._canvas_top() + _MONTH_TOP_PAD
        grid_left = _MONTH_GRID_PAD
        grid_right = max(grid_left + 1, self.width() - _MONTH_GRID_PAD)
        grid_bottom = max(top + 1, self.height() - _MONTH_GRID_PAD)
        col_w = max(1, (grid_right - grid_left) // 7)
        row_h = max(1, (grid_bottom - top) // n_rows)
        x = grid_left + weekday * col_w
        y = top + week * row_h
        return QRect(x, y, col_w, row_h)

    def _paint_month(self) -> None:
        """Paint the Month grid: a weeks-x-weekdays calendar of the visible month.

        Recomputes _problem_weeks from scratch (so a clean month clears it),
        draws the weekday header, then each day-cell. A cell only shows a glyph +
        count when its day has a gap/failure or is otherwise non-empty (clean days
        stay blank); future cells (day after `now`) are dimmed because they can't
        carry an outcome yet; any week-row containing a gap/failure is flagged in
        _problem_weeks and gets a heavy border. The model owns severity/bucketing
        (_day_cell_summary → _day_cell_worst/bucket_cell), so the paint only
        decides geometry and colour — never what counts as a problem.
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            weeks = self._month_weeks()
            by_date = self._events_by_date()
            anchor = self._month_anchor()
            now_date = datetime.fromtimestamp(self._now / 1_000_000, UTC).date()

            self._problem_weeks = set()
            self._paint_month_header(painter)

            for w_idx, week in enumerate(weeks):
                for wd_idx, day in enumerate(week):
                    cell = by_date.get(day, [])
                    _glyphs, worst, _counts = self._day_cell_summary(cell)
                    # A week is a "problem week" if any of its cells holds a gap
                    # or a failed run — read off the worst event so one check
                    # covers both (the worst is failure/gap when present).
                    if worst is not None and _event_severity(worst) >= _SEVERITY[
                        ("gap", "")
                    ]:
                        self._problem_weeks.add(w_idx)
                    in_month = day.month == anchor.month
                    is_future = day > now_date
                    self._paint_month_cell(
                        painter, w_idx, wd_idx, day, worst, _glyphs,
                        in_month, is_future,
                    )

            self._paint_problem_week_borders(painter)
        finally:
            # End the painter even if a draw raised — an un-ended QPainter on a
            # widget can corrupt the backing store on the next paint.
            painter.end()

    def _paint_month_header(self, painter: QPainter) -> None:
        """Draw the Mon..Sun weekday labels across the top of the grid.

        Kept minimal (abbreviations, a thin baseline) — richer styling is the
        visual pass. Anchored to the same column geometry as the cells via
        _month_cell_rect(0, wd) so a header column lines up with its day column.
        """
        painter.setPen(QColor(150, 150, 150))
        top = self._canvas_top()
        for wd, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            rect = self._month_cell_rect(0, wd)
            painter.drawText(
                QRect(rect.x() + 2, top, rect.width() - 4, _MONTH_TOP_PAD),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )

    def _paint_month_cell(
        self,
        painter: QPainter,
        week: int,
        weekday: int,
        day: date,
        worst: CalendarEvent | None,
        glyphs: str,
        in_month: bool,
        is_future: bool,
    ) -> None:
        """Draw one day-cell: its border, day number, and (if any) worst glyph.

        Days outside the visible month (grid padding) and future days are dimmed
        so the eye stays on the current month's past — a future or adjacent-month
        cell can't carry an outcome, so muting it is honest, not decorative. A
        problem day's worst glyph is drawn in its outcome colour; a clean day
        shows only its faint day number (no glyph), which is the de-noising the
        spec asks for.
        """
        rect = self._month_cell_rect(week, weekday)
        # Cell outline — faint; the heavy problem-week border is drawn separately
        # on top so it dominates.
        painter.setPen(QColor(70, 70, 70))
        painter.drawRect(rect)

        # Day number, top-left. Dimmed for out-of-month/future cells.
        dim = (not in_month) or is_future
        num_color = QColor(90, 90, 90) if dim else QColor(170, 170, 170)
        painter.setPen(num_color)
        painter.drawText(
            QRect(rect.x() + 3, rect.y() + 2, rect.width() - 6, 16),
            Qt.AlignmentFlag.AlignLeft,
            str(day.day),
        )

        # Worst-outcome glyph + count, centred — only on a non-empty cell, and
        # only for in-month days (an adjacent-month cell belongs to a neighbour
        # view, so we don't summarise it here). Future in-month cells can hold
        # `projected` events; those still draw but in the dim upcoming colour.
        if worst is None or not in_month:
            return
        _glyph, color = _glyph_color(worst)
        painter.setPen(color)
        painter.drawText(
            QRect(rect.x() + 3, rect.y() + 18, rect.width() - 6, rect.height() - 20),
            Qt.AlignmentFlag.AlignCenter,
            glyphs,
        )

    def _paint_problem_week_borders(self, painter: QPainter) -> None:
        """Draw a heavy border around every week-row in _problem_weeks.

        Spanning the full row (col 0 left edge → col 6 right edge) makes a bad
        week jump out at a glance before the user reads any cell. Drawn LAST so it
        sits on top of the faint per-cell outlines. Reads _problem_weeks, which
        _paint_month populated this same paint — one computation, two uses (the
        border here and the test's assertion).
        """
        pen = painter.pen()
        painter.setPen(QColor(200, 90, 90))  # the failure red — a bad week is red
        for w_idx in self._problem_weeks:
            left = self._month_cell_rect(w_idx, 0)
            right = self._month_cell_rect(w_idx, 6)
            row_rect = QRect(
                left.x(), left.y(), right.right() - left.x(), left.height()
            )
            painter.drawRect(row_rect)
        painter.setPen(pen)

    # -- matrix paint ---------------------------------------------------------
    #
    # The by-timer matrix is the Month window seen the OTHER way round from the
    # calendar grid: instead of one cell per day summarising all timers, it lays
    # timers down the gutter (like Day/Week) and the visible month's days across
    # the columns, so each row is one timer's outcome streak. Its diagnostic point
    # (spec §5) is reading a SINGLE timer's history at a glance — a lone ✘ (a
    # failed run) sits in a different column than a lone ⛌ (a missed slot), so
    # "did it fail or just not run?" is answerable per timer. A high-volume timer
    # whose busiest day exceeds CELL_DRAW_MAX collapses its whole row to one solid
    # ▦ band (the aggregate timer), reusing the model's bucket_cell rather than
    # glyph-storming — the same over-full rule Day and Week apply.

    def _matrix_day_dates(self) -> list[date]:
        """The visible month's days as a flat, ordered list of UTC dates.

        Built by flattening _month_weeks and keeping only IN-MONTH days, so the
        matrix columns are exactly the calendar month's days (28–31 of them),
        never the adjacent-month padding the grid shows. Shared by the row-cell
        builder and the paint so a column always means the same day in both.
        Bounded: at most 31 days. Ordering is calendar order (1st → last).
        """
        anchor = self._month_anchor()
        days: list[date] = []
        for week in self._month_weeks():
            for day in week:
                if day.month == anchor.month:
                    days.append(day)
        return days

    def _matrix_row_cells(self, unit: str) -> list[str]:
        """One timer's matrix row as a list of per-day cell readouts.

        The TESTABLE seam for the matrix (mirrors _day_cell_summary for Month):
        for `unit`, returns one string per day-column of the visible month —
        the day's worst-outcome glyph (✘ failure, ⛌ gap, ✔ success, ⏲/◇ upcoming)
        or "" for a quiet day. This is what makes a failure row and a gap row
        DISTINGUISHABLE without inspecting pixels: the failure lands a ✘ in its
        column, the gap a ⛌ in its, and clean days stay empty.

        Aggregate exception: if the timer's busiest day exceeds CELL_DRAW_MAX
        events, the whole row collapses to a SINGLE "▦ <count>" band cell (red-
        flagged with the failure count) — the aggregate timer reads as one band,
        not a glyph storm. The over-full decision and the (count, failures) math
        both come from the model's bucket_cell, so the view never re-decides what
        "over-full" means (same rule as Day/Week).
        """
        days = self._matrix_day_dates()
        # Bucket this timer's events by UTC date once, so each day-column is a
        # dict lookup rather than a re-scan of the whole event list per column.
        by_date: dict[date, list[CalendarEvent]] = {}
        for ev in self._events:
            if ev.unit != unit:
                continue
            d = datetime.fromtimestamp(ev.when / 1_000_000, UTC).date()
            by_date.setdefault(d, []).append(ev)

        # Aggregate detection: a row is a "band" when ANY single day overflows
        # CELL_DRAW_MAX. We collapse the WHOLE row (all the unit's events) into one
        # band cell — the matrix's aggregate-timer band is per-row, not per-cell,
        # so a busy timer reads as one solid streak. bucket_cell gives the total
        # and the failure count for the red flag.
        busiest = max((len(v) for v in by_date.values()), default=0)
        if busiest > CELL_DRAW_MAX:
            row_events = [e for e in self._events if e.unit == unit]
            count, failures = bucket_cell(row_events)
            return [f"▦ {count}" + (f" ({failures}✘)" if failures else "")]

        # Normal row: one worst-outcome glyph per day-column, "" for quiet days.
        cells: list[str] = []
        for day in days:
            worst = self._day_cell_worst(by_date.get(day, []))
            if worst is None:
                cells.append("")  # quiet day — no glyph (the de-noising goal)
                continue
            glyph, _color = _glyph_color(worst)
            cells.append(glyph)
        return cells

    def _paint_matrix(self) -> None:
        """Paint the by-timer matrix: timer rows × the visible month's day-columns.

        Each row is one timer; each column one in-month day; each cell the day's
        worst-outcome glyph (via _matrix_row_cells, the same severity rule the
        other views use). An aggregate timer's row is a single ▦ band drawn across
        the full width instead of per-day glyphs. Geometry shares
        _canvas_top/_TOP_PAD/_ROW_H/_GUTTER_W with row_hit_point and
        mousePressEvent, so a click in matrix mode lands on the painted row exactly
        like Day/Week. Exact spacing/labels are the post-build visual pass; this
        proves the data renders and never raises.
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            top = self._canvas_top()
            width = self.width()
            grid_left = _GUTTER_W
            grid_right = max(grid_left + 1, width - 8)
            days = self._matrix_day_dates()
            n_cols = max(1, len(days))
            col_w = max(1, (grid_right - grid_left) // n_cols)

            self._paint_matrix_header(painter, top, grid_left, col_w, days)

            glyph_font = QFont(self.font())
            painter.setFont(glyph_font)
            for i, unit in enumerate(self._units):
                row_y = top + _TOP_PAD + i * _ROW_H
                self._paint_unit_label(painter, unit, row_y)
                self._paint_matrix_row(
                    painter, unit, row_y, grid_left, col_w, grid_right
                )
        finally:
            # End the painter even if a draw raised — an un-ended QPainter on a
            # widget can corrupt the backing store on the next paint.
            painter.end()

    def _paint_matrix_header(
        self,
        painter: QPainter,
        top: int,
        grid_left: int,
        col_w: int,
        days: list[date],
    ) -> None:
        """Draw a thin day-number tick for each matrix column.

        Labels are the day-of-month numbers (1..N) across the top so a row's
        outcome can be read against a date. Kept minimal (a number per column) —
        denser axis styling is the visual pass; its presence anchors the columns.
        """
        painter.setPen(QColor(150, 150, 150))
        for col, day in enumerate(days):
            x = grid_left + col * col_w
            painter.drawText(
                QRect(x + 1, top, col_w - 2, _TOP_PAD - 2),
                Qt.AlignmentFlag.AlignVCenter,
                str(day.day),
            )

    def _paint_matrix_row(
        self,
        painter: QPainter,
        unit: str,
        row_y: int,
        grid_left: int,
        col_w: int,
        grid_right: int,
    ) -> None:
        """Paint one timer's matrix row from its _matrix_row_cells readout.

        An aggregate row (_matrix_row_cells returned a single "▦ …" band cell)
        draws ONE band across the full width — a busy timer reads as a solid
        streak, never a glyph storm. A normal row draws each non-empty per-day
        cell at its column, in the cell's worst-outcome colour. Colour is re-
        derived from the day's worst event (not stored in the string) so the glyph
        keeps its outcome colour — the failure-never-hidden rule the model encodes.
        """
        cells = self._matrix_row_cells(unit)
        days = self._matrix_day_dates()
        # Aggregate band: _matrix_row_cells collapses an over-full row to exactly
        # one "▦ …" cell. Detect that shape and draw the band instead of columns.
        if len(cells) == 1 and cells[0].startswith("▦"):
            row_events = [e for e in self._events if e.unit == unit]
            _count, failures = bucket_cell(row_events)
            band_color = _RAN_FAIL[1] if failures > 0 else QColor(110, 110, 120)
            painter.fillRect(
                QRect(grid_left, row_y + 6, grid_right - grid_left, _ROW_H - 12),
                QColor(band_color.red(), band_color.green(), band_color.blue(), 60),
            )
            painter.setPen(band_color)
            painter.drawText(
                QRect(grid_left + 4, row_y, grid_right - grid_left, _ROW_H),
                Qt.AlignmentFlag.AlignVCenter,
                cells[0],
            )
            return

        # Normal row: place each day's glyph at its column. We re-bucket the
        # unit's events by date to recover the worst event's COLOUR (the cell
        # string carries only the glyph); the glyph itself comes from `cells` so
        # paint and the testable readout can never disagree.
        by_date: dict[date, list[CalendarEvent]] = {}
        for ev in self._events:
            if ev.unit != unit:
                continue
            d = datetime.fromtimestamp(ev.when / 1_000_000, UTC).date()
            by_date.setdefault(d, []).append(ev)
        for col, (day, glyph) in enumerate(zip(days, cells, strict=True)):
            if not glyph:
                continue  # quiet day — nothing drawn
            worst = self._day_cell_worst(by_date.get(day, []))
            color = _glyph_color(worst)[1] if worst is not None else _UNKNOWN[1]
            x = grid_left + col * col_w
            painter.setPen(color)
            painter.drawText(
                QRect(x + 1, row_y, col_w - 2, _ROW_H),
                Qt.AlignmentFlag.AlignCenter,
                glyph,
            )

    def day_hit_point(self, when_usec: int) -> QPoint:
        """Widget-local centre of the day-cell containing the µs-epoch `when`.

        Used by tests (and a future keyboard/scroll handler) to address a day
        deterministically, and built on the SAME _month_cell_rect the paint uses,
        so a click here lands on the drawn cell. Locates `when`'s UTC date in the
        visible month's week-rows; if the date isn't in the grid (a window the
        host shouldn't produce), falls back to the first cell's centre rather than
        raising — a hit-point helper must always return a point.
        """
        target = datetime.fromtimestamp(when_usec / 1_000_000, UTC).date()
        for w_idx, week in enumerate(self._month_weeks()):
            for wd_idx, day in enumerate(week):
                if day == target:
                    rect = self._month_cell_rect(w_idx, wd_idx)
                    return QPoint(rect.x() + rect.width() // 2,
                                  rect.y() + rect.height() // 2)
        rect = self._month_cell_rect(0, 0)
        return QPoint(rect.x() + rect.width() // 2, rect.y() + rect.height() // 2)

    # -- interaction ----------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        """Hit-test a left click and emit selected(unit) for the right target.

        Day/Week are timer-row layouts, so the click maps to a row → that timer.
        Month is a day-cell grid, so the click maps to a day → the unit of that
        day's WORST-outcome event (so the host opens the timer that actually
        failed/missed, not a healthy sibling). Both dispatch to the SAME geometry
        helpers the paints use, so a click always lands on what's drawn. Left
        button only — right-click is reserved for a future context menu and must
        not fire a selection.
        """
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position()
        if self._mode == "month":
            unit = self._month_click_unit(int(pos.x()), int(pos.y()))
        else:
            unit = self._row_click_unit(int(pos.y()))
        if unit is not None:
            self.selected.emit(unit)
            return
        super().mousePressEvent(event)

    def _row_click_unit(self, y: int) -> str | None:
        """Map a click y to a timer-row unit (Day/Week), or None off any row.

        Uses the SAME _TOP_PAD/_ROW_H geometry as row_hit_point/_paint_day, so a
        click lands on the row the user sees. Clicks above row 0 (the axis) or
        below the last row resolve to None (no selection).
        """
        first_row_top = self._canvas_top() + _TOP_PAD
        if y < first_row_top:
            return None  # in the axis/header region — not a row
        index = (y - first_row_top) // _ROW_H
        if 0 <= index < len(self._units):
            return self._units[index]
        return None

    def _month_click_unit(self, x: int, y: int) -> str | None:
        """Map a click (x, y) to a Month day-cell's worst-outcome unit, or None.

        Walks the visible month's cells via the SAME _month_cell_rect the paint
        uses, finds the cell the point lands in, and returns that day's worst
        event's unit through _day_cell_summary (one severity rule for paint,
        border, and click). A click on an empty/clean day or off the grid returns
        None — nothing to select there.
        """
        by_date = self._events_by_date()
        for w_idx, week in enumerate(self._month_weeks()):
            for wd_idx, day in enumerate(week):
                if self._month_cell_rect(w_idx, wd_idx).contains(x, y):
                    _glyphs, worst, _counts = self._day_cell_summary(
                        by_date.get(day, [])
                    )
                    return worst.unit if worst is not None else None
        return None
