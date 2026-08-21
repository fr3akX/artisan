# Santoker Active-Roast Control Recovery Design

## Problem

An ordinary BLE range loss leaves the Santoker drum running at its configured speed. In the ruined-roast incident, however, the drum physically slowed or stopped when telemetry disappeared. The retained diagnostic report last showed power 70, fan 80, and drum 30, with no outgoing drum-zero command. This is consistent with a controller, firmware watchdog, or internal power reset that also interrupts BLE.

PR #7 currently retains and restores heater power after transport loss, but not fan or drum state. Restoring power alone after a controller reset can leave heat active before mechanical controls recover.

## Approved behavior

During an active roast, from successful CHARGE until DROP, Artisan automatically restores the last intended Santoker drum, fan, and power values after a confirmed transport loss and protocol reconnect.

Automatic restoration is restricted to that active-roast window. It is never armed before CHARGE, after DROP, after roast RESET, after monitoring stops, or merely because incoming telemetry differs from Artisan's remembered state.

## Intended-state capture

At successful CHARGE, Artisan snapshots valid current Santoker power, fan, and drum telemetry as the initial intended state. This captures controls configured before CHARGE.

While the roast remains active, outgoing `santoker(...)` commands for power (`FA`), fan (`CA`), or drum (`C0`) replace the corresponding intended value. Commands issued during an RX-silent interval therefore take precedence over the earlier telemetry snapshot.

At confirmed transport loss, valid current telemetry fills any intended control that was unavailable at CHARGE, without replacing a value already established by an outgoing command.

Only integer values from 0 through 100 are retained or replayed.

## Reconnection and ordering

Transport loss marks the retained controls pending. Nothing is sent until a subsequent accepted Santoker frame confirms protocol readiness in the same monitoring generation.

Recovery is staged for safety:

1. Restore drum.
2. Restore fan.
3. Wait for fresh drum and fan telemetry to confirm both intended values.
4. Restore heater power.
5. Keep retrying unresolved controls no more than once per second until fresh telemetry confirms convergence.

If drum or fan state is unavailable, it does not block power restoration. A known non-converged mechanical state does block heater restoration.

The generation guard already used by Santoker event actions remains in force so callbacks from an old monitoring session cannot actuate a replacement connection.

## Lifecycle and safety boundaries

- Successful CHARGE starts the recoverable control snapshot.
- DROP immediately clears intended state and pending recovery.
- Roast RESET, monitoring restart, and monitoring stop clear intended state and pending recovery.
- Undoing CHARGE does not replay controls; the next non-active frame cancels pending recovery.
- Machine ON and warm-up are never included in active-roast control recovery.
- A telemetry mismatch without prior confirmed transport loss never triggers a command, preventing Artisan from fighting intentional local-panel changes.
- The UI reports the reconnection, while application logs record the exact restored targets and values. Existing Santoker diagnostics continue to capture the corresponding TX frames.

## Testing

Focused unit tests cover:

- CHARGE snapshots valid power, fan, and drum values.
- Active-roast outgoing commands supersede the snapshot.
- Transport loss plus reconnect restores drum and fan before power.
- Power remains blocked until known mechanical controls converge.
- Retries are throttled and end only on fresh matching telemetry.
- Ordinary telemetry mismatches without transport loss do not actuate controls.
- Pre-CHARGE, post-DROP, RESET, monitoring-stop, invalid-value, and stale-generation boundaries remain non-actuating.
- Santoker protocol freshness is reset and re-established independently for power, fan, and drum.
