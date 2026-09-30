# Santoker desktop diagnostic trace runtime

The desktop now enables the reviewed recorder/transport/private-HTTP foundations
for real device **134 + BLE + no simulator**, excluding viewer mode. TCP, serial,
other devices, legacy Santoker Diagnostics, `.alog` schemas, profile uploads and
roast/inventory queues are unchanged. No trace upload is automatic.

## Local lifecycle and UI

`ApplicationWindow.startRoastServer` creates `TraceRuntime` independently of server
configuration and before the Roast Server controller starts. One recorder writer
constructs and recovers the single `TraceStore` under the application data directory
(`santoker-traces`). The separate daemon transfer worker shares that exact store;
there is no second directory lock owner. No producer call reads disk/keyring or
uses HTTP. Capture starts immediately even if store recovery or a previous upload
is blocked; bounded recorder admissions still apply and losses are visible.

ON begins the immutable handle and emits `session_on` **before constructing and
starting a fresh Santoker**. The later nonmodal previous-session listing cannot
delay this. Preheat/reconnect and multiple roasts share one ON/OFF artifact.
Direct START emits its first roast boundary as soon as the session exists, before
BLE callbacks; ON->START emits it before START actions. Duplicate roast start/end
calls are idempotent. The successful marker branches emit charge, dry end, first/
second crack start/end, drop and cool end; undo/no-op branches emit no new marker.
Nothing is serialized into a roast profile.

Metadata is the trusted app version, Qt's numeric OS major/minor/micro components
(or fixed `unknown` where unavailable), allowlisted build architecture, sampling
interval and C/F unit. It does not use hostname, MAC, OS description, account,
settings dump or a user-entered path. Invalid configuration reports fixed capture
failure without preventing legacy device control.

Reader-local parser callbacks retain immutable original connection IDs, emitting
only contract enums. Protocol readiness belongs to the immutable session. Control
records represent proposed protocol controls, not success; warm-up targets are
converted from protocol tenths-Celsius to the manifest's unit. Raw RX boundaries
and actual TX chunks/results remain authoritative. No `device_ack` is inferred.

Help -> Debug -> **Upload diagnostic log…** is separate from roast upload. Both
this action and the previous-session prompt list retained IDs in pages of 50,
with **Upload and delete** / **Just delete**. Selected IDs are immutable for the
accepted operation. Closing the nonmodal window retains logs. Upload failure
retains logs for another explicit action; startup, ON, roast save/upload, account
changes and refresh never enqueue a transfer. Older cleanup completion refreshes
availability without prompting for the newly captured session. Current and
actual-cleanup-pending/finalizing sessions cannot be uploaded/deleted.

Fixed translated notices report capture/storage/queue capacity failure, operation
failure and incomplete shutdown. No exception text or credentials enter them.
The presentation only polls bounded in-memory snapshots with its Qt timer; worker
threads do not touch widgets or enqueue packet/status signal floods.

## Manual authorization and lifetime

`settings_changed` invalidates permission; only the controller's current verified
`identityChanged` signal can grant it. Persisted settings identity never does.
Signals are connected before controller startup. One outstanding job (at most 50
selected IDs) is admitted **before** any worker queueing, and refresh is coalesced.

For each explicitly selected upload the worker checks the pinned generation,
loads credentials off-thread, constructs a separate immutable credential client,
authorizes/prepares the original ticket, checks auth/me, calls `begin_upload`, and
enters `open_upload`. HTTP repeats fresh auth/me using that same client. Its
`before_disclosure` callback atomically commits the original generation permit.
Changes before this commit veto disclosure; changes afterward cannot retarget the
request. A matching late receipt updates only the original session/ticket and is
persisted before local deletion. Stale-generation success/failure notices are not
shown as new-account results. Unlink failure preserves receipt/deletion-pending
state, never automatically reuploads.

The held descriptor, client, ticket, shared store and worker stay owned until the
**actual synchronous HTTP call settles**, not a logical deadline/cancellation.
Only after leaving the descriptor context may failure/receipt transitions occur.
Shutdown cannot close the recorder (which owns store close) while a transfer runs.

## OFF and bounded application shutdown

OFF order is roast boundary, `request_trace_close`, warm-up safety OFF writes,
`stop`, retirement of that exact Santoker. Repeated OFF is idempotent. Runtime
retains old owners until `trace_cleanup_complete`, independent of a five-second
incomplete seal; settled historical handles are reaped. Transport retains its
reviewed maximum 16 actual owners and its global BLE SDK loop references.

Once exit is approved, new ON and diagnostic jobs are rejected and undisclosed
upload permission is invalidated. After existing stop/settings work the Qt timer
allows up to **15 seconds** for actual trace cleanup and worker/store settlement.
Window close is ignored while deferred; repeated menu/window quit is idempotent.
Store flush/join remains off the UI thread. Global BLE close requires all actual
traced owners to be settled, never merely an incomplete summary.

At the deadline a fixed translated warning and sanitized log report incomplete
cleanup, then normal QApplication exit proceeds **without explicitly closing a
still-owned BLE loop, descriptor or store**, and without forcibly terminating any
thread. This is an explicitly accepted process-crash boundary, not completed
cleanup: an uncheckpointed tail can be lost, remote upload outcome can remain
unknown, and startup recovery/idempotent explicit retry remain necessary. Daemon
workers cannot make blocked filesystem/OS code safely interruptible.

## Qualification

Deterministic fake BLE/HTTP/keyring and real offscreen Qt tests cover blocked old
upload plus new ON, generation veto before disclosure, late original receipt,
no automatic retry, owner barrier versus timeout, fixed failures, session/roast
boundaries, OFF write ordering, simulator exclusions and deferred repeated quit.
The full desktop suite passes on the Linux host with CI-matched Python 3.14.6:
4,222 passed, 22 expected skips and four dependency deprecation warnings. Scoped
Ruff, mypy and Pyright checks pass. Existing partial-window fixtures now initialize
the new inactive trace fields; the new Qt tests restore the native QtCore module
only within their own monkeypatch scope to isolate pre-existing collection mocks.
Contract/golden and profile fixture files are unchanged. Native Windows/macOS/
Raspberry Pi, live hardware, throughput/control latency and process-crash durability
are **not** qualified by these tests. After repeated background provider failures,
the final runtime review and verification were performed inline, not by an
independent reviewer.
