# Phase B — Cross-examination (Task 10 review)

Two reviewers this pass (stabilization-slim roster per workspace rules), so Phase B
is one reply each: every reviewer read the other's complete Phase A findings and
replied from their own lens. Verbatim below.

---

## code-reviewer → on test-analyzer's findings

**P1-A (one-sided render barrier pin).** (a) Agree — verified by trace: with line 386 deleted, `test_render_waits_for_both_enrichments` still passes because results delivered first makes `rowCount()==0` true for the wrong reason (only "schedules" was ever in `_pending_enrich`). (b) Agree with the mirror test. (c) None — clean finding.

**P1-B (schedules wrong-scope guard unpinned).** (a) Agree. (b) Agree. (c) My trace sharpens the stakes: with lines 341-342 deleted, a user-scope schedules response landing after a flip doesn't just pollute `_last_schedules` — if the user-scope results response already discarded "results", the schedules arrival empties `_pending_enrich`, `_maybe_render` fires, and `_data_scope = self.scope` stamps **system** onto rendered **user** rows. That defeats the action-enablement gate (QA synthesis #7), making this guard load-bearing for the read-only contract, not just display hygiene. The mirror test should additionally assert no render occurred.

**P1-C (`/`-repetition misbuckets).** (a) Agree — this is my P1-1; independent convergence. (b) Agree with adding cases; their fix as stated is **insufficient and would regress a passing case if applied naively**. (c) Two corrections from my trace: first, their bucket claim is wrong for `*-*-* *:00/15:00` — it classifies "daily", not "hourly", because the tz-strip heuristic at models.py:73 (`"/" in parts[-1]`) eats the entire time token before the hours check runs. Same mechanism for `00/06`. Second, "detect `/` before bucketing" on the raw expression would false-positive every IANA-timezone expression (`Mon *-*-* 06:00:00 America/Los_Angeles` — a real fixture case currently classifying correctly as "weekly (Mon)") into raw fallback. Correct fix order: anchor the tz-strip to an alphabetic-token shape (no digits/colons), *then* require simple hour/minute subfields, falling back to raw otherwise.

**P1-D (request-id seam uncovered).** (a) Agree — both sides build ids from independent f-strings; drift keeps everything green while production tabs freeze. (b) Agree; the fakebin test pinning `fetch_calendar`'s `--` argv also covers the flag-injection guard I flagged as weakly documented (my P2-4/P2-5). (c) None.

**P1-E (non-timer else branch deletable).** (a) Agree — verified no test selects a service row through `_on_selection`; deleting lines 529-530 strands the Schedule tab at "loading…", the exact frozen-tab failure class QA synthesis #4 exists to prevent. (b) Agree. (c) The test should run in the Services view (`view_box.setCurrentIndex(1)`) so row 0 is genuinely a service.

**P1-F (no live test for parse_show_schedules).** (a) Agree, with a caveat: I couldn't verify a realsystemd suite/marker exists, so the fix presumes infrastructure I haven't seen. (b) Agree in direction. (c) This **strengthens my P1-2**: I found that one unrecognized trigger line freezes table refresh in both views indefinitely (parse raises after the by-id pop but before `_pending_enrich.discard`, every 10s cycle). Their live canary catches format drift pre-ship; my per-unit-degradation fix bounds blast radius when drift ships anyway. Phase C should treat these as complementary halves, not alternatives — the live test alone leaves the production failure mode at "total freeze."

**Cross-check on my findings.** Nothing in their report contradicts mine. Their P1-C converges with my P1-1 (with the trace corrections above). Their P2 on the possibly-unreachable `Mon,Tue,Wed,Thu,Fri` branch pairs with my dead-`_WEEKDAYS` finding — same probe-then-remove disposition; if systemd normalizes to `Mon..Fri`, that comma branch is dead code too. Their dedup-cements-"daily" P2 sits in the same contract zone as my P1-1: both reduce to "the classifier claims more confidence than the expression supports." My comment-rot and unchecked-return findings (P2-2 through P2-8) are orthogonal to their coverage lens and stand unaffected.

