# Opt-in Santoker BLE transport tracing (transport slice only)

No UI, ON/OFF wiring, HTTP, TCP/serial, simulator, profile, or other-device capture
is enabled by this slice. Existing no-sink BLE APIs/call shapes are unchanged.
`ble_port.py` gains only an optional constructed-client observer; the new helper
is selected only by an explicit Santoker BLE sink.
The [v1 wire contract](diagnostic-traces-v1.md) remains unchanged.

## Interfaces and later UI obligations

- Create one immutable `SessionHandle` before connecting and pass it as the final
  optional keyword `Santoker(..., connect_using_ble=True, trace_handle=handle)`.
  TCP/serial ignores this argument and emits nothing. The caller must gate out
  simulators and create a **new Santoker instance and handle for every ON**.
  A traced instance cannot be restarted or rebound to a new session.
- Call `santoker.request_trace_close(cleanup_timeout=5.0)` **before** warmup/safety
  OFF writes. This records closing intent and starts the recorder deadline; it
  does not disable writes. Then call existing `santoker.stop()` after safety work.
  Stop disables new writes/reconnections but retains old-session ownership.
- Poll `santoker.trace_cleanup_complete` in memory. It becomes true only after
  reader cancellation settles, all admitted SDK writes settle, unsubscribe and
  disconnect finish, and the SDK client reports disconnected. Never use a legacy
  `on_stop`, `.stop()` return, a canceled caller Future, or a sealed file as this
  barrier. `stop()` without prior `request_trace_close()` starts the default
  recorder deadline, but cannot retroactively mark the beginning of safety work.
- The helper seals the handle at the actual barrier. The recorder independently
  seals incomplete when its deadline expires, even while a synchronous connect,
  subscription, write cancellation, or disconnect remains blocked. A later actual
  barrier never changes that artifact back to complete. Do not call
  `handle.cleanup_finished()` from UI generation invalidation.
- UI-generation filtering must still guard application callbacks. Raw callbacks
  retain their original session/connection identity and are not filtered through
  a new ON generation. UI code must use queued signals, not direct widget access.
  Poll recorder status/reasons and report capture/capacity failures without
  implying hardware cleanup. Keep application/global BLE-loop shutdown ownership
  until these transport barriers settle, or explicitly accept incomplete shutdown.
- Milestones, control-state annotations, parser-result annotations, upload prompts,
  disclosure/authorization and shutdown orchestration remain later work. These
  hooks do not add `.alog` fields or attach to legacy diagnostics generations.

## What is captured

Every notification is admitted as raw RX before parser readiness, stale-connection,
closing, or parser-overflow guards. An old callback retains its old connection ID;
it may still contribute raw RX to its original open trace, but never feeds a new
connection's parser. Post-seal callbacks are refused by the recorder.

A traced `SantokerCube_BLE.send()` records whole-command intent separately from
actual per-chunk `write_gatt_char` attempts/results. Payloads, canonical UART UUID,
connection UUID, operation UUID, index and response flag reflect that actual SDK
call. Outcomes distinguish written/error/timeout/cancelled. Even response=True or
written is **not** a device ACK. No ACK records are generated. A synchronous caller
wait timeout leaves SDK ownership intact; the result is emitted only when that
underlying await settles. One command-wide monotonic deadline begins at synchronous
admission, not at each chunk. It is checked before every SDK attempt, including the
first after queueing; timeout of an in-progress chunk retains its ownership through
actual cancellation settlement. Expired queued commands and remaining chunks cause
no SDK attempt and no fabricated attempt/result records. Intent alone is not proof
that any chunk was attempted. A canceled/expired write stops subsequent chunks.

Subscription success is emitted only after the actual SDK await returns, not the
legacy swallowing wrapper. Unsubscribe failures are recorded; a subsequently
successful disconnect can still establish cleanup. A failed disconnect never sets
the barrier. Each sequential non-write SDK operation has an independently observed
actual task-completion record that cannot be canceled through its awaiting proxy.
Lifecycle cancellation requests SDK cancellation once and drains actual settlement;
repeated cancellation cannot bypass that drain. Final reader/write/disconnect cleanup
is independently shielded/drained too. Only actual lifecycle-task completion releases
the private loop, never cancellation of its concurrent Future. Recorder deadlines
still bound capture while SDK cancellation cleanup remains blocked.

Scan records deliberately do not invent a separate observed connect
start: the existing synchronous scan/connect bridge combines those steps. OFF is
rechecked when it returns; a late client is disconnected without notifications or
on_connect. An optional keyword-only `client_observer` on `BLE.scan_and_connect`
and its async implementation receives the exact constructed client before the
connect await, so timeout/exception paths cannot discard transport ownership.
No-observer calls retain the original positional call shape. Constructed clients
remain owned through connect settlement and actual disconnect, including a late
connect after OFF. The module-wide scan termination Event is not session cancellation.

## Bounds and failure semantics

The opt-in helper reuses the existing private client-loop plus module-wide SDK-loop
model. At most 16 active/closing/failed-cleanup owners are admitted process-wide.
Owners remain strongly retained through actual SDK/task cleanup, independently of
recorder timeouts. Capacity exhaustion refuses another traced transport visibly;
there is no unbounded fallback thread/task queue. A failed disconnect retains its
slot **and the exact unresolved connection/client** (no blind retry or automatic
timeout-based clearance). Those references are cleared only after verified cleanup.
Later UI must surface
this condition; application restart is not proof that physical hardware cleaned up.

Per owner: at most 16 admitted writes of at most 65,536 bytes each. Write-capacity
exhaustion emits incomplete/overflow status and raises `BufferError` without an
SDK attempt; invalid write bounds raise `ValueError`. Parser handoff is limited to 200 pending notifications **and** 256 KiB raw bytes, including
callbacks not yet run on the parser loop. Notifications above 65,536 bytes also
trigger parser overflow. The consumer additionally holds at most one notification
backlog. The traced header search discards noise instead of concatenating it
forever. These are structural bounds, not measured whole-process RSS claims.

Overflow poisons that parser and stops/disconnects the entire traced transport;
it never silently resumes a stream with a missing fragment. Raw RX can still be
captured. A status `queue_overflow` and the new allowlisted
`SessionHandle.mark_incomplete('queue_overflow')` mark known parser loss without
inventing dropped raw-admission counts. `mark_incomplete(reason)` is bounded,
serialized by the recorder admission lock, does no IO, rejects unknown reasons,
and cannot change sealed/failed handles. Its only foundation change is in the
recorder; contract and store are unchanged. Existing untraced parser behavior is
unchanged.

## Qualification boundary

Tests use fake SDK operations and real recorder artifacts validated by the wire
validator: late connect/OFF, stale callbacks, pre-readiness/closing RX, split writes,
exceptions/cancellation/timeouts, failed subscription, blocked cleanup, overflow,
and independently constructed 1/2/3-byte incoming A5/B5 telemetry (rejecting 0/4).
No native BLE device, network, credentials, UI integration, Raspberry Pi load test,
or platform hardware qualification is claimed.
