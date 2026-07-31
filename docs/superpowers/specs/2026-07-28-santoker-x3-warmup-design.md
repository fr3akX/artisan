# Santoker X3 Master Warm-up Support Design

**Date:** 2026-07-28  
**Status:** Approved design

## Purpose

Add first-class Santoker X3 Master warm-up control to Artisan while preserving the existing generic `santoker(<target>,<value>)` command and all existing Santoker presets.

The feature provides:

- a dedicated X3 Master Bluetooth machine preset;
- a unit-aware warm-up target slider;
- a stateful warm-up toggle;
- named protocol targets and decoded state;
- validated command sequencing;
- header-readiness and roast-state safety checks;
- optimistic UI updates reconciled with machine reports; and
- deterministic tests that require no roasting hardware.

The protocol behavior is based on static analysis of the official Santoker Android app. Physical X3 verification remains a separate follow-up.

## Confirmed protocol

Warm-up uses the existing Santoker frame encoder:

```text
EE <A5-or-B5> <target> 02 04 03 <value:3-byte-big-endian>
<Modbus-CRC-low-high> FF FC FF FF
```

The relevant targets are:

| Function | Target | Value |
|---|---:|---:|
| Warm-up state | `0x7E` | `0` off, `1` on |
| Warm-up target | `0x7F` | Celsius × 10 |

The accepted target range is 100–300°C.

For the X3 Master, Artisan must use the header selected by a complete valid incoming packet. Static app analysis indicates that this is likely `A5`, including over Bluetooth.

## Scope

### In scope

- Named `WARMUP` and `WARMUP_TEMP` targets in `artisanlib.santoker`.
- Header-readiness state based on a complete accepted packet.
- Warm-up desired/reported state and callbacks.
- Validated semantic commands:
  - `santokerWarmupTemp(<temperature>)`
  - `santokerWarmup(<enabled>)`
- Celsius/Fahrenheit UI conversion.
- Target-first warm-up start sequence.
- Pre-CHARGE enforcement and automatic OFF at CHARGE.
- Optimistic UI state with incoming report reconciliation.
- New `Santoker X3 Master Bluetooth` preset.
- Active protocol, integration, and preset tests.
- Protocol-gap documentation updates where implementation changes status.

### Out of scope

- Automatically sending Machine ON (`0x7A`).
- Heating, cooling, or blend controls (`0x7B`–`0x7D`).
- Machine model synchronization (`0x8E`).
- Firmware discovery or automatic drum-speed capability detection.
- Advanced calibration or limit settings.
- Changes to existing Q + X or R-series presets.
- Response timeouts before live hardware establishes report cadence.
- Claiming physical-device verification.

## Architecture

### Protocol layer: `artisanlib.santoker.Santoker`

`Santoker` owns wire-level behavior and protocol state only. It must not depend on graph units, roast event indexes, buttons, sliders, or Qt widgets.

Add named constants:

```python
WARMUP = b'\x7E'
WARMUP_TEMP = b'\x7F'
MIN_WARMUP_TEMP_C = 100.0
MAX_WARMUP_TEMP_C = 300.0
DEFAULT_WARMUP_TEMP_C = 190.0
```

Add state for:

- whether a complete packet has established the outbound header;
- the desired warm-up target in Celsius;
- the latest reported warm-up target, if any; and
- warm-up state, represented as unknown until a report or optimistic command establishes it.

Expose typed getters for readiness, target, and warm-up state.

Add typed semantic methods with success/failure results:

- update the desired Celsius target;
- start warm-up by sending target first and ON second;
- stop warm-up by sending OFF only; and
- reset connection-derived protocol state on disconnect.

The existing generic `create_msg()`, `send_msg()`, and `santoker()` behavior remains unchanged. Raw commands continue to bypass semantic validation by design.

### Application integration: `main.py` and `canvas.py`

Application integration owns:

- command parsing;
- displayed-unit conversion;
- roast-state checks;
- translated operator messages;
- slider and button synchronization;
- automatic OFF at CHARGE; and
- Qt thread-affinity boundaries.

The semantic command path must be independent of the raw `santoker()` branch. It must not reinterpret `0x7E` or `0x7F` passed through the raw command.

### Preset layer

Create:

```text
src/includes/Machines/Santoker/X3_Master_Bluetooth.aset
```

Derive it from `Q_+_X_Series_Bluetooth.aset`, retaining its existing telemetry, event, fire, airflow, and hidden drum configuration.

