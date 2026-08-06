# Artisan Desktop Inventory Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add namespace-safe green-coffee lot selection and durable reserve/finalize/release inventory tracking to Artisan's existing self-hosted Roast Server connector.

**Architecture:** Add strict inventory contracts, a dedicated SQLite cache/command journal, and a UI-thread lifecycle coordinator under `artisanlib.roastserver`. Reuse the existing credential, configuration fence, synchronous HTTP client, one worker/QThread, retry timer, and controller; keep inventory commands independent from the profile outbox and integrate only narrow profile, Roast Properties, CHARGE, save, reset, recovery, and shutdown seams.

**Tech Stack:** Python 3.12+, PyQt6 `QObject`/`QThread`/Qt models and widgets, `requests`, stdlib `sqlite3`/`json`/`decimal`/`uuid`/`pathlib`, existing Artisan profile validation and weight conversion, pytest, Ruff, mypy, pyright, pylint, codespell.

## Global Constraints

- Implement against deployed Artisan Server commit `5dd3c751b24f5247e2a175ecc71ff3bb2304451f`; do not modify the server repository.
- Follow `docs/superpowers/specs/2026-08-05-artisan-desktop-inventory-connector-design.md` and preserve its approved CHARGE-undo correction.
- Use Python 3.12 or newer and only dependencies already pinned in `src/requirements.txt`; add no dependency and update no pin.
- Use the existing Roast Server bearer credential, canonical origin, organization identity, client instance UUID, no-proxy session, redirect rejection, TLS verification, response limits, and 12-second total operation deadline.
- Use exact integer grams in `1..2_147_483_647`; convert through Artisan's existing weight conversion and round an exact half gram upward.
- Store optional profile keys exactly as `roastServerInventoryOrigin`, `roastServerInventoryOrganizationUUID`, `roastServerBeanLotUUID`, and `roastServerBeanLotName`.
- Namespace every lot, reservation, command, and recovery record by canonical server origin plus confirmed organization UUID; never remap work after configuration changes.
- Reserve must be durable before CHARGE completes. Finalize/release may be queued before reserve delivery but cannot be leased before reserve succeeds. One reservation has at most one terminal intent.
- Undoing CHARGE retains the same roast UUID, reservation UUID, planned grams, and locked lot. Only accepted reset, abort, or shutdown queues release.
- No network request may block the UI or roasting hardware path. Short full-synchronous SQLite writes required for CHARGE/reset are allowed in UI affinity; all HTTP remains in the existing worker thread.
- Do not import or mutate `plus` from `src/artisanlib/roastserver/**`; preserve all artisan.plus selection, queue, cache, credentials, UUID registration, and sync behavior.
- Add code-built widgets only. Use `QApplication.translate(context, text)` and plain text for visible strings; do not edit generated `src/uic/**`, translations, help derivatives, protobuf outputs, dependencies, packaging, or release metadata.
- Automated tests use fake HTTP, temporary storage, fake credentials/workers, and offscreen Qt only. They must not contact an external endpoint, cloud account, keyring daemon, or roasting hardware.
- Match neighboring AGPLv3+ headers, single-quoted Python style, complete annotations, narrow exception boundaries, and precise qualified ignores.
- Run Python tools from `src/` so `pyproject.toml` is authoritative.

---

## Server contract pinned for this plan

| Operation | Request | Success |
|---|---|---|
| Active lots | `GET /api/v1/inventory/bean-lots?limit=100[&cursor=<opaque-cursor>]` | `200`, exact `{items,next_cursor}` |
| Reserve | `POST /api/v1/inventory/reservations` | `201`, exact mutation projection |
| Finalize | `POST /api/v1/inventory/reservations/{reservation_uuid}/finalize` | `200`, exact mutation projection |
| Release | `POST /api/v1/inventory/reservations/{reservation_uuid}/release` | `200`, exact mutation projection |

Mutation requests use `Content-Type: application/json` and:

```text
Idempotency-Key: inventory-v1:<client-instance-uuidhex>:<reservation-uuidhex>:<operation>
```

Reserve JSON has `client_reservation_uuid`, `client_instance_uuid`, `roast_uuid`, `lot_id`, `planned_grams`, and canonical UTC `occurred_at`. Finalize has optional `actual_grams` and `occurred_at`. Release has `occurred_at`.

Recognize only the fixed status/code/message triples below from an envelope with the exact shape `{"error":{"code":"<allowlisted-code>","message":"<matching-fixed-message>","details":null}}`:

| Status | Code | Message |
|---|---|---|
| `404` | `bean_lot_not_found` | `Bean lot not found` |
| `409` | `bean_lot_archived` | `Bean lot archived` |
| `409` | `invalid_inventory_transition` | `Invalid inventory transition` |
| `409` | `inventory_idempotency_conflict` | `Idempotency key conflicts with an earlier request` |
| `404` | `inventory_reservation_not_found` | `Inventory reservation not found` |
| `503` | `inventory_unavailable` | `Inventory unavailable` |
| `422` | `invalid_request` | `Invalid request` |

`401`/`403` pause both connector queues. `429`, transport failures, deadlines, and retryable `5xx` retry. A generic route `404` pauses inventory as unsupported without pausing profile upload. Arbitrary error bodies are discarded.

## File structure

### Create

- `src/artisanlib/roastserver/inventory_contract.py` — strict wire models, profile-link parsing, weight conversion, canonical command builders, and fixed failure mapping.
- `src/artisanlib/roastserver/inventory_store.py` — exact SQLite schema, cache publication, roast lifecycle, commands, leasing, retries, recovery, and pruning.
- `src/artisanlib/roastserver/inventory.py` — UI-thread lifecycle coordinator with no widget imports.
- `src/artisanlib/roastserver/inventory_dialogs.py` — lot table/chooser and interrupted-reservation recovery dialog.
- `src/test/unitary/artisanlib/roastserver/test_inventory_contract.py`
- `src/test/unitary/artisanlib/roastserver/test_inventory_store.py`
- `src/test/unitary/artisanlib/roastserver/test_inventory_api.py`
- `src/test/unitary/artisanlib/roastserver/test_inventory.py`
- `src/test/unitary/artisanlib/roastserver/test_inventory_worker.py`
- `src/test/unitary/artisanlib/roastserver/test_inventory_controller.py`
- `src/test/unitary/artisanlib/roastserver/test_inventory_dialogs.py`

### Modify

- `src/artisanlib/roastserver/contract.py` — inventory-safe failure categories/messages.
- `src/artisanlib/roastserver/api.py` — four bounded inventory API methods and strict allowlisted inventory error parsing.
- `src/artisanlib/roastserver/worker.py` — inventory store lifecycle, refresh/mutation slots, fair shared scheduling, failure handling, and signals.
- `src/artisanlib/roastserver/controller.py` — coordinator/store ownership, public inventory façade, signals, configuration invalidation, and shutdown.
- `src/artisanlib/roastserver/dialogs.py` — aggregate inventory queue/failure/recovery access in Roast Server status.
- `src/artisanlib/roastserver/__init__.py` — stable public inventory exports only.
- `src/artisanlib/atypes.py` — four optional `ProfileData` fields.
- `src/artisanlib/canvas.py` — inventory profile state, CHARGE precommit, CHARGE-undo preservation, and reset release/selection retention.
- `src/artisanlib/main.py` — startup wiring, profile round trip, successful-save finalization, recovery presentation, and shutdown release.
- `src/artisanlib/roast_properties.py` — staged inventory selection row near Beans and weight.
- Existing focused tests in `test_worker.py`, `test_roastserver_controller.py`, `test_roastserver_dialogs.py`, `test_main.py`, `test_canvas.py`, and `test_coexistence.py` where regression behavior belongs.

## Shared interfaces and dependency direction