**Verdict unchanged: FIX-FIRST** — their mutation-survival evidence (A, B, E) independently corroborates that the commit's new control flow is under-pinned, and P1-C/P1-1 is a wrong-data bug in the headline feature.

---

## test-analyzer → on code-reviewer's findings

### P1-1 (cadence misbuckets + tz-strip eating step values)

**(a) Problem: agree — and their mechanism corrects mine.** I re-walked `models.py:71-76`: for `*-*-* *:00/15:00`, `parts[-1]` is `*:00/15:00`, the `"/" in parts[-1]` check fires, the *time field is stripped as a timezone*, and the remainder `*-*-*` defaults to "daily". My Phase A claimed this expression bucketed "hourly" via the `hours == "*"` branch — wrong; the tz-strip intercepts first and produces "daily", which is worse. Same finding class, more accurate root cause on their side. The range (`9..17`) and minutely fallthroughs also verify.

**(b) Solution: agree.** Letters-only tz token + simple-subfield gate matches the parser's own "honest raw beats wrong bucket" contract. The weekday membership check against `_WEEKDAYS` also resolves their P2-1 dead-code finding — but note weekday tokens arrive as spans/lists (`Mon..Fri`, `Sat,Sun`), so membership must apply per-component, not whole-token.

**(c) Coverage the fix needs:** `CADENCE_CASES` rows for: hour-step (`00/06:00:00`-normalized), minute-step (`*:00/15:00`), range (`9..17:00:00`), minutely (`*-*-* *:*:00`) — each pinning raw-fallback (or whatever label is decided); a row proving tz-strip still fires for `UTC` and IANA but NOT for `/`-bearing time fields; a negative weekday case (alpha-leading non-weekday → raw). My existing tz rows guard the strip's true-positive side; the fix needs the false-positive side pinned.

### P1-2 (one bad trigger line freezes refresh permanently)

**(a) Problem: agree — this extends my report.** I walked the `_on_failed` path (self-heals: transient failure, refetch next cycle) and rated failure-mid-cycle P2, but did not walk the *deterministic parse raise* through the barrier. Verified: the raise at `systemd_client.py:266` escapes after the pop (`main_window.py:338`) and before `discard("schedules")` (`:345`); the next cycle's wholesale rebuild (`:382`) refetches the same poisoned payload and re-raises. `_maybe_render` is never satisfied, both views freeze at last-good indefinitely. Blast radius claim verified. I upgrade my P2 to agreement with their P1.

**(b) Solution: agree with direction, prefer their second option, with a test-contract warning.** Their per-unit-degrade option changes the parser's fail-loud contract, which an existing test deliberately pins (`test_parse_show_schedules_rejects_unrecognized_trigger_shape`) — that pin must be rewritten knowingly, not just deleted, and a loud-error channel must survive. The render-with-stale option keeps the parser strict and all parser tests intact; smaller diff. Either way the fix lives in the window's schedules branch.

**(c) Coverage the fix needs:** (1) window test: malformed schedules payload mid-cycle → barrier releases, table renders (cadence "—" or stale), error posted; (2) recovery test: next cycle with good payload restores cadences; (3) if the parser goes per-unit-tolerant, a test pinning that the *other* units in the batch still parse and the bad unit renders "—". Note this scenario inherently delivers schedules in varied orderings — it would also close my barrier-one-sidedness P1 if written to deliver schedules first.

### My findings vs. their report

- **Strengthened:** my `/`-repetition P1 (corrected mechanism, above); my failure-mid-cycle P2 (superseded by their P1-2).
- **No contradiction:** their "by-id alignment + scope-flip walked clean" verifies the *production* guards are correct — my P1s there are *pin* gaps (guard at `main_window.py:341-342` untested; barrier one-sided). Correct-but-unpinned is exactly what needs tests.
- **Their "FakeClient parity" does not cover my id-seam P1:** signature parity ≠ request-id string parity between the real client and window expectations. That gap stands.
- **P2-6:** agree; the fix's test extends `test_on_failed_*` — calendar failure after a good schedtab fill must preserve triggers. P2-7's tightened regex needs a trailing-junk-raises test plus fixture re-verification.
