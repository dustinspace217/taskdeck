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
    """Drive one complete refresh cycle through _on_finished."""
    window.refresh()
    window._on_finished("timers:user", timers_json)
    window._on_finished("services:user", units_json)
    window._on_finished("results:user", RESULTS_TEXT)


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
    assert window._expected_tab_ids == {
        "log:user:a.timer",
        "details:user:a.timer",
        "cat:user:a.timer",
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


def test_on_failed_writes_expected_tab_and_cat_stamps_schedule(qtbot):
    window, client = make_window(qtbot)
    window._expected_tab_ids = {"cat:user:a.timer"}
    window._on_failed("cat:user:a.timer", "No files found")
    assert "(fetch failed)" in window.tab_unitfile.toPlainText()
    assert "No files found" in window.tab_unitfile.toPlainText()
    # The Schedule tab is populated from the cat SUCCESS branch — a cat
    # failure must not strand it at loading… (QA SFH-F5).
    assert "fetch failed" in window.tab_schedule.toPlainText()
    # And the status bar still carries the error.
    assert "No files found" in window.statusBar().currentMessage()


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