Dependencies flow:

```text
contract/origin/settings <- inventory_contract <- inventory_store <- inventory
                                               <- api             <- worker
inventory/worker/settings <- controller <- dialogs/main/canvas/roast_properties
```

`inventory_contract.py` produces:

```python
ProcessingMethod = Literal[
    'washed', 'natural', 'honey', 'pulped-natural', 'wet-hulled',
    'anaerobic', 'experimental', 'other',
]
InventoryOperation = Literal['reserve', 'finalize', 'release']
ReservationState = Literal['reserved', 'finalized', 'released']
LedgerOperation = Literal[
    'opening_balance', 'manual_adjustment', 'reservation',
    'reservation_release', 'consumption',
]

@dataclass(frozen=True, slots=True)
class InventoryProfileLink:
    namespace: Namespace
    lot_id: UUID
    lot_name: str

@dataclass(frozen=True, slots=True)
class BeanLot:
    lot_id: UUID
    name: str
    origin: str | None
    varietals: tuple[str, ...]
    processing_method: ProcessingMethod | None
    crop_year: int | None
    on_hand_grams: int
    reserved_grams: int
    available_grams: int
    unresolved_conflict_count: int

@dataclass(frozen=True, slots=True)
class InventoryBalance:
    lot_id: UUID
    on_hand_grams: int
    reserved_grams: int
    available_grams: int
    unresolved_conflict_count: int

@dataclass(frozen=True, slots=True)
class InventoryReservation:
    reservation_id: UUID
    client_reservation_uuid: UUID
    lot_id: UUID
    roast_uuid: UUID
    client_instance_uuid: UUID
    state: ReservationState
    planned_grams: int
    actual_grams: int | None
    reserved_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    open_conflict_id: UUID | None

@dataclass(frozen=True, slots=True)
class InventoryConflict:
    conflict_id: UUID
    lot_id: UUID
    source_ledger_entry_id: UUID
    roast_uuid: UUID | None
    reservation_id: UUID | None
    trigger_operation: LedgerOperation
    available_grams_snapshot: int
    state: Literal['open', 'resolved']
    resolution_note: str | None
    resolved_by_user_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime

@dataclass(frozen=True, slots=True)
class BeanLotPage:
    items: tuple[BeanLot, ...]
    next_cursor: str | None

@dataclass(frozen=True, slots=True)
class InventoryMutationResult:
    reservation: InventoryReservation
    balance: InventoryBalance
    conflict: InventoryConflict | None
    idempotent_replay: bool

@dataclass(frozen=True, slots=True)
class InventoryCommandRequest:
    operation: InventoryOperation
    reservation_uuid: UUID
    roast_uuid: UUID
    lot_id: UUID
    request_json: bytes
    idempotency_key: str
    occurred_at: datetime
```

It also produces `parse_bean_lot_page()`, `parse_inventory_mutation()`, `parse_inventory_error()`, `parse_profile_link()`, `profile_link_fields()`, `green_weight_grams()`, and `build_reserve_request()`/`build_finalize_request()`/`build_release_request()`.

`inventory_store.py` produces these immutable values and the exact methods named in Tasks 2–3:

```python
InventoryCommandState = Literal[
    'pending', 'leased', 'retry_wait', 'paused', 'failed', 'complete'
]
InventoryLifecycle = Literal[
    'reserve_queued', 'reserved', 'finalize_queued', 'finalized',
    'release_queued', 'released', 'paused', 'failed',
]

@dataclass(frozen=True, slots=True)
class InventoryCommand:
    id: str
    namespace: Namespace
    roast_uuid: UUID
    lot_id: UUID
    reservation_uuid: UUID
    operation: InventoryOperation
    request_json: bytes
    idempotency_key: str
    dependency_id: str | None
    state: InventoryCommandState
    attempts: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    lease_token: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class InventoryRoastState:
    namespace: Namespace
    roast_uuid: UUID
    lot_id: UUID
    lot_name: str
    reservation_uuid: UUID
    server_reservation_uuid: UUID | None
    planned_grams: int
    actual_grams: int | None
    lifecycle: InventoryLifecycle
    terminal_intent: Literal['finalize', 'release'] | None
    reserve_occurred_at: datetime
    finalize_occurred_at: datetime | None
    release_occurred_at: datetime | None
    server_state: ReservationState | None
    balance: InventoryBalance | None
    conflict_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class InventoryQueueCounts:
    pending: int
    retrying: int
    paused: int
    failed: int
    complete: int

@dataclass(frozen=True, slots=True)
class FailedInventoryCommand:
    id: str
    namespace: Namespace
    roast_uuid: UUID
    lot_id: UUID
    reservation_uuid: UUID
    operation: InventoryOperation
    attempts: int
    error_code: str
    error_message: str
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class InterruptedReservation:
    namespace: Namespace
    roast_uuid: UUID
    lot_id: UUID
    lot_name: str
    reservation_uuid: UUID
    planned_grams: int
    lifecycle: InventoryLifecycle
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class LotCacheSnapshot:
    namespace: Namespace
    lots: tuple[BeanLot, ...]
    refreshed_at: datetime | None
```

`inventory.py` produces:

```python
@dataclass(frozen=True, slots=True)
class PreparedInventoryCharge:
    tracked: bool
    namespace: Namespace | None
    roast_uuid: UUID | None
    reservation_uuid: UUID | None
    lot_id: UUID | None
    lot_name: str | None
    planned_grams: int | None
    existing: bool

@dataclass(frozen=True, slots=True)
class InventoryNotice:
    code: str
    roast_uuid: UUID | None
    reservation_uuid: UUID | None
    lot_id: UUID | None
    balance: InventoryBalance | None
    conflict_id: UUID | None

class InventoryCoordinatorError(RuntimeError):
    code: str
```

`InventoryCoordinator` receives immutable values; it never reads `ApplicationWindow`, `canvas`, widgets, QSettings, credentials, or plus globals.

The worker and UI coordinator use two `InventoryStore` instances pointing to the same private `data_root / 'inventory'`; each instance owns its SQLite connection. WAL and short explicit transactions coordinate access.

---

### Task 1: Add strict inventory contracts and deterministic command construction

**Files:**
- Create: `src/artisanlib/roastserver/inventory_contract.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory_contract.py`
- Modify: `src/artisanlib/roastserver/contract.py`
- Modify: `src/artisanlib/roastserver/__init__.py`

**Interfaces:**
- Consumes: `Namespace`, `FailureKind`, `PublicFailure`, `POSTGRESQL_INTEGER_MAX`, `settings.namespace_for()`, the exact-object/UUID/timestamp helpers already in `contract.py` when their bounds match, and `artisanlib.util.convertWeight`/`weight_units` only in `green_weight_grams()`.
- Produces: every `inventory_contract.py` type and function listed under “Shared interfaces.”

- [ ] **Step 1: Write failing strict parser, link, weight, and command tests**

Create payload factories for one lot and one mutation. Cover exact fields, bool-as-int rejection, lowercase 32-hex UUIDs, canonical six-digit UTC timestamps, all processing codes, unique bounded varietals, signed/on-hand and nonnegative/reserved bounds, arithmetic consistency, conflict relationships, duplicate page IDs, profile-link all-or-none behavior, namespace mismatch, every weight unit, `.5` upward rounding, zero/nonfinite/overflow rejection, immutable JSON bytes, and deterministic idempotency keys.

