# Silent-Failure Audit — Task Deck v1 (QA Phase A, independent)

## Overall assessment

The subprocess layer is the strongest part of this codebase: every request is guaranteed exactly one terminal signal (identity-guarded handlers with a regression test pinning the contract), the watchdog acts as a catch-all so no request can hang forever, failure messages carry systemd's stderr verbatim, spawn failure is handled separately from crash, parsers fail loudly by contract, and detail-tab writes are freshness-gated by exact request id. The hard failure modes were all anticipated.

The weaknesses are concentrated in the *last hop*: between "the failure signal arrived at MainWindow" and "a human actually saw it." The status bar is the sole error channel, and routine refresh traffic overwrites it on a 10-second clock; the parse-error path recreates the exact frozen-"loading…" state the code's own comments promise to avoid; the `(ValueError, KeyError)` catch has a `TypeError`/`OverflowError` gap through which a malformed response leaves stale data rendered under a fresh-looking status line. Nothing is P0, but three findings are P1 because in realistic flows an error reaches the UI and then vanishes before a person could act on it.

## F1 — Routine refresh status overwrites unacknowledged errors (P1)

All errors go to `statusBar().showMessage(...)`, and every successful refresh cycle writes the routine freshness line via the same mechanism/priority every 10s. Action failures are the worst case: a failed action posts `ERROR [action:X]: …` and the next tick (0-10s later) replaces it with `N units · refreshed HH:MM:SS`. The refresh channel succeeding says nothing about whether the action succeeded, yet it erases the only record of the failure. Concurrent failures also shadow each other (second `_on_failed` overwrites the first). For list-fetch errors, overwrite-on-next-success is arguably correct recovery semantics; for action errors it is not.

**Fix (small diff):** move the routine freshness line to a permanent right-aligned widget (`statusBar().addPermanentWidget(QLabel)` — permanent widgets coexist with and are never displaced by `showMessage`), reserving `showMessage` for errors and action transients. An error then survives until the next error or explicit action. **Test gap:** nothing asserts an error remains visible across a subsequent successful refresh.

## F2 — Parse failure leaves detail tabs frozen at "loading…", with retry blocked (P1)

Row selected → tabs set `loading…` → `log:` response exits 0 but `parse_journal` raises → caught in `_on_finished` → status bar only. Result: (1) Log tab shows `loading…` indefinitely; (2) the status error washes out within ≤10s (F1); (3) **retry is blocked** — `_last_detail_unit` is already set, so re-clicking the row is a silent no-op; the user must select a different row and come back.

**Fix:** share the kind→tab map between `_on_failed` and the parse-error handler; on detail-kind parse failure write `(parse failed)\n{exc!r}` into the tab AND reset `_last_detail_unit = None` so re-selection retries.

## F3 — `(ValueError, KeyError)` catch misses TypeError/OverflowError/OSError; escapees leave stale data labeled fresh (P1)

