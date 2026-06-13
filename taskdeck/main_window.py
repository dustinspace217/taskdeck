"""MainWindow: DSM-style layout A — toolbar, full-width table, tabbed detail pane.

This is the only module that constructs widgets. All systemd I/O goes through
the injected SystemdClient (constructor arg, so tests inject one and never
spawn subprocesses); all action policy lives in actions.py.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QItemSelectionModel, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QTabWidget,
)

from taskdeck.actions import ActionNotAllowed, action_argv
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
        self.view_box.addItems(["Timers", "Services"])
        self.view_box.currentIndexChanged.connect(lambda _i: self.refresh())
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
        self.setCentralWidget(splitter)
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

    def closeEvent(self, event: object) -> None:  # noqa: N802 (Qt naming)
        self._timer.stop()
        super().closeEvent(event)
