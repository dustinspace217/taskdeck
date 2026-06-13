# Phase A Review — Task 10: cadence column + show-based Schedule tab

*Agent: code-reviewer (Fable). Reviewed commit 39c145e independently — no other review seen.*

I reviewed the current state of `taskdeck/systemd_client.py`, `taskdeck/models.py`, `taskdeck/main_window.py`, and the four test files plus `tests/fixtures/show_schedules.txt`. Overall the request/response plumbing is carefully done — the findings cluster in the cadence classifier and in comment/discipline residue from the old design.

---

## P1 — should fix

### P1-1. `_classify_calendar` produces confidently wrong buckets for step-value and range time fields — and the timezone-strip heuristic eats them

`taskdeck/models.py`, lines 70–101.

**Problem.** The classifier's own docstring promises "an honest raw string beats a wrong bucket," but several common normalized shapes get a wrong bucket instead of the raw fallback:

- `OnCalendar=00/6:00:00` ("every 6 hours") normalizes to `*-*-* 00/06:00:00`. The timezone-strip heuristic at line 73 — `"/" in parts[-1]` — sees the `/` in the *time field* and strips it as if it were an IANA timezone. The remaining `*-*-*` then takes the default time and classifies as **"daily"**. A 4×/day timer labeled daily.
- `OnCalendar=*:0/15` ("every 15 minutes") normalizes to `*-*-* *:00/15:00` — same `/`-strip path, also lands on **"daily"**. A 96×/day timer labeled daily.
- Even without the tz-strip collision, an hours field like `9..17` or `00/06` falls through every branch (`!= "*"`, no comma) into the date checks and returns "daily". Minute-level granularity is never examined at all, so `minutely` (`*-*-* *:*:00`) classifies as "hourly".

**Why it matters.** The Cadence column is the headline feature of this commit, and step values are one of the most common real-world `OnCalendar` idioms (every-N-hours/minutes maintenance timers). This isn't a degraded label — it's a wrong factual claim about how often the user's job runs, which is exactly the bug class the docstring disclaims.

**Proposed solution.** Two changes: (a) anchor the timezone check so it can't match a time field — strip only if the last token matches a letters-only shape (no digits, no colons); (b) before bucketing, verify the hour and minute subfields are "simple" (a plain number, `*`, or a comma list of plain numbers) and fall back to the raw expression otherwise — honoring the documented contract. The existing `CADENCE_CASES` table should grow rows for `*-*-* 00/06:00:00`, `*-*-* *:00/15:00`, and `*-*-* 09..17:00:00`.

**Related nit (fold in):** the weekday branch (line 79, `parts[0][0].isalpha()`) treats *any* alpha-leading token as a weekday; a membership check against actual weekday names would close it — and `_WEEKDAYS` is sitting right there unused (see P2-1).

### P1-2. One unrecognized trigger line freezes table refresh in BOTH views indefinitely

`taskdeck/systemd_client.py` lines 247–277; `taskdeck/main_window.py` lines 337–347 and 402–406.

