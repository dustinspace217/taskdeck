"""Window-logic tests with a fake client — no subprocesses, pure routing.

MainWindow takes the client by injection precisely so these tests can drive
_on_finished/_on_failed directly with canned payloads and assert the
orchestration: pending-set lifecycle, the batched results fetch, stale-scope
drops, and error surfacing. auto_refresh=False guarantees zero side effects.
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


class FakeClient(QObject):
    """Signal-compatible stand-in for SystemdClient that records every call.

    Methods return True (request accepted) so the window's happy paths run;
    individual tests can flip `accept` to exercise rejection handling.
    """

    finished = Signal(str, str)
    failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.accept = True

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
    assert not [c for c in client.calls if c[0] == "fetch_results"]
    assert window.model.rowCount() == 0
    assert "0 units" in window.statusBar().currentMessage()


def test_stale_scope_response_is_dropped(qtbot):
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:system", LIST_TIMERS_JSON)  # user scope is active
    assert window._timers == []


def test_malformed_json_surfaces_parse_error(qtbot):
    window, client = make_window(qtbot)
    window.refresh()
    window._on_finished("timers:user", "not json")
    assert window.statusBar().currentMessage().startswith("ERROR parsing")


def test_failed_signal_surfaces_verbatim(qtbot):
    window, client = make_window(qtbot)
    window._on_failed("timers:user", "systemctl exit 1: boom")
    assert "boom" in window.statusBar().currentMessage()


def test_services_view_hides_inactive(qtbot):
    window, client = make_window(qtbot)
    window.view_box.setCurrentIndex(1)  # Services view
    window.refresh()
    window._on_finished("timers:user", "[]")
    window._on_finished("services:user", LIST_UNITS_JSON)
    window._on_finished("results:user", "Result=success\nExecMainStatus=0\n")
    assert window.model.rowCount() == 1  # b.service (inactive) is hidden


def test_stale_tab_response_is_dropped(qtbot):
    # Tab writes are freshness-gated by exact request id; a response for a
    # unit that was never selected (or no longer is) must not touch the tabs.
    window, client = make_window(qtbot)
    window.tab_log.setPlainText("untouched")
    window._on_finished("log:user:ghost.service", '{"MESSAGE":"hi"}')
    assert window.tab_log.toPlainText() == "untouched"


def test_rejected_results_fetch_keeps_old_unit_list(qtbot):
    # If a previous batched `show` is still in flight (fetch_results returns
    # False), the window must NOT swap _result_units — the in-flight response
    # has to parse against the unit list its argv was built from.
    window, client = make_window(qtbot)
    window._result_units = ["old.service"]
    client.accept = False
    window.refresh()
    window._on_finished("timers:user", LIST_TIMERS_JSON)
    window._on_finished("services:user", LIST_UNITS_JSON)
    assert window._result_units == ["old.service"]
