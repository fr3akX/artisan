# Santoker APK-Parity Active-Roast State Recovery Design

## Problem

Artisan's current active-roast reconnect controller restores drum, fan, and heater-power percentages after a confirmed Santoker transport loss. During a live power-cycle test, Artisan reconnected and successfully restored drum and fan, but the temperature slider had no effect and the heater remained off.

The reconnect therefore succeeded and the mechanical recovery gate completed. The missing state is the Santoker operating mode: a controller restart resets Machine ON and Heating ON independently of the heater-power percentage. Replaying `FA=<power>` without re-enabling those modes cannot resume heating.

This design extends, rather than replaces, `2026-08-20-santoker-control-recovery-design.md`. It supersedes that document's rule excluding Machine ON from recovery. Warm-up and roast-event recovery remain excluded.

## Official-app evidence

Static analysis was performed against the current official SANTOKER GO ROASTER Android application, package `com.santoker.roastassistant`, version 26.7.9.

- XAPK SHA-256: `076530cb48698fb2f5b13cf1b738add3a9c45d6b5d35c94a7323dce1fa0f`
- Base APK SHA-256: `99e31074864179fa5d9e244db092dd24f84c44b87d672c0b0fe0a5ac20ba6743`
- Decompiled source: `/tmp/santoker-26.7.9-jadx`

The official application keeps desired command state separately from reported roaster state. Its recurring one-second processing loop reads reports, constructs commands for desired/report mismatches, sends them, and repeats. The relevant command priority in `bm6.Z()` is:

1. Machine ON/OFF (`0x7A`)
2. Heating ON/OFF (`0x7B`)
3. Drum (`0xC0`)
4. Airflow (`0xCA`)
5. Fire/heater power (`0xFA`)

Desired state survives transport reconnection. Reported state is used to confirm convergence, so a missing or failed command is retried rather than treated as restored after one write.

## Approved behavior

During an active Artisan roast, from successful CHARGE until DROP, Artisan treats the intended Santoker physical state as:

- Machine ON (`0x7A=1`)
- Heating ON (`0x7B=1`)
- Last intended drum speed (`0xC0`)
- Last intended airflow (`0xCA`)
- Last intended heater power (`0xFA`)

After a confirmed transport loss and same-generation protocol reconnect, Artisan automatically reconciles those targets in that order. This deliberately means that switching the roaster off and back on during an active Artisan roast reactivates Machine ON and Heating ON. DROP, RESET, monitoring restart, or monitoring stop cancels that intent before any later reconnect.

## Intended-state capture

Successful CHARGE initializes Machine ON and Heating ON intent to `1` and snapshots valid current drum, fan, and power telemetry. Mode intent does not cause commands during ordinary CHARGE; it is retained only for confirmed reconnect recovery.

While the roast remains active, outgoing Artisan commands for `0x7A`, `0x7B`, `0xC0`, `0xCA`, or `0xFA` replace the corresponding intended value. Explicit mode-off commands therefore supersede the CHARGE defaults and must not be changed back to ON by reconnect logic.

Valid mode values are exactly integers `0` and `1`. Valid percentage values are exactly integers `0` through `100`. Boolean objects are not accepted as integers. Invalid or unknown targets are not retained.

At confirmed loss, valid current percentage telemetry may fill an intended percentage target that was unavailable at CHARGE. Machine and Heating intent are never inferred from stale reported values at loss: they retain the successful-CHARGE defaults or later explicit Artisan commands.

## Protocol state

`Santoker` gains independent reported state and freshness for Machine ON and Heating ON:

- `getMachineOn() -> int`
- `isMachineOnFresh() -> bool`
- `setMachineOn(value: int) -> bool`
- `getHeatingOn() -> int`
- `isHeatingOnFresh() -> bool`
- `setHeatingOn(value: int) -> bool`

The getters return `-1` until a valid report has been accepted. Only report values `0` and `1` are accepted for these fields. A successful setter invalidates that target's freshness before writing, so only a subsequent report can confirm the command. Protocol reset clears both freshness flags and reported mode values while preserving controller-owned desired intent.

Diagnostics record decoded Machine ON and Heating ON values and continue recording the corresponding TX frames through the existing generic Santoker diagnostic channel.

## Reconciliation algorithm

Recovery remains armed only by the existing generation-guarded ready-false/disconnect path. Initial protocol unreadiness, a telemetry mismatch by itself, or an outgoing command does not infer transport loss.

After each accepted same-generation frame, the controller performs these steps:

1. Cancel if the roast is not active or DROP has occurred.
2. Wait if protocol readiness has not returned.
3. Remove a pending target only when a setter was previously attempted and a fresh report equals its intended value.
4. Select the first pending target in fixed priority order: Machine, Heating, Drum, Air, Power.
5. Retry that target no more than once per second until confirmed.
6. Advance to the next target only after fresh confirmation of the current target.
7. Finish only when all retained targets have fresh matching post-write reports.

Only one target is attempted per reconciliation call. This mirrors the official application's ordered desired/report reconciliation, prevents a blind five-command burst, and makes the attempted sequence deterministic in diagnostics.

The implementation remains accepted-frame-driven instead of adding a second Qt or worker timer. Santoker telemetry supplies repeated reconciliation opportunities, while the existing per-target monotonic throttle reproduces the official app's maximum one-second retry cadence without introducing another thread-affinity boundary.

## Safety and lifecycle boundaries

- Recovery is automatic only from successful CHARGE through DROP.
- Confirmed transport loss is mandatory before any recovery write.
- Machine and Heating are confirmed before mechanical and power targets.
- Heater power is last, so a controller reset that reports zero power does not receive nonzero power until Machine, Heating, Drum, and Air have confirmed.
- An explicit active-roast `0x7A=0` or `0x7B=0` command replaces the default ON intent and is restored as OFF.
- DROP, roast RESET, monitoring restart, and monitoring stop clear all intended and pending state.
- A stale callback from an earlier monitoring generation cannot send to a replacement connection.
- Warm-up (`0x7E`, `0x7F`), roast events (`0x80`-`0x84`), cooling, blending, calibration, and configuration commands are not restored.
- No test or validation step sends commands to live roasting hardware.

## User-visible behavior and logging

The existing translated `Connected` status is emitted once when the first reconnect-recovery write is attempted. Application logs and Santoker diagnostics show each ordered target/value attempt, including `7A=1` and `7B=1`, so a failed live recovery can distinguish transport, operating-mode, mechanical, and power stages.

No new translated UI strings are required.

## Testing

Focused deterministic tests cover:

- Independent Machine and Heating report parsing, freshness reset, setters, validation, and diagnostics.
- Successful CHARGE defaults Machine and Heating intent to ON without immediately sending either command.
- Exact power-cycle recovery sequence: `7A=1`, `7B=1`, `C0=<drum>`, `CA=<air>`, `FA=<power>`.
- Fresh post-write confirmation is required before advancing each stage.
- Failed writes and missing confirmations retry no more than once per second.
- An explicit active-roast `7A` or `7B` command overrides the CHARGE default, including OFF.
- A heater-power slider change during disconnection replaces the pending `FA` target.
- Initial unreadiness, telemetry mismatch without confirmed loss, pre-CHARGE, post-DROP, RESET, monitoring boundaries, and stale-generation callbacks remain non-actuating.
- Existing warm-up, drum/fan/power recovery, inventory CHARGE, and full-suite behavior remain unchanged.
