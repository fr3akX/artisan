# Artisan Desktop Inventory Connector Design

**Date:** 2026-08-05
**Status:** Approved in conversation; awaiting written-spec review
**Depends on:** Artisan Roast Server connector and deployed server inventory API

## Purpose

Add inventory tracking to Artisan's independent Roast Server connector. An operator selects one active green-coffee lot in Roast Properties, Artisan durably reserves its planned green weight at CHARGE, finalizes consumption after a successful local profile save, and releases the reservation after an abort or reset. The workflow must remain safe while offline, survive process restarts, expose delayed stock conflicts, and never block roasting on a network request.

The feature is independent of artisan.plus. The initial server is `https://artisan.frxhome.chown.lv`, but the implementation supports any conforming self-hosted Artisan Roast Server.

## Success criteria

- An operator can refresh, search, select, clear, and inspect active server bean lots from Roast Properties.
- A selected lot requires a valid positive green input weight before CHARGE can complete.
- CHARGE persists an inventory reservation command before recording the event and proceeds without waiting for the network.
- A successful Save, Save As, or autosave of the active roast queues one finalization; Save Copy does not finalize it.
- Abort, full reset, or accepted application shutdown queues release for a nonterminal reservation.
- Repeated or undone/re-marked CHARGE uses one roast UUID and one reservation.
- Offline commands retry idempotently and in lifecycle order after reconnect or restart.
- Old profiles and servers without inventory support continue to work.
- Inventory state is isolated by canonical server origin and organization UUID.
- No automated test requires external network access, an account, or roasting hardware.

## Scope

### In scope

- Active bean-lot listing through the deployed bearer-only desktop API.
- A cached, searchable lot chooser in Roast Properties.
- One selected lot per roast.
- Optional profile fields preserving the selected lot and its server namespace.
- Durable reserve, finalize, and release commands in a dedicated SQLite store.
- Offline queueing, retries, idempotency, restart recovery, and configuration fencing.
- Current balance and open-conflict feedback returned by mutations.
- User-visible queue, stale-cache, terminal-failure, and interrupted-reservation state.
- Coexistence with profile upload on the existing Roast Server worker thread.

### Out of scope

- Lot creation, editing, archiving, images, manual adjustments, or ledger browsing in Artisan.
- Conflict reconciliation from Artisan.
- Multiple inventory locations.
- Blends consuming more than one lot.
- Automatic or most-recent-lot selection.
- Blocking CHARGE on current server availability or cached stock balance.
- Hardware-derived weights.
- Editing a lot or reservation after finalization.
- Changes to artisan.plus inventory, credentials, queues, or synchronization.

## Existing server contract

All endpoints use the existing Roast Server bearer credential, canonical origin, no-proxy session, TLS and redirect policy, response-size bounds, and operation deadline.

### List active lots

```text
GET /api/v1/inventory/bean-lots?limit=100[&cursor=<opaque>]
```

The response is an exact object containing `items` and `next_cursor`. Each item contains:

- `lot_id`, `name`, `origin`, `varietals`, `processing_method`, and `crop_year`;
- `on_hand_grams`, `reserved_grams`, and `available_grams`;
- `unresolved_conflict_count`.

The endpoint already filters to active lots. Pagination cursors are opaque.

### Reserve

```text
POST /api/v1/inventory/reservations
Idempotency-Key: inventory-v1:<client-instance-uuid>:<reservation-uuid>:reserve
```

The canonical JSON request contains:

- `client_reservation_uuid`;
- `client_instance_uuid`;
- `roast_uuid`;
- `lot_id`;
- `planned_grams`;
- `occurred_at`.

### Finalize and release

```text
POST /api/v1/inventory/reservations/<reservation-uuid>/finalize
Idempotency-Key: inventory-v1:<client-instance-uuid>:<reservation-uuid>:finalize

POST /api/v1/inventory/reservations/<reservation-uuid>/release
Idempotency-Key: inventory-v1:<client-instance-uuid>:<reservation-uuid>:release
```

Finalize contains optional positive `actual_grams` and required `occurred_at`. Release contains only `occurred_at`.

