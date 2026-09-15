# Santoker private trace HTTP primitive

This slice adds synchronous, worker-only transfer primitives. It does **not** wire
capture, Qt prompts, settings, credentials, scheduling, account generations, or
application shutdown. The [wire contract](diagnostic-traces-v1.md) is unchanged.
Use a **separate** `RoastServerClient(origin, captured_credential)` instance, never
the roast/inventory worker's client. No autonomous retry or server DELETE occurs.

## Explicit invocation order

The future coordinator owns the original selected session, authorization,
credential, client, store lifetime, and actual synchronous operation:

1. An explicit Upload/Retry action calls `store.authorize(id, destination)` when
   first authorizing/rebinding, then `ticket = store.prepare(id)`. Retry within
   the same authorization calls `prepare` only: a fresh attempt UUID is persisted,
   but the sealed artifact is never recompressed. Do not run disk/HTTP on the UI,
   BLE, sampling or recorder producer threads.
2. Use the captured dedicated client to call `identity = client.test_connection()`.
   Construct `Destination(captured_origin, str(identity.organization.id),
   str(identity.user.id))` and call `store.begin_upload(ticket, destination)`.
   This preserves the store's explicit authenticated-identity transition.
3. Enter `with store.open_upload(ticket) as source:`. This requires the exact
   active uploading ticket on an open store. Private filesystem primitives reject
   links/reparse points/hardlinks; the **held descriptor** is checked for size and
   validated for gzip format, session, and compressed hash, then rewound. No
   pathname is reopened after validation. No store mutex is held across the yield.
4. Inside that context, call
   `receipt = client.put_diagnostic_trace(ticket, source,
   before_disclosure=commit_selected_generation)`.
   The client independently repeats **fresh** `/api/v1/auth/me` with the same
   immutable client credential and rejects origin/org/user mismatch before PUT.
   This second identity check is intentional: the store preflight is not a cached
   authorization for disclosure. The held stream is revalidated in bounded reads
   under the trace deadline, not copied into RAM. The optional callback takes no
   arguments and must raise to veto; it runs after fresh identity/validation,
   immediately before PUT dispatch. It performs no scheduling itself.
5. Wait for the **actual call to settle**, even if a UI waiter is canceled or the
   client is closed. Exit the stream context only afterward. The HTTP method
   never closes its caller-owned source. While held, `upload_failed`,
   `accept_receipt`, duplicate opens, and `store.close` reject with `session_busy`;
   existing busy-state checks also prevent deletion/reauthorization/reprepare.
6. After context exit, successful return is only a candidate for the caller's
   explicit `store.accept_receipt(ticket, receipt,
   request_origin=captured_origin)`. That transition rechecks the exact ticket,
   persists the receipt **before unlink**, and leaves unlink failures deletion-
   pending. HTTP does not mutate store metadata or delete any local data itself.
7. If preflight/open/HTTP failed, after the actual operation and context have
   settled, explicitly call `store.upload_failed(ticket)`. Retain bytes and pinned
   authorization. A timeout, cancellation, connection error, 409, 410, or malformed
   response is **not proof of remote absence** and never authorizes local deletion.
   Another request requires an explicit Retry. Do not call `upload_failed` after a
   receipt was accepted or deletion became pending; preserve that ledger state.

The receipt parser accepts only HTTP 200/201, exact `application/json`, at most
8 KiB, no duplicate keys/nonfinite numbers, and exactly the contract's eight
fields. It checks canonical non-nil UUIDs, pinned session/org/user/hash/size,
integer (not boolean) size, real microsecond UTC `stored_at`, and status `stored`.
Trace PUT uses only `application/gzip`, canonical identity headers, and lowercase
compressed SHA-256. Trace headers cannot be supplied on arbitrary generic paths.
The existing sanitized session, TLS, no-proxy/cookie/redirect behavior and response
header defenses apply. Requests are exact-length streams with reads at most
64 KiB, no recompression or whole-artifact load, and no transport retry.

## Deadline, cancellation, and remaining integration ownership

One trace call has a finite **120-second** absolute monotonic deadline, including
its fresh identity check, local revalidation, PUT, and receipt. The preceding
explicit store preflight is a separate ordinary client operation. Generic client
operations remain **12 seconds**, with unchanged **4-second connect / 10-second
read** timeouts; the existing roast worker's 15-second shutdown is untouched and
must not be used as a trace lifetime policy. Deadline expiry permanently closes
that client. A later explicit Retry needs another dedicated client from the
explicitly selected credential; it must still match the pinned destination, never
automatically reload/rebind to a replacement account.

Deadline/close use existing client safety and checked bounded body reads. Closing
an adapter cannot forcibly end arbitrary blocked OS/transport/filesystem code;
a logical cancellation must not release the held stream, store process lock, or
attempt ownership while known operation work can still execute. Shutdown must
retain the single shared `TraceStore` (the recorder currently owns its close)
until **all** its users settle. This slice intentionally does not invent a worker
or change recorder shutdown.

The future UI coordinator must atomically validate/commit the selected account
and UI generation in `before_disclosure`, invalidate queued permission on settings
changes, and keep an already committed dispatch bound to its original credential,
origin, ticket and selected session. A later valid receipt can finish only that
original artifact; it must not update a new-account UI or a new ON session. The
callback is a seam, not an implementation of this synchronization. Credentials
must not be persisted in tickets/ledgers, logged, or placed in signals/reprs.

Hostile concurrent mutation by the same OS user remains outside the existing
private filesystem trust boundary. Linux synthetic tests are not native Windows/
macOS qualification, 65-MiB throughput qualification, Raspberry Pi performance,
or evidence from live hardware/services/accounts.
