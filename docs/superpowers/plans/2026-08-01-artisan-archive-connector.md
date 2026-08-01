# Artisan Archive Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an artisan.plus-independent desktop connector that durably uploads exact saved `.alog` revisions, browses and verifies a self-hosted Artisan Roast Server archive, and opens cached server profiles read-only.

**Architecture:** Put all connector state and behavior in focused `artisanlib.roastserver` modules: strict public contracts and settings at the boundary, synchronous bounded HTTP and SQLite/cache stores below one `QObject` worker, and a main-thread controller plus code-built modeless dialogs above it. Integrate only narrow menu, successful-save, startup/shutdown, and explicit server-read-only load hooks into `ApplicationWindow`; communicate with the worker using opaque request IDs and immutable public result objects so credentials and mutable artisan.plus state never cross the boundary.

**Tech Stack:** Python 3.12+, PyQt6 `QObject`/`QThread`/Qt models and widgets, `requests`, `keyring`, stdlib `sqlite3`/`hashlib`/`json`/`pathlib`, Pydantic-backed existing Artisan profile validation, pytest, Ruff, mypy, pyright, pylint, codespell.

## Global Constraints

- Implement against Artisan Roast Server commit `3912314527485cf9d9dd90bf73f6a5876bf90ef3` (`3912314`); do not change the server repository in this worktree.
- Use Python 3.12 or newer and the dependencies already pinned in `src/requirements.txt`; add no dependency and update no pin.
- Keep the deployed default configurable as `https://artisan.frxhome.chown.lv`.
- Accept only canonical `https://host[:port]` production origins; permit canonical `http://localhost[:port]`, `http://127.0.0.1[:port]`, and `http://[::1][:port]` for development and tests.
- Always verify TLS, set `requests.Session.trust_env = False`, and set `allow_redirects=False` on every authenticated request; never forward a credential or body after a redirect.
- Enforce a `16 * 1024 * 1024` byte local, upload, download, snapshot, and cached-profile ceiling and a `60 * 1024` byte connector metadata ceiling below the server's `65_536` byte default.
- Keep automatic upload false by default and reject enabling it until the current origin has passed `/api/v1/auth/me`, the returned identity is persisted, and the credential has been stored successfully.
- Store bearer credentials only through the dedicated keyring service `org.artisan-scope.Artisan.RoastServer`; never put a credential in QSettings, SQLite, profile data, cache sidecars, paths, logs, exception text, Qt signals, test literals, test artifacts, or assertion output.
- Use opaque request IDs and an in-memory locked vault for candidate credentials; worker signals carry only frozen public dataclasses, safe categories, bounded messages, and internally generated paths.
- Namespace outbox and cache access by canonical origin plus confirmed organization UUID; a server or organization change must make the previous namespace unreachable from controller and dialogs.
- Do not import or mutate `plus` from any `src/artisanlib/roastserver/` module; do not reuse artisan.plus credentials, token, globals, queues, cache paths, workers, status icon, UUID register, or sync state.
- Automated tests must use fake sessions, fake keyrings, temporary local storage, and fake worker APIs only; no test may contact the deployed server, another external endpoint, a cloud account, a keyring daemon, or roasting hardware.
- Keep all HTTP, keyring, snapshot, SQLite, cache hashing, and archive download work off the UI thread; update Qt widgets only in main-thread slots.
- Server-sourced opens are read-only: skip recent files and `plusAddPath()`, clear `curFile`, leave plus modification/sync state untouched, mark clean, force Save As, and never overwrite a cache file.
- Use `QApplication.translate(context, text)` and plain text for every new visible string, but do not edit `src/translations/*`, `src/uic/*`, `src/ui/*`, help derivatives, protobuf outputs, or any other generated/derived file in this slice.
- Match neighboring AGPLv3+ production headers, single-quoted Python style, complete annotations, narrow exception boundaries, and precise qualified ignores.

---

## Server contract pinned for this plan

All API calls are relative to the canonical origin and include `Authorization: Bearer <credential>` without cookies:

| Operation | Request | Required success contract |
|---|---|---|
| Test identity | `GET /api/v1/auth/me` | `200`; exact object `user{id,email,nickname}`, `organization{id,name,slug}`, `role` in `admin/member` |
| Compatible metadata | `POST /api/v1/aroast`, `Content-Type: application/json` | `200`; `success=true`, matching `result.roast_id`, aware `result.modified_at`, integer `rlimit/rusage/rremaining` |
| List archive | `GET /api/v1/roasts` | Query `limit` 1–100, opaque `cursor` up to 512 chars, `search` up to 200, aware UTC `roast_at_from`/`roast_at_to`, exact `machine`, state in `awaiting_profile/parsed/parse_failed`; exact `items` plus `next_cursor` |
| Roast detail | `GET /api/v1/roasts/{32-lower-hex-uuid}` | Exact list-item fields plus `current_metadata`, consistent `current_revision`, and exact relative links `self/chart/revisions` |
| Upload revision | `POST /api/v1/roasts/{uuid}/revisions` | Multipart fields `profile`, `sha256`, `idempotency_key`, `metadata`; `200` for current-hash/idempotent success or `201` for a new revision; matching UUID/hash and exact `roast/chart/revisions/download` links |
| Download revision | `GET /api/v1/roasts/{uuid}/revisions/{number}/download` | `200`, exact `application/x-artisan-profile`, `Content-Length`, `Content-Disposition: attachment; filename="{uuid}-r{number}.alog"`, matching `X-Content-SHA256`, `X-Checksum-SHA256`, strong `ETag`, and `X-Revision-Number` |

A revision object has exactly `revision_number`, `sha256`, `byte_size`, `parser_version`, `parse_state`, `parse_diagnostic_code`, `parse_diagnostic_message`, `uploaded_at`, `metadata`, and `reparse_recommended`. A roast list item has exactly `roast_uuid`, `state`, `roast_at`, `title`, `batch_prefix`, `batch_number`, `batch_position`, `operator`, `machine`, `machine_setup`, `temperature_unit`, `duration_seconds`, `green_weight_kg`, `roasted_weight_kg`, `revision_count`, `updated_at`, and `labels`; each label has exactly `label_uuid`, `name`, `color`, and `archived`.

Errors are accepted only from the exact envelope `{"error":{"code":str,"message":str,"details":value}}`. Display `message` only when code, length, UTF-8, NUL, and control-character checks pass; otherwise use the fixed connector category text. `401` pauses for credentials, `429` and `5xx` retry, other `4xx` fail permanently, and `3xx` is always an invalid-response failure.

## Known baseline that must not be attributed to this connector

