# Santoker X3 Master Protocol Research

## Scope and verification status

This document records commands found by static analysis of the official Santoker Android app (`com.santoker.roastassistant`, version 26.7.7), with emphasis on the X3 Master.

No public Santoker protocol specification was found. The findings below have not been confirmed with a live Bluetooth capture or physical X3. Confidence labels mean:

- **High:** the app action can be traced directly to a packet target and encoding.
- **Medium:** the packet is clear, but its name or applicability is inferred from adjacent app state or UI resources.
- **Low:** an internal packet was found, but its full purpose is unknown.

Do not test commands that energize or reconfigure a roaster without suitable equipment, supervision, and safety precautions.

## Packet format

Most commands use this frame:

```text
EE <A5-or-B5> <target> 02 04 03 <value:3-byte-big-endian>
<Modbus-CRC-low-high> FF FC FF FF
```

The CRC covers:

```text
02 04 03 <value:3-byte-big-endian>
```

It does not cover the leading frame header or target byte.

The app selects the second header byte from the machine type:

- `A5`: Master-style protocol
- `B5`: non-Master-style protocol

The X3 is identified by the app as `X_3_ELECTRIC / "X3 master"`, so the official app appears to use `A5` for it even over Bluetooth.

## Cross-check against Artisan

### Important distinction: transport support versus product support

Artisan already implements the general IO command:

```text
santoker(<target>,<value>)
```

The command parser in `src/artisanlib/main.py` interprets `target` as hex, rounds `value` to an integer, and forwards both to `Santoker.send_msg()`. `src/artisanlib/santoker.py` then creates the same fixed three-byte payload and CRC frame used by the Android app. Consequently, every standard unsigned Android command in this document is already **sendable at the wire level** without adding a new encoder or a method per target.

The remaining gaps are higher-level ones: named constants and state, model-aware validation, UI/preset actions, incoming-state handling, and tests. “Not exposed” below therefore does not mean “impossible to send with `santoker()`.”

### Command coverage matrix

| Android app target | Android meaning | Named in `santoker.py` | Decoded/stored by Artisan | Used by tracked Santoker presets | Generic send works | Gap |
|---:|---|---|---|---|---|---|
| `0x7A` | Machine on/off | No | No | No | Yes | No named state, UI action, or safety/state checks |
| `0x7B` | Heating on/off | No | No | No | Yes | No named state, UI action, or safety/state checks |
| `0x7C` | Cooling on/off | No | No | No | Yes | No named state or UI action |
| `0x7D` | Blend/agitation on/off | No | No | No | Yes | No named state or UI action |
| `0x7E` | Warm-up on/off | `WARMUP` | Yes, state callback | X3 Master Bluetooth | Yes | Physical X3 verification pending |
| `0x7F` | Warm-up target | `WARMUP_TEMP` | Yes, °C × 10 | X3 Master Bluetooth | Yes | Physical X3 verification pending |
| `0x80` | Charge/start | `CHARGE` | Yes, rising-edge callback | Yes | Yes | Implemented; Artisan also transmits event markers rather than only recording them locally |
| `0x81`–`0x83` | Dry, FC, and SC event states | `DRY`, `FCs`, `SCs` | Yes, rising-edge callbacks | Yes | Yes | Artisan presets transmit these targets, although no equivalent outbound builder path was found in the analyzed Android app |
| `0x84` | Drop/end | `DROP` | Yes, rising-edge callback | Yes | Yes | Implemented |
| `0x85`, `0x86` | Minimum/maximum fire | `MIN_POWER`, `MAX_POWER`, marked unsupported | No | `0x85` only in Cube pre-charge actions | Yes | No settings UI, validation, or state reporting |
| `0x87`, `0x88` | BT/ET calibration | `BT_CALIB`, `ET_CALIB`, marked unsupported | No | No | Yes, after signed conversion | No settings UI, signed-value helper, validation, or state reporting |
| `0x8B`, `0x8C` | Minimum/maximum airflow | No | No | No | Yes | No named support, settings UI, validation, or state reporting |
| `0x91`, `0x92` | Out-/in-bean time | No | No | No | Yes | No named support, settings UI, unit handling, or state reporting |
| `0x95` | Q50 mute temperature | No | No | No | Yes | Correctly not exposed as an X3 feature; no Q50-specific workflow either |
| `0x32` | App's “Serial” setting | No | No | No | Yes | Meaning is still unresolved, so implementation would be premature |
| `0x8A`, `0x8F` | Setup/synchronization | No | No | No | Yes | Semantics and required sequence are unresolved |
| `0x8E` | Model/capacity synchronization | No | No | No | Technically, as one 24-bit integer | No model representation or synchronization workflow |
| `0xC0` | Drum speed | `DRUM` | Yes | Configured; hidden in Q/X preset UI | Yes | No X3 firmware capability check; Q/X preset range remains `0–100`, not app's `30–100` |
| `0xCA` | Airflow | `AIR` | Yes | Yes | Yes | Implemented, but no app-equivalent state-dependent limits |
| `0xFA` | Fire/power | `POWER` | Yes | Yes | Yes | Implemented, but no app-equivalent state-dependent limits |

