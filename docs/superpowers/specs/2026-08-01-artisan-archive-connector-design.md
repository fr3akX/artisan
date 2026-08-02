# Artisan Archive Connector Design

**Date:** 2026-08-01  
**Status:** Approved  
**Slice:** Phase 4 of the Artisan Roast Server MVP

## Purpose

Add a first-class Artisan desktop connector for an independently hosted Artisan Roast Server. The connector uploads exact saved `.alog` revisions through a durable queue, browses the server archive, downloads checksum-verified profiles, and opens them as read-only server-sourced profiles. It operates independently of artisan.plus.

The initial deployment target is `https://artisan.frxhome.chown.lv`, but the connector remains configurable for any conforming self-hosted server.

## Scope

### In scope

- Configurable canonical server origin.
- A revocable bearer credential copied from the server web UI and stored in the operating-system keyring.
- Explicit connection testing against `GET /api/v1/auth/me`.
- Automatic upload disabled by default and enabled only by explicit user choice after a successful connection test.
- Manual upload of the current saved `.alog`.
- Automatic queueing after successful Save, Save As, and autosave.
- An independent persistent SQLite outbox and immutable content-addressed snapshots.
- Compatible `/aroast` metadata followed by full multipart revision upload.
- Retry, pause, failed-job visibility, retry-now, and removal.
- Server roast browsing, filtering, cursor pagination, verified download, local cache, and offline reopening.
- Read-only server-sourced loading that forces Save As and does not update recent files or artisan.plus’s UUID register.
- Read-only display of roast labels.
- Cross-platform lifecycle, keyring, cache, and Qt thread-affinity behavior.

### Out of scope

- Inventory lots, reservation, release, finalization, or conflicts.
- Editing server comments or labels from Artisan.
- Applying annotations to `.alog` data.
- Automatically synchronizing the full server archive.
- Profile comparison or analytics.
- Sharing credentials, queues, caches, globals, workers, or status icons with artisan.plus.
- Password-based login from Artisan.
- Disabling TLS verification.

## User experience

### Configuration

`Config → Roast Server…` opens a configuration dialog containing:

- Server URL.
- Bearer credential field with password echo behavior.
- **Test connection**.
- Connected user, organization, and role returned by `/auth/me`.
- **Enable Roast Server**.
- **Upload saved profiles automatically**, initially disabled.
- Queue counts for pending, retrying, paused, and failed work.
- A failed-job table with safe error text, **Retry**, and **Remove** actions.
- Cache size and **Clear unused cache**.

The credential is accepted only after a successful connection test. Saving it uses a dedicated keyring service. If keyring storage fails, the dialog reports a fixed actionable error and does not put the token in settings or files.

Disabling the connector stops network processing but retains queue and cache data. Removing the credential pauses queued work rather than deleting it.

### Upload commands

`File → Upload to Roast Server` queues the current profile only when it has a saved local `.alog`. It never performs a synchronous upload in the UI thread.

A successful Save, Save As, or autosave queues the exact bytes only when the connector and automatic upload are both enabled. A server-sourced unsaved profile becomes eligible only after the user saves it locally through Save As.

### Archive browser

`File → Server Roasts…` opens a modeless dialog with:

- Free-text search.
- Parse-state, machine, and UTC date filters.
- Newest-first rows showing roast date, title, batch, machine, labels, parse state, and revision count.
- Automatic cursor pagination and an accessible **Load more** fallback.
- Refresh, online/offline status, and cached-copy status.

Selecting **Open** downloads the current revision when online or offers a previously verified cached revision while offline. The dialog never hides a refresh failure when retained data is shown.

## Package architecture

A new `artisanlib.roastserver` package contains focused modules:

- `contract.py`: strict UUID, timestamp, cursor, URL, identity, roast, revision, and error parsing.
- `settings.py`: immutable connector settings, canonical URL normalization, QSettings persistence, client instance UUID, and keyring operations.
- `api.py`: bounded synchronous HTTP client used only outside the UI thread.
- `metadata.py`: deterministic projection from a saved Artisan profile into bounded `/aroast` and revision metadata.
- `outbox.py`: SQLite schema, atomic operations, retry state, deduplication, and content-addressed snapshot ownership.
- `cache.py`: namespaced revision cache, atomic publication, validation, pruning, and offline listing.
- `worker.py`: QObject/QThread queue processing and signals.
- `controller.py`: application lifecycle, queue hooks, state aggregation, and UI-safe commands.
- `dialogs.py`: configuration/queue and archive-browser dialogs.

