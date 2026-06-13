"""Window-logic tests with a fake client — no subprocesses, pure routing.

MainWindow takes the client by injection precisely so these tests can drive
_on_finished/_on_failed directly with canned payloads and assert the
orchestration. Construction uses auto_refresh=False (no timer, no constructor
refresh); tests that need the selection-fetch path flip the PRIVATE
_auto_refresh flag afterwards — the timer was never started and the
constructor refresh never fired, so this enables selection fetches with zero
live-timer flakiness and zero production-code churn (QA Phase C test idiom).
"""
from PySide6.QtCore import QObject, Signal

from taskdeck.main_window import MainWindow

LIST_TIMERS_JSON = (
    '[{"next":1781136604691295,"left":1,"last":1781115051863104,"passed":1,'
    '"unit":"a.timer","activates":"a.service"}]'
)
LIST_UNITS_JSON = (
    '[{"unit":"a.service","load":"loaded","active":"active","sub":"running",'
    '"description":"A"},'
    '{"unit":"b.service","load":"loaded","active":"inactive","sub":"dead",'
    '"description":"B"}]'
)
# A ran-unit results payload for a.service/b.service cycles.
RESULTS_TEXT = (
    "Result=success\nExecMainExitTimestamp=Wed\nExecMainStatus=0\n\n"
    "Result=exit-code\nExecMainExitTimestamp=Thu\nExecMainStatus=1\n"
)
# A schedules payload for the one timer (a.timer) in LIST_TIMERS_JSON —
# the wire shape `systemctl show -p TimersCalendar,…` actually emits.
SCHEDULES_TEXT = (
    "TimersCalendar={ OnCalendar=*-*-* 03:50:00 ; "
    "next_elapse=Fri 2026-06-12 03:50:00 PDT }\n"
    "NextElapseUSecRealtime=Fri 2026-06-12 03:50:00 PDT\n"
)