Every mutation returns an exact `reservation`, `balance`, optional `conflict`, and `idempotent_replay` projection with `Cache-Control: no-store`. UUIDs are lower-case hyphenless values. Timestamps are aware UTC values in canonical microsecond `Z` form. Gram values are strict integers bounded by `2_147_483_647`.

The client emits canonical JSON with sorted keys, compact separators, UTF-8, and non-finite values forbidden. Each command stores the immutable request bytes and idempotency key once; retries never rebuild them from mutable profile state.

## Package architecture

The feature extends `src/artisanlib/roastserver/` with focused units:

- `inventory_contract.py`: immutable request/result types, limits, canonical encoding, strict list/mutation/error parsing, and response relationship validation.
- `inventory_store.py`: dedicated SQLite schema and atomic lot-cache, roast-state, command, lease, retry, recovery, and pruning operations.
- `inventory.py`: UI-thread lifecycle coordinator for lot selection, CHARGE, save, reset, profile load, recovery, and public status.
- `inventory_dialogs.py`: searchable lot chooser and interrupted-reservation recovery UI.
- Existing `api.py`: authenticated list/reserve/finalize/release methods using the current hardened transport.
- Existing `worker.py`: lot refresh and inventory command delivery alongside the profile outbox.
- Existing `controller.py`: public inventory signals and commands, configuration proof, worker wiring, and shutdown ordering without absorbing the inventory state machine.

Roast Properties owns only the visible controls and delegates all behavior to the coordinator. Dialogs never perform HTTP or manipulate SQLite directly.

The inventory command journal remains separate from the profile-upload outbox. Inventory operations have lifecycle dependencies and terminal choices that do not fit immutable revision snapshot ownership. Both stores reuse the connector's private-root and SQLite hardening conventions.

## Namespace and configuration ownership

A namespace is the exact pair of canonical server origin and confirmed organization UUID. Lot cache rows, roast state, and commands carry that namespace. The stable client instance UUID comes from existing Roast Server settings.

The profile stores the following optional public fields:

- `roastServerInventoryOrigin`;
- `roastServerInventoryOrganizationUUID`;
- `roastServerBeanLotUUID`;
- `roastServerBeanLotName`.

Persisting origin and organization prevents a copied profile from accidentally binding the same lot UUID on a different server or tenant. All fields are compatibility-optional in `ProfileData`; old profiles load unchanged. Names are display hints only. A newly reserved lot must be confirmed by UUID in the current namespace cache.

If origin, identity, credential, enabled state, or namespace changes, the existing configuration-generation fence immediately prevents new operations under the old authority. An already permitted request may finish, but its generation is rechecked before any response changes local state. Old commands remain paused in their original namespace and resume only when that exact namespace is configured and authenticated again. They are never remapped, copied, or deleted automatically.

A profile selection from another namespace appears as historical and unavailable. The operator must clear it or select a current-namespace lot before a new CHARGE. Changing configuration clears any unsaved current selection from the active editor rather than risking a UUID collision.

## Local persistence

The dedicated `inventory.sqlite3` uses WAL mode, full synchronous commits, foreign keys, bounded busy handling, explicit transactions, schema fingerprinting, and the connector's private path/permission checks. UI and worker affinity use separate SQLite connections with short transactions.

Conceptually, the store contains:

### Cached lots

Per namespace and lot UUID:

- the exact bounded desktop lot projection;
- a cache-generation identifier;
- the complete refresh timestamp.

A successful complete pagination run atomically publishes a new generation and removes prior rows only after validation. A failed, canceled, partial, excessive, or stale-generation refresh leaves the previous generation intact.

### Roast inventory state

Per namespace and roast UUID:

- selected lot UUID and last-known name;
- client reservation UUID;
- planned and optional actual grams;
- lifecycle and terminal intent;
- reserve, finalize, and release occurrence times;
- latest verified server reservation state;
- latest balance and conflict identifiers;
- bounded terminal error information;
- created and updated timestamps.

The lifecycle distinguishes at least `reserve_queued`, `reserved`, `finalize_queued`, `finalized`, `release_queued`, `released`, `paused`, and `failed`. Terminal intent is immutable: a reservation can choose finalize or release, never both.