```python
def test_build_reserve_request_is_canonical_and_stable() -> None:
    request = build_reserve_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1250,
        occurred_at=NOW,
    )
    assert request.idempotency_key == (
        f'inventory-v1:{CLIENT_UUID.hex}:{RESERVATION_UUID.hex}:reserve'
    )
    assert request.request_json == (
        b'{"client_instance_uuid":"33333333333343338333333333333333",'
        b'"client_reservation_uuid":"44444444444444448444444444444444",'
        b'"lot_id":"22222222222242228222222222222222",'
        b'"occurred_at":"2026-08-05T12:00:00.000000Z",'
        b'"planned_grams":1250,'
        b'"roast_uuid":"11111111111141118111111111111111"}'
    )


def test_green_weight_rounds_half_up_after_existing_conversion() -> None:
    assert green_weight_grams(1.0005, 'Kg') == 1001
    assert green_weight_grams(0.0004, 'Kg') is None
```

- [ ] **Step 2: Run the new tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_contract.py -v
```

Expected: collection fails because `inventory_contract.py` does not exist.

- [ ] **Step 3: Implement frozen wire models and exact parsers**

Add inventory-specific safe categories without changing existing messages:

```python
class FailureKind(StrEnum):
    # existing members remain unchanged
    INVENTORY_REJECTED = 'inventory_rejected'
    INVENTORY_CONFLICT = 'inventory_conflict'
    INVENTORY_UNSUPPORTED = 'inventory_unsupported'
    LOCAL_INVENTORY = 'local_inventory'
```

Add fixed messages to `FAILURE_MESSAGES`. Define `MAX_INVENTORY_CURSOR_CHARS = 4096`, `MAX_INVENTORY_PAGES = 100`, and `MAX_CACHED_LOTS = 10_000`. In `inventory_contract.py`, use exact-key checks and private validators; enforce lot names at 200 code points/800 UTF-8 bytes, origins and each of at most 16 varietals at 100 code points/400 bytes, crop years in `1000..9999`, exact processing codes, `available_grams == on_hand_grams - reserved_grams`, conflict/open-ID agreement, and expected identities passed to `parse_inventory_mutation()`. Encode request payloads with `json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')`.

Implement weight conversion with decimal half-up semantics:

```python
def green_weight_grams(value: object, unit: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(float(value)) or float(value) <= 0 or unit not in weight_units:
        return None
    grams = convertWeight(float(value), weight_units.index(cast(str, unit)), 0)
    rounded = int(Decimal(str(grams)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return rounded if 1 <= rounded <= POSTGRESQL_INTEGER_MAX else None
```

`parse_inventory_error()` returns a `PublicFailure` only for the pinned exact envelope/status triples; return `None` for arbitrary bodies or a mismatched triple.

- [ ] **Step 4: Run focused and existing contract tests**

```bash
cd src
.venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_contract.py \
  test/unitary/artisanlib/roastserver/test_contract.py
.venv/bin/ruff check artisanlib/roastserver/inventory_contract.py \
  test/unitary/artisanlib/roastserver/test_inventory_contract.py
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/artisanlib/roastserver/{contract.py,inventory_contract.py,__init__.py} \
  src/test/unitary/artisanlib/roastserver/test_inventory_contract.py
git commit -m "feat(roastserver): add inventory contracts"
```

---

### Task 2: Create the exact inventory schema and atomic lot cache

**Files:**
- Create: `src/artisanlib/roastserver/inventory_store.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory_store.py`

**Interfaces:**
- Consumes: Task 1 `BeanLot`, `InventoryProfileLink`, `InventoryMutationResult`, `InventoryCommandRequest`, `Namespace`, and existing `_filesystem` private-root hardening patterns.
- Produces: `InventoryStore.open()`, `close()`, `replace_lots(namespace, lots, refreshed_at)`, `cached_lots(namespace)`, and `cache_snapshot(namespace)` plus frozen store dataclasses.

- [ ] **Step 1: Write failing schema, isolation, and cache-publication tests**

Test a fresh database, exact schema rejection after adding a rogue table/trigger/index, private permissions, two simultaneous store connections, namespace isolation, complete replacement, rollback on duplicate UUID/invalid lot, stale cache timestamps, deterministic ordering, and old-generation retention after an injected failure.

```python
def test_replace_lots_is_atomic_and_namespace_isolated(tmp_path: Path) -> None:
    store = opened_store(tmp_path / 'inventory')
    store.replace_lots(NAMESPACE, (lot(name='Old'),), NOW)
    with pytest.raises(InventoryStoreError):
        store.replace_lots(NAMESPACE, (lot(name='New'), lot(name='Duplicate')), LATER)
    assert [item.name for item in store.cached_lots(NAMESPACE)] == ['Old']
    assert store.cached_lots(OTHER_NAMESPACE) == ()
```

- [ ] **Step 2: Run the store tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_store.py -v
```

Expected: collection fails because `inventory_store.py` does not exist.

- [ ] **Step 3: Implement schema version 1 and strict fingerprinting**

Use these tables and constraints as one canonical statement tuple:

```text
schema_version(version=1)
namespaces(id, origin, organization_uuid, namespace_key; unique origin/org and key)
lot_cache_generations(namespace_id PK, generation 32-hex, refreshed_at)
bean_lots(namespace_id, generation, lot_uuid, name, origin, varietals_json,
          processing_method, crop_year, on_hand_grams, reserved_grams,
          available_grams, unresolved_conflict_count;
          PK namespace_id/lot_uuid; FK generation; arithmetic checks)
roast_inventory(namespace_id, roast_uuid, lot_uuid, lot_name,
                client_reservation_uuid, server_reservation_uuid,
                planned_grams, actual_grams,
                lifecycle, terminal_intent, reserve/finalize/release times,
                server_state, balance fields, conflict_uuid,
                error_code, error_message, created_at, updated_at;
                PK namespace_id/roast_uuid; unique namespace/reservation)
inventory_commands(id, namespace_id, roast_uuid, lot_uuid, reservation_uuid,
                   operation, request_json BLOB, idempotency_key, dependency_id,
                   state, attempts, next_attempt_at, lease_expires_at, lease_token,
                   error_code, error_message, created_at, updated_at, completed_at;
                   unique namespace/reservation/operation)
```

Enforce exact allowed states and nullability with SQL `CHECK` constraints. Configure `PRAGMA journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, and a bounded busy timeout. Use `BEGIN IMMEDIATE` for writes and a per-instance `RLock`. Reject every schema object not in the exact fingerprint.

- [ ] **Step 4: Implement atomic cache generation publication**

Validate all lots before `BEGIN IMMEDIATE`; insert a random generation, replace rows, and update `lot_cache_generations` in one transaction. Serialize varietals as canonical compact JSON and verify it again on reads. Return immutable tuples ordered casefolded name then UUID.

- [ ] **Step 5: Run store tests, two-connection probes, and Ruff**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_store.py -v
.venv/bin/ruff check artisanlib/roastserver/inventory_store.py \
  test/unitary/artisanlib/roastserver/test_inventory_store.py