No module imports mutable state from `plus`. Reusable behavior is copied behind Roast Server-owned interfaces rather than referencing plus globals.

## Configuration and identity

QSettings stores only:

- canonical base origin;
- enabled flag;
- automatic-upload flag;
- stable random client instance UUID;
- most recently confirmed organization/user public identity;
- a crash-recovery marker containing only a verified pending canonical origin and public identity while credential activation is incomplete;
- bounded cache-size preference; and
- non-secret dialog geometry.

The credential uses a dedicated keyring service name and an account key derived from the canonical origin. Tokens and token hashes never enter QSettings, SQLite, profile data, cache sidecars, exception strings, logs, or Qt signals. Every security-relevant QSettings change is synchronized, checked for access/format status, and compared with an exact fresh read-back before it can affect processing; failure forces the in-process connector and automatic upload off with a fixed public error.

A successful `/auth/me` response supplies canonical user and organization UUIDs, organization slug/name, nickname/email, and role. Outbox and cache records are namespaced by canonical server origin plus organization UUID. Changing server or organization cannot expose another namespace’s jobs or cached profiles. Persisted public identity is never connection proof by itself: startup first pauses the namespace, reads the active credential in worker affinity, and validates `/auth/me` against the exact persisted canonical origin and identity before any lease or delivery. Offline/transient validation keeps enqueue available for the known active namespace but keeps delivery paused; an identity mismatch remains paused, reports the fixed credential failure, and durably disables automatic upload.

Candidate credential activation is worker-secret two-phase with a durable public journal. The worker tests the candidate without replacing keyring state and retains the candidate and old credential only in a redacted private transaction. The controller first durably records the verified pending canonical origin and public identity. That pending marker remains present while the worker writes and exactly reads back keyring and performs the final `/auth/me`; active public settings are not promoted early. Only after the controller receives the exact final activation does it promote and freshly read back disabled active settings while still retaining the pending marker, then clear and freshly read back the marker, and only then acknowledge the worker transaction and expose proof.

Interactive cancellation revokes proof immediately and leaves delivery paused while the worker restores and reads back the prior keyring state. The worker reports an explicit rollback result; only a successful result lets the controller restore/read back prior active settings while retaining the journal and then clear it. A keyring or settings settlement failure retains either the pending recovery marker or an already promoted active candidate that matches the committed keyring, never a false-clean cross-store state. Recovery applies the same journal: an exact pending credential completes final validation and promotion; a provably absent first credential or exact prior active credential can clear an uncommitted marker; every other mismatch remains marked and paused.

Application shutdown never starts an unacknowledged cross-store rollback. It first interrupts and revokes delivery authority, then preserves the durable cut: old keyring plus a pending marker before candidate commit, candidate keyring plus a pending marker after commit/final authentication, or matching promoted active candidate after promotion. Worker stop only scrubs in-memory candidate/old-credential references. Restart reconciles the journal before proof or delivery; only opaque random transaction IDs and immutable public settlement results cross Qt signals.

## URL and transport policy

- Production origins must be canonical `https://host[:port]` URLs with no userinfo, path other than `/`, query, or fragment.
- Canonical loopback HTTP is allowed only for development/testing.
- TLS certificate verification is always enabled.
- Environment proxy inheritance is disabled for connector sessions.
- Redirects are disabled for every authenticated request.
- A redirect response is an error; credentials and bodies are never forwarded.
- Responses are size-bounded before JSON/profile parsing.
- Server error envelopes are parsed into fixed safe codes/messages; arbitrary HTML and infrastructure text are not displayed.
- `401` clears the connected state and pauses work without deleting the keyring entry automatically.

## Upload data flow

### Enqueue

After successful serialization:

1. The Artisan serializer computes one immutable `bytes` value, writes exactly that value to the selected `.alog`, and captures the aware saved/modified timestamp used for that revision.
2. Require a canonical roast UUID in the detached saved profile and enforce the immutable byte value at `1..16 MiB`; the post-save request carries the exact bytes, detached `ProfileData`, and timestamp, never a mutable source pathname.
3. The UI controller performs only bounded length validation and detachment. It does not read, stat, or hash the path; worker affinity owns all snapshot, metadata, SQLite, and network work.
4. The outbox hashes and atomically publishes those exact bytes as an immutable content-addressed snapshot under the connector’s private application-data directory without replacing an existing inode.
5. While retaining the cross-process filesystem lock, durably insert a random, expiring, one-use staging owner for that namespace/SHA and return its token with the snapshot. Concurrent same-hash staging owners coexist.
6. Build bounded compatible metadata from the detached saved profile and the exact caller-supplied timestamp.
7. In one transaction, validate and consume exactly that unexpired staging token, then insert or resolve a SQLite job keyed by organization, roast UUID, and SHA-256.
8. Use the deterministic idempotency key `archive-v1:<client-instance-uuid>:<roast-uuid>:<sha256>`.

The queue owns the snapshot and the worker clears request byte/profile references after consumption. Later writes to the same original path cannot change, merge, or relabel queued revisions. Duplicate pending, completed, or idempotently acknowledged UUID/hash uploads do not create duplicate work.

### Delivery

The worker uses a shared linearizable configuration-permit protocol. Revocation synchronously marks the exact generation revoked, so every permit acquisition linearized afterward fails. Authorization installation, finalized candidate activation installation, namespace resume, lease transition, a timer arm capable of reaching leasing, each delivery request, and each online browse/detail/download request acquire an exact-generation permit immediately at the operation boundary and release it when that one bounded operation returns. The permit lock is held only for acquire/release, never during keyring, SQLite, cache, or HTTP work. An operation that already acquired its permit is formally in flight before revocation and may finish under the previously verified same namespace; no subsequent boundary can begin under that generation. UI proof and actions are invalidated immediately without waiting for such bounded in-flight work.

The worker:

1. Posts compatible metadata to `/api/v1/aroast` using the profile modification timestamp.
2. Uploads the exact snapshot to `/api/v1/roasts/{uuid}/revisions` as multipart fields `profile`, `sha256`, `idempotency_key`, and bounded `metadata`.
3. Validates roast UUID, returned revision SHA-256, revision number, state, and links.
4. Marks the job complete using the unique token returned for that exact unexpired lease and releases its snapshot only when no job or unexpired staging owner references it.

The server’s idempotent current-hash response is success. Reverting to an older non-current hash remains a new server revision according to the server contract.

### Retry classification

- Connection errors, TLS-temporary errors, timeouts, `429`, and `5xx`: retry with persisted exponential backoff from 5 seconds to 5 minutes; a valid bounded `Retry-After` may extend the delay.
- `401`: pause for credentials.
- Other `4xx`: permanent failed state with stable safe details.
- Local missing/corrupt snapshot: permanent failed state.
- Application shutdown or worker interruption: leave the leased job recoverable after a bounded lease expires.

Queue work never blocks saving, recording, or hardware operation. Worker state reaches UI only through signals.

## Outbox persistence

The outbox uses schema version 2. Opening a version-1 database first fingerprints every persistent schema object and the complete canonical version-1 tables, quoted SQL literals, columns, declared types/defaults, SQL constraints, foreign keys, and indexes (including index columns, uniqueness, origin, and partial state); only that exact unreleased schema migrates transactionally. Version 2 is checked to the same exactness, and any extra view/trigger/object or malformed/unknown schema fails closed. Version 2 adds:

- separate multi-owner `snapshot_staging` rows keyed by random token, with namespace, SHA-256, generated path, byte count, source timestamp, creation timestamp, and expiry;
- a nullable unique random `lease_token` on each job attempt; and
- database and read-time state invariants for snapshot ownership, completion, retry/failure details, and lease token/expiry nullability.

The SQLite database uses WAL mode, full synchronous commits, foreign keys, busy timeout, and `BEGIN IMMEDIATE` transitions. Tables store connector namespace, job/roast UUIDs, SHA-256, snapshot relative path/size, canonical duplicate-free JSON-object metadata, deterministic idempotency key, state (`pending`, `leased`, `retry_wait`, `paused`, `failed`, `complete`), attempts, next-attempt timestamp, lease expiry/token, allowlisted stable error code/message, and created/updated/completed timestamps.

