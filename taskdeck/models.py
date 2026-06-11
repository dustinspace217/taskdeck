"""Table model and time rendering for Task Deck.

Rendering lives here (not in systemd_client) so the client remains a faithful
transcription of systemd's µs epochs and the same fixtures drive both layers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from taskdeck.systemd_client import LastResult, ServiceRow, TimerRow


def format_delta(seconds: float) -> str:
    """Render a positive duration with two significant units, DSM-style.

    Unit pairs chosen for glanceability: seconds alone under a minute, then
    m / h+m / d+h, then bare days past ~5 weeks where hours are noise.
    """
    # Callers must pass non-negative durations (format_when negates past
    # deltas before calling). A negative here is a caller bug — fail loudly
    # at dev time rather than rendering "-345600s" in the table.
    assert seconds >= 0, "format_delta requires non-negative seconds"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        hours, rem = divmod(s, 3600)
        return f"{hours}h {rem // 60}m"
    days, rem = divmod(s, 86400)
    if days >= 35:
        return f"{days}d"
    return f"{days}d {rem // 3600}h"


def format_when(ts_usec: int | None, now: datetime) -> str:
    """Render a µs epoch as 'absolute (relative)' — e.g. 'today 23:10 (in 4h 10m)'.

    `now` is a parameter, not datetime.now(), so tests are deterministic and
    the table can render one consistent instant per refresh. Absolute form
    scales with distance: today / weekday within 6 days / 'Mon DD' beyond.
    """
    if ts_usec is None:
        return "—"
    dt = datetime.fromtimestamp(ts_usec / 1_000_000)
    # Epoch-space subtraction, not (dt - now): both datetimes are naive LOCAL,
    # and wall-clock subtraction goes off by ±1h when the interval spans a DST
    # transition. now.timestamp() interprets naive-local consistently.
    delta = ts_usec / 1_000_000 - now.timestamp()
    if dt.date() == now.date():
        absolute = f"today {dt:%H:%M}"
    elif abs(delta) < 6 * 86400:
        absolute = f"{dt:%a %H:%M}"
    else:
        absolute = f"{dt:%b %d %H:%M}"
    relative = f"in {format_delta(delta)}" if delta >= 0 else f"{format_delta(-delta)} ago"
    return f"{absolute} ({relative})"


COLUMNS = ("Task", "Status", "Next run", "Last run", "Last result")

# Custom item roles so the window can recover unit names from a selected row
# without parsing display text (display strings are for humans only).
ROLE_UNIT = int(Qt.ItemDataRole.UserRole) + 1
ROLE_ACTIVATES = int(Qt.ItemDataRole.UserRole) + 2
# Sort keys served separately from display strings: the proxy must sort the
# time columns by raw µs epochs — display-string sorting would order
# "Jun 12" < "today", which is chronologically meaningless. The window sets
# proxy.setSortRole(ROLE_SORT).
ROLE_SORT = int(Qt.ItemDataRole.UserRole) + 3

# The only column that gets result tinting; index derived, not hardcoded.
_RESULT_COL = COLUMNS.index("Last result")

# Muted red/green that read on both Breeze light and dark; exact shades are
# cosmetic, the INFORMATION is also in the ✔/✘ glyphs (color-blind safe).
_GREEN = QBrush(QColor(0x4C, 0xAF, 0x50))
_RED = QBrush(QColor(0xE5, 0x73, 0x73))


# Type aliases keep the row-tuple annotations readable. DisplayTuple holds one
# rendered string per column; SortTuple holds the per-column sort key served
# via ROLE_SORT (ints for the time columns → chronological sorting).
DisplayTuple = tuple[str, str, str, str, str]
SortTuple = tuple[str, str, int, int, str]
Row = tuple[DisplayTuple, SortTuple, str, str, bool | None]

# Sentinels for missing timestamps in sort keys: a timer with no next elapse
# sorts LAST ascending (max int64); a unit that never ran sorts OLDEST (-1).
_SORT_NO_NEXT = 2**63 - 1
_SORT_NO_LAST = -1


def _result_text(result: LastResult | None) -> str:
    """Render a LastResult as the result-column string ("—" when unknown)."""
    if result is None:
        return "—"
    if result.result == "success":
        return "✔ success"
    status = f" ({result.exec_status})" if result.exec_status is not None else ""
    return f"✘ {result.result}{status}"


class TaskTableModel(QAbstractTableModel):  # type: ignore[misc]
    """Five-column model serving both the Timers and Services views.

    Rows are precomputed display tuples: set_*_rows() renders everything once
    per refresh (cheap — tens of rows), so data() is a trivial lookup and the
    view never re-renders timestamps on every paint. `now` is taken at set
    time so the whole table reflects ONE instant.
    """

    def __init__(self) -> None:
        super().__init__()
        # Each row: (display, sort_keys, unit, activates, ok) — see Row alias.
        self._rows: list[Row] = []

    # -- population ---------------------------------------------------------

    def set_timer_rows(
        self,
        timers: list[TimerRow],
        services: list[ServiceRow],
        results: dict[str, LastResult],
        now: datetime,
    ) -> None:
        """Populate the Timers view: one row per timer, joined to its service.

        services supplies live state for the activated unit; results supplies
        last-run outcomes (absent key = unknown, rendered "—").
        """
        services_by_name = {s.unit: s for s in services}
        rows: list[Row] = []
        for t in timers:
            svc = services_by_name.get(t.activates)
            # "activating" counts as running: Type=oneshot services (the
            # normal shape for timer jobs — probed: astrowidget-fetch is one)
            # report ActiveState=activating for their WHOLE ExecStart run and
            # only reach "active" at exit (man systemd.service).
            if svc is not None and svc.active in ("active", "activating"):
                status = "▶ running"
            elif t.next_usec is not None:
                status = "⏲ waiting"
            else:
                status = "○ inactive"
            result = results.get(t.activates)
            display = (
                t.unit,
                status,
                format_when(t.next_usec, now),
                format_when(t.last_usec, now),
                _result_text(result),
            )
            sort = (
                t.unit,
                status,
                t.next_usec if t.next_usec is not None else _SORT_NO_NEXT,
                t.last_usec if t.last_usec is not None else _SORT_NO_LAST,
                display[4],
            )
            ok = None if result is None else result.result == "success"
            rows.append((display, sort, t.unit, t.activates, ok))
        self._reset(rows)

    def set_service_rows(
        self,
        services: list[ServiceRow],
        results: dict[str, LastResult],
        now: datetime,
    ) -> None:
        """Populate the Services view: one row per service, self-activated."""
        rows: list[Row] = []
        for s in services:
            # Same oneshot rationale as the Timers view: "activating" IS the
            # running phase for oneshot services.
            running = s.active in ("active", "activating")
            status = "▶ running" if running else f"○ {s.sub}"
            if s.active == "failed":
                status = "✘ failed"
            result = results.get(s.unit)
            display = (s.unit, status, "—", "—", _result_text(result))
            sort = (s.unit, status, _SORT_NO_NEXT, _SORT_NO_LAST, display[4])
            ok = None if result is None else result.result == "success"
            # Services activate themselves; ROLE_ACTIVATES = own unit name.
            rows.append((display, sort, s.unit, s.unit, ok))
        self._reset(rows)

    def _reset(self, rows: list[Row]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    # -- QAbstractTableModel interface ---------------------------------------

    # Parameter types are the UNION the Qt API actually passes at runtime
    # (QModelIndex | QPersistentModelIndex — what PySide6's .pyi stubs declare).
    # NOTE: Fedora's RPM omits the PEP 561 py.typed marker, so mypy currently
    # treats PySide6 as Any and does NOT enforce these; the union is kept
    # because it is true to runtime and becomes load-bearing if the stubs ever
    # turn visible (marker added upstream, or typeshed-style stubs installed).
    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        # isValid() does NOT bounds-check against the CURRENT row count — an
        # index captured before a shrinking reset and dereferenced after would
        # raise IndexError (Power of Ten rule 5: check what can actually fail).
        if not 0 <= index.row() < len(self._rows):
            return None
        display, sort, unit, activates, ok = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return display[index.column()]
        if role == ROLE_SORT:
            return sort[index.column()]
        if role == ROLE_UNIT:
            return unit
        if role == ROLE_ACTIVATES:
            return activates
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and index.column() == _RESULT_COL
            and ok is not None
        ):
            return _GREEN if ok else _RED
        return None