```

Expected: all pass and no connection/thread warning appears.

- [ ] **Step 6: Commit**

```bash
git add src/artisanlib/roastserver/inventory_store.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_store.py
git commit -m "feat(roastserver): add inventory cache store"
```

---

### Task 3: Implement durable reservation and command-journal transitions

**Files:**
- Modify: `src/artisanlib/roastserver/inventory_store.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_store.py`

**Interfaces:**
- Consumes: Task 2 schema and Task 1 command/result objects.
- Produces the exact operations:

```python
enqueue_reserve(namespace, request, lot_name, now) -> InventoryRoastState
enqueue_finalize(namespace, request, actual_grams, now) -> InventoryRoastState
enqueue_release(namespace, request, now) -> InventoryRoastState
lease_next(namespace, now, lease_seconds) -> InventoryCommand | None
next_due_at(namespace) -> datetime | None
mark_complete(command_id, lease_token, result, now) -> InventoryRoastState
mark_retry(command_id, lease_token, now, next_attempt_at, failure) -> None
mark_paused(command_id, lease_token, now, failure) -> None
mark_failed(command_id, lease_token, now, failure) -> None
recover_expired_leases(now) -> int
pause_namespace(namespace, now, code) -> int
resume_namespace(namespace, now) -> int
retry_same(command_id, now) -> None
counts(namespace) -> InventoryQueueCounts
failed_commands(namespace) -> tuple[FailedInventoryCommand, ...]
interrupted_reservations() -> tuple[InterruptedReservation, ...]
roast_state(namespace, roast_uuid) -> InventoryRoastState | None
```

- [ ] **Step 1: Add failing lifecycle, ordering, lease, and recovery tests**

Cover duplicate reserve idempotence, same roast/different immutable body rejection, finalize/release mutual exclusion, terminal-before-reserve queueing, lease eligibility, lease token CAS and expiry, reserve failure cascading a fixed dependency failure, atomic response/cache/conflict update, retry persistence, credential pause/resume, manual same-command retry, restart recovery, interruption discovery, completed-history pruning, and two-store races.

```python
def test_terminal_command_waits_for_reserve_and_only_one_intent_wins(store: InventoryStore) -> None:
    reserve = store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    store.enqueue_finalize(NAMESPACE, FINALIZE_REQUEST, 1200, NOW)
    with pytest.raises(InventoryStoreError, match='terminal intent'):
        store.enqueue_release(NAMESPACE, RELEASE_REQUEST, NOW)
    leased = store.lease_next(NAMESPACE, NOW, 30)
    assert leased is not None and leased.operation == 'reserve'
    assert store.lease_next(NAMESPACE, NOW, 30) is None
```

- [ ] **Step 2: Run journal tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_store.py \
  -k 'command or reservation or lease or retry or interrupted' -v
```

Expected: failures identify missing methods.

- [ ] **Step 3: Implement immutable enqueue and dependency rules**

Validate the command request against namespace/roast/lot/reservation state before opening the transaction. Insert reserve plus roast state atomically. Insert exactly one terminal command with `dependency_id` set to the reserve command. A terminal command is leaseable only when its dependency is `complete`; `mark_failed()` on reserve atomically fails a queued dependent command with `dependency_failed`.

- [ ] **Step 4: Implement fenced leasing and atomic verified completion**

Every lease creates a random 32-hex token and increments attempts. Completion/retry/pause/fail must update only `WHERE id=? AND state='leased' AND lease_token=? AND lease_expires_at>?`; otherwise raise `InventoryStoreError('lease_lost')`. On success, update the command, roast state, matching cached lot balance, and conflict in one transaction after revalidating the Task 1 result.

- [ ] **Step 5: Implement restart, aggregates, manual retry, and bounded pruning**

Expired leases become ready without changing immutable bodies. `interrupted_reservations()` returns nonterminal roast states with no terminal intent. `retry_same()` may move only `failed` commands back to pending and preserves request bytes/key. Prune completed commands only after a terminal roast state and a 30-day retention boundary; never prune interrupted or failed records.

- [ ] **Step 6: Run the full store module**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_store.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/roastserver/inventory_store.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_store.py
git commit -m "feat(roastserver): journal inventory commands"
```

---

### Task 4: Add bounded inventory HTTP methods and fixed error classification

**Files:**
- Modify: `src/artisanlib/roastserver/api.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory_api.py`

**Interfaces:**
- Consumes: Task 1 parsers/builders and existing `_DeadlineGuard`, `_request()`, bounded response reader, retry-after parser, and session hardening.
- Produces:

```python
RoastServerClient.list_inventory_lots(cursor: str | None = None, limit: int = 100) -> BeanLotPage
RoastServerClient.execute_inventory_command(request: InventoryCommandRequest) -> InventoryMutationResult
```

- [ ] **Step 1: Write failing request/response and failure tests**

Use the existing recording-session style with the autouse network guard. Assert exact method/path/query/body/content length/content type/idempotency header, success status sets, no redirects/proxies/cookies, complete response closure, size bounds, strict parser calls, fixed exact errors, generic route `404` unsupported, malformed success invalid response, `401`/`403` credential pause, `429`/`5xx` retry, and token/body absence from `repr` and failures.

```python
def test_finalize_uses_immutable_body_and_idempotency_header(session: RecordingSession) -> None:
    session.respond(200, mutation_payload(state='finalized'))
    result = client(session).execute_inventory_command(FINALIZE_REQUEST)
    call = session.calls.single()
    assert call.path == f'/api/v1/inventory/reservations/{RESERVATION_UUID.hex}/finalize'
    assert call.headers['Idempotency-Key'] == FINALIZE_REQUEST.idempotency_key
    assert call.data == FINALIZE_REQUEST.request_json
    assert result.reservation.state == 'finalized'
```

- [ ] **Step 2: Run API tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_api.py -v
```

Expected: methods are absent.

- [ ] **Step 3: Implement list and mutation methods inside `_run_operation()`**

Validate cursor length against the server's 4096-character input bound and local 100-page loop boundary; use `limit=100`. Build mutation paths only from operation plus canonical reservation UUID. Pass immutable JSON bytes to `_request()` and add `Idempotency-Key` through a new narrowly validated optional header argument; reject any caller-supplied authorization/content headers.

- [ ] **Step 4: Implement operation-specific error parsing**

For non-success inventory responses, read at most the existing JSON ceiling under the same deadline, call `parse_inventory_error(status, body)`, and otherwise classify by status. A generic `404` becomes fixed `INVENTORY_UNSUPPORTED`; the exact `bean_lot_not_found` remains terminal `INVENTORY_REJECTED`. Do not change archive/upload response-body discard behavior.

- [ ] **Step 5: Run inventory and existing API suites**

```bash
cd src
.venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_api.py \
  test/unitary/artisanlib/roastserver/test_api.py
.venv/bin/ruff check artisanlib/roastserver/api.py \
  test/unitary/artisanlib/roastserver/test_inventory_api.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/artisanlib/roastserver/api.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_api.py
git commit -m "feat(roastserver): add inventory API client"
```

---

### Task 5: Implement the UI-thread inventory lifecycle coordinator

**Files:**
- Create: `src/artisanlib/roastserver/inventory.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory.py`

**Interfaces:**
- Consumes: Task 1 links/builders/weight conversion, Task 3 `InventoryStore`, existing `ConnectorSettings`/`Namespace`, and injected `clock`, `uuid_factory`, and wake callback.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class InventoryContext:
    origin: str
    namespace: Namespace | None
    enabled: bool
    previously_authenticated: bool
    client_instance_uuid: UUID

@dataclass(frozen=True, slots=True)
class PreparedInventoryCharge:
    tracked: bool
    namespace: Namespace | None
    roast_uuid: UUID | None
    reservation_uuid: UUID | None
    lot_id: UUID | None
    lot_name: str | None
    planned_grams: int | None
    existing: bool