`snapshot_bytes()` retains the cross-process filesystem lock through exact-byte publication, file/directory durability, and staging-row commit; the existing hardened `snapshot_saved_file()` seam follows the same ownership contract but is not used by post-save integration. `enqueue()` atomically validates and consumes only its unexpired token. Startup expires abandoned stages and deletes a snapshot row/file only after proving that no job and no unexpired stage references it; unindexed publication-crash residue is then collected. Same-hash stages and jobs may share one immutable file without replacing its inode.

Every lease creates a unique token. Complete, retry, and fail require `(job_id, lease_token, now, ...)` and compare-and-swap only a currently leased row whose token matches and whose expiry is strictly after `now`; stale ownership yields the fixed `lease_lost` error. Recovery, namespace pause, and job removal invalidate the token. Before leasing, the indexed generated path, private read-only state, byte count, and SHA-256 are verified. Paths are generated internally and cannot escape the private root.

## Browse and download data flow

1. List `/roasts` with normalized filters and cursor.
2. Strictly validate every item, label, timestamp, count, state, and next cursor.
3. Fetch `/roasts/{uuid}` and require response identity/link consistency.
4. Resolve the current revision and download `/revisions/{number}/download` with redirects disabled.
5. Stream into a cache-generated `<token>.part` while the cache retains an exclusive OS lock on the paired `<token>.lock` for the complete stage lifecycle.
6. Enforce the 16 MiB ceiling while hashing streamed bytes.
7. Require exact content type, revision number, content-disposition name, byte count, and checksum headers.
8. Compare computed SHA-256 to detail metadata and response headers.
9. Validate the `.alog` through Artisan deserialization before making it visible as openable.
10. Atomically publish the cache file and sidecar; successful or failed publication consumes the stage only after all cleanup attempts finish.

A failure leaves the current Artisan profile untouched. Every profile, sidecar, publication temporary, and stage cleanup is attempted independently; the public result remains the fixed cache failure. Validation failure before publication calls `discard_staging()` explicitly. Worker shutdown calls `CacheStore.close()`, which discards every stage owned by that store and releases all lifecycle locks.

Cache paths are namespaced by server and organization, then roast/revision/hash. Sidecars contain public metadata only and require the cached revision number to equal the roast's current `revision_count`. Contract JSON uses immutable, explicitly tagged array values so empty arrays, empty objects, and pair-shaped arrays remain distinct and sidecar serialization is lossless. Cache pruning securely opens and retains every supplied protected regular-file identity before any deletion; missing, inaccessible, linked/reparse, non-regular, or changing protected paths abort the complete prune. Generated paths and hard-link aliases are protected by device/file identity. Pair and rollback cleanup removes only the exact owned pathname when its identity still matches, never a replacement pathname or every hard link.

Stage creation and temporary-tree scans retain the global cache process lock. Maintenance never infers abandonment from timestamps: it attempts a nonblocking exclusive lock on each generated stage lock. Contended pairs survive regardless of mtime or wall-clock changes; an acquired lock proves the owner process exited, after which both generated pair paths are removed. A stage lock remains held across download, validation, publication or explicit discard, cleanup, and final release on POSIX and Windows.

## Read-only profile loading

`ApplicationWindow.loadFile()` receives an explicit server-read-only mode. In that mode it:

- validates and loads through the normal profile path;
- skips `plusAddPath()`;
- does not add the cache file to recent files;
- sets `curFile` to `None` after successful load;
- does not update plus modification/sync state or trigger plus synchronization;
- marks the profile clean and records transient server source/revision state in the Roast Server controller; and
- displays a status message with server, revision, and online/stale cache state.

Standard Save then behaves as Save As. The cache file is never overwritten. Once saved locally, the new local file resumes ordinary Artisan and optional connector behavior.

## Metadata projection

The connector stores only the subset needed by `/aroast` and searchable revision hints:

- roast UUID, source modification time, roast date/time;
- title, batch prefix/number/position;
- operator, machine, setup;
- green, roasted, and defect weights with units normalized through existing Artisan utilities;
- event times and temperatures when finite;
- development time/ratio; and
- selected non-sensitive roast descriptors supported by the compatible contract.