Existing presets remain byte-for-byte unchanged unless derived-file tooling requires an intentional metadata update.

## Header readiness

A BLE Santoker currently starts with a `B5` header and adapts to incoming `A5` or `B5`. Warm-up commands must not rely on that initial default.

The parser must:

1. Read a candidate second header byte into a local variable.
2. Parse the target, code header, data length, data, CRC, and tail.
3. Apply Artisan's existing CRC acceptance policy for Santoker compatibility.
4. Only after the complete frame is accepted:
   - assign the corresponding outbound header;
   - mark header readiness true; and
   - register the reading.

A malformed, incomplete, invalid-CRC, or invalid-tail frame must not establish readiness.

Disconnect resets readiness and reported warm-up state. It retains the desired target so reconnecting does not discard the user's selection. No command is queued across a disconnected or unready state.

## State model

The application distinguishes three concepts:

1. **Desired target:** the user's selected value, initially 190°C.
2. **Optimistic warm-up state:** updated immediately after a command is accepted for sending.
3. **Reported state:** values received from targets `0x7E` and `0x7F`.

Reported values are authoritative when present.

### Incoming `0x7E`

Accept only raw values `0` and `1`. A valid value updates warm-up state and invokes the state callback only when the value changed.

Other values are logged and ignored.

### Incoming `0x7F`

Decode as Celsius × 10. Accept only values in 100–300°C. A valid value updates both reported and desired target, then invokes the target callback if the value changed.

Out-of-range values are logged and ignored.

### Callbacks and Qt affinity

`Santoker` callbacks may run on BLE, serial, or network communication threads. They must only emit Qt signals. Slots on the main UI thread update widgets and application state.

Programmatic slider updates block slider signals so a report does not produce a duplicate outgoing command.

## Semantic commands

### `santokerWarmupTemp(<temperature>)`

The argument is in the event temperature slider's current display unit.

The command flow is:

1. Parse the numeric expression using the same bounded expression policy as other IO commands.
2. Convert Fahrenheit to Celsius when Artisan's temperature-slider mode is Fahrenheit.
3. Reject a Celsius result outside 100–300°C.
4. Save the desired Celsius target in application state.
5. Copy the target to the active `Santoker` instance, if connected.
6. If warm-up is active and the header is ready, send `0x7F` immediately.
7. If warm-up is inactive or no connection exists, retain it locally without transmitting.

This permits users to select the target before connecting or before enabling warm-up.

### `santokerWarmup(<enabled>)`

The argument is evaluated as a boolean. The X3 preset invokes it with:

```text
santokerWarmup(1 - $)
```

Turning ON performs:

1. Verify that a Santoker object exists.
2. Verify that one complete packet established header readiness.
3. Verify that CHARGE has not occurred.
4. Validate the desired Celsius target.
5. Send target `0x7F` with rounded Celsius × 10.
6. Send state `0x7E = 1`.
7. Optimistically update the warm-up button to active.

Turning OFF performs:

1. Verify that a Santoker object and established header exist.
2. Send state `0x7E = 0`.
3. Optimistically update the button to inactive.

No `0x7A` Machine ON command is sent in either path.

## Roast lifecycle

Warm-up is available before CHARGE, including when Artisan recording has started but no CHARGE marker exists yet.

A new, successfully accepted CHARGE must:

1. Detect whether X3 semantic warm-up controls are configured and warm-up is locally or reportedly active.
2. Send Warm-up OFF before executing the existing CHARGE button action.
3. Optimistically clear the button state.
4. Continue normal CHARGE handling, including the preset's `0x80 = 1` action.

Undoing CHARGE does not restart warm-up.

After CHARGE:

- Warm-up ON requests are rejected.
- Target changes may update the desired value locally but do not enable warm-up.
- If a later `0x7E = 1` report arrives, Artisan sends OFF again and displays a warning.

The automatic behavior is enabled only when the active settings contain the semantic X3 warm-up controls. It must not send `0x7E` for existing combined Q/X or other Santoker presets.

## UI and preset design

### Slider layout

| Slider | Purpose | Visible | Range |
|---|---|---:|---:|
| 1 | Airflow | Yes | 0–100% |
| 2 | Drum | No | 0–100% |
| 3 | Warm-up target | Yes | 100–300°C |
| 4 | Fire | Yes | 0–100% |

Slider 3 configuration includes:

- event type label `Warm-up`;
- IO command action;
- command `santokerWarmupTemp({})`;
- temperature-slider flag enabled;
- Celsius base limits 100 and 300;
- initial desired target 190°C; and
- visibility in normal preset operating states.

Artisan's existing temperature-slider conversion changes the displayed range to 212–572°F and the initial value to 374°F in Fahrenheit mode. The protocol always receives Celsius × 10.

Application UI initialization locates the slider by its semantic command, not by exposing a slider index to `Santoker`. It moves the slider to the current desired target without firing an action.

### Warm-up button

Add one stateful custom event button:

```text
Label:   WARM-UP
Command: santokerWarmup(1 - $)
```

UI state synchronization locates matching buttons by semantic command. Protocol code remains unaware of button indexes.

A rejected command must restore the inactive state after the custom-button action completes. An incoming report may subsequently set either state.

## Error handling and operator feedback

Use translated Artisan messages for:

- no Santoker connection;
- waiting for valid Santoker roaster data;
- warm-up unavailable after CHARGE;
- target outside 100–300°C; and
- machine reporting warm-up ON after CHARGE.

Protocol-layer invalid reports are logged rather than shown repeatedly to the operator.

Transport writes remain asynchronous and fire-and-forget. Successful enqueueing is not an acknowledgement. No response timeout is introduced in this implementation.

On disconnect:

- readiness resets;
- UI warm-up state becomes unknown/inactive;
- desired target remains visible;
- no OFF write is attempted over the dead connection; and
- the existing disconnect notification remains visible.

## Testing

### Protocol tests

Add active behavioral tests in `src/test/unitary/artisanlib/test_santoker.py` for:

- exact target packet for 190°C;
- exact ON and OFF packets;
- target-first start sequence;
- stop sending only OFF;
- inactive target changes being cached without transmission;
- active target changes sending `0x7F`;
- target range validation;
- rejection before header readiness;
- readiness only after a fully accepted frame;
- `A5` selection from a valid incoming BLE frame;
- `0x7E` decoding and change-only callbacks;
- `0x7F` decoding and change-only callbacks;
- invalid incoming state and temperature values; and
- disconnect state reset while retaining the desired target.

Tests mock writes and communication boundaries. They never connect to BLE, a network endpoint, or hardware.

### Application integration tests

Add focused tests for:

- Celsius input;
- Fahrenheit-to-Celsius conversion;
- pre-CHARGE acceptance;
- post-CHARGE rejection;
- rejected-button state restoration;
- report-driven button updates;
- report-driven slider movement without retransmission;
- OFF preceding the existing CHARGE action;
- repeated OFF after an unsafe post-CHARGE ON report; and
- disconnect UI behavior.

If the main-window command path cannot be tested without excessive Qt setup, extract only conversion and decision logic into small typed pure helpers. Do not replace behavioral coverage with source-string assertions.

### Preset contract test

Parse the X3 preset and verify:

- device `134`;
- BLE enabled;
- fire and airflow mappings retained;
- roast-event mappings retained;
- warm-up slider command, temperature flag, range, initial target, and visibility;
- warm-up button label and semantic command; and
- existing telemetry extra devices retained.

## Validation

Run the narrow checks first from `src/`:

```bash
pytest test/unitary/artisanlib/test_santoker.py
pytest test/unitary/artisanlib/test_santoker_warmup.py
ruff check artisanlib/santoker.py artisanlib/main.py artisanlib/canvas.py \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_warmup.py
```

Then run:

```bash
mypy
pyright
pytest
```

Run `git diff --check` and review all generated or preset changes. If new translated strings require derived outputs, run `./build-derived.sh`, inspect its full diff, and retain only expected generated changes.

A physical X3 test is not required for unit validation. The final implementation notes must explicitly retain the static-analysis caveat and list live BLE capture as follow-up verification.

## Acceptance criteria

The feature is complete when:

1. The new X3 Bluetooth preset exposes a 190°C unit-aware warm-up slider and stateful button.
2. Warm-up ON sends `0x7F` before `0x7E = 1`.
3. Warm-up cannot start before a complete accepted packet or after CHARGE.
4. CHARGE sends warm-up OFF before its existing charge action.
5. Incoming `0x7E` and `0x7F` reports reconcile the UI without command loops.
6. Existing generic Santoker commands and presets retain their behavior.
7. Focused protocol, integration, and preset tests pass without hardware.
8. Documentation clearly distinguishes static protocol confidence from physical verification.