InventoryCoordinator.prepare_charge(
    context: InventoryContext,
    link: InventoryProfileLink | None,
    roast_uuid: UUID | None,
    green_weight: object,
    weight_unit: object,
) -> PreparedInventoryCharge
InventoryCoordinator.commit_charge(
    prepared: PreparedInventoryCharge,
) -> InventoryNotice
InventoryCoordinator.finalize_saved_profile(
    context: InventoryContext,
    profile: ProfileData,
) -> InventoryNotice | None
InventoryCoordinator.release_for_reset(
    context: InventoryContext,
    roast_uuid: UUID | None,
) -> InventoryNotice | None
InventoryCoordinator.resolve_interrupted(
    context: InventoryContext,
    roast_uuid: UUID,
    action: Literal['finalize', 'release', 'keep'],
) -> InventoryNotice
InventoryCoordinator.is_locked(
    namespace: Namespace,
    roast_uuid: UUID | None,
    profile_has_charge: bool,
) -> bool
```

- [ ] **Step 1: Write failing pure lifecycle tests**

Test no-lot untracked warning, selected+disabled blocking, stale namespace blocking, cached-lot requirement, offline known namespace allowance, invalid weight blocking, generated UUIDs, repeated preparation/commit reuse, finalize once, actual weight/fallback planned warning, release once, terminal exclusivity, loaded historical lock, cross-install save no-op, recovery actions, wake only after successful transactions, and fixed safe exceptions.

```python
def test_undo_and_recharge_reuse_existing_reservation(coordinator: InventoryCoordinator) -> None:
    first = coordinator.prepare_charge(CONTEXT, LINK, None, 1.25, 'Kg')
    coordinator.commit_charge(first)
    second = coordinator.prepare_charge(CONTEXT, LINK, first.roast_uuid, 9.0, 'Kg')
    assert second.existing
    assert second.reservation_uuid == first.reservation_uuid
    assert second.planned_grams == 1250
```

- [ ] **Step 2: Run coordinator tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory.py -v
```

Expected: collection fails because `inventory.py` does not exist.

- [ ] **Step 3: Implement validation/preparation without side effects**

`prepare_charge()` returns an untracked preparation when no link exists. It raises fixed `InventoryCoordinatorError` codes for disabled connector, inactive/mismatched namespace, absent cached lot, and invalid weight. For an existing nonterminal roast state, it returns its immutable reservation/planned quantity without generating IDs or rebuilding a command.

- [ ] **Step 4: Implement durable commit, save, reset, and recovery actions**

`commit_charge()` builds and stores reserve before returning. `finalize_saved_profile()` requires matching namespace-bound profile link, canonical roast UUID, CHARGE at `timeindex[0] != -1`, and active local state; it captures actual grams if valid or omits them and returns `planned_weight_used`. `release_for_reset()` queues release only with no terminal intent. Recovery accepts only literal `finalize`, `release`, or `keep`.

- [ ] **Step 5: Run focused tests and static checks**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory.py -v
.venv/bin/ruff check artisanlib/roastserver/inventory.py \
  test/unitary/artisanlib/roastserver/test_inventory.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/artisanlib/roastserver/inventory.py \
  src/test/unitary/artisanlib/roastserver/test_inventory.py
git commit -m "feat(roastserver): coordinate inventory lifecycle"
```

---

### Task 6: Open the inventory store in the worker and schedule both queues fairly

**Files:**
- Modify: `src/artisanlib/roastserver/worker.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory_worker.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_worker.py`

**Interfaces:**
- Consumes: Task 3 worker-side `InventoryStore`; existing `Outbox`, `ConfigurationFence`, `WorkerConfiguration`, and single-shot timer.
- Produces worker constructor argument `inventory_store: InventoryStore`, inventory store lifecycle, and fair queue selection while preserving `process_queue_once()`.

- [ ] **Step 1: Write failing startup/shutdown and fairness tests**

Use fake outbox/inventory stores with due times and lease recordings. Cover open/recover order, partial-open cleanup, closing both stores before `stopped`, earliest due selection, tie alternation that explicitly alternates queue class, one lease/network operation per callback, sustained non-starvation, minimum next-due timer, stale configuration before lease, and pause/resume of both stores.

```python
def test_equal_due_times_alternate_queue_class(worker: RoastServerWorker) -> None:
    worker._outbox.next_due_at.return_value = NOW  # test-owned fakes
    worker._inventory_store.next_due_at.return_value = NOW
    run_ticks(worker, 4)
    assert worker.queue_classes_leased == [
        'profile', 'inventory', 'profile', 'inventory'
    ]
```

- [ ] **Step 2: Run worker tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_worker.py -v
```

Expected: constructor/signaling assertions fail.

- [ ] **Step 3: Add inventory store lifecycle and dual-store configuration actions**

Open/recover inventory immediately after outbox and before cache publication is considered started. On configuration disable/credential failure/namespace change, pause both stores with the same fixed reason. On authorization, resume both under one operation permit before arming the timer. Stop closes cache, inventory, and outbox deterministically and reports local inventory errors without leaving the thread alive.

- [ ] **Step 4: Replace single-store due selection with explicit fair selection**

Add a private queue-class enum and `_last_queue_class`. Compare `outbox.next_due_at()` and `inventory_store.next_due_at()`. Earlier due wins; exact ties choose the class not chosen on the prior tie. Acquire the existing `lease_next` permit immediately before leasing the selected store. A callback delivers only that lease and then rearms from the minimum next due.

- [ ] **Step 5: Run focused worker suites**

```bash
cd src
.venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_worker.py \
  test/unitary/artisanlib/roastserver/test_worker.py
```

Expected: all pass, including original timer and shutdown tests.

- [ ] **Step 6: Commit**

```bash
git add src/artisanlib/roastserver/worker.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_worker.py \
  src/test/unitary/artisanlib/roastserver/test_worker.py
git commit -m "feat(roastserver): schedule inventory commands fairly"
```

---

### Task 7: Deliver inventory commands and publish complete lot refreshes

**Files:**
- Modify: `src/artisanlib/roastserver/worker.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_worker.py`

**Interfaces:**
- Consumes: Task 4 API methods and Task 3 command/cache transitions.
- Produces worker slots/signals:

```python
inventoryLotsChanged = pyqtSignal(object)
inventoryQueueChanged = pyqtSignal(object)
inventoryFailedChanged = pyqtSignal(object)
inventoryReservationChanged = pyqtSignal(object)
inventoryRecoveryChanged = pyqtSignal(object)

Worker slots added by this task are `refresh_inventory(self, opaque_id: str) -> None`, `retry_inventory_command(self, command_id: str) -> None`, and `wake_inventory(self) -> None`. The first consumes a vault request; the latter two accept only generated command IDs or no payload.
```

- [ ] **Step 1: Add failing refresh, delivery, stale-response, and failure tests**

Cover all pages, 100-page/10,000-lot limits, cursor cycle/duplicate lot/partial error old-cache retention, permit boundaries, response identity validation, atomic success, retry delays, `Retry-After`, 401/403 dual-queue pause, unsupported inventory-only pause, terminal exact errors, ambiguous malformed success, manual retry same bytes/key, successful conflict emission, and configuration revocation after HTTP but before commit.

```python
def test_revoked_mutation_response_cannot_commit(worker_harness: WorkerHarness) -> None:
    worker_harness.api.on_execute = worker_harness.revoke_configuration
    worker_harness.run_inventory_tick()
    assert worker_harness.inventory_store.command(COMMAND_ID).state == 'leased'
    assert worker_harness.signals.inventoryReservationChanged == []
```

- [ ] **Step 2: Run delivery tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_worker.py \
  -k 'refresh or delivery or failure or conflict or revoked' -v