The tracked Santoker presets include `src/includes/Machines/Santoker/Q_+_X_Series_Bluetooth.aset`, `src/includes/Machines/Santoker/Q_+_X_Series_WiFi.aset`, and `src/includes/Machines/Santoker/X3_Master_Bluetooth.aset`. The dedicated X3 preset exposes warm-up through the semantic commands `santokerWarmupTemp({})`, `santokerWarmup(1 - $)`, and `santokerWarmup(0)`, alongside Santoker fire, airflow, drum, and roast-event controls. The older Q/X presets still expose fire and airflow sliders and roast-event commands. Drum is configured as a command and extra device there, but its slider and increment/decrement buttons are hidden. Those presets still contain no machine on/off, heating, cooling, or blend controls.

### Telemetry coverage matrix

| Targets | Artisan status | Gap |
|---|---|---|
| `0xF0`–`0xF6`, `0xF8` | Named and decoded | Board, BT, ET, legacy BT/ET, RoR, and IR are implemented |
| `0xC0`, `0xCA`, `0xFA` | Named, decoded, and available as extra-device channels | Implemented |
| `0x80`–`0x84` | Named and decoded into event callbacks | Implemented |
| `0x7A`–`0x7D` | Frame is accepted, but `register_reading()` ignores the target | Machine, heating, cooling, and blend states are not retained or exposed |
| `0x7E`, `0x7F` | Named, decoded, and reconciled into warm-up state/target | Physical X3 verification pending |
| `0x85`–`0x88`, `0x8B`, `0x8C`, `0x91`, `0x92` | Frame is accepted, but value is ignored | Machine settings cannot be displayed or synchronized |

### Protocol behavior already aligned

- Artisan's frame header, code bytes, fixed three-byte payload, CRC input, and tail match the app findings.
- Artisan supports WiFi, serial, and BLE through the same packet implementation.
- It initially selects `A5` for WiFi and `B5` for BLE, but accepts either header on incoming frames and adopts the observed header for later writes. This accommodates an X3 using `A5` over BLE.
- Existing temperature and RoR scaling agrees with the app analysis.
- Existing fire, airflow, drum, and event target numbers agree with the app analysis.
- Warm-up support now names `0x7E` and `0x7F`, validates the target range, gates semantic warm-up writes on observed header readiness, limits warm-up enablement to pre-`CHARGE`, and reconciles echoed warm-up state/target reports.

### Functional and safety gaps

1. **Physical BLE/header verification is still pending.** Static analysis and tests now cover the warm-up path, but a live X3 capture is still needed to confirm that outbound BLE warm-up writes use the expected `A5` header and match real hardware behavior.
2. **No model/firmware detection.** Artisan adapts the header but does not identify X3, inspect its capability version, or gate features such as drum-speed control on confirmed firmware support.
3. **Setup-target semantics remain unresolved.** The app's `0x8A`, `0x8E`, and `0x8F` synchronization/setup sequence is still not understood well enough to automate safely.
4. **No acknowledgements or report-cadence guarantees.** Warm-up state/target reports are now reconciled when seen, but sending is still fire-and-forget and protocol-level success/failure semantics remain unknown.
5. **Controls outside warm-up scope are still mostly generic.** Machine on/off, heating, cooling, blend, advanced settings, and model-specific limits remain largely reachable only through raw targets or older preset actions.
6. **Raw `santoker()` intentionally bypasses semantic safety.** The new warm-up helpers enforce pre-`CHARGE`, header-readiness, and display-unit conversion rules, but direct `santoker(<target>,<value>)` calls can still skip those safeguards.
7. **Unsigned-only encoder API.** Negative calibration values must be converted by the caller to their 16-bit two's-complement number, such as `-1` to `65535`, to produce low bytes `FF FF`. Passing `-1` directly fails unsigned `to_bytes()` conversion.
8. **Preset/app event difference.** Artisan sends `0x81`–`0x83` for DRY/FC/SC from its presets, while the analyzed app path records those events locally. This should be verified on hardware before assuming both behaviors are interchangeable.

## User-facing outbound commands

### Operating mode commands

These are direct mappings from the app's `ON_OFF`, `HEATING`, `COOLING`, `BLEND`, and `WARM_UP` actions.

