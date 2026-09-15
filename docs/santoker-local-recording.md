# Santoker local recorder/store foundation

This foundation does not itself connect to BLE, roast controls, Qt, or HTTP.
An optional [transport-only Santoker BLE adapter](santoker-ble-transport-tracing.md)
now accepts its immutable handles; no application ON/OFF or HTTP wiring is enabled.
A separate [private HTTP primitive](santoker-trace-http.md) now supports explicit
caller-owned held-stream transfer; there is still no UI or upload scheduler.
The unchanged
[wire contract](diagnostic-traces-v1.md) remains authoritative. No automatic
upload, publication, retention eviction, credential persistence, or live device
access is implemented.

## Integration interfaces

- `artisanlib.santoker_trace.CaptureConfig`: frozen allowlisted configuration.
  Supply application/OS version constants, not user-entered descriptions.
- `TraceRecorder(root, ...)`: starts one daemon writer. Root setup/recovery happen
  on that thread; constructing the recorder and `start(config)` do no filesystem
  work. A fresh `SessionHandle` is returned immediately, including a truthful
  failed handle if capacity/startup has failed. Retain that exact handle in old
  connection/operation callbacks; never look up a newly current session.
- `handle.emit(kind, scalar_fields, payload=bytes_like) -> bool`: bounded queue
  admission only, **not durability or transport success**. Supply precisely the
  contract's kind-specific scalar fields; the recorder supplies session, sequence,
  admission timestamp, gap count, and base64 payload. Mutable payloads must be
  copied before returning to their producer. Immutable `bytes` are reused;
  base64/JSON/contract validation/compression run off the producer.
- `request_close(cleanup_timeout=5.0)` records closing intent, not transport
  completion. Cleanup traffic remains admissible until `cleanup_finished()`
  asserts the actual transport barrier or `cleanup_timed_out()` seals incomplete.
  Deadlines are checked by the writer and by admission/status/barrier calls, so a
  stalled disk cannot allow a late barrier to relabel an expired session complete.
  No per-session timer thread is used. Post-seal callbacks return false without
  consuming sequence numbers. The caller remains responsible for actually
  cleaning up its transport; recorder sealing never cancels or proves cleanup.
- `handle.mark_incomplete(reason)` records an allowlisted known loss reason under
  the admission lock, without inventing rejected raw-admission counts. It does no
  IO and cannot mutate a sealed/failed session. Parser overflow uses this when raw
  RX was recorded but could not safely reach the parser.
- `handle.status()` and `recorder.failure` are in-memory polling interfaces.
  Failures are sticky, fixed categories, not exception/configuration dumps.
  `stored_events` means journal writes accepted, not individually fsynced events.
  A rejected admission creates a precisely accounted sequence gap; an unknown
  crash tail never creates invented exact lost-event counts.
- `shutdown()` requests incomplete sealing and returns without joining by default.
  `shutdown(wait=True, timeout=...)` belongs on a non-UI thread. A hung filesystem
  cannot be forcibly interrupted safely; the single worker remains bounded.

`TraceStore` is a synchronous worker-only disk interface. Its lifetime process
lock excludes a second store owner; its mutex serializes capture and job metadata
operations. A later bounded upload/manager worker may share the same store
instance via `store_factory` (publish the instance after startup completes).
**Do not construct another store on the same root, call disk APIs from UI/transport
threads, or add per-packet queued UI signals.** Store operations can delay journal
writing, but never hold the recorder's producer-admission lock during disk IO.

## Resource and completion semantics

Defaults: 4,096 queued/in-flight events AND 2 MiB charged admission bytes, at most
16 active/closing/finalizing session states, and one writer thread. Admission
charges 1,024 bytes per item, six times bounded scalar string lengths, plus raw
payload bytes. Python/RSS and the pre-existing parser backlog are **not** claimed
to be bounded by 2 MiB. The currently writing item remains charged until finished.
No futures, per-session task queue, or unbounded rejected-event history exists.
Old completed handles are caller-owned; the recorder drops their registry entries.

Queue overflow permits subsequent admissions but permanently makes the session
incomplete. Body/event/record caps stop capture visibly, account already queued
loss, and reserve the terminal summary. Sealing validates the whole artifact with
unchanged `encode_trace`; upload preparation verifies it with `validate_trace`.
For early rejection without importing contract internals, the writer also validates
each bounded event in an isolated contract envelope. This costs extra compression
CPU and is a deliberate foundation tradeoff, not a throughput qualification.

The default disk cap is 1 GiB including every regular file observed in the root.
Capture cannot consume the 65 MiB artifact-preparation allowance, 1 MiB failure/
metadata allowance, or each active journal's 64 KiB summary reservation. Metadata
is bounded to 16 KiB per atomic replacement. Artifacts use a worst-case compressed
size reservation before preparation; no partial artifact is published. Existing
journals/artifacts/evidence are never evicted to make room. Journals remain beside
sealed gzip files until explicit deletion/verified-receipt deletion. Occupancy can
therefore exceed the capture allowance after preparation while remaining below
the aggregate cap; further capture then fails visibly. Disk exhaustion can also
prevent writing failure metadata; the in-memory failure remains visible.

## Durability, filesystem boundary, recovery

The caller supplies an exact trace root whose parent already exists. All ancestors
are checked before root creation. The store uses the existing Roast Server secure
filesystem primitives: private modes on POSIX, protected current-user native DACLs
on Windows, symlink/reparse checks, process locking, directory synchronization,
and identity-checked unlink. Hardlinked files (including the lock) are rejected
before permission changes. Windows privacy does not depend on chmod emulation.
As with those existing primitives, hostile concurrent mutation by the same OS user
is outside the trust boundary. Native Windows/macOS qualification remains needed.