### Command journal

Each command contains:

- random command identifier;
- namespace and reservation identifiers;
- operation (`reserve`, `finalize`, or `release`);
- immutable canonical request JSON and idempotency key;
- dependency on reserve completion for terminal commands;
- state, attempts, next-attempt time, and lease token/expiry;
- allowlisted bounded failure code/message;
- creation, update, and completion timestamps.

A transaction updates command, roast state, cached balance, and conflict status together after a verified response. Lease compare-and-swap fencing prevents stale worker attempts from completing a command. Startup recovers expired leases. Completed command history is retained for bounded diagnosis and later pruned only after its roast state is terminal and no recovery workflow needs it.

## Lot cache and chooser

The Roast tab of Roast Properties gains an **Inventory lot** row near **Beans** with:

- selected lot name and compact available balance;
- **Choose…**, **Clear**, and **Refresh** actions;
- plain-text status for cached/offline data, queued work, conflicts, or errors.

**Choose…** opens a searchable table showing name, origin, varietals, process, crop year, available weight, and conflict count. All active lots remain selectable, including lots with zero or negative available stock. Those balances produce warnings but do not block selection or CHARGE because the server intentionally supports delayed over-consumption conflicts.

The chooser immediately displays the last complete cache. It labels retained data with its refresh timestamp whenever it has not been confirmed in the current online session. Opening an empty or previously cached chooser requests a refresh; **Refresh** always requests one explicitly. Network work remains on the worker thread.

Pagination uses the server's 100-item maximum and follows opaque cursors sequentially. A refresh is bounded to 100 pages and 10,000 unique lots. Repeated cursors, duplicate lot UUIDs, invalid structures, bound exhaustion, cancellation, or any failed page rejects the full refresh and retains the old cache.

Selecting a lot stores its inventory link independently of artisan.plus. It copies the lot name into **Beans** only when Beans is empty. It never changes the title or green input weight and never overwrites later manual or Plus changes. Clearing the lot does not clear Beans.

The row is editable before CHARGE and locked after CHARGE. A loaded historical profile containing CHARGE remains locked for display, but it cannot create or finalize a reservation unless the local store identifies it as this installation's active reservation.

## Weight conversion

Inventory quantities use the profile's green input value and existing Artisan weight-unit conversion. The converted positive gram value is rounded to the nearest whole gram, with an exact half gram rounded upward, and must remain in `1..2_147_483_647`. A value that rounds to zero, is non-finite, has an unknown unit, or exceeds the bound is invalid.

CHARGE captures immutable planned grams. A successful qualifying save captures the then-current green input as `actual_grams` when valid. If it is missing or invalid, Artisan queues finalization without `actual_grams`; the server consumes planned grams and Artisan displays a warning that planned weight was used. Saving itself is never failed by inventory finalization.

## Roast lifecycle

### Selection before CHARGE

No selected lot preserves existing Artisan behavior. If Roast Server is enabled, CHARGE proceeds and emits a clear nonmodal warning that the roast will not be tracked in inventory. Artisan never chooses a lot automatically.

If a lot remains selected while Roast Server is explicitly disabled, CHARGE is blocked with a choice to enable Roast Server or clear the inventory lot. Explicit selection must not silently become untracked consumption. This differs from transient offline or credential-validation states in a previously authenticated namespace, which remain queueable.

If a lot is selected, CHARGE requires a current-namespace cached lot and valid positive grams. Invalid weight or unavailable selection blocks CHARGE with an actionable validation message; it does not create a UUID or command.

### CHARGE

Immediately before Artisan commits a new CHARGE event:

1. Confirm the selected lot and weight.
2. Generate a canonical roast UUID early if the profile lacks one.
3. Generate one random client reservation UUID.
4. Capture planned grams and canonical occurrence time.
5. In one full-synchronous local transaction, insert roast state and immutable reserve command.
6. Only after that transaction succeeds, permit CHARGE to complete and lock the lot selector.
7. Wake the worker, but never wait for network delivery.