| Function | Target | Value | Confidence |
|---|---:|---:|---|
| Machine on | `0x7A` | `1` | High |
| Machine off | `0x7A` | `0` | High |
| Heating on | `0x7B` | `1` | High |
| Heating off | `0x7B` | `0` | High |
| Cooling on | `0x7C` | `1` | High |
| Cooling off | `0x7C` | `0` | High |
| Blend/agitation on | `0x7D` | `1` | High |
| Blend/agitation off | `0x7D` | `0` | High |
| Warm-up on | `0x7E` | `1` | High |
| Warm-up off | `0x7E` | `0` | High |
| Warm-up temperature | `0x7F` | °C × 10 | High |

The official app restricts warm-up temperature to **100–300°C**.

### Manual controls

| Function | Target | Value | X3 constraints observed in app | Confidence |
|---|---:|---:|---|---|
| Drum speed | `0xC0` | Percentage | `30–100`; enabled for compatible X3 firmware | High |
| Airflow/fan | `0xCA` | Percentage | `1–100` | High |
| Fire/heater power | `0xFA` | Percentage | `1–100` | High |

The app enables X3 drum-speed control only when the machine firmware/version bytes meet its compatibility check. One accepted X-series signature is version `3.3.6` or newer.

### Roast start and finish

| Function | Target | Value | Confidence |
|---|---:|---:|---|
| Charge/start roast | `0x80` | `1` | High |
| Drop/end roast | `0x84` | `1` | High |

Targets `0x81`, `0x82`, and `0x83` correspond to yellow/dry, first crack, and second crack state reports in Artisan. No matching outbound packet construction for those three event markers was found in the analyzed app path; the app can mark those events locally.

## Warm-up examples

```text
Warm-up ON:
EE A5 7E 02 04 03 00 00 01 31 BD FF FC FF FF

Warm-up OFF:
EE A5 7E 02 04 03 00 00 00 F0 7D FF FC FF FF

Set warm-up temperature to 200°C:
EE A5 7F 02 04 03 00 07 D0 F3 D1 FF FC FF FF
```

## Artisan semantic and generic-command examples

The dedicated X3 preset uses semantic warm-up helpers:

```text
santokerWarmupTemp(190)
santokerWarmup(1)
santokerWarmup(0)
```

`santokerWarmupTemp(<value>)` interprets `<value>` in Artisan's current temperature-slider unit (`°C` or `°F`) and converts it to the protocol's required `°C × 10` payload. The semantic warm-up commands also enforce the implemented pre-`CHARGE` and header-readiness checks.

Artisan's existing generic encoder still produces the standard three-byte value format. After Artisan has received a valid X3 frame and selected the machine's `A5` header, the raw command mappings are conceptually:

```text
santoker(7a,1)      # machine on
santoker(7a,0)      # machine off
santoker(7b,1)      # heating on
santoker(7b,0)      # heating off
santoker(7c,1)      # cooling on
santoker(7c,0)      # cooling off
santoker(7d,1)      # blend/agitation on
santoker(7d,0)      # blend/agitation off

santoker(7f,2000)   # warm-up target: 200.0°C
santoker(7e,1)      # warm-up on
santoker(7e,0)      # warm-up off

santoker(c0,50)     # drum speed: 50%
santoker(ca,50)     # airflow: 50%
santoker(fa,50)     # fire: 50%

santoker(80,1)      # charge/start roast
santoker(84,1)      # drop/end roast
```

These examples document encoding, not a recommended automatic operating sequence. The official app applies state, model, temperature, and firmware checks before sending many of them, and raw `santoker()` calls bypass the semantic warm-up safeguards above.

## Advanced configuration commands

The official app exposes an advanced machine-settings screen. These targets can alter calibration or machine behavior and should not be sent speculatively.

| Function shown by app | Target | Encoding | Confidence |
|---|---:|---|---|
| Minimum fire | `0x85` | Unsigned 16-bit value in the low two bytes | High |
| Maximum fire | `0x86` | Unsigned 16-bit value in the low two bytes | High |
| Bean-temperature calibration | `0x87` | Signed 16-bit value in the low two bytes | High |
| Air-temperature calibration | `0x88` | Signed 16-bit value in the low two bytes | High |
| Minimum airflow | `0x8B` | Unsigned 16-bit value in the low two bytes | High |
| Maximum airflow | `0x8C` | Unsigned 16-bit value in the low two bytes | High |
| Out-bean time | `0x91` | Unsigned 16-bit value in the low two bytes | High |
| In-bean time | `0x92` | Unsigned 16-bit value in the low two bytes | High |
| Mute temperature, Q50-specific | `0x95` | °C × 10 | High |
| “Serial” advanced setting | `0x32` | One-byte enum, accepted values `1–3` | Medium |

Notes:

- The app increments calibration and in/out-bean time settings by one raw unit. The physical unit of the calibration values has not been conclusively established from static analysis.
- `0x95` is explicitly restricted by the app to the Q50 model and a range of `30–50°C`; it is not an X3 command.
- The precise meaning of the app resource named `advanced_setting_serial` and its enum values `1–3` remains unknown.
- Artisan's current generic encoder accepts only unsigned integers. A negative signed calibration offset can still be represented by passing its unsigned 16-bit two's-complement value; for example, pass `65535` for `-1` so the low payload bytes are `FF FF`.

## Internal synchronization and setup packets

The packet builder also emits the following non-user-facing targets:

| Target | Observed payload/use | Confidence |
|---:|---|---|
| `0x8A` | Boolean-style value `1` during setup/synchronization | Low |
| `0x8E` | Machine model/type byte plus a 16-bit nominal capacity | Medium |
| `0x8F` | Boolean-style value `1` during setup/synchronization | Low |

For target `0x8E`, the three data bytes are:

```text
<model/type ASCII byte> <capacity:16-bit-big-endian>
```

The app maps X3 to:

```text
58 00 1E
```

Here `0x58` is ASCII `X`, and the app's X3 capacity value is `30`, apparently representing the X3's nominal 3.0 kg class. This packet appears intended to synchronize or configure the selected machine model. It should not be sent merely to identify a connected machine.

The exact distinction between the `0x8A` and `0x8F` setup packets was not recoverable from the obfuscated app alone.

## Reported state and telemetry targets

These targets are already recognized by Artisan or were corroborated by the app. They are primarily machine-to-app reports, although control targets are also echoed as state.

| Target | Meaning | Encoding |
|---:|---|---|
| `0xF0` | Controller/board temperature | °C × 10 |
| `0xF1` | Bean temperature | °C × 10 |
| `0xF2` | Environmental/air temperature | °C × 10 |
| `0xF3` | Legacy bean temperature | °C × 10 |
| `0xF4` | Legacy environmental temperature | °C × 10 |
| `0xF5` | Bean-temperature rate of rise | 0.1°C/min with Santoker sign nibble |
| `0xF6` | Environmental-temperature rate of rise | 0.1°C/min with Santoker sign nibble |
| `0xF8` | Infrared temperature | °C × 10 |
| `0xFA` | Fire/heater setting | Percentage |
| `0xCA` | Airflow/fan setting | Percentage |
| `0xC0` | Drum speed | Percentage |
| `0x80` | Charge/start state | Boolean |
| `0x81` | Yellow/dry state | Boolean |
| `0x82` | First-crack state | Boolean |
| `0x83` | Second-crack state | Boolean |
| `0x84` | Drop/end state | Boolean |
| `0x7A`–`0x7E` | Operating-mode state echoes | Boolean |
| `0x7F` | Warm-up temperature state | °C × 10 |

## App behavior that is not a distinct protocol command

The app's voice-action enum contains `MUTE`, but no general-purpose mute target was found. For supported models, mute is implemented by changing airflow to model-dependent low values through target `0xCA`; restoring mute similarly restores a model-dependent airflow value. The X3 does not appear to use the Q50-specific mute-temperature target.

The app also applies sequencing and guard conditions around commands. Examples include preventing warm-up after a roast starts, enforcing safe temperature limits, applying minimum airflow during warm-up, and checking model/firmware capabilities before changing drum speed.

## Evidence

- Official Google Play app:
  <https://play.google.com/store/apps/details?id=com.santoker.roastassistant>
- Official iOS app:
  <https://apps.apple.com/us/app/santoker/id6504513978>
- The iOS release history explicitly mentions X3 and “热机” behavior.
- Static Android app analysis identified:
  - X3 as `X_3_ELECTRIC / "X3 master"`
  - actions named `ON_OFF`, `WARM_UP`, `WARM_UP_TEMP`, `HEATING`, `COOLING`, `BLEND`, `FIRE`, `AIRFLOW`, `SPEED`, and `MUTE`
  - direct packet construction for targets `0x7A`–`0x7F`, `0x80`, `0x84`, `0x85`–`0x88`, `0x8A`–`0x8C`, `0x8E`, `0x8F`, `0x91`, `0x92`, `0x95`, `0xC0`, `0xCA`, and `0xFA`
  - app UI resource names for fire/airflow limits, temperature calibration, in/out-bean time, serial, warm-up, heating, cooling, and blending

## Remaining verification work

Before treating this as an implementation specification:

1. Capture official-app traffic from a physical X3 for each command.
2. Confirm that an X3 Master expects `A5` for outbound BLE frames.
3. Verify command acknowledgements and echoed state targets.
4. Confirm whether operating-mode commands require machine-on or another prerequisite state.
5. Determine calibration units and the meaning of serial values `1–3`.
6. Identify the exact roles of setup targets `0x8A` and `0x8F`.
7. Verify the minimum X3 firmware required for target `0xC0` drum-speed control.
