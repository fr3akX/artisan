# Santoker X3 Warm-up Visibility Design

**Date:** 2026-07-29
**Status:** Approved for implementation

## Purpose

Show the Santoker X3 Warm-up controls only while Artisan is monitoring or recording. The controls currently appear as soon as the X3 preset is loaded, before Artisan has started the device connection. Hiding them in the fully OFF state better matches the operator workflow while preserving immediate access after ON or START.

This design supersedes only the visibility statements in `2026-07-29-santoker-x3-compact-warmup-controls-design.md`. Protocol readiness, target handling, report authority, CHARGE safety, RESET behavior, placement, and all other requirements remain unchanged.

## Visibility rule

The compact Warm-up control is visible when all of these conditions are true:

1. the active settings declare `santokerWarmup=true`;
2. Artisan is not in ArtisanViewer mode; and
3. Artisan is monitoring (`qmc.flagon`) or recording (`qmc.flagstart`).

Visibility must not depend on whether a Santoker communication object exists or whether a valid protocol packet has arrived. ON or START reveals the controls immediately, while readiness continues to govern whether the Warm-up toggle can be activated.

## State behavior

| Artisan state | Warm-up pair | Target input | Toggle |
|---|---|---|---|
| Fully OFF | Hidden | Cached | Hidden |
| ON/START requested, awaiting valid data | Visible | Editable | Disabled and unchecked |
| Monitoring/recording with valid Santoker data | Visible | Editable | Enabled before CHARGE |
| Recording stopped but monitoring remains ON | Visible | Editable | Governed by readiness and CHARGE state |
| Unexpected Santoker transport loss while Artisan remains ON | Visible | Editable | Disabled and unchecked |
| After CHARGE while still ON/recording | Visible | Editable | Disabled and unchecked until successful RESET |

Hiding the control must not reset the controller's desired Celsius target, send a target or state command, or alter the CHARGE latch. The cached target reappears unchanged on the next ON or START.

## Integration

`ApplicationWindow.updateSantokerWarmupControls()` remains the single method that computes and applies Warm-up visibility and widget state. Its visibility predicate adds the monitoring/recording condition to the existing capability and ArtisanViewer checks.

The established ON, START, STOP, and OFF lifecycle paths must refresh Warm-up controls after their state flags change. Existing readiness, report, CHARGE, RESET, settings-load, and disconnect refreshes remain in place. A transport disconnect does not hide the controls if Artisan is still logically ON; it only removes protocol readiness and disables the toggle.

No visibility decision may be based on `self.santoker is not None`, because the communication object can exist outside an active connection. No visibility decision may be based on header readiness, because that would delay display beyond the approved immediate ON/START behavior.

## Compatibility and safety

- Only settings with `santokerWarmup=true` can show the controls.
- Other Santoker presets and unrelated machines remain unchanged.
- The target stays editable whenever the pair is visible, including while awaiting data and after CHARGE.
- Activation still requires a complete valid Santoker packet and remains pre-CHARGE only.
- Target-first ON, OFF-before-CHARGE, report reconciliation, and the prohibition on Machine ON `0x7A` remain unchanged.
- No profile schema, wire protocol, command syntax, preset, or translation changes are required.

## Testing

Focused Qt-independent and main-window tests will verify:

1. X3 controls are hidden while both `flagon` and `flagstart` are false.
2. Monitoring ON makes the controls visible immediately before protocol readiness.
3. Recording START makes the controls visible immediately.
4. The toggle remains disabled until valid Santoker data establishes readiness.
5. Stopping recording keeps the controls visible while monitoring remains ON.
6. Full OFF hides the controls.
7. The desired target survives OFF and reappears unchanged on the next ON/START.
8. A transport disconnect while logically ON leaves the pair visible but disables and unchecks the toggle.
9. Missing capability and ArtisanViewer mode always keep the pair hidden.
10. Existing CHARGE/RESET, C/F conversion, report reconciliation, command-ordering, and non-X3 isolation tests continue to pass.

Validation will include focused Santoker tests in both import orders, main-window and smoke tests, Ruff, mypy, pyright, codespell, feature-scoped pre-commit, and full-suite comparison against the documented unrelated baseline failures. No test connects to hardware or a network endpoint.

## Acceptance criteria

1. The X3 Warm-up pair is absent before ON or START.
2. ON or START reveals it immediately, without waiting for device data.
3. Full OFF hides it again without losing the selected target.
4. Readiness controls toggle enablement, not pair visibility.
5. Unexpected transport loss while Artisan remains ON leaves the pair visible and safe.
6. Existing protocol, CHARGE, RESET, preset, and compatibility behavior is unchanged.