The current plus tests contain order-sensitive import mocking. The pre-implementation baseline was recorded from the clean connector worktree with:

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_main.py test/unitary/plus
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/plus/test_sync.py::TestAddSync::test_add_sync_successful
```

Current baseline: the combined selection reports `1 failed, 657 passed, 1 skipped`; `TestAddSync::test_add_sync_successful` recursively invokes its patched `builtins.__import__` and then raises while catching a mocked non-exception. The exact failed node passes alone (`1 passed`). This is a pre-existing collection/order isolation defect. New connector tests must not patch `builtins.__import__` or add modules to `sys.modules` globally, must pass alone and before/after focused existing main/plus tests, and must not increase the baseline failure set.

---

## File Structure

### Create

- `src/artisanlib/roastserver/__init__.py` — package marker and stable public exports only; no process-wide mutable state.
- `src/artisanlib/roastserver/contract.py` — frozen public dataclasses, strict UUID/timestamp/cursor/JSON/error/identity/roast/revision parsers, and fixed safe failure categories.
- `src/artisanlib/roastserver/settings.py` — canonical origin and namespace derivation, immutable settings, `QSettings` adapter, and dedicated keyring abstraction.
- `src/artisanlib/roastserver/api.py` — bounded synchronous `requests` client and retry classification; no Qt imports.
- `src/artisanlib/roastserver/metadata.py` — deterministic compatible `/aroast` and revision-hint projection from `ProfileData`.
- `src/artisanlib/roastserver/outbox.py` — exact-file snapshotting, SQLite schema/migration/transactions, leasing, retries, deduplication, and snapshot ownership.
- `src/artisanlib/roastserver/cache.py` — namespaced temporary files, verified publication/sidecars, offline rows, validation, size accounting, and protected pruning.
- `src/artisanlib/roastserver/worker.py` — opaque vault, one worker event loop, outbox delivery, test/browse/download/cache commands, and credential-free signals.
- `src/artisanlib/roastserver/controller.py` — main-thread settings/lifecycle state, command façade, validation/open orchestration, and UI-facing signals.
- `src/artisanlib/roastserver/dialogs.py` — code-built modeless configuration/failed-jobs dialog and archive browser/table model.
- `src/test/unitary/artisanlib/roastserver/conftest.py` — fake keyring/session helpers, temporary roots, and an autouse external-network guard without global module replacement.
- `src/test/unitary/artisanlib/roastserver/test_contract.py`
- `src/test/unitary/artisanlib/roastserver/test_settings.py`
- `src/test/unitary/artisanlib/roastserver/test_api.py`
- `src/test/unitary/artisanlib/roastserver/test_metadata.py`
- `src/test/unitary/artisanlib/roastserver/test_outbox.py`
- `src/test/unitary/artisanlib/roastserver/test_cache.py`
- `src/test/unitary/artisanlib/roastserver/test_worker.py`
- `src/test/unitary/artisanlib/roastserver/test_controller.py`
- `src/test/unitary/artisanlib/roastserver/test_dialogs.py`
- `src/test/unitary/artisanlib/roastserver/test_coexistence.py`

### Modify

- `src/artisanlib/main.py:1412-1889,2239-2255,4357-4440,13297-13358,13703-13845,17374-17446,20247-21620,28348-28390` — controller/action/dialog ownership, menu hooks, successful save/autosave hooks, explicit server-source loading, startup, and bounded shutdown.
- `src/test/unitary/artisanlib/test_main.py:315-770` — focused server-read-only load, save hook, recent-file, and plus exclusion assertions using its existing real-Qt isolation.

### Explicitly unchanged

- `src/plus/**`, `src/translations/**`, `src/ui/**`, `src/uic/**`, `src/help/**`, protobuf outputs, dependency files, server files, and packaging/release metadata.

## Shared interfaces and dependency direction

Dependencies flow `contract <- settings/metadata/api <- outbox/cache <- worker <- controller <- dialogs/main`; no lower module imports controller, dialogs, main, or plus.

- `contract.py` produces `ServerIdentity`, `Namespace`, `LabelSummary`, `Revision`, `RoastSummary`, `RoastPage`, `RoastDetail`, `RevisionUpload`, `ServerProfileSource`, `ArchiveFilters`, `FailureKind`, `PublicFailure`, and the strict `parse_*()` functions.
- `settings.py` produces `ConnectorSettings`, `SettingsStore`, `CredentialStore`, `SystemCredentialStore`, `CredentialStoreError`, `canonical_origin()`, `namespace_for()`, and `credential_account()`.
- `api.py` produces `ApiFailure`, `DownloadReceipt`, `RoastServerClient`, and `ClientFactory = Callable[[str, str], RoastServerClient]`.
- `metadata.py` produces frozen `ProjectedMetadata(aroast_json: bytes, revision_json: bytes)` and `project_profile(profile: ProfileData, modified_at: datetime) -> ProjectedMetadata`.
- `outbox.py` produces frozen `Snapshot`, `Job`, `QueueCounts`, `FailedJob`, `EnqueueResult`, and `Outbox` operations listed in Task 5.
- `cache.py` produces frozen `CacheStats`, `CachedRevision`, `CachedPage`, and `CacheStore` operations listed in Task 6.
- `worker.py` produces generic `OpaqueVault[T]`, frozen command objects, `RoastServerWorker`, and immutable signal payloads; only opaque request IDs cross slots that refer to credentials or mutable profiles.
- `controller.py` produces `RoastServerController`; `ApplicationWindow` calls only controller public methods and receives server profiles through its `profileReady(str, ServerProfileSource)` signal, converting the internally generated string to `Path` in the slot.

## Test fixture contracts

Implement each named helper directly above the tests in the owning test file (repeat the small detached payload factories where another file needs them); keep only the autouse network guard and generic temporary/fake-keyring fixtures in `conftest.py`. Never import one test module from another or depend on test collection order:

- `valid_identity_payload()` returns the exact `/auth/me` object shown in Task 1 with fresh detached dictionaries.
- `valid_revision_payload(number=1, sha256=None, byte_size=None)` returns all ten pinned revision fields, aware `uploaded_at`, `parse_state='parsed'`, empty public metadata, and checksum/size derived from `PROFILE_BYTES` unless overridden.
- `valid_roast_item_payload()` returns all 18 pinned list fields for roast UUID `11111111111141118111111111111111`, one active green label, `state='parsed'`, and `revision_count=1`.
- `valid_roast_page_payload()` returns `{'items': [valid_roast_item_payload()], 'next_cursor': None}`.
- `valid_roast_detail_payload()` adds `current_metadata`, `current_revision`, and exact `self/chart/revisions` links to a detached list item.
- `valid_upload_payload()` returns matching `roast_uuid`, `state`, revision, and exact `roast/chart/revisions/download` links.
- `PROFILE_BYTES` is `repr()` of a minimal valid Artisan profile encoded as UTF-8; `ROAST_UUID` is the UUID in that profile; `IDEMPOTENCY_KEY` uses the exact `archive-v1` format and a fixed non-secret client UUID.
- `RecordingSession`, `json_response()`, and `raw_response()` implement only the `requests.Session` attributes used by `api.py`, record keyword arguments, stream configured chunks, and raise if an unconfigured call occurs. `client_factory()` builds a client with an ephemeral credential assembled from integer character codes and never includes that value in `repr`.
- `sample_profile()` returns a detached `ProfileData` in mode C with 1 kg input, 0.85 kg output, bounded machine/operator/batch fields, canonical roast UUID/epoch, and all six computed event points. `minimal_profile()` retains only canonical UUID, roast epoch, mode, weight, and empty computed data. `MODIFIED` is `2026-08-01T12:34:56.123456+00:00`.
- `opened_outbox(tmp_path)` creates private `outbox.sqlite3` and snapshot roots with an injected clock. `enqueue_fixture()` writes `PROFILE_BYTES`, snapshots it, projects `sample_profile()`, and enqueues under `NAMESPACE`; `NAMESPACE` is `namespace_for('https://example.test', UUID('22222222-2222-4222-8222-222222222222'))`, and `NOW` is aware UTC.
- `staged_download` is a connector-generated temporary file containing `PROFILE_BYTES`; `DETAIL`/`RECEIPT` strictly match it. `cached_revision` publishes that fixture. `three_cached_revisions` publishes three distinct generated roast/revision/hash tuples with increasing download times.
- `worker_harness` owns a real offscreen `QThread`, temporary real Outbox/CacheStore, fake credential store, fake API factory, both opaque vaults, a deterministic clock, and bounded `wait_for_signal()`/`run_one_queue_tick()` helpers. `api_failure(status)` maps through the production classifier rather than constructing arbitrary text.
- `controller_harness` uses a fake worker object with recording slots/signals plus real opaque vaults, temporary INI `QSettings`, fake credential store, and fake profile validator. `PROFILE_PATH`/`PROFILE` are a regular temporary `.alog` and its detached in-memory saved dictionary.
- Dialog fixtures use a signal-capable fake with exactly the controller signals/methods in Task 8. `IDENTITY`, `FAILED_JOB`, `SAFE_FAILURE`, online/cached pages, roast UUIDs, and `STALE_SERVER_SOURCE` are frozen production dataclasses, not mocks or dictionaries.
- `save_window()` creates an `ApplicationWindow.__new__` object with only the existing save dependencies and a fake controller; `server_load_window` captures the active profile before each call and uses existing `test_main.py` Qt fixtures. `valid_profile()` is a detached profile accepted by `validateProfileDict()`.

---

### Task 1: Lock down public response and safe-error contracts

**Files:**
- Create: `src/artisanlib/roastserver/__init__.py`
- Create: `src/artisanlib/roastserver/contract.py`
- Create: `src/test/unitary/artisanlib/roastserver/conftest.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_contract.py`

**Interfaces:**
- Consumes: only stdlib `dataclasses`, `datetime`, `enum`, `json`, `math`, `re`, `typing`, and `uuid`.
- Produces: all frozen types and parsers named in “Shared interfaces”; parsers accept `object`, reject bool-as-int, reject unknown/missing keys, and return detached immutable tuples/frozen dataclasses.

- [ ] **Step 1: Add a connector-only network guard and strict parser tests**

Create a local autouse guard that raises if an unmocked request reaches `requests.sessions.Session.request`; do not patch `sys.modules`. Add tests named below with explicit valid server-shaped dictionaries and one-field mutations:

```python
# test_contract.py

def test_identity_requires_exact_hyphenated_uuids_and_role() -> None:
    identity = parse_identity({
        'user': {'id': '11111111-1111-4111-8111-111111111111',
                 'email': 'owner@example.test', 'nickname': 'Owner'},
        'organization': {'id': '22222222-2222-4222-8222-222222222222',
                         'name': 'Roastery', 'slug': 'roastery'},
        'role': 'admin',
    })
    assert identity.organization.id.hex == '22222222222242228222222222222222'


def test_roast_page_rejects_extra_key_bad_cursor_and_invalid_label() -> None:
    payload = valid_roast_page_payload()
    payload['items'][0]['internal_id'] = 'private'
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)


def test_detail_requires_state_revision_and_relative_link_consistency() -> None:
    payload = valid_roast_detail_payload()
    payload['links']['self'] = '/api/v1/roasts/' + 'f' * 32
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(payload)


def test_error_parser_never_returns_html_controls_or_oversized_text() -> None:
    for body in (b'<html>proxy secret</html>',
                 b'{"error":{"code":"bad","message":"line\\nsecret","details":null}}'):
        assert parse_error_envelope(body) is None
```

Also cover all exact list/revision/detail/upload fields from the pinned table, lowercase 32-hex roast UUIDs, aware timestamps, finite numbers, safe integers `<= 9_007_199_254_740_991`, metadata depth `<= 64`, cursor length `<= 512`, label colors, response consistency, and safe server errors capped at 500 characters.

- [ ] **Step 2: Run the contract tests and verify RED**

Run:

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_contract.py -v
```

Expected: collection fails because `artisanlib.roastserver.contract` does not exist.

- [ ] **Step 3: Implement frozen contracts and exact parsers**

Use these concrete declarations and constants; parser helpers must compare `set(mapping)` to the documented exact key set before reading values:

```python
MAX_PROFILE_BYTES: Final[int] = 16 * 1024 * 1024
MAX_METADATA_BYTES: Final[int] = 60 * 1024
MAX_JSON_BYTES: Final[int] = 2 * 1024 * 1024
MAX_CURSOR_CHARS: Final[int] = 512
JS_SAFE_INTEGER_MAX: Final[int] = 9_007_199_254_740_991
POSTGRESQL_INTEGER_MAX: Final[int] = 2_147_483_647

class FailureKind(StrEnum):
    OFFLINE = 'offline'
    CREDENTIAL_REJECTED = 'credential_rejected'
    RATE_LIMITED = 'rate_limited'
    INVALID_RESPONSE = 'invalid_response'
    PROFILE_REJECTED = 'profile_rejected'
    LOCAL_PROFILE = 'local_profile'
    CHECKSUM_MISMATCH = 'checksum_mismatch'
    CACHE_CORRUPT = 'cache_corrupt'
    KEYRING = 'keyring'

FAILURE_MESSAGES: Final[dict[FailureKind, str]] = {
    FailureKind.OFFLINE: 'Offline / server unavailable.',
    FailureKind.CREDENTIAL_REJECTED: 'Credential rejected or revoked.',
    FailureKind.RATE_LIMITED: 'Request rate limited.',
    FailureKind.INVALID_RESPONSE: 'Invalid server response.',
    FailureKind.PROFILE_REJECTED: 'Profile rejected by server.',
    FailureKind.LOCAL_PROFILE: 'Local saved file changed or unavailable.',
    FailureKind.CHECKSUM_MISMATCH: 'Download checksum mismatch.',
    FailureKind.CACHE_CORRUPT: 'Cached copy corrupt or unavailable.',
    FailureKind.KEYRING: 'Operating-system keyring unavailable.',
}

@dataclass(frozen=True, slots=True)
class PublicFailure:
    kind: FailureKind
    code: str
    message: str
    retryable: bool

@dataclass(frozen=True, slots=True)
class Namespace:
    origin: str
    organization_id: UUID
    key: str

@dataclass(frozen=True, slots=True)
class ArchiveFilters:
    search: str | None = None
    state: Literal['awaiting_profile', 'parsed', 'parse_failed'] | None = None
    machine: str | None = None
    roast_at_from: datetime | None = None
    roast_at_to: datetime | None = None

@dataclass(frozen=True, slots=True)
class ServerProfileSource:
    namespace: Namespace
    roast_uuid: UUID
    revision_number: int
    sha256: str
    stale: bool
```

Define all remaining response dataclasses with every field in the pinned contract. Implement `parse_identity()`, `parse_roast_page()`, `parse_roast_detail()`, `parse_revision_upload()`, `parse_aroast_ack()`, and `parse_error_envelope()`. Validate detail links against the parsed UUID and upload links against UUID/revision. Export only immutable types and parsing functions from `__init__.py`.

- [ ] **Step 4: Run contract tests GREEN and the package import check**

Run:

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_contract.py -v
.venv/bin/python -c "from artisanlib.roastserver.contract import RoastDetail, ServerIdentity"
```

Expected: all contract tests pass and the import exits 0.

- [ ] **Step 5: Commit the contract boundary**

```bash
git add src/artisanlib/roastserver/__init__.py \
  src/artisanlib/roastserver/contract.py \
  src/test/unitary/artisanlib/roastserver/conftest.py \
  src/test/unitary/artisanlib/roastserver/test_contract.py
git commit -m "feat(roastserver): define strict archive contracts"
```

---

### Task 2: Persist non-secret settings and isolate keyring credentials

**Files:**
- Create: `src/artisanlib/roastserver/settings.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_settings.py`

**Interfaces:**
- Consumes: `contract.Namespace`, `contract.ServerIdentity`, a `QSettings` instance injected into `SettingsStore`, and an injected keyring-shaped backend.
- Produces:
  - `canonical_origin(value: str) -> str`
  - `namespace_for(origin: str, organization_id: UUID) -> Namespace`
  - `credential_account(origin: str) -> str`
  - `SettingsStore.load() -> ConnectorSettings`
  - `SettingsStore.set_origin(origin: str) -> ConnectorSettings`
  - `SettingsStore.save_connection(origin: str, identity: ServerIdentity) -> ConnectorSettings`
  - `SettingsStore.save_options(enabled: bool, automatic_upload: bool, cache_limit_bytes: int) -> ConnectorSettings`
  - `SettingsStore.save_geometry(configuration: QByteArray | None, browser: QByteArray | None) -> None`
  - `CredentialStore.get(origin: str) -> str | None`, `set(origin: str, credential: str) -> None`, and `delete(origin: str) -> None`.

- [ ] **Step 1: Write origin, QSettings, namespace, and fake-keyring tests**

```python
@pytest.mark.parametrize(('raw', 'expected'), [
    (' HTTPS://Example.COM:443/ ', 'https://example.com'),
    ('https://example.com:8443', 'https://example.com:8443'),
    ('http://127.0.0.1:8000/', 'http://127.0.0.1:8000'),
    ('http://[::1]:8000', 'http://[::1]:8000'),
])
def test_canonical_origin(raw: str, expected: str) -> None:
    assert canonical_origin(raw) == expected

@pytest.mark.parametrize('raw', [
    'http://example.com', 'https://user@example.com', 'https://example.com/api',
    'https://example.com/?query=1', 'https://example.com/#fragment',
])
def test_origin_policy_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(SettingsError, match='valid HTTPS origin'):
        canonical_origin(raw)


def test_settings_never_store_credential_and_auto_upload_defaults_false(qsettings: QSettings) -> None:
    settings = SettingsStore(qsettings).load()
    assert settings.origin == 'https://artisan.frxhome.chown.lv'
    assert not settings.enabled and not settings.automatic_upload
    assert not any('token' in key.casefold() or 'credential' in key.casefold()
                   for key in qsettings.allKeys())


def test_keyring_failure_has_fixed_message_and_no_secret(fake_keyring: FakeKeyring) -> None:
    secret = ''.join(chr(value) for value in (115, 101, 99, 114, 101, 116))
    fake_keyring.set_error = RuntimeError('backend echoed ' + secret)
    with pytest.raises(CredentialStoreError) as raised:
        SystemCredentialStore(fake_keyring).set('https://example.test', secret)
    assert secret not in str(raised.value)
```

Also assert only group `RoastServer` keys `origin`, `enabled`, `automaticUpload`, `clientInstanceUUID`, `identityUserID`, `identityUserEmail`, `identityUserNickname`, `identityOrganizationID`, `identityOrganizationName`, `identityOrganizationSlug`, `identityRole`, `cacheLimitBytes`, `configurationGeometry`, and `browserGeometry` are written; cache bounds are 64 MiB–4 GiB; client UUID is stable; origin/org namespace hashes differ; and account keys contain only `origin-sha256:<64 lowercase hex>`.

- [ ] **Step 2: Run settings tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py -v
```

Expected: import fails because `settings.py` is absent.

- [ ] **Step 3: Implement immutable settings and keyring adapters**

```python
KEYRING_SERVICE: Final[str] = 'org.artisan-scope.Artisan.RoastServer'
DEFAULT_ORIGIN: Final[str] = 'https://artisan.frxhome.chown.lv'
DEFAULT_CACHE_LIMIT_BYTES: Final[int] = 512 * 1024 * 1024
MIN_CACHE_LIMIT_BYTES: Final[int] = 64 * 1024 * 1024
MAX_CACHE_LIMIT_BYTES: Final[int] = 4 * 1024 * 1024 * 1024
KEYRING_FAILURE_MESSAGE: Final[str] = (
    'The Roast Server credential could not be stored in the operating-system keyring. '
    'Verify that your system keyring is available and try again.'
)

@dataclass(frozen=True, slots=True)
class ConnectorSettings:
    origin: str
    enabled: bool
    automatic_upload: bool
    client_instance_uuid: UUID
    identity: ServerIdentity | None
    cache_limit_bytes: int
    configuration_geometry: QByteArray | None
    browser_geometry: QByteArray | None
```

Canonicalize IDNA hostnames, lowercase DNS names, preserve bracketed IPv6, remove default ports, and reject whitespace/control characters after trimming the outer input. `set_origin()` must clear the persisted identity, force `enabled=False`, and force `automatic_upload=False` when the canonical origin changes. `save_options()` must raise `SettingsError` when `automatic_upload=True` but no identity is confirmed for the current origin. Wrap every backend exception with `CredentialStoreError(KEYRING_FAILURE_MESSAGE) from None`; never log the exception or credential. Treat a missing credential on delete as success.

- [ ] **Step 4: Run settings and secret-storage tests GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py -v
```

Expected: all tests pass with a temporary INI-format `QSettings` and fake keyring.

- [ ] **Step 5: Commit settings/keyring isolation**

```bash
git add src/artisanlib/roastserver/settings.py \
  src/test/unitary/artisanlib/roastserver/test_settings.py
git commit -m "feat(roastserver): isolate settings and credentials"
```

---

### Task 3: Build the bounded non-redirecting HTTP client

**Files:**
- Create: `src/artisanlib/roastserver/api.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_api.py`

**Interfaces:**
- Consumes: canonical origin, in-memory credential, strict contract parsers, `requests.Session`, paths/streams generated by connector code.
- Produces:
  - `RoastServerClient.test_connection() -> ServerIdentity`
  - `post_aroast(roast_uuid: UUID, aroast_json: bytes) -> None`
  - `upload_revision(roast_uuid: UUID, sha256: str, idempotency_key: str, metadata_json: bytes, snapshot: BinaryIO) -> RevisionUpload`
  - `list_roasts(filters: ArchiveFilters, cursor: str | None = None, limit: int = 50) -> RoastPage`
  - `get_roast(roast_uuid: UUID) -> RoastDetail`
  - `download_revision(detail: RoastDetail, destination: BinaryIO) -> DownloadReceipt`
  - `ApiFailure(failure: PublicFailure, status_code: int | None, retry_after_seconds: int | None)`.

- [ ] **Step 1: Add fake-session request, upload, retry, and download tests**

Use a recording `requests.Session` fake that returns chunk iterators and never opens sockets. Generate an ephemeral credential in memory and redact it from assertion messages. Cover:

```python
def test_session_disables_proxy_inheritance_tls_bypass_and_redirects(client_factory) -> None:
    client, session = client_factory(json_response(200, valid_identity_payload()))
    client.test_connection()
    assert session.trust_env is False
    request = session.calls[0]
    assert request.verify is True
    assert request.allow_redirects is False
    assert request.timeout == (4.0, 10.0)


def test_redirect_is_rejected_without_followup(client_factory) -> None:
    client, session = client_factory(raw_response(307, b'', {'Location': 'https://other.test'}))
    with pytest.raises(ApiFailure) as raised:
        client.test_connection()
    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert len(session.calls) == 1


def test_upload_multipart_has_exact_fields_and_validates_current_hash_success(client_factory) -> None:
    client, session = client_factory(json_response(200, valid_upload_payload()))
    checksum = hashlib.sha256(PROFILE_BYTES).hexdigest()
    result = client.upload_revision(ROAST_UUID, checksum, IDEMPOTENCY_KEY,
                                    b'{"machine":"Test Drum"}',
                                    io.BytesIO(PROFILE_BYTES))
    assert result.revision.sha256 == checksum
    assert set(session.calls[0].data) == {'sha256', 'idempotency_key', 'metadata'}
    assert set(session.calls[0].files) == {'profile'}
```

Also test bounded identity/list/detail/upload JSON, content-length lies, chunk overflow, arbitrary HTML, both checksum headers, ETag, exact MIME/disposition/revision/length, streamed SHA-256, short/long bodies, response UUID/link mismatch, no authorization value in logs or `ApiFailure`, and retry classification: `ConnectionError`/`Timeout`/`SSLError`, `429`, and `500..599` retry; `401` credential pause; other `400..499` permanent. Parse delta-seconds and RFC-date `Retry-After`, clamp to `0..3600`, and ignore malformed values.

- [ ] **Step 2: Run API tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_api.py -v
```

Expected: import fails because `api.py` is absent.

- [ ] **Step 3: Implement one bounded request path and exact endpoint methods**

```python
CONNECT_TIMEOUT_SECONDS: Final[float] = 4.0
READ_TIMEOUT_SECONDS: Final[float] = 10.0
MAX_RETRY_AFTER_SECONDS: Final[int] = 3600

@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    roast_uuid: UUID
    revision_number: int
    sha256: str
    byte_count: int
    filename: str

class RoastServerClient:
    def __init__(self, origin: str, credential: str,
                 session: requests.Session | None = None) -> None:
        self._origin = canonical_origin(origin)
        self._credential = credential
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
```

Route every endpoint through `_request(method, path, *, params=None, data=None, files=None, json_bytes=None, stream=False)`, always passing `verify=True`, `allow_redirects=False`, `(4.0, 10.0)`, `Cache-Control: no-store`, an Artisan version user agent, and the authorization header assembled only at call time. Read JSON through `_bounded_body(response, MAX_JSON_BYTES)` before parsing. Stream download chunks directly into the injected binary destination while enforcing 16 MiB and hashing; remove/close ownership remains the caller's responsibility. Never include request headers, bodies, response bodies, arbitrary exception text, or local paths in `ApiFailure`.

- [ ] **Step 4: Run API and network-guard tests GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_api.py \
  test/unitary/artisanlib/roastserver/test_contract.py -v
```

Expected: all pass; the autouse network guard records zero real requests.

- [ ] **Step 5: Commit the transport boundary**

```bash
git add src/artisanlib/roastserver/api.py \
  src/test/unitary/artisanlib/roastserver/test_api.py
git commit -m "feat(roastserver): add bounded archive API client"
```

---

### Task 4: Project deterministic compatible metadata

**Files:**
- Create: `src/artisanlib/roastserver/metadata.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_metadata.py`

**Interfaces:**
- Consumes: detached `ProfileData`, aware source file modification time, `artisanlib.util.convertWeight` and `fromFtoCstrict`; imports no plus module.
- Produces: `ProjectedMetadata` and `project_profile()`; both JSON byte strings use UTF-8, sorted keys, compact separators, `allow_nan=False`, and stay at or below 60 KiB.

- [ ] **Step 1: Write deterministic mapping and boundary tests**

```python
def test_projection_is_deterministic_and_matches_aroast_names(sample_profile: ProfileData) -> None:
    modified = datetime(2026, 8, 1, 12, 34, 56, 123456, tzinfo=UTC)
    first = project_profile(sample_profile, modified)
    second = project_profile(copy.deepcopy(sample_profile), modified)
    assert first == second
    aroast = json.loads(first.aroast_json)
    assert aroast['roast_id'] == '11111111111141118111111111111111'
    assert aroast['modified_at'] == '2026-08-01T12:34:56.123456+00:00'
    assert aroast['amount'] == pytest.approx(1.0)
    assert aroast['charge_temp'] == pytest.approx(190.0)


def test_revision_hints_include_operator_units_and_events(sample_profile: ProfileData) -> None:
    hints = json.loads(project_profile(sample_profile, MODIFIED).revision_json)
    assert hints['operator'] == 'Roaster One'
    assert hints['temperature_unit'] == 'C'
    assert hints['green_weight_kg'] == pytest.approx(1.0)
    assert hints['events']['first_crack_start']['time_seconds'] == 480.0


def test_unknown_nonfinite_unsafe_and_oversized_values_are_omitted() -> None:
    profile = minimal_profile()
    profile['operator'] = 'x\x00y'
    profile['ambientTemp'] = math.inf
    projected = project_profile(profile, MODIFIED)
    assert b'operator' not in projected.revision_json
    assert b'Infinity' not in projected.revision_json
```

Test `g/Kg/kg/lb/oz` to kg, Fahrenheit temperatures/RoR to Celsius, roast epoch to UTC, batch fields, machine/setup, moisture/density/colors/ambient values, `CHARGE/TP/DRY/FCs/FCe/DROP` time and ET/BT values, development time/ratio, supported energy/CO2 values, string maxima from the server schema, exact UUID validation, safe integers, and deterministic omission until the cap fits. Assert neither JSON includes free-form profile bytes, paths, credentials, plus sync hashes, schedule state, comments, or unknown keys.

- [ ] **Step 2: Run metadata tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_metadata.py -v
```

Expected: import fails because `metadata.py` is absent.

- [ ] **Step 3: Implement the explicit projection tables**

```python
@dataclass(frozen=True, slots=True)
class ProjectedMetadata:
    aroast_json: bytes
    revision_json: bytes

_EVENT_MAP: Final[tuple[tuple[str, str, str | None, str | None], ...]] = (
    ('charge', 'CHARGE_time', 'CHARGE_ET', 'CHARGE_BT'),
    ('turning_point', 'TP_time', None, 'TP_BT'),
    ('dry_end', 'DRY_time', None, 'DRY_BT'),
    ('first_crack_start', 'FCs_time', None, 'FCs_BT'),
    ('first_crack_end', 'FCe_time', None, 'FCe_BT'),
    ('drop', 'DROP_time', 'DROP_ET', 'DROP_BT'),
)
```

Build `/aroast` with only server-supported names: `roast_id`, `modified_at`, `date`, `amount`, `end_weight`, `end_weight_est`, `defects_weight`, `label`, `batch_prefix`, `batch_number`, `batch_pos`, `machine`, `setup`, moisture/density/color/ambient fields, event fields, `FCs_RoR`, `DEV_time`, `DEV_ratio`, and supported BTU/CO2 values. Build revision hints with canonical descriptive names plus nested events and descriptors; omit free-form notes and all plus-prefixed keys. Truncate bounded strings by Unicode code points without splitting, omit invalid values rather than coercing them to strings, and remove lowest-priority descriptor keys in a fixed ordered tuple if encoded revision hints exceed 60 KiB; required identity/time keys may never be removed.

- [ ] **Step 4: Run metadata tests GREEN and verify no plus import**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_metadata.py -v
.venv/bin/python - <<'PY'
import ast
from pathlib import Path
module = ast.parse(Path('artisanlib/roastserver/metadata.py').read_text(encoding='utf-8'))
assert all(not (isinstance(node, (ast.Import, ast.ImportFrom)) and
                any(name.name == 'plus' or name.name.startswith('plus.')
                    for name in (node.names if isinstance(node, ast.Import) else
                                 [ast.alias(name=node.module or '')])))
           for node in ast.walk(module))
PY
```

Expected: all tests and AST assertion pass.

- [ ] **Step 5: Commit deterministic metadata**

```bash
git add src/artisanlib/roastserver/metadata.py \
  src/test/unitary/artisanlib/roastserver/test_metadata.py
git commit -m "feat(roastserver): project bounded roast metadata"
```

---

### Task 5: Add immutable snapshots and the durable SQLite outbox

**Files:**
- Create: `src/artisanlib/roastserver/outbox.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_outbox.py`

**Interfaces:**
- Consumes: connector-owned private root, `Namespace`, saved `.alog` path, `ProjectedMetadata`, client UUID, and an injected UTC clock.
- Produces:
  - `Outbox.open() -> None`, `close() -> None`, `recover_expired_leases(now: datetime) -> int`
  - `snapshot_saved_file(namespace: Namespace, source: Path) -> Snapshot`
  - `enqueue(namespace: Namespace, snapshot: Snapshot, roast_uuid: UUID, metadata: ProjectedMetadata, client_uuid: UUID) -> EnqueueResult`
  - `lease_next(namespace: Namespace, now: datetime, lease_seconds: int = 60) -> Job | None`
  - `mark_complete(job_id: str, lease_token: str, now: datetime) -> None`
  - `mark_retry(job_id: str, lease_token: str, now: datetime, next_attempt_at: datetime, failure: PublicFailure) -> None`
  - `mark_failed(job_id: str, lease_token: str, now: datetime, failure: PublicFailure) -> None`
  - `pause_namespace(namespace: Namespace, now: datetime, code: str) -> int`
  - `resume_namespace(namespace: Namespace, now: datetime) -> int`
  - `retry_now(job_id: str, now: datetime) -> None`, `remove(job_id: str) -> None`
  - `counts(namespace: Namespace) -> QueueCounts`, `failed_jobs(namespace: Namespace) -> tuple[FailedJob, ...]`, `protected_paths(namespace: Namespace) -> frozenset[Path]`.

- [ ] **Step 1: Write migration, snapshot, deduplication, lease, and ownership tests**

```python
def test_snapshot_is_exact_and_immune_to_source_edits(outbox: Outbox, saved_profile: Path) -> None:
    snapshot = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    original = snapshot.absolute_path.read_bytes()
    saved_profile.write_bytes(b'changed later')
    assert snapshot.absolute_path.read_bytes() == original
    assert snapshot.sha256 == hashlib.sha256(original).hexdigest()


def test_duplicate_uuid_hash_resolves_one_job_and_one_snapshot(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox)
    second = enqueue_fixture(outbox)
    assert first.job.id == second.job.id
    assert first.created and not second.created
    assert outbox.counts(NAMESPACE).pending == 1


def test_expired_lease_recovers_after_restart(tmp_path: Path) -> None:
    first = opened_outbox(tmp_path)
    job = enqueue_fixture(first).job
    assert first.lease_next(NAMESPACE, NOW, lease_seconds=60).id == job.id
    first.close()
    second = opened_outbox(tmp_path)
    assert second.recover_expired_leases(NOW + timedelta(seconds=61)) == 1
    assert second.lease_next(NAMESPACE, NOW + timedelta(seconds=61)).id == job.id
```

Also cover strict canonical-v1 fingerprinting and transactional v1-to-v2 migration rollback; quoted-literal case, every persistent trigger/view/object, exact table/FK/index pragma metadata, malformed columns/types/defaults/checks/FKs/index uniqueness/origin/partial/columns, cross-table owner byte counts, and malformed/duplicate/non-object/noncanonical JSON; WAL/foreign keys/busy timeout; secure database/sidecar establishment; POSIX descriptor-relative no-follow operations and explicitly path-based locked maintenance scanning; deterministic Windows native reparse, exact parsed-DACL, write-capable flush, write-through no-replace/EEXIST, and lock-contention seams; Windows-marked runtime ACL/publication/unlink/locking tests independent of optional reparse creation; 16 MiB exact/overflow; source inode/size/mtime change; atomic no-clobber publication preserving an existing/open inode; restrictive read-only/private permissions; propagated file/directory durability failures; idempotent first-root creation races; namespace isolation; deterministic idempotency; concurrent same-hash stages with distinct tokens; a real process barrier that opens between snapshot and enqueue; abandoned-stage expiry/cleanup; snapshot tamper before lease; stale attempt A after recovery/re-lease B; lease expiry CAS; pause/remove invalidation; exact bounded integer lease durations; safe public failure allowlists/control rejection; deduplication, counts, retry-now, and shared-reference/stage cleanup.

- [ ] **Step 2: Run outbox tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_outbox.py -v
```

Expected: import fails because `outbox.py` is absent.

- [ ] **Step 3: Implement strict schema version 2, staging ownership, and fenced atomic operations**

Use these immutable row/view shapes; `Snapshot` carries its one-use random staging owner and a completed job has all three snapshot fields plus lease fields set to `None` after ownership release:

```python
@dataclass(frozen=True, slots=True)
class Snapshot:
    namespace: Namespace
    sha256: str
    relative_path: str
    absolute_path: Path
    byte_count: int
    source_modified_at: datetime
    staging_token: str

@dataclass(frozen=True, slots=True)
class Job:
    id: str
    namespace: Namespace
    roast_uuid: UUID
    content_sha256: str
    snapshot_sha256: str | None
    snapshot_path: Path | None
    snapshot_byte_count: int | None
    aroast_json: str
    revision_json: str
    idempotency_key: str
    state: Literal['pending', 'leased', 'retry_wait', 'paused', 'failed', 'complete']
    attempts: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    lease_token: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class QueueCounts:
    pending: int
    retrying: int
    paused: int
    failed: int
    complete: int

@dataclass(frozen=True, slots=True)
class FailedJob:
    id: str
    roast_uuid: UUID
    sha256: str
    attempts: int
    next_attempt_at: datetime | None
    error_code: str
    error_message: str
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job: Job
    created: bool
```

The SQL block below is the canonical version-1 fingerprint accepted as migration input, not the final schema. Compare its normalized `sqlite_master` SQL plus table/index/foreign-key pragmas exactly before changing it; any malformed or unknown unreleased queue schema fails closed. Timestamps are canonical aware UTC text and UUIDs are lowercase 32-hex:

```sql
CREATE TABLE schema_version (
    version INTEGER NOT NULL CHECK (version = 1)
);
CREATE TABLE namespaces (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    organization_uuid TEXT NOT NULL CHECK (length(organization_uuid) = 32),
    namespace_key TEXT NOT NULL UNIQUE CHECK (length(namespace_key) = 64),
    UNIQUE(origin, organization_uuid)
);
CREATE TABLE snapshots (
    namespace_id INTEGER NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    relative_path TEXT NOT NULL UNIQUE,
    byte_count INTEGER NOT NULL CHECK (byte_count BETWEEN 1 AND 16777216),
    created_at TEXT NOT NULL,
    PRIMARY KEY(namespace_id, sha256)
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY CHECK (length(id) = 32),
    namespace_id INTEGER NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    roast_uuid TEXT NOT NULL CHECK (length(roast_uuid) = 32),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    snapshot_sha256 TEXT,
    snapshot_relative_path TEXT,
    snapshot_byte_count INTEGER,
    aroast_json TEXT NOT NULL,
    revision_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) <= 255),
    state TEXT NOT NULL CHECK (state IN
      ('pending','leased','retry_wait','paused','failed','complete')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(namespace_id, roast_uuid, content_sha256),
    FOREIGN KEY(namespace_id, snapshot_sha256)
      REFERENCES snapshots(namespace_id, sha256)
);
CREATE INDEX jobs_ready_idx
  ON jobs(namespace_id, state, next_attempt_at, created_at);
```

Transactionally rebuild that canonical input as schema version 2. Version 2 changes `schema_version` to `CHECK (version = 2)`, adds nullable unique `jobs.lease_token TEXT CHECK (lease_token IS NULL OR length(lease_token) = 32)`, enforces leased/non-leased token+expiry and complete/active snapshot state checks, and adds exactly:

```sql
CREATE TABLE snapshot_staging (
    token TEXT PRIMARY KEY CHECK (length(token) = 32),
    namespace_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    relative_path TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count BETWEEN 1 AND 16777216),
    source_modified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(namespace_id, sha256)
      REFERENCES snapshots(namespace_id, sha256) ON DELETE CASCADE
);
CREATE INDEX snapshot_staging_expiry_idx
  ON snapshot_staging(expires_at);
```

Open with `PRAGMA journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, and `busy_timeout=5000`. Securely establish the database and any existing WAL/SHM sidecars inside the verified private root while holding the cross-process lock before SQLite access. `snapshot_saved_file()` holds that lock through atomic no-clobber publication, read-only/private hardening, directory durability, and durable insertion of a random expiring `snapshot_staging` row; same-hash owners coexist. `enqueue()` atomically validates and consumes exactly its unexpired token. Startup expires stages and deletes a snapshot row/file only when no job and no unexpired stage references it; unindexed crash residue is then collected.

Generate paths only as `<namespace-key>/snapshots/<sha-prefix>/<sha>.alog`; reject absolute, noncanonical, or traversing stored paths. POSIX security-critical opens/publication/removal are descriptor-relative and no-follow; generated-tree startup maintenance may scan path-wise only while holding the process lock, rejecting links/reparse entries under the non-malicious-same-user boundary. Windows operations use native no-reparse handles componentwise, an ACE-enumerated exact protected current-user DACL, write-capable synchronized directory flush handles, and `MoveFileExW` write-through publication without replacement; access-denied and other supported durability failures propagate. Existing content is verified and reused after EEXIST without inode replacement, and the already-flushed temporary content is not reopened read-only for an invalid write flush after publication. First-root creation races are idempotent and reverify/harden the winner before opening the process lock. Verify read-only/private size and SHA before leasing. Every lease gets a random unique token; complete/retry/fail uses `WHERE id=? AND state='leased' AND lease_token=? AND lease_expires_at>?` and raises exactly `OutboxError('lease_lost')` unless one row changes. Recovery, pause, and removal clear/invalidate ownership. Completion/removal deletes the snapshot only after proving no job/stage owner in the same transaction. Validate canonical duplicate-free JSON objects, exact schema pragmas, cross-table snapshot path/byte-count ownership, and durable state on write/read; expose only fixed allowlisted failure text/codes, reject controls, propagate supported durability failures, and require `type(lease_seconds) is int` in `1..86400`. The idempotency key remains exactly `archive-v1:{client_uuid.hex}:{roast_uuid.hex}:{sha256}`.

- [ ] **Step 4: Run outbox tests GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_outbox.py -v
```

Expected: all persistence, restart, and filesystem tests pass on the temporary root.

- [ ] **Step 5: Commit the durable outbox**

```bash
git add src/artisanlib/roastserver/outbox.py \
  src/test/unitary/artisanlib/roastserver/test_outbox.py
git commit -m "feat(roastserver): add durable revision outbox"
```

---

### Task 6: Add verified namespaced cache and offline archive rows

**Files:**
- Create: `src/artisanlib/roastserver/cache.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_cache.py`

**Interfaces:**
- Consumes: private cache root, `Namespace`, strict `RoastDetail`, `DownloadReceipt`, staged connector-generated file, archive filters, and protected paths.
- Produces:
  - `new_staging_file(namespace: Namespace) -> tuple[Path, BinaryIO]`
  - `discard_staging(path: Path) -> None`
  - `close() -> None` plus `CacheStore` context-manager lifecycle
  - `publish(namespace, detail, receipt, staged_path, validated_at) -> CachedRevision`
  - `find_current(namespace, roast_uuid, revision_number, sha256) -> CachedRevision | None`
  - `validate(cached: CachedRevision) -> CachedRevision`
  - `list_offline(namespace, filters) -> CachedPage`
  - `stats(namespace) -> CacheStats`
  - `prune(namespace, limit_bytes, protected_paths) -> CacheStats`
  - `clear_unused(namespace, protected_paths) -> CacheStats`.

- [ ] **Step 1: Write publication, corruption, isolation, offline, and pruning tests**

```python
def test_publish_uses_generated_path_and_public_sidecar(cache: CacheStore, staged_download) -> None:
    cached = cache.publish(NAMESPACE, DETAIL, RECEIPT, staged_download, NOW)
    assert cached.path.name == f'1-{RECEIPT.sha256}.alog'
    sidecar = json.loads(cached.sidecar_path.read_text(encoding='utf-8'))
    assert sidecar['schema_version'] == 1
    assert set(sidecar) == {'schema_version', 'origin', 'organization_uuid',
                            'roast', 'revision', 'downloaded_at'}
    assert 'credential' not in json.dumps(sidecar).casefold()


def test_corrupt_cached_profile_is_not_openable(cache: CacheStore, cached_revision) -> None:
    cached_revision.path.write_bytes(b'corrupt')
    with pytest.raises(CacheError) as raised:
        cache.validate(cached_revision)
    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT


def test_prune_never_deletes_open_path(cache: CacheStore, three_cached_revisions) -> None:
    protected = frozenset({three_cached_revisions[0].path})
    cache.prune(NAMESPACE, limit_bytes=1, protected_paths=protected)
    assert three_cached_revisions[0].path.exists()
```

Also test atomic sidecar/profile publication, temp removal after each failure, exact sidecar parser, exact `revision.revision_number == roast.revision_count`, lossless frozen JSON and sidecar round trips for empty objects/arrays and pair-shaped arrays, 16 MiB cap, checksum and byte count, a different origin/org namespace, newest-first offline filtering, labels retained read-only, one latest cached row per roast, stale status, LRU by `downloaded_at`, clear-unused behavior, and failure when sidecar identity does not match generated path. Add protected-path tests for missing, inaccessible, symlink/reparse, non-regular, replaced/racing, open-descriptor, generated-path, and hard-link aliases; all supplied paths must resolve to retained stable regular identities before the first deletion.

Add causal cleanup tests that replace a stage pathname after its retained identity observation but immediately before its atomic quarantine move, add a hard-link alias, inject the first quarantine deletion failure, and prove every later sidecar/profile/publication-temp/stage cleanup is still attempted while neither the replacement pathname nor other hard links are removed. Install another replacement at the original name after quarantine and prove it survives. Add deterministic thread barriers proving concurrent stage creation and `close()` linearize without stranded path pairs or descriptors, plus real subprocess barriers proving an arbitrarily old active stage survives maintenance, explicit owner discard removes its pair, a process crash makes its pair collectible, and two first-root publishers targeting the same destination both complete with one valid pair. Add portable complete-flow native seams and `@pytest.mark.win32` runtime tests for cache ACLs, lock contention, reparse/junction containment, replacement, file/directory flush failures, quarantine moves, verified-handle deletion, and deletion; non-Windows runs must report native Windows tests as skipped rather than passed.

- [ ] **Step 2: Run cache tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_cache.py -v
```

Expected: import fails because `cache.py` is absent.

- [ ] **Step 3: Implement generated cache layout and sidecar schema**

```python
@dataclass(frozen=True, slots=True)
class CachedRevision:
    namespace: Namespace
    roast: RoastSummary
    revision: Revision
    path: Path
    sidecar_path: Path
    downloaded_at: datetime

@dataclass(frozen=True, slots=True)
class CacheStats:
    byte_count: int
    revision_count: int

@dataclass(frozen=True, slots=True)
class CachedPage:
    items: tuple[CachedRevision, ...]
```

Use `<namespace-key>/roasts/<roast-uuid>/<revision-number>-<sha256>.alog` and an adjacent `.json`; server strings never form paths. Each externally written stage is a generated `<namespace-key>/tmp/<token>.part` plus `<token>.lock`. Hold the shared global process lock while creating or scanning pairs and retain a blocking OS-exclusive lock descriptor on `<token>.lock` from creation through download, validation, publication or explicit discard, exhaustive cleanup, and release. A per-store state `RLock`, always acquired before the filesystem/process lock, linearizes the open-state check plus stage creation/registration against the close transition plus stage capture. `close()` marks closed while holding that lock, internally consumes every captured pair despite the closed public state, attempts all captures after failures, releases every descriptor, and is idempotent; `discard_staging()` consumes exactly one owned pair while open. Maintenance uses a nonblocking POSIX `flock`/Windows byte-range lock from the shared filesystem layer: contention preserves an active stage without consulting mtime, while acquisition proves process abandonment and triggers exhaustive removal of both exact generated pair paths.

Write publication artifacts under fresh generated `.part` paths, flush/fsync, chmod files `0o600` and directories `0o700` where supported, then replace the profile before the sidecar. If any operation or cleanup fails, attempt every applicable sidecar, profile, publication temporary, stage part, and stage lock action independently before raising only fixed `CacheError`. The shared identity-bound removal primitive, called while the process lock serializes internal connector mutations, atomically moves the original pathname to a connector-generated quarantine name containing the expected stable identity and a random token in the same verified directory, using native no-replace/write-through operations where available, then verifies the moved object and deletes only the expected identity. Startup applies that encoded expectation to crash residue and preserves/fails closed on a mismatch. On mismatch it restores without deleting when possible; it never overwrites a later original-name replacement, never scans/removes hard-link aliases, and Windows prefers deletion through the same verified handle. Same-user adversarial mutation remains outside the threat model. Publication returns only after stage cleanup succeeds and otherwise rolls back pair paths it published. `validate()` must re-read and strictly parse the sidecar, require cached current revision equality, stream/hash the profile, and compare all identity/size/checksum fields before returning. Contract frozen JSON represents arrays with an explicit immutable tag while preserving object tuple compatibility so sidecar serialization exactly reconstructs arrays and objects. Pruning first securely opens every supplied protected path without links/reparse points, requires and retains a stable regular descriptor identity, aborts before any deletion on any resolution or revalidation failure, then sorts unprotected entries by downloaded time oldest first and attempts sidecar-first/profile-second exact-identity cleanup.

- [ ] **Step 4: Run cache tests GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/roastserver/test_cache.py -v
```

Expected: all cache and offline tests pass.

- [ ] **Step 5: Commit the verified cache**

```bash
git add src/artisanlib/roastserver/cache.py \
  src/test/unitary/artisanlib/roastserver/test_cache.py
git commit -m "feat(roastserver): add verified offline cache"
```

---

### Task 7: Process queue and archive commands in one QObject worker

**Files:**
- Create: `src/artisanlib/roastserver/worker.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_worker.py`

**Interfaces:**
- Consumes: `Outbox`, `CacheStore`, `CredentialStore`, injected `ClientFactory`, injected clock, `OpaqueVault` entries, and credential-free `WorkerConfiguration`.
- Produces `RoastServerWorker` slots `start()`, `configure(object)`, `test_connection(str)`, `enqueue_saved(str)`, `retry_job(str)`, `remove_job(str)`, `browse(str)`, `open_online(str)`, `open_cached(str)`, `publish_staged(str)`, `discard_staged(str)`, `clear_unused(str)`, and `stop()`; signals are those below.

- [ ] **Step 1: Add worker delivery, retry, credential, browse, and signal tests**

Use a fake client factory and real temporary Outbox/CacheStore. Invoke slots on a real `QThread`, capture `QSignalSpy`/thread IDs, and process Qt events with bounded polling. Cover:

```python
def test_delivery_posts_aroast_before_exact_snapshot_upload(worker_harness) -> None:
    job = worker_harness.enqueue_saved_profile()
    complete = Mock(wraps=worker_harness.outbox.mark_complete)
    worker_harness.outbox.mark_complete = complete
    worker_harness.run_one_queue_tick()
    assert worker_harness.client.calls == [
        ('post_aroast', job.roast_uuid, job.aroast_json.encode()),
        ('upload_revision', job.roast_uuid, job.content_sha256,
         job.idempotency_key, job.revision_json.encode(), job.snapshot_sha256),
    ]
    leased = worker_harness.last_leased_job
    assert leased.lease_token is not None
    complete.assert_called_once_with(leased.id, leased.lease_token, worker_harness.now)
    assert worker_harness.outbox.counts(NAMESPACE).complete == 1
    assert not job.snapshot_path.exists()


def test_401_pauses_namespace_without_deleting_keyring_entry(worker_harness) -> None:
    worker_harness.client.failure = api_failure(401)
    worker_harness.run_one_queue_tick()
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    assert worker_harness.credentials.delete_calls == []


def test_public_signals_are_emitted_on_worker_thread_without_secret(worker_harness) -> None:
    request_id = worker_harness.request_connection_test()
    payload = worker_harness.wait_for_signal('connectionTested')
    assert payload[0] == request_id
    assert worker_harness.ephemeral_secret not in repr(payload)
    assert worker_harness.signal_thread != worker_harness.ui_thread
```

Test transient delays `min(5 * 2 ** (attempts - 1), 300)` and `max(backoff, retry_after)`, persisted retry after restart, permanent 4xx/local corruption failure, expired lease recovery, interruption leaving a lease recoverable, stale attempt A being unable to complete/retry/fail after recovery and lease B, disabled/removal pause, credential restoration resume, duplicate/current-hash success, queue counts, failed-job retry/remove, browse retained-cache fallback, online detail/download staging, cached validation, cache pruning, and each safe signal payload. Every terminal/retry assertion must prove the worker passes the token returned on that exact lease. Every online-open failure after stage creation—including HTTP streaming, checksum/header, UI deserialization/validation, publish-command vault loss, interruption, and shutdown—must prove `discard_staging()` or consuming `publish()` ran; stop must prove `CacheStore.close()` attempted every remaining stage after the timer stopped.

- [ ] **Step 2: Run worker tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_worker.py -v
```

Expected: import fails because `worker.py` is absent.

- [ ] **Step 3: Implement the opaque vault, command payloads, timers, and signals**

```python
T = TypeVar('T')

class OpaqueVault(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, T] = {}

    def put(self, value: T) -> str:
        request_id = uuid.uuid4().hex
        with self._lock:
            self._values[request_id] = value
        return request_id

    def take(self, request_id: str) -> T:
        with self._lock:
            return self._values.pop(request_id)

@dataclass(frozen=True, slots=True)
class WorkerConfiguration:
    origin: str
    namespace: Namespace | None
    enabled: bool
    automatic_upload: bool
    client_instance_uuid: UUID
    cache_limit_bytes: int

@dataclass(frozen=True, slots=True)
class ConnectionTestRequest:
    origin: str
    credential: str

@dataclass(frozen=True, slots=True)
class SavedProfileRequest:
    namespace: Namespace
    path: Path
    profile: ProfileData | None
    manual: bool

@dataclass(frozen=True, slots=True)
class BrowseRequest:
    namespace: Namespace
    filters: ArchiveFilters
    cursor: str | None
    refresh: bool

@dataclass(frozen=True, slots=True)
class OnlineOpenRequest:
    namespace: Namespace
    roast_uuid: UUID

@dataclass(frozen=True, slots=True)
class CachedOpenRequest:
    cached: CachedRevision

@dataclass(frozen=True, slots=True)
class PublishRequest:
    detail: RoastDetail
    receipt: DownloadReceipt
    staged_path: Path

class RoastServerWorker(QObject):
    connectionTested = pyqtSignal(str, object)
    operationFailed = pyqtSignal(str, object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    archivePageReady = pyqtSignal(str, object)
    downloadStaged = pyqtSignal(str, object)
    cachedReady = pyqtSignal(str, object)
    cachePublished = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    stopped = pyqtSignal()
```

Create the `QTimer` in `start()` after the worker has moved threads. Each timeout leases at most one job, executes compatible metadata then multipart upload, commits one terminal/retry state, emits public aggregates, and schedules the next due timestamp. Candidate credentials and `ProfileData` live only in separate injected `OpaqueVault` instances; their signals contain request IDs only. For connection testing, take the candidate, call `/auth/me`, write keyring only after success, discard the local reference in `finally`, then emit identity. On startup/configuration retrieve the active credential directly from `CredentialStore` in the worker. On delivery, require `job.lease_token is not None`, call `upload_revision(job.roast_uuid, job.content_sha256, job.idempotency_key, job.revision_json.encode('utf-8'), snapshot_file)`, and require its parsed response to match the job. Commit only through `mark_complete(job.id, job.lease_token, now)`, `mark_retry(job.id, job.lease_token, now, next_attempt_at, failure)`, or `mark_failed(job.id, job.lease_token, now, failure)`; treat fixed `lease_lost` as stale ownership and never retry a transition with a newer token.

For online opens, retain the returned cache stage path until exactly one terminal owner action: UI validation failure and every pre-publication cancellation/error command `discard_staging(path)`; `publish()` consumes it on both success and failure. Do not unlink stage paths directly. A lost/invalid opaque publish request is a discard action, and retrying requires a newly generated stage. For cache pruning, union the controller-provided open cache paths with `Outbox.protected_paths()` before deletion, even though upload snapshots live under a separate generated subtree; propagate fixed cache failure if any protected path cannot be securely resolved rather than retrying without it. On stop, set a `threading.Event`, stop the timer, drain/discard pending cache stage commands, call `CacheStore.close()` to attempt every owned stage and release locks, close SQLite, and emit `stopped`; never call `QThread.terminate()`.

- [ ] **Step 4: Run worker, outbox, and cache tests GREEN**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_worker.py \
  test/unitary/artisanlib/roastserver/test_outbox.py \
  test/unitary/artisanlib/roastserver/test_cache.py -v
```

Expected: all pass with fake clients/keyrings and temporary files only.

- [ ] **Step 5: Commit worker processing**

```bash
git add src/artisanlib/roastserver/worker.py \
  src/test/unitary/artisanlib/roastserver/test_worker.py
git commit -m "feat(roastserver): process archive work off UI thread"
```

---

### Task 8: Add the main-thread lifecycle/controller façade

**Files:**
- Create: `src/artisanlib/roastserver/controller.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_controller.py`

**Interfaces:**
- Consumes: `SettingsStore`, `CredentialStore`, data root, injected client factory, `profile_validator: Callable[[Path], None]`, worker/vaults, and an `ApplicationWindow` connection to `profileReady`.
- Produces controller methods:
  - `start() -> None`, `shutdown(timeout_ms: int = 15_000) -> bool`
  - `test_connection(origin: str, candidate: str) -> str`
  - `apply_options(origin: str, enabled: bool, automatic_upload: bool, cache_limit_bytes: int) -> None`
  - `remove_credential() -> None`
  - `saved_profile(path: Path, profile: ProfileData) -> None`, `manual_upload(path: Path) -> None`
  - `refresh_queue() -> None`, `retry_job(job_id: str) -> None`, `remove_job(job_id: str) -> None`
  - `browse(filters: ArchiveFilters, refresh: bool = True) -> str`, `load_more() -> str | None`
  - `open_roast(roast_uuid: UUID) -> str`, `open_cached(cached: CachedRevision) -> str`
  - `clear_unused_cache() -> None`
  - `record_open_source(path: Path, source: ServerProfileSource) -> None`, `record_local_save(path: Path) -> None`
  - dialogs/main never access worker/stores directly.

- [ ] **Step 1: Write controller state-machine and non-blocking façade tests**

```python
def test_auto_upload_cannot_enable_before_confirmed_test(controller: RoastServerController) -> None:
    with pytest.raises(ControllerError, match='Test the connection'):
        controller.apply_options(origin='https://example.test', enabled=True,
                                 automatic_upload=True,
                                 cache_limit_bytes=512 * 1024 * 1024)


def test_candidate_credential_crosses_only_the_vault(controller_harness) -> None:
    request_id = controller_harness.controller.test_connection(
        'https://example.test', controller_harness.ephemeral_secret)
    assert controller_harness.secret_vault.contains(request_id)
    assert controller_harness.worker.test_ids == [request_id]
    assert controller_harness.ephemeral_secret not in repr(controller_harness.worker.calls)


def test_saved_profile_returns_without_snapshot_or_http_on_ui_thread(controller_harness) -> None:
    started = time.monotonic()
    controller_harness.controller.saved_profile(PROFILE_PATH, PROFILE)
    assert time.monotonic() - started < 0.05
    assert controller_harness.enqueue_vault.size() == 1
    assert controller_harness.client.calls == []
```

Also test identity persistence only after worker success, keyring failure leaves old settings and auto off, origin/org namespace switch, `401` clears connected UI state but keeps credential, credential removal pauses work, disabled processing, immutable signal forwarding on main thread, validation-before-publication handshake, cached stale source, open-path protection, idempotent start, and shutdown ordering/wait timeout without terminate.

- [ ] **Step 2: Run controller tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_controller.py -v
```

Expected: import fails because `controller.py` is absent.

- [ ] **Step 3: Implement controller signals, state transitions, and thread ownership**

```python
class RoastServerController(QObject):
    settingsChanged = pyqtSignal(object)
    identityChanged = pyqtSignal(object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    archivePageReady = pyqtSignal(str, object)
    operationFailed = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    profileReady = pyqtSignal(str, object)

    _configureWorker = pyqtSignal(object)
    _testWorker = pyqtSignal(str)
    _enqueueWorker = pyqtSignal(str)
    _browseWorker = pyqtSignal(str)
    _openOnlineWorker = pyqtSignal(str)
    _openCachedWorker = pyqtSignal(str)
    _publishWorker = pyqtSignal(str)
    _stopWorker = pyqtSignal()
```

Construct one `QThread` and one `RoastServerWorker`, connect commands with queued connections, then start only after all connections exist. `saved_profile(path, profile)` returns immediately when disabled/auto-off; otherwise transfer the save-local `ProfileData` object and path through the enqueue vault and emit only its ID. `manual_upload(path)` enqueues a request that deserializes the saved file in the worker. On `downloadStaged`, call the injected Artisan validator in the UI thread while the staged file is still hidden; on success vault a publish command, and on validator failure vault a `discard_staged` command before emitting `INVALID_RESPONSE`. Controller cancellation, opener exceptions, and vault failures must choose the same explicit discard command and must never unlink the path directly. Only after `cachePublished` or `cachedReady` emit `profileReady(str(path), source)`; after `ApplicationWindow.openRoastServerProfile()` reports success through `record_open_source()`, track the path in `_open_cache_paths`. `shutdown()` requests interruption, emits stop (which closes all remaining cache stages), calls `thread.quit()`, waits at most 15 seconds, and returns false with a fixed log message if still running.

- [ ] **Step 4: Run controller/worker tests GREEN**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_controller.py \
  test/unitary/artisanlib/roastserver/test_worker.py -v
```

Expected: all pass and every UI-facing `QSignalSpy` callback observes the main thread.

- [ ] **Step 5: Commit controller lifecycle**

```bash
git add src/artisanlib/roastserver/controller.py \
  src/test/unitary/artisanlib/roastserver/test_controller.py
git commit -m "feat(roastserver): add connector lifecycle controller"
```

---

### Task 9: Build the modeless configuration and failed-job dialog

**Files:**
- Create: `src/artisanlib/roastserver/dialogs.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_dialogs.py`

**Interfaces:**
- Consumes: only `RoastServerController` public methods/signals and frozen settings/identity/queue/cache/failure objects.
- Produces `RoastServerConfigDialog(QDialog)`, `FailedJobsModel(QAbstractTableModel)`, and later archive dialog classes; no HTTP, keyring, SQLite, cache, or plus imports.

- [ ] **Step 1: Write configuration transition and rendering tests**

Instantiate a real offscreen `QApplication` and a signal-capable fake controller. Test:

```python
def test_credential_uses_password_echo_and_auto_upload_starts_disabled(dialog) -> None:
    assert dialog.credential_edit.echoMode() is QLineEdit.EchoMode.Password
    assert not dialog.automatic_upload_check.isChecked()
    assert not dialog.automatic_upload_check.isEnabled()


def test_successful_test_enables_opt_in_and_renders_public_identity(dialog, controller) -> None:
    dialog.server_edit.setText('https://example.test')
    dialog.credential_edit.setText(controller.ephemeral_secret)
    dialog.test_button.click()
    assert controller.test_calls[0][0] == 'https://example.test'
    controller.identityChanged.emit(IDENTITY)
    assert dialog.identity_label.text() == 'Owner — Roastery (admin)'
    assert dialog.automatic_upload_check.isEnabled()
    assert dialog.credential_edit.text() == ''


def test_refresh_failure_keeps_failed_rows_and_shows_safe_plain_text(dialog, controller) -> None:
    controller.failedJobsChanged.emit((FAILED_JOB,))
    controller.operationFailed.emit('queue', SAFE_FAILURE)
    assert dialog.failed_model.rowCount() == 1
    assert dialog.error_label.textFormat() is Qt.TextFormat.PlainText
```

Cover invalid URL, keyring fixed action text, origin change re-locking auto upload, enable/auto save calls, counts for pending/retrying/paused/failed, stable failed columns (roast UUID, attempts, next try, category/message), per-row Retry/Remove buttons, cache bytes, Clear unused cache, geometry round trip, disable/credential removal retaining data, and modeless close/hide behavior.

- [ ] **Step 2: Run dialog configuration tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_dialogs.py -k 'config or failed or credential' -v
```

Expected: import fails because `dialogs.py` is absent.

- [ ] **Step 3: Implement code-built configuration UI and table model**

Use `QApplication.translate('RoastServer', text)` for all labels and fixed messages. The dialog contains `server_edit`, password `credential_edit`, `test_button`, identity labels, `enabled_check`, `automatic_upload_check`, four count labels, `QTableView` with `FailedJobsModel`, Retry/Remove controls, cache label, `clear_cache_button`, and Close. Set every error/status label to plain text and `setOpenExternalLinks(False)`. Connect buttons only to controller methods. Never retain candidate text in the model, properties, settings, or exception objects; clear the credential edit after either successful storage or dialog close.

- [ ] **Step 4: Run configuration dialog tests GREEN**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_dialogs.py -k 'config or failed or credential' -v
```

Expected: selected tests pass offscreen without a keyring daemon or HTTP.

- [ ] **Step 5: Commit the configuration UI**

```bash
git add src/artisanlib/roastserver/dialogs.py \
  src/test/unitary/artisanlib/roastserver/test_dialogs.py
git commit -m "feat(roastserver): add connector configuration dialog"
```

---

### Task 10: Add cursor browsing, offline rows, and verified-open UX

**Files:**
- Modify: `src/artisanlib/roastserver/dialogs.py`
- Modify: `src/artisanlib/roastserver/controller.py`
- Modify: `src/artisanlib/roastserver/worker.py`
- Test: `src/test/unitary/artisanlib/roastserver/test_dialogs.py`
- Test: `src/test/unitary/artisanlib/roastserver/test_controller.py`
- Test: `src/test/unitary/artisanlib/roastserver/test_worker.py`

**Interfaces:**
- Consumes: `archivePageReady`, `operationFailed`, `onlineChanged`, `profileReady`; controller `browse(filters, refresh)`, `load_more()`, `open_roast(uuid)`, and `open_cached(cached)`.
- Produces `RoastTableModel`, `RoastServerBrowserDialog`, immutable `ArchivePageView(rows, next_cursor, online, retained_error)` and staged/cached open flow.

- [ ] **Step 1: Add pagination, retained-error, offline, and open tests**

```python
def test_refresh_failure_retains_rows_and_displays_error(browser, controller) -> None:
    controller.archivePageReady.emit('first', ONLINE_PAGE)
    browser.refresh_button.click()
    controller.operationFailed.emit('refresh', OFFLINE_FAILURE)
    assert browser.roast_model.rowCount() == len(ONLINE_PAGE.rows)
    assert browser.error_label.text() == 'Offline / server unavailable.'


def test_load_more_appends_without_duplicates_and_fallback_is_accessible(browser, controller) -> None:
    controller.archivePageReady.emit('first', PAGE_ONE)
    assert browser.load_more_button.isEnabled()
    browser.load_more_button.click()
    controller.archivePageReady.emit('next', PAGE_TWO_WITH_OVERLAP)
    assert browser.roast_model.roast_uuids() == (ROAST_ONE, ROAST_TWO)
    assert browser.load_more_button.accessibleName() == 'Load more server roasts'


def test_offline_cached_open_marks_source_stale(browser, controller) -> None:
    controller.archivePageReady.emit('offline', CACHED_PAGE)
    browser.select_roast(ROAST_ONE)
    browser.open_button.click()
    controller.open_cached.assert_called_once_with(CACHED_REVISION)
    controller.profileReady.emit(str(CACHED_REVISION.path), STALE_SERVER_SOURCE)
    assert STALE_SERVER_SOURCE.stale is True
```

Cover search trim/200-char cap and debounce, state/machine filters, UTC start/end boundaries, newest-first rows, columns roast date/title/batch/machine/labels/parse state/revisions/cache, immutable read-only labels, scrollbar near-end auto-page, visible Load more fallback, refresh replacing only on success, online/offline/stale indicators, awaiting-profile Open disabled, current revision detail resolution, exact download staging, validation failure leaving active profile callback untouched, checksum failure temp cleanup, verified cache fallback offer, and corrupt cached refusal.

- [ ] **Step 2: Run browser/open tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_dialogs.py -k 'browser or page or offline or open' \
  test/unitary/artisanlib/roastserver/test_controller.py -k 'download or cached or profile' \
  test/unitary/artisanlib/roastserver/test_worker.py -k 'browse or download or cached' -v
```

Expected: failures because archive model/dialog and complete open command path do not exist.

- [ ] **Step 3: Implement modeless browser and retained-result paging**

```python
@dataclass(frozen=True, slots=True)
class ArchiveRow:
    roast: RoastSummary
    cached_revision: int | None
    cached_sha256: str | None
    stale: bool

@dataclass(frozen=True, slots=True)
class ArchivePageView:
    rows: tuple[ArchiveRow, ...]
    next_cursor: str | None
    online: bool
    retained_error: PublicFailure | None
```

`RoastTableModel` returns display strings only, joins label names without rich text, and exposes UUID/cached data through custom roles. `RoastServerBrowserDialog` is modeless, uses a single-shot 300 ms search timer, converts `QDate` filters to aware UTC inclusive bounds, retains the previous model on failure, and de-duplicates append pages by roast UUID. Connect vertical-scroll maximum proximity and the explicit translated `Load more` button to the same controller call.

Complete worker/controller flow: online Open fetches detail, requires `current_revision`, streams into cache staging, and emits public receipt/detail/path; controller validates, commands atomic publish, then calls the opener with `stale=False`. On retryable online failure, worker validates an exact cached current revision if known and emits a fallback object; the browser asks with a fixed plain-text `QMessageBox` before `open_cached()`. Offline rows open only after cache revalidation and use `stale=True`.

- [ ] **Step 4: Run all dialog/controller/worker tests GREEN**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver/test_dialogs.py \
  test/unitary/artisanlib/roastserver/test_controller.py \
  test/unitary/artisanlib/roastserver/test_worker.py -v
```

Expected: all pass with no external request and no active-profile callback on any pre-open failure.

- [ ] **Step 5: Commit archive browsing and open flow**

```bash
git add src/artisanlib/roastserver/dialogs.py \
  src/artisanlib/roastserver/controller.py \
  src/artisanlib/roastserver/worker.py \
  src/test/unitary/artisanlib/roastserver/test_dialogs.py \
  src/test/unitary/artisanlib/roastserver/test_controller.py \
  src/test/unitary/artisanlib/roastserver/test_worker.py
git commit -m "feat(roastserver): browse and verify archived profiles"
```

---

### Task 11: Wire menus, successful saves, startup, and bounded shutdown

**Files:**
- Modify: `src/artisanlib/main.py:1412-1889,2239-2255,4357-4440,13297-13358,17374-17446,20247-21620,28348-28390`
- Modify: `src/test/unitary/artisanlib/test_main.py`

**Interfaces:**
- Consumes: `RoastServerController`, `RoastServerConfigDialog`, `RoastServerBrowserDialog`, existing `getProfile()/serialize()/fileSave()/automaticsave()/getDataDirectory()` paths.
- Produces main slots `showRoastServerConfig()`, `showServerRoasts()`, `uploadToRoastServer()`, `validateRoastServerProfile()`, and `openRoastServerProfile()`; adds controller/action/dialog slots to `ApplicationWindow.__slots__`.

- [ ] **Step 1: Add menu, save/autosave, manual eligibility, and lifecycle tests**

Append focused tests using `ApplicationWindow.__new__`, existing Qt isolation, temporary `.alog` files, and fake controller/dialogs:

```python
def test_successful_save_notifies_connector_after_serialize(tmp_path: Path) -> None:
    window, controller = save_window(tmp_path, automatic=True)
    ordered = Mock()
    with patch('artisanlib.main.serialize') as serialize_mock:
        ordered.attach_mock(serialize_mock, 'serialize')
        ordered.attach_mock(controller.saved_profile, 'saved_profile')
        assert window.fileSave(str(tmp_path / 'saved.alog'))
    assert [entry[0] for entry in ordered.mock_calls] == ['serialize', 'saved_profile']
    controller.saved_profile.assert_called_once_with(tmp_path / 'saved.alog', window.profile)


def test_failed_or_copy_save_does_not_auto_enqueue(tmp_path: Path) -> None:
    window, controller = save_window(tmp_path, automatic=True)
    with patch('artisanlib.main.serialize', side_effect=OSError('write failed')):
        assert not window.fileSave(str(tmp_path / 'failed.alog'))
    controller.saved_profile.assert_not_called()
    with patch('artisanlib.main.serialize'):
        assert window.fileSave(str(tmp_path / 'copy.alog'), copy=True)
    controller.saved_profile.assert_not_called()


def test_manual_upload_requires_clean_saved_alog(window_with_controller) -> None:
    window_with_controller.curFile = None
    window_with_controller.uploadToRoastServer()
    window_with_controller.roastserver_controller.manual_upload.assert_not_called()
```

Also assert autosave hook follows successful serialization, auto-off makes controller return without I/O, Save As uses the chosen path, save copy is excluded, manual action never calls HTTP synchronously, File menu order includes `Server Roasts...` and `Upload to Roast Server`, Config includes `Roast Server...` in all UI modes, modeless dialogs are reused/raised, controller is created after settings load/data path readiness, and accepted shutdown calls controller before `QApplication.exit()` with no unsafe thread termination.

- [ ] **Step 2: Run focused main tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/test_main.py -k 'roast_server or roastserver' -v
```

Expected: selected tests fail because actions, controller ownership, and save hooks are absent.

- [ ] **Step 3: Add narrow ApplicationWindow actions and successful-save hooks**

Add initialized attributes:

```python
self.roastserver_controller: RoastServerController | None = None
self.roastserver_config_dialog: RoastServerConfigDialog | None = None
self.roastserver_browser_dialog: RoastServerBrowserDialog | None = None
self.roastServerUploadAction = QAction(
    QApplication.translate('Menu', 'Upload to Roast Server'), self)
self.roastServerRoastsAction = QAction(
    QApplication.translate('Menu', 'Server Roasts...'), self)
self.roastServerConfigAction = QAction(
    QApplication.translate('Menu', 'Roast Server...'), self)
```

Place server roasts/upload after Save As and before exports in File; place configuration before the UI-mode selector in Config. Slots create each modeless dialog once, then `show()`, `raise_()`, and `activateWindow()`. Manual upload requires non-viewer mode, a clean profile, non-null `curFile`, `.alog` suffix, and an existing regular file; otherwise show a fixed translated message.

Immediately after successful `serialize()` in non-copy `fileSave()` and `automaticsave()`, call `controller.saved_profile(Path(filename), pf)`; do not alter serialization, plus hashing, registration, or save return behavior. In `main()`, after `settingsLoad()` and data paths are ready, create the controller with `<getDataDirectory()>/roastserver`, connect `profileReady` to `openRoastServerProfile`, and start it. In accepted `closeApp()`, call `shutdown(15_000)` before device teardown and continue shutdown after a fixed timeout message.

- [ ] **Step 4: Run focused main and connector tests GREEN**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/test_main.py -k 'roast_server or roastserver' \
  test/unitary/artisanlib/roastserver -v
```

Expected: selected main and all connector tests pass; existing unrelated main selections are not changed.

- [ ] **Step 5: Commit application hooks**

```bash
git add src/artisanlib/main.py src/test/unitary/artisanlib/test_main.py
git commit -m "feat(roastserver): wire menus and saved profile hooks"
```

---

### Task 12: Make server-sourced loading read-only and prove plus coexistence

**Files:**
- Modify: `src/artisanlib/main.py:13703-13845,15686-15755,17390-17446`
- Modify: `src/test/unitary/artisanlib/test_main.py`
- Create: `src/test/unitary/artisanlib/roastserver/test_coexistence.py`

**Interfaces:**
- Consumes: `ServerProfileSource` from a cache-validated controller callback.
- Produces `ApplicationWindow.loadFile(filename: str, quiet: bool = False, *, server_source: ServerProfileSource | None = None) -> bool`, `validateRoastServerProfile(path: Path) -> None`, and `openRoastServerProfile(path: Path, source: ServerProfileSource) -> bool`.

- [ ] **Step 1: Add read-only rollback, Save As, and coexistence tests**

```python
def test_server_load_skips_recent_register_and_sync(server_load_window, tmp_path: Path) -> None:
    cache_file = tmp_path / 'cache.alog'
    cache_file.write_text(repr(valid_profile()), encoding='utf-8')
    with patch.object(server_load_window, 'plusAddPath') as plus_add, \
         patch('artisanlib.main.plus.sync.sync') as plus_sync, \
         patch('artisanlib.main.QSettings') as settings:
        assert server_load_window.loadFile(
            str(cache_file), server_source=SERVER_SOURCE)
    plus_add.assert_not_called()
    plus_sync.assert_not_called()
    settings.return_value.setValue.assert_not_called()
    assert server_load_window.curFile is None
    assert server_load_window.qmc.plus_file_last_modified is None
    assert server_load_window.qmc.plus_sync_record_hash is None
    server_load_window.qmc.fileCleanSignal.emit.assert_called()


def test_server_open_failure_restores_current_profile(server_load_window, tmp_path: Path) -> None:
    previous = copy.deepcopy(server_load_window.getProfile())
    server_load_window.setProfile.side_effect = [False, True]
    assert not server_load_window.loadFile(str(tmp_path / 'verified.alog'),
                                           server_source=SERVER_SOURCE)
    assert server_load_window.setProfile.call_args_list[-1].args[1] == previous


def test_save_after_server_open_uses_save_as_and_resumes_normal_hooks(server_load_window) -> None:
    server_load_window.curFile = None
    server_load_window.fileSave_current_action()
    server_load_window.ArtisanSaveFileDialog.assert_called_once()
    server_load_window.plusAddPath.assert_called_once()
    server_load_window.roastserver_controller.saved_profile.assert_called_once()
```

In `test_coexistence.py`, AST-scan every production file under `artisanlib/roastserver` for `import plus`/`from plus`, instantiate controller with fake plus sentinel objects, and assert settings/token/outbox/cache/worker sentinel identities and values are unchanged. Test plus connected and disconnected states, no connector status action mutation, no plus UUID registration/sync on server open, ordinary plus behavior after local Save As, package roots distinct from `plus.config.outbox_cache/uuid_cache/sync_cache`, and connector tests before/after the known baseline command do not add module contamination.

- [ ] **Step 2: Run read-only/coexistence tests and verify RED**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/test_main.py -k 'server_load or server_open or save_after_server' \
  test/unitary/artisanlib/roastserver/test_coexistence.py -v
```

Expected: failures because `loadFile()` lacks `server_source`, rollback, and plus-exclusion behavior.

- [ ] **Step 3: Implement prevalidation, rollback, transient source state, and Save As transition**

Before mutating the active profile in server mode, deserialize with existing `artisanlib.util.deserialize()`, apply the same legacy `samplinginterval`/extra-marker normalization, and call `validateProfileDict(..., quiet=True, validate_signature=True)`. Capture the current profile, `curFile`, clean/dirty state, plus modification fields, and previous controller source. Only then reset/apply the verified server profile.

On success in server mode:

```python
self.curFile = None
self.qmc.plus_file_last_modified = None
self.qmc.plus_sync_record_hash = None
self.qmc.fileCleanSignal.emit()
self.updateWindowTitle()
self.roastserver_controller.record_open_source(Path(filename), server_source)
state = QApplication.translate('Message', 'stale cached copy') if server_source.stale \
    else QApplication.translate('Message', 'online verified copy')
self.sendmessage(QApplication.translate(
    'Message', 'Roast Server revision {0} opened read-only ({1})').format(
        server_source.revision_number, state))
```

Do not call `plusAddPath`, `setCurrentFile`, `updatePlusStatus`, `plus.sync.sync`, or schedule update in that branch. If apply/redraw fails, restore the captured profile and its clean/dirty/source state before returning false. A normal local load clears transient server source. A successful Save As from a server source follows the existing ordinary save path, registers plus normally, clears server source, sets the new `curFile`, and becomes eligible for optional connector auto-upload; because standard Save sees `curFile is None`, it necessarily opens Save As and cannot overwrite cache.

- [ ] **Step 4: Run main/read-only/coexistence tests in both orders GREEN**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver \
  test/unitary/artisanlib/test_main.py -k 'roastserver or server_load or server_open or save_after_server' -v
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/test_main.py -k 'roastserver or server_load or server_open or save_after_server' \
  test/unitary/artisanlib/roastserver -v
```

Expected: both commands pass with identical connector test counts and no external calls.

- [ ] **Step 5: Commit read-only coexistence**

```bash
git add src/artisanlib/main.py \
  src/test/unitary/artisanlib/test_main.py \
  src/test/unitary/artisanlib/roastserver/test_coexistence.py
git commit -m "feat(roastserver): open server profiles read only"
```

---

### Task 13: Complete automated gates, optional live validation, and final review

**Files:**
- Review only: all files listed above
- Do not modify: translations/generated/server/dependencies/plus

**Interfaces:**
- Consumes: the complete implementation and committed test suite.
- Produces: reproducible automated evidence, an explicitly optional manual deployment result, a clean reviewed branch, and merge/PR handoff.

- [ ] **Step 1: Re-run the known order-dependent plus baseline unchanged**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/artisanlib/test_main.py test/unitary/plus
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q \
  test/unitary/plus/test_sync.py::TestAddSync::test_add_sync_successful
```

Expected baseline: the combined selection has only the documented `TestAddSync::test_add_sync_successful` failure while the exact node passes alone. Record both results; do not weaken connector tests or edit plus files to mask it.

- [ ] **Step 2: Run all connector and focused integration tests with external access blocked**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
  test/unitary/artisanlib/roastserver \
  test/unitary/artisanlib/test_main.py -k \
  'roastserver or roast_server or server_load or server_open or save_after_server' -v
```

Expected: all selected tests pass; fake session request counts match expectations, fake keyring is used, and no deployed/cloud/hardware access occurs.

- [ ] **Step 3: Run static and security checks**

```bash
cd src
.venv/bin/ruff check artisanlib/roastserver test/unitary/artisanlib/roastserver artisanlib/main.py
.venv/bin/mypy
.venv/bin/pyright
.venv/bin/codespell artisanlib/roastserver test/unitary/artisanlib/roastserver
pylint --disable=C,R,E0401,E0611 --extension-pkg-allow-list=PyQt6 \
  --load-plugins=pylint.extensions.no_self_use,pylint.extensions.private_import \
  artisanlib/roastserver
cd ..
pre-commit run --all-files
```

Expected: checks pass or only already-recorded unrelated baseline failures remain. Confirm `git diff --name-only` contains no dependency, translation, UI/UIC, help, protobuf, plus, server, packaging, or release file.

- [ ] **Step 4: Run the full configured test suite and compare only to recorded baselines**

```bash
cd src
QT_QPA_PLATFORM=offscreen .venv/bin/pytest
```

Expected: connector tests pass in full collection. Report the known plus order-dependent failures distinctly and investigate any new failure whose node/error was absent from Step 1 or pre-implementation full-suite evidence; do not claim a clean full suite when the baseline remains.

- [ ] **Step 5: Self-review spec coverage, placeholder text, and type/interface consistency inline**

Run these mechanical checks, then inspect every spec heading against Tasks 1–12 and fix the plan/implementation mismatch before continuing:

```bash
cd "$(git rev-parse --show-toplevel)"
src/.venv/bin/python - <<'PY'
from pathlib import Path
roots = [Path('docs/superpowers/plans/2026-08-01-artisan-archive-connector.md'),
         Path('src/artisanlib/roastserver'),
         Path('src/test/unitary/artisanlib/roastserver')]
banned = ('TB' + 'D', 'TO' + 'DO', 'implement ' + 'later',
          'fill in ' + 'details', 'similar to ' + 'task',
          'appropriate error ' + 'handling')
for root in roots:
    files = [root] if root.is_file() else root.rglob('*')
    for path in files:
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            assert not any(item.casefold() in text.casefold() for item in banned), path
PY
rg -n 'import plus|from plus' src/artisanlib/roastserver
rg -n 'Authorization|Bearer|credential|token' \
  src/artisanlib/roastserver src/test/unitary/artisanlib/roastserver
```

Expected: first two searches return no implementation placeholders or plus imports. Review every credential-related hit from the third search and prove it is an interface name, fixed redaction logic, or ephemeral-memory test construction—not a value, log, signal, file write, exception payload, or artifact. Cross-check exact names/types from “Shared interfaces” against imports/call sites, SQL columns, signals, and test fixtures.

- [ ] **Step 6: Inspect permissions, files, logs, and the complete diff**

```bash
find "${TMPDIR:-/tmp}" -maxdepth 3 -type f -name '*roastserver*' -print 2>/dev/null || true
git status --short
git diff --check
git diff --stat
git diff -- src/artisanlib/main.py
git log --oneline --decorate -15
```

Expected: no credential-bearing test artifact, no unexpected generated file, no whitespace error, only scoped main hooks, and one reviewable commit per implementation task. On POSIX test roots, assert connector directories are `0700` and snapshots/cache/database files are no broader than `0600`.

- [ ] **Step 7: Perform live integration only with explicit authorization and a newly deployed credential**

Do not run this step unless the user explicitly authorizes contacting `https://artisan.frxhome.chown.lv` after Steps 1–6 and provides/creates a new revocable web-issued credential on this machine. Never paste the credential into a shell, test, issue, plan, log, or file. Enter it only in the password-mode configuration field, then manually:

1. Test `/api/v1/auth/me` and verify displayed user, organization, and role.
2. Confirm automatic upload remains off, opt in explicitly, and save a deterministic local `.alog`.
3. Verify the queued UUID/hash appears as one revision in the web archive; save again unchanged and verify idempotency; make a content change and verify a new revision.
4. Browse/filter/paginate labels, download/open current revision read-only, verify Save invokes Save As, and verify the cache file timestamp/bytes do not change.
5. Disconnect network, reopen the verified cached copy with stale status, then restore network.
6. Verify artisan.plus account/status/queue/register behavior remains unchanged throughout.
7. Revoke the temporary credential in the server web UI unless it is explicitly intended for continued use; remove it from the connector keyring through the dialog when validation is complete.

Expected: all 13 acceptance criteria pass. If authorization or a deployed credential is absent, mark this step “not run—requires explicit deployed access”; automated completion remains valid.

- [ ] **Step 8: Final review and merge handoff**

Request a focused review of security boundaries, SQL/snapshot ownership, redirect/size/checksum validation, Qt thread affinity/shutdown, read-only rollback, and plus independence. Apply review changes through the same RED/GREEN gates, rerun Steps 2–6, then use the repository's normal PR/merge process. Do not squash away the reviewable task commits unless the maintainer requests it, and do not merge with an unexplained new test/static failure.
