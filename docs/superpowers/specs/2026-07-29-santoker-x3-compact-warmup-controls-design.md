# Santoker X3 Compact Warm-up Controls Design

**Date:** 2026-07-29
**Status:** Approved in conversation; awaiting written-spec review

## Purpose

Replace the Santoker X3 Master's tall Warm-up event slider and lower custom event button with a compact, first-class control in Artisan's top bar. The new control keeps warm-up visible during machine preparation without consuming graph width.

This design supersedes only the UI and preset-control portions of `2026-07-28-santoker-x3-warmup-design.md`. The existing protocol, target-first sequencing, report reconciliation, header-readiness, CHARGE safety, and raw-command compatibility requirements remain in force.

## Scope

### In scope

- A compact X3-only top-bar Warm-up control.
- A genuinely checkable Warm-up button.
- A numeric, unit-aware target input below the button.
- An explicit preset capability identifying first-class Warm-up support.
- Valid-packet readiness notification for button enablement.
- Removal of the old visible Warm-up slider and lower custom button from the X3 preset.
- Updated state synchronization, lifecycle behavior, tests, documentation, and translations.

### Out of scope

- A generic framework for arbitrary preset-defined top-bar controls.
- Changes to existing Santoker Q/X, R-series, or Cube presets.
- Changes to raw `santoker()` behavior.
- Machine ON (`0x7A`) or other newly discovered X3 controls.
- Live-device response timeouts or acknowledgement tracking.
- Changes to the established `0x7E`/`0x7F` wire protocol.

## User interface

### Placement

The top control bar places Warm-up between ON and START, matching the operating sequence:

```text
RESET | ON | [ WARM-UP ] | START | timer
             [ 190 °C  ]
```

The Warm-up control is a compact vertical pair sized to fit within the height of the existing top-bar buttons:

1. a checkable `QPushButton` labeled `WARM-UP`; and
2. a `QSpinBox` directly below it.

The pair is visible only when the active settings declare Santoker Warm-up capability. It is hidden in ArtisanViewer and for all other machine configurations.

### Toggle button

The Warm-up button is a real checkable button rather than a custom event button whose state is inferred from event-action variables.

- Unchecked means Warm-up is inactive or unknown.
- Checked means the optimistic or latest reported machine state is ON.
- Incoming `0x7E` reports remain authoritative.
- A rejected command restores the previous safe state.
- Disconnect unchecks and disables the button.
- A complete accepted Santoker packet enables the button before CHARGE.
- CHARGE unchecks and disables the button until RESET.
- RESET re-enables the button only when the Santoker connection remains protocol-ready.

The button uses Artisan's established button styling and clearly distinct checked/unchecked states. It does not use or generate roast event data.

### Numeric target input

The numeric input represents the controller's desired Celsius target in Artisan's current display unit.

| Display mode | Range | Default |
|---|---:|---:|
| Celsius | 100–300°C | 190°C |
| Fahrenheit | 212–572°F | 374°F |

The spin box:

- shows a localized unit suffix;
- uses whole-degree values;
- remains editable before ON, while active, after CHARGE, and while disconnected;
- sets `keyboardTracking` false so partially typed values are not transmitted;
- commits typed values on Enter or focus loss;
- applies arrow-button steps immediately;
- caches changes locally while Warm-up is inactive;
- sends `0x7F` immediately when Warm-up is active and ready;
- preserves the underlying Celsius target when display units change; and
- accepts valid machine `0x7F` reports as authoritative without retransmitting them.

Programmatic target updates block widget signals.

## Configuration and preset

Add an explicit device setting:

```ini
[Device]
santokerWarmup=true
```

The setting defaults to false, is loaded and saved with device settings, and is true only in `X3_Master_Bluetooth.aset`. Application logic uses this capability—not the presence of hidden sliders or custom commands—to display and govern the compact control.

Update the X3 preset to:

- remove slider 3's Warm-up visibility, temperature flag, command, and 100–300 limits;
- remove the ninth custom WARM-UP button and restore all parallel custom-button arrays to eight entries;
- retain airflow on slider 1, hidden drum on slider 2, and fire on slider 4;
- retain the two extra Santoker telemetry devices and their matching communication placeholders;
- retain all roast-event mappings; and
- retain `santokerWarmup(0);santoker(80,1)` for CHARGE so OFF precedes the machine CHARGE command.

The semantic commands `santokerWarmupTemp(...)` and `santokerWarmup(...)` remain available for advanced command use. The top control calls the typed controller API directly rather than routing through command strings.

## Components and responsibilities

### `artisanlib.santoker.Santoker`

The protocol layer keeps its existing warm-up target/state methods and report callbacks. Add a readiness callback that fires once when a complete accepted frame first establishes the outbound header. Malformed, incomplete, invalid-tail, or rejected-CRC packets cannot trigger readiness.

Disconnect resets readiness and invokes the existing disconnect/UI path so the button becomes inactive. The desired target remains cached.

### `artisanlib.santoker_warmup.SantokerWarmupController`

The Qt-independent controller remains the source of truth for:

- desired target in Celsius;
- C/F conversion;
- validation;
- pre-CHARGE gating;
- serialized ON/OFF/CHARGE operations; and
- report reconciliation.

No widget references are added to the controller.

### `artisanlib.main.ApplicationWindow`