```

Expected: missing slots and delivery branches fail.

- [ ] **Step 3: Implement bounded sequential refresh under one generation**

Consume a vault-backed refresh request containing the exact namespace and generation. Fetch `limit=100` pages sequentially, checking cancellation/configuration after every page. Reject repeated cursors, duplicate UUIDs, page 101, or item 10,001. Only then call `replace_lots()` and emit its immutable snapshot. Preserve old cache on every failure.

- [ ] **Step 4: Implement one-call command execution and transition classification**

Open one client per command, acquire an operation permit named `inventory_<operation>`, call `execute_inventory_command()`, recheck generation, and atomically complete. Persist retry delay through existing `_retry_delay()`. `401`/`403` pause both stores and clear authorization. `INVENTORY_UNSUPPORTED` pauses only inventory commands. Fixed terminal/ambiguous failures use `mark_failed()`; manual retry preserves body/key. Emit conflict as successful reservation state plus a fixed prominent failure category.

- [ ] **Step 5: Emit combined inventory aggregates at every state boundary**

After startup, configuration, `wake_inventory`, refresh, delivery, retry, recovery choice, and failure, emit queue counts, failed commands, interrupted reservations, and changed reservation/cache snapshots. `wake_inventory()` emits aggregates and schedules the earliest due class so a UI-side SQLite enqueue cannot remain idle behind a stopped timer. Signals contain frozen public values only.

- [ ] **Step 6: Run inventory worker and API/store suites**

```bash
cd src
.venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_worker.py \
  test/unitary/artisanlib/roastserver/test_inventory_api.py \
  test/unitary/artisanlib/roastserver/test_inventory_store.py
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/roastserver/worker.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_worker.py
git commit -m "feat(roastserver): deliver inventory operations"
```

---

### Task 8: Wire coordinator and inventory signals through the controller

**Files:**
- Modify: `src/artisanlib/roastserver/controller.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory_controller.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_roastserver_controller.py`

**Interfaces:**
- Consumes: Tasks 3, 5, and 7; existing settings proof/generation/vault patterns.
- Produces controller signals and façade methods used by main/dialogs:

```python
inventoryLotsChanged = pyqtSignal(object)
inventoryStateChanged = pyqtSignal(object)
inventoryQueueChanged = pyqtSignal(object)
inventoryFailedChanged = pyqtSignal(object)
inventoryRecoveryRequired = pyqtSignal(object)
inventoryConflict = pyqtSignal(object)

inventory_context() -> InventoryContext
inventory_lots() -> tuple[BeanLot, ...]
refresh_inventory_lots() -> str
prepare_inventory_charge(
    link: InventoryProfileLink | None,
    roast_uuid: UUID | None,
    weight: object,
    unit: object,
) -> PreparedInventoryCharge
commit_inventory_charge(prepared: PreparedInventoryCharge) -> InventoryNotice
finalize_inventory_profile(profile: ProfileData) -> InventoryNotice | None
release_inventory_roast(roast_uuid: UUID | None) -> InventoryNotice | None
resolve_interrupted_inventory(
    roast_uuid: UUID,
    action: Literal['finalize', 'release', 'keep'],
) -> InventoryNotice
retry_inventory_command(command_id: str) -> None
```

- [ ] **Step 1: Write failing controller ownership and fencing tests**

Test UI store opens before worker start, separate worker store same root, startup recovery emission, context for enabled/offline/disabled states, refresh request vault/generation, façade delegation, configuration changes clearing stale refresh tracking, inventory commands retained across origin changes, signal forwarding, controller shutdown closing UI store after worker, and no secret/request bytes in signals.

- [ ] **Step 2: Run controller tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_inventory_controller.py -v
```

Expected: inventory constructor and façade members are absent.

- [ ] **Step 3: Add store/coordinator factories and worker wiring**

Add injectable `inventory_store_factory` and `inventory_coordinator_factory`. Build two store instances at `root / 'inventory'`; pass one to the worker and one to the coordinator. Add `_wakeInventoryWorker = pyqtSignal()` connected to `worker.wake_inventory`. Supply that signal's emitter as the coordinator wake callback. Open the UI store in `start()`, emit cached/recovery state, and close it only after worker shutdown completes. Preserve current fake worker compatibility by updating all constructor assertions explicitly.

- [ ] **Step 4: Implement context and public lifecycle façade**

Derive context only from current settings, persisted/confirmed identity, proof, and client UUID. Previously authenticated offline namespaces remain queueable; explicit disable remains disabled. Catch only `InventoryCoordinatorError`/`InventoryStoreError`, translate them to fixed public controller errors/notices, and never catch arbitrary widget errors here.

- [ ] **Step 5: Wire worker inventory signals and generation tracking**

Use queued connections. Accept lot/mutation signals only for the current generation/namespace, then refresh coordinator-visible snapshots. Keep failed/interrupted old-namespace records inspectable but never actionable under a different context.

- [ ] **Step 6: Run controller regressions**

```bash
cd src
.venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_controller.py \
  test/unitary/artisanlib/roastserver/test_roastserver_controller.py
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/roastserver/controller.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_controller.py \
  src/test/unitary/artisanlib/roastserver/test_roastserver_controller.py
git commit -m "feat(roastserver): expose inventory controller"
```

---

### Task 9: Round-trip namespace-bound lot links through profiles

**Files:**
- Modify: `src/artisanlib/atypes.py`
- Modify: `src/artisanlib/canvas.py`
- Modify: `src/artisanlib/main.py`
- Modify: `src/test/unitary/artisanlib/test_main.py`

**Interfaces:**
- Consumes: Task 1 `parse_profile_link()`/`profile_link_fields()`.
- Produces four optional `ProfileData` keys and matching `qmc` attributes; no reservation is created while loading.

- [ ] **Step 1: Add failing old/new/copy/read-only profile tests**

Test old profile load with all fields absent, exact new field save/load, incomplete/malformed link dropping safely with fixed warning, profile from another namespace remaining historical, Save Copy preserving the link but not invoking finalization, and Roast Server read-only save-state rollback preserving all four qmc values.

```python
def test_inventory_profile_fields_round_trip_without_affecting_plus(window: ApplicationWindow) -> None:
    set_qmc_inventory_link(window.qmc, LINK)
    profile = window.getProfile(generate_hash=False)
    assert profile['roastServerBeanLotUUID'] == LOT_UUID.hex
    assert window.setProfile('profile.alog', profile)
    assert qmc_inventory_link(window.qmc) == LINK
    window.plusAddPath.assert_not_called()
```

- [ ] **Step 2: Run focused main tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/test_main.py -k 'inventory_profile' -v
```

Expected: missing TypedDict/qmc fields or assertions fail.

- [ ] **Step 3: Add typed qmc state and serialization**

Add four `str | None` canvas attributes and slot declarations. In `getProfile()`, emit the exact four keys only when `parse_profile_link()` validates the complete qmc set. In `setProfile()`, clear prior values first, parse the four fields, and restore a valid link without consulting network/cache. Add the fields to read-only save snapshot/rollback lists.

- [ ] **Step 4: Keep copy/recent/template boundaries explicit**

Do not add inventory fields to `RecentRoast`; recent templates must not auto-select lots. Save Copy includes the historical profile fields in bytes but the existing `copy=True` branch remains excluded from inventory finalization. Do not change plus keys or hash behavior beyond naturally hashing the new optional fields.

- [ ] **Step 5: Run main/profile regression tests**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_main.py -k 'inventory_profile or RoastServerReadOnly'
.venv/bin/ruff check artisanlib/atypes.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/artisanlib/{atypes.py,canvas.py,main.py} \
  src/test/unitary/artisanlib/test_main.py
git commit -m "feat(profile): persist inventory lot link"
```

---

### Task 10: Build the cached lot chooser and Roast Properties row

**Files:**
- Create: `src/artisanlib/roastserver/inventory_dialogs.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_inventory_dialogs.py`
- Modify: `src/artisanlib/roast_properties.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_coexistence.py`

**Interfaces:**
- Consumes: Task 8 controller façade/signals and Task 9 qmc profile link.
- Produces `BeanLotTableModel`, `InventoryLotDialog`, and a staged Roast Properties selection committed only by `close_OK()`.

- [ ] **Step 1: Write failing model/dialog/selection tests**

Use real offscreen Qt and a signal-capable fake controller. Cover case-insensitive search over name/origin/varietals/process/year, columns and weight rendering, zero/negative/conflict warnings, cached timestamp/offline status, refresh preserving rows on error, choose/clear, keyboard/accessibility, HTML-like lot names rendered plain, lock state, Cancel rollback, OK commit, Beans fill-only-if-empty, and no title/weight/plus mutation.

