"""MainWindow: DSM-style layout A — toolbar, full-width table, tabbed detail pane.

This is the only module that constructs widgets. All systemd I/O goes through
the injected SystemdClient (constructor arg, so tests inject one and never
spawn subprocesses); all action policy lives in actions.py.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from PySide6.QtCore import QItemSelectionModel, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTabWidget,
)

from taskdeck.actions import ActionNotAllowed, action_argv
from taskdeck.calendar_model import (
    FWD_PROJECTION_ITERATIONS,
    GAP_TOLERANCE_USEC,
    CalendarEvent,
    cadence_interval_usec,
    compute_gaps,
    local_calendar_window,
    parse_projection,
    parse_run_journal,
    projection_iterations,
)
from taskdeck.calendar_view import CalendarView
from taskdeck.models import (
    ROLE_ACTIVATES,
    ROLE_SORT,
    ROLE_UNIT,
    TaskTableModel,
    classify_cadence,
    strip_ansi,
    ts_missing,
)
from taskdeck.systemd_client import (
    SCOPE_SYSTEM,
    SCOPE_USER,
    LastResult,
    ScheduleInfo,
    ServiceRow,
    SystemdClient,
    TimerRow,
    parse_journal,
    parse_list_timers,
    parse_list_units,
    parse_show_results,
    parse_show_schedules,
)


class MainWindow(QMainWindow):  # type: ignore[misc]
    """Assembles the v1 UI and orchestrates refresh cycles.

    Refresh design: a cycle = list-timers + list-units for the active scope;
    when both land, TWO batched `show` fetches enrich them — one for every
    relevant service's last result, one for every timer's effective triggers
    (the Cadence column). A render barrier (_pending_enrich) holds the table
    until BOTH land, so Cadence and Last-result appear together. Tab content
    (log/details/schedule/unit file) loads lazily on row selection.
    SystemdClient's single-flight coalescing bounds concurrent subprocesses;
    responses for a scope the user has already navigated away from are dropped
    by the scope check in _on_finished.

    Request-id dispatch convention: only the KIND (text before the first ":")
    is ever parsed out of a request id. Unit names are NEVER recovered from
    ids — ":" is legal inside unit names — they come from the selection model.
    """

    REFRESH_MS = 10_000

    def __init__(self, client: SystemdClient, auto_refresh: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("Task Deck")
        self.client = client
        self.scope = SCOPE_USER
        self._timers: list[TimerRow] = []
        self._services: list[ServiceRow] = []
        self._pending: set[str] = set()
        # Unit lists keyed by the EXACT request id, so every response parses
        # against the argv that produced it — a scope flip mid-flight can no
        # longer misalign blocks against another scope's units (QA synthesis
        # #6). Bounded: ≤1 in-flight per scope per kind → ≤2 entries each.
        self._result_units_by_id: dict[str, list[str]] = {}
        self._schedule_units_by_id: dict[str, list[str]] = {}
        # Enrichment fetches still outstanding for the current cycle; the
        # table renders when this empties so the Cadence and Last-result
        # columns appear together instead of popping in separately.
        self._pending_enrich: set[str] = set()
        self._last_schedules: dict[str, ScheduleInfo] = {}
        # Which scope the RENDERED rows came from. Action enablement requires
        # it to match self.scope — otherwise the sub-second window after a
        # scope flip leaves the old scope's rows interactive (QA synthesis #7).
        self._data_scope: str | None = None
        # Last parsed results, cached so the show-inactive toggle re-renders
        # locally — a refresh() there would be single-flight-rejected
        # mid-cycle and the checkbox would look dead for up to 10s.
        self._last_results: dict[str, LastResult] = {}
        # Kind of the currently displayed error, for kind-aware clearing:
        # a fetch error is cleared by the SAME kind succeeding (recovery),
        # while action messages persist until the next action (QA synthesis #5).
        self._error_kind: str | None = None
        # Freshness filter for detail tabs: only responses whose FULL id is in
        # this set may write a tab (string equality, no id parsing — colons are
        # legal in unit names). Replaced wholesale on each selection, so stale
        # responses for a unit the user left are dropped, and the calendar
        # response can never append under another unit's schedule. Bounded: ≤4.
        self._expected_tab_ids: set[str] = set()
        # Dedup guard: the selected unit whose tabs are already loaded. Lets
        # the post-refresh selection restore re-select without re-fetching
        # (and re-flashing "loading…") every 10s cycle.
        self._last_detail_unit: str | None = None

        # -- Calendar-page build state (page 1 of the central stack) ----------
        # The calendar fans MANY fetches in per build (a coverage probe, one
        # journal query, and one projection per eligible timer×expression) and
        # renders only when they ALL land — a separate fan-in barrier from the
        # table's _pending_enrich, because the two pages are independent and a
        # calendar build must never stall (or be stalled by) a table refresh
        # cycle. _cal_pending holds the calcover/calproj/caljournal ids still
        # outstanding for the CURRENT build.
        self._cal_pending: set[str] = set()
        self._cal_events: list[CalendarEvent] = []
        self._cal_units: list[str] = []
        self._cal_window: tuple[int, int] = (0, 0)
        self._cal_now: int = 0
        # Per-build GENERATION counter (F3). The same timers exist across rapid
        # rebuilds, so an unstamped request id (caljournal:user, calproj:user:X)
        # would REPEAT build-to-build — and the client's single-flight registry
        # keys by id, so Build A's in-flight response could satisfy Build B's
        # barrier with Build A's (stale-window) slots. Bumping the generation and
        # embedding it in every fan-in id (@gen / #i@gen) makes each build's ids
        # UNIQUE; a superseded build's response then carries an id that is no
        # longer in the new build's _cal_pending, so the existing not-in-pending
        # guard drops it for free. (The earlier draft threaded an explicit
        # generation arg via a per-id map — deleted: the map was overwritten by
        # the repeating ids, so it never actually fixed the bug. Stamping the id
        # is the real fix.)
        self._cal_gen: int = 0
        # The journal-coverage floor for THIS build (F1), set by _on_cal_coverage
        # once the coverage probe lands and read by _finalize_calendar as
        # compute_gaps' coverage_start. A gap before this floor is "no data", not
        # a miss. Reset to None at build start (R2-3): None means the floor never
        # landed (the coverage probe failed before _on_cal_coverage could set it),
        # which _finalize_calendar reads as "suppress gaps entirely" rather than
        # judging against a 0/epoch floor that would bloom gaps back to the dawn
        # of time. The probe (or its failure path) is what fills it; projections
        # are gated behind a SUCCESSFUL probe so the floor is always known (and
        # non-None) before any slot is judged on the happy path.
        self._cal_coverage_start: int | None = None
        # Whether THIS build's render is DEGRADED (R2-3): any calendar fetch
        # (coverage probe, journal, or a projection) failed or poisoned, so the
        # render is partial. Reset False at _build_calendar; set True in the
        # _on_failed and _on_finished-except calendar barrier-release paths.
        # Passed to set_events so the calendar's own HEALTH strip can surface the
        # degradation ("⚠ partial — some data failed to load") — not just the
        # ephemeral status bar, which a 10s refresh line can overwrite. Boolean
        # only; naming WHICH layer failed is deferred (DEF-CAL-08).
        self._cal_degraded: bool = False
        # The projection PLAN for THIS build: one (unit, expr_idx, expr,
        # iterations) entry per eligible timer×OnCalendar-expression (F4 projects
        # EVERY expression, not just calendar[0]). Computed at build start but
        # NOT fired until the coverage probe lands (F1 gating) — held here in the
        # meantime. Bounded by eligible-timers × expressions-per-timer.
        self._cal_proj_plan: list[tuple[str, int, str, int]] = []
        # The FORWARD-projection plan for THIS build: one (unit, expr_idx, expr)
        # entry per eligible timer×expression, projected from NOW with the small
        # FWD_PROJECTION_ITERATIONS budget so EVERY cadence yields upcoming slots.
        # The win_start plan above is sized to cover the window and is capped, so a
        # fast cadence (minutely, or hourly at Month scale) burns the cap on PAST
        # slots and produces no future one — the "upcoming doesn't show" symptom.
        # This separate now-based projection guarantees a handful of `projected`
        # slots regardless. No iteration count is stored (it's always the constant);
        # fired alongside the win_start plan once coverage lands. Bounded the same.
        self._cal_fwd_plan: list[tuple[str, int, str]] = []
        # Per-timer slots accumulated from projection responses in THIS build,
        # held until the journal lands so gaps can be computed (a gap needs both
        # the scheduled slots AND the actual runs). Keyed by timer unit name;
        # F4 UNIONS each expression's slots into the unit's list.
        self._cal_slots: dict[str, list[int]] = {}
        # calproj request id → its timer unit name, recorded at fire time. The
        # unit is NOT recovered by splitting the id (":" is legal inside unit
        # names — see the class docstring's request-id convention, and the id now
        # also carries a #exprIdx@gen tag); it is looked up here exactly as
        # _result_units_by_id recovers a results fetch's unit list. Bounded by
        # the number of eligible timer×expression projections in one build.
        self._cal_proj_unit_by_id: dict[str, str] = {}
        # service-name → timer-name map for THIS build, so the pure journal
        # parser can bucket run records back to their timer (runs/projections/
        # gaps are all keyed by timer, not the activated service).
        self._cal_service_to_timer: dict[str, str] = {}

        self.model = TaskTableModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)
        # Sort by raw keys (epochs for the time columns), not display strings —
        # "Jun 12" < "today" alphabetically is chronological nonsense.
        self.proxy.setSortRole(ROLE_SORT)

        self._build_toolbar()
        self._build_central()
        # Freshness lives in a PERMANENT widget: showMessage() would let the
        # routine refresh line overwrite a posted error within one 10s cycle
        # — the routine channel erasing the only record of a one-shot failure
        # (QA synthesis #5). Permanent widgets coexist with showMessage.
        self._freshness = QLabel("starting…")
        self.statusBar().addPermanentWidget(self._freshness)
        self.statusBar().showMessage("Ready")

        self.client.finished.connect(self._on_finished)
        self.client.failed.connect(self._on_failed)

        # Auto-refresh while the window lives; stopped in closeEvent. 10s per
        # spec — fast enough to feel live, slow enough to be invisible load.
        # auto_refresh=False lets tests build the window with zero subprocess
        # side effects (and set_scope honors the same flag).
        self._auto_refresh = auto_refresh
        # Close-to-tray state. _hide_to_tray is flipped on by app.py ONLY when a
        # system tray is available; without a tray, close quits as usual.
        # _quitting is set by the tray's Quit so a real exit isn't intercepted.
        self._hide_to_tray = False
        self._quitting = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        if auto_refresh:
            self._timer.start()
            self.refresh()

    # -- construction --------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("Main")
        bar.setMovable(False)

        self.scope_box = QComboBox()
        self.scope_box.addItem("User", SCOPE_USER)
        self.scope_box.addItem("System 🔒 (read-only)", SCOPE_SYSTEM)
        self.scope_box.currentIndexChanged.connect(
            lambda _i: self.set_scope(self.scope_box.currentData())
        )
        bar.addWidget(QLabel(" Scope: "))
        bar.addWidget(self.scope_box)

        self.view_box = QComboBox()
        # "Calendar" is a THIRD view, not a third scope: it swaps the central
        # stack to the CalendarView page rather than re-querying the table.
        # _on_view_changed branches on the selected text so the Timers/Services
        # table refresh and the Calendar page-swap stay distinct — a bare
        # refresh() here would re-query systemctl when the user only wanted to
        # look at the calendar.
        self.view_box.addItems(["Timers", "Services", "Calendar"])
        self.view_box.currentIndexChanged.connect(lambda _i: self._on_view_changed())
        bar.addWidget(QLabel(" View: "))
        bar.addWidget(self.view_box)

        # Spec'd toggle (QA synthesis #10): without it, a service stopped
        # from the GUI vanishes from the filtered view and the inverse
        # operation is unreachable — Stop becomes a one-way door.
        self.act_show_inactive = QAction("Show inactive", self)
        self.act_show_inactive.setCheckable(True)
        # Local re-render from cached data, deliberately NOT refresh().
        self.act_show_inactive.toggled.connect(lambda _c: self._render_rows())
        bar.addAction(self.act_show_inactive)
        bar.addSeparator()

        # QActions (not buttons) so enable/disable state is one flag per verb.
        self.act_run = QAction("▶ Run now", self)
        self.act_enable = QAction("⏻ Enable", self)
        self.act_disable = QAction("⏸ Disable", self)
        self.act_stop = QAction("■ Stop", self)
        self.action_buttons = [self.act_run, self.act_enable, self.act_disable, self.act_stop]
        self.act_run.triggered.connect(lambda: self._do_action("start", run_now=True))
        self.act_enable.triggered.connect(lambda: self._do_action("enable"))
        self.act_disable.triggered.connect(lambda: self._do_action("disable"))
        self.act_stop.triggered.connect(lambda: self._do_action("stop"))
        for act in self.action_buttons:
            bar.addAction(act)
        bar.addSeparator()

        refresh = QAction("⟳ Refresh", self)
        # Manual ⟳ is the universal recovery gesture: it also invalidates the
        # detail-tab dedup, so frozen or stale tabs refetch (QA synthesis #4).
        refresh.triggered.connect(self._manual_refresh)
        bar.addAction(refresh)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("filter…")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.setMaximumWidth(220)
        self.filter_box.textChanged.connect(self.proxy.setFilterFixedString)
        bar.addWidget(self.filter_box)

    def _build_central(self) -> None:
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)

        self.tabs = QTabWidget()
        self.tab_log = self._make_text_tab("Log")
        self.tab_details = self._make_text_tab("Details")
        self.tab_schedule = self._make_text_tab("Schedule")
        self.tab_unitfile = self._make_text_tab("Unit file")
        # Shared by the failure AND parse-error paths — defined once, because
        # a typo'd key in a duplicated literal would silently no-op (.get
        # returning None is invisible), exactly the failure class this app
        # exists to avoid.
        self._kind_to_tab = {
            "log": self.tab_log,
            "details": self.tab_details,
            "cat": self.tab_unitfile,
            "calendar": self.tab_schedule,
            "schedtab": self.tab_schedule,
        }

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        # Central widget is a QStackedWidget so the table view and the calendar
        # view are two independent pages the View dropdown swaps between. A
        # stack (not a tab widget) because the page is chosen from the existing
        # View dropdown, not a second set of tabs — one selection control, no
        # redundant chrome. Page 0 = the table+tabs splitter (the default v1
        # surface); page 1 = the CalendarView. CalendarView owns its OWN nav
        # state (mode + window), so swapping pages never disturbs the table's
        # selection/scroll restore path (spec §7).
        self._stack = QStackedWidget()
        self._stack.addWidget(splitter)              # index 0 — table page
        self.calendar_view = CalendarView()
        self._stack.addWidget(self.calendar_view)    # index 1 — calendar page
        # selected(unit): a calendar row click feeds the SAME detail tabs the
        # table selection uses, via the adapter. rebuild(start, end): a nav move
        # asks the host to refetch exactly the new window.
        self.calendar_view.selected.connect(self._on_calendar_selected)
        self.calendar_view.rebuild.connect(self._build_calendar)
        self.setCentralWidget(self._stack)
        self._update_action_enablement()

    def _make_text_tab(self, title: str) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setReadOnly(True)
        # Monospace via style hint (not a hardcoded family) so KDE's
        # configured fixed-width font is respected.
        font = view.font()
        font.setStyleHint(font.StyleHint.Monospace)
        font.setFamily("monospace")
        view.setFont(font)
        self.tabs.addTab(view, title)
        return view

    # -- refresh orchestration -------------------------------------------------

    def set_scope(self, scope: str) -> None:
        self.scope = scope
        # Same unit name in another scope is a DIFFERENT unit — force refetch,
        # drop in-flight tab responses from the old scope, and stop showing
        # the old scope's tab content under the new label.
        self._last_detail_unit = None
        self._expected_tab_ids = set()
        for view in self._kind_to_tab.values():
            view.setPlainText("(select a unit)")
        self._update_action_enablement()
        if self._auto_refresh:
            self.refresh()

    def _on_view_changed(self) -> None:
        """Handle a View-dropdown change: Timers/Services refresh the table page;
        Calendar swaps to the calendar page and triggers a build.

        Branching on the selected TEXT (not the index) keeps this readable and
        rename-safe — the alternative, a bare refresh() on every change, would
        re-query systemctl when the user only wanted to view the calendar, and
        would fall the calendar's index into _render_rows' Services `else`
        branch (rendering the calendar index as a service table). The page swap
        is the whole point of the Calendar entry.
        """
        if self.view_box.currentText() == "Calendar":
            self._stack.setCurrentWidget(self.calendar_view)
            # Build the visible window now so the page isn't blank on first show.
            # The CalendarView starts in Day mode; build the LOCAL calendar day
            # CONTAINING now (local 00:00 → next local 00:00), not [now-24h, now].
            # The old [now-24h, now] ended AT now, so nothing was ever projected
            # on first show (every future slot is > now = the window's right edge);
            # a now-containing day has room to the right of the now-marker for
            # today's upcoming runs. The window edges come from the model's pure
            # local_calendar_window so first-show and the view's nav_today agree on
            # exactly which day "today" is. auto_refresh=False (tests) fires zero
            # subprocesses here.
            if self._auto_refresh:
                now_usec = int(datetime.now(UTC).timestamp() * 1_000_000)
                win_start, win_end = local_calendar_window("day", now_usec)
                self._build_calendar(win_start, win_end)
            return
        # Timers or Services → back to the table page, and refresh it (the
        # column set differs between the two, so a re-query is correct).
        self._stack.setCurrentIndex(0)
        self.refresh()

    def _manual_refresh(self) -> None:
        """⟳: refresh AND invalidate the tab dedup so the selected unit's
        tabs refetch — the user-reachable recovery path for any frozen or
        stale detail state (parse failures deliberately do not auto-retry)."""
        self._last_detail_unit = None
        self.refresh()

    def refresh(self) -> None:
        self._pending = {f"timers:{self.scope}", f"services:{self.scope}"}
        # Return values deliberately ignored: a single-flight rejection means
        # the SAME id is already in flight, which satisfies _pending equally.
        self.client.list_timers(self.scope)
        self.client.list_services(self.scope)

    def _post_error(self, kind: str, message: str) -> None:
        """All error posting routes through here: timestamped (a persistent
        error must be self-dating, or a long-resolved one reads as current)
        and kind-tagged for the clearing rule in _clear_error_if."""
        self._error_kind = kind
        self.statusBar().showMessage(f"ERROR {datetime.now():%H:%M:%S} {message}", 0)

    def _clear_error_if(self, kind: str) -> None:
        """Clear the displayed error when the SAME kind has now succeeded —
        overwrite-on-recovery is correct for fetch channels. Action messages
        use the kind 'action' and are never cleared here; they persist until
        the next action or error replaces them."""
        if self._error_kind == kind:
            self._error_kind = None
            self.statusBar().clearMessage()

    def _on_finished(self, request_id: str, stdout: str) -> None:
        # ONLY the kind is parsed from the id (see class docstring).
        kind = request_id.split(":", 1)[0]
        try:
            self._dispatch_finished(kind, request_id, stdout)
        except (ValueError, KeyError) as exc:
            # Parse/validation failures surface as error states, never an
            # empty table pretending systemd has no units. !r so a terse
            # KeyError payload still names the missing key.
            self._post_error(kind, f"parsing {request_id}: {exc!r}")
            if kind in ("results", "schedules"):
                # An enrichment parse failure must NOT freeze the table. Release
                # the render barrier so the cycle renders with STALE (or empty)
                # results/cadence — a table that never shows new Next-run times
                # is a worse failure than one stale column, and re-raising every
                # 10s on a poison payload (one exotic trigger line, or systemd
                # reshaping its `show` text) would freeze BOTH views forever
                # (QA 2026-06-12 FREEZE-1). The by-id entry was already popped
                # before the raising parse — nothing dangles — and the failed
                # assignment left _last_results/_last_schedules at their prior
                # value, which is exactly the stale data we want to render. The
                # error stays loud on the status bar; ⟳ and the next clean cycle
                # self-heal.
                self._pending_enrich.discard(kind)
                self._maybe_render()
                return
            if kind in ("calcover", "caljournal", "calproj") and request_id in self._cal_pending:
                # F2 belt-and-suspenders: a parse error inside a calendar fan-in
                # handler (e.g. a future parser that raises on a malformed line)
                # must NOT wedge the barrier. The model parse-skip fixes mean
                # this rarely fires, but a poison response must never leave
                # _cal_pending non-empty forever — release the id and
                # finalize-partial so the page renders whatever DID land. Mirrors
                # the _on_failed calendar branch; the error stays loud, the next
                # nav/tick rebuilds. The poisoned fetch makes the render partial,
                # so mark it degraded (R2-3) — the HEALTH strip surfaces it.
                self._cal_degraded = True
                self._cal_proj_unit_by_id.pop(request_id, None)
                self._cal_pending.discard(request_id)
                if not self._cal_pending:
                    self._finalize_calendar()
                return
            tab = self._kind_to_tab.get(kind)
            if tab is not None and request_id in self._expected_tab_ids:
                # A tab frozen at "loading…" would hide the failure. The
                # dedup is deliberately LEFT SET: auto-retrying a persistently
                # corrupt source would strobe "loading…" + three subprocess
                # spawns every cycle (QA Phase B break) — ⟳ is the retry path,
                # and the error stays visible right here meanwhile.
                tab.setPlainText(f"(parse failed)\n{exc!r}")

    def _dispatch_finished(self, kind: str, request_id: str, stdout: str) -> None:
        if kind == "timers":
            if request_id != f"timers:{self.scope}":
                return  # stale response from a scope the user left
            self._timers = parse_list_timers(stdout)
            self._pending.discard(request_id)
            self._clear_error_if(kind)
        elif kind == "services":
            if request_id != f"services:{self.scope}":
                return
            self._services = parse_list_units(stdout)
            self._pending.discard(request_id)
            self._clear_error_if(kind)
        elif kind == "results":
            units = self._result_units_by_id.pop(request_id, None)
            if units is None:
                return  # response for a request we never recorded — drop
            if request_id != f"results:{self.scope}":
                return  # aligned, but belongs to a scope the user left — drop
            self._clear_error_if(kind)
            self._last_results = parse_show_results(stdout, units)
            self._pending_enrich.discard("results")
            self._maybe_render()
            return
        elif kind == "schedules":
            units = self._schedule_units_by_id.pop(request_id, None)
            if units is None:
                return
            if request_id != f"schedules:{self.scope}":
                return
            self._clear_error_if(kind)
            self._last_schedules = parse_show_schedules(stdout, units)
            self._pending_enrich.discard("schedules")
            self._maybe_render()
            return
        elif kind == "action":
            # The payload IS stderr for action ids (see the client): systemd
            # explains no-ops there even at exit 0 — e.g. `enable` on an
            # [Install]-less unit. Show its words, persistently; a bare "ok"
            # would narrate nothing-happened as success.
            unit = request_id.partition(":")[2]
            self._error_kind = "action"
            if stdout.strip():
                self.statusBar().showMessage(
                    f"{unit}: {datetime.now():%H:%M:%S} {stdout.strip()}", 0
                )
            else:
                self.statusBar().showMessage(f"{unit}: command accepted", 0)
            # The user just acted on this unit — fresh tabs are exactly what
            # they want; clearing the dedup makes the post-refresh selection
            # restore refetch them (QA synthesis #4).
            self._last_detail_unit = None
            self.refresh()
            return
        elif kind in ("log", "details", "cat", "calendar", "schedtab"):
            if request_id not in self._expected_tab_ids:
                return  # stale response for a unit/scope no longer selected
            self._fill_tab(kind, stdout)
            return
        elif kind == "calcover":
            self._on_cal_coverage(request_id, stdout)
            return
        elif kind == "caljournal":
            self._on_cal_journal(request_id, stdout)
            return
        elif kind == "calproj":
            self._on_cal_projection(request_id, stdout)
            return
        else:
            return  # unknown kind (future request types): never fall through
        if not self._pending:
            # Both lists landed → batched enrichment: one `show` for every
            # relevant service's last result, one for every timer's triggers.
            # The render waits for BOTH so the columns appear together.
            result_units = sorted(
                {t.activates for t in self._timers} | {s.unit for s in self._services}
            )
            timer_units = sorted(t.unit for t in self._timers)
            self._pending_enrich = set()
            # Empty-list guards are load-bearing: `show` with NO units dumps
            # manager properties, which the parsers would reject downstream.
            if result_units:
                self._pending_enrich.add("results")
                if self.client.fetch_results(self.scope, result_units):
                    self._result_units_by_id[f"results:{self.scope}"] = result_units
                # else: a previous fetch is still in flight — its OWN entry
                # stays recorded, so its response parses against the argv
                # that produced it. Fresh lists render on the next cycle.
            else:
                self._last_results = {}
            if timer_units:
                self._pending_enrich.add("schedules")
                if self.client.fetch_schedules(self.scope, timer_units):
                    self._schedule_units_by_id[f"schedules:{self.scope}"] = timer_units
            else:
                self._last_schedules = {}
            self._maybe_render()

    def _maybe_render(self) -> None:
        """Render once all enrichment for the current cycle has landed."""
        if not self._pending_enrich:
            self._data_scope = self.scope
            self._render_rows()

    # -- calendar build --------------------------------------------------------

    def _build_calendar(self, win_start: int, win_end: int) -> None:
        """Fan out the fetches for a calendar window [win_start, win_end] (µs).

        Two-stage fan-out (F1 gating):
        1. Fire the coverage probe + the run-outcome journal query NOW, and
           compute (but do NOT yet fire) the projection PLAN — one entry per
           eligible timer×OnCalendar-expression (F4 projects every expression).
        2. When the coverage probe lands (_on_cal_coverage), the journal-coverage
           floor is known, so the planned projections fire then. Gating the
           projections on coverage means every projected slot is judged against a
           floor that's already settled, never a window edge that would bloom
           false gaps on a short-retention journal (the F1 P0).

        All responses fan IN via _dispatch_finished's calcover/calproj/caljournal
        branches; _finalize_calendar renders when _cal_pending empties — a
        separate barrier from the table's render barrier so the two pages never
        block each other.

        Each fan-in id is STAMPED with this build's generation (F3): the same
        timers exist across rapid rebuilds, so an unstamped id would repeat and
        the client's single-flight registry would let a superseded build's
        response satisfy this build's barrier. The generation makes every id
        unique, so a stale response is dropped by the not-in-_cal_pending guard.

        Timer eligibility (spec §9):
        - DISABLED timer (next is None): contributes past runs + gaps but NO
          projection — a disabled timer has no future schedule to project.
        - NEVER-RAN timer (last 0/None): still projected; gaps need a run
          baseline they lack, so they simply produce none.
        - EMPTY `activates`: shown as `—`, skipped for journal AND projection —
          there is no service to mine runs for and projecting it alone is noise.
        - Monotonic-only / unclassifiable cadence: cadence_interval_usec returns
          None → projection_iterations returns 0 → no projection fetch (its
          single next run would come from list-timers as an `approx` event, a
          later task; v1's Day slice omits it).

        `win_start/win_end` come either from _on_view_changed (first show) or the
        CalendarView's rebuild signal (a nav move). The journal is clamped to
        [win_start, now] — never query the future, where no run can have happened.
        """
        scope = self.scope
        now_usec = int(datetime.now(UTC).timestamp() * 1_000_000)
        # Bump the generation FIRST so every id stamped below belongs to THIS
        # build (F3). A response stamped with a prior generation is no longer in
        # _cal_pending after the next bump, so the dispatch guard drops it.
        self._cal_gen += 1
        gen = self._cal_gen
        # Reset the per-build accumulators. Replacing _cal_pending wholesale (plus
        # the unique generation stamp) is what drops a late response from a
        # superseded window — its id is no longer in the set.
        self._cal_pending = set()
        self._cal_events = []
        self._cal_slots = {}
        self._cal_proj_unit_by_id = {}
        self._cal_proj_plan = []
        self._cal_fwd_plan = []
        # None = floor not yet established (R2-3). A SUCCESSFUL coverage probe
        # fills it in _on_cal_coverage; if the probe fails, it stays None and
        # _finalize_calendar suppresses gaps rather than judging against epoch.
        self._cal_coverage_start = None
        self._cal_degraded = False  # set True if any fetch fails this build (R2-3)
        self._cal_window = (win_start, win_end)
        self._cal_now = now_usec
        # Row order + the service→timer map are derived from the live timers.
        # Timers with an empty `activates` are listed (so the row shows) but
        # excluded from the service→timer map (no runs to bucket to them).
        self._cal_units = [t.unit for t in self._timers]
        self._cal_service_to_timer = {
            t.activates: t.unit for t in self._timers if t.activates
        }

        # Compute the projection plan (F4: every OnCalendar expr of every
        # eligible timer). NOT fired here — _on_cal_coverage fires it once the
        # coverage floor is known (F1 gating).
        span = max(1, win_end - win_start)
        for timer in self._timers:
            if timer.next_usec is None:
                continue  # disabled — past runs/gaps only, never a projection
            if not timer.activates:
                continue  # no service to mine — listed but not projected (§9)
            info = self._last_schedules.get(timer.unit)
            interval = cadence_interval_usec(info)
            iterations = projection_iterations(interval, span)
            if iterations <= 0:
                continue  # monotonic-only / unclassifiable → no calendar series
            # iterations > 0 implies cadence_interval_usec saw a real interval,
            # which it derives ONLY from a non-empty info.calendar — so info is
            # non-None and has at least one expression here. The guard is a
            # belt-and-suspenders narrowing (also satisfies mypy) rather than an
            # `assert` (which -O would strip): if a future refactor ever broke
            # that invariant, skip the timer instead of crashing the whole build.
            if info is None or not info.calendar:
                continue
            # F4: plan a projection for EACH OnCalendar expression, not just
            # calendar[0]. A multi-trigger timer's secondary-trigger miss is only
            # a slot (and thus only a gap) if that expression is projected; the
            # old calendar[0]-only path made secondary misses invisible. Each
            # expression gets the SAME iteration count (sized from the smallest
            # cadence, which never under-counts) and projects from win_start so
            # the past slots compute_gaps needs are produced.
            for expr_idx, expr in enumerate(info.calendar):
                self._cal_proj_plan.append((timer.unit, expr_idx, expr, iterations))
                # ALSO plan a NOW-based forward projection for the same expression
                # (B). The win_start projection above is capped and can spend its
                # whole budget on past slots (a minutely timer always; an hourly
                # one at Month scale), leaving NO future slot → the timer's
                # upcoming ⏲ never shows. The forward projection, based at now with
                # the small FWD_PROJECTION_ITERATIONS budget, guarantees a few
                # upcoming slots whatever the cap did. Same eligibility (it shares
                # this loop), so a disabled / serviceless / monotonic timer gets
                # neither projection. The fixed iteration count is the constant, so
                # only (unit, expr_idx, expr) is stored.
                self._cal_fwd_plan.append((timer.unit, expr_idx, expr))

        # Fire the coverage probe + the journal query NOW (both stamped with the
        # generation). Recorded in the fan-in set BEFORE firing so a synchronous
        # test-client response finds its id already expected.
        tag = f"@{gen}"
        cal_cover_id = f"calcover:{scope}{tag}"
        self._cal_pending.add(cal_cover_id)
        # Return ignored throughout: a single-flight rejection means an identical
        # (same-id) query is already in flight; its response still satisfies the
        # fan-in, so re-firing is unnecessary (Power of Ten r7).
        _ = self.client.fetch_cal_coverage(scope, win_start, tag)

        journal_until = min(now_usec, win_end)
        cal_journal_id = f"caljournal:{scope}{tag}"
        self._cal_pending.add(cal_journal_id)
        _ = self.client.fetch_cal_journal(scope, win_start, journal_until, tag)

        # The projection plan stays UNFIRED until coverage lands. _cal_pending is
        # non-empty here (the two queries above), so no early finalize is needed.

    def _on_cal_coverage(self, request_id: str, stdout: str) -> None:
        """Fan-in handler for the journal-coverage probe (F1).

        Parses the FIRST JSON line's __REALTIME_TIMESTAMP — the oldest journal
        entry in the window — to set the coverage floor, THEN fires the planned
        projections (which were held until this floor was known). A response not
        in _cal_pending is a stale-build leftover and dropped.

        Coverage floor rule:
        - A usable oldest timestamp → coverage_start = max(win_start, oldest):
          gaps before the journal's reach are "no data", never misses.
        - NO usable record (empty/blank/unparseable) → coverage_start = now,
          which suppresses ALL gaps. An empty result cannot distinguish "nothing
          ran" from "everything rotated out", so we never invent a miss we can't
          prove — the safe direction.
        """
        if request_id not in self._cal_pending:
            return  # stale/unexpected — not in this build's fan-in
        self._cal_pending.discard(request_id)
        win_start, _win_end = self._cal_window
        oldest = self._first_journal_timestamp(stdout)
        if oldest is None:
            # Can't prove any miss — suppress all gaps by clamping coverage to now.
            self._cal_coverage_start = self._cal_now
        else:
            self._cal_coverage_start = max(win_start, oldest)
        # Coverage floor is settled → fire the planned projections (F1 gating).
        # Each id is stamped #exprIdx@gen so it is unique to this build (F3); the
        # unit is recorded per id for the response branch to recover (":" is legal
        # in unit names, so it is never split out of the id).
        scope = self.scope
        gen = self._cal_gen
        for unit, expr_idx, expr, iterations in self._cal_proj_plan:
            proj_tag = f"#{expr_idx}@{gen}"
            proj_id = f"calproj:{scope}:{unit}{proj_tag}"
            self._cal_pending.add(proj_id)
            self._cal_proj_unit_by_id[proj_id] = unit
            # base_epoch is win_start so projecting from the PAST yields the exact
            # scheduled slots compute_gaps needs (a missed slot is a scheduled
            # instant with no run nearby), per spec §4.3. compute_gaps' own
            # coverage_start clamp (set above) does the pre-coverage suppression.
            _ = self.client.fetch_cal_projection(
                scope, unit, expr, win_start, iterations, proj_tag
            )
        # Fire the NOW-based forward projections (B). They share the calproj kind
        # and fan-in path: _on_cal_projection unions their slots into _cal_slots,
        # and since every forward slot is strictly > now, _finalize_calendar emits
        # them as `projected` (the s > now branch) — they never reach the gap walk
        # (which judges only [coverage_start, now]). The base_epoch is `now` and
        # the iteration count is the small fixed FWD_PROJECTION_ITERATIONS, so a
        # fast cadence still yields a few upcoming slots. The tag is "#f{idx}@gen"
        # — the leading 'f' keeps it DISTINCT from the win_start tag "#{idx}@gen"
        # so the same unit×expr's two projections never collide in _cal_pending or
        # _cal_proj_unit_by_id. base_epoch (now) goes through cal_projection_argv →
        # _at_epoch (µs→seconds floor), so no raw µs reaches the --base-time arg.
        now = self._cal_now
        for unit, expr_idx, expr in self._cal_fwd_plan:
            fwd_tag = f"#f{expr_idx}@{gen}"
            fwd_id = f"calproj:{scope}:{unit}{fwd_tag}"
            self._cal_pending.add(fwd_id)
            self._cal_proj_unit_by_id[fwd_id] = unit
            _ = self.client.fetch_cal_projection(
                scope, unit, expr, now, FWD_PROJECTION_ITERATIONS, fwd_tag
            )
        # If the coverage probe was the last outstanding fetch AND there are no
        # projections to fire (e.g. a window with no eligible timers, the journal
        # already landed), finalize now so the page shows an empty (not stale)
        # calendar instead of hanging.
        if not self._cal_pending:
            self._finalize_calendar()

    @staticmethod
    def _first_journal_timestamp(stdout: str) -> int | None:
        """Read __REALTIME_TIMESTAMP (µs epoch) off the FIRST usable JSON line.

        The coverage probe streams chronologically, so the first line is the
        oldest entry — exactly the coverage floor (see fetch_cal_coverage). We
        stop at the first line that parses to a usable timestamp; blank lines and
        a leading non-JSON banner are skipped, and an entirely empty/unparseable
        stream returns None (the caller reads that as "no coverage"). Bounded by
        a small banner count, not journal size — we return on the first hit.
        """
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # banner / non-JSON line — keep scanning
            ts = obj.get("__REALTIME_TIMESTAMP")
            try:
                return int(ts)
            except (TypeError, ValueError):
                continue  # field absent or non-numeric on this record
        return None

    def _finalize_calendar(self) -> None:
        """Compute gaps, emit projected events, and push everything to the view.

        Called once the build's fan-in (_cal_pending) empties. BOTH the
        `projected` future events AND the gaps are emitted HERE (not per
        response) so they share ONE deduped slot source per unit —
        sorted(set(self._cal_slots[unit])) — the R2-1 fix. Per-fetch projected
        emission (the old _on_cal_projection path) drew a slot twice when two
        OnCalendar expressions projected it, and disagreed with the gap walk on
        the now-instant slot. Deriving both from the same deduped set gives each
        instant exactly one owner: projection owns s > now, the gap walk owns
        s <= now (compute_gaps' closed [coverage_start, now]).

        Gaps need BOTH a timer's scheduled slots (its projection) AND its actual
        runs (the one journal query) — only when both have landed is a missed
        slot identifiable.

        The clamp is [coverage_start, now], where coverage_start is the
        journal-coverage floor the coverage probe established (_on_cal_coverage),
        NOT win_start. This is the F1 fix: on a short-retention journal the
        window can start before any journal data exists, and clamping to
        win_start would bloom false amber gaps across the pre-coverage region.
        Clamping to the actual journal floor keeps 'no run' before coverage
        rendering as 'no data', never a miss (spec §4.3).

        Unset-floor suppression (R2-3): if coverage_start is None the probe never
        landed (a degraded build where the coverage fetch failed), so we SKIP gap
        computation entirely — judging against a 0/epoch floor would bloom gaps
        back to the dawn of time. Past runs and projections still render; the
        degraded flag (passed to set_events) tells the user the render is partial.
        """
        win_start, win_end = self._cal_window
        now = self._cal_now
        events = list(self._cal_events)
        # Emit `projected` events from the SAME deduped slot source the gap walk
        # reads (R2-1). Per unit: dedup with sorted(set(...)) — collapsing the F4
        # cross-expression union and any DST-duplicate instants — then draw only
        # the slots strictly after now (s > now). A slot at/before now is a past
        # run's scheduled time (its outcome is a 'ran' event) or a missed slot (a
        # gap, below), so drawing it as `projected` too would double up. This is
        # the (now, ∞) half of the boundary partition compute_gaps owns the other
        # side of.
        for unit, slots in self._cal_slots.items():
            for s in sorted(set(slots)):
                if s > now:
                    events.append(
                        CalendarEvent(unit=unit, when=s, kind="projected")
                    )
        # Runs are already in _cal_events (kind="ran"); split them out per timer
        # to pair against that timer's projected slots for gap detection.
        runs_by_unit: dict[str, list[CalendarEvent]] = {}
        for ev in events:
            if ev.kind == "ran":
                runs_by_unit.setdefault(ev.unit, []).append(ev)
        # Gaps only when the coverage floor is known. None ⇒ the probe failed
        # (a degraded build) ⇒ suppress all gaps (R2-3) rather than judge against
        # epoch; past runs + projections (appended above) still render.
        coverage_start = self._cal_coverage_start
        if coverage_start is not None:
            for unit, slots in self._cal_slots.items():
                gaps = compute_gaps(
                    slots,
                    runs_by_unit.get(unit, []),
                    unit,
                    coverage_start=coverage_start,
                    now=now,
                    tolerance_usec=GAP_TOLERANCE_USEC,
                )
                events.extend(gaps)
        self.calendar_view.set_events(
            events, self._cal_units, win_start, win_end, now,
            degraded=self._cal_degraded,
        )

    def _on_cal_journal(self, request_id: str, stdout: str) -> None:
        """Fan-in handler for the single journal run-outcome query.

        Parses the JSON-lines into 'ran' events (bucketed back to their timers
        by the service→timer map this build captured), accumulates them, and
        finalizes if this was the last outstanding fetch. A response whose id is
        not in _cal_pending is dropped — that set is reset per build, so a slow
        read landing after the user navigated to a new window is ignored.
        """
        if request_id not in self._cal_pending:
            return  # not part of the current build's fan-in (stale or unexpected)
        # Parse BEFORE releasing the id: if parse_run_journal ever raises (a future
        # parser — today it skips bad lines), the id must still be in _cal_pending
        # so the _on_finished except branch can finalize-partial and mark the build
        # degraded. Discarding first would leave that branch's `in _cal_pending`
        # guard False, silently skipping the degrade + partial render (a dead
        # defense caught in Round-2 stabilization). On success the order is moot.
        self._cal_events.extend(
            parse_run_journal(stdout, self._cal_service_to_timer)
        )
        self._cal_pending.discard(request_id)
        if not self._cal_pending:
            self._finalize_calendar()

    def _on_cal_projection(
        self, request_id: str, stdout: str, generation: int | None = None
    ) -> None:
        """Fan-in handler for one timer×expression future-slot projection.

        The parsed instants are ACCUMULATED into _cal_slots only — this handler
        no longer emits any `projected` events. ALL of them (past ones, from the
        win_start base-time, and future ones) are the scheduled slots gap
        detection needs; the future ones become `projected` events later, in
        _finalize_calendar, from the SAME deduped slot source the gap path reads
        (R2-1). Emitting them here per-fetch was the root of two bugs: the
        per-expression union meant a slot projected by two OnCalendar
        expressions was drawn TWICE (a duplicate glyph and a double `upcoming`
        count), and the per-fetch `s > now` split disagreed with the gap walk's
        boundary on the now-instant slot. Deferring emission to finalize — over
        sorted(set(_cal_slots[unit])) — collapses both: one deduped source, one
        boundary, one owner per instant.

        F4 (multi-trigger): a timer fires one projection per OnCalendar
        expression, so this EXTENDS the unit's slot list rather than overwriting
        it — a second trigger's missed slot must be able to become a gap, which
        it can't if the first trigger's response clobbered it. The duplicate µs
        instants this union can create are folded by sorted(set(...)) at finalize
        (for both the gap walk and the projected-event emission).

        F3 (generation): the primary staleness guard is `request_id not in
        _cal_pending` — a superseded build's stamped id is no longer pending, so
        a late echo is dropped. `generation` is an optional secondary guard: when
        supplied and not matching the current build, drop regardless. It is
        belt-and-suspenders against any future caller that delivers an id which
        coincidentally re-enters the pending set; the stamped id is the real fix.
        """
        if generation is not None and generation != self._cal_gen:
            return  # explicitly stamped as a prior build's response — drop
        if request_id not in self._cal_pending:
            return  # stale/unexpected — not in this build's fan-in
        # Read (via get, not pop) and leave the id pending until the parse
        # succeeds: if parse_projection ever raises (a future parser — today it
        # skips bad lines), the id must still be in _cal_pending so the
        # _on_finished except branch can finalize-partial and mark degraded.
        # Removing the id/mapping first would make that branch unreachable (a dead
        # defense caught in Round-2 stabilization). On success the order is moot.
        unit = self._cal_proj_unit_by_id.get(request_id)
        if unit is not None:
            slots = parse_projection(stdout)
            # Full slot list (past + future) drives gap detection AND projected-
            # event emission in finalize. setdefault + extend UNIONS this
            # expression's slots with any already accumulated for the unit from a
            # sibling OnCalendar expression (F4). No `projected` events are built
            # here — finalize owns that, from the deduped union (R2-1).
            self._cal_slots.setdefault(unit, []).extend(slots)
        self._cal_proj_unit_by_id.pop(request_id, None)
        self._cal_pending.discard(request_id)
        if not self._cal_pending:
            self._finalize_calendar()

    def _on_calendar_selected(self, unit: str) -> None:
        """Adapter: a calendar-row click loads the same detail tabs the table
        selection uses, for `unit` (always a timer on the calendar).

        Reuses the existing detail-fetch flow verbatim (log/details/cat plus the
        timer's schedtab) so the calendar and table share one detail pane — the
        whole point of the QStackedWidget design (a click anywhere shows the same
        per-unit detail). _expected_tab_ids is reset to exactly these ids so a
        response for a unit the user has since clicked away from is dropped, the
        same freshness contract the table selection enforces.

        Gated on _auto_refresh like _on_selection: with it off (tests) this
        fires zero subprocesses. _last_detail_unit is set so a repeat click on
        the same unit doesn't re-flash "loading…".
        """
        if not self._auto_refresh:
            return
        if unit == self._last_detail_unit:
            return  # already loaded — don't re-flash loading… / refetch
        self._last_detail_unit = unit
        self._expected_tab_ids = {
            f"log:{self.scope}:{unit}",
            f"details:{self.scope}:{unit}",
            f"cat:{self.scope}:{unit}",
            f"schedtab:{self.scope}:{unit}",
        }
        for view in self._kind_to_tab.values():
            view.setPlainText("loading…")
        ok_log = self.client.fetch_log(self.scope, unit)
        ok_details = self.client.fetch_details(self.scope, unit)
        ok_cat = self.client.fetch_cat(self.scope, unit)
        ok_sched = self.client.fetch_tab_schedule(self.scope, unit)
        if not (ok_log and ok_details and ok_cat and ok_sched):
            # Same RACE-B guard as _on_selection: a single-flight rejection means
            # a stale in-flight fetch for this id could fill the tab as fresh;
            # leaving the dedup unset makes the next interaction retry.
            self._last_detail_unit = None

    def _render_rows(self) -> None:
        """Render cached data into the model; restore selection and scroll.

        Split from _apply_results so the show-inactive toggle can re-render
        locally without a subprocess round-trip.
        """
        now = datetime.now()
        # Model resets invalidate every index and clear the selection — left
        # alone, the 10s refresh would silently deselect the user's row and
        # blank the action buttons. Capture and restore by unit name. The
        # viewport scroll position dies in the reset too (the view snaps to
        # the top every cycle in long lists) — capture/restore around it.
        selected = self._selected()
        keep_unit = selected[0] if selected else None
        scroll = self.table.verticalScrollBar().value()
        if self.view_box.currentIndex() == 0:  # 0 = Timers (index survives renames)
            self.model.set_timer_rows(
                self._timers, self._services, self._last_results, self._last_schedules, now
            )
        else:
            services = self._services
            if not self.act_show_inactive.isChecked():
                services = [s for s in services if s.active != "inactive"]
            self.model.set_service_rows(services, self._last_results, now)
        self.table.verticalScrollBar().setValue(scroll)
        if keep_unit is not None and not self._reselect(keep_unit):
            # The selected unit VANISHED from the new data. A model reset
            # clears the selection without any selectionChanged signal
            # (QItemSelectionModel::reset is signal-free — QA Phase B), so
            # invalidation must happen HERE; _on_selection never fires.
            self._last_detail_unit = None
            self._expected_tab_ids = set()
            for view in self._kind_to_tab.values():
                view.setPlainText("(unit no longer listed)")
        self._freshness.setText(
            f"{self.model.rowCount()} units · {self.scope} scope · refreshed {now:%H:%M:%S}"
        )
        # The same signal-free reset means enablement is NOT re-evaluated
        # when selection restoration fails — do it explicitly every render.
        self._update_action_enablement()

    def _reselect(self, unit: str) -> bool:
        """Re-select the row matching `unit` after a model reset.

        Walks the PROXY (visible order) so filtering/sorting are respected.
        Returns False when the unit is no longer present, so the caller can
        invalidate tab state (no signal fires for that case). A successful
        restore re-fires _on_selection, where _last_detail_unit dedups the
        tab refetch.
        """
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if idx.data(ROLE_UNIT) == unit:
                self.table.selectionModel().select(
                    idx,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                return True
        return False

    def _on_failed(self, request_id: str, message: str) -> None:
        # systemd's own words, verbatim — never an empty table pretending
        # success. NOTE: a failed LIST fetch (timers/services) leaves _pending
        # non-empty, so no render happens this cycle (last-good table + visible
        # error); the next 10s refresh rebuilds _pending wholesale — self-heals.
        kind = request_id.split(":", 1)[0]
        self._post_error(kind, f"[{request_id}]: {message}")
        # A failed enrichment fetch must not leave its unit list behind — the
        # id frees in the client, and a future response must not match it.
        self._result_units_by_id.pop(request_id, None)
        self._schedule_units_by_id.pop(request_id, None)
        if kind in ("results", "schedules"):
            # Same freeze-prevention as the parse-failure path (FREEZE-1):
            # release the render barrier so a DETERMINISTIC enrichment fetch
            # failure (a systemd version that rejects the -p props, say) renders
            # the cycle with stale data instead of freezing both views forever.
            # Unlike a list fetch, the lists DID land — only the barrier is
            # stuck — so without this the table never updates again. The error
            # stays loud; ⟳ and the next clean cycle self-heal.
            self._pending_enrich.discard(kind)
            self._maybe_render()
            return
        if kind in ("calcover", "caljournal", "calproj") and request_id in self._cal_pending:
            # Release the calendar fan-in barrier on a failed build fetch so the
            # page renders with whatever DID land (partial layer) instead of
            # hanging at a blank calendar forever — the same freeze-prevention as
            # the table's enrichment path. calcover is included (F2): a failed
            # coverage probe must not wedge the build — _on_cal_coverage is what
            # fires the projections, so without releasing here a coverage failure
            # would leave the journal query alone in the barrier and the planned
            # projections never fired; finalize-partial renders the past runs that
            # DID land. The error stays loud on the status bar; the next nav/tick
            # rebuilds. The proj-unit map entry frees too, so a late success echo
            # for the same id can't match. The failed fetch makes the render
            # partial, so mark it degraded (R2-3): the HEALTH strip surfaces it in
            # the calendar's own surface, not just the ephemeral status bar. A
            # failed coverage probe additionally leaves _cal_coverage_start None,
            # so _finalize_calendar also suppresses gaps (can't judge a floor it
            # never learned).
            self._cal_degraded = True
            self._cal_proj_unit_by_id.pop(request_id, None)
            self._cal_pending.discard(request_id)
            if not self._cal_pending:
                self._finalize_calendar()
            return
        if kind == "calendar" and request_id in self._expected_tab_ids:
            # The chained elapse preview failed AFTER schedtab already rendered
            # triggers + cadence — replace ONLY the "(calculating…)" placeholder
            # so the good content survives (QA 2026-06-12 P2-6). The status bar
            # carries the full error regardless.
            current = self.tab_schedule.toPlainText().replace(
                "(calculating…)", f"(elapse preview failed: {message})"
            )
            self.tab_schedule.setPlainText(current)
            return
        # A detail tab frozen at "loading…" would hide the failure — write it
        # into the tab the response was meant for (freshness-gated like any
        # other tab write). Every tab covers its own failure now — including
        # the Schedule tab, whose schedtab fetch is freshness-gated like the
        # others. (The old design populated Schedule from the cat branch and
        # needed a special-case stamp here; DEF-V11-02 removed that.)
        tab = self._kind_to_tab.get(kind)
        if tab is not None and request_id in self._expected_tab_ids:
            tab.setPlainText(f"(fetch failed)\n{message}")

    # -- selection / detail tabs -----------------------------------------------

    def _selected(self) -> tuple[str, str] | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        idx = indexes[0]
        return idx.data(ROLE_UNIT), idx.data(ROLE_ACTIVATES)

    def _on_selection(self, *_args: object) -> None:
        self._update_action_enablement()
        if not self._auto_refresh:
            # Same contract as the constructor flag: ZERO subprocess side
            # effects when disabled — selection fetches included. Gated before
            # the placeholders so test screenshots don't freeze on "loading…".
            return
        selected = self._selected()
        if selected is None:
            return
        unit, _activates = selected
        if unit == self._last_detail_unit:
            return  # same unit (e.g. post-refresh restore) — tabs already loaded
        self._last_detail_unit = unit
        # Freshness contract: only these exact ids may write tabs from now on.
        self._expected_tab_ids = {
            f"log:{self.scope}:{unit}",
            f"details:{self.scope}:{unit}",
            f"cat:{self.scope}:{unit}",
        }
        for view in self._kind_to_tab.values():
            view.setPlainText("loading…")
        ok_sched = True
        if unit.endswith(".timer"):
            # The Schedule tab reads systemd's EFFECTIVE triggers (show), not
            # the unit-file text — drop-ins and exotic syntax made text
            # scraping lie (DEF-V11-02, resolved here).
            self._expected_tab_ids.add(f"schedtab:{self.scope}:{unit}")
            ok_sched = self.client.fetch_tab_schedule(self.scope, unit)
        else:
            self.tab_schedule.setPlainText("(not a timer — no schedule)")
        ok_log = self.client.fetch_log(self.scope, unit)
        ok_details = self.client.fetch_details(self.scope, unit)
        ok_cat = self.client.fetch_cat(self.scope, unit)
        if not (ok_log and ok_details and ok_cat and ok_sched):
            # RACE B (QA Phase B): a PRE-action fetch for this same unit may
            # still be in flight; its id matches the freshly rebuilt
            # freshness set, so its STALE payload will fill the tab as if
            # fresh. Leaving the dedup unset makes the next cycle's reselect
            # retry until all three fetches are actually accepted — bounded
            # by the watchdog capping in-flight life.
            self._last_detail_unit = None

    def _fill_tab(self, kind: str, stdout: str) -> None:
        if kind == "log":
            entries = parse_journal(stdout)
            lines = []
            for e in entries:
                stamp = "—"
                if not ts_missing(e.ts_usec) and e.ts_usec is not None:
                    try:
                        stamp = datetime.fromtimestamp(e.ts_usec / 1_000_000).strftime(
                            "%b %d %H:%M:%S"
                        )
                    except (OverflowError, OSError, ValueError):
                        # Corrupt-but-numeric epochs (this machine has journal
                        # corruption) must render as missing — not crash the
                        # whole tab, not clamp to a plausible-looking lie.
                        stamp = "—"
                marker = "✘ " if e.priority <= 3 else ""
                # ANSI stripped at render only — the parser stays a faithful
                # transcription (see models.strip_ansi).
                lines.append(f"{stamp} {marker}{e.identifier}: {strip_ansi(e.message)}")
            self.tab_log.setPlainText("\n".join(lines) or "(no journal entries)")
            self.tab_log.verticalScrollBar().setValue(
                self.tab_log.verticalScrollBar().maximum()
            )
        elif kind == "details":
            self.tab_details.setPlainText(stdout.strip() or "(no properties)")
        elif kind == "cat":
            # Unit file only — the Schedule tab has its own show-based fetch
            # (schedtab) so it reflects EFFECTIVE triggers, drop-ins included.
            self.tab_unitfile.setPlainText(stdout.strip() or "(unit file not found)")
        elif kind == "schedtab":
            # Single-unit fetch; the freshness gate guarantees this response
            # belongs to the current selection, so the placeholder unit name
            # used for block alignment never surfaces anywhere.
            info = parse_show_schedules(stdout, ["selected"]).get("selected")
            if info is None or (not info.calendar and not info.monotonic):
                self.tab_schedule.setPlainText("(no triggers configured)")
                return
            lines = ["Triggers (effective, drop-ins included):"]
            lines += [f"  OnCalendar={expr}" for expr in info.calendar]
            lines += [f"  {spec}" for spec in info.monotonic]
            lines.append(f"\nCadence: {classify_cadence(info)}")
            if info.next_elapse:
                lines.append(f"Next elapse: {info.next_elapse}")
            text = "\n".join(lines)
            if info.calendar:
                # systemd-analyze appends the next five elapses for the first
                # calendar trigger. Admit the response into the freshness set
                # BEFORE requesting; a later selection replaces the whole set,
                # so it can never append under another unit's schedule.
                expr = info.calendar[0]
                text += "\n\n(calculating…)"
                self._expected_tab_ids.add(f"calendar:{expr}")
                # Return deliberately ignored: a single-flight rejection means a
                # previous selection already chained this SAME expression, whose
                # calendar:{expr} id was just re-admitted above and whose elapse
                # output is identical — so "(calculating…)" still resolves from
                # that in-flight response. Nothing to retry. (Power-of-Ten r7.)
                _ = self.client.fetch_calendar(expr)
            self.tab_schedule.setPlainText(text)
        elif kind == "calendar":
            current = self.tab_schedule.toPlainText().replace("(calculating…)", "").rstrip()
            self.tab_schedule.setPlainText(current + "\n\n" + stdout.strip())

    # -- actions ---------------------------------------------------------------

    def _update_action_enablement(self) -> None:
        # _data_scope must match: in the sub-second window after a scope
        # flip, the OLD scope's rows are still rendered and selectable —
        # without this gate an action could target a same-named unit in the
        # wrong scope (QA synthesis #7).
        allowed = (
            self.scope == SCOPE_USER
            and self._data_scope == self.scope
            and self._selected() is not None
        )
        tooltip = "" if self.scope == SCOPE_USER else "system units are read-only by design"
        for act in self.action_buttons:
            act.setEnabled(allowed)
            act.setToolTip(tooltip)

    def _do_action(self, verb: str, run_now: bool = False) -> None:
        selected = self._selected()
        if selected is None:
            return
        unit, activates = selected
        # Run-now targets the ACTIVATED service (ROLE_ACTIVATES), not the
        # timer: starting a .timer merely re-arms its schedule; starting the
        # service is what "run now" means to a Task Scheduler user.
        target = activates if run_now else unit
        if verb == "stop":
            # Per-view text: stopping a .timer cancels future SCHEDULING and
            # never interrupts a currently running job — the generic warning
            # would describe the wrong consequence (QA synthesis #13; the
            # retarget-to-service alternative was rejected as racy in Phase B).
            if self.view_box.currentIndex() == 0:
                text = (
                    f"Stop scheduling {target}?\n\nThis cancels future runs. A "
                    "currently running job, if any, continues to completion."
                )
            else:
                text = f"Stop {target}?\n\nStopping can interrupt a job mid-run."
            answer = QMessageBox.question(self, "Stop unit?", text)
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            argv = action_argv(verb, self.scope, target, systemctl=self.client.systemctl_path)
        except ActionNotAllowed as exc:
            # Belt-and-suspenders: buttons are disabled in system scope, but if
            # enablement ever regresses, the guard still refuses — loudly.
            self.statusBar().showMessage(f"refused: {exc}", 0)
            return
        accepted = self.client.run_action(argv, target)
        if not accepted:
            # Single-flight rejection: an action on this unit is already in
            # flight. Surfacing it beats silently dropping the click.
            self.statusBar().showMessage(f"{target}: previous action still running", 5000)
            return
        self.statusBar().showMessage(f"{verb} {target}…", 0)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        # With a tray present, the X button HIDES to the tray and keeps the
        # background monitor watching — Quit (tray menu) is the only real exit,
        # and it sets _quitting first. The refresh timer keeps running while
        # hidden so the table is current the instant the window is re-shown;
        # the cost is a few `systemctl` reads per 10s, which is cheap and
        # bounded (see DEF-TR-01 if that ever needs binding to visibility).
        if self._hide_to_tray and not self._quitting:
            event.ignore()
            self.hide()
            return
        self._timer.stop()
        # This is the last request()-issuing path; free any QProcess parked by a
        # fetch/action that finished after the last refresh, which would
        # otherwise linger until GC (no further request() would sweep it).
        self.client.flush_finished()
        super().closeEvent(event)