class FakeClient(QObject):
    """Signal-compatible stand-in for SystemdClient that records every call.

    Methods return `accept` (default True) so tests can exercise the
    single-flight-rejection handling by flipping it.
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

    def run_action(self, argv: list, unit: str) -> bool:
        return self._record("run_action", list(argv), unit)


def make_window(qtbot):
    client = FakeClient()
    window = MainWindow(client, auto_refresh=False)
    qtbot.addWidget(window)
    return window, client


def run_full_cycle(window, timers_json=LIST_TIMERS_JSON, units_json=LIST_UNITS_JSON):
    """Drive one complete refresh cycle through _on_finished.

    The render waits for BOTH enrichments (results + schedules); when the
    cycle has no timers the schedules fetch never fired, and the delivery
    below is dropped by the by-id alignment guard — harmless either way.
    """
    window.refresh()
    window._on_finished("timers:user", timers_json)
    window._on_finished("services:user", units_json)
    window._on_finished("results:user", RESULTS_TEXT)
    window._on_finished("schedules:user", SCHEDULES_TEXT)


def calls_of(client, name):
    return [c for c in client.calls if c[0] == name]


# -- refresh orchestration ----------------------------------------------------


def test_both_lists_landing_triggers_batched_results_fetch(qtbot):
    window, client = make_window(qtbot)
    window.refresh()  # manual refresh is not gated by auto_refresh
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    assert ("fetch_results", "user", ["a.service", "b.service"]) in client.calls


def test_empty_scope_renders_zero_rows_without_results_fetch(qtbot):
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", "[]")
    window._on_finished("services:user", "[]")
    assert not calls_of(client, "fetch_results")
    # The empty-timer-list guard is load-bearing: `show` with NO units dumps
    # manager properties, which _walk_show_blocks would reject (QA 2026-06-12).
    assert not calls_of(client, "fetch_schedules")
    assert window.model.rowCount() == 0
    # Freshness moved to the permanent label (QA synthesis #5).
    assert "0 units" in window._freshness.text()


def test_stale_scope_response_is_dropped(qtbot):
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:system", LIST_TIMERS_JSON)  # user scope is active
    assert window._timers == []


def test_malformed_json_surfaces_parse_error(qtbot):
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", "not json")
    message = window.statusBar().currentMessage()
    assert message.startswith("ERROR") and "parsing" in message


def test_null_field_payload_surfaces_validation_error(qtbot):
    # The frozen-table killer (QA AT-F9a): a null activates inside a VALID
    # array must surface as a loud parse error, not a TypeError escaping to
    # an unwatched stderr while the table silently freezes.
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished(
        "timers:user", '[{"unit":"x.timer","activates":null,"next":null,"last":null}]'
    )
    message = window.statusBar().currentMessage()
    assert message.startswith("ERROR") and "activates" in message


def test_failed_signal_surfaces_verbatim(qtbot):
    window, client = make_window(qtbot)
    window._on_failed("timers:user", "systemctl exit 1: boom")
    assert "boom" in window.statusBar().currentMessage()


def test_services_view_hides_inactive(qtbot):
    window, client = make_window(qtbot)
    window.view_box.setCurrentIndex(1)  # Services view
    run_full_cycle(window)
    assert window.model.rowCount() == 1  # b.service (inactive) is hidden


def test_show_inactive_toggle_rerenders_locally(qtbot):
    window, client = make_window(qtbot)
    window.view_box.setCurrentIndex(1)
    run_full_cycle(window)
    fetches_before = len(calls_of(client, "fetch_results"))
    window.act_show_inactive.setChecked(True)
    assert window.model.rowCount() == 2  # inactive b.service now visible
    # Local re-render — no new subprocess round-trip (QA Phase B break:
    # a refresh here would be single-flight-rejected and look dead).
    assert len(calls_of(client, "fetch_results")) == fetches_before


# -- error-channel semantics ---------------------------------------------------


def test_error_survives_successful_refresh(qtbot):
    # The status-channel split (QA synthesis #5): an ACTION error must not be
    # washed out by the routine freshness line of the next refresh cycle.
    window, client = make_window(qtbot)
    window._on_failed("action:a.service", "boom")
    run_full_cycle(window)
    assert "boom" in window.statusBar().currentMessage()
    assert "refreshed" in window._freshness.text()


def test_fetch_error_cleared_by_same_kind_success(qtbot):
    # Overwrite-on-recovery: the channel that failed is now demonstrably
    # working, so the stale error clears (kind-aware rule).
    window, client = make_window(qtbot)
    window.refresh()
    window._on_failed("timers:user", "transient")
    assert "transient" in window.statusBar().currentMessage()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    assert "transient" not in window.statusBar().currentMessage()


# -- results alignment ----------------------------------------------------------


def test_rejected_results_fetch_keeps_recorded_unit_list(qtbot):
    # If a previous batched `show` is still in flight (fetch_results returns
    # False), its OWN by-id entry must survive so its response parses against
    # the unit list its argv was built from.
    window, client = make_window(qtbot)
    window._result_units_by_id["results:user"] = ["old.service"]
    client.accept = False
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    assert window._result_units_by_id["results:user"] == ["old.service"]


def test_results_response_without_recorded_units_is_dropped(qtbot):
    window, client = make_window(qtbot)
    window._on_finished("results:user", RESULTS_TEXT)
    assert window.model.rowCount() == 0  # nothing rendered, nothing crashed


def test_other_scope_results_are_consumed_but_not_rendered(qtbot):
    # The response is popped (alignment bookkeeping) but never rendered under
    # the wrong scope label (QA synthesis #6).
    window, client = make_window(qtbot)
    window._result_units_by_id["results:system"] = ["ok.service", "bad.service"]
    window._on_finished("results:system", RESULTS_TEXT)
    assert "results:system" not in window._result_units_by_id
    assert window.model.rowCount() == 0


# -- schedules enrichment ---------------------------------------------------------


def test_render_waits_for_both_enrichments(qtbot):
    # Results landing FIRST must not render a table with a blank Cadence
    # column that flickers full a beat later — the render is a barrier on
    # both enrichment fetches of the cycle.
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    assert ("fetch_schedules", "user", ["a.timer"]) in client.calls
    window._on_finished("results:user", RESULTS_TEXT)
    assert window.model.rowCount() == 0  # schedules still pending — no render
    window._on_finished("schedules:user", SCHEDULES_TEXT)
    assert window.model.rowCount() == 1


def test_render_waits_for_both_enrichments_schedules_first(qtbot):
    # The MIRROR of the test above (QA 2026-06-12 PIN-1): the barrier must
    # block on the RESULTS side too. Delivering schedules first and asserting
    # no render pins `_pending_enrich.add("results")` — without it, this would
    # render early with a blank Last-result column.
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    window._on_finished("schedules:user", SCHEDULES_TEXT)
    assert window.model.rowCount() == 0  # results still pending — no render
    window._on_finished("results:user", RESULTS_TEXT)
    assert window.model.rowCount() == 1


def test_cadence_column_renders_from_schedules(qtbot):
    from PySide6.QtCore import Qt

    window, client = make_window(qtbot)
    run_full_cycle(window)
    cadence = window.model.index(0, 2).data(Qt.ItemDataRole.DisplayRole)
    assert cadence == "daily"  # *-*-* 03:50:00 from SCHEDULES_TEXT


def test_schedules_response_without_recorded_units_is_dropped(qtbot):
    # Same by-id alignment guard as results: a response we never recorded
    # (or whose entry a failure already freed) is dropped, never parsed
    # against a guessed unit list.
    window, client = make_window(qtbot)
    window._on_finished("schedules:user", SCHEDULES_TEXT)
    assert window._last_schedules == {}


def test_other_scope_schedules_are_consumed_but_not_rendered(qtbot):
    # PIN-2 (QA 2026-06-12): the schedules twin of the results scope guard.
    # A user-scope schedules response landing after a flip must NOT pollute
    # _last_schedules or stamp the wrong scope onto rendered rows — without the
    # guard, the barrier could empty and _data_scope would mislabel rows,
    # defeating the action-enablement gate (QA synthesis #7). The entry is
    # still popped (alignment bookkeeping) but nothing renders.
    window, client = make_window(qtbot)
    window._schedule_units_by_id["schedules:system"] = ["a.timer"]
    window._on_finished("schedules:system", SCHEDULES_TEXT)
    assert "schedules:system" not in window._schedule_units_by_id
    assert window._last_schedules == {}
    assert window.model.rowCount() == 0


def test_rejected_schedules_fetch_keeps_recorded_unit_list(qtbot):
    # The schedules twin of test_rejected_results_fetch_keeps_recorded_unit_list:
    # if a previous batched schedules `show` is still in flight, its by-id entry
    # must survive so its response parses against the argv it was built from.
    window, client = make_window(qtbot)
    window._schedule_units_by_id["schedules:user"] = ["old.timer"]
    client.accept = False
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    assert window._schedule_units_by_id["schedules:user"] == ["old.timer"]


def test_malformed_schedules_releases_barrier_and_renders_stale(qtbot):
    # FREEZE-1 (QA 2026-06-12): one unparseable trigger line must NOT freeze
    # the table. The render barrier releases, the cycle renders (with no/stale
    # cadence), and the error is loud. Before the fix, the parse raised between
    # the by-id pop and the barrier discard, and every 10s cycle re-raised on
    # the same poison payload — both views frozen at last-good forever.
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    window._on_finished("results:user", RESULTS_TEXT)
    window._on_finished("schedules:user", "TimersCalendar=garbage no braces\n")
    assert window.model.rowCount() == 1  # rendered despite the bad schedule
    assert window.model.index(0, 2).data() == "—"  # cadence honest-unknown
    message = window.statusBar().currentMessage()
    assert message.startswith("ERROR") and "schedules" in message


def test_malformed_schedules_recovers_next_cycle(qtbot):
    # The self-heal half of FREEZE-1: once the source emits a good payload,
    # the next cycle restores cadence and clears the error.
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    window._on_finished("results:user", RESULTS_TEXT)
    window._on_finished("schedules:user", "TimersCalendar=garbage no braces\n")
    assert "ERROR" in window.statusBar().currentMessage()
    run_full_cycle(window)  # good payload this time
    assert window.model.index(0, 2).data() == "daily"
    assert "ERROR" not in window.statusBar().currentMessage()


def test_failed_schedules_fetch_releases_barrier(qtbot):
    # The fetch-failure twin of FREEZE-1: a DETERMINISTIC schedules fetch
    # failure (a systemd version rejecting the -p props) would also freeze the
    # table forever — the lists land every cycle but the barrier never clears.
    # _on_failed releases it so the table renders with stale data + error.
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    window._on_finished("results:user", RESULTS_TEXT)
    window._on_failed("schedules:user", "Unknown property SCHEDULE_PROPS")
    assert window.model.rowCount() == 1  # not frozen
    assert "Unknown property" in window.statusBar().currentMessage()


# -- detail-tab lifecycle --------------------------------------------------------


def select_with_fetches(qtbot, window):
    """Enable the selection-fetch path and select row 0."""
    window._auto_refresh = True  # test idiom — timer was never started
    window.table.selectRow(0)


def test_selection_fetches_and_freshness_ids(qtbot):
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert ("fetch_log", "user", "a.timer") in client.calls
    # a.timer is a timer, so the Schedule tab gets its own show-based fetch.
    assert ("fetch_tab_schedule", "user", "a.timer") in client.calls
    assert window._expected_tab_ids == {
        "log:user:a.timer",
        "details:user:a.timer",
        "cat:user:a.timer",
        "schedtab:user:a.timer",
    }


def test_post_action_refetches_tabs(qtbot):
    # The 3-way convergent staleness fix (QA synthesis #4): after an action,
    # the post-refresh selection restore must REFETCH the tabs.
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert len(calls_of(client, "fetch_log")) == 1
    window._on_finished("action:a.service", "")
    run_full_cycle(window)  # the action-triggered refresh
    assert len(calls_of(client, "fetch_log")) == 2


def test_race_b_rejected_selection_fetch_leaves_dedup_unset(qtbot):
    # RACE B (QA Phase B): if any selection fetch is single-flight-rejected,
    # a stale pre-action response could fill the tab as fresh. The dedup must
    # stay unset so the next cycle retries.
    window, client = make_window(qtbot)
    run_full_cycle(window)
    client.accept = False
    select_with_fetches(qtbot, window)
    assert window._last_detail_unit is None


def test_same_unit_reselect_does_not_refetch(qtbot):
    # The dedup half: a post-refresh restore of the SAME unit must not
    # re-flash loading… or triple the subprocess rate (QA over-clearing guard).
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert len(calls_of(client, "fetch_log")) == 1
    run_full_cycle(window)  # 10s-style cycle; selection restored
    assert len(calls_of(client, "fetch_log")) == 1


def test_vanished_unit_clears_tabs_and_dedup(qtbot):
    # A model reset clears the selection WITHOUT a signal — invalidation
    # happens in the render path, not the selection handler (QA Phase B
    # refutation of the clear-on-None proposals).
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert window._last_detail_unit == "a.timer"
    run_full_cycle(window, timers_json="[]", units_json="[]")
    assert window._last_detail_unit is None
    assert window._expected_tab_ids == set()
    assert window.tab_log.toPlainText() == "(unit no longer listed)"


def test_stale_tab_response_is_dropped(qtbot):
    window, client = make_window(qtbot)
    window.tab_log.setPlainText("untouched")
    window._on_finished("log:user:ghost.service", '{"MESSAGE":"hi"}')
    assert window.tab_log.toPlainText() == "untouched"


def test_set_scope_stamps_tabs_and_clears_expected_ids(qtbot):
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"log:user:a.timer"}
    window.tab_log.setPlainText("old scope content")
    window.set_scope("system")
    assert window._expected_tab_ids == set()
    assert window.tab_log.toPlainText() == "(select a unit)"
    window._on_finished("log:user:a.timer", '{"MESSAGE":"late"}')
    assert window.tab_log.toPlainText() == "(select a unit)"


def test_parse_failure_writes_tab_and_keeps_dedup(qtbot):
    # Frozen-loading… fix (QA synthesis #4): the failure lands IN the tab;
    # the dedup stays set so a persistently corrupt source doesn't strobe —
    # ⟳ is the retry path.
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"log:user:a.timer"}
    window._last_detail_unit = "a.timer"
    window._on_finished("log:user:a.timer", "not json\n")
    assert "(parse failed)" in window.tab_log.toPlainText()
    assert window._last_detail_unit == "a.timer"


def test_on_failed_writes_expected_tab_only(qtbot):
    # Since DEF-V11-02 the Schedule tab has its OWN fetch (schedtab) — a cat
    # failure must write the Unit file tab and leave a good schedule alone
    # (the old design populated Schedule from cat and stamped it here too).
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"cat:user:a.timer", "schedtab:user:a.timer"}
    window.tab_schedule.setPlainText("Triggers (effective, drop-ins included):")
    window._on_failed("cat:user:a.timer", "No files found")
    assert "(fetch failed)" in window.tab_unitfile.toPlainText()
    assert "No files found" in window.tab_unitfile.toPlainText()
    assert window.tab_schedule.toPlainText().startswith("Triggers")
    # And the status bar still carries the error.
    assert "No files found" in window.statusBar().currentMessage()


def test_on_failed_schedtab_writes_schedule_tab(qtbot):
    # The Schedule tab covers its own failure — never stranded at loading…
    # for exactly the broken units this tool exists to investigate (SFH-F5).
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"schedtab:user:a.timer"}
    window.tab_schedule.setPlainText("loading…")
    window._on_failed("schedtab:user:a.timer", "Failed to get properties")
    assert "(fetch failed)" in window.tab_schedule.toPlainText()
    assert "Failed to get properties" in window.tab_schedule.toPlainText()


def test_schedtab_fill_renders_triggers_cadence_and_chains_calendar(qtbot):
    # The Schedule tab pipeline: schedtab payload → triggers + cadence line,
    # then a chained systemd-analyze fetch for the first calendar trigger
    # (admitted into the freshness set BEFORE requesting).
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"schedtab:user:a.timer"}
    window._on_finished("schedtab:user:a.timer", SCHEDULES_TEXT)
    text = window.tab_schedule.toPlainText()
    assert "OnCalendar=*-*-* 03:50:00" in text
    assert "Cadence: daily" in text
    assert "Next elapse: Fri 2026-06-12 03:50:00 PDT" in text
    assert "(calculating…)" in text
    assert ("fetch_calendar", "*-*-* 03:50:00") in client.calls
    assert "calendar:*-*-* 03:50:00" in window._expected_tab_ids
    window._on_finished("calendar:*-*-* 03:50:00", "Next elapse: …five iterations…")
    text = window.tab_schedule.toPlainText()
    assert "(calculating…)" not in text
    assert "five iterations" in text


def test_schedtab_fill_no_triggers_is_honest(qtbot):
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"schedtab:user:x.timer"}
    window._on_finished(
        "schedtab:user:x.timer",
        "TimersCalendar=\nTimersMonotonic=\nNextElapseUSecRealtime=\n",
    )
    assert window.tab_schedule.toPlainText() == "(no triggers configured)"
    assert not calls_of(client, "fetch_calendar")


def test_schedtab_monotonic_only_skips_calendar_chain(qtbot):
    # A monotonic-only timer has no calendar expression for systemd-analyze;
    # the tab must finish WITHOUT a dangling "(calculating…)".
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"schedtab:user:b.timer"}
    window._on_finished(
        "schedtab:user:b.timer",
        "TimersMonotonic={ OnUnitActiveUSec=1d ; next_elapse=5d 13h }\n"
        "TimersMonotonic={ OnBootUSec=12h ; next_elapse=12h }\n"
        "NextElapseUSecRealtime=\n",
    )
    text = window.tab_schedule.toPlainText()
    assert "OnUnitActiveUSec=1d" in text
    assert "Cadence: every 1d + boot+12h" in text
    assert "(calculating…)" not in text
    assert not calls_of(client, "fetch_calendar")


def test_calendar_preview_failure_preserves_triggers(qtbot):
    # P2-6 (QA 2026-06-12): the chained elapse preview failing must replace
    # ONLY the "(calculating…)" placeholder — the triggers and cadence already
    # rendered by the schedtab fill must survive (the old code blanked the whole
    # tab to "(fetch failed)").
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"schedtab:user:a.timer"}
    window._on_finished("schedtab:user:a.timer", SCHEDULES_TEXT)
    assert "(calculating…)" in window.tab_schedule.toPlainText()
    window._on_failed("calendar:*-*-* 03:50:00", "systemd-analyze timed out")
    text = window.tab_schedule.toPlainText()
    assert "OnCalendar=*-*-* 03:50:00" in text  # triggers survive
    assert "Cadence: daily" in text
    assert "(calculating…)" not in text
    assert "elapse preview failed" in text
    # The status bar still carries the full error.
    assert "timed out" in window.statusBar().currentMessage()


def test_non_timer_selection_stamps_schedule_tab(qtbot):
    # PIN-4 (QA 2026-06-12): selecting a SERVICE row (not a timer) stamps the
    # Schedule tab "(not a timer — no schedule)" and fires NO schedtab fetch —
    # without the else branch the tab strands at "loading…" for every service.
    window, client = make_window(qtbot)
    window.view_box.setCurrentIndex(1)  # Services view: row 0 is a.service
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert window.tab_schedule.toPlainText() == "(not a timer — no schedule)"
    assert not calls_of(client, "fetch_tab_schedule")
    assert not any(i.startswith("schedtab:") for i in window._expected_tab_ids)


def test_on_failed_unexpected_id_leaves_tab_untouched(qtbot):
    window, client = make_window(qtbot)
    window.tab_log.setPlainText("untouched")
    window._on_failed("log:user:ghost.service", "boom")
    assert window.tab_log.toPlainText() == "untouched"
    assert "boom" in window.statusBar().currentMessage()


def test_manual_refresh_clears_dedup(qtbot):
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert len(calls_of(client, "fetch_log")) == 1
    window._manual_refresh()
    run_full_cycle(window)
    assert len(calls_of(client, "fetch_log")) == 2


# -- actions ---------------------------------------------------------------------


def test_run_now_targets_activated_service_with_exact_argv(qtbot):
    # The semantic heart of the Run-now button: it starts the SERVICE the
    # timer activates (re-arming the .timer would do nothing visible), with
    # --no-block so a long oneshot can't blow the watchdog (USER-P1).
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    window._do_action("start", run_now=True)
    assert (
        "run_action",
        ["systemctl", "--user", "start", "--no-block", "--", "a.service"],
        "a.service",
    ) in client.calls


def test_action_rejection_is_surfaced(qtbot):
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    client.accept = False
    window._do_action("start", run_now=True)
    assert "previous action still running" in window.statusBar().currentMessage()


def test_action_refusal_is_surfaced(qtbot):
    # Belt-and-suspenders: even if button enablement regressed, the policy
    # layer refuses — loudly.
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    window.scope = "system"  # bypass set_scope to simulate an enablement bug
    window._do_action("start")
    assert "refused" in window.statusBar().currentMessage()
    assert not calls_of(client, "run_action")


def test_action_ok_with_stderr_shows_systemds_words(qtbot):
    # exit-0-with-stderr (probed: enable on an [Install]-less unit): the
    # payload carries systemd's explanation — show it, never a bare "ok"
    # narrating nothing-happened as success.
    window, client = make_window(qtbot)
    window._on_finished("action:a.service", "The unit files have no installation config")
    assert "no installation config" in window.statusBar().currentMessage()


def test_failed_action_does_not_refresh(qtbot):
    # Pinned as a visible decision (QA SFH): a failed action surfaces its
    # error and leaves the table alone — the refresh would imply success.
    window, client = make_window(qtbot)
    window._on_failed("action:a.service", "boom")
    assert not calls_of(client, "list_timers")


# -- scope-transition safety -------------------------------------------------------


def test_data_scope_gates_enablement_during_transition(qtbot):
    # After a system→user flip, the OLD scope's rows linger until the new
    # cycle lands — actions must stay disabled in that window (QA SFH-F8).
    window, client = make_window(qtbot)
    run_full_cycle(window)
    select_with_fetches(qtbot, window)
    assert all(a.isEnabled() for a in window.action_buttons)
    window._data_scope = "system"  # simulate: rendered rows belong elsewhere
    window._update_action_enablement()
    assert all(not a.isEnabled() for a in window.action_buttons)


def test_scroll_position_survives_refresh(qtbot):
    # Model resets snap the viewport to the top — the 10s refresh must not
    # yank the user mid-browse (QA AT-F4).
    window, client = make_window(qtbot)
    many = [
        {
            "next": 1781136604691295,
            "left": 1,
            "last": 1781115051863104,
            "passed": 1,
            "unit": f"unit{i:03}.timer",
            "activates": f"unit{i:03}.service",
        }
        for i in range(60)
    ]
    import json as _json

    payload = _json.dumps(many)
    window.resize(600, 300)
    window.show()
    qtbot.waitExposed(window)
    run_full_cycle(window, timers_json=payload, units_json="[]")
    qtbot.wait(20)  # let the offscreen view lay out before reading ranges
    bar = window.table.verticalScrollBar()
    assert bar.maximum() > 0, "test premise: the list must overflow the viewport"
    bar.setValue(bar.maximum() // 2)
    kept = bar.value()
    run_full_cycle(window, timers_json=payload, units_json="[]")
    qtbot.wait(20)
    assert bar.value() == kept