Exceptions outside the catch set propagate to the PySide6 slot boundary, where Qt prints to stderr and *continues* — invisible for a GUI app. Accidental escape routes: valid JSON of the wrong shape (object/scalar instead of array → `TypeError` from `item["unit"]` — the spec's "malformed JSON … surfaced" promise does not cover this shape class); absurd journal timestamps (`datetime.fromtimestamp` raises `OverflowError`/`OSError` on corrupt-but-numeric values — this machine demonstrably has journal corruption). When an escapee fires on a list response: table keeps previous rows AND the status bar keeps the previous successful `refreshed HH:MM:SS` — stale data rendered as fresh, error visible nowhere. The hard-rule violation.

**Fix (two layers):** (1) parsers: `if not isinstance(raw, list): raise ValueError(...)` in both list parsers; render-time `try/except (OverflowError, OSError, ValueError)` around timestamp conversion falling back to the em-dash per entry. (2) Backstop: `sys.excepthook` in app.py posting `UNEXPECTED ERROR: …` so the next unanticipated class is loud.

## F4 — Success path discards stderr entirely (P2; raise to P1 if the `enable` probe confirms)

stderr is read only on non-zero exit. systemd tooling emits meaningful warnings on stderr with exit 0: `journalctl` corruption warnings (partial log rendered with no indication — compounds DEF-T2-01: between the two, NO channel reports a partial log); `systemctl enable` on a unit with no `[Install]` section prints "The unit files have no installation config…" — timer-activated services typically have no `[Install]`. **Needs a 30-second probe** for the exit code on systemd 258; if exit 0, the user clicks Enable, sees "ok", nothing changes, and systemd's explanation was discarded — textbook misleading-success.

**Fix:** read stderr on success; minimal viable: for `action:` kinds show `{request_id} ok — warning: {stderr}` when non-empty.

## F5 — `systemctl cat` failure strands the Schedule tab at "loading…" forever (P2)

The Schedule tab is populated only from the `cat` success branch; `_on_failed`'s tab map routes `cat` failures to the Unit-file tab only. Trigger: a dangling/deleted unit — exactly the broken units a person opens this tool to investigate. **Fix:** in `_on_failed`, when kind == "cat", also write the Schedule tab: `(no schedule — unit file fetch failed)`.

## F6 — Cross-scope race: stale `results:` response parsed against an overwritten unit list (P2)

`_result_units` is a single slot shared across scopes while in-flight requests are keyed per scope. Two scope flips inside ~5s + a slow `show` → stale `results:user` parsed against the system list → ValueError (loud, confusing) or silent misattribution. **Fix:** key the unit list by request id (`_result_units_by_id[request_id]`), popped in the results handler; bounded at 2 entries.

## F7 — Detail tabs never refresh after initial load; several paths render stale tab content as fresh (P2)

(a) Run now → Log tab lies (pre-run entries; re-click is a no-op). (b) Unit vanishes from a refresh → selection clears but `_last_detail_unit`/`_expected_tab_ids` aren't reset; on reappearance + re-click, the dedup skips the refetch — pre-disappearance data presented as current. (c) Scope switch leaves old-scope tab content; a late old-scope tab response can still write tabs (`_expected_tab_ids` not cleared in `set_scope`).

**Fixes:** (a) clear `_last_detail_unit` in the action-success branch before `refresh()`; (b) on `_selected() is None`, clear both fields and stamp the tabs; (c) clear `_expected_tab_ids` in `set_scope`. v2 alternative: timestamp each tab ("as of HH:MM:SS").

## F8 — Scope/view transition window leaves old rows displayed and *interactive* (P2)

After a System→User switch, action buttons re-enable immediately while the table still shows system rows (until `_apply_results` lands). Same-named unit in both scopes → action targets a different unit than the row the user clicked. If the new scope's fetch fails, wrong-scope rows persist indefinitely under the new scope label. **Fix:** `_data_scope` field recording which scope the rendered rows came from; enablement requires `_data_scope == self.scope`.

## F9 — `_on_failed` never clears `_pending`; one failed list blocks rendering the sibling's fresh data for a cycle (P3)

Self-heals on the next 10s refresh; error surfaced meanwhile. **Fix:** accept + document, or discard the failed id and render with data on hand.

## F10 — Bootstrap paths (P3, mostly fine)

`systemctl` missing entirely: handled well (persistent FailedToStart errors, nothing overwrites them). PySide6 missing: raw ImportError traceback — one-line catch in `__main__.py` suggesting the dnf install. No display: Qt's abort message, acceptable. Exit with in-flight processes: Qt teardown warning, harmless — worth one comment so a future session doesn't "fix" it into the DEF-T4-01 segfault zone.

## Deferments register — severity check

- **DEF-T2-01 (LOW)** — undersized *in combination* with F4: together there is NO channel reporting partial logs, on a machine known to have journal corruption. As a pair, MEDIUM; fixing either restores a channel.
- **NOTE-T7(b)** — informational classification covers the happy path only; the failure-path consequence (wrong view's rows for up to a cycle, interactive — F8) is a defect class, not polish. Re-grade the failure-path half LOW-MEDIUM.
- **DEF-A-01 (LOW), DEF-T4-01 (MEDIUM)** — agreed. DEF-T4-01's write-up is exemplary deferment documentation.

## Test gaps tied to findings

No coverage for: error survival across a subsequent successful refresh (F1); tab state after parse failure + blocked-retry dedup (F2); valid-JSON-wrong-shape payloads through `_on_finished` (F3); `cat` failure's effect on the Schedule tab (F5); cross-scope results response against a swapped `_result_units` (F6).
