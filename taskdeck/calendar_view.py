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
from datetime import date, datetime, timedelta

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from taskdeck.calendar_model import (
    CELL_DRAW_MAX,
    CalendarEvent,
    Health,
    bucket_cell,
    local_calendar_window,
    summarize,
)

# µs in a day — used only for relative time→x fractions and the Day-view axis
# span, never for window navigation (that is local-calendar-aligned now via
# local_calendar_window in the model). Kept here because the Day/Week paints
# still position events as a fraction of the window span.
_DAY_USEC = 86_400_000_000

# The modes set_mode accepts. Replaces the old fixed-µs-span table: nav no longer
# shifts by a constant span — it moves by ONE LOCAL calendar unit via the model's
# local_calendar_window (±1 day/week/month, DST- and month-length-correct). The
# set is what set_mode/local_calendar_window validate against; "matrix" is a
# Month sub-view (spec §5) and shares the Month window.
_MODES: frozenset[str] = frozenset({"day", "week", "month", "matrix"})

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


def _event_category(event: CalendarEvent) -> str:
    """Coarse routing category for diagnostic click-through.

    Collapses (kind, result) into the four buckets the host routes on:
    'failure' (a run that exited non-zero → jump to its Log), 'gap' (a missed
    scheduled run → jump to the Schedule), 'ran' (a healthy run), 'upcoming'
    (projected/approx, or anything unrecognized — never routed as a problem).
    """
    if event.kind == "ran":
        return "failure" if event.result == "failure" else "ran"
    if event.kind == "gap":
        return "gap"
    return "upcoming"


def _local_dt(when_usec: int) -> datetime:
    """A µs UTC epoch as a NAIVE LOCAL datetime — the calendar's display clock.

    `when_usec` is an absolute UTC µs epoch (a slot/run time, or a window edge).
    EVERY time the user SEES — a Day x-position's hour, a Week cell's hour glyph,
    a Month/matrix day-cell, the now-marker, day-bucketing into cells — must read
    in the user's LOCAL timezone, or a PDT run at 22:00 local (05:00 UTC next day)
    would land in the wrong day-cell and a 06:00-local run would label as 13:00.
    fromtimestamp WITHOUT a tz argument is exactly that local conversion (verified
    2026-06-20 against a forced America/Los_Angeles tz). This is the SINGLE seam
    for it, so a future "show in UTC" toggle has one place to change — and so the
    model's UTC-µs absolutes never leak a UTC wall-clock into the display.

    NOTE the deliberate split: model + subprocess @-args stay UTC-absolute
    (journalctl/systemd-analyze @<seconds> are absolute, timezone-free); ONLY this
    display path and the window-boundary derivation (local_calendar_window) touch
    the local calendar.
    """
    return datetime.fromtimestamp(when_usec / 1_000_000)


def _local_date(when_usec: int) -> date:
    """The LOCAL calendar date of a µs UTC epoch (see _local_dt for the why).

    Used wherever an event is bucketed into a day-cell (Month grid, matrix
    columns, day_hit_point) so the cell a run lands in matches the user's
    wall-clock day, not a UTC one.
    """
    return _local_dt(when_usec).date()