**Problem.** `parse_show_schedules` raises `ValueError` on any trigger line `_TRIGGER_RE` doesn't match — fail-loud, per the stated contract. But the raise happens *after* the by-id unit list is popped and *before* `_pending_enrich.discard("schedules")`, so the render barrier never clears that cycle. That's fine once. The trouble is it recurs **every 10-second cycle**: the next refresh re-fetches schedules for the same timer set, hits the same line, raises again. Net effect: a single exotic timer (or a systemd release that reshapes the `TimersCalendar={ … }` text) permanently freezes data updates in both the Timers *and* Services views — lists are fetched and stored but `_maybe_render` is never satisfied. The status-bar error is visible (so it's not a *silent* failure), but the blast radius of one unparseable line is total: stale Next-run times, new units never appearing, the freshness label stuck.

**Context.** This extends a pre-existing pattern — a persistently failing `results` fetch had the same render-blocking shape before Task 10 — but the schedules parser adds a much sharper trigger: it depends on systemd's *human-formatted property text*, which is less stable across versions than the blank-line block structure the results parser depends on.

**Proposed solution.** Degrade per-unit instead of per-batch: on an unrecognized trigger line, record that unit's `ScheduleInfo` as a sentinel (or simply omit it → "—") and surface one status-bar error naming the unit and the raw line — loud, attributed, and the other 30 timers keep rendering. Alternatively, keep the batch raise but have the `schedules` parse-failure path fall back to rendering with `_last_schedules` as-is (stale cadence beats a frozen table). Either preserves fail-loud visibility without the paralysis.

---

## P2 — minor

- **P2-1. Dead code: `_WEEKDAYS`** (`models.py:48`). Defined, never referenced (verified by grep). Either use it for the membership check in P1-1's related nit, or delete it.
- **P2-2. Comment rot: "Five-column model"** (`models.py:235`). `COLUMNS` is now six.
- **P2-3. Comment rot: MainWindow refresh-design docstring** (`main_window.py:53-57`). Describes the pre-Task-10 single-`show` cycle; there are now two batched shows and a render barrier.
- **P2-4. Comment rot: `fetch_calendar`'s expression provenance** (`systemd_client.py:548-550`). "expression comes from the unit file's OnCalendar= line" — that was the old cat-scraping design; since Task 10 the expression is normalized `show` output, so the `--` injection guard is belt-and-suspenders rather than load-bearing. A future reader should know that.
- **P2-5. Unchecked return on `fetch_calendar`** (`main_window.py:596`). The discard is actually benign (a single-flight rejection means a previous selection chained the *same* expression, whose id was just admitted and whose content is identical) — but that three-step argument is exactly what Power-of-Ten rule 7 says must be written down at an intentional ignore.
- **P2-6. Calendar-chain failure overwrites good Schedule-tab content** (`main_window.py:483-489` with the `calendar → tab_schedule` mapping). If the chained `systemd-analyze calendar` call fails, `_on_failed` replaces the *entire* Schedule tab — triggers, cadence, next-elapse, all rendered successfully a moment earlier — with "(fetch failed)". The failure path should replace only the "(calculating…)" placeholder and keep the triggers.
- **P2-7. `_TRIGGER_RE` is a prefix match** (`systemd_client.py:244`). Trailing content after the first trigger would be silently ignored — at odds with the parser's fail-loud charter. Tightening to require the line to end in `}` converts that drift into the loud ValueError the docstring promises.
- **P2-8. `_classify_monotonic` has no docstring** (`models.py:123-128`); also defined *after* its only caller, unlike the ordering convention above it.

---

## Areas checked and found clean

- **Request/response alignment and scope-flip races.** The by-id unit-list registry (`_schedule_units_by_id`) correctly mirrors the results pattern: pop-before-scope-check, single-flight rejection preserving the in-flight request's own entry, and `_on_failed` freeing entries so a future response can't match a dead argv. I walked the flip-mid-enrichment interleavings — the wholesale `_pending_enrich = set()` reset cleans up every stale-barrier path I could construct. No premature mixed-scope render is reachable.
- **`_walk_show_blocks` refactor.** Behavior-preserving extraction: leading/middle/trailing empty blocks, consecutive blank lines, truncated output, and the more-blocks-than-units raise all match the previous inline contract, and the parsing tests pin each case.
- **Schedtab freshness gating.** The placeholder unit name "selected" never escapes; the calendar id is admitted to `_expected_tab_ids` *before* the request, so the chained response can't land under another unit's schedule; selection-change races all resolve correctly. RACE B handling correctly extends to `ok_sched`.
- **Models wiring.** Six-wide Display/Sort tuples are consistent, `_RESULT_COL` is derived not hardcoded, services render cadence "—", absent schedule keys render "—".
- **Tests.** FakeClient signature parity, the both-enrichments render-barrier test, the monotonic-only no-dangling-"(calculating…)" test, schedtab failure coverage, and the real-capture fixture are all solid. The cadence table test is well-chosen against probed normalized shapes — its gap is exactly the step/range shapes in P1-1.

---

**Verdict: FIX-FIRST** — P1-1 ships wrong cadence labels for common every-N-hours/minutes timers (the commit's headline column), and P1-2 lets one unparseable trigger line permanently freeze both views' refresh.
