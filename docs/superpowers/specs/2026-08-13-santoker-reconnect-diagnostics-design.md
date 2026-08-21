# Santoker Reconnect Restoration and Diagnostics Design

**Date:** 2026-08-13
**Status:** Implemented

## Purpose

Preserve a user's Santoker warm-up intent and active-roast heater-power intent across an automatic transport reconnect, restore each intent only in its safe roast phase after valid machine data resumes, and add a read-only diagnostics window that captures enough decoded and wire-level evidence to investigate reconnect behavior.

The feature addresses [fr3akX/artisan issue #6](https://github.com/fr3akX/artisan/issues/6). Static analysis of the official Santoker Roast Assistant Android app v26.7.7 indicates that it keeps desired command state across BLE loss, reconnects and re-subscribes, and then reconciles desired state with reported `0x7E`/`0x7F` state. Physical-device verification remains a follow-up; tests must not access roasting hardware.

## Scope

### In scope

- Preserve desired warm-up ON across automatic BLE, Wi-Fi, or serial reconnects within one monitoring session.
- Keep desired warm-up state and target separate from reported machine state and target.
- Re-send target `0x7F` and warm-up ON `0x7E = 1` after a valid post-reconnect Santoker frame establishes protocol readiness.
- Retry reconciliation at a bounded rate while valid reports contradict desired ON.
- Retain all existing pre-CHARGE warm-up safety behavior and prohibit warm-up restoration after CHARGE.
- Retain the latest valid Artisan-requested fire/heater power (`0xFA`, `0–100`) issued while recording after CHARGE and before DROP, and restore it only within that same active-roast phase following an automatic transport loss.
- Capture active-roast power requests made during the silent-link interval before the transport reports its disconnect, because TX diagnostics record requests rather than confirmed delivery.
- Capture connection events, protocol transitions, decoded state, and raw RX/TX frame hex automatically from monitoring start.
- Retain a bounded history for the current monitoring session, including while the diagnostics window is closed.
- Add a read-only Santoker Diagnostics window under **Config → Device → Santoker**.
- Support **Copy All** and UTF-8 **Save as Text…** export.
- Add deterministic tests without network, BLE, serial, or roasting hardware.

### Out of scope

- Raw or semantic command controls in the diagnostics window.
- Persisting diagnostics across Artisan restarts or across multiple monitoring sessions.
- Changing existing Santoker wire targets or frame encoding.
- Automatically sending Machine ON (`0x7A`).
- Claiming that a queued TX frame was acknowledged or physically applied.
- A generic diagnostics framework for every Artisan device.
- Storing device addresses, network hosts, credentials, account data, or unrelated Artisan state in diagnostics exports.

## Chosen approach

Use a dedicated, Qt-independent Santoker diagnostics session model. Protocol and application code record events at their authoritative sources; the Qt dialog consumes immutable snapshots.

This is preferred over extending Artisan's serial log because that log is global, optional, unstructured, and cannot reliably represent desired-versus-reported state. It is preferred over keeping capture state in the dialog because capture must begin before the dialog opens and continue after it closes.

## State model

The implementation must distinguish these concepts:

1. **Desired enabled intent** — `None`, `True`, or `False`, owned by `SantokerWarmupController`.
2. **Desired target** — the user's Celsius target, owned by `SantokerWarmupController` and copied to the active protocol object for transmission.
3. **Reported enabled state** — the latest valid `0x7E` report, or unknown after transport loss.
4. **Reported target** — the latest valid `0x7F` report, or unknown after transport loss.
5. **Transport/protocol state** — monitoring, connected, protocol-ready, selected transport, and active header.
6. **Restoration state** — idle, waiting for valid data, pending convergence, converged, or blocked by CHARGE.

A transport disconnect clears connection-derived readiness and reported freshness only. It must not clear desired ON or the desired target during an automatic reconnect.

A reported `0x7F` value no longer overwrites the user's desired target. It updates only reported target state. This separation prevents a stale post-reconnect report from replacing the target Artisan is meant to restore.

The top-bar Warm-up control represents the safe desired intent while reconnect is in progress. If desired ON is awaiting reconnect, the button remains checked but disabled until protocol readiness returns. Reported state and restoration progress remain explicit in diagnostics. CHARGE always clears the desired ON intent and the button.

## Warm-up lifecycle and reconciliation

### Explicit user requests

A successfully accepted ON request:

1. verifies an active Santoker device and complete-frame protocol readiness;
2. verifies that CHARGE has not occurred;
3. validates the desired target;
4. records desired enabled intent as ON;
5. sends target `0x7F` first; and
6. sends warm-up `0x7E = 1` second.

An explicit OFF request records desired enabled intent as OFF before inspecting transport readiness. Therefore an OFF request always cancels pending ON restoration, including while disconnected or racing with transport loss. If the device is protocol-ready, Artisan sends `0x7E = 0`; otherwise it performs the safe local cancellation without a write. This cancellation returns success because the requested desired state was achieved locally, so command and UI callers uncheck the control without a misleading connection error.

### Automatic transport loss

On an automatic disconnect:

- protocol readiness becomes false;
- reported warm-up state and target become unknown;
- desired enabled intent and desired target remain unchanged;
- restoration becomes `waiting for valid data` when desired intent is ON;
- no command is sent over the unavailable transport; and
- diagnostics record the disconnect and state transition.

Stopping monitoring is not an automatic reconnect. An explicit monitoring stop clears pending desired ON so a future monitoring session cannot unexpectedly start heating. The completed diagnostics session remains available until the next monitoring start.

### Restoration trigger

A connection callback alone is insufficient. Restoration begins only after a complete accepted Santoker frame has:

- established an accepted `A5` or `B5` header;
- passed the existing Santoker CRC compatibility check;
- passed code-header, length, and tail checks; and
- set protocol readiness true.

If desired intent is ON and CHARGE has not occurred, Artisan sends desired target followed by ON. This first post-reconnect attempt is immediate once the valid frame is accepted.

### Convergence and retries

Valid `0x7E` and `0x7F` reports update reported state independently. While desired intent remains ON and restoration has not converged, each complete accepted frame invokes reconciliation. Reconciliation re-sends desired target then ON when either reported value is unknown or differs from the desired value, subject to the rate limit below.

Reconciliation attempts are rate-limited to at most one attempt per second, measured with a monotonic clock. Repeated readiness callbacks, high-rate telemetry, and duplicate reports are idempotent and cannot create a tight write loop. A transport-loss transition clears the previous-attempt deadline so the first valid post-reconnect frame can trigger an immediate attempt. If accepted frames stop arriving, Artisan does not run a blind retry timer; it waits for the next valid frame.

Restoration is converged only when valid reports show both desired target and ON. Because writes are asynchronous and fire-and-forget, diagnostics describe writes as requested or queued, never acknowledged.

### CHARGE safety

CHARGE remains serialized with warm-up operations. On CHARGE:

- desired ON is cleared;
- restoration becomes blocked;
- Warm-up OFF is sent before the existing CHARGE action when the transport is ready;
- the top-bar control is unchecked and disabled; and
- any later reported ON causes the existing safety OFF behavior.

Undoing CHARGE or RESET does not resurrect the previous ON intent. A new explicit ON request is required.

## Active-roast heater-power lifecycle

Use a separate Qt-independent `SantokerPowerController`; heater recovery must not weaken or overload the pre-CHARGE warm-up safety model.

Every valid Artisan-requested `0xFA` value from `0` through `100` issued while Artisan is recording after CHARGE and before DROP becomes the desired heater-power intent for the current roast. The request is retained before the transport write, including requests made after RX traffic has silently stopped but before Bleak reports the disconnect. Requests made outside recording, pre-CHARGE, post-DROP, and raw out-of-range requests retain their existing generic-command behavior but are not eligible for automatic restoration.

An automatic transport loss marks restoration pending only when a desired power value exists. A transport-connected callback does not send power. The first complete accepted post-reconnect Santoker frame triggers an unconditional replay of the desired `0xFA` value only when CHARGE is set and DROP is not set. The first replay does not trust `Santoker.getPower()`, because that value can still be the stale pre-loss report.

After the first replay, subsequent accepted frames compare reported power with desired power. A matching report converges restoration; a contradictory or unknown report permits another request no more than once per second. There is no blind timer retry without accepted machine data.

A valid post-reconnect frame before CHARGE cancels the pending power replay without sending. DROP immediately clears desired and pending power state, including across DROP undo. Successful roast RESET, monitoring stop, and the next monitoring start also clear both. A power request made while protocol readiness is already false marks delivery pending directly, covering disconnects that cannot produce another ready-true to ready-false transition. Worker-thread Santoker commands capture the monitoring generation when their event-action thread is created; a queued command from an earlier generation is discarded rather than being sent to a replacement session. Fan (`0xCA`), drum (`0xC0`), and heating-mode (`0x7B`) restoration remain out of scope; live evidence showed normal X3 power output while `0x7B` reported zero, so Artisan must not invent a `0x7B` transition.

## Diagnostics session model

Add the focused, Qt-independent module `artisanlib.santoker_diagnostics`.

### Responsibilities

- Own one monitoring session's metadata, current snapshot, event history, and discarded-event count.
- Accept updates safely from BLE, serial, network, and UI threads.
- Return immutable snapshot values for UI rendering and export.
- Format a stable, human-readable text report.
- Contain recorder failures so diagnostics can never interrupt communication.

### Thread safety and bounds

Use a lock around mutation and snapshot creation. Store at most **5,000 events** in chronological order. When an append evicts an older event, increment a discarded count.

The dialog and export show this marker format:

```text
[older entries discarded: 137]
```

The marker is metadata, not an event occupying the bounded buffer. Closing or reopening the dialog does not reset the model.

### Session lifetime

A fresh diagnostics session is created immediately when monitoring starts with primary device `134`, before the `Santoker` object starts its transport. Capture therefore includes initial connection attempts and packets even if the diagnostics window has never opened.

The session records monitoring stop and then becomes read-only. It remains accessible after stop and after the dialog closes. The next Santoker monitoring start replaces it with a new session. Diagnostics are not written to settings or disk unless the user explicitly chooses **Save as Text…**.

### Event representation

Each immutable event includes:

- monotonically increasing sequence number;
- UTC timestamp with millisecond precision;
- category (`session`, `connection`, `protocol`, `state`, `restoration`, `rx`, or `tx`);
- optional direction (`RX` or `TX`);
- concise decoded description; and
- optional packet bytes rendered as spaced hexadecimal.

Raw byte data is retained in the model and formatted only for snapshots/export. Timestamps and monotonic retry timing are injectable or otherwise controllable in unit tests.

### Captured events

Capture these events:

- monitoring start and stop;
- transport connected and disconnected;
- reconnect count changes;
- protocol readiness and selected `A5`/`B5` header;
- complete accepted RX frames;
- rejected frames, with rejection reason and available partial bytes when safely recoverable;
- every requested TX frame before it reaches the transport boundary;
- decoded changes to board, BT, ET, IR, BT RoR, ET RoR, power, fan, drum, roast milestones, warm-up state, and warm-up target;
- desired warm-up state/target changes;
- restoration waiting, attempt, convergence, throttle, cancellation, and CHARGE blocking.

Routine unchanged decoded readings need not add duplicate state-transition events, but their accepted raw RX frames are still captured.

### Snapshot fields

The immutable current snapshot contains:

- session start/end and monitoring-active status;
- configured transport kind: BLE, Wi-Fi, or serial;
- connected state, protocol readiness, active header, reconnect count, and last-packet UTC time;
- board, BT, ET, IR, BT RoR, ET RoR, power, fan, and drum readings;
- desired warm-up state and target;
- reported warm-up state and target;
- restoration state and last-attempt time;
- CHARGE safety latch; and
- retained/discarded event counts.

Unknown values are represented explicitly as `unknown`, not as zero. Existing internal `-1` sentinels must not appear as plausible measurements in the diagnostics UI.

## Protocol instrumentation

`artisanlib.santoker.Santoker` remains responsible for frame parsing and encoding. It receives an optional diagnostics-session dependency; normal behavior remains valid when no recorder is supplied.

### RX

The parser gathers the bytes belonging to the candidate frame while preserving current stream behavior. After parsing:

- an accepted frame is recorded with raw bytes and decoded target/value;
- a validation failure is recorded with its reason and bytes available for that candidate;
- an incomplete read records available partial bytes before the existing reconnect/error path continues; and
- diagnostics exceptions are caught and logged without changing acceptance or reconnect behavior.

Bytes skipped while searching for the first header byte are not retained. A candidate frame starts at the accepted first header byte, preventing arbitrary stream noise from consuming the bounded history.

### TX

`send_msg()` constructs a frame once, records it as a **TX request**, and passes the same bytes to BLE or `AsyncComm`. Recording must not imply that Bleak, the socket, or the serial driver wrote or acknowledged it.

### Connection callbacks

Internal connected/disconnected wrappers update diagnostics before invoking existing external callbacks. Disconnect resets reported protocol state before the UI callback, while retaining desired controller intent outside the protocol object.

## Application integration

### Controller

Extend `SantokerWarmupController` with desired enabled intent and restoration bookkeeping under its existing serialization lock. Keep unit conversion, CHARGE gating, retry timing, and reconciliation Qt-independent.

Expose small typed methods for:

- reading desired enabled intent;
- noting automatic transport loss;
- clearing intent on monitoring stop;
- reconciling after readiness or a valid report;
- reporting restoration state; and
- preserving existing target and CHARGE APIs.

The controller may call only the typed `SantokerWarmupDevice` protocol. It must not import widgets or mutate Qt state.

### Qt thread boundary

Communication callbacks update the locked diagnostics model directly. They emit focused Qt signals for readiness/report transitions that require widget changes or controller reconciliation. Widget mutations and automatic reconciliation calls occur only in main-thread slots. No BLE, network, serial, or sampling worker directly modifies a widget or sends restoration commands.

### Monitoring lifecycle

`canvas.py` creates the session before constructing/starting `Santoker`, passes it to the protocol object, and synchronizes controller/session state. Automatic reconnects reuse the same `Santoker`, controller intent, and diagnostics session. The explicit disconnect/monitoring-stop path clears pending ON only after safety shutdown handling.

## Diagnostics window

### Entry point

Add a translated **Diagnostics…** button inside the Santoker group on **Config → Device**. The button is available whether monitoring is active or stopped, so the last session can be inspected. If no session exists, the window shows `No Santoker monitoring session captured`.

Opening diagnostics does not apply or discard Device Configuration edits and does not start communication.

### Layout

Implement the dialog in the dedicated production module `artisanlib.santoker_diagnostics_ui`; do not add its implementation to `main.py` or `devices.py`. `ApplicationWindow` owns one modeless dialog instance so closing Device Configuration does not close diagnostics. Repeated **Diagnostics…** activation raises the existing window rather than creating duplicates.

The window contains:

1. **Session and Connection** — monitoring, transport, connected state, readiness, header, reconnect count, and last packet.
2. **Machine State** — temperatures, rates, power, fan, and drum.
3. **Warm-up State** — desired/reported state, desired/reported target, restoration status, and CHARGE latch.
4. **History** — a read-only chronological view with timestamp, category/direction, description, and packet hex.
5. **Buttons** — **Copy All**, **Save as Text…**, and **Close**.

There are no clear, retry, reconnect, raw-send, semantic-send, toggle, target-edit, or other machine-control actions.

### Refresh behavior

A GUI-thread `QTimer` refreshes snapshot labels and appends newly sequenced history entries. Reopening the window loads the current retained history and then follows new events. If entries were evicted while the window was closed, it reloads the retained range and updates the discarded marker.

Refreshing must not rebuild all 5,000 rows on every timer tick. Closing the window stops only its timer; session capture continues.

### Export

**Copy All** places the complete formatted report on the clipboard. **Save as Text…** asks for a destination and writes UTF-8 text with normalized `\n` line endings.

The report contains:

- a format heading/version;
- safe session metadata;
- the current snapshot;
- retained events in chronological order; and
- the discarded-entry marker/count.

It excludes credentials, device addresses, configured host names/IPs, file paths, account information, roast profile content, and unrelated logs. File-dialog cancellation is silent. Write errors use Artisan's normal translated error/message boundary and do not discard the in-memory session.

## Error handling

- Diagnostics mutation and formatting failures are logged and contained; they never reject an otherwise valid packet or suppress a command.
- Invalid frames retain the parser's current behavior and additionally produce a bounded diagnostic event when possible.
- A reconnect never restores before valid protocol readiness.
- Duplicate reports and callbacks remain idempotent.
- Retry throttling prevents command floods when the machine repeatedly reports stale state.
- Unknown snapshot values render as unknown rather than stale or zero.
- Export failure does not close the dialog or alter capture.

## Compatibility

- Existing `santoker(<target>,<value>)` raw command semantics remain unchanged.
- Existing warm-up semantic command syntax and packet order remain unchanged.
- Existing Santoker presets and serialized roast/profile schemas remain unchanged.
- Non-Santoker devices do not create diagnostics sessions or incur packet-history storage.
- Wi-Fi, serial, and BLE use the same state model without assuming a particular physical transport.
- The diagnostics dependency is optional at the protocol layer to preserve direct construction in tests and utilities.

## Testing

### Diagnostics model tests

Add focused tests for:

- thread-safe append/snapshot behavior;
- immutable snapshot results;
- 5,000-event retention, exact discarded count, and marker;
- stable sequence and chronological ordering;
- unknown-value rendering;
- text formatting and UTF-8-safe content;
- safe metadata exclusions; and
- formatter/recorder failure containment.

### Protocol tests

Extend `test_santoker.py` for:

- accepted raw RX capture;
- CRC, tail, code-header, invalid-header, and truncated-frame rejection capture where bytes are available;
- TX request capture using exactly the bytes passed to the transport;
- connection/readiness/header transitions;
- decoded state-transition capture without duplicate decoded events;
- reported target no longer overwriting desired target; and
- disconnect clearing reported state while retaining externally owned intent.

### Controller and integration tests

Extend `test_santoker_warmup.py` for:

- desired and reported state separation;
- disconnect preserving desired ON;
- valid post-reconnect readiness sending target before ON;
- no restoration on connection callback alone;
- contradictory valid reports causing rate-limited retries;
- convergence when target and ON reports match;
- explicit OFF cancelling pending restoration, including a concurrent disconnect boundary;
- CHARGE clearing/blocking restoration;
- RESET not resurrecting ON;
- explicit monitoring stop clearing pending ON;
- UI showing desired ON while reconnect is pending; and
- all worker-originated UI changes crossing queued signals;
- silent-link `0xFA` requests retaining only the latest valid value;
- unconditional first active-roast power replay after a valid post-reconnect frame;
- no power replay before CHARGE, after DROP, without transport loss, or after monitoring stop;
- stale matching pre-loss power not suppressing the first replay; and
- reported-power convergence and one-second retry throttling.

### Dialog tests

Use real Qt widgets to verify:

- the Device Configuration Santoker entry point;
- no-session, active-session, and completed-session rendering;
- every history/snapshot field is read-only;
- absence of command/control actions;
- incremental GUI-thread refresh;
- close/reopen retaining one monitoring session;
- next monitoring start replacing the session;
- discarded marker display;
- Copy All output; and
- Save as Text cancellation, success, and contained failure.

All tests mock transport and file-dialog boundaries. They must not scan for Bluetooth devices, open sockets/serial ports, contact a cloud service, or issue commands to hardware.

## Validation

Run narrow checks from `src/` first:

```bash
pytest test/unitary/artisanlib/test_santoker.py
pytest test/unitary/artisanlib/test_santoker_warmup.py
pytest test/unitary/artisanlib/test_santoker_diagnostics.py
pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py
ruff check artisanlib/santoker.py artisanlib/santoker_warmup.py \
  artisanlib/santoker_diagnostics.py artisanlib/santoker_diagnostics_ui.py \
  artisanlib/main.py artisanlib/canvas.py artisanlib/devices.py \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker_diagnostics_ui.py
```

Then run the broad project checks applicable to the final diff:

```bash
mypy
pyright
pytest
```

From the repository root also run `git diff --check` and the relevant pre-commit hooks. If new translated strings require derived outputs, run `src/build-derived.sh`, inspect the complete diff, and retain only expected generated changes. Report any checks blocked by missing Qt platform libraries.

## Acceptance criteria

1. Desired warm-up ON and target survive an automatic transport reconnect within one monitoring session.
2. No restoration command is sent merely because a transport connected.
3. The first complete valid post-reconnect frame can trigger target-then-ON restoration before CHARGE.
4. Repeated contradictory reports retry no more than once per second and converge without a tight loop.
5. Explicit OFF, CHARGE, or monitoring stop cancels pending ON restoration.
6. Restoration never enables warm-up after CHARGE, and existing unsafe reported-ON handling remains intact.
7. Desired and reported warm-up state/target remain separately inspectable.
8. Capture begins at Santoker monitoring start and survives diagnostics close/reopen until the next monitoring session.
9. The 5,000-event buffer reports exactly how many older entries were discarded.
10. The diagnostics window is strictly read-only and is available at **Config → Device → Santoker → Diagnostics…**.
11. Copy and UTF-8 text export include safe snapshot/history data and exclude credentials and identifying connection values.
12. Existing raw commands, presets, profile data, and non-Santoker device behavior remain compatible.
13. Focused and broad automated checks pass without live hardware, subject to clearly reported environment limitations.
14. Release notes retain the caveat that official-app behavior was established by static analysis and needs physical X3 verification.
15. The latest valid requested `0xFA` value is retained even when requested during the silent-link interval before disconnect detection.
16. The first valid post-reconnect frame replays desired heater power only during an active roast (after CHARGE and before DROP), without trusting stale pre-loss power state.
17. Contradictory post-reconnect power reports retry no more than once per second and matching reports converge restoration.
18. DROP, successful roast RESET, monitoring stop, and a new monitoring session clear heater-power restoration intent; fan, drum, and `0x7B` are not restored.
19. Power commands outside recording, pre-CHARGE, or post-DROP remain raw commands but do not become later active-roast restoration intent.
20. An active-roast power request made while protocol readiness is already false becomes pending without requiring another readiness transition.
21. A queued Santoker command from an earlier monitoring generation cannot execute against a replacement session.