```python
def test_selecting_lot_fills_only_empty_beans_and_commits_on_ok(dialog: editGraphDlg) -> None:
    dialog.beansedit.setPlainText('')
    dialog.chooseInventoryLot(LOT)
    assert dialog.beansedit.toPlainText() == LOT.name
    assert dialog.aw.qmc.roastServerBeanLotUUID is None
    dialog.close_OK()
    assert dialog.aw.qmc.roastServerBeanLotUUID == LOT.lot_id.hex
```

- [ ] **Step 2: Run dialog tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_inventory_dialogs.py -v
```

Expected: missing module/widgets fail.

- [ ] **Step 3: Implement the table model and modal chooser**

Use fixed columns `Lot`, `Origin`, `Varietals`, `Process`, `Crop year`, `Available`, `Conflicts`. Keep source lots immutable; apply search via `QSortFilterProxyModel` or a deterministic model filter. Render weights with existing Artisan utilities. **Refresh** calls the controller and disables only itself until result/failure. Keep the previous model on failure and show its timestamp.

- [ ] **Step 4: Add a staged Inventory lot row near Beans**

Create plain-text selected-name/balance/status labels and **Choose…**, **Clear**, **Refresh** buttons in `textLayout` directly after Beans. Snapshot original link in the dialog constructor. Selecting changes only dialog-local link and optionally empty `beansedit`. `close_OK()` writes all four qmc fields; Cancel writes none. Disable Choose/Clear when profile CHARGE exists or controller reports an active reservation; Refresh remains available.

- [ ] **Step 5: Verify plus coexistence and Qt cleanup**

Disconnect controller signals in `clean_up()`. If settings/identity changes while the dialog is open, clear a newly staged cross-namespace selection; preserve an originally loaded historical link as unavailable until the operator explicitly clears it. Assert plus coffee/blend widgets, their fields, and callbacks are byte-for-byte behaviorally unchanged with and without a Roast Server controller.

- [ ] **Step 6: Run dialog/coexistence suites**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_dialogs.py \
  test/unitary/artisanlib/roastserver/test_coexistence.py
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/roastserver/inventory_dialogs.py \
  src/artisanlib/roast_properties.py \
  src/test/unitary/artisanlib/roastserver/test_inventory_dialogs.py \
  src/test/unitary/artisanlib/roastserver/test_coexistence.py
git commit -m "feat(roast): add inventory lot chooser"
```

---

### Task 11: Persist reservation before CHARGE and preserve it across undo/re-mark

**Files:**
- Modify: `src/artisanlib/main.py`
- Modify: `src/artisanlib/canvas.py`
- Modify: `src/test/unitary/artisanlib/test_canvas.py`
- Modify: `src/test/unitary/artisanlib/test_main.py`

**Interfaces:**
- Consumes: Task 8 `prepare_inventory_charge()`/`commit_inventory_charge()` and Task 9 qmc link/weight.
- Produces `ApplicationWindow.prepareRoastServerInventoryCharge()` and `commitRoastServerInventoryCharge()` wrappers used only by `canvas._markCharge()`.

- [ ] **Step 1: Add failing CHARGE ordering and behavior tests**

Cover no-lot warning/proceed, selected+disabled block, stale lot/invalid weight block, local store failure block, offline durable proceed, reservation commit before `timeindex[0]` mutation, UUID assignment after commit, manual/no-data early return without reserve, selector lock, undo no coordinator release, and re-mark no duplicate/new UUID.

```python
def test_inventory_commit_precedes_charge_event(canvas: tgraph, ordered: Mock) -> None:
    canvas.aw.commitRoastServerInventoryCharge.side_effect = (
        lambda prepared: ordered('inventory', canvas.timeindex[0]) or ROAST_UUID.hex
    )
    canvas._markCharge()
    assert ordered.call_args.args == ('inventory', -1)
    assert canvas.timeindex[0] >= 0
    assert canvas.roastUUID == ROAST_UUID.hex
```

- [ ] **Step 2: Run focused canvas/main tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_canvas.py -k inventory_charge \
  test/unitary/artisanlib/test_main.py -k inventory_charge
```

Expected: wrappers/hooks are absent.

- [ ] **Step 3: Add UI-safe ApplicationWindow wrappers**

Build `InventoryProfileLink` from qmc, pass current UUID/green weight/unit to the controller, map fixed coordinator errors to translated plain status, and return an explicit blocked/prepared result. Do not catch hardware exceptions or perform HTTP. An untracked preparation emits the warning only when CHARGE can actually be committed.

- [ ] **Step 4: Refactor the new-CHARGE branch around a prepared intent**

Prepare before acquiring `profileDataSemaphore`. Inside the existing new-CHARGE branch, first establish that manual input/auto index/current data can produce CHARGE. Immediately before assigning `timeindex[0]`, synchronously commit the prepared reservation. On failure, return without CHARGE. Assign the returned roast UUID, then continue existing warmup/PID/annotation/event behavior unchanged.

The undo branch must not call release, clear qmc inventory fields, clear roast UUID, or unlock the coordinator state. Re-mark preparation recognizes the existing state and commit becomes a no-op.

- [ ] **Step 5: Run CHARGE plus Santoker regression tests**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_canvas.py -k 'inventory_charge or charge or santoker'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/artisanlib/{main.py,canvas.py} \
  src/test/unitary/artisanlib/{test_main.py,test_canvas.py}
git commit -m "feat(roast): reserve inventory at charge"
```

---

### Task 12: Finalize after save and release on reset or shutdown

**Files:**
- Modify: `src/artisanlib/main.py`
- Modify: `src/artisanlib/canvas.py`
- Modify: `src/test/unitary/artisanlib/test_main.py`
- Modify: `src/test/unitary/artisanlib/test_canvas.py`

**Interfaces:**
- Consumes: Task 8 finalization/release façade and existing exact serialized-profile save hook.
- Produces `notifyRoastServerInventorySavedProfile()` and reset/shutdown ordering.

- [ ] **Step 1: Add failing save/reset/shutdown lifecycle tests**

Cover Save/Save As/autosave finalization after exact file commit, repeated saves once, Save Copy never, no CHARGE never, different roast UUID never, valid actual grams, invalid actual planned fallback warning, profile upload failure independence, reset canceled no-op, reset release before roast UUID clear, reset durability failure blocks reset, finalized/finalize-queued no release, selection retained despite roast-properties reset, and shutdown release before worker interruption.

```python
def test_successful_save_finalizes_inventory_but_copy_does_not(tmp_path: Path) -> None:
    window, controller, _profile = roastserver_save_window()
    assert window.fileSave(str(tmp_path / 'roast.alog'))
    controller.finalize_inventory_profile.assert_called_once()
    controller.finalize_inventory_profile.reset_mock()
    assert window.fileSave(str(tmp_path / 'copy.alog'), copy=True)
    controller.finalize_inventory_profile.assert_not_called()
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_main.py -k 'inventory_save or inventory_shutdown' \
  test/unitary/artisanlib/test_canvas.py -k inventory_reset
```

Expected: lifecycle hooks are absent.

- [ ] **Step 3: Extend the existing post-save hook without coupling queues**

After serialization/file transaction success and only when `copy` is false, call inventory finalization with the same detached profile already sent to `notifyRoastServerSavedProfile()`. Keep calls independent in separate narrow `try` blocks so neither suppresses the other or rolls back the `.alog`. Surface planned-weight fallback through translated nonmodal status.

- [ ] **Step 4: Add accepted-reset release and selection retention**

Treat `canvas.reset()` as Artisan's abort/full-reset integration seam. After `checkSaved()` accepts and before `roastUUID` is cleared, call controller release for the current UUID. If the durable local write fails, return `False` before profile mutation. Snapshot the four qmc inventory fields before ordinary property reset and restore them after successful reset; do not retain roast UUID or reservation state in qmc.

