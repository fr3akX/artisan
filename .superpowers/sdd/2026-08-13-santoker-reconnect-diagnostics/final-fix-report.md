# Final Santoker reconnect diagnostics fix report

**Date:** 2026-08-13
**Design base:** `4f21ccbcb`
**Pre-feature translation serialization:** `7774c934a`
**Environment:** Python 3.12.3 from `src/.venv`; Qt tests used `QT_QPA_PLATFORM=offscreen`. No hardware, BLE scan, serial connection, socket, network, cloud, or account operation was performed.

## Findings and fixes

### Important 1 — active target edits

`Santoker.requestWarmupOn()` now records `_desired_warmup = True` only after readiness/range validation succeeds. A later `setWarmupTarget()` therefore sends the target command while preserving report/request separation and diagnostics desired ON state.

Evidence:

- `test_active_target_edit_sends_only_target_and_preserves_desired_on`
- `test_real_santoker_active_target_edit_sends_only_target_and_keeps_diagnostics_on`

The real Santoker/controller integration test requests ON, accepts a reported ON frame, edits the active target, proves only `0x7F` is sent, and proves diagnostics remain desired ON.

### Important 2 — protocol target quantization

Desired targets are canonicalized to 0.1°C at both controller and Santoker protocol boundaries. Constructor targets, Fahrenheit conversion, controller storage, display conversion, report comparison, active edits, and ON requests now share the exact packet precision. `375°F` stores `190.6°C`, displays from that canonical value, and converges against a `190.6°C` report without another request.

Evidence: `test_fahrenheit_target_is_canonical_and_converges_without_resend`.

### Important 3 — post-CHARGE safety OFF throttle

The controller now tracks the last successful safety-OFF attempt separately with the injected monotonic clock. The first CHARGE OFF remains immediate. A successful request remains pending until an accepted frame reports OFF; stale ON reports retry no faster than one second. A readiness-race failure retains pending state and does not consume retry eligibility.

Evidence:

- `test_charge_safety_off_is_immediate_then_throttled_until_reported_off`
- `test_mark_charge_failed_race_retains_safety_off`
- `test_mark_charge_retries_failed_off_until_success`
- updated serialized ON/CHARGE ordering test advances the deterministic safety clock before a repeat.

### Important 4 — monitoring stop with pending safety OFF

`stop_monitoring()` includes `_safety_off_pending` in its final ready-device OFF decision, then clears session state as before.

Evidence: `test_stop_monitoring_retries_pending_safety_off_after_readiness_recovers`.

### Important 5 — diagnostics history rendering

Normal refresh now uses an end-positioned `QTextCursor` and never reads/replaces the full document. The document maximum block count is set to the session retention limit, so continuous eviction stays bounded and progresses without rebuild. Full replacement is limited to session replacement or a genuine sequence gap larger than retained history.

Evidence:

- `test_refresh_appends_history_incrementally` spies that neither `setPlainText()` nor `toPlainText()` is called on an incremental refresh.
- `test_refresh_progresses_through_eviction_without_rebuilding_history` proves six incremental evictions at retention three make zero `setPlainText()` calls, keep exactly the retained session lines, and use `maximumBlockCount == 3`.
- `test_refresh_rebuilds_retained_history_when_evicted` retains the unrecoverable-gap rebuild contract.

### Important 6 — translation serialization scope

Method:

1. Ran the repository Qt extractor (`src/pylupdate6pro.py`) into working catalogs and copied its output to `/tmp/santoker-ts-extracted`.
2. Restored each of the 32 `artisan_*.ts` catalogs byte-for-byte from `7774c934a` using `git show`.
3. Used a temporary XML merge script to select only messages/locations extracted from `artisanlib/santoker_diagnostics_ui.py`, add those locations to existing messages, and add new extracted unfinished messages without inventing translations.
4. Regenerated all tracked `artisan_*.qm` catalogs with the environment's Qt `lrelease`. Their resulting bytes equal the already tracked generated catalogs, so no QM worktree delta remained.

Audit:

- 32 TS catalogs changed by exactly 180 inserted lines each; zero base lines deleted or reserialized (`5760 insertions`, `0 deletions` against `7774c934a`).
- 38 feature context/source messages were identical across all extracted catalogs.
- Every pre-feature translation and every unrelated location compared semantically equal to `7774c934a`.
- All added locations name only `../artisanlib/santoker_diagnostics_ui.py`.
- `git diff --check 4f21ccbcb --` passed before commit.

### Minor 7 — queued old callback generation gate

Each monitoring start and stop advances `santokerMonitoringGeneration`. Canvas attaches generation-aware queued signals for readiness, state, target, and accepted frames, plus a generation-aware queued callback gate for connection and milestone callbacks. Old-generation queued callbacks are ignored on the main thread and cannot mutate/reconcile a replacement Santoker. Simulator session creation remains unchanged and creates no transport callback.

Evidence: `test_queued_old_generation_callbacks_cannot_mutate_replacement_device` queues stale readiness, state, frame, and generic device callbacks and proves no state/command/callback effect on the replacement.

