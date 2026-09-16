# Santoker BLE diagnostic traces — wire contract v1

This is the shared normative foundation for Artisan and Artisan Server, not a
claim that capture, UI, upload routes, or durable storage are implemented. Both
repositories carry this identical document, pure independently importable helpers,
and identical golden vectors. No dependency, `.alog`, roast upload, public roast,
or other-device/transport contract changes are part of this foundation.

## Envelope, encoding and limits

An uploadable artifact is **one gzip member containing UTF-8 JSON Lines**:
one manifest, zero or more events, and exactly one required terminal summary.
Every record is one JSON object followed by LF (including the last). No blank
records, BOM, duplicate keys (at any depth), nonfinite numbers, second manifest,
second/nonterminal summary, missing summary, additional gzip member, trailing
compressed bytes (including zero padding), truncated trailer or bad CRC/ISIZE.
Objects have exact per-kind key sets below; no extension/metadata bags in v1.
Unknown versions, fields and enum values are rejected, never silently discarded.

The gzip header is exactly `1f8b08000000000000ff`: DEFLATE, flags=0, mtime=0,
XFL=0, OS=255; no filenames, comments or optional gzip metadata. Encoders use
level-6 raw DEFLATE followed by gzip CRC32 and uncompressed-size trailer. Canonical
JSON uses sorted keys, ASCII escaping, separators `,`/`:`, no extra whitespace.
Validators accept other valid JSON whitespace/key ordering, but enforce that
fixed gzip header. Compressed SHA-256 covers **all exact artifact bytes**.
DEFLATE library versions can affect compression: persist the first sealed artifact
and retry its bytes; never regenerate it for a retry or promise universal
cross-zlib byte identity. The golden vector pins the reference encoding.

| Limit | v1 default |
|---|---:|
| Compressed bytes | 65 MiB (68,157,440) |
| Expanded bytes | 64 MiB (67,108,864) |
| Individual record, including LF | 64 KiB (65,536) |
| Event records, **including summary**, excluding manifest | 500,000 |
| JSON nesting (root object = 1; containers only) | 8 |

Manifest plus non-summary events must fit **expanded limit minus 64 KiB** and
non-summary events must number at most **499,999**. The reserved 64 KiB and one
event are exclusively for the required terminal failure/gap summary. Custom
limits in pure tests reserve `record_bytes` and one event in the same way.
Reject oversize input; do not truncate it and report success. The future recorder
must bound admission/queues and stop capture visibly at the smaller body/count
caps, retaining preceding records and sealing incomplete. No old trace eviction.
For standard level-6 encoding, the compressed allowance provides headroom above
the 64 MiB expansion bound, including incompressible data, and is below the
existing 82m reverse-proxy cap. This is not a benchmark or runtime performance
qualification. Recorder RAM, preparation disk, request concurrency and quota
budgets remain separate later implementation gates.

`validate_trace(BinaryIO, expected_session_id=..., expected_sha256=..., limits=...)`
reads at most 64 KiB per read/decompression step, bounds a line before JSON
decoding, bounds nesting before recursive decoding, and retains only the
manifest, summary, current bounded line/chunk and aggregate counters. It returns
`ValidatedTrace(manifest, summary, sha256, byte_size, expanded_size)` only after
trailer and EOF validation. Fixed validation error categories never echo payloads.
`encode_trace(Iterable[Record], BinaryIO, limits=...)` validates and writes one
record at a time; it does not accumulate a session. Callers supply bounded input
records. On any error, including short writes, the destination is partial and
**must not be published**. Helpers do not close streams, fsync, implement storage
receipts, recover journals or run asynchronously. Use them off UI/event loops.

## Primitive types and metadata