A local persistence failure blocks CHARGE because proceeding would create untracked consumption contrary to the explicit selection. Offline or unauthenticated delivery does not block it; status becomes **Reservation queued**.

Undoing CHARGE does not release the reservation, unlock the lot, or create a new UUID. The UI explains that inventory remains reserved for the current roast and asks the operator to re-mark CHARGE or reset/abort. Re-marking CHARGE reuses the same roast and reservation without inserting another reserve command. This rule is intentional and supersedes the contradictory release-on-undo sentence in the conversational Design 3 draft.

### Save and finalization

A successful Save, Save As, or autosave queues finalization only when:

- the serialized active profile's canonical roast UUID matches the active local reservation;
- the roast currently contains CHARGE; and
- no terminal intent has already been recorded.

The finalization transaction captures valid current actual grams or explicitly omits them, then records one immutable finalize command. It may be queued before reserve reaches the server, but the worker cannot lease it until reserve succeeds. Repeated saves are idempotent and do not change the captured terminal command.

Save Copy is explicitly excluded from the lifecycle hook, regardless of its serialized UUID. Loading and saving a historical profile cannot finalize a reservation belonging to another session or installation.

Finalization is independent of profile upload. Either queue may reach the server first. The backend supports an inventory-created `awaiting_profile` roast shell and later reuses it when the `.alog` revision arrives.

### Reset, abort, and shutdown

A successful full reset or abort records release when the active reservation has no terminal intent. Release may be queued before reserve delivery and waits on it. Finalized or finalize-queued reservations are never released.

Canceled reset/abort operations do nothing. After accepted reset/abort, the selected lot remains selected but unlocked even when ordinary roast-property reset settings clear other properties. This supports retries and consecutive batches without silently reserving the next batch before CHARGE.

An accepted application shutdown that discards an active, nonfinalizing roast durably queues release before the controller closes its UI-side store. Shutdown does not wait for network delivery. A process crash cannot run this hook and is handled through interrupted recovery.

### Profile loading

Loading a profile restores the namespace-bound lot link. An uncharged profile is editable if its namespace and lot are current. A charged profile presents the historical selection as locked. A lot missing from the current complete cache is unavailable for new reservation until refresh confirms it.

## Worker scheduling and concurrency

Inventory shares the existing single Roast Server worker, authenticated client, QThread, operation-permit fence, retry timer, and shutdown lifecycle. It does not add a second network worker.

Only one authenticated network operation runs at a time. The worker chooses the earliest due item across profile and inventory queues. Equal due times alternate queue class so sustained profile uploads cannot starve reservations and sustained inventory work cannot starve uploads. Each timer callback begins at most one network operation and rearms for the earliest remaining due time.

Interactive lot refresh is serialized with queue traffic. It receives prompt scheduling after the current operation but cannot overlap a mutation or bypass configuration proof. Cancellation or configuration revocation prevents publication.

The UI coordinator performs only bounded local state validation and short SQLite transactions. It never performs HTTP. Worker results reach it through immutable, credential-free Qt signals. UI widgets are updated only in UI affinity.

## Strict response validation

List and mutation parsers reject unknown or missing fields, wrong JSON types, booleans used as integers, noncanonical UUIDs/timestamps, unsafe or oversized text, unknown enums, excessive nesting/items, non-finite values, and response-size violations.

Mutation verification additionally requires:

- expected client reservation, client instance, roast, and lot UUIDs;
- the operation's expected reservation state;
- returned actual/planned quantities consistent with the immutable command;
- `available_grams == on_hand_grams - reserved_grams` within bounds;
- matching lot identity between reservation, balance, and conflict;
- an open conflict tied to the returned reservation/roast when present;
- `open_conflict_id` agreement between reservation and conflict;
- no impossible completion timestamps or state relationships.

A verified response atomically completes the command and updates local state. A response from a revoked configuration generation is discarded before SQLite mutation.

Inventory may strictly recognize only the server's exact fixed status/code/message triples for safe classification. Arbitrary server bodies, HTML, proxy messages, paths, tracebacks, and details never cross into logs, storage, signals, or UI.

## Failure classification