Journals use unbuffered writes so quota bookkeeping includes outstanding disk
writes. Fsync checkpoints default to once per second; creation/sealing and metadata
transitions synchronize immediately. **A power failure can lose the uncheckpointed
tail**, and a stalled filesystem can extend the wall-clock interval. There is no
per-event durability promise. Short writes, fsync failures, invalid records, and
storage errors fail visibly without propagating into transport controls.

Startup recovery is off the UI thread. It reads bounded whole journal lines,
validates a salvageable prefix, preserves original damaged/partial evidence, and
seals interrupted journals as `incomplete/crash_recovery`. A durable terminal
summary is revalidated against its prefix through a bounded discard sink; its exact
tail gaps, counters, reasons and end timestamp survive. Trailing damage adds
`capture_error` without discarding those known losses. Non-object JSON tail records
stop prefix salvage without disabling healthy sessions. Unknown power-loss tail
counts are not invented. A damaged manifest/ledger is delete-only,
never implicitly upload-authorized. Already published valid immutable artifacts
are reused, not recompressed. Valid interrupted metadata candidates are re-synced
and published; invalid candidates and partial gzip preparation are retained under
fixed evidence names. A second conflicting damaged candidate fails closed rather
than growing an unbounded evidence-name series or overwriting evidence. Root
entries not owned by the selected session are never deleted.

## Explicit upload-job metadata

Worker-side selected-ID sequence:

1. `list_sessions()` / `read_session(id)` read retained IDs/status; the UI must use
   cached worker results, not disk polling on its thread.
2. `authorize(id, Destination(origin, organization_id, uploader_user_id))` is an
   **explicit user action**. It normalizes the origin and creates an authorization
   epoch. A different account/origin, including another user in the same org,
   requires a new explicit authorization; retries never rebind automatically.
3. `prepare(id)` persists `preparing`, verifies the already durable gzip/digest/
   size, and returns a frozen `UploadTicket`. The ticket includes the exact path,
   digest, size, pinned destination, session, authorization epoch and a freshly
   persisted attempt UUID. Retrying preserves artifact bytes and authorization,
   **not ticket equality**: every attempt transition rejects stale callbacks.
4. The future upload worker must pin its immutable credential/client separately,
   verify identity with that same client, then call `begin_upload(ticket,
   authenticated_destination)`. `open_upload(ticket)` holds the validated private
   descriptor without holding the store mutex during network IO. Follow the exact
   [HTTP invocation order](santoker-trace-http.md); exit its context only after the
   actual call settles, before failure/receipt transitions or store close. Send
   contract identity preconditions and exact ticket bytes. Neither this ledger
   nor its ticket contains credentials.
5. `upload_failed(ticket)` retains bytes and pinned authorization. A further
   `prepare` is an explicit Retry action. Startup converts abandoned preparing/
   uploading jobs to authorized but **never starts a request**.
6. `accept_receipt(ticket, receipt, request_origin=actual_pinned_origin)` verifies
   exact receipt fields: UUID resource/session/org/user, digest, integer compressed
   size, stored status, and real contract UTC timestamp. Origin and authorization
   epoch and attempt UUID must match the job. It atomically persists the valid receipt before
   recording deletion intent or unlinking anything.
7. Unlink failure leaves `deletion_pending`, never a fresh upload. Startup may
   finish that already authorized deletion. Crash before/after receipt replacement
   or unlink retains evidence/receipt or resumes deletion safely. Removed local
   ledgers retain job identity/receipt, but not trace bytes. No server DELETE is
   implied by local deletion.

`delete_local(id)` rejects active/finalizing/preparing/uploading sessions. It only
removes the selected session's owned files, after durable deletion intent. Intent
records explicit user/verified-receipt authority and each selected file's device
and inode before unlink can rename anything. Recovery reconciles only matching
quarantine names **and actual identities**, never unrelated quarantine entries;
remaining selected bytes or identity mismatches prevent `removed`. Ledger states
requiring a receipt/intent fail closed as damaged if those prerequisites are absent.
There is no cancellation-equals-abort shortcut. Later HTTP worker ownership must last
until its actual request operation has ended. UI account-generation fencing and
transport lifecycle barriers are explicitly later integration work.

## Checked host evidence

Linux x86_64, Python 3.12.3. Run from `src`:

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=. \
  /tmp/artisan-issue12-parent-tests.lbQyJM/bin/python test/benchmark_santoker_trace.py
```

Synthetic 20,000-notification burst, 20 raw bytes per notification, writer stalled
before startup (latency and tracemalloc measured in separate runs):

- Admitted 1,409; explicitly rejected 18,591; charged 2,096,592 bytes.
- Mixed admission/rejection latency p50 1.582 us, p99 3.926 us, max 569.219 us.
- Admitted-only latency p50 2.463 us, p99 5.669 us.
- Tracemalloc current 516,720 bytes, peak 517,368 bytes during the memory run.
- Release/drain/seal/validate: approximately 354 ms latency run / 425 ms memory run.
- Both generated incomplete artifacts round-tripped through the unchanged wire
  validator. Different sessions/timestamps correctly produce different artifacts;
  retries of one session reuse its persisted bytes.

These are **this host's synthetic recorder measurements**, not Raspberry Pi,
whole-process memory, BLE/parser throughput, UI latency, sampling jitter, or
control-safety qualification. No hardware/network was used.