All listed fields are required; no optional keys or `null` values unless explicitly
stated. Integers must be JSON integers (not booleans or float spellings), within
0..2^63-1 unless narrower bounds are given. UUIDs are non-nil canonical lowercase
hyphenated UUID strings. Each session and connection/operation receives a fresh
immutable UUID; connection identity is session-local, **not a physical BLE MAC**.
UTC strings are real calendar timestamps exactly `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
Version strings match `[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}`.

Manifest exact fields:

| Key | Value |
|---|---|
| `kind`, `schema_version` | `manifest`, integer `1` |
| `session_id` | UUID for one ON/OFF cycle |
| `device`, `transport` | `Santoker`, `ble` |
| `app` | exact object `{name: "Artisan", version: <version>}` |
| `os` | exact object `{family, version, architecture}` |
| `os.family` | `linux`, `macos`, `windows` |
| `os.architecture` | `x86`, `x86_64`, `arm`, `arm64`, `other` |
| `os.version` | version string, not arbitrary platform-description text |
| `started_at` | UTC timestamp sampled with monotonic origin before first connection |
| `clock` | exact object `{source: "monotonic", unit: "ns", origin_ns: <integer>}` |
| `sample_interval_ms` | integer 1..60,000 |
| `temperature_unit` | `C` or `F` |

No device name/model entered by a user, BLE MAC, hostname, username, account/org
identity, local path, exception message, authorization/settings dump or arbitrary
logs belong in the bundle. Metadata is derived from allowlisted application
constants/settings only. Unknown credential keys are rejected at every object
level. Version token syntax limits size/content, but cannot establish provenance
or discover secrets deliberately disguised as version tokens: producers must
select only the documented OS/app version sources.

## Event ordering, gaps and clock correlation

Every event **and summary** has these exact common fields:
`kind`, `session_id`, `seq`, `mono_ns`, `dropped_before`, plus only the kind-specific
fields below. `session_id` must equal the manifest. Sequence starts from 1; the
manifest has no sequence. Sequence counts **every attempted recorder admission**,
including rejected admissions and the summary's own admission. It does not count
callbacks after capture has explicitly stopped. Such stopping is visibly reported
and terminates the trace as incomplete rather than pretending to capture forever.

Admission assigns sequence and samples session-relative `mono_ns` at the **same
serialized admission point**, not at producer occurrence time. Accepted records
are emitted in that order; times are nonnegative and nondecreasing (ties allowed).
`mono_ns = monotonic_now_ns - clock.origin_ns`. Concurrent producer timestamps
must not be substituted: acquisition order is not a claim of causality or exact
hardware occurrence time. No change to existing parser scheduling is implied.

If the preceding stored sequence is `p` (0 before the first event), the next
record must have `seq = p + dropped_before + 1`. Thus `dropped_before > 0`
explicitly declares exactly the missing range `[p+1, seq-1]`. This applies to every
kind, including the summary: final tail loss requires no extra gap record, giant
range array or unbounded validator history. Each missing sequence is accounted
once. An optional structured `gap` event can add a reason, but must itself have
positive `dropped_before`. Declared loss forces incomplete completion. Dropped
counts refer to rejected admissions, not bytes, parser noise or transport loss.

UTC end may precede UTC start after wall-clock adjustment; validators deliberately
do not reject that. Use relative monotonic time for duration/order and the
start/origin pair for approximate UTC correlation, not wall clock subtraction.

### Exact per-kind fields and allowed values

Fields listed here are in addition to the common fields. IDs are UUIDs.

| Kind | Additional fields |
|---|---|
| `connection` | `connection_id`, `state` |
| `rx` | `connection_id`, `characteristic`, `direction: "rx"`, `payload` |
| `command_intent` | `connection_id`, `operation_id`, `payload` |
| `tx_attempt` | `connection_id`, `operation_id`, `chunk_index`, `characteristic`, `direction: "tx"`, `response` (boolean), `payload` |
| `tx_result` | `connection_id`, `operation_id`, `chunk_index`, `outcome` |
| `device_ack` | `connection_id`, `operation_id`, `code` (integer 0..65,535) |
| `parser` | `connection_id`, `result` |
| `control` | `operation_id`, `action`, `value` (boolean or finite number -1000..10,000) |
| `milestone` | `roast_ordinal` (integer 1..500,000), `name` |
| `status` | `severity`, `code` |
| `gap` | `reason` |
| `summary` | See next section |

- `connection.state`: `scan_started`, `scan_failed`, `connect_started`, `connected`,
  `connect_failed`, `subscribe_started`, `subscribed`, `subscribe_failed`,
  `unsubscribe_started`, `unsubscribed`, `unsubscribe_failed`, `disconnect_started`,
  `disconnected`, `disconnect_failed`.
- RX characteristic: `6e400003-b5a3-f393-e0a9-e50e24dcca9e`.
  TX characteristic: `6e400002-b5a3-f393-e0a9-e50e24dcca9e`.
- `chunk_index`: integer 0..500,000, zero-based within the immutable command
  operation; do not reuse an operation ID for another command/connection.
- `tx_result.outcome`: `written`, `rejected`, `timeout`, `cancelled`, `error`.
  Timeout/cancellation are uncertain outcomes, not proof nothing reached the device.
- `parser.result`: `accepted`, `noise`, `truncated`, `invalid_header`,
  `invalid_length`, `crc_mismatch`, `invalid_tail`.
- `control.action`: `power`, `fan`, `drum`, `machine_on`, `heating_on`, `warmup`,
  `warmup_target`. Value records intent in device/application units; temperature
  target uses the manifest's unit. No arbitrary command text or settings snapshots.
- `milestone.name`: `roast_start`, `roast_end`, `charge`, `dry_end`, `fc_start`,
  `fc_end`, `sc_start`, `sc_end`, `drop`, `cool_end`. Ordinals are session-local;
  no saved `.alog` path or roast foreign key is needed.
- `status.severity`: `info`, `warning`, `error`.
- `status.code`: any incomplete reason below, or `session_on`, `off_requested`,
  `cleanup_finished`, `protocol_ready`, `protocol_not_ready`.
- `gap.reason`: any incomplete reason below.

One RX record preserves **one notification**, including noise and bytes ignored by
the parser. One `tx_attempt` preserves **one actual chunk write attempt**. Do not
coalesce or split these boundaries for convenience. `command_intent` may contain
the whole proposed command; it is not a successful write. Results correlate by
immutable connection/operation/chunk, not by a mutable current-session pointer.
A `device_ack` is reserved for an actually observed protocol acknowledgement with
a justified command association, never inferred from write completion or accepted
telemetry. Do not emit it where the protocol provides no ACK semantics. Missing
results after a crash mean unknown, never success. Structural validation does
not prove device causality or require preceding observed intents/results, which
can be absent because of declared loss. Later recorder integration must test
immutable identity binding, including delayed callbacks across reconnect/OFF/ON.

### Payloads and explicit privacy limit

Exact payload object: `{encoding: "base64", byte_length: <integer>, data: <string>}`.
`data` is strict canonical RFC 4648 base64 with padding, no whitespace, zero
padding bits, decoding to exactly `byte_length` bytes. Length range is 0..65,536;
the record limit also applies, so base64 overhead reduces usable payload size.

Reserved redaction object: `{encoding: "redacted", byte_length: <original length>,
reason: "sensitive_protocol_field"}`. No `data` or other keys. It represents whole
boundary omission, explicitly losing fidelity and preserving the original length;
it does not pretend to be byte-exact or silently replace bytes.

**The in-scope Santoker application BLE protocol has no credential exchange.**
Reviewed `santoker.py` command construction sends protocol targets and numeric
control values, not credentials. Preserve raw application bytes verbatim in v1.
Neither implementation claims universal secret detection inside arbitrary noisy,
malformed or malicious BLE bytes (even a token-looking byte string is preserved).
No heuristic/cross-notification substitutions are performed. If identified
sensitive protocol fields are introduced/discovered, stop and define capture-side
redaction at notification/chunk boundaries before persistence; the reserved
representation permits explicit omission. Metadata allowlisting is not a promise
that arbitrary application payloads are secret-free.

## Mandatory terminal summary and interrupted sessions

Additional fields: `ended_at` (UTC), `completion` (`complete` or `incomplete`),
`reasons` (unique bounded array of allowed reason strings), `accepted_events`,
`dropped_events`, `last_seq` (all integers).

- `accepted_events` equals the number of stored non-summary event records.
- `dropped_events` equals the sum of every `dropped_before`, **including summary**.
- `last_seq` equals the summary's own `seq`, equivalently
  `accepted_events + dropped_events + 1`.
- `complete` requires zero drops and empty reasons, and the producer must have
  observed actual OFF cleanup completion. A `.stop()` return or gzip trailer alone
  does not establish cleanup completion.
- `incomplete` requires at least one reason and may have zero known dropped events.
  Reasons: `crash_recovery`, `queue_overflow`, `storage_limit`, `event_limit`,
  `io_error`, `cleanup_timeout`, `capture_error`.

An active journal is not uploadable. Later recovery may salvage only durable whole
records, discard a partial tail and synthesize an explicitly incomplete
`crash_recovery` summary. Unknown power-loss tail is not an invented precise count;
only known rejected admission ranges may be counted. A syntactically truncated
gzip is rejected by these helpers, **not recovered** by upload validation. Crash
checkpoint intervals, power-loss durability and cleanup deadlines are later gates.

## HTTP API and receipt (specified only; not implemented here)

Base `/api/v1/diagnostic-traces`, independent of roast upload/publication.

- `PUT /{session_uuid}`: bearer-only, `Content-Type: application/gzip`, immutable
  bytes. Required `X-Trace-SHA256` is lowercase 64-digit compressed SHA-256.
  Required `X-Trace-Organization-ID` and `X-Trace-Uploader-ID` assert the manually
  pinned destination identity. Authenticate and check both assertions against
  current membership/user **before consuming the body**; headers never assign
  ownership. Session path must match validated manifest. Invalid/missing bearer
  never falls back to browser cookie auth. Identity mismatch: 403 before body.
- Idempotency scope is `(organization_id, uploader_user_id, session_uuid)`, not
  credential ID or org alone. First committed storage returns 201; same exact
  bytes retry returns 200 and same resource receipt. Conflicting bytes: 409
  `idempotency_conflict`. Permanently deleted identity: 410, never a stored receipt.
  Invalid schema/digest: 422; limits: 413; overload/uncertain storage: 503, no
  affirmative receipt. Enforce body limits regardless of Content-Length.
- Stored receipt exact fields: `id`, `session_id`, `organization_id`,
  `uploader_user_id` (UUIDs), `sha256` (lowercase compressed digest), `byte_size`
  (compressed size), `stored_at` (UTC), `storage_status: "stored"`.
  Return only after verified object persistence/readback and committed ready
  metadata. A caller-supplied digest echoed back or HEAD alone is not durability.
- Desktop manual authorization pins origin/org/user plus an immutable credential
  client and artifact path/digest/size. Verify authenticated identity with that
  client and enforce preconditions, not a later replacement account. Validate
  status and all receipt fields against pinned destination and immutable artifact
  **before local deletion**. Persist valid receipt before unlink; unlink failure
  is deletion-pending, not a new upload. Origin is checked against the request's
  pinned destination, not an untrusted receipt field. Same-org user changes still
  require explicit reauthorization. Never let stale completion update new-account UI.
- `GET /`: metadata-only list, ready records, bounded cursor pagination default
  50/max 100, stable `(stored_at,id)` ordering. Include safe app/OS/device metadata,
  session/start/end/storage timestamps, completion/reasons/counts and compressed
  size. `GET /{id}/download` and `DELETE /{id}` use the **returned server resource
  id**, not PUT's session UUID.
- Every list/download/delete predicate is current tenant AND (uploader OR current
  organization admin). Uploader must still have membership. Inaccessible resource
  reads return generic 404. No public roast visibility inheritance, public URLs,
  browser viewer or required roast association. Download is verified private
  attachment with UUID-derived filename, `Cache-Control: private, no-store`,
  `X-Content-Type-Options: nosniff`.
- Browser DELETE requires CSRF; bearer DELETE does not use cookie fallback.
  Persist explicit deletion intent, allow authorized retry, and return 204 only
  after confirmed physical deletion plus committed tombstone; failure is retriable
  503/pending, never misleading success. Retain traces until explicit deletion.

## Storage ownership and later implementation gates

No SQL/S3/filesystem lifecycle is implemented by these helpers. Later persistence
must reserve durable identity and quota before object mutations, use immutable
object keys/conditional PUT, verify bounded readback digest, then commit ready
publication. Count unpublished reservations and delete-pending bytes against
quota. No silent expiry, background retry upload without user action, or local
pending trace eviction. New ON records separately immediately while a nonmodal
previous-trace prompt offers Upload and delete or Delete; dismiss/failure retains.

A canceled HTTP task does **not** cancel an already-running `asyncio.to_thread`
SDK PUT/DELETE. Lifecycle ownership must include the actual underlying operation
and its input stream until completion, with bounded admission and finite SDK
timeouts. Do not release reservation/lock, close its spool, or confirm deletion
while known mutation work can still execute. Test blocked fake-SDK PUT, cancel
caller, race DELETE, release PUT: no late bytes may survive successful deletion.
Preserve uncertain operations for process-crash reconciliation, not blind cleanup.

DELETE first records durable intent and hides the object from ready access/replay,
then physically deletes, then leaves a permanent compact tombstone containing
**only resource/session/organization/uploader identity needed to prevent resurrection**.
No trace bytes, timestamps, app/device metadata or bundle summary survive as a
`tombstone`; no stored receipt is returned for it. Reconciliation distinguishes
unpublished uploads from explicit deletion intents and is not age-based expiry.
Bucket durability/versioning/lifecycle and physical historical-version deletion
must be qualified before deployment; this document does not verify infrastructure.

Later work: recorder and crash-safe store, async OFF cleanup barrier, bounded
transport hooks and parser/control correlation, manual pinned uploader, Qt prompt,
private server SQL/S3/API/UI, cancellation/fault-injection integration tests,
quotas/deletion reconciliation, then cross-platform/Raspberry Pi benchmarks.
The existing parser handoff can accumulate pending queue-put coroutines outside
its nominal queue; recorder bounds must not be described as whole-process bounds.
No performance/non-disruption claims until those benchmarks and regressions pass.

## Shared golden evidence

Desktop: `src/test/unitary/artisanlib/data/diagnostic-traces-v1/`.
Server: `backend/tests/data/diagnostic-traces-v1/`.
Each has identical `incomplete.jsonl`, `incomplete.jsonl.gz`, `incomplete.sha256`.
Reference: 784 compressed bytes, 2926 expanded bytes, SHA-256
`a4129615d192ce4645a0fbc756147d20a02c701395ecf58e3e2342a4c2827201`.
It covers raw notification/chunk boundaries, intent vs write, lifecycle/parser/
control/milestone events, explicit gaps and recovered incomplete terminal tail.
Matching pure test suites also cover empty complete sessions, redaction and its
limits, malformed/hostile input, streaming fragmentation, truncation at every byte,
resource boundaries/reserves, clocks, counts and immutable artifact assertions.
Database/authorization/storage behavior is documented, **not tested by this slice**.
