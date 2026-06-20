"""Integration tests for the Calendar page wired into MainWindow.

These pin the host-side behavior of Task 7's end-to-end Day slice: selecting
"Calendar" in the View dropdown swaps the central QStackedWidget to the
CalendarView page; a calendar-row click fires the SAME detail-tab fetches the
table selection uses (the selection adapter); and a calendar build fires exactly
ONE journal query plus a projection per ELIGIBLE timer (disabled timers get no
projection — spec §9).

A local FakeClient (records calls, no subprocess) is used rather than the real
SystemdClient so the build/select/swap logic is exercised hermetically — the
same injection idiom test_window_logic.py uses, extended with the two calendar
fetch methods. MainWindow is built with auto_refresh=False so construction fires
zero subprocesses; tests that need the selection-fetch path flip the private
_auto_refresh flag afterwards (the timer was never started).
"""
from datetime import UTC, datetime

from PySide6.QtCore import QObject, Signal

from taskdeck.main_window import MainWindow
from taskdeck.systemd_client import ScheduleInfo, TimerRow


def _usec(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()) * 1_000_000


def _utc(usec):
    """Render a µs epoch as the `Wkd YYYY-MM-DD HH:MM:SS` payload that the
    `(in UTC):` line carries, so parse_projection reads it back to the same epoch.
    Lets a test express slot instants as epochs and feed them through the real
    projection parser without hand-writing date strings."""
    dt = datetime.fromtimestamp(usec / 1_000_000, UTC)
    return dt.strftime("%a %Y-%m-%d %H:%M:%S")