- Connection failures, transient TLS failures, total-deadline expiry, `429`, `500`, `502`, `503 inventory_unavailable`, and `504` retry with persisted exponential backoff and bounded valid `Retry-After` handling.
- `401` or `403` revokes online proof and pauses profile and inventory delivery for credential or authorization repair without deleting durable work.
- Exact `bean_lot_not_found`, `bean_lot_archived`, `invalid_inventory_transition`, and `inventory_reservation_not_found` responses become visible terminal command failures.
- Exact `inventory_idempotency_conflict` is a prominent terminal integrity failure and cannot be automatically replaced with a new key.
- `422 invalid_request`, malformed success responses, relationship mismatches, and other fixed-contract violations become visible terminal failures.
- A generic route-level `404` or otherwise recognizable older-server response pauses inventory as **Server does not support inventory** while profile uploads continue.
- A valid mutation response containing a conflict is delivery success, not command failure. The UI shows the resulting negative balance and open-conflict warning.

A malformed response after a request may represent an ambiguous committed server operation. Automatic retries stop, but **Retry same command** reuses the exact immutable body and idempotency key. The server therefore replays a committed result instead of applying the mutation twice. The UI never offers “replace key” for such a command.

## Interrupted-reservation recovery

On startup, the coordinator finds nonterminal reservations lacking finalize/release intent after an unclean process exit. It shows a persistent **Interrupted reservations** notice and a recovery dialog. Each entry displays the namespace, roast, lot, planned grams, known server state, and last command status.

The operator chooses exactly one action:

- **Finalize using planned weight** queues finalize without actual grams.
- **Release** queues release.
- **Keep pending** leaves the reservation unchanged for later recovery.

The client never guesses whether coffee was consumed. Recovery choices are durable and obey reserve-before-terminal ordering even if reserve never reached the server. Pending interrupted reservations do not block unrelated roasts, but remain visible until resolved. A reservation from an inactive namespace can be inspected and kept pending; delivery resumes only after configuring and authenticating that exact namespace.

## Older-server and offline behavior

Failure or absence of inventory support never disables profile upload, archive browsing, ordinary roasting, or profile save. Inventory controls show a fixed unavailable state. Existing cached lots remain visible with their refresh timestamp, but a selected lot can create a new offline reservation only when it belongs to the current previously authenticated namespace.

Disabling Roast Server pauses delivery while retaining cache, commands, and recovery state. Removing credentials does the same. Re-enabling and proving the same namespace resumes eligible work.

## UI status and accessibility

Normal queued, retrying, offline, and cached states use nonmodal status text. Modal or prominent messages are reserved for:

- selected-lot weight or namespace validation that blocks CHARGE;
- local durability failure;
- terminal server rejection or integrity failure;
- open inventory conflict;
- interrupted-reservation recovery.

All inventory text uses `QApplication.translate()` with stable contexts, plain text, accessible names, keyboard-accessible actions, and existing platform conventions. Server and profile strings are never interpreted as rich text.

The existing Roast Server configuration/status UI includes aggregate inventory command counts and access to failed/interrupted items. It does not expose request bodies, credentials, arbitrary server messages, or destructive bulk deletion.

## Security and privacy

- No new credential storage exists; inventory uses the established OS-keyring credential only in worker affinity.
- Authorization, token values/hashes, and request bodies never enter profile data, QSettings, SQLite diagnostics, logs, exceptions, or Qt signals.
- Profile fields contain only public namespace and lot identifiers/name.
- TLS verification remains mandatory, redirects disabled, proxies disabled, and response reads bounded.
- SQLite and side files remain inside the existing private connector application-data boundary with restrictive permissions and link/reparse-point defenses.
- Configuration generation and namespace are verified at every network and commit boundary.
- User/server text is bounded, normalized where required, and displayed as plain text.

## Automated testing

### Contract and API tests