Metadata is deterministic JSON, finite, safe-integer bounded, NUL-free, and capped below the server metadata limit. Strict response contracts freeze JSON objects as sorted immutable key/value tuples and JSON arrays as immutable explicitly tagged array values; this preserves source shape for empty and pair-shaped containers. Unknown profile values are omitted rather than stringified. Exact `.alog` bytes remain authoritative.

## Qt lifecycle

The controller is created after the main window and settings paths are ready. It owns one worker and one QThread. Startup opens/migrates the outbox and recovers leases, but only resumes processing after worker-side keyring read and exact `/auth/me` validation of the persisted canonical origin and identity. Known-namespace offline saves may enqueue while this proof is absent and remain paused. Interactive rollback is asynchronous and delivery remains paused; a bounded settlement timer reports a fixed keyring status without clearing the journal if acknowledgement has not arrived. Shutdown requests interruption, synchronously revokes new operation permits, wakes the worker, and waits a bounded interval without attempting settings/keyring rollback. The worker explicitly discards every cache stage, scrubs secret references while preserving durable activation cuts, closes the cache and SQLite, and is never terminated unsafely. The production `stopped → deleteLater → quit` wiring destroys the worker and child timer in worker affinity after both stores close, including after a delayed bounded shutdown.

Dialogs call controller methods; they do not perform HTTP or direct queue writes. Worker signals carry immutable public result objects without credentials, credential hashes, or response bodies. Every externally callable worker slot rejects direct wrong-thread use with a fixed failure before keyring, HTTP, cache, outbox, or filesystem I/O; opaque vault entries are safely consumed when rejection is needed to erase sensitive payloads.

## Security

- Credential and authorization headers are always redacted.
- Private directories/files use restrictive permissions. POSIX security-critical opens and publication are descriptor-relative, no-follow, and no-clobber. Identity-bound removal never check-closes-unlinks the original name: while holding the connector process lock it atomically moves that entry to a random connector-generated quarantine name in the same verified directory with native no-replace semantics where available, verifies the moved identity, and deletes only that quarantine entry. An unexpected identity is restored without deletion when the original name remains free; a later replacement at the original name is never overwritten. Quarantine names encode the expected stable identity as well as a random token, so startup maintenance collects crash-left quarantine entries only after the same identity verification; mismatches remain preserved and fail closed. Maintenance scans other connector-generated names path-wise while holding the process lock, rejects links at every observed entry, and relies on the stated non-malicious-same-user boundary rather than claiming descriptor-relative scanning. Windows opens every component with native no-reparse handles, verifies an exact protected current-user-only DACL ACE-by-ACE, publishes and quarantines with native write-through moves, prefers identity verification and deletion through one held native file handle, and uses write-capable synchronized directory handles for metadata flushes. Unsupported security APIs and access-denied durability failures fail closed.
- SQLite database/WAL/SHM paths are checked and hardened under the private root and connector process lock before SQLite can access existing entries.
- The local same-user threat model assumes a non-malicious account and protects against accidental links/reparse points, stale permissions, crashes, and competing connector processes. All internal connector-process mutations are serialized by the advisory process lock. A malicious process already running as the same user can normally modify that user’s private files/memory and is outside this boundary; random quarantine names reduce accidental races but do not expand that threat claim.
- Cache/outbox filenames never use server-provided names.
- No untrusted response is passed to `eval`, pickle, shell commands, filesystem paths, or Qt rich text.
- UI strings use plain text and fixed translations.
- Download verification precedes deserialization and profile replacement.
- Cross-organization cache/outbox records are unreachable after identity changes.
- No automated test contacts the deployed server or another external endpoint.

## Error presentation

The UI exposes fixed categories:

- Offline / server unavailable.
- Credential rejected or revoked.
- Request rate limited.
- Invalid server response.
- Profile rejected by server.
- Local saved file changed or unavailable.
- Download checksum mismatch.
- Cached copy corrupt or unavailable.
- Roast Server settings could not be saved.

Safe server validation messages may be retained when they match the versioned error envelope and length/control-character bounds. Tokens, request bodies, profile contents, paths, tracebacks, and arbitrary proxy pages are never shown.

## Testing

### Qt-independent