class FakeClient(QObject):
    """Signal-compatible stand-in for SystemdClient that records every call.

    Mirrors test_window_logic.FakeClient (kept local rather than imported so the
    two test modules stay independent), with the two calendar fetch methods
    Task 7 adds: fetch_cal_journal (the single manager-scoped run query) and
    fetch_cal_projection (one per eligible timer). Both record and return
    `accept` so single-flight-rejection handling can be exercised by flipping it.
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

    def fetch_cal_projection(
        self,
        scope: str,
        unit: str,
        expr: str,
        base_epoch: int,
        iterations: int,
        tag: str = "",
    ) -> bool:
        # `tag` mirrors the real client (F3 generation stamping): the host passes
        # f"#{exprIdx}@{gen}" so each build's projection ids are unique. Recorded
        # so a test can inspect it; the existing positional asserts (c[2]=unit,
        # c[3]=expr) are unchanged because tag is appended last.
        return self._record(
            "fetch_cal_projection", scope, unit, expr, base_epoch, iterations, tag
        )

    def fetch_cal_journal(
        self, scope: str, since_epoch: int, until_epoch: int, tag: str = ""
    ) -> bool:
        return self._record("fetch_cal_journal", scope, since_epoch, until_epoch, tag)

    def fetch_cal_coverage(self, scope: str, since_epoch: int, tag: str = "") -> bool:
        # The F1 unfiltered oldest-entry probe: one chronological `journalctl`
        # read to learn the journal's coverage floor for the window, so a short
        # retention can't bloom false gaps before the journal even existed.
        # `tag` is the per-build generation stamp (F3). Recorded like the others.
        return self._record("fetch_cal_coverage", scope, since_epoch, tag)

    def run_action(self, argv: list, unit: str) -> bool:
        return self._record("run_action", list(argv), unit)

    def flush_finished(self) -> None:
        self.calls.append(("flush_finished",))


def make_window(qtbot):
    client = FakeClient()
    window = MainWindow(client, auto_refresh=False)
    qtbot.addWidget(window)
    return window, client


def calls_of(client, name):
    return [c for c in client.calls if c[0] == name]


def pending_id(window, prefix):
    """The single outstanding fan-in id whose kind matches `prefix`.

    Fan-in ids are now STAMPED with the build generation (F3) — `calcover:user`
    becomes `calcover:user@3`, `calproj:user:x` becomes `calproj:user:x#0@3` —
    so a test can no longer deliver a response by a hardcoded id. It must read
    the ACTUAL fired id from the build's pending set instead. This returns the
    one id starting with `prefix` (coverage/journal are unique per build);
    asserts exactly one so a test never silently matches the wrong one.
    """
    matches = [i for i in window._cal_pending if i.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one pending {prefix!r}, got {matches}"
    return matches[0]


# -- stacked-widget swap ------------------------------------------------------


def test_selecting_calendar_shows_calendar_page(qtbot):
    # The View dropdown gains a "Calendar" entry that swaps the central
    # QStackedWidget to the CalendarView page (page 1) instead of refreshing
    # the table.
    window, client = make_window(qtbot)
    idx = window.view_box.findText("Calendar")
    assert idx >= 0
    window.view_box.setCurrentIndex(idx)
    assert window._stack.currentWidget() is window.calendar_view


def test_switching_back_to_timers_shows_table_page(qtbot):
    # Selecting Timers/Services after Calendar must return to the table page
    # (page 0) — the two views are independent pages, and the table path must
    # not be reachable while Calendar is showing nor stranded after leaving it.
    window, client = make_window(qtbot)
    cal_idx = window.view_box.findText("Calendar")
    window.view_box.setCurrentIndex(cal_idx)
    assert window._stack.currentWidget() is window.calendar_view
    window.view_box.setCurrentIndex(window.view_box.findText("Timers"))
    assert window._stack.currentIndex() == 0


# -- selection adapter --------------------------------------------------------


def test_calendar_selection_fires_detail_fetches(qtbot):
    # A click on a calendar row emits selected(unit); the adapter fires the same
    # detail-tab fetches the table selection uses, and sets the freshness id set
    # so only those responses may write tabs.
    window, client = make_window(qtbot)
    window._auto_refresh = True
    window._on_calendar_selected("a.timer")
    assert ("fetch_log", "user", "a.timer") in client.calls
    assert window._expected_tab_ids == {
        f"{k}:user:a.timer" for k in ("log", "details", "cat", "schedtab")
    }


# -- build fan-out (eligibility) ----------------------------------------------


def test_build_skips_projection_for_disabled_timer(qtbot):
    # A disabled timer (next=None) gets no projection fetch; a never-ran timer
    # (last=0) still gets one. The single past-runs query + the coverage probe
    # fire once regardless (spec §9 / §4.2; F1). Projections are now GATED on the
    # coverage probe (F1), so they only fan out after it lands.
    window, client = make_window(qtbot)
    window._timers = [
        TimerRow("off.timer", "off.service", None, 123),   # disabled
        TimerRow("new.timer", "new.service", 999, 0),       # never ran
    ]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}
    window._build_calendar(1_781_000_000_000_000, 1_781_086_400_000_000)
    # Before coverage lands, NO projection has fired (the gating).
    assert not calls_of(client, "fetch_cal_projection"), "projections gated on coverage"
    assert len(calls_of(client, "fetch_cal_coverage")) == 1   # one probe per build
    assert len(calls_of(client, "fetch_cal_journal")) == 1    # one run query per build
    # Coverage lands → the planned projections fan out (only for eligible timers).
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(1_781_000_000_000_000))
    projs = [c for c in client.calls if c[0] == "fetch_cal_projection"]
    assert not any(c[2] == "off.timer" for c in projs)   # disabled → no projection
    assert any(c[2] == "new.timer" for c in projs)        # enabled → projection


# -- build fan-IN (responses → set_events) ------------------------------------


def test_calendar_fan_in_assembles_runs_and_gaps_into_set_events(qtbot):
    # The fan-IN half: once BOTH the single journal query and the per-timer
    # projection land, _finalize_calendar must compute gaps and push the
    # assembled events into the view. This pins the host wiring (journal→ran,
    # projection→slots, finalize→gap+set_events) that the model unit tests can't
    # reach — a regression in the dispatch routing or finalize would slip past
    # the fan-OUT tests above. (Found as a coverage gap in Phase-2 review.)
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    slot1 = _usec(2026, 6, 15, 6)           # has a run → "ran"
    slot2 = _usec(2026, 6, 16, 6)           # no run, in coverage → "gap"
    win_start = _usec(2026, 6, 14, 0)
    win_end = now = _usec(2026, 6, 17, 0)

    # Capture what the view is ultimately handed.
    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append((a, k))  # type: ignore[method-assign]

    window._cal_now = now  # finalize clamps gaps to [coverage_start, _cal_now]
    window._build_calendar(win_start, win_end)
    window._cal_now = now  # _build_calendar recomputes now from the clock; pin it

    # The build now GATES projections on the coverage probe (F1): deliver it
    # first (oldest = win_start so the whole window is in coverage) by its ACTUAL
    # stamped id, which fires the planned projection.
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))

    # Projection: parse_projection reads only the "(in UTC):" lines → two slots.
    proj = (
        "  Original form: *-*-* 06:00:00\n"
        "    Next elapse: Mon 2026-06-15 06:00:00 PDT\n"
        "       (in UTC): Mon 2026-06-15 06:00:00 UTC\n"
        "   Iteration #2: Tue 2026-06-16 06:00:00 PDT\n"
        "       (in UTC): Tue 2026-06-16 06:00:00 UTC\n"
    )
    # Journal: one FAILED run at slot1 for the activated service.
    journal = (
        '{"USER_UNIT":"new.service","JOB_RESULT":"failed",'
        f'"__REALTIME_TIMESTAMP":"{slot1}"}}\n'
    )
    # Deliver by the ACTUAL stamped ids (generation-tagged), not hardcoded ones.
    window._on_finished(pending_id(window, "calproj:"), proj)
    window._on_finished(pending_id(window, "caljournal:"), journal)

    assert len(handed) == 1, "set_events fires exactly once, after BOTH land"
    events = handed[0][0][0]
    ran = [e for e in events if e.kind == "ran"]
    gaps = [e for e in events if e.kind == "gap"]
    assert any(e.unit == "new.timer" and e.result == "failure" and e.when == slot1
               for e in ran), "journal failure parsed + bucketed to its timer"
    assert any(e.unit == "new.timer" and e.when == slot2 for e in gaps), \
        "slot2 had no run within coverage → a gap (projection→slots→finalize wired)"


# -- helpers for the two-stage (coverage-probe → projections) build -----------


def _coverage_line(oldest_usec):
    """Build a journalctl `-n1` JSON line carrying the oldest entry's timestamp.

    F1's coverage probe runs an UNFILTERED `journalctl -o json -n1 --since=@…`
    and reads __REALTIME_TIMESTAMP off the single record to learn how far back
    the journal actually reaches. The handler reads only that field, so a minimal
    one-field line is enough."""
    return f'{{"__REALTIME_TIMESTAMP":"{oldest_usec}"}}\n'


def _proj_ids(client):
    """The unit names of every fetch_cal_projection call recorded so far."""
    return [c[2] for c in client.calls if c[0] == "fetch_cal_projection"]


# -- F1: gap coverage clamped to the JOURNAL floor, not the window ------------


def test_coverage_probe_clamps_gaps_to_journal_floor(qtbot):
    # F1 (P0): the journal on this machine may only reach back a couple days, so
    # a window whose start predates the oldest entry must NOT bloom amber gaps
    # across the pre-coverage region. The coverage probe reports an oldest entry
    # strictly NEWER than win_start; gaps before that floor must be suppressed.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 10, 0)
    now = win_end = _usec(2026, 6, 17, 0)
    # Journal only reaches back to the 15th — three daily slots (10/11/12) sit
    # BEFORE coverage and must be silent, not gaps.
    oldest = _usec(2026, 6, 15, 0)
    pre_slot = _usec(2026, 6, 12, 6)   # before coverage → must NOT be a gap
    post_slot = _usec(2026, 6, 16, 6)  # after coverage, no run → IS a gap

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now  # pin (the build recomputes now from the clock)

    # Stage 1: the coverage probe lands → the floor is known → projections fan out.
    # Deliver by the ACTUAL stamped coverage id read off the build's pending set.
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(oldest))
    assert "new.timer" in _proj_ids(client), "projection fans out after coverage lands"

    # Stage 2: the projection (two slots, one pre- one post-coverage) and the
    # journal (no runs at all) land → finalize.
    proj = (
        f"       (in UTC): {_utc(pre_slot)} UTC\n"
        f"       (in UTC): {_utc(post_slot)} UTC\n"
    )
    window._on_finished(pending_id(window, "calproj:"), proj)
    window._on_finished(pending_id(window, "caljournal:"), "")  # no runs

    assert len(handed) == 1
    gaps = [e for e in handed[0][0] if e.kind == "gap"]
    gap_whens = {e.when for e in gaps}
    assert pre_slot not in gap_whens, "pre-coverage slot must be silent (no journal data)"
    assert post_slot in gap_whens, "post-coverage missed slot is a real gap"


def test_zero_coverage_records_suppresses_all_gaps(qtbot):
    # F1 zero-records branch: an EMPTY probe result can't distinguish "nothing
    # ran" from "all rotated out", so coverage_start = now and EVERY slot is
    # silent (no gaps at all). The safe direction — never invent a miss we can't
    # prove.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 10, 0)
    now = win_end = _usec(2026, 6, 17, 0)

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now

    # ZERO records → coverage_start = now. Deliver by the actual stamped id.
    window._on_finished(pending_id(window, "calcover:"), "")
    proj = (
        f"       (in UTC): {_utc(_usec(2026, 6, 12, 6))} UTC\n"
        f"       (in UTC): {_utc(_usec(2026, 6, 16, 6))} UTC\n"
    )
    window._on_finished(pending_id(window, "calproj:"), proj)
    window._on_finished(pending_id(window, "caljournal:"), "")

    assert len(handed) == 1
    gaps = [e for e in handed[0][0] if e.kind == "gap"]
    assert gaps == [], "zero coverage records → no gaps anywhere (can't prove a miss)"


# -- F2 (release half): a malformed projection releases the fan-in barrier ----


def test_malformed_projection_releases_barrier_and_renders_partial(qtbot):
    # F2 (P0): a malformed `(in UTC)` date used to raise straight out of
    # parse_projection (now skipped by the model fix); BELT-AND-SUSPENDERS, even
    # an outright parse FAILURE for a calproj id must release the calendar barrier
    # and finalize-partial — never wedge the fan-in forever. Here we simulate a
    # hard failure echo for the projection id and assert the page still renders
    # with whatever DID land (the journal's runs).
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 17, 0)

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now
    # Coverage lands → the projection fans out (now in pending with a stamped id).
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))

    # The journal lands fine; the projection FAILS (a poison subprocess). Both are
    # delivered by their ACTUAL stamped ids; the failed projection id must be the
    # real one for the barrier-release branch to match it in _cal_pending.
    proj_id = pending_id(window, "calproj:")
    window._on_finished(pending_id(window, "caljournal:"), "")
    window._on_failed(proj_id, "systemd-analyze: bad calendar")

    assert len(handed) == 1, "barrier released on the projection failure → partial render"


# -- F3: a stale-generation response is dropped -------------------------------


def test_stale_generation_response_is_dropped(qtbot):
    # F3 (P0): rapid nav starts Build B before Build A's projection lands. Build
    # A's response must NOT satisfy Build B's barrier with Build A's slots. The
    # per-build generation stamp drops the stale response.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    # Build A. Deliver A's coverage by its ACTUAL stamped id so A's projection
    # fans out, then capture A's real projection id (stamped #idx@genA) before B
    # supersedes it.
    win_a = _usec(2026, 6, 14, 0)
    window._build_calendar(win_a, _usec(2026, 6, 17, 0))
    gen_a = window._cal_gen  # A's generation
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_a))
    proj_id_a = pending_id(window, "calproj:")  # A's actual fired projection id

    # Build B supersedes A (a nav move) BEFORE A's projection arrives. The bump
    # makes B's ids carry a different generation, so A's id is no longer pending.
    win_b = _usec(2026, 6, 1, 0)
    window._build_calendar(win_b, _usec(2026, 6, 4, 0))
    assert window._cal_gen != gen_a, "a new build bumps the generation"
    assert proj_id_a not in window._cal_pending, "A's stamped id is not in B's barrier"

    # A's late projection echo arrives — it must be dropped (stale generation),
    # NOT applied to B's barrier or slots. Delivered by A's REAL stamped id; the
    # generation kwarg is the secondary guard, but the primary drop is that A's
    # id is no longer in B's pending set.
    before = len(handed)
    window._on_cal_projection(
        proj_id_a,
        f"       (in UTC): {_utc(_usec(2026, 6, 15, 6))} UTC\n",
        generation=gen_a,
    )
    assert len(handed) == before, "stale-generation projection did not finalize B"
    assert "new.timer" not in window._cal_slots, "stale slots not merged into B"


# -- F4: multi-trigger timer projects EVERY OnCalendar expr -------------------


def test_multi_trigger_projects_each_expression(qtbot):
    # F4 (P1): a timer with two OnCalendar expressions must fire a projection per
    # expression (unique id per expr), and a miss on the SECOND trigger must
    # surface as a gap. Before F4 only calendar[0] was projected, so a
    # second-trigger miss was never even a slot.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("multi.timer", "multi.service", 999, 0)]
    # Two daily triggers at different hours — the SECOND (18:00) will miss.
    window._last_schedules = {
        "multi.timer": ScheduleInfo(("*-*-* 06:00:00", "*-*-* 18:00:00"), ())
    }

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 17, 0)
    slot_a = _usec(2026, 6, 15, 6)    # first trigger — HAS a run
    slot_b = _usec(2026, 6, 15, 18)   # second trigger — NO run → a gap

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now
    # Coverage lands by its actual stamped id → the planned projections fan out.
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))

    # Two distinct projection fetches fired — one per expression (unique ids).
    proj_calls = [c for c in client.calls if c[0] == "fetch_cal_projection"]
    assert len(proj_calls) == 2, "one projection per OnCalendar expr"
    exprs = {c[3] for c in proj_calls}
    assert exprs == {"*-*-* 06:00:00", "*-*-* 18:00:00"}

    # The build fired ids with a per-expr index + generation suffix
    # (calproj:user:multi.timer#0@gen, #1@gen); recover BOTH from pending. Both
    # ids belong to multi.timer, and F4 unions every expression's slots into the
    # unit's one list — so it doesn't matter WHICH id carries which slot; what
    # matters is that both 06:00 and 18:00 reach the unit's slot list (only then
    # is the 18:00 miss even a candidate gap). Deliver one slot per id.
    proj_ids = sorted(i for i in window._cal_pending if i.startswith("calproj:"))
    assert len(proj_ids) == 2, "two outstanding projection ids, one per expr"
    window._on_finished(proj_ids[0], f"       (in UTC): {_utc(slot_a)} UTC\n")
    window._on_finished(proj_ids[1], f"       (in UTC): {_utc(slot_b)} UTC\n")
    # Journal: only the 06:00 run happened; 18:00 was missed.
    journal = (
        '{"USER_UNIT":"multi.service","JOB_RESULT":"done",'
        f'"__REALTIME_TIMESTAMP":"{slot_a}"}}\n'
    )
    window._on_finished(pending_id(window, "caljournal:"), journal)

    assert len(handed) == 1
    events = handed[0][0]
    # The unioned slots cover BOTH triggers, so the 18:00 miss is a real gap.
    gap_whens = {e.when for e in events if e.kind == "gap"}
    assert slot_b in gap_whens, "second-trigger miss surfaces as a gap (slots unioned)"
    assert slot_a not in gap_whens, "first trigger ran → not a gap"
