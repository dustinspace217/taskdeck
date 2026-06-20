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

from datetime import UTC, datetime

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


# Layout constants for the painted canvas. These are STARTING values for the
# visual-iteration pass, not load-bearing contract — the tests pin behavior, not
# these numbers. Named so the iteration pass has one place to tune.
_GUTTER_W = 160      # left column width for the timer-unit label
_ROW_H = 28          # height of one timer row in the canvas
_TOP_PAD = 24        # space above row 0 for the hour axis / header


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
        btn = self._mode_buttons.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        self.update()

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

        Only the Day paint exists in this task (Week/Month/matrix come later); an
        unimplemented mode falls back to Day so the widget always draws something
        rather than a blank page. A QPainter that escapes paintEvent un-ended can
        wedge Qt, so each _paint_* opens and ends its own painter.
        """
        if self._mode == "day":
            self._paint_day()
        else:
            # Week/Month/matrix not yet implemented — draw the Day grid so the
            # page is never blank during the staged rollout (Tasks 8-10 replace
            # this branch with the real paints).
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

    # -- interaction ----------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        """Hit-test a click to a timer row and emit selected(unit).

        Maps the click's y back to a row index using the SAME geometry as
        row_hit_point/_paint_day (one source of truth), so a click lands on the
        row the user sees. Clicks above row 0 (the axis) or below the last row
        select nothing. Left button only — right-click is reserved for a future
        context menu and must not fire a selection.
        """
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        y = int(event.position().y())
        first_row_top = self._canvas_top() + _TOP_PAD
        if y < first_row_top:
            super().mousePressEvent(event)
            return  # in the axis/header region — not a row
        index = (y - first_row_top) // _ROW_H
        if 0 <= index < len(self._units):
            self.selected.emit(self._units[index])
            return
        super().mousePressEvent(event)
