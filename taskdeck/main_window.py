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
from taskdeck.models import ROLE_ACTIVATES, ROLE_SORT, ROLE_UNIT, TaskTableModel
from taskdeck.systemd_client import (
    SCOPE_SYSTEM,
    SCOPE_USER,
    ServiceRow,
    SystemdClient,
    TimerRow,
    parse_journal,
    parse_list_timers,
    parse_list_units,
    parse_show_results,
)


class MainWindow(QMainWindow):  # type: ignore[misc]
    """Assembles the v1 UI and orchestrates refresh cycles.

    Refresh design: a cycle = list-timers + list-units for the active scope;
    when both land, one batched `show` fetches last-results for every relevant
    service. Tab content (log/details/schedule/unit file) loads lazily on row
    selection. SystemdClient's single-flight coalescing bounds concurrent
    subprocesses; responses for a scope the user has already navigated away
    from are dropped by the scope check in _on_finished.

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
        self._result_units: list[str] = []
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
        refresh.triggered.connect(self.refresh)
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
        # Same unit name in another scope is a DIFFERENT unit — force refetch.
        self._last_detail_unit = None
        self._update_action_enablement()
        if self._auto_refresh:
            self.refresh()

    def refresh(self) -> None:
        self._pending = {f"timers:{self.scope}", f"services:{self.scope}"}
        # Return values deliberately ignored: a single-flight rejection means
        # the SAME id is already in flight, which satisfies _pending equally.
        self.client.list_timers(self.scope)
        self.client.list_services(self.scope)

    def _on_finished(self, request_id: str, stdout: str) -> None:
        # ONLY the kind is parsed from the id (see class docstring).
        kind = request_id.split(":", 1)[0]
        try:
            self._dispatch_finished(kind, request_id, stdout)
        except (ValueError, KeyError) as exc:
            # Parse failures surface as error states, never an empty table
            # pretending systemd has no units (no-silent-failure rule).
            # KeyError covers parser records missing required fields; !r so
            # its terse payload still names the missing key.
            self.statusBar().showMessage(f"ERROR parsing {request_id}: {exc!r}", 0)

    def _dispatch_finished(self, kind: str, request_id: str, stdout: str) -> None:
        if kind == "timers":
            if request_id != f"timers:{self.scope}":
                return  # stale response from a scope the user left
            self._timers = parse_list_timers(stdout)
            self._pending.discard(request_id)
        elif kind == "services":
            if request_id != f"services:{self.scope}":
                return
            self._services = parse_list_units(stdout)
            self._pending.discard(request_id)
        elif kind == "results":
            if request_id == f"results:{self.scope}":
                self._apply_results(stdout)
            return
        elif kind == "action":
            self.statusBar().showMessage(f"{request_id} ok", 5000)
            self.refresh()
            return
        elif kind in ("log", "details", "cat", "calendar"):
            if request_id not in self._expected_tab_ids:
                return  # stale response for a unit/scope no longer selected
            self._fill_tab(kind, stdout)
            return
        else:
            return  # unknown kind (future request types): never fall through
        if not self._pending:
            # Both lists landed → one batched show for every relevant service.
            new_units = sorted(
                {t.activates for t in self._timers} | {s.unit for s in self._services}
            )
            if not new_units:
                self._result_units = []
                self._apply_results("")
                return
            # Guard above is load-bearing: `show` with NO units dumps manager
            # properties, which the parser would reject downstream.
            if self.client.fetch_results(self.scope, new_units):
                self._result_units = new_units
            # else: a previous results fetch is still in flight — keep the OLD
            # unit list so its response parses against the argv that produced
            # it (units and blocks must stay aligned). Fresh lists render on
            # the next cycle.

    def _apply_results(self, stdout: str) -> None:
        results = parse_show_results(stdout, self._result_units)
        now = datetime.now()
        # Model resets invalidate every index and clear the selection — left
        # alone, the 10s refresh would silently deselect the user's row and
        # blank the action buttons. Capture and restore by unit name.
        selected = self._selected()
        keep_unit = selected[0] if selected else None
        if self.view_box.currentIndex() == 0:  # 0 = Timers (index survives renames)
            self.model.set_timer_rows(self._timers, self._services, results, now)
        else:
            visible = [s for s in self._services if s.active != "inactive"]
            self.model.set_service_rows(visible, results, now)
        if keep_unit is not None:
            self._reselect(keep_unit)
        self.statusBar().showMessage(
            f"{self.model.rowCount()} units · {self.scope} scope · refreshed {now:%H:%M:%S}", 0
        )

    def _reselect(self, unit: str) -> None:
        """Re-select the row matching `unit` after a model reset, if present.

        Walks the PROXY (visible order) so filtering/sorting are respected;
        a unit that vanished from the new data simply stays deselected. The
        restored selection re-fires _on_selection, where _last_detail_unit
        dedups the tab refetch.
        """
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if idx.data(ROLE_UNIT) == unit:
                self.table.selectionModel().select(
                    idx,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                return

    def _on_failed(self, request_id: str, message: str) -> None:
        # systemd's own words, verbatim — never an empty table pretending success.
        self.statusBar().showMessage(f"ERROR [{request_id}]: {message}", 0)
        # A detail tab frozen at "loading…" would hide the failure — write it
        # into the tab the response was meant for (freshness-gated like any
        # other tab write).
        kind = request_id.split(":", 1)[0]
        tab = {
            "log": self.tab_log,
            "details": self.tab_details,
            "cat": self.tab_unitfile,
            "calendar": self.tab_schedule,
        }.get(kind)
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
        for view in (self.tab_log, self.tab_details, self.tab_schedule, self.tab_unitfile):
            view.setPlainText("loading…")
        self.client.fetch_log(self.scope, unit)
        self.client.fetch_details(self.scope, unit)
        self.client.fetch_cat(self.scope, unit)

    def _fill_tab(self, kind: str, stdout: str) -> None:
        if kind == "log":
            entries = parse_journal(stdout)
            lines = []
            for e in entries:
                stamp = (
                    datetime.fromtimestamp(e.ts_usec / 1_000_000).strftime("%b %d %H:%M:%S")
                    if e.ts_usec
                    else "—"
                )
                marker = "✘ " if e.priority <= 3 else ""
                lines.append(f"{stamp} {marker}{e.identifier}: {e.message}")
            self.tab_log.setPlainText("\n".join(lines) or "(no journal entries)")
            self.tab_log.verticalScrollBar().setValue(
                self.tab_log.verticalScrollBar().maximum()
            )
        elif kind == "details":
            self.tab_details.setPlainText(stdout.strip() or "(no properties)")
        elif kind == "cat":
            self.tab_unitfile.setPlainText(stdout.strip() or "(unit file not found)")
            # Schedule tab: pull OnCalendar= lines out of the unit file and ask
            # systemd-analyze for the next elapses of the first one.
            cal_lines = [
                line.strip()
                for line in stdout.splitlines()
                if line.strip().startswith("OnCalendar=")
            ]
            if cal_lines:
                expr = cal_lines[0].partition("=")[2]
                self.tab_schedule.setPlainText("\n".join(cal_lines) + "\n\n(calculating…)")
                # Admit the calendar response into the freshness set BEFORE
                # requesting it; a later selection replaces the whole set, so
                # this response can never append under another unit's schedule.
                self._expected_tab_ids.add(f"calendar:{expr}")
                self.client.fetch_calendar(expr)
            else:
                self.tab_schedule.setPlainText(
                    "(no OnCalendar= schedule — boot/login-triggered or plain service)"
                )
        elif kind == "calendar":
            current = self.tab_schedule.toPlainText().replace("(calculating…)", "").rstrip()
            self.tab_schedule.setPlainText(current + "\n\n" + stdout.strip())

    # -- actions ---------------------------------------------------------------

    def _update_action_enablement(self) -> None:
        allowed = self.scope == SCOPE_USER and self._selected() is not None
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
            answer = QMessageBox.question(
                self,
                "Stop unit?",
                f"Stop {target}?\n\nStopping can interrupt a job mid-run.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            argv = action_argv(verb, self.scope, target)
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