- Canonical URL and redirect rejection.
- Strict response and error parsing.
- Metadata boundary and determinism tests.
- Multipart upload and checksum/idempotency contracts.
- Retry classification and `Retry-After` bounds.
- Strict v1 fingerprint/v2 migration, malformed schema/JSON/state rejection, multi-owner staging with real process barriers and abandoned expiry, duplicate enqueue, no-clobber/open-inode publication, tamper detection, fenced A/B leases, restart recovery, retry/failure/removal, fsync/permission failure, multiprocess cleanup, and snapshot job/stage reference tests.
- Windows-marked runtime reparse/ACL/locking/publication/deletion tests plus platform-independent deterministic ctypes/native seams for exact ACL parsing, reparse rejection, write-capable flush success/failure, write-through no-replace publication, EEXIST reuse, and lock contention; non-Windows validation does not claim Windows runtime execution.
- Cache publication, namespace isolation, corruption, checksum, pruning, stale-open, lossless tagged-JSON sidecar round trips, exact current-revision checks, fail-closed protected-path identity/race handling, and exhaustive identity-bound cleanup tests.
- Real subprocess barriers for active old stages, owner discard, process-crash collection, first-root creation, and same-destination publication. Windows-marked runtime cache tests cover ACLs, reparse rejection, replacement, file/directory flush, locking, and deletion; portable full-flow native seams run on every platform, and Linux reports native Windows cases as skipped.
- No-proxy and token-redaction tests.

### Qt

- Configuration validation, keyring failure, opt-in auto-upload, and connection-test transitions.
- Queue-state rendering and retry/remove actions.
- Cursor browsing, filter normalization, refresh failure with retained rows, offline cache view, and opening.
- Worker signal/thread-affinity and production-wiring shutdown tests, including delayed stop and no surviving worker/timer/store handles.
- Deterministic pre-permit revocation tests at authorization install, activation install, namespace resume, lease, timer arm, upload metadata/revision, and online browse/detail/download boundaries, plus bounded grandfathered in-flight permit classification.
- Real-QThread shutdown/restart tests at pre-keyring, post-keyring, post-final-auth, post-promotion/pre-ack, and post-ack cuts, with organization A/B jobs, consistent pending/promoted recovery state, rollback-failure journaling, and zero upload before exact proof.
- Save/autosave/manual hooks remain non-blocking; blocked-worker same-path saves preserve two distinct exact byte/UUID/metadata/timestamp revisions with no UI read/stat/hash.
- Security QSettings access, format, and fresh-readback mismatch failures remain fail-closed.
- Open-cache protected paths propagate immediately and remain current for publication pruning, clear, and namespace round trips.
- Read-only server load excludes recent files and plus registration, clears `curFile`, and forces Save As.

### Coexistence

- Existing plus settings, credentials, token, queue, cache paths, status actions, UUID register, and upload behavior remain unchanged.
- Connector operation while plus is connected uses independent workers and storage.
- Server-read-only opening does not invoke plus sync.

### Manual integration

After automated tests, issue a new revocable credential from `https://artisan.frxhome.chown.lv`, configure it on this machine, test `/auth/me`, manually queue a deterministic local profile, verify it appears in the web archive, download/open it read-only in Artisan, Save As, and revoke any temporary validation credential not intended for continued use.

## Acceptance criteria

1. Setup uses a web-issued revocable bearer credential stored only in the OS keyring.
2. Automatic upload is disabled by default and cannot be enabled before a successful connection test.
3. Save/autosave queue exact immutable snapshots without blocking the UI.
4. Queue work survives restart and classifies transient/permanent/credential failures correctly.
5. Identical UUID/hash upload is idempotent; changed/reverted content follows server revision rules.
6. Manual upload uses the same queue.
7. Server lists paginate/filter safely and retain visible refresh errors.
8. Downloads are size/checksum/header verified before deserialization.
9. Cached revisions can open offline with a stale indicator.
10. Server profiles open with no writable `curFile`; Save invokes Save As and cache files remain immutable.
11. Read-only opening does not update recent files or artisan.plus registration/sync.
12. Credentials, queues, caches, workers, settings, and status remain independent from artisan.plus.
13. Automated tests require no network, server, cloud account, keyring daemon, or hardware.