- Canonical UUID, UTC timestamp, grams, enum, text, JSON, and idempotency-key boundaries.
- Exact lot-page and mutation parsing, including missing/extra fields and bool-as-int rejection.
- Pagination limits, cursor loops, duplicate lots, partial failure, old-cache retention, and configuration cancellation.
- Request paths, methods, headers, canonical bodies, response limits, redirects, no-proxy behavior, and token redaction.
- Every retry, pause, unsupported-server, terminal, ambiguous-response, and conflict classification.
- Identity and arithmetic relationship checks for successful mutations.

### Store tests

- Exact fresh schema and migration/fingerprint rejection.
- Namespace isolation and profile namespace binding.
- Atomic cache generation replacement and rollback.
- CHARGE transaction durability, duplicate enqueue, immutable bodies/keys, and reserve-before-terminal dependency.
- One terminal intent, lease fencing, lease expiry, retry persistence, pause/resume, pruning, and tamper rejection.
- Atomic command/reservation/balance/conflict update.
- Clean and unclean shutdown recovery across process restarts.
- Filesystem permissions and platform-marked Windows ACL/reparse behavior consistent with existing connector stores.

### Lifecycle tests

- No-lot warning without CHARGE blocking.
- Selected-lot invalid weight and unavailable namespace blocking before event insertion.
- Local persistence failure preventing CHARGE.
- Offline CHARGE success after durable enqueue.
- Lot locking, CHARGE undo, repeated CHARGE UUID reuse, and no duplicate reserve.
- Beans populated only when empty; title/weight untouched; clear does not clear Beans.
- Save/Save As/autosave finalization once; Save Copy excluded.
- Valid actual weight and invalid-actual planned-weight fallback.
- Abort/reset/shutdown release, canceled reset no-op, and retained unlocked selection.
- Profile old/new round trips, historical lock state, and cross-namespace unavailability.
- All interrupted-recovery choices.

### Worker and Qt tests

- One-operation serialization and fair scheduling under sustained work in both queues.
- Retry timer chooses the earliest due item and starts at most one operation per callback.
- Configuration fence discards stale refresh and mutation responses.
- Signals contain immutable public data and execute UI updates only in UI affinity.
- Wrong-thread worker calls fail before SQLite, keyring, HTTP, or filesystem access.
- Startup, delayed shutdown, store closure, and zero surviving worker/timer handles.
- Search, selection, refresh, cached/offline indicators, keyboard access, and fixed error rendering.

### Coexistence and regression

- Existing Roast Server uploads, archive cache, settings, credentials, and read-only download behavior remain unchanged.
- artisan.plus coffee/blend selection, UUID registration, queue/cache, and synchronization remain unchanged.
- Existing profiles without inventory fields load and save normally.
- No automated test contacts the deployed server or requires roasting hardware.

## Manual acceptance

Use a disposable local or staging server organization, never production inventory history:

1. Create active lots with positive, zero, and negative availability and refresh the chooser.
2. Select a lot and confirm Beans is filled only when empty.
3. Verify missing/invalid green weight blocks CHARGE only when a lot is selected.
4. CHARGE online and offline; confirm recording remains responsive and the lot locks.
5. Undo and re-mark CHARGE; verify the same roast/reservation UUID and one reserve command.
6. Save and verify exactly one finalized deduction with actual or planned grams as appropriate.
7. Abort/reset and verify release plus retained unlocked selection.
8. Restart at reserve-before-send, reserve-after-send, and post-roast/pre-save cuts; exercise all recovery choices.
9. Force delayed over-consumption and verify successful delivery plus visible conflict and negative balance.
10. Change origin/organization during list and mutation requests; confirm stale responses are discarded and namespaces do not cross.
11. Point to an older server without inventory routes; confirm profile upload and ordinary roasting continue.
12. Run a no-hardware Qt smoke on supported macOS, Windows, and Linux environments.

## Rollout

Inventory tracking requires no separate global feature flag. It is dormant unless Roast Server is enabled and the operator explicitly selects a current-namespace lot. On first use, the worker creates/migrates the inventory store and refreshes active lots after normal Roast Server identity proof.

The implementation must land through focused TDD steps: contracts, persistence, API methods, coordinator state machine, worker scheduling, profile serialization, Roast Properties UI, recovery UI, and integration/regression gates. No production inventory mutation is part of automated or release validation.
