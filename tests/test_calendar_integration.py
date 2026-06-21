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

from taskdeck.calendar_model import FWD_PROJECTION_ITERATIONS, summarize
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

    NOTE: this asserts uniqueness, so it MUST NOT be used for `calproj:` — every
    eligible timer×expression now has TWO projection ids (the win_start one and
    the now-based forward one, B). Use past_proj_id / fwd_proj_ids for those.
    """
    matches = [i for i in window._cal_pending if i.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one pending {prefix!r}, got {matches}"
    return matches[0]


# Projection ids carry a tag after the unit name: the WIN_START projection is
# "...#<idx>@<gen>" and the NOW-based FORWARD projection (B) is "...#f<idx>@<gen>"
# — the leading 'f' is the discriminator. These split the two so a test can
# deliver PAST slots (gaps) on the win_start id and FUTURE slots (or empty) on the
# forward id, without one masking the other.


def _is_fwd(proj_id):
    """True if `proj_id` is a NOW-based forward projection (tag '#f…')."""
    # The tag begins at the LAST '#'; the unit name may itself contain no '#'
    # (systemd unit names don't), so the last '#' starts the tag. A forward tag's
    # first char after '#' is 'f'; a win_start tag's is a digit.
    return proj_id.rsplit("#", 1)[1].startswith("f")


def past_proj_id(window):
    """The single outstanding WIN_START projection id (the one feeding gaps).

    Asserts exactly one so a single-timer/single-expr test never silently matches
    the wrong projection. For a multi-expr test, use the per-id forms directly.
    """
    matches = [
        i for i in window._cal_pending if i.startswith("calproj:") and not _is_fwd(i)
    ]
    assert len(matches) == 1, f"expected one win_start calproj, got {matches}"
    return matches[0]


def fwd_proj_ids(window):
    """Every outstanding NOW-based forward projection id (B).

    A test drains these (delivering empty or future slots) so the fan-in barrier
    releases — every forward projection is in _cal_pending and must be answered
    for _finalize_calendar to fire.
    """
    return [i for i in window._cal_pending if i.startswith("calproj:") and _is_fwd(i)]


def drain_fwd_projections(window, payload=""):
    """Answer all outstanding forward projections with `payload` (default empty).

    Lets a test that only cares about PAST gaps release the forward half of the
    barrier without inventing future slots. With the default empty payload the
    forward projections contribute no slots, so they don't perturb the assertions.
    """
    for fid in fwd_proj_ids(window):
        window._on_finished(fid, payload)


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
    # The win_start projection carries the two past slots; the forward projection
    # (B) is drained empty so the barrier releases without adding future slots.
    window._on_finished(past_proj_id(window), proj)
    drain_fwd_projections(window)
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
    """Build the first JSON line of F1's coverage probe, carrying the oldest ts.

    F1's probe runs an UNFILTERED `journalctl -o json --since=@… --output-fields=
    __REALTIME_TIMESTAMP` (deliberately NOT `-n1` — that counts from the END and
    returns the newest line; see fetch_cal_coverage). The stream is chronological,
    so its FIRST line is the oldest entry at/after the window start, and the handler
    reads __REALTIME_TIMESTAMP off only that line — a minimal one-field line suffices."""
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
    window._on_finished(past_proj_id(window), proj)
    drain_fwd_projections(window)  # forward projection adds no slots here
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
    window._on_finished(past_proj_id(window), proj)
    drain_fwd_projections(window)
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

    # The journal lands fine; the win_start projection FAILS (a poison
    # subprocess). The forward projection (B) is drained so the only thing keeping
    # the barrier open is the failure-release of the win_start id. The failed id
    # must be the real one for the barrier-release branch to match it in
    # _cal_pending.
    proj_id = past_proj_id(window)
    window._on_finished(pending_id(window, "caljournal:"), "")
    drain_fwd_projections(window)
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
    proj_id_a = past_proj_id(window)  # A's actual fired win_start projection id

    # Build B supersedes A (a nav move) BEFORE A's projection arrives. The bump
    # makes B's ids carry a different generation, so A's id is no longer pending.
    win_b = _usec(2026, 6, 1, 0)
    window._build_calendar(win_b, _usec(2026, 6, 4, 0))
    assert window._cal_gen != gen_a, "a new build bumps the generation"
    assert proj_id_a not in window._cal_pending, "A's stamped id is not in B's barrier"

    # A's late projection echo arrives — it must be dropped (stale generation),
    # NOT applied to B's barrier or slots. Delivered through the PRODUCTION
    # dispatch entry `_on_finished(proj_id_a, ...)` with NO generation kwarg
    # (production never passes one — the host only ever calls _on_cal_projection
    # via _dispatch_finished, which forwards just request_id + stdout). This
    # proves the real not-in-pending guard drops the stale echo, not the optional
    # belt-and-suspenders generation arg the previous version exercised (TA-P2a).
    before = len(handed)
    window._on_finished(
        proj_id_a,
        f"       (in UTC): {_utc(_usec(2026, 6, 15, 6))} UTC\n",
    )
    assert len(handed) == before, "stale-generation projection did not finalize B"
    assert proj_id_a not in window._cal_pending, "A's stale id never re-enters B's barrier"
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

    # Two WIN_START projection fetches fired — one per expression. The build also
    # fires a NOW-based forward projection per expression (B, base_epoch=now), so
    # filter to the win_start ones (base_epoch=win_start) to assert F4's one-per-
    # expr contract on the slots that feed gaps. (c[4] is base_epoch.)
    win_proj_calls = [
        c for c in client.calls
        if c[0] == "fetch_cal_projection" and c[4] == win_start
    ]
    assert len(win_proj_calls) == 2, "one win_start projection per OnCalendar expr"
    exprs = {c[3] for c in win_proj_calls}
    assert exprs == {"*-*-* 06:00:00", "*-*-* 18:00:00"}
    # The forward projections fired too — one per expression, based at now.
    fwd_proj_calls = [
        c for c in client.calls
        if c[0] == "fetch_cal_projection" and c[4] == now
    ]
    assert len(fwd_proj_calls) == 2, "one forward projection per OnCalendar expr"

    # The build fired win_start ids with a per-expr index + generation suffix
    # (calproj:user:multi.timer#0@gen, #1@gen); recover BOTH from pending. Both
    # ids belong to multi.timer, and F4 unions every expression's slots into the
    # unit's one list — so it doesn't matter WHICH id carries which slot; what
    # matters is that both 06:00 and 18:00 reach the unit's slot list (only then
    # is the 18:00 miss even a candidate gap). Deliver one slot per win_start id;
    # drain the forward projections empty (they'd only add future slots, not gaps).
    proj_ids = sorted(
        i for i in window._cal_pending if i.startswith("calproj:") and not _is_fwd(i)
    )
    assert len(proj_ids) == 2, "two outstanding win_start projection ids, one per expr"
    window._on_finished(proj_ids[0], f"       (in UTC): {_utc(slot_a)} UTC\n")
    window._on_finished(proj_ids[1], f"       (in UTC): {_utc(slot_b)} UTC\n")
    drain_fwd_projections(window)
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


# -- R2-1: the now-instant slot is drawn SOMEWHERE (never nowhere) ------------


def test_now_instant_slot_is_drawn_somewhere(qtbot):
    # R2-1 (AT-P2a end-to-end): a no-run slot at EXACTLY `now` must appear in the
    # assembled events — as a gap (the closed gap boundary owns it) — never fall
    # through both projection (s > now) and the gap walk and be drawn nowhere.
    # Whole-second window so the slot lands exactly on `now` (µs-floored epochs).
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 16, 6)   # `now` lands exactly on the 06:00 slot
    now_slot = _usec(2026, 6, 16, 6)

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))
    # One win_start projection slot, exactly at now; no runs at all. The forward
    # projection is drained empty — the now-instant slot must be owned by the
    # win_start/gap path, not the forward one (which only emits s > now).
    proj = f"       (in UTC): {_utc(now_slot)} UTC\n"
    window._on_finished(past_proj_id(window), proj)
    drain_fwd_projections(window)
    window._on_finished(pending_id(window, "caljournal:"), "")

    assert len(handed) == 1
    events = handed[0][0]
    drawn = [e for e in events if e.when == now_slot and e.kind in ("gap", "projected")]
    assert drawn, "the now-instant slot must be drawn as a gap or projected, never nowhere"
    # With no run it is specifically a GAP (the closed boundary judges it).
    assert any(e.kind == "gap" for e in drawn), "no-run now-instant slot is a gap"


# -- R2-1: cross-fetch dedup of a coincident projected slot -------------------


def test_multi_trigger_coincident_slot(qtbot):
    # R2-1 (CR-P2): two OnCalendar expressions that project the SAME future
    # instant must yield exactly ONE `projected` event (not one per fetch) and
    # summarize().upcoming must count it ONCE. The old per-fetch emission in
    # _on_cal_projection drew it twice (the F4 union concatenates both fetches);
    # emitting from sorted(set(_cal_slots[unit])) in finalize collapses them.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("multi.timer", "multi.service", 999, 0)]
    # Two expressions that both fire at the SAME wall-clock instant (06:00) — a
    # contrived-but-legal multi-trigger whose slots coincide.
    window._last_schedules = {
        "multi.timer": ScheduleInfo(("*-*-* 06:00:00", "*-*-* 06:00:00"), ())
    }

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 15, 0)
    future_slot = _usec(2026, 6, 16, 6)     # strictly after now → a projection

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))

    # Both expressions fire a WIN_START projection; deliver the SAME future slot to
    # each. The forward projections (one per expr) are drained empty so the
    # assertion pins cross-EXPRESSION dedup of the win_start slots, not forward
    # ones (finalize dedups every source the same way via sorted(set(...))).
    proj_ids = sorted(
        i for i in window._cal_pending if i.startswith("calproj:") and not _is_fwd(i)
    )
    assert len(proj_ids) == 2, "one win_start projection id per expression"
    same_proj = f"       (in UTC): {_utc(future_slot)} UTC\n"
    window._on_finished(proj_ids[0], same_proj)
    window._on_finished(proj_ids[1], same_proj)
    drain_fwd_projections(window)
    window._on_finished(pending_id(window, "caljournal:"), "")

    assert len(handed) == 1
    events = handed[0][0]
    projected = [e for e in events if e.kind == "projected" and e.when == future_slot]
    assert len(projected) == 1, "coincident slot projected by two exprs → ONE event"
    # summarize() must agree: the coincident slot counts once toward upcoming.
    assert summarize(events).upcoming == 1, "upcoming counts the coincident slot once"


# -- R2-3: degraded render on a coverage-probe failure ------------------------


def test_coverage_failure_marks_render_degraded(qtbot):
    # R2-3 (SF-P1 + AT-P3a): a FAILED coverage probe must (a) leave the floor
    # unset → no gaps blooming to epoch, and (b) mark the render degraded so the
    # view's HEALTH strip warns the user the data is partial. We fail calcover via
    # _on_failed, then deliver the journal; the build finalizes degraded with no
    # false gaps. (Projections were gated on a SUCCESSFUL probe, so a failed probe
    # fires none — only the journal remains in the barrier.)
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 10, 0)
    now = win_end = _usec(2026, 6, 17, 0)

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append((a, k))  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now

    # Fail the coverage probe by its ACTUAL stamped id. No projections fire (they
    # were gated on a successful probe); the journal alone remains in the barrier.
    window._on_failed(pending_id(window, "calcover:"), "journalctl: boom")
    # Seed a past, in-window, NO-RUN slot so the unset-floor suppression is the
    # ONLY thing keeping it from blooming a false gap (AT-P3a). Without the None
    # floor, finalize would judge this slot against a 0/epoch floor and report it
    # as a gap; the suppression is what this assertion actually locks. (Slots
    # would normally come from a projection, which a failed probe never fires —
    # this mimics the poison-projection-then-coverage-fail path where slots DID
    # land but the floor never did.)
    orphan_slot = _usec(2026, 6, 14, 6)
    window._cal_slots["new.timer"] = [orphan_slot]
    # The journal lands (a run, but NOT near the orphan slot), releasing the last
    # barrier slot → finalize.
    journal = (
        '{"USER_UNIT":"new.service","JOB_RESULT":"done",'
        f'"__REALTIME_TIMESTAMP":"{_usec(2026, 6, 15, 6)}"}}\n'
    )
    window._on_finished(pending_id(window, "caljournal:"), journal)

    assert len(handed) == 1, "build finalizes once after the journal releases the barrier"
    args, kwargs = handed[0]
    assert kwargs.get("degraded") is True, "a failed coverage probe → degraded render"
    events = args[0]
    # The seeded orphan slot has no run nearby and sits before `now`; only the
    # unset-floor suppression keeps it from being a false gap.
    assert not any(e.kind == "gap" for e in events), "unset floor suppresses all gaps"


def test_complete_build_is_not_degraded(qtbot):
    # R2-3 non-vacuity guard (TA): the HAPPY fan-in path must pass degraded=False.
    # Without this negative assertion a hardcoded degraded=True would pass the
    # positive test above and train the user to ignore the warning. A clean build
    # (coverage + projection + journal all land) is NOT degraded.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 17, 0)

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append((a, k))  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))
    proj = f"       (in UTC): {_utc(_usec(2026, 6, 15, 6))} UTC\n"
    window._on_finished(past_proj_id(window), proj)
    drain_fwd_projections(window)
    window._on_finished(pending_id(window, "caljournal:"), "")

    assert len(handed) == 1
    _args, kwargs = handed[0]
    assert kwargs.get("degraded") is False, "a complete fan-in is NOT degraded"


def test_parse_error_in_calendar_handler_marks_degraded_and_releases(qtbot, monkeypatch):
    # R2-3 / stabilization TA-P3: the OTHER site that sets `_cal_degraded` is the
    # `_on_finished` parse-error except branch (the belt-and-suspenders sibling of
    # the tested `_on_failed` path). F2 hardened every real parser to skip bad
    # lines, so this branch only fires if a FUTURE parser raises — which is exactly
    # what its comment promises to handle. We simulate that future raise by patching
    # parse_run_journal to raise, and pin the branch's two contractual effects:
    # (a) the poisoned id leaves `_cal_pending` so the fan-in barrier can't wedge
    # forever, and (b) the build is marked degraded so the HEALTH strip warns. A
    # regression that dropped the `_cal_degraded = True` line in THIS branch (but
    # not in `_on_failed`) would pass every other test; this is the guard.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 10, 0)
    now = win_end = _usec(2026, 6, 17, 0)
    window._build_calendar(win_start, win_end)
    window._cal_now = now

    def boom(*_a, **_k):
        raise ValueError("simulated future parser raise")

    # parse_run_journal is a module-level import in main_window (called by
    # _on_cal_journal), so patch it there, not at its definition site.
    monkeypatch.setattr("taskdeck.main_window.parse_run_journal", boom)

    journal_id = pending_id(window, "caljournal:")
    window._on_finished(journal_id, '{"USER_UNIT":"new.service","JOB_RESULT":"done"}\n')

    assert window._cal_degraded is True, "a parse error marks the build degraded"
    assert journal_id not in window._cal_pending, "the poisoned id is released — no wedge"


# -- R2-4: out-of-order fan-in (journal before coverage) ----------------------


def test_out_of_order_journal_before_coverage(qtbot):
    # TA-P1 / AT-P3a: the journal can land BEFORE the coverage probe. Delivering
    # caljournal first must NOT trigger an early finalize (projections are still
    # outstanding once coverage fires them), and the final clamp must use the real
    # coverage floor (not 0/epoch). We deliver journal → coverage → projection and
    # assert exactly one finalize, with gaps clamped to the coverage floor.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("new.timer", "new.service", 999, 0)]
    window._last_schedules = {"new.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 10, 0)
    now = win_end = _usec(2026, 6, 17, 0)
    oldest = _usec(2026, 6, 15, 0)          # coverage floor
    pre_slot = _usec(2026, 6, 12, 6)        # before coverage → silent (no epoch bloom)
    post_slot = _usec(2026, 6, 16, 6)       # after coverage, no run → a gap

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now

    # Journal lands FIRST (no runs). The coverage probe and the planned (not-yet-
    # fired) projection are still outstanding, so NO finalize yet.
    window._on_finished(pending_id(window, "caljournal:"), "")
    assert handed == [], "journal-before-coverage must not finalize early"

    # Coverage lands → floor known → projections fire (win_start + forward).
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(oldest))
    assert "new.timer" in _proj_ids(client), "projection fans out after coverage"
    # Win_start projection carries the two past slots; the forward projection is
    # drained empty. Finalize fires only when the barrier empties (both delivered).
    proj = (
        f"       (in UTC): {_utc(pre_slot)} UTC\n"
        f"       (in UTC): {_utc(post_slot)} UTC\n"
    )
    window._on_finished(past_proj_id(window), proj)
    drain_fwd_projections(window)

    assert len(handed) == 1, "finalizes exactly once, after all three land"
    gap_whens = {e.when for e in handed[0][0] if e.kind == "gap"}
    assert pre_slot not in gap_whens, "pre-coverage slot silent → clamp used the floor, not epoch"
    assert post_slot in gap_whens, "post-coverage missed slot is a real gap"


# -- R2-4: empty-scope build finalizes an EMPTY calendar ----------------------


def test_empty_scope_build_finalizes_empty(qtbot):
    # TA-P2c: a build with no eligible timers must finalize an EMPTY (not stale)
    # calendar via the early-finalize branch — the coverage probe + journal land,
    # no projections fire, and set_events is handed an empty event list. Without
    # the early finalize the page would hang on a blank calendar.
    window, client = make_window(qtbot)
    window._timers = []   # no timers → no projections, no runs to bucket
    window._last_schedules = {}

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 17, 0)

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append((a, k))  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now
    # Coverage + journal land; no projection plan was built (no timers).
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))
    window._on_finished(pending_id(window, "caljournal:"), "")

    assert len(handed) == 1, "empty-scope build still finalizes (no hang)"
    args, kwargs = handed[0]
    assert args[0] == [], "an empty scope renders an empty (not stale) calendar"
    assert kwargs.get("degraded") is False, "a clean empty build is not degraded"


# -- B: forward projection from NOW — every cadence yields an upcoming slot ----


def test_minutely_timer_yields_an_upcoming_projected_event(qtbot):
    # B (the reported symptom: "upcoming doesn't show for some timers"). A MINUTELY
    # timer's WIN_START projection is capped (CELL_DRAW_MAX_PER_WINDOW) and, on a
    # window ending near now, spends the whole cap on PAST slots → ZERO future
    # slots → no upcoming ⏲. The NOW-based forward projection (small budget) must
    # produce an upcoming slot that finalize emits as a `projected` event. We drive
    # this through the REAL host build path (per the task: "assert via the host
    # build path"), feeding the win_start projection only past slots and the
    # forward projection a future slot — exactly the production split.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("minutely.timer", "minutely.service", 999, 0)]
    # The NORMALIZED minutely form `systemctl show` emits (every minute). It
    # classifies as "minutely" → interval 60s → projection_iterations caps at
    # CELL_DRAW_MAX_PER_WINDOW over the window, the cap-exhaustion this fix targets.
    window._last_schedules = {
        "minutely.timer": ScheduleInfo(("*-*-* *:*:00",), ())
    }

    win_start = _usec(2026, 6, 16, 0)
    now = win_end = _usec(2026, 6, 17, 0)
    past_slot = _usec(2026, 6, 16, 23, 58)   # ≤ now → a win_start slot (a gap here)
    future_slot = _usec(2026, 6, 17, 0, 1)   # > now → the upcoming slot

    handed: list = []
    window.calendar_view.set_events = lambda *a, **k: handed.append(a)  # type: ignore[method-assign]

    window._build_calendar(win_start, win_end)
    window._cal_now = now

    # A forward projection (base_epoch == now) MUST have been planned for the
    # minutely timer — that is the whole fix. (The win_start one bases at win_start.)
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))
    fwd_calls = [
        c for c in client.calls
        if c[0] == "fetch_cal_projection" and c[2] == "minutely.timer" and c[4] == now
    ]
    assert fwd_calls, "a minutely timer gets a NOW-based forward projection"

    # WIN_START projection: only a PAST slot (mimicking the cap-exhausted-on-past
    # reality — systemd would return 500 past slots; one is enough to prove the
    # point). FORWARD projection: the upcoming slot. Journal: empty.
    window._on_finished(
        past_proj_id(window), f"       (in UTC): {_utc(past_slot)} UTC\n"
    )
    for fid in fwd_proj_ids(window):
        window._on_finished(fid, f"       (in UTC): {_utc(future_slot)} UTC\n")
    window._on_finished(pending_id(window, "caljournal:"), "")

    assert len(handed) == 1
    events = handed[0][0]
    upcoming = [
        e for e in events
        if e.kind == "projected" and e.unit == "minutely.timer" and e.when == future_slot
    ]
    assert upcoming, (
        "the minutely timer must show an upcoming 'projected' event — the forward "
        "projection produced it where the capped win_start projection could not"
    )


def test_forward_projection_uses_now_base_distinct_from_win_start(qtbot):
    # Non-vacuity guard for B: the forward projection's base_epoch is NOW and the
    # win_start projection's is WIN_START — they are genuinely two different
    # fetches, not one mislabeled. A regression that based both at win_start (so
    # the forward one also produced only past slots) would still 'fire a second
    # projection' but show no upcoming; this pins the base_epoch difference that
    # makes the forward slots future.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("daily.timer", "daily.service", 999, 0)]
    window._last_schedules = {"daily.timer": ScheduleInfo(("*-*-* 06:00:00",), ())}

    win_start = _usec(2026, 6, 14, 0)
    now = win_end = _usec(2026, 6, 17, 0)

    window.calendar_view.set_events = lambda *a, **k: None  # type: ignore[method-assign]
    window._build_calendar(win_start, win_end)
    window._cal_now = now
    window._on_finished(pending_id(window, "calcover:"), _coverage_line(win_start))

    proj_calls = [c for c in client.calls if c[0] == "fetch_cal_projection"]
    bases = {c[4] for c in proj_calls}
    assert win_start in bases, "a win_start-based projection fired"
    assert now in bases, "a now-based forward projection fired"
    assert win_start != now and len(bases) == 2, "the two projections use distinct bases"
    # The forward projection uses the small fixed budget, not the window-sized one.
    fwd = [c for c in proj_calls if c[4] == now]
    assert all(c[5] == FWD_PROJECTION_ITERATIONS for c in fwd), (
        "forward projection uses FWD_PROJECTION_ITERATIONS"
    )


# -- diagnostic click-through (v2): failure -> Log tab, gap -> Schedule tab ----


def test_event_category_buckets_outcomes():
    # The routing category collapses (kind, result) into the buckets the host
    # steers on: a failed run -> 'failure' (Log), a missed run -> 'gap' (Schedule),
    # a healthy run -> 'ran', a projection -> 'upcoming'.
    from taskdeck.calendar_model import CalendarEvent
    from taskdeck.calendar_view import _event_category

    assert _event_category(CalendarEvent("u.timer", 1, "ran", "failure")) == "failure"
    assert _event_category(CalendarEvent("u.timer", 1, "ran", "success")) == "ran"
    assert _event_category(CalendarEvent("u.timer", 1, "gap", "")) == "gap"
    assert _event_category(CalendarEvent("u.timer", 1, "projected", "")) == "upcoming"
    assert _event_category(CalendarEvent("u.timer", 1, "approx", "")) == "upcoming"


def test_calendar_click_failure_jumps_to_log_tab(qtbot):
    # Clicking a FAILED event steers the detail pane to the Log tab (the journal
    # shows why it failed) and names the run in the status bar.
    window, _client = make_window(qtbot)
    window.tabs.setCurrentWidget(window.tab_details)  # start on a different tab
    window._on_calendar_event_activated("backup.timer", "failure", _usec(2026, 6, 20, 6, 0))
    assert window.tabs.currentWidget() is window.tab_log
    assert "Failed run: backup.timer" in window.statusBar().currentMessage()


def test_calendar_click_gap_jumps_to_schedule_tab(qtbot):
    # A GAP (missed run, no journal entry) steers to the Schedule tab and names the
    # missed slot in the status bar instead of opening an empty log.
    window, _client = make_window(qtbot)
    window.tabs.setCurrentWidget(window.tab_log)
    window._on_calendar_event_activated("backup.timer", "gap", _usec(2026, 6, 20, 6, 0))
    assert window.tabs.currentWidget() is window.tab_schedule
    msg = window.statusBar().currentMessage()
    assert "Gap: backup.timer" in msg and "no run found" in msg


def test_calendar_click_healthy_keeps_current_tab(qtbot):
    # A successful run or an upcoming projection must NOT yank the user to another
    # tab — only problems (failure/gap) steer.
    window, _client = make_window(qtbot)
    window.tabs.setCurrentWidget(window.tab_details)
    window._on_calendar_event_activated("backup.timer", "ran", _usec(2026, 6, 20, 6, 0))
    assert window.tabs.currentWidget() is window.tab_details
    window._on_calendar_event_activated("backup.timer", "upcoming", _usec(2026, 6, 20, 6, 0))
    assert window.tabs.currentWidget() is window.tab_details


# -- right-click context menu (v2): Run now / View logs / Open in Timers ------


def test_context_run_starts_the_timers_service(qtbot):
    # "Run now" starts the timer's ACTIVATED SERVICE (not the .timer, which would
    # only re-arm the schedule), through the same action_argv guard as the toolbar.
    window, client = make_window(qtbot)
    window._timers = [TimerRow("backup.timer", "backup.service", 999, 0)]
    window._on_calendar_menu_action("run", "backup.timer")
    runs = [c for c in client.calls if c[0] == "run_action"]
    assert len(runs) == 1
    assert runs[0][2] == "backup.service"  # target = the service, not the timer
    assert "start" in runs[0][1]           # argv carries the start verb


def test_context_run_refused_in_system_scope(qtbot):
    # System scope is read-only by design — the action_argv guard refuses, no
    # run_action fires, and the refusal is surfaced (belt-and-suspenders).
    from taskdeck.systemd_client import SCOPE_SYSTEM

    window, client = make_window(qtbot)
    window._timers = [TimerRow("backup.timer", "backup.service", 999, 0)]
    window.scope = SCOPE_SYSTEM
    window._on_calendar_menu_action("run", "backup.timer")
    assert not [c for c in client.calls if c[0] == "run_action"]
    assert "refused" in window.statusBar().currentMessage()


def test_context_logs_opens_log_tab(qtbot):
    window, _client = make_window(qtbot)
    window.tabs.setCurrentWidget(window.tab_details)
    window._on_calendar_menu_action("logs", "backup.timer")
    assert window.tabs.currentWidget() is window.tab_log


def test_context_open_in_timers_switches_view_and_pends_select(qtbot):
    window, _client = make_window(qtbot)
    window._on_calendar_menu_action("table", "backup.timer")
    assert window.view_box.currentText() == "Timers"
    assert window._pending_table_select == "backup.timer"


def test_context_menu_emits_each_action(qtbot):
    # The menu builder wires each entry to emit menu_action(action, unit); triggering
    # them in add-order yields run / logs / table for the clicked unit.
    window, _client = make_window(qtbot)
    cv = window.calendar_view
    emitted: list = []
    cv.menu_action.connect(lambda a, u: emitted.append((a, u)))
    for act in cv._build_context_menu("backup.timer").actions():
        act.trigger()
    assert emitted == [
        ("run", "backup.timer"),
        ("logs", "backup.timer"),
        ("table", "backup.timer"),
    ]


def test_context_menu_run_greyed_in_system_scope(qtbot):
    # 'Run now' (the first action) is greyed when the host says runs aren't allowed.
    window, _client = make_window(qtbot)
    cv = window.calendar_view
    cv.set_run_enabled(False)
    assert cv._build_context_menu("x.timer").actions()[0].isEnabled() is False
    cv.set_run_enabled(True)
    assert cv._build_context_menu("x.timer").actions()[0].isEnabled() is True