The main window owns the compact widgets, their placement, translated labels/tooltips, enabled/visible state, and Qt signal boundaries.

It will:

- create the top-bar button/spin-box pair;
- show it when the capability is active;
- connect user edits directly to typed controller methods;
- update widgets from readiness, state, target, disconnect, RESET, and CHARGE events;
- block signals around programmatic changes; and
- keep all widget mutation on the UI thread.

### `artisanlib.canvas.tphasescanvas`

Canvas lifecycle integration continues to own connection construction and roast-state transitions. It supplies the readiness callback and retains serialized CHARGE handling. It does not perform direct widget updates.

## State and data flow

### Before connection

- The compact pair is visible for X3 settings.
- The target is editable and defaults to 190°C.
- The toggle is disabled and unchecked.
- Target changes are cached without transmission.

### After connection but before a valid packet

- The toggle remains disabled.
- The existing waiting-for-data behavior remains available if an advanced semantic command attempts activation.

### After readiness

- The readiness signal enables the toggle if CHARGE has not occurred.
- Clicking ON validates the target, sends `0x7F` first, sends `0x7E=1` second, and checks the button optimistically.
- Clicking OFF sends only `0x7E=0` and unchecks the button optimistically.

### Incoming reports

- `0x7E=0/1` updates the check state.
- A post-CHARGE ON report sends OFF again and leaves the button disabled and unchecked.
- A valid `0x7F` updates controller state and the displayed spin-box value.
- Report-driven UI updates never emit duplicate commands.

### CHARGE and RESET

CHARGE remains serialized with semantic Warm-up operations. If Warm-up is active, OFF is sent before the existing `0x80=1` action. The top button becomes unchecked and disabled.

Undoing CHARGE does not restart Warm-up. RESET returns Artisan to the pre-CHARGE lifecycle; the button becomes enabled only if protocol readiness is still true.

### Disconnect

Disconnect disables and unchecks the button. The target remains visible and editable. Reconnection requires a new complete accepted packet before activation is enabled.

## Error handling

Retain translated operator messages for:

- no Santoker connection;
- waiting for valid Santoker data;
- Warm-up unavailable after CHARGE; and
- target outside 100–300°C.

Normal disabled-button states prevent most invalid UI actions. Semantic command users still receive the existing messages. Transport remains asynchronous and fire-and-forget; the UI does not claim device acknowledgement merely because a command was queued.

## Compatibility and upgrade behavior

- Existing Santoker presets remain byte-for-byte unchanged.
- Existing generic and semantic command syntax remains unchanged.
- The capability defaults false, so unrelated machines cannot acquire Warm-up UI or automatic writes.
- Profile and serialized roast data schemas do not change.
- Users upgrading from the current X3 test build must select **Santoker X3 Master BT** once after installation. This reloads the new capability and removes the old slider/custom-button configuration from preserved application settings.

## Testing

### Protocol tests

Add coverage that readiness callbacks:

- do not run for malformed or incomplete packets;
- run after the first complete accepted packet;
- run only once per connection readiness transition; and
- can run again after disconnect and reconnection.

Retain exact `0x7F`-before-`0x7E` and report-decoding tests.

### Controller tests

Retain C/F conversion, inclusive boundaries, pre-/post-CHARGE behavior, serialization, and report reconciliation tests. Add no Qt dependencies.

### Main-window tests

Use real Qt widgets where practical to verify:

- top-bar order is ON, Warm-up pair, START;
- capability-based visibility;
- button disabled before readiness and after CHARGE;
- readiness and RESET enablement;
- real checkable behavior and rejected-state restoration;
- disconnect state;
- typed edits commit only on Enter/focus loss;
- arrows apply immediately;
- inactive changes cache and active changes transmit;
- C/F range, suffix, default, and runtime conversion;
- report-driven button and spin-box updates block outgoing signals; and
- CHARGE OFF ordering remains intact.

### Preset contract tests

Verify:

- `santokerWarmup=true`;
- no Warm-up event slider command or visible slider 3;
- no lower custom WARM-UP button;
- all parallel slider and button arrays have valid lengths;
- airflow, drum, fire, telemetry, and roast-event mappings remain intact;
- extra communication placeholders match extra device count;
- CHARGE remains OFF then `0x80=1`; and
- no `0x7A` command appears.

### Regression and static validation

Run focused Santoker tests in both import orders, main smoke tests, Ruff, mypy, pyright, codespell, generated translation tooling, feature-scoped pre-commit, and the full pytest suite compared with the documented unrelated baseline failures. No test connects to hardware or a network endpoint.

## Acceptance criteria

1. X3 settings show a compact Warm-up toggle above a numeric target field between ON and START.
2. Other machine settings do not show the control.
3. The target field is unit-aware, defaults to 190°C, and follows the agreed commit behavior.
4. The button is disabled until protocol readiness and after CHARGE until RESET.
5. The button's checked state reconciles with valid machine reports.
6. Target edits and reports do not produce command loops.
7. Target-first activation, OFF-before-CHARGE, pre-CHARGE gating, and no-`0x7A` behavior remain intact.
8. The old visible slider and lower custom button are absent from the X3 preset.
9. Existing presets, raw commands, and serialized profile behavior remain compatible.
10. Focused tests and static checks pass without hardware; full-suite results introduce no failures beyond the documented baseline.