- [ ] **Step 5: Queue shutdown release before worker interruption**

In `closeApp()`, after `checkSaved()` accepts but before `controller.shutdown()`, call the release façade for an active nonfinalizing roast. If local enqueue fails, report a fixed warning and continue shutdown; the unchanged nonterminal state must appear in restart recovery. Do not wait for network delivery.

- [ ] **Step 6: Run save/reset/shutdown regressions**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_main.py -k 'roastserver or inventory' \
  test/unitary/artisanlib/test_canvas.py -k 'reset or charge'
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/{main.py,canvas.py} \
  src/test/unitary/artisanlib/{test_main.py,test_canvas.py}
git commit -m "feat(roast): complete inventory lifecycle"
```

---

### Task 13: Present interrupted recovery, failures, conflicts, and queue status

**Files:**
- Modify: `src/artisanlib/roastserver/inventory_dialogs.py`
- Modify: `src/artisanlib/roastserver/dialogs.py`
- Modify: `src/artisanlib/roastserver/controller.py`
- Modify: `src/artisanlib/main.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_dialogs.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_roastserver_dialogs.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_controller.py`
- Modify: `src/test/unitary/artisanlib/test_main.py`

**Interfaces:**
- Consumes: Task 8 inventory signals/façade and Task 10 dialog patterns.
- Produces `InterruptedReservationsDialog`, inventory failed-command model/actions, startup recovery presentation, and prominent conflict warnings.

- [ ] **Step 1: Write failing recovery and status tests**

Cover all three recovery choices, exact namespace/roast/lot/planned/state display, inactive-namespace Keep-only behavior, persistent notice after Keep, command retry same key, no destructive replace-key/remove action, aggregate counts, unsupported-server status without profile-queue failure, conflict negative balance/plain text, model accessibility, dialog reuse, signal disconnection, and startup deferred until the main window event loop.

- [ ] **Step 2: Run dialog/controller/main tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_dialogs.py \
  test/unitary/artisanlib/roastserver/test_roastserver_dialogs.py \
  test/unitary/artisanlib/roastserver/test_inventory_controller.py \
  test/unitary/artisanlib/test_main.py -k inventory_recovery
```

Expected: missing models/dialogs/signals fail.

- [ ] **Step 3: Implement interrupted-reservation dialog**

Display one row per frozen recovery record with Lot, Roast, Planned, Namespace, and Status. Provide **Finalize planned**, **Release**, and **Keep pending**. Disable mutation choices outside the active authenticated namespace. Each accepted mutation calls `resolve_interrupted_inventory()` once and updates only after the resulting state signal.

- [ ] **Step 4: Extend Roast Server status with inventory aggregates**

Add separate counts for pending/retrying/paused/failed/interrupted inventory work and a failed-command table with **Retry same command** only. Preserve existing profile failed-job table and actions. Show fixed `Server does not support inventory` without changing connector online/profile upload status.

- [ ] **Step 5: Wire startup notice and conflict presentation in main**

Connect `inventoryRecoveryRequired` and defer opening with `QTimer.singleShot(0, self.showInventoryRecovery)` after startup. Keep one dialog instance. On conflict, show a translated prominent warning containing only lot display name, safe integer balance, and fixed reconciliation guidance; no server details or rich text.

- [ ] **Step 6: Run all UI-focused inventory tests**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver/test_inventory_dialogs.py \
  test/unitary/artisanlib/roastserver/test_roastserver_dialogs.py \
  test/unitary/artisanlib/roastserver/test_inventory_controller.py \
  test/unitary/artisanlib/test_main.py -k 'inventory or roastserver'
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/roastserver/{inventory_dialogs.py,dialogs.py,controller.py} \
  src/artisanlib/main.py \
  src/test/unitary/artisanlib/roastserver/{test_inventory_dialogs.py,test_roastserver_dialogs.py,test_inventory_controller.py} \
  src/test/unitary/artisanlib/test_main.py
git commit -m "feat(roastserver): expose inventory recovery"
```

---

### Task 14: Harden cross-platform storage and run complete regression gates

**Files:**
- Modify: `src/artisanlib/roastserver/inventory_store.py`
- Modify: `src/artisanlib/roastserver/worker.py`
- Modify: `src/artisanlib/roastserver/controller.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_store.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_worker.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_inventory_controller.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_coexistence.py`

**Interfaces:**
- Consumes: completed Tasks 1–13.
- Produces: verified implementation with documented residual platform/manual risks.

- [ ] **Step 1: Add cross-process and platform storage probes**

Add real subprocess tests for two connections racing cache replacement, reserve enqueue, lease/recovery, and terminal intent. Add Windows-marked runtime tests for private DACL inheritance, database/WAL/SHM reparse rejection, and restart recovery. Add portable injected-native seam tests so non-Windows CI still verifies fail-closed decisions.

- [ ] **Step 2: Add end-to-end fake-server lifecycle coverage**

Run one real controller/QThread with temporary stores and a deterministic fake API through refresh → select → offline CHARGE → reserve delivery → save → finalize → conflict signal, then a second run through CHARGE → reset → release. Assert one network operation at a time, exact bodies/keys, no plus calls, and no surviving thread/timer/store handles.

- [ ] **Step 3: Run all focused inventory and Roast Server tests**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/roastserver \
  test/unitary/artisanlib/test_main.py \
  test/unitary/artisanlib/test_canvas.py
```

Expected: all pass; platform-specific native tests are skipped only on nonmatching platforms.

- [ ] **Step 4: Run static and repository checks**

```bash
cd src
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pyright
.venv/bin/codespell
cd ..
pre-commit run --all-files
```

Expected: all pass. If a Qt system library prevents a command, record the exact command/error and run every remaining check.

- [ ] **Step 5: Run the complete configured pytest suite**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest
```

Expected: all configured tests pass. Do not contact production or real hardware.

- [ ] **Step 6: Inspect final scope and security invariants**

```bash
git status --short
git diff --check
git diff --stat 195d0ea89..HEAD
rg -n "Authorization|Bearer |credential|Idempotency-Key|request_json" \
  src/artisanlib/roastserver src/test/unitary/artisanlib/roastserver
```

Confirm only expected files changed; no credential/request body is logged or signaled; generated files, dependencies, plus modules, server repository, and production inventory are untouched.

- [ ] **Step 7: Perform disposable manual acceptance**

Against a disposable local/staging organization, execute the 12 scenarios in the design's “Manual acceptance” section. Record server commit, client commit, platform, commands, expected/actual reservation/balance/conflict states, and cleanup. Never use production inventory history.

- [ ] **Step 8: Commit final hardening/tests**

```bash
git add \
  src/artisanlib/roastserver/{inventory_store.py,worker.py,controller.py} \
  src/test/unitary/artisanlib/roastserver/{test_inventory_store.py,test_inventory_worker.py,test_inventory_controller.py,test_coexistence.py}
git commit -m "test(roastserver): verify inventory connector"
```

---

## Completion checklist

- [ ] Every task commit exists and each task passed its focused tests before the next task began.
- [ ] Four optional profile fields round-trip without changing old profiles or recent-roast templates.
- [ ] CHARGE durability, undo/re-mark reuse, save finalization, reset/abort/shutdown release, and crash recovery match the approved spec.
- [ ] Inventory and profile queues share one serialized worker without starvation.
- [ ] Every stale configuration response is fenced before cache/command mutation.
- [ ] Manual retry preserves exact request bytes and idempotency key.
- [ ] artisan.plus behavior and generated/derived files are unchanged.
- [ ] Full pytest and static/pre-commit gates pass, or exact environment-only blockers are reported.
- [ ] `git status --short` contains no unexpected artifact.
