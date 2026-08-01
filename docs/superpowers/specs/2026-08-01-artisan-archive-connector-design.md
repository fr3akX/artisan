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
- bounded cache-size preference; and
- non-secret dialog geometry.

The credential uses a dedicated keyring service name and an account key derived from the canonical origin. Tokens never enter QSettings, SQLite, profile data, cache sidecars, exception strings, logs, or Qt signals.

A successful `/auth/me` response supplies canonical user and organization UUIDs, organization slug/name, nickname/email, and role. Outbox and cache records are namespaced by canonical server origin plus organization UUID. Changing server or organization cannot expose another namespace’s jobs or cached profiles.

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

1. Require a canonical roast UUID in the saved profile and a local `.alog` path.
2. Open the saved path without following symlinks where supported and enforce the local 16 MiB profile ceiling.
3. Read exact bytes, compute SHA-256, and confirm the file remains the same size/identity through the snapshot operation.
4. Atomically publish an immutable content-addressed snapshot under the connector’s private application-data directory without replacing an existing inode.
5. While retaining the cross-process filesystem lock, durably insert a random, expiring, one-use staging owner for that namespace/SHA and return its token with the snapshot. Concurrent same-hash staging owners coexist.
6. Build bounded compatible metadata from the in-memory saved profile.
7. In one transaction, validate and consume exactly that unexpired staging token, then insert or resolve a SQLite job keyed by organization, roast UUID, and SHA-256.
8. Use the deterministic idempotency key `archive-v1:<client-instance-uuid>:<roast-uuid>:<sha256>`.

The queue owns the snapshot. Later edits to the original path cannot change queued bytes. Duplicate pending, completed, or idempotently acknowledged UUID/hash uploads do not create duplicate work.

### Delivery

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

`snapshot_saved_file()` retains the cross-process filesystem lock through publication, file/directory durability, and staging-row commit. `enqueue()` atomically validates and consumes only its unexpired token. Startup expires abandoned stages and deletes a snapshot row/file only after proving that no job and no unexpired stage references it; unindexed publication-crash residue is then collected. Same-hash stages and jobs may share one immutable file without replacing its inode.

Every lease creates a unique token. Complete, retry, and fail require `(job_id, lease_token, now, ...)` and compare-and-swap only a currently leased row whose token matches and whose expiry is strictly after `now`; stale ownership yields the fixed `lease_lost` error. Recovery, namespace pause, and job removal invalidate the token. Before leasing, the indexed generated path, private read-only state, byte count, and SHA-256 are verified. Paths are generated internally and cannot escape the private root.

## Browse and download data flow

1. List `/roasts` with normalized filters and cursor.
2. Strictly validate every item, label, timestamp, count, state, and next cursor.
3. Fetch `/roasts/{uuid}` and require response identity/link consistency.
4. Resolve the current revision and download `/revisions/{number}/download` with redirects disabled.
5. Enforce the 16 MiB ceiling while hashing streamed bytes.
6. Require exact content type, revision number, content-disposition name, byte count, and checksum headers.
7. Compare computed SHA-256 to detail metadata and response headers.
8. Validate the `.alog` through Artisan deserialization before making it visible as openable.
9. Atomically publish the cache file and sidecar.

A failure leaves the current Artisan profile untouched and removes temporary/corrupt data.

Cache paths are namespaced by server and organization, then roast/revision/hash. Sidecars contain public metadata only. Cache pruning never deletes an open file or a file referenced by pending work.

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

Metadata is deterministic JSON, finite, safe-integer bounded, NUL-free, and capped below the server metadata limit. Unknown profile values are omitted rather than stringified. Exact `.alog` bytes remain authoritative.

## Qt lifecycle

The controller is created after the main window and settings paths are ready. It owns one worker and one QThread. Startup opens/migrates the outbox, recovers leases, then starts processing only if enabled and credentialed. Shutdown requests interruption, wakes the worker, waits a bounded interval, closes SQLite, and never terminates a thread unsafely.

Dialogs call controller methods; they do not perform HTTP or direct queue writes. Worker signals carry immutable public result objects without credentials or response bodies.

## Security

- Credential and authorization headers are always redacted.
- Private directories/files use restrictive permissions. POSIX security-critical opens, publication, and removal are descriptor-relative, no-follow, and no-clobber. Startup maintenance scans connector-generated names path-wise while holding the process lock, rejects links at every observed entry, and relies on the stated non-malicious-same-user boundary rather than claiming descriptor-relative scanning. Windows opens every component with native no-reparse handles, verifies an exact protected current-user-only DACL ACE-by-ACE, publishes with a native write-through no-replace move, and uses write-capable synchronized directory handles for metadata flushes. Unsupported security APIs and access-denied durability failures fail closed.
- SQLite database/WAL/SHM paths are checked and hardened under the private root and connector process lock before SQLite can access existing entries.
- The local same-user threat model assumes a non-malicious account and protects against accidental links/reparse points, stale permissions, crashes, and competing connector processes. A malicious process already running as the same user can normally modify that user’s private files/memory and is outside this boundary; no claim is made that advisory locks defend against such a process.
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
- Cache publication, namespace isolation, corruption, checksum, pruning, and stale-open tests.
- No-proxy and token-redaction tests.

### Qt

- Configuration validation, keyring failure, opt-in auto-upload, and connection-test transitions.
- Queue-state rendering and retry/remove actions.
- Cursor browsing, filter normalization, refresh failure with retained rows, offline cache view, and opening.
- Worker signal/thread-affinity and shutdown tests.
- Save/autosave/manual hooks remain non-blocking.
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