def _short_unit_name(unit: str) -> str:
    """Strip the trailing .timer/.service suffix from a unit name for display.

    `unit` is a full systemd unit name (e.g. "backup.timer", "logrotate.service").
    The Month cell shows several timer names per day, so the suffix is pure noise
    there — every entry is a timer/service, the suffix carries no per-row signal.
    We only strip the KNOWN trailing suffixes (not a blind rsplit on ".") so a
    dotted base name like "my.app.timer" → "my.app" survives, and an already-bare
    name is returned unchanged. Kept tiny and module-level so the Month paint and
    its test share one definition.
    """
    for suffix in (".timer", ".service"):
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


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
    - `event_activated = Signal(str, str, qlonglong)` — (unit, category, when) for
      the WORST event under the click; the host routes a failure → Log tab and a
      gap → Schedule tab (diagnostic click-through). Additive to `selected`.
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
    # Diagnostic click-through (v2): (unit, category, when_usec) for the WORST
    # event the click landed on. category ∈ {'failure','gap','ran','upcoming'};
    # the host routes a failure → Log tab, a gap → Schedule tab. Emitted ALONGSIDE
    # `selected` (which loads the detail tabs) — this signal only adds the routing,
    # so a host that ignores it keeps the old select-only behavior. qlonglong for
    # `when`, same µs-overflow reason as `rebuild`.
    event_activated = Signal(str, str, "qlonglong")
    # Right-click context menu (v2): (action, unit), action ∈ {'run','logs','table'}.
    # The widget builds NO argv and enforces NO policy — it emits the intent and the
    # host runs it (the read-only guard stays in actions.py). 'Run now' is greyed
    # when set_run_enabled(False) (system/read-only scope).
    menu_action = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Track the cursor without a held button so a Month-cell HOVER can reveal a
        # day's FULL timer list — the cell paints only the first few + "+N more",
        # and Dustin asked the overflow be hover/click-expandable. Hover, because a
        # left-click is already taken (it opens the day's worst unit). See
        # mouseMoveEvent / _month_day_tooltip_text.
        self.setMouseTracking(True)
        # Whether the right-click "Run now" is offered. The host sets it = user
        # scope only (system scope is read-only); default True until told. Policy
        # lives in the host (the actions.py guard) — this flag is only the greying.
        self._run_enabled = True
        # Nav/window state — OWNED here (spec §7) so a 10s tick or an arrow click
        # never disturbs the table. Defaults are harmless zeros until the host's
        # first set_events; _mode gates which _paint_* runs.
        self._mode = "day"
        self._window_start = 0
        self._window_end = 0
        self._now = 0
        self._events: list[CalendarEvent] = []
        self._units: list[str] = []
        # Cached Health for the visible window (model's summarize over _events),
        # recomputed each set_events. The strip reads it; tests read it as the
        # outward state. Defaults to the all-zero Health so the strip is valid
        # before the first set_events.
        self._health = Health()
        # Whether the most recent set_events delivered a DEGRADED (partial) build
        # — some calendar fetch failed host-side (R2-3). The host passes this; the
        # HEALTH strip prefixes a "⚠ partial" warning when True so the degradation
        # is visible in the calendar's own surface, not just the ephemeral status
        # bar. False until the host says otherwise; tests read it as outward state.
        self._degraded = False
        # Active Month-grid filter, one of None / "fail" / "gap" / "upcoming".
        # None = show everything; a kind dims the non-matching cells so the user
        # can isolate (e.g.) just the failures. Owned here (Month-grid-only state)
        # and cleared when leaving the Month grid so it never silently dims a
        # later return. See set_filter / set_mode.
        self._filter: str | None = None
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

        # Two strips sit between the nav row and the canvas: the always-on HEALTH
        # summary, then the Month-grid-only filter toggles. They're built as
        # container QWidgets (not bare sub-layouts) so the filter strip can be
        # hidden with setVisible(False) and the QVBoxLayout collapses its height
        # to zero — which keeps _canvas_top (and thus every hit-test) honest:
        # the canvas always starts directly below whatever chrome is showing.
        self._build_health_strip(outer)
        self._build_filter_strip(outer)

    def _build_health_strip(self, outer: QVBoxLayout) -> None:
        """Build the always-visible HEALTH strip: summary + legend, then issues.

        Two stacked rows inside one container widget:
        1. A row with the summary label (filled from the model's summarize() — the
           view never counts outcomes itself, so the strip and the Phase-2 plasmoid
           read the same numbers) on the left and an always-visible glyph LEGEND on
           the right. The legend fixes Dustin's "no idea what Gaps is" — there was
           no key anywhere — by spelling out each glyph's meaning in its own colour.
        2. An issues label below it: the model already builds Health.issues ("unit @
           date" per failure/gap), but only the counts were shown — so the user
           couldn't read WHICH unit had a gap. We surface that compact list here.

        A container (not bare labels) keeps the strip's geometry one layout item
        _canvas_top can measure, matching the filter strip's shape. Built with a
        QVBoxLayout so the issues line stacks under the summary/legend row.
        """
        self._health_strip = QWidget()
        strip_layout = QVBoxLayout(self._health_strip)
        strip_layout.setContentsMargins(8, 2, 8, 2)
        strip_layout.setSpacing(1)

        # Row 1: summary (left) + legend (right).
        top_row = QHBoxLayout()
        self._health_label = QLabel("")
        top_row.addWidget(self._health_label)
        top_row.addStretch(1)
        self._legend_label = self._build_legend_label()
        top_row.addWidget(self._legend_label)
        strip_layout.addLayout(top_row)

        # Row 2: the compact issues list (which unit failed/gapped), under the
        # summary. Empty (and so visually absent) on a clean window.
        self._issues_label = QLabel("")
        self._issues_label.setWordWrap(True)  # a long list wraps, never clips
        strip_layout.addWidget(self._issues_label)

        outer.addWidget(self._health_strip)

    def _build_legend_label(self) -> QLabel:
        """Build the always-visible glyph legend ("✔ ran · ✘ failed · …").

        Returns one QLabel whose rich text colours each glyph with the SAME colour
        the cells use (_glyph_color via the _RAN_OK/_RAN_FAIL/_GAP/_PROJECTED
        tuples), so the key and the grid speak one visual language — a user can map
        a cell glyph straight to its meaning. Rich text (Qt's tiny HTML subset) is
        used ONLY here, to colour individual spans within one label; a plain label
        can't colour per-glyph. The four entries are the kinds the user actually
        sees (ran/failed/missed/upcoming); 'approx' shares the upcoming meaning so
        it isn't a separate key entry. Built once at construction — the legend text
        is static, so it never changes after this.
        """
        def span(glyph: str, color: QColor, word: str) -> str:
            # One coloured glyph + its plain-text meaning. .name() is the #rrggbb
            # hex Qt rich text needs; the word stays default-coloured so only the
            # glyph carries colour (matching the cells).
            return f'<span style="color:{color.name()}">{glyph}</span> {word}'

        legend = QLabel(
            " · ".join(
                (
                    span(_RAN_OK[0], _RAN_OK[1], "ran"),
                    span(_RAN_FAIL[0], _RAN_FAIL[1], "failed"),
                    span(_GAP[0], _GAP[1], "missed"),
                    span(_PROJECTED[0], _PROJECTED[1], "upcoming"),
                )
            )
        )
        legend.setTextFormat(Qt.TextFormat.RichText)
        return legend

    def _build_filter_strip(self, outer: QVBoxLayout) -> None:
        """Build the Month-grid filter toggles (All / Failures / Gaps / Upcoming).

        Checkable, auto-exclusive buttons (one radio group without a QButtonGroup,
        mirroring the mode toggle). Each calls set_filter with its kind — "All"
        passes None (show everything). The whole strip is a container widget so it
        can be hidden outside the Month grid (see _sync_filter_strip): the filter
        DIMS Month-grid cells, which is meaningless in Day/Week/matrix, so showing
        it there would be a dead control. Hidden by default (Day is the start mode).
        """
        self._filter_strip = QWidget()
        strip_layout = QHBoxLayout(self._filter_strip)
        strip_layout.setContentsMargins(8, 2, 8, 2)
        strip_layout.addWidget(QLabel("Show:"))

        # (button label, filter kind). None = "All" (no dimming). The kinds match
        # set_filter's accepted values exactly so a button can never request an
        # unknown filter.
        self._filter_buttons: dict[str | None, QPushButton] = {}
        for label, kind in (
            ("All", None),
            ("Failures", "fail"),
            ("Gaps", "gap"),
            ("Upcoming", "upcoming"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)  # one-of-N without a QButtonGroup
            # Default-arg binds `kind` per-iteration (avoid the late-bind closure
            # capturing the LAST kind for every button).
            btn.clicked.connect(lambda _checked=False, k=kind: self.set_filter(k))
            self._filter_buttons[kind] = btn
            strip_layout.addWidget(btn)
        self._filter_buttons[None].setChecked(True)  # default: All (no filter)

        strip_layout.addStretch(1)
        self._filter_strip.setVisible(False)  # Day is the default mode → hidden
        outer.addWidget(self._filter_strip)

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
        if mode not in _MODES:
            return  # unrecognized — keep the current mode, never raise
        self._mode = mode
        # The top-level toggle only has Day/Week/Month buttons; "matrix" is a
        # Month sub-mode, so it checks the MONTH button (the user is still "in
        # Month", just viewing the matrix). .get() returns None for "matrix".
        btn = self._mode_buttons.get("month" if mode == "matrix" else mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        # The filter is a Month-GRID-only state (it dims grid cells). Leaving the
        # grid — to Day/Week/matrix — clears it so a later return to Month never
        # inherits a stale dim, and the strip's "All" button resets visually.
        # Reset BEFORE _sync_filter_strip so the strip reflects the cleared state.
        if mode != "month" and self._filter is not None:
            self._filter = None
            self._filter_buttons[None].setChecked(True)
        self._sync_matrix_toggle()
        self._sync_filter_strip()
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

    def _sync_filter_strip(self) -> None:
        """Show the filter strip only in the Month GRID, hide it everywhere else.

        Visible for mode == "month" alone — NOT matrix (the matrix has no per-cell
        dim; its diagnostic is per-timer rows) and not Day/Week. Hiding the
        container collapses its layout height to zero, which is exactly why
        _canvas_top stays correct: the canvas reclaims the space the moment the
        strip hides. No checked-state sync here — set_mode already resets the "All"
        button when it clears the filter on leaving the grid.
        """
        self._filter_strip.setVisible(self._mode == "month")

    def _on_matrix_toggled(self, checked: bool) -> None:
        """Switch between the Month grid and the by-timer matrix on toggle.

        `checked` is the new button state (True=matrix, False=grid). Both are the
        Month window, so this only swaps the mode (and thus which _paint_* runs) —
        no nav, no rebuild. Routed through set_mode so visibility/checked stay in
        one place; set_mode's own _sync_matrix_toggle is a no-op here since the
        button is already in the wanted state.
        """
        self.set_mode("matrix" if checked else "month")

    # Accepted filter kinds. None ("All") shows everything; each other value dims
    # Month-grid cells whose worst event isn't that kind. Defined as a module-ish
    # set so set_filter validates against ONE list and a button can't request an
    # unknown kind. "fail"/"gap"/"upcoming" map to event kinds in _cell_matches_filter.
    _FILTER_KINDS = (None, "fail", "gap", "upcoming")

    def set_filter(self, kind: str | None) -> None:
        """Set the Month-grid filter (None/"fail"/"gap"/"upcoming") and repaint.

        `kind` selects which cells stay bright in the Month grid: "fail" keeps
        days with a failed run, "gap" days with a missed slot, "upcoming" days
        with a projected/approx run; None (All) dims nothing. An unrecognized kind
        is IGNORED (keep the current filter, no raise) — same tolerance as
        set_mode's unknown-mode guard, so a typo'd caller can't wedge the paint.
        Only the repaint changes; the filter dims inside _paint_month, it never
        drops events or refetches.
        """
        if kind not in self._FILTER_KINDS:
            return  # unrecognized — keep the current filter, never raise
        self._filter = kind
        # Keep the strip button in sync for a programmatic set_filter (a click
        # already checked its own button). .get() is safe: kind is a valid key.
        btn = self._filter_buttons.get(kind)
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
        degraded: bool = False,
    ) -> None:
        """Inject the parsed events + the window they cover, then repaint.

        Called by the host AFTER it has fetched and parsed (via calendar_model);
        the widget does no I/O. `units` is the row order (timer unit names);
        `events` are placed against `[window_start, window_end]`; `now` positions
        the ▲ now marker. `degraded` (R2-3) is True when the host's build was
        partial — some calendar fetch failed — so the HEALTH strip can warn the
        user the data is incomplete (defaults False so existing callers and the
        happy path are unaffected). Storing then update() defers the actual draw
        to paintEvent — Qt coalesces repaints, so a burst of set_events (10s tick
        + nav) costs one paint.
        """
        self._events = list(events)
        self._units = list(units)
        self._window_start = window_start
        self._window_end = window_end
        self._now = now
        self._degraded = degraded
        # Roll the window's events into a Health summary via the MODEL (not local
        # counting) so the strip and the plasmoid agree on the numbers, then push
        # it to the strip text. Caching it (self._health) lets tests read the
        # outward state and avoids re-summarizing on every repaint.
        self._health = summarize(self._events)
        self._update_health_strip()
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
    # Each nav move steps the window by ONE LOCAL calendar unit (±1 day / week /
    # month) and emits `rebuild(start, end)` so the host refetches exactly the new
    # window. The widget owns this state; it never calls the host directly (spec
    # §7). The local-calendar math lives in the model (local_calendar_window) so
    # the Phase-2 plasmoid navigates identically — a boundary computed here would
    # fork between the two consumers.

    def nav_next(self) -> None:
        """Step to the NEXT local calendar unit and ask the host to refetch.

        The current window is half-open [start, end), so `end` is EXACTLY the next
        unit's local 00:00 (next day, next Monday, or next month's 1st). Feeding it
        back through local_calendar_window re-floors to that unit's boundaries —
        DST- and month-length-correct, because the boundary is re-derived from the
        local calendar, not by adding a fixed span (a +30-day month would drift).
        """
        self._set_window(local_calendar_window(self._mode, self._window_end))

    def nav_prev(self) -> None:
        """Step to the PREVIOUS local calendar unit and ask the host to refetch.

        `win_start - 1µs` lands one microsecond before this unit's local 00:00 —
        i.e. the last instant of the PREVIOUS unit — which local_calendar_window
        then floors to that previous unit's boundaries. Re-deriving from the local
        calendar (not subtracting a fixed span) keeps it correct across DST and
        variable month length. How far back data exists is the host's limit; the
        widget just steps and asks.
        """
        self._set_window(local_calendar_window(self._mode, self._window_start - 1))

    def nav_today(self) -> None:
        """Return to the local calendar unit CONTAINING `now`.

        'Today' = the now-containing window for the current mode (Day = today's
        local midnight-to-midnight, Week = this local week, Month = this month) —
        the same window first-show uses. `_now` is the absolute µs instant the host
        last set via set_events; local_calendar_window floors it to the local unit.
        """
        self._set_window(local_calendar_window(self._mode, self._now))

    def _set_window(self, window: tuple[int, int]) -> None:
        """Adopt a new (start, end) window, repaint, and emit rebuild.

        Centralized so nav_next/nav_prev/nav_today share one emit contract — the
        repaint shows the (now-empty) new window immediately for feedback, and
        rebuild tells the host to fill it. `window` comes from the model's
        local_calendar_window, so both edges are local-calendar-aligned UTC µs.
        """
        self._window_start, self._window_end = window
        self._update_range_label()
        self.update()
        self.rebuild.emit(self._window_start, self._window_end)

    def _update_range_label(self) -> None:
        """Refresh the chrome's range text from the current window, in LOCAL dates.

        The window is half-open [start, end) in UTC µs; the label must read in the
        user's LOCAL calendar (the window IS a local unit now), so we convert with
        fromtimestamp WITHOUT a tz (= local wall-clock). `end` is the EXCLUSIVE
        next-unit boundary (local 00:00), so we label the INCLUSIVE last day
        (end - 1µs) — otherwise a one-day Day window would read "Jun 20 – Jun 21"
        and a month would read "Jun 01 – Jul 01". A single-day window collapses to
        just that date. Minimal formatting on purpose — richer styling is the
        post-build visual pass.
        """
        if self._window_end <= self._window_start:
            self._range_label.setText("")
            return
        start = datetime.fromtimestamp(self._window_start / 1_000_000)
        # Inclusive last day: one µs before the exclusive end boundary.
        last = datetime.fromtimestamp((self._window_end - 1) / 1_000_000)
        if start.date() == last.date():
            self._range_label.setText(f"{start:%b %d}")
        else:
            self._range_label.setText(f"{start:%b %d} – {last:%b %d}")

    def _update_health_strip(self) -> None:
        """Render the cached Health into the strip's label text.

        Reads self._health (set by set_events from the model's summarize). The
        format is intentionally terse — glyph counts so a problem window is
        scannable — and minimal on purpose: the rich styling (colour per count,
        the issues popover) is the post-build visual pass. The glyphs reuse the
        module's visual grammar (✔ ok, ✘ failed, ⛌ gap, ⏲ upcoming) so the strip
        speaks the same language as the cells. A fully clean window reads "all
        clear" rather than a row of zeros, so healthy is restful, not noisy — the
        de-noising the spec asks for, applied to the summary too.

        Degraded (R2-3): when the host's build was partial (some fetch failed) we
        REPLACE the normal summary with a "⚠ partial" warning. A degraded build
        suppresses gaps and may be missing runs/projections, so its counts would
        read falsely reassuring ("all clear" on a window we couldn't fully
        measure) — the warning is the honest signal, surfaced in the calendar's
        own strip rather than only the ephemeral status bar that a 10s refresh
        line can overwrite. Boolean only; naming which layer failed is DEF-CAL-08.
        """
        if self._degraded:
            self._health_label.setText(
                "⚠ partial — some data failed to load (⟳ to retry)"
            )
            self._update_issues_label()
            return
        h = self._health
        if h.failed == 0 and h.gaps == 0:
            # No failures and no missed slots → the restful state. We still note
            # ok/upcoming counts so the strip isn't blank, but lead with "all
            # clear" so a healthy window doesn't shout numbers.
            self._health_label.setText(
                f"All clear · {h.ok}✔ · {h.upcoming}⏲"
            )
            self._update_issues_label()
            return
        self._health_label.setText(
            f"{h.failed}✘ · {h.gaps}⛌ · {h.ok}✔ · {h.upcoming}⏲"
        )
        self._update_issues_label()

    # How many issue strings the strip lists before collapsing the rest into a
    # "(+N more)" suffix. The model's Health.issues can be long on a bad window;
    # showing a handful keeps the strip compact while still naming the offenders —
    # the full detail lives in the per-cell drill-down.
    _ISSUES_SHOWN = 4

    def _update_issues_label(self) -> None:
        """Render the cached Health.issues into the compact issues line.

        Reads self._health.issues (built by the model's summarize() — one "unit @
        date" per failure and per gap region). Only the COUNTS were surfaced
        before, so the user could see "1 failed" but not WHICH unit — this line
        closes that gap. Shows up to _ISSUES_SHOWN entries joined with " · ", with
        a "(+N more)" tail when there are more, so a bad window names its offenders
        without flooding the strip. A clean window (no issues) sets empty text, so
        the line takes no visible space. A degraded build also clears it: its
        counts are untrustworthy, so listing partial issues would mislead.
        """
        issues = [] if self._degraded else self._health.issues
        if not issues:
            self._issues_label.setText("")
            return
        shown = issues[: self._ISSUES_SHOWN]
        text = " · ".join(shown)
        extra = len(issues) - len(shown)
        if extra > 0:
            text += f"  (+{extra} more)"
        self._issues_label.setText(text)

    # -- painting -------------------------------------------------------------

    def _canvas_top(self) -> int:
        """Y of the painted canvas's top edge = below the LOWEST chrome strip.

        The chrome is the nav row plus the HEALTH strip plus the (sometimes
        hidden) filter strip, all laid out by the QVBoxLayout above the final
        stretch. We take the maximum bottom across every VISIBLE non-stretch item
        so the canvas starts directly under whatever chrome is currently showing —
        on Day/Week/matrix the filter strip is hidden, so the canvas sits higher
        than the Month grid's. Measuring (not a magic constant) keeps row_hit_point
        and every paint sharing one coordinate space whatever the platform font
        height — a click can't desync from what's drawn even as strips appear.

        A HIDDEN widget must be SKIPPED by visibility, NOT trusted to collapse:
        Qt keeps a hidden widget's last geometry, so the hidden filter strip
        reports a stale large bottom (~479px, probed 2026-06-20) that — if counted
        — pushed the Day/Week/matrix canvas hundreds of pixels down, leaving the
        grid stranded near the bottom edge. The Month grid escaped only because it
        derives its own bottom from self.height(); Week/matrix read this value
        directly, so the stale-geometry bug stranded exactly those two views.

        Why max-over-items rather than reading item 0: with three strips a fixed
        index would silently ignore the others, putting the canvas UNDER the
        chrome and corrupting every hit-test. The stretch item carries no widget,
        so we skip it too (no sensible bottom for the canvas).
        """
        layout = self.layout()
        if layout is None or layout.count() == 0:
            return 0
        bottom = 0
        # Bounded by layout.count() (a handful of items) — Power of Ten rule 2.
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            # The final addStretch item has no widget and no layout; skip it so
            # the stretch's full-height geometry never pushes the canvas down.
            if widget is None and item.layout() is None:
                continue
            # A hidden strip keeps a STALE geometry the layout does NOT collapse
            # — skip by visibility, or the hidden filter strip strands the canvas.
            if widget is not None and not widget.isVisible():
                continue
            # int() pins the type: PySide6's stubs type QRect.bottom() as Any,
            # which mypy --strict rejects; it is always an int pixel coordinate.
            bottom = max(bottom, int(item.geometry().bottom()))
        return bottom

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

            self._paint_hour_axis(painter, top, axis_left, axis_right, span)

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
        self,
        painter: QPainter,
        top: int,
        axis_left: int,
        axis_right: int,
        span: int,
    ) -> None:
        """Draw the Day time axis: a baseline, hourly minor ticks, and LOCAL-time
        hour labels every 3 hours (00 03 06 09 12 15 18 21).

        The Day window is local-midnight→midnight (24h, usually — a DST day is 23
        or 25h, which is exactly why each hour's x is derived from its OWN epoch,
        not from i/24). For each local hour we recompute that wall-clock instant as
        an absolute µs epoch and map it with the SAME `frac = (when - start)/span`
        the events use (_paint_row_events), so a tick and the glyphs under it share
        one mapping — a 06:00-local run's ✔ sits exactly under the "06" label. The
        hours are LOCAL (start.replace(hour=h)) so a PDT user reads wall-clock,
        never the UTC hour (the trap the task calls out: 06:00 local must label 06,
        not 13). Density: a label every 3h keeps the axis readable; minor ticks
        every hour give finer position cues without crowding the labels.

        `span` is the window width in µs (passed from _paint_day, == window_end -
        window_start, floored to ≥1). `axis_left`/`axis_right` bound the drawable
        axis (the gutter on the left, an 8px inset on the right) — the exact same
        bounds the event placement uses, so labels and glyphs can't desync.
        """
        baseline = top + _TOP_PAD - 4
        painter.setPen(QColor(90, 90, 90))
        painter.drawLine(axis_left, baseline, axis_right, baseline)

        if self._window_end <= self._window_start:
            return  # no window yet (pre-first-set_events) — just the baseline

        axis_w = axis_right - axis_left
        # The LOCAL midnight that starts this Day window. Reading window_start in
        # local time and zeroing the clock gives the wall-clock 00:00 every hour
        # tick is offset from — local (not UTC) so the labels read in the user's
        # zone. Hours past it are built with timedelta on the AWARE instant so a
        # DST transition inside the day shifts the ticks correctly.
        midnight_local = _local_dt(self._window_start).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # 25 ticks covers 00:00 through the next midnight inclusive, so a label can
        # sit at both ends; bounded by construction (Power of Ten rule 2).
        for h in range(25):
            tick_local = midnight_local + timedelta(hours=h)
            # The hour's absolute µs epoch: .astimezone() attaches the local tz so
            # .timestamp() is the correct UTC instant for that local wall-clock,
            # then the same frac mapping the events use places it on the axis.
            tick_usec = int(tick_local.astimezone().timestamp()) * 1_000_000
            frac = (tick_usec - self._window_start) / span
            if frac < 0.0 or frac > 1.0:
                continue  # outside the visible window (a DST-short/long day edge)
            x = int(axis_left + frac * axis_w)
            # Labelled hours (every 3h) get a taller tick + the "HH" text; the rest
            # get a short minor tick so the eye can still register hour granularity.
            labelled = tick_local.hour % 3 == 0
            tick_h = 6 if labelled else 3
            painter.setPen(QColor(110, 110, 110))
            painter.drawLine(x, baseline - tick_h, x, baseline)
            if labelled:
                painter.setPen(QColor(150, 150, 150))
                # Centre the 2-digit label on the tick; clamp the rect into the
                # axis so an edge label (00 / 24) isn't clipped off-canvas.
                lx = max(axis_left, min(x - 12, axis_right - 24))
                painter.drawText(
                    QRect(lx, top, 24, _TOP_PAD - 6),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{tick_local.hour:02d}",
                )

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
        LOCAL hour-of-day, then its glyph) so a single readout captures both WHEN in
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
            # Full-height day separators, drawn BEFORE the rows so the (subtle)
            # lines sit UNDER the glyphs — a divider must never overpaint an
            # outcome glyph. They run the whole row area so the 7 day-columns read
            # as distinct lanes top-to-bottom, not just in the header band.
            self._paint_week_separators(painter, top, grid_left, col_w)

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

    def _week_start_date(self) -> date:
        """The LOCAL date of the Week window's first column (its Monday).

        The window is local-Monday-00:00 aligned (local_calendar_window), so its
        local date IS the Monday the 7 columns count from. Derived once and shared
        by the header labels and the row bucketing so a column's label and the
        events placed under it always mean the same local day.
        """
        return _local_date(self._window_start)

    def _paint_week_header(
        self, painter: QPainter, top: int, grid_left: int, col_w: int
    ) -> None:
        """Draw the 7 day-column dividers and LOCAL date labels across the top.

        Labels are the window's local Monday + N local days (LOCAL, matching the
        local-aligned window and the local day-bucketing in _paint_week_row) — a
        UTC label here would disagree with where a near-midnight run is bucketed.
        Adding `timedelta(days=N)` to the local START DATE (not N×86400s to the µs
        epoch) keeps the labels correct across a DST transition inside the week.
        Kept minimal (a thin tick + "MMM DD") — richer formatting is the visual pass.
        """
        painter.setPen(QColor(90, 90, 90))
        baseline = top + _TOP_PAD - 4
        start_date = self._week_start_date()
        for day in range(7):
            x = grid_left + day * col_w
            painter.drawLine(x, top + 2, x, baseline)
            # Label each column with its LOCAL date so the week is anchored in
            # time exactly where its events are bucketed.
            col_date = start_date + timedelta(days=day)
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(
                QRect(x + 2, top, col_w - 4, _TOP_PAD - 4),
                Qt.AlignmentFlag.AlignVCenter,
                f"{col_date:%b %d}",
            )
            painter.setPen(QColor(90, 90, 90))

    def _paint_week_separators(
        self, painter: QPainter, top: int, grid_left: int, col_w: int
    ) -> None:
        """Draw visible full-height vertical dividers between the 7 day-columns.

        The header (_paint_week_header) only divides the label band; these extend
        the column boundaries down the WHOLE canvas so each glyph reads clearly as
        belonging to one day-lane top-to-bottom — including the empty space below
        the last timer, so the 7 columns read as a real week grid. Drawn BEFORE the
        rows so the lines sit UNDER the glyphs (a divider must never overpaint an
        outcome). The colour is a dim-but-VISIBLE grey: the previous near-background
        60,60,60 was countable by a pixel probe yet invisible to the eye, which read
        as no divider at all (Dustin's Week retest). We draw the 8 boundaries 0..7
        (left edge of col 0 through right edge of col 6) so the last column closes.
        """
        line_top = top + _TOP_PAD - 2
        line_bottom = self.height()  # full canvas height, not just the populated rows
        painter.setPen(QColor(108, 108, 116))  # dim but visible on the dark canvas
        # 8 boundaries close all 7 columns (0=left of col 0 … 7=right of col 6);
        # bounded loop (Power of Ten rule 2).
        for day in range(8):
            x = grid_left + day * col_w
            painter.drawLine(x, line_top, x, line_bottom)

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

        Events are bucketed into day-columns by their LOCAL date's offset from the
        window's local Monday. Each non-empty cell draws either an "HH glyph"
        worst-outcome readout or — above CELL_DRAW_MAX events — an aggregate band,
        the same over-full rule the Day view uses (the count and failure flag both
        come from the model's bucket_cell). The health column at the right shows
        the worst event across the whole week for this row.
        """
        # Bucket the row's events into 7 day-columns by LOCAL-DATE difference (NOT
        # a µs offset // _DAY_USEC): the window is local-Monday aligned and may not
        # be exactly 7×86400s wide across a DST transition, so a µs division could
        # misbucket a run near a local-midnight DST boundary. Subtracting local
        # dates is the user's-calendar-correct column index. A day index outside
        # [0, 6] (an event just outside the window from a prefetch) is dropped — it
        # has no column; the health summary still considers all events.
        start_date = self._week_start_date()
        columns: list[list[CalendarEvent]] = [[] for _ in range(7)]
        for ev in row_events:
            day = (_local_date(ev.when) - start_date).days
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
        event as its LOCAL hour-of-day then its glyph, so one readout carries both
        time-of-day and the worst outcome (spec §6's Week cell). The hour is LOCAL
        (was UTC — a glaring bug for a PDT user: a 06:00-local run read "13"); see
        _local_dt for the UTC-µs→local-display rule.
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
        hour = _local_dt(worst.when).hour  # LOCAL hour-of-day (see _local_dt)
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

    # How many per-timer name lines a Month cell shows before collapsing the rest
    # into a "+N more" line. 3 fits a normal cell height (~_ROW_H×… of vertical
    # space) without overflowing; beyond that the cell would clip. Dustin chose
    # timer NAMES in the Month cell (over a bare glyph+count), so the cell trades
    # density for legibility — this cap keeps it from becoming a wall of text.
    _MONTH_CELL_MAX_LINES = 3

    def _day_cell_timer_lines(
        self, events: list[CalendarEvent]
    ) -> list[tuple[str, str, QColor]]:
        """One (short_name, glyph, color) line per TIMER for a Month day-cell.

        `events` are every timer's events on one calendar day (already bucketed by
        the caller). Returns one line per distinct timer — that timer's WORST event
        for the day (failure > gap > success > upcoming via _day_cell_worst), so a
        day's row reads "which timers, and how did each fare". Ordered WORST-FIRST
        across timers (a failed timer sorts above a healthy one) so problems
        surface at the top of the cell — the de-noising goal applied within the
        cell. The short-name strips the .timer/.service suffix so the cell shows
        "backup", not "backup.timer"; truncation-to-fit is the painter's job (it
        knows the cell width), so this returns the full short name.

        Pure-ish (no painter, no self-state mutation) so the Month cell's per-timer
        readout has a directly testable core, mirroring _day_cell_summary.
        """
        # Group the day's events by timer, keep each timer's worst. dict preserves
        # first-seen order, which the severity sort below then overrides — so the
        # grouping order doesn't bias the final worst-first ordering.
        by_unit: dict[str, list[CalendarEvent]] = {}
        for ev in events:
            by_unit.setdefault(ev.unit, []).append(ev)
        worst_per_unit: list[CalendarEvent] = []
        for unit_events in by_unit.values():
            worst = self._day_cell_worst(unit_events)
            if worst is not None:
                worst_per_unit.append(worst)
        # Worst-first: higher severity sorts earlier so a ✘/⛌ leads the cell. A
        # stable sort keeps same-severity timers in first-seen order (harmless —
        # same glyph), so the ordering is deterministic for the test.
        worst_per_unit.sort(key=_event_severity, reverse=True)
        lines: list[tuple[str, str, QColor]] = []
        for ev in worst_per_unit:
            glyph, color = _glyph_color(ev)
            lines.append((_short_unit_name(ev.unit), glyph, color))
        return lines

    def _month_anchor(self) -> date:
        """The first day of the visible month, derived from window_start (LOCAL).

        The host sizes the Month window to the local calendar month; we take its
        START instant, read its LOCAL year/month, and anchor on the 1st — so the
        grid shows a WHOLE calendar month. LOCAL (not UTC) is load-bearing for a
        tz EAST of UTC: local 1st 00:00 is the PREVIOUS day in UTC, so a UTC read
        would pick the wrong month. Reading it local keeps the grid month and the
        local event-bucketing (_events_by_date) in the same timezone, avoiding an
        off-by-one at month ends.
        """
        start = _local_dt(self._window_start)
        return date(start.year, start.month, 1)

    def _month_weeks(self) -> list[list[date]]:
        """The visible month as a list of week-rows of 7 LOCAL dates each.

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
        """Bucket all events by their LOCAL calendar date.

        One pass over the flat event list (Power of Ten rule 2: bounded by the
        event count, itself bounded by the projection cap). The Month grid then
        looks each cell's date up here, so a day with no events is simply absent
        from the dict and draws blank. LOCAL (not UTC) matches the local grid dates
        from _month_weeks, so a run at e.g. 22:00 local (05:00 UTC next day) lands
        in the cell the user expects, not the next day's.
        """
        by_date: dict[date, list[CalendarEvent]] = {}
        for ev in self._events:
            d = _local_date(ev.when)
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
            now_date = _local_date(self._now)  # LOCAL "today" (see _local_dt)

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
                    # Pass the cell's EVENTS (not a pre-rendered glyph string): the
                    # cell now lists per-timer names+glyphs (Dustin's choice), which
                    # it derives from the events via _day_cell_timer_lines. `worst`
                    # still drives the dim/filter decision (one severity rule).
                    self._paint_month_cell(
                        painter, w_idx, wd_idx, day, worst, cell,
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

    def _cell_matches_filter(self, worst: CalendarEvent | None) -> bool:
        """Does this cell's worst event match the active Month-grid filter?

        Returns True (don't dim) when no filter is active (`_filter` is None) so
        the unfiltered grid is unchanged. With a filter set, the cell matches when
        its worst event's kind maps to the filter: "fail" → a failed run, "gap" →
        a gap, "upcoming" → a projected/approx run. An empty cell (worst is None)
        never matches a kind filter — there is nothing of that kind there, so it
        dims. Reading off the WORST event mirrors the cell's own glyph, so what
        the filter keeps bright is exactly what the cell draws (no surprise where
        a cell stays bright but shows a non-matching glyph).
        """
        if self._filter is None:
            return True  # All → nothing dimmed
        if worst is None:
            return False  # empty day can't match a kind filter → dim it
        if self._filter == "fail":
            return worst.kind == "ran" and worst.result == "failure"
        if self._filter == "gap":
            return worst.kind == "gap"
        if self._filter == "upcoming":
            return worst.kind in ("projected", "approx")
        return True  # unreachable (set_filter validates), but never over-dim

    def _paint_month_cell(
        self,
        painter: QPainter,
        week: int,
        weekday: int,
        day: date,
        worst: CalendarEvent | None,
        cell: list[CalendarEvent],
        in_month: bool,
        is_future: bool,
    ) -> None:
        """Draw one day-cell: border, day number, and up to N per-timer name lines.

        Days outside the visible month (grid padding) and future days are dimmed
        so the eye stays on the current month's past — a future or adjacent-month
        cell can't carry an outcome, so muting it is honest, not decorative. When
        a Month-grid filter is active, cells whose worst event doesn't match the
        filter are ALSO dimmed (and their lines drawn muted) so only the kind the
        user asked for stays bright — the filter never drops a cell, it just
        recedes the rest, keeping the month's shape intact.

        The body is Dustin's explicit choice: up to _MONTH_CELL_MAX_LINES per-timer
        rows ("<glyph> <short-name>", worst-first via _day_cell_timer_lines), each
        in its outcome colour, then a "+N more" line when the day has more timers
        than fit. A clean day with no events shows only its faint day number (the
        de-noising the spec asks for). `cell` is the day's events; `worst` (already
        computed by the caller from the same events) drives the dim/filter flag so
        the cell's dimming matches the worst-outcome rule the rest of the grid uses.
        """
        rect = self._month_cell_rect(week, weekday)
        # Cell outline — faint; the heavy problem-week border is drawn separately
        # on top so it dominates.
        painter.setPen(QColor(70, 70, 70))
        painter.drawRect(rect)

        # A cell dims when it's out-of-month, future, OR (filter active) its worst
        # event doesn't match the chosen kind. Folding the filter into the same
        # `dim` flag means one code path mutes both the day number and the lines,
        # so a filtered-out cell recedes uniformly rather than half-bright.
        filtered_out = not self._cell_matches_filter(worst)
        dim = (not in_month) or is_future or filtered_out

        # Day number, top-left. Dimmed per the flag above.
        num_color = QColor(90, 90, 90) if dim else QColor(170, 170, 170)
        painter.setPen(num_color)
        painter.drawText(
            QRect(rect.x() + 3, rect.y() + 2, rect.width() - 6, 16),
            Qt.AlignmentFlag.AlignLeft,
            str(day.day),
        )

        # Per-timer name lines — only on a non-empty in-month day (an adjacent-
        # month cell belongs to a neighbour view, so we don't summarise it here).
        if worst is None or not in_month:
            return
        self._paint_month_cell_lines(painter, rect, cell, filtered_out)

    def _paint_month_cell_lines(
        self,
        painter: QPainter,
        rect: QRect,
        cell: list[CalendarEvent],
        filtered_out: bool,
    ) -> None:
        """Draw the per-timer "<glyph> <short-name>" lines inside a Month cell.

        Splits the heavy body of _paint_month_cell out so each function stays one
        readable unit (Power of Ten rule 4). `rect` is the cell geometry; `cell`
        the day's events; `filtered_out` mutes the lines to grey (the filter recede)
        instead of their outcome colour. Shows up to _MONTH_CELL_MAX_LINES timers
        worst-first, then a "+N more" line for the overflow so a busy day signals
        there's more without clipping. Each line is one _ROW_LINE_H tall, starting
        below the day number; truncation to the cell width is left to QPainter's
        elided draw via the clipped rect (a long name just clips at the cell edge).
        """
        lines = self._day_cell_timer_lines(cell)
        shown = lines[: self._MONTH_CELL_MAX_LINES]
        line_h = 14  # one name row; small so 3 fit under the day number
        y = rect.y() + 18  # below the day-number band
        for short_name, glyph, color in shown:
            # A filtered-out line draws muted grey so un-asked-for kinds recede; a
            # matching (or unfiltered) line keeps its outcome colour.
            painter.setPen(QColor(95, 95, 95) if filtered_out else color)
            painter.drawText(
                QRect(rect.x() + 4, y, rect.width() - 8, line_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{glyph} {short_name}",
            )
            y += line_h
        overflow = len(lines) - len(shown)
        if overflow > 0:
            # "+N more" tells the user the day has timers beyond the first few,
            # drawn faint so it reads as a secondary hint, not another outcome.
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(
                QRect(rect.x() + 4, y, rect.width() - 8, line_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"+{overflow} more",
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
        """The visible month's days as a flat, ordered list of LOCAL dates.

        Built by flattening _month_weeks (LOCAL dates) and keeping only IN-MONTH
        days, so the matrix columns are exactly the calendar month's days (28–31 of
        them), never the adjacent-month padding the grid shows. Shared by the
        row-cell builder and the paint so a column always means the same day in both.
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
        # Bucket this timer's events by LOCAL date once, so each day-column is a
        # dict lookup rather than a re-scan of the whole event list per column.
        # LOCAL matches the local matrix columns (see _local_dt).
        by_date: dict[date, list[CalendarEvent]] = {}
        for ev in self._events:
            if ev.unit != unit:
                continue
            d = _local_date(ev.when)
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
        # unit's events by LOCAL date to recover the worst event's COLOUR (the cell
        # string carries only the glyph); the glyph itself comes from `cells` so
        # paint and the testable readout can never disagree. LOCAL matches the
        # local matrix columns (see _local_dt).
        by_date: dict[date, list[CalendarEvent]] = {}
        for ev in self._events:
            if ev.unit != unit:
                continue
            d = _local_date(ev.when)
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
        so a click here lands on the drawn cell. Locates `when`'s LOCAL date in the
        visible month's (local) week-rows; if the date isn't in the grid (a window
        the host shouldn't produce), falls back to the first cell's centre rather
        than raising — a hit-point helper must always return a point.
        """
        target = _local_date(when_usec)
        for w_idx, week in enumerate(self._month_weeks()):
            for wd_idx, day in enumerate(week):
                if day == target:
                    rect = self._month_cell_rect(w_idx, wd_idx)
                    return QPoint(rect.x() + rect.width() // 2,
                                  rect.y() + rect.height() // 2)
        rect = self._month_cell_rect(0, 0)
        return QPoint(rect.x() + rect.width() // 2, rect.y() + rect.height() // 2)

    # -- interaction ----------------------------------------------------------

    def set_run_enabled(self, enabled: bool) -> None:
        """Host toggle for the context menu's 'Run now' (user scope only)."""
        self._run_enabled = enabled

    def _build_context_menu(self, unit: str) -> QMenu:
        """Build the right-click menu for `unit`, wiring each action to emit
        menu_action(action, unit). Split out from contextMenuEvent so a test can
        trigger an action without driving a modal exec(). 'Run now' is greyed per
        _run_enabled; the host still enforces the real read-only guard."""
        menu = QMenu(self)
        run = menu.addAction("▶ Run now")
        run.setEnabled(self._run_enabled)
        if not self._run_enabled:
            run.setToolTip("system units are read-only by design")
        run.triggered.connect(lambda: self.menu_action.emit("run", unit))
        logs = menu.addAction("View logs")
        logs.triggered.connect(lambda: self.menu_action.emit("logs", unit))
        table = menu.addAction("Open in Timers")
        table.triggered.connect(lambda: self.menu_action.emit("table", unit))
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        """Right-click → Run now / View logs / Open in Timers for the unit under
        the cursor. Resolves the unit with the SAME geometry the left-click uses;
        off any unit, no menu. The widget emits intent (menu_action); the host
        executes and owns the read-only guard."""
        pos = event.pos()
        x, y = int(pos.x()), int(pos.y())
        unit = (
            self._month_click_unit(x, y)
            if self._mode == "month"
            else self._row_click_unit(y)
        )
        if unit is None:
            super().contextMenuEvent(event)
            return
        self._build_context_menu(unit).exec(event.globalPos())

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
        x, y = int(pos.x()), int(pos.y())
        if self._mode == "month":
            unit = self._month_click_unit(x, y)
        else:
            unit = self._row_click_unit(y)
        if unit is not None:
            self.selected.emit(unit)
            # Also route by the clicked OUTCOME (diagnostic click-through): resolve
            # the worst event at the same point and emit its category so the host
            # can steer a failure → Log tab, a gap → Schedule tab. `selected` above
            # already loaded the tabs; this only chooses which one to show.
            ev = self._event_at(x, y)
            if ev is not None:
                self.event_activated.emit(unit, _event_category(ev), ev.when)
            return
        super().mousePressEvent(event)

    def _event_at(self, x: int, y: int) -> CalendarEvent | None:
        """The WORST event at a click point, or None off any target.

        Day/Week: the worst event of the clicked timer ROW. Month: the worst event
        of the clicked day-CELL. Reuses the SAME geometry (_row_click_unit /
        _month_cell_rect) and the SAME _event_severity the paint uses, so the click
        resolves to exactly the event whose glyph dominates the cell — what the user
        is aiming at.
        """
        if self._mode == "month":
            by_date = self._events_by_date()
            for w_idx, week in enumerate(self._month_weeks()):
                for wd_idx, day in enumerate(week):
                    if self._month_cell_rect(w_idx, wd_idx).contains(x, y):
                        _glyphs, worst, _counts = self._day_cell_summary(
                            by_date.get(day, [])
                        )
                        return worst
            return None
        unit = self._row_click_unit(y)
        if unit is None:
            return None
        row_events = [e for e in self._events if e.unit == unit]
        return max(row_events, key=_event_severity) if row_events else None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        """Reveal a Month day-cell's FULL timer list as a tooltip on hover.

        The cell paints only the first _MONTH_CELL_MAX_LINES timers + "+N more";
        hovering shows the rest (Dustin asked the overflow be expandable, and a
        left-click is already taken by select). Month mode only — Day/Week show
        every event inline. Reuses the same _month_cell_rect / _events_by_date /
        _day_cell_timer_lines the paint uses, so the tooltip can never disagree with
        the painted cell. mouseTracking (enabled in __init__) makes this fire on a
        bare hover, not only while a button is held.
        """
        if self._mode == "month":
            pos = event.position()
            x, y = int(pos.x()), int(pos.y())
            by_date = self._events_by_date()
            for w_idx, week in enumerate(self._month_weeks()):
                for wd_idx, day in enumerate(week):
                    if self._month_cell_rect(w_idx, wd_idx).contains(x, y):
                        text = self._month_day_tooltip_text(day, by_date.get(day, []))
                        if text:
                            QToolTip.showText(
                                event.globalPosition().toPoint(), text, self
                            )
                        else:
                            QToolTip.hideText()
                        super().mouseMoveEvent(event)
                        return
            QToolTip.hideText()
        super().mouseMoveEvent(event)

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

    def _month_day_tooltip_text(self, day: date, cell: list[CalendarEvent]) -> str:
        """Build the hover-tooltip body for a Month day-cell: EVERY timer that day.

        The cell truncates to _MONTH_CELL_MAX_LINES names + "+N more"; this returns
        the full worst-first list (one "<glyph> <short-name>" per timer, from the
        SAME _day_cell_timer_lines the cell paints) under a local-date header, so
        the overflow is fully readable on hover. Empty string for a day with no
        events → mouseMoveEvent shows no tooltip.
        """
        lines = self._day_cell_timer_lines(cell)
        if not lines:
            return ""
        body = "\n".join(f"{glyph} {short_name}" for short_name, glyph, _color in lines)
        return f"{day:%a %b %d}\n{body}"