### Minor 8 — accepted RX description

Accepted raw RX events now retain the exact packet and describe the decoded target byte and integer value, for example `accepted frame target=7F value=1900`. Raw duplicate frames are still recorded separately.

Evidence: updated `test_accepted_frame_is_only_recorded_once_for_unchanged_fields` proves two frame callbacks/raw records while decoded-state recording remains deduplicated.

### Minor 9 — `max_events` actual integer

The constructor rejects `bool` and non-integer values before checking the 1..5000 range.

Evidence: `test_max_events_is_bounded_to_5000` covers `True` and `2.0`.

### Minor 10 — zero discarded marker

The UI now renders the discarded marker only when `discarded_event_count > 0`.

Evidence: `test_dialog_shows_all_sections_and_unknown_fields` asserts the marker is empty at zero; eviction tests assert the positive marker.

## RED evidence

Focused regression tests were added before production changes.

1. Protocol active edit and RX description:
   - Command: `cd src && .venv/bin/pytest test/unitary/artisanlib/test_santoker.py::TestSantokerWarmupProtocol::test_active_target_edit_sends_only_target_and_preserves_desired_on test/unitary/artisanlib/test_santoker.py::TestSantokerWarmupProtocol::test_accepted_frame_is_only_recorded_once_for_unchanged_fields -q`
   - RED: **2 failed**. Active edit made zero calls; accepted description was only `accepted frame`.
2. Quantization, safety throttle, stop, generation:
   - Command: `cd src && QT_QPA_PLATFORM=offscreen .venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup.py -k 'fahrenheit_target_is_canonical or charge_safety_off_is_immediate or stop_monitoring_retries_pending or queued_old_generation' -q`
   - RED: **4 failed**. Stored target was `190.555...`; safety OFF repeated at 0.2s; pending stop sent nothing; generation signals were absent.
3. Incremental/eviction/marker UI:
   - Command: `cd src && QT_QPA_PLATFORM=offscreen .venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py -k 'appends_history_incrementally or progresses_through_eviction or shows_all_sections' -q`
   - RED: **3 failed**. `setPlainText()` was called on every refresh/eviction and the zero marker was visible.
4. `max_events` type:
   - Command: `cd src && .venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics.py::test_max_events_is_bounded_to_5000 -q`
   - RED: **1 failed** because `True` was accepted.

## GREEN and validation

- Same four focused groups after implementation: **2 passed**, **4 passed**, **3 passed**, **1 passed**.
- Complete focused import order 1: **176 passed in 4.38s**.
- Complete focused reverse import order: **176 passed in 4.28s**.
- Smoke: **4 passed in 0.35s**.
- Task 5/6 real-widget UI tests are included in both 176-test runs: diagnostics UI **18 passed** and warm-up UI **9 passed**.
- Ruff over all feature production/tests: passed with no findings.
- Production Pyright: **0 errors, 0 warnings; no information messages**.
- Codespell: passed (`0`).
- Feature pre-commit over non-generated feature files: all applicable hooks passed. TS files were intentionally excluded from the whitespace mutator because exact pre-feature serialization is an explicit acceptance requirement; XML/textual/semantic audits cover them separately.
- Qt extraction completed successfully. Qt `lrelease translations/artisan_*.ts` completed successfully for all catalogs.
- Translation semantic/textual audit: passed as described above.
- `git diff --check 4f21ccbcb --`: passed.
- Full mypy: known repository configuration blocker, unchanged: duplicate `conftest` module mapping at `test/unitary/artisanlib/roastserver/conftest.py` and root `conftest.py`; exit 2 before analysis.
- Complete pytest: **12 failed, 3708 passed, 22 skipped in 103.63s**. No Santoker/diagnostics/reconnect/UI/isolation failures. The exact unchanged residual families are six qcheckcombobox import/mock tests, two roastlog import/mock tests, three roastpath import/mock tests, and `plus/test_sync.py::TestAddSync::test_add_sync_successful`.

## Changed files

Production/tests:

- `src/artisanlib/canvas.py`
- `src/artisanlib/main.py`
- `src/artisanlib/santoker.py`
- `src/artisanlib/santoker_diagnostics.py`
- `src/artisanlib/santoker_diagnostics_ui.py`
- `src/artisanlib/santoker_warmup.py`
- `src/test/unitary/artisanlib/test_santoker.py`
- `src/test/unitary/artisanlib/test_santoker_diagnostics.py`
- `src/test/unitary/artisanlib/test_santoker_diagnostics_ui.py`
- `src/test/unitary/artisanlib/test_santoker_warmup.py`

Generated translation sources:

- all 32 tracked `src/translations/artisan_*.ts` catalogs.

Report:

- `.superpowers/sdd/2026-08-13-santoker-reconnect-diagnostics/final-fix-report.md`

## Residual risks

- Physical Santoker X3 verification remains pending; behavior is covered by static analysis and deterministic mocks only.
- Full mypy remains blocked by the existing duplicate-conftest mapping.
- Full pytest retains the exact 12 unrelated baseline failures listed above.
- No live-device/network/cloud validation was performed.
