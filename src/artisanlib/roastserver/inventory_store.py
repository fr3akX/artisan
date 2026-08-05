#
# ABOUT
# Artisan Roast Server inventory persistence
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# OpenAI, 2026

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Final, Literal, NoReturn, cast
from uuid import UUID, uuid4

from artisanlib.roastserver import _filesystem as secure_filesystem
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    MAX_ERROR_MESSAGE_CODE_POINTS,
    MAX_JSON_BYTES,
    POSTGRESQL_INTEGER_MAX,
    FailureKind,
    Namespace,
    PublicFailure,
)
from artisanlib.roastserver.inventory_contract import (
    MAX_CACHED_LOTS,
    BeanLot,
    InventoryBalance,
    InventoryCommandRequest,
    InventoryConflict,
    InventoryMutationResult,
    InventoryOperation,
    InventoryReservation,
    ProcessingMethod,
    ReservationState,
    build_finalize_request,
    build_release_request,
    build_reserve_request,
)

_SCHEMA_VERSION: Final[int] = 1
_DATABASE_NAME: Final[str] = 'inventory.sqlite3'
_LOCK_NAME: Final[str] = '.inventory.lock'
_BUSY_TIMEOUT_MS: Final[int] = 5_000
_STORAGE_ERROR: Final[str] = 'inventory storage operation failed'
_MAX_ORIGIN_CHARS: Final[int] = 2_048
_MAX_LOT_NAME_CHARS: Final[int] = 200
_MAX_LOT_NAME_BYTES: Final[int] = 800
_MAX_DESCRIPTOR_CHARS: Final[int] = 100
_MAX_DESCRIPTOR_BYTES: Final[int] = 400
_MAX_VARIETALS: Final[int] = 16
_MAX_ERROR_CODE_CHARS: Final[int] = 100
_MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 255
_MAX_LEASE_SECONDS: Final[int] = 24 * 60 * 60
_COMPLETED_RETENTION_DAYS: Final[int] = 30
_DEPENDENCY_FAILURE_CODE: Final[str] = 'dependency_failed'
_DEPENDENCY_FAILURE_MESSAGE: Final[str] = 'Inventory reservation dependency failed.'
_PAUSE_CODES: Final[frozenset[str]] = frozenset(
    {
        'connector_disabled',
        'credential_rejected',
        'credential_removed',
        'inventory_unsupported',
    }
)
_FAILURE_CODES_BY_KIND: Final[dict[FailureKind, frozenset[str]]] = {
    FailureKind.OFFLINE: frozenset(
        {
            'connection_error',
            'deadline_exceeded',
            'inventory_unavailable',
            'offline',
            'request_error',
            'server_unavailable',
            'tls_error',
        }
    ),
    FailureKind.CREDENTIAL_REJECTED: frozenset(
        {'authentication_required', 'credential_rejected'}
    ),
    FailureKind.RATE_LIMITED: frozenset({'rate_limited'}),
    FailureKind.INVALID_RESPONSE: frozenset(
        {'client_closed', 'invalid_response', 'request_error'}
    ),
    FailureKind.INVENTORY_REJECTED: frozenset(
        {
            'bean_lot_archived',
            'bean_lot_not_found',
            'invalid_inventory_transition',
            'invalid_request',
            'inventory_reservation_not_found',
        }
    ),
    FailureKind.INVENTORY_CONFLICT: frozenset(
        {'inventory_idempotency_conflict'}
    ),
    FailureKind.INVENTORY_UNSUPPORTED: frozenset({'inventory_unsupported'}),
    FailureKind.LOCAL_INVENTORY: frozenset({'local_inventory'}),
}
_UUID_HEX_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_NAMESPACE_KEY_RE: Final[re.Pattern[str]] = re.compile(r'^namespace-sha256:([0-9a-f]{64})$')
_PROCESSING_METHODS: Final[frozenset[str]] = frozenset(
    {
        'washed',
        'natural',
        'honey',
        'pulped-natural',
        'wet-hulled',
        'anaerobic',
        'experimental',
        'other',
    }
)

InventoryCommandState = Literal[
    'pending', 'leased', 'retry_wait', 'paused', 'failed', 'complete'
]
InventoryLifecycle = Literal[
    'reserve_queued',
    'reserved',
    'finalize_queued',
    'finalized',
    'release_queued',
    'released',
    'paused',
    'failed',
]

_UUID_CHECK = (
    "length({column}) = 32 AND lower({column}) = {column} "
    "AND {column} NOT GLOB '*[^0-9a-f]*'"
)
_NAMESPACE_KEY_CHECK = (
    "length(namespace_key) = 64 AND lower(namespace_key) = namespace_key "
    "AND namespace_key NOT GLOB '*[^0-9a-f]*'"
)

_SCHEMA_V1_STATEMENTS: tuple[str, ...] = (
    '''CREATE TABLE schema_version (
    version INTEGER NOT NULL CHECK (version = 1)
)''',
    f'''CREATE TABLE namespaces (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK (length(origin) BETWEEN 1 AND {_MAX_ORIGIN_CHARS}),
    organization_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='organization_uuid')}),
    namespace_key TEXT NOT NULL UNIQUE CHECK ({_NAMESPACE_KEY_CHECK}),
    UNIQUE(origin, organization_uuid)
)''',
    f'''CREATE TABLE lot_cache_generations (
    namespace_id INTEGER PRIMARY KEY REFERENCES namespaces(id) ON DELETE CASCADE,
    generation TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='generation')}),
    refreshed_at TEXT NOT NULL CHECK (length(refreshed_at) > 0),
    UNIQUE(namespace_id, generation)
)''',
    f'''CREATE TABLE bean_lots (
    namespace_id INTEGER NOT NULL,
    generation TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='generation')}),
    lot_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='lot_uuid')}),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND {_MAX_LOT_NAME_CHARS}
                              AND length(CAST(name AS BLOB)) <= {_MAX_LOT_NAME_BYTES}),
    origin TEXT CHECK (origin IS NULL OR (length(origin) BETWEEN 1 AND {_MAX_DESCRIPTOR_CHARS}
                         AND length(CAST(origin AS BLOB)) <= {_MAX_DESCRIPTOR_BYTES})),
    varietals_json TEXT NOT NULL CHECK (length(CAST(varietals_json AS BLOB)) <= {MAX_JSON_BYTES}),
    processing_method TEXT CHECK (processing_method IS NULL OR processing_method IN
      ('washed','natural','honey','pulped-natural','wet-hulled','anaerobic','experimental','other')),
    crop_year INTEGER CHECK (crop_year IS NULL OR crop_year BETWEEN 1000 AND 9999),
    on_hand_grams INTEGER NOT NULL CHECK (on_hand_grams BETWEEN -{POSTGRESQL_INTEGER_MAX} AND {POSTGRESQL_INTEGER_MAX}),
    reserved_grams INTEGER NOT NULL CHECK (reserved_grams BETWEEN 0 AND {POSTGRESQL_INTEGER_MAX}),
    available_grams INTEGER NOT NULL CHECK (available_grams BETWEEN -{POSTGRESQL_INTEGER_MAX} AND {POSTGRESQL_INTEGER_MAX}),
    unresolved_conflict_count INTEGER NOT NULL
      CHECK (unresolved_conflict_count BETWEEN 0 AND {POSTGRESQL_INTEGER_MAX}),
    PRIMARY KEY(namespace_id, lot_uuid),
    FOREIGN KEY(namespace_id, generation)
      REFERENCES lot_cache_generations(namespace_id, generation) ON DELETE CASCADE,
    CHECK (available_grams = on_hand_grams - reserved_grams)
)''',
    f'''CREATE TABLE roast_inventory (
    namespace_id INTEGER NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    roast_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='roast_uuid')}),
    lot_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='lot_uuid')}),
    lot_name TEXT NOT NULL CHECK (length(lot_name) BETWEEN 1 AND {_MAX_LOT_NAME_CHARS}
                                  AND length(CAST(lot_name AS BLOB)) <= {_MAX_LOT_NAME_BYTES}),
    client_reservation_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='client_reservation_uuid')}),
    server_reservation_uuid TEXT CHECK (server_reservation_uuid IS NULL OR ({_UUID_CHECK.format(column='server_reservation_uuid')})),
    planned_grams INTEGER NOT NULL CHECK (planned_grams BETWEEN 1 AND {POSTGRESQL_INTEGER_MAX}),
    actual_grams INTEGER CHECK (actual_grams IS NULL OR actual_grams BETWEEN 1 AND {POSTGRESQL_INTEGER_MAX}),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN
      ('reserve_queued','reserved','finalize_queued','finalized',
       'release_queued','released','paused','failed')),
    terminal_intent TEXT CHECK (terminal_intent IS NULL OR terminal_intent IN ('finalize','release')),
    reserve_occurred_at TEXT NOT NULL CHECK (length(reserve_occurred_at) > 0),
    finalize_occurred_at TEXT,
    release_occurred_at TEXT,
    server_state TEXT CHECK (server_state IS NULL OR server_state IN ('reserved','finalized','released')),
    balance_on_hand_grams INTEGER,
    balance_reserved_grams INTEGER,
    balance_available_grams INTEGER,
    balance_unresolved_conflict_count INTEGER,
    conflict_uuid TEXT CHECK (conflict_uuid IS NULL OR ({_UUID_CHECK.format(column='conflict_uuid')})),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND {_MAX_ERROR_CODE_CHARS}),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) BETWEEN 1 AND {MAX_ERROR_MESSAGE_CODE_POINTS}),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    PRIMARY KEY(namespace_id, roast_uuid),
    UNIQUE(namespace_id, client_reservation_uuid),
    CHECK ((terminal_intent IS NULL AND finalize_occurred_at IS NULL AND release_occurred_at IS NULL)
        OR (terminal_intent = 'finalize' AND finalize_occurred_at IS NOT NULL AND release_occurred_at IS NULL)
        OR (terminal_intent = 'release' AND finalize_occurred_at IS NULL AND release_occurred_at IS NOT NULL)),
    CHECK ((server_state IS NULL AND server_reservation_uuid IS NULL)
        OR (server_state IS NOT NULL AND server_reservation_uuid IS NOT NULL)),
    CHECK (server_state != 'finalized'
        OR (terminal_intent = 'finalize' AND actual_grams IS NOT NULL)),
    CHECK (server_state != 'released'
        OR (terminal_intent = 'release' AND actual_grams IS NULL)),
    CHECK (actual_grams IS NULL OR terminal_intent = 'finalize'),
    CHECK ((balance_on_hand_grams IS NULL AND balance_reserved_grams IS NULL
            AND balance_available_grams IS NULL AND balance_unresolved_conflict_count IS NULL)
        OR (balance_on_hand_grams IS NOT NULL AND balance_reserved_grams IS NOT NULL
            AND balance_available_grams IS NOT NULL
            AND balance_unresolved_conflict_count IS NOT NULL
            AND balance_on_hand_grams BETWEEN -{POSTGRESQL_INTEGER_MAX} AND {POSTGRESQL_INTEGER_MAX}
            AND balance_reserved_grams BETWEEN 0 AND {POSTGRESQL_INTEGER_MAX}
            AND balance_available_grams BETWEEN -{POSTGRESQL_INTEGER_MAX} AND {POSTGRESQL_INTEGER_MAX}
            AND balance_unresolved_conflict_count BETWEEN 0 AND {POSTGRESQL_INTEGER_MAX}
            AND balance_available_grams = balance_on_hand_grams - balance_reserved_grams)),
    CHECK (conflict_uuid IS NULL OR (balance_on_hand_grams IS NOT NULL AND server_state IS NOT NULL)),
    CHECK ((lifecycle NOT IN ('paused','failed') AND error_code IS NULL AND error_message IS NULL)
        OR (lifecycle = 'paused' AND error_code IS NOT NULL AND error_message IS NULL)
        OR (lifecycle = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL))
)''',
    f'''CREATE TABLE inventory_commands (
    id TEXT PRIMARY KEY CHECK ({_UUID_CHECK.format(column='id')}),
    namespace_id INTEGER NOT NULL,
    roast_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='roast_uuid')}),
    lot_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='lot_uuid')}),
    reservation_uuid TEXT NOT NULL CHECK ({_UUID_CHECK.format(column='reservation_uuid')}),
    operation TEXT NOT NULL CHECK (operation IN ('reserve','finalize','release')),
    request_json BLOB NOT NULL CHECK (typeof(request_json) = 'blob'
                                      AND length(request_json) BETWEEN 1 AND {MAX_JSON_BYTES}),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND {_MAX_IDEMPOTENCY_KEY_CHARS}),
    dependency_id TEXT REFERENCES inventory_commands(id)
      CHECK (dependency_id IS NULL OR ({_UUID_CHECK.format(column='dependency_id')})),
    state TEXT NOT NULL CHECK (state IN ('pending','leased','retry_wait','paused','failed','complete')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    lease_token TEXT UNIQUE CHECK (lease_token IS NULL OR ({_UUID_CHECK.format(column='lease_token')})),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND {_MAX_ERROR_CODE_CHARS}),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) BETWEEN 1 AND {MAX_ERROR_MESSAGE_CODE_POINTS}),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    completed_at TEXT,
    UNIQUE(namespace_id, reservation_uuid, operation),
    FOREIGN KEY(namespace_id, roast_uuid)
      REFERENCES roast_inventory(namespace_id, roast_uuid) ON DELETE CASCADE,
    CHECK ((operation = 'reserve' AND dependency_id IS NULL)
        OR (operation IN ('finalize','release') AND dependency_id IS NOT NULL)),
    CHECK (dependency_id IS NULL OR dependency_id != id),
    CHECK ((state = 'leased' AND lease_expires_at IS NOT NULL AND lease_token IS NOT NULL)
        OR (state != 'leased' AND lease_expires_at IS NULL AND lease_token IS NULL)),
    CHECK ((state = 'complete' AND completed_at IS NOT NULL)
        OR (state != 'complete' AND completed_at IS NULL)),
    CHECK ((state = 'pending' AND next_attempt_at IS NULL
                              AND error_code IS NULL AND error_message IS NULL)
        OR (state = 'leased' AND next_attempt_at IS NULL
                             AND error_code IS NULL AND error_message IS NULL)
        OR (state = 'retry_wait' AND next_attempt_at IS NOT NULL
                                 AND error_code IS NOT NULL AND error_message IS NOT NULL)
        OR (state = 'paused' AND error_code IS NOT NULL AND error_message IS NULL)
        OR (state = 'failed' AND next_attempt_at IS NULL
                             AND error_code IS NOT NULL AND error_message IS NOT NULL)
        OR (state = 'complete' AND next_attempt_at IS NULL
                               AND error_code IS NULL AND error_message IS NULL))
)''',
    '''CREATE INDEX inventory_commands_ready_idx
  ON inventory_commands(namespace_id, state, next_attempt_at, created_at)''',
    '''CREATE INDEX inventory_commands_dependency_idx
  ON inventory_commands(dependency_id)''',
    '''CREATE INDEX roast_inventory_interrupted_idx
  ON roast_inventory(lifecycle, terminal_intent, updated_at)''',
    '''CREATE INDEX bean_lots_name_idx
  ON bean_lots(namespace_id, name, lot_uuid)''',
)
_CANONICAL_SCHEMA_V1_STATEMENTS: Final[tuple[str, ...]] = _SCHEMA_V1_STATEMENTS


type _SchemaPragmaRow = tuple[object, ...]
type _SchemaObject = tuple[object, object, object]
type _SchemaIndexFingerprint = tuple[_SchemaPragmaRow, tuple[_SchemaPragmaRow, ...]]
type _SchemaTableFingerprint = tuple[
    str,
    tuple[_SchemaPragmaRow, ...],
    tuple[_SchemaPragmaRow, ...],
    tuple[_SchemaIndexFingerprint, ...],
]
type _SchemaFingerprint = tuple[
    tuple[_SchemaObject, ...], tuple[_SchemaTableFingerprint, ...]
]


class InventoryStoreError(RuntimeError):
    pass


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


class InventoryStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._database_path = self.root / _DATABASE_NAME
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            connection: sqlite3.Connection | None = None
            try:
                secure_filesystem.prepare_private_root(self.root)
                with self._filesystem_lock():
                    self._secure_database_files_before_connect()
                    connection = sqlite3.connect(
                        self._database_path,
                        timeout=_BUSY_TIMEOUT_MS / 1_000,
                        isolation_level=None,
                        check_same_thread=False,
                    )
                    connection.row_factory = sqlite3.Row
                    connection.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
                    journal_mode = cast(
                        str, connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
                    )
                    if journal_mode.casefold() != 'wal':
                        raise InventoryStoreError('SQLite WAL mode is unavailable')
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute('PRAGMA foreign_keys=ON')
                    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                        raise InventoryStoreError('SQLite foreign keys are unavailable')
                    self._connection = connection
                    self._initialize_schema()
                    self._validate_durable_rows()
                    self._harden_database_files()
            except InventoryStoreError:
                self._connection = None
                if connection is not None:
                    connection.close()
                raise
            except (OSError, sqlite3.Error, secure_filesystem.FilesystemError):
                self._connection = None
                if connection is not None:
                    connection.close()
                raise InventoryStoreError(_STORAGE_ERROR) from None

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            if connection is None:
                return
            try:
                with self._filesystem_lock():
                    self._connection = None
                    connection.close()
                    self._harden_database_files()
            except (OSError, sqlite3.Error, secure_filesystem.FilesystemError):
                raise InventoryStoreError(_STORAGE_ERROR) from None

    def database_pragmas(self) -> tuple[str, bool, int, int]:
        with self._storage_boundary(), self._lock:
            connection = self._require_connection()
            journal = cast(str, connection.execute('PRAGMA journal_mode').fetchone()[0])
            foreign_keys = bool(connection.execute('PRAGMA foreign_keys').fetchone()[0])
            busy_timeout = cast(int, connection.execute('PRAGMA busy_timeout').fetchone()[0])
            synchronous = cast(int, connection.execute('PRAGMA synchronous').fetchone()[0])
            return journal.casefold(), foreign_keys, busy_timeout, synchronous

    def replace_lots(
        self,
        namespace: Namespace,
        lots: tuple[BeanLot, ...],
        refreshed_at: datetime,
    ) -> None:
        namespace_values = _namespace_values(namespace)
        validated_lots = _validated_lots(lots)
        refreshed_text = _datetime_text(refreshed_at)
        generation = uuid4().hex
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            namespace_id = self._namespace_id(
                connection, namespace_values, create=True
            )
            if namespace_id is None:
                raise InventoryStoreError('inventory namespace was not persisted')
            connection.execute(
                'DELETE FROM bean_lots WHERE namespace_id = ?', (namespace_id,)
            )
            connection.execute(
                '''INSERT INTO lot_cache_generations
                   (namespace_id, generation, refreshed_at) VALUES (?, ?, ?)
                   ON CONFLICT(namespace_id) DO UPDATE SET
                     generation = excluded.generation,
                     refreshed_at = excluded.refreshed_at''',
                (namespace_id, generation, refreshed_text),
            )
            connection.executemany(
                '''INSERT INTO bean_lots
                   (namespace_id, generation, lot_uuid, name, origin,
                    varietals_json, processing_method, crop_year, on_hand_grams,
                    reserved_grams, available_grams, unresolved_conflict_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    (
                        namespace_id,
                        generation,
                        item.lot_id.hex,
                        item.name,
                        item.origin,
                        _varietals_json(item.varietals),
                        item.processing_method,
                        item.crop_year,
                        item.on_hand_grams,
                        item.reserved_grams,
                        item.available_grams,
                        item.unresolved_conflict_count,
                    )
                    for item in validated_lots
                ),
            )

    def cached_lots(self, namespace: Namespace) -> tuple[BeanLot, ...]:
        return self.cache_snapshot(namespace).lots

    def cache_snapshot(self, namespace: Namespace) -> LotCacheSnapshot:
        namespace_values = _namespace_values(namespace)
        with self._storage_boundary(), self._read_transaction() as connection:
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return LotCacheSnapshot(namespace, (), None)
            generation_row = connection.execute(
                '''SELECT generation, refreshed_at FROM lot_cache_generations
                   WHERE namespace_id = ?''',
                (namespace_id,),
            ).fetchone()
            if generation_row is None:
                return LotCacheSnapshot(namespace, (), None)
            generation = _stored_uuid_hex(generation_row['generation'])
            refreshed_at = _stored_datetime(generation_row['refreshed_at'])
            rows = connection.execute(
                '''SELECT * FROM bean_lots
                   WHERE namespace_id = ? AND generation = ?''',
                (namespace_id, generation),
            ).fetchall()
            lots = tuple(_row_to_lot(row, generation) for row in rows)
            if len(lots) > MAX_CACHED_LOTS or len({item.lot_id for item in lots}) != len(lots):
                raise InventoryStoreError('stored lot cache is invalid')
            return LotCacheSnapshot(
                namespace,
                tuple(sorted(lots, key=lambda item: (item.name.casefold(), item.lot_id.hex))),
                refreshed_at,
            )

    def enqueue_reserve(
        self,
        namespace: Namespace,
        request: InventoryCommandRequest,
        lot_name: str,
        now: datetime,
    ) -> InventoryRoastState:
        namespace_values = _namespace_values(namespace)
        request_value = _validated_command_request(request, 'reserve')
        lot_name_value = _bounded_text(
            lot_name, _MAX_LOT_NAME_CHARS, _MAX_LOT_NAME_BYTES, 'lot name'
        )
        now_text = _datetime_text(now)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            namespace_id = self._namespace_id(connection, namespace_values, create=True)
            if namespace_id is None:
                raise InventoryStoreError('inventory namespace was not persisted')
            existing = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (namespace_id, request_value.roast_uuid.hex),
            ).fetchone()
            if existing is not None:
                self._require_reserve_match(
                    connection,
                    namespace_id,
                    existing,
                    request_value,
                    lot_name_value,
                )
                return _row_to_roast(existing, namespace)
            reservation_owner = connection.execute(
                '''SELECT roast_uuid FROM roast_inventory
                   WHERE namespace_id = ? AND client_reservation_uuid = ?''',
                (namespace_id, request_value.reservation_uuid.hex),
            ).fetchone()
            if reservation_owner is not None:
                raise InventoryStoreError('inventory reservation UUID conflicts')
            connection.execute(
                '''INSERT INTO roast_inventory
                   (namespace_id, roast_uuid, lot_uuid, lot_name,
                    client_reservation_uuid, planned_grams, lifecycle,
                    reserve_occurred_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'reserve_queued', ?, ?, ?)''',
                (
                    namespace_id,
                    request_value.roast_uuid.hex,
                    request_value.lot_id.hex,
                    lot_name_value,
                    request_value.reservation_uuid.hex,
                    request_value.planned_grams,
                    _datetime_text(request_value.occurred_at),
                    now_text,
                    now_text,
                ),
            )
            self._insert_command(
                connection, namespace_id, request_value, None, now_text
            )
            self._prune_completed(connection, now)
            row = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (namespace_id, request_value.roast_uuid.hex),
            ).fetchone()
            if row is None:
                raise InventoryStoreError('inventory roast was not persisted')
            return _row_to_roast(row, namespace)

    def enqueue_finalize(
        self,
        namespace: Namespace,
        request: InventoryCommandRequest,
        actual_grams: int | None,
        now: datetime,
    ) -> InventoryRoastState:
        if actual_grams is not None and not _bounded_int(
            actual_grams, 1, POSTGRESQL_INTEGER_MAX
        ):
            raise ValueError('inventory actual grams are invalid')
        request_value = _validated_command_request(request, 'finalize')
        if request_value.requested_actual_grams != actual_grams:
            raise ValueError('inventory actual grams do not match request')
        return self._enqueue_terminal(namespace, request_value, actual_grams, now)

    def enqueue_release(
        self,
        namespace: Namespace,
        request: InventoryCommandRequest,
        now: datetime,
    ) -> InventoryRoastState:
        request_value = _validated_command_request(request, 'release')
        return self._enqueue_terminal(namespace, request_value, None, now)

    def _enqueue_terminal(
        self,
        namespace: Namespace,
        request: InventoryCommandRequest,
        actual_grams: int | None,
        now: datetime,
    ) -> InventoryRoastState:
        namespace_values = _namespace_values(namespace)
        now_text = _datetime_text(now)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                raise InventoryStoreError('inventory reservation is unavailable')
            row = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (namespace_id, request.roast_uuid.hex),
            ).fetchone()
            if row is None:
                raise InventoryStoreError('inventory reservation is unavailable')
            _require_request_matches_roast(row, request)
            terminal_intent = cast(str | None, row['terminal_intent'])
            if terminal_intent is not None and terminal_intent != request.operation:
                raise InventoryStoreError('inventory terminal intent conflicts')
            reserve = connection.execute(
                '''SELECT * FROM inventory_commands
                   WHERE namespace_id = ? AND reservation_uuid = ?
                     AND operation = 'reserve' ''',
                (namespace_id, request.reservation_uuid.hex),
            ).fetchone()
            if reserve is None:
                raise InventoryStoreError('inventory reserve dependency is unavailable')
            existing = connection.execute(
                '''SELECT * FROM inventory_commands
                   WHERE namespace_id = ? AND reservation_uuid = ? AND operation = ?''',
                (namespace_id, request.reservation_uuid.hex, request.operation),
            ).fetchone()
            occurred_column = (
                'finalize_occurred_at'
                if request.operation == 'finalize'
                else 'release_occurred_at'
            )
            if existing is not None:
                if not _command_body_matches(existing, request):
                    raise InventoryStoreError('inventory immutable command conflicts')
                if row['actual_grams'] != actual_grams:
                    raise InventoryStoreError('inventory immutable command conflicts')
                return _row_to_roast(row, namespace)
            if terminal_intent is not None or row['server_state'] in {'finalized', 'released'}:
                raise InventoryStoreError('inventory terminal intent conflicts')
            lifecycle = f'{request.operation}_queued'
            connection.execute(
                f'''UPDATE roast_inventory
                    SET actual_grams = ?, lifecycle = ?, terminal_intent = ?,
                        {occurred_column} = ?, error_code = NULL,
                        error_message = NULL, updated_at = ?
                    WHERE namespace_id = ? AND roast_uuid = ?''',
                (
                    actual_grams,
                    lifecycle,
                    request.operation,
                    _datetime_text(request.occurred_at),
                    now_text,
                    namespace_id,
                    request.roast_uuid.hex,
                ),
            )
            self._insert_command(
                connection,
                namespace_id,
                request,
                _stored_uuid_hex(reserve['id']),
                now_text,
            )
            self._prune_completed(connection, now)
            updated = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (namespace_id, request.roast_uuid.hex),
            ).fetchone()
            if updated is None:
                raise InventoryStoreError('inventory roast was not persisted')
            return _row_to_roast(updated, namespace)

    def lease_next(
        self, namespace: Namespace, now: datetime, lease_seconds: int
    ) -> InventoryCommand | None:
        namespace_values = _namespace_values(namespace)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError(
                f'lease_seconds must be an integer between 1 and {_MAX_LEASE_SECONDS}'
            )
        now_text = _datetime_text(now)
        try:
            lease_expires_text = _datetime_text(
                now + timedelta(seconds=lease_seconds)
            )
        except OverflowError as exc:
            raise ValueError('lease_seconds is out of range') from exc
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return None
            row = connection.execute(
                '''SELECT command.* FROM inventory_commands AS command
                   LEFT JOIN inventory_commands AS dependency
                     ON dependency.id = command.dependency_id
                   WHERE command.namespace_id = ?
                     AND (command.state = 'pending'
                          OR (command.state = 'retry_wait'
                              AND command.next_attempt_at <= ?))
                     AND (command.dependency_id IS NULL
                          OR dependency.state = 'complete')
                   ORDER BY CASE WHEN command.state = 'retry_wait'
                                 THEN command.next_attempt_at
                                 ELSE command.created_at END,
                            command.created_at, command.rowid
                   LIMIT 1''',
                (namespace_id, now_text),
            ).fetchone()
            if row is None:
                return None
            lease_token = uuid4().hex
            cursor = connection.execute(
                '''UPDATE inventory_commands
                   SET state = 'leased', attempts = attempts + 1,
                       next_attempt_at = NULL, lease_expires_at = ?, lease_token = ?,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE id = ? AND state IN ('pending', 'retry_wait')''',
                (lease_expires_text, lease_token, now_text, row['id']),
            )
            if cursor.rowcount != 1:
                raise InventoryStoreError('lease_lost')
            leased = connection.execute(
                'SELECT * FROM inventory_commands WHERE id = ?', (row['id'],)
            ).fetchone()
            if leased is None:
                raise InventoryStoreError('leased inventory command disappeared')
            return _row_to_command(leased, namespace)

    def next_due_at(self, namespace: Namespace) -> datetime | None:
        namespace_values = _namespace_values(namespace)
        with self._storage_boundary(), self._read_transaction() as connection:
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return None
            row = connection.execute(
                '''SELECT CASE WHEN command.state = 'retry_wait'
                               THEN command.next_attempt_at
                               ELSE command.created_at END AS due_at
                   FROM inventory_commands AS command
                   LEFT JOIN inventory_commands AS dependency
                     ON dependency.id = command.dependency_id
                   WHERE command.namespace_id = ?
                     AND command.state IN ('pending', 'retry_wait')
                     AND (command.dependency_id IS NULL
                          OR dependency.state = 'complete')
                   ORDER BY due_at, command.created_at, command.rowid LIMIT 1''',
                (namespace_id,),
            ).fetchone()
            return None if row is None else _stored_datetime(row['due_at'])

    def mark_complete(
        self,
        command_id: str,
        lease_token: str,
        result: InventoryMutationResult,
        now: datetime,
    ) -> InventoryRoastState:
        command_hex = _command_identifier(command_id)
        token_hex = _command_identifier(lease_token)
        now_text = _datetime_text(now)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            row = self._leased_command(
                connection, command_hex, token_hex, now_text
            )
            namespace = _namespace_from_command(connection, row)
            roast = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (row['namespace_id'], row['roast_uuid']),
            ).fetchone()
            if roast is None:
                raise InventoryStoreError('inventory roast is unavailable')
            validated_result = _validated_mutation_result(result, row, roast)
            operation = cast(InventoryOperation, row['operation'])
            if operation != 'reserve' and roast['server_reservation_uuid'] != (
                validated_result.reservation.reservation_id.hex
            ):
                raise InventoryStoreError('inventory server reservation conflicts')
            cursor = connection.execute(
                '''UPDATE inventory_commands
                   SET state = 'complete', next_attempt_at = NULL,
                       lease_expires_at = NULL, lease_token = NULL,
                       error_code = NULL, error_message = NULL,
                       updated_at = ?, completed_at = ?
                   WHERE id = ? AND state = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?''',
                (now_text, now_text, command_hex, token_hex, now_text),
            )
            if cursor.rowcount != 1:
                raise InventoryStoreError('lease_lost')
            reservation = validated_result.reservation
            balance = validated_result.balance
            persisted_actual_grams = (
                reservation.actual_grams
                if operation == 'finalize'
                else cast(int | None, roast['actual_grams'])
            )
            lifecycle: InventoryLifecycle
            if operation == 'reserve':
                terminal_intent = cast(str | None, roast['terminal_intent'])
                lifecycle = cast(
                    InventoryLifecycle,
                    'reserved'
                    if terminal_intent is None
                    else f'{terminal_intent}_queued',
                )
            else:
                lifecycle = cast(InventoryLifecycle, f'{operation}d')
            connection.execute(
                '''UPDATE roast_inventory
                   SET server_reservation_uuid = ?, server_state = ?, lifecycle = ?,
                       actual_grams = ?,
                       balance_on_hand_grams = ?, balance_reserved_grams = ?,
                       balance_available_grams = ?,
                       balance_unresolved_conflict_count = ?, conflict_uuid = ?,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (
                    reservation.reservation_id.hex,
                    reservation.state,
                    lifecycle,
                    persisted_actual_grams,
                    balance.on_hand_grams,
                    balance.reserved_grams,
                    balance.available_grams,
                    balance.unresolved_conflict_count,
                    (
                        validated_result.conflict.conflict_id.hex
                        if validated_result.conflict is not None
                        else None
                    ),
                    now_text,
                    row['namespace_id'],
                    row['roast_uuid'],
                ),
            )
            connection.execute(
                '''UPDATE bean_lots
                   SET on_hand_grams = ?, reserved_grams = ?, available_grams = ?,
                       unresolved_conflict_count = ?
                   WHERE namespace_id = ? AND lot_uuid = ?''',
                (
                    balance.on_hand_grams,
                    balance.reserved_grams,
                    balance.available_grams,
                    balance.unresolved_conflict_count,
                    row['namespace_id'],
                    row['lot_uuid'],
                ),
            )
            self._prune_completed(connection, now)
            updated = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (row['namespace_id'], row['roast_uuid']),
            ).fetchone()
            if updated is None:
                raise InventoryStoreError('inventory roast is unavailable')
            return _row_to_roast(updated, namespace)

    def mark_retry(
        self,
        command_id: str,
        lease_token: str,
        now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> None:
        _datetime_text(next_attempt_at)
        _datetime_text(now)
        if next_attempt_at.astimezone(UTC) < now.astimezone(UTC):
            raise ValueError('next attempt cannot be before now')
        self._transition_leased_failure(
            command_id,
            lease_token,
            now,
            failure,
            'retry_wait',
            next_attempt_at,
        )

    def mark_paused(
        self,
        command_id: str,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
    ) -> None:
        self._transition_leased_failure(
            command_id, lease_token, now, failure, 'paused', None
        )

    def mark_failed(
        self,
        command_id: str,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
    ) -> None:
        self._transition_leased_failure(
            command_id, lease_token, now, failure, 'failed', None
        )

    def _transition_leased_failure(
        self,
        command_id: str,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
        state: Literal['retry_wait', 'paused', 'failed'],
        next_attempt_at: datetime | None,
    ) -> None:
        command_hex = _command_identifier(command_id)
        token_hex = _command_identifier(lease_token)
        now_text = _datetime_text(now)
        next_text = (
            None if next_attempt_at is None else _datetime_text(next_attempt_at)
        )
        error_code, error_message = _failure_fields(failure)
        stored_message = None if state == 'paused' else error_message
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            row = self._leased_command(
                connection, command_hex, token_hex, now_text
            )
            cursor = connection.execute(
                '''UPDATE inventory_commands
                   SET state = ?, next_attempt_at = ?, lease_expires_at = NULL,
                       lease_token = NULL, error_code = ?, error_message = ?,
                       updated_at = ?
                   WHERE id = ? AND state = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?''',
                (
                    state,
                    next_text,
                    error_code,
                    stored_message,
                    now_text,
                    command_hex,
                    token_hex,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise InventoryStoreError('lease_lost')
            if state in {'paused', 'failed'}:
                connection.execute(
                    '''UPDATE roast_inventory
                       SET lifecycle = ?, error_code = ?, error_message = ?, updated_at = ?
                       WHERE namespace_id = ? AND roast_uuid = ?''',
                    (
                        state,
                        error_code,
                        stored_message,
                        now_text,
                        row['namespace_id'],
                        row['roast_uuid'],
                    ),
                )
            if state == 'failed' and row['operation'] == 'reserve':
                connection.execute(
                    '''UPDATE inventory_commands
                       SET state = 'failed', next_attempt_at = NULL,
                           lease_expires_at = NULL, lease_token = NULL,
                           error_code = ?, error_message = ?, updated_at = ?
                       WHERE dependency_id = ?
                         AND state IN ('pending', 'retry_wait', 'paused')''',
                    (
                        _DEPENDENCY_FAILURE_CODE,
                        _DEPENDENCY_FAILURE_MESSAGE,
                        now_text,
                        command_hex,
                    ),
                )
            self._prune_completed(connection, now)

    def recover_expired_leases(self, now: datetime) -> int:
        now_text = _datetime_text(now)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            cursor = connection.execute(
                '''UPDATE inventory_commands
                   SET state = 'pending', next_attempt_at = NULL,
                       lease_expires_at = NULL, lease_token = NULL,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE state = 'leased' AND lease_expires_at <= ?''',
                (now_text, now_text),
            )
            self._prune_completed(connection, now)
            return cursor.rowcount

    def pause_namespace(self, namespace: Namespace, now: datetime, code: str) -> int:
        namespace_values = _namespace_values(namespace)
        now_text = _datetime_text(now)
        code_value = _pause_code(code)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return 0
            cursor = connection.execute(
                '''UPDATE inventory_commands
                   SET state = 'paused', lease_expires_at = NULL, lease_token = NULL,
                       error_code = ?, error_message = NULL, updated_at = ?
                   WHERE namespace_id = ? AND state IN ('pending', 'retry_wait')''',
                (code_value, now_text, namespace_id),
            )
            connection.execute(
                '''UPDATE roast_inventory
                   SET lifecycle = 'paused', error_code = ?, error_message = NULL,
                       updated_at = ?
                   WHERE namespace_id = ?
                     AND lifecycle NOT IN ('finalized', 'released', 'failed')
                     AND EXISTS (
                       SELECT 1 FROM inventory_commands
                       WHERE inventory_commands.namespace_id = roast_inventory.namespace_id
                         AND inventory_commands.roast_uuid = roast_inventory.roast_uuid
                         AND inventory_commands.state = 'paused')''',
                (code_value, now_text, namespace_id),
            )
            self._prune_completed(connection, now)
            return cursor.rowcount

    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
        namespace_values = _namespace_values(namespace)
        now_text = _datetime_text(now)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return 0
            roast_ids = tuple(
                cast(str, row['roast_uuid'])
                for row in connection.execute(
                    '''SELECT DISTINCT roast_uuid FROM inventory_commands
                       WHERE namespace_id = ? AND state = 'paused' ''',
                    (namespace_id,),
                )
            )
            cursor = connection.execute(
                '''UPDATE inventory_commands
                   SET state = 'pending', next_attempt_at = NULL,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE namespace_id = ? AND state = 'paused' ''',
                (now_text, namespace_id),
            )
            for roast_uuid in roast_ids:
                self._restore_roast_lifecycle(
                    connection, namespace_id, roast_uuid, now_text
                )
            self._prune_completed(connection, now)
            return cursor.rowcount

    def retry_same(self, command_id: str, now: datetime) -> None:
        command_hex = _command_identifier(command_id)
        now_text = _datetime_text(now)
        with (
            self._storage_boundary(),
            self._filesystem_lock(),
            self._transaction() as connection,
        ):
            row = connection.execute(
                'SELECT * FROM inventory_commands WHERE id = ?', (command_hex,)
            ).fetchone()
            if row is None or row['state'] != 'failed':
                raise InventoryStoreError('inventory command is not failed')
            connection.execute(
                '''UPDATE inventory_commands
                   SET state = 'pending', next_attempt_at = NULL,
                       lease_expires_at = NULL, lease_token = NULL,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE id = ? AND state = 'failed' ''',
                (now_text, command_hex),
            )
            self._restore_roast_lifecycle(
                connection,
                cast(int, row['namespace_id']),
                _stored_uuid_hex(row['roast_uuid']),
                now_text,
            )
            self._prune_completed(connection, now)

    def counts(self, namespace: Namespace) -> InventoryQueueCounts:
        namespace_values = _namespace_values(namespace)
        with self._storage_boundary(), self._read_transaction() as connection:
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return InventoryQueueCounts(0, 0, 0, 0, 0)
            row = connection.execute(
                '''SELECT
                     COALESCE(SUM(state IN ('pending', 'leased')), 0) AS pending,
                     COALESCE(SUM(state = 'retry_wait'), 0) AS retrying,
                     COALESCE(SUM(state = 'paused'), 0) AS paused,
                     COALESCE(SUM(state = 'failed'), 0) AS failed,
                     COALESCE(SUM(state = 'complete'), 0) AS complete
                   FROM inventory_commands WHERE namespace_id = ?''',
                (namespace_id,),
            ).fetchone()
            if row is None:
                raise InventoryStoreError('inventory command count query failed')
            return InventoryQueueCounts(
                cast(int, row['pending']),
                cast(int, row['retrying']),
                cast(int, row['paused']),
                cast(int, row['failed']),
                cast(int, row['complete']),
            )

    def failed_commands(
        self, namespace: Namespace
    ) -> tuple[FailedInventoryCommand, ...]:
        namespace_values = _namespace_values(namespace)
        with self._storage_boundary(), self._read_transaction() as connection:
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return ()
            rows = connection.execute(
                '''SELECT * FROM inventory_commands
                   WHERE namespace_id = ? AND state = 'failed'
                   ORDER BY updated_at DESC,
                            CASE operation WHEN 'reserve' THEN 0 ELSE 1 END,
                            id''',
                (namespace_id,),
            ).fetchall()
            result: list[FailedInventoryCommand] = []
            for row in rows:
                command = _row_to_command(row, namespace)
                if command.error_code is None or command.error_message is None:
                    raise InventoryStoreError('failed inventory command is invalid')
                result.append(
                    FailedInventoryCommand(
                        command.id,
                        namespace,
                        command.roast_uuid,
                        command.lot_id,
                        command.reservation_uuid,
                        command.operation,
                        command.attempts,
                        command.error_code,
                        command.error_message,
                        command.updated_at,
                    )
                )
            return tuple(result)

    def interrupted_reservations(self) -> tuple[InterruptedReservation, ...]:
        with self._storage_boundary(), self._read_transaction() as connection:
            rows = connection.execute(
                '''SELECT roast.*, namespace.origin, namespace.organization_uuid,
                          namespace.namespace_key
                   FROM roast_inventory AS roast
                   JOIN namespaces AS namespace ON namespace.id = roast.namespace_id
                   WHERE roast.terminal_intent IS NULL
                     AND roast.lifecycle NOT IN ('finalized', 'released')
                   ORDER BY roast.updated_at, namespace.namespace_key, roast.roast_uuid'''
            ).fetchall()
            return tuple(_row_to_interrupted(row) for row in rows)

    def roast_state(
        self, namespace: Namespace, roast_uuid: UUID
    ) -> InventoryRoastState | None:
        namespace_values = _namespace_values(namespace)
        if not isinstance(roast_uuid, UUID):
            raise ValueError('inventory roast UUID is invalid')
        with self._storage_boundary(), self._read_transaction() as connection:
            namespace_id = self._namespace_id(
                connection, namespace_values, create=False
            )
            if namespace_id is None:
                return None
            row = connection.execute(
                '''SELECT * FROM roast_inventory
                   WHERE namespace_id = ? AND roast_uuid = ?''',
                (namespace_id, roast_uuid.hex),
            ).fetchone()
            return None if row is None else _row_to_roast(row, namespace)

    def _require_reserve_match(
        self,
        connection: sqlite3.Connection,
        namespace_id: int,
        roast: sqlite3.Row,
        request: InventoryCommandRequest,
        lot_name: str,
    ) -> None:
        if (
            roast['lot_uuid'] != request.lot_id.hex
            or roast['lot_name'] != lot_name
            or roast['client_reservation_uuid'] != request.reservation_uuid.hex
            or roast['planned_grams'] != request.planned_grams
            or _stored_datetime(roast['reserve_occurred_at']) != request.occurred_at.astimezone(UTC)
        ):
            raise InventoryStoreError('inventory immutable reservation conflicts')
        command = connection.execute(
            '''SELECT * FROM inventory_commands
               WHERE namespace_id = ? AND reservation_uuid = ?
                 AND operation = 'reserve' ''',
            (namespace_id, request.reservation_uuid.hex),
        ).fetchone()
        if command is None or not _command_body_matches(command, request):
            raise InventoryStoreError('inventory immutable command conflicts')

    @staticmethod
    def _insert_command(
        connection: sqlite3.Connection,
        namespace_id: int,
        request: InventoryCommandRequest,
        dependency_id: str | None,
        now_text: str,
    ) -> None:
        connection.execute(
            '''INSERT INTO inventory_commands
               (id, namespace_id, roast_uuid, lot_uuid, reservation_uuid,
                operation, request_json, idempotency_key, dependency_id, state,
                attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)''',
            (
                uuid4().hex,
                namespace_id,
                request.roast_uuid.hex,
                request.lot_id.hex,
                request.reservation_uuid.hex,
                request.operation,
                request.request_json,
                request.idempotency_key,
                dependency_id,
                now_text,
                now_text,
            ),
        )

    @staticmethod
    def _leased_command(
        connection: sqlite3.Connection,
        command_id: str,
        lease_token: str,
        now_text: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            '''SELECT * FROM inventory_commands
               WHERE id = ? AND state = 'leased' AND lease_token = ?
                 AND lease_expires_at > ?''',
            (command_id, lease_token, now_text),
        ).fetchone()
        if row is None:
            raise InventoryStoreError('lease_lost')
        return cast(sqlite3.Row, row)

    @staticmethod
    def _restore_roast_lifecycle(
        connection: sqlite3.Connection,
        namespace_id: int,
        roast_uuid: str,
        now_text: str,
    ) -> None:
        row = connection.execute(
            '''SELECT * FROM roast_inventory
               WHERE namespace_id = ? AND roast_uuid = ?''',
            (namespace_id, roast_uuid),
        ).fetchone()
        if row is None:
            raise InventoryStoreError('inventory roast is unavailable')
        server_state = cast(str | None, row['server_state'])
        terminal_intent = cast(str | None, row['terminal_intent'])
        if server_state in {'finalized', 'released'}:
            lifecycle = server_state
        elif server_state is None:
            lifecycle = 'reserve_queued'
        elif terminal_intent is None:
            lifecycle = 'reserved'
        else:
            lifecycle = f'{terminal_intent}_queued'
        connection.execute(
            '''UPDATE roast_inventory
               SET lifecycle = ?, error_code = NULL, error_message = NULL,
                   updated_at = ?
               WHERE namespace_id = ? AND roast_uuid = ?''',
            (lifecycle, now_text, namespace_id, roast_uuid),
        )

    @staticmethod
    def _prune_completed(connection: sqlite3.Connection, now: datetime) -> None:
        threshold = _datetime_text(now - timedelta(days=_COMPLETED_RETENTION_DAYS))
        eligible = '''completed_at <= ? AND EXISTS (
              SELECT 1 FROM roast_inventory AS roast
              WHERE roast.namespace_id = inventory_commands.namespace_id
                AND roast.roast_uuid = inventory_commands.roast_uuid
                AND roast.lifecycle IN ('finalized', 'released'))'''
        connection.execute(
            f'''DELETE FROM inventory_commands
                WHERE operation IN ('finalize', 'release') AND {eligible}''',
            (threshold,),
        )
        connection.execute(
            f'''DELETE FROM inventory_commands
                WHERE operation = 'reserve' AND {eligible}
                  AND NOT EXISTS (
                    SELECT 1 FROM inventory_commands AS dependent
                    WHERE dependent.dependency_id = inventory_commands.id)''',
            (threshold,),
        )

    def _initialize_schema(self) -> None:
        connection = self._require_connection()
        connection.execute('BEGIN IMMEDIATE')
        try:
            tables = {
                cast(str, row['name'])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if 'schema_version' not in tables:
                if tables:
                    raise InventoryStoreError('inventory schema is unversioned')
                if _SCHEMA_V1_STATEMENTS != _CANONICAL_SCHEMA_V1_STATEMENTS:
                    raise InventoryStoreError('inventory schema definition is invalid')
                for statement in _SCHEMA_V1_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    'INSERT INTO schema_version(version) VALUES (?)', (_SCHEMA_VERSION,)
                )
            versions = connection.execute(
                'SELECT version FROM schema_version'
            ).fetchall()
            if (
                len(versions) != 1
                or type(versions[0]['version']) is not int
                or versions[0]['version'] != _SCHEMA_VERSION
            ):
                raise InventoryStoreError('unsupported inventory schema version')
            self._validate_schema_fingerprint()
            if connection.execute('PRAGMA foreign_key_check').fetchall():
                raise InventoryStoreError('inventory schema foreign keys are invalid')
            integrity = connection.execute('PRAGMA integrity_check').fetchall()
            if len(integrity) != 1 or integrity[0][0] != 'ok':
                raise InventoryStoreError('inventory database integrity check failed')
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, InventoryStoreError):
                raise
            raise InventoryStoreError('inventory schema initialization failed') from None

    def _validate_schema_fingerprint(self) -> None:
        try:
            canonical = sqlite3.connect(':memory:')
            try:
                for statement in _SCHEMA_V1_STATEMENTS:
                    canonical.execute(statement)
                expected = _schema_fingerprint(canonical)
            finally:
                canonical.close()
            actual = _schema_fingerprint(self._require_connection())
        except sqlite3.Error:
            raise InventoryStoreError('inventory schema definition is invalid') from None
        if actual != expected:
            raise InventoryStoreError('inventory schema fingerprint is invalid')

    def _validate_durable_rows(self) -> None:
        connection = self._require_connection()
        try:
            namespace_rows = connection.execute('SELECT * FROM namespaces').fetchall()
            namespaces: dict[int, tuple[str, str, str]] = {}
            for row in namespace_rows:
                origin = _stored_bounded_text(row['origin'], _MAX_ORIGIN_CHARS, None)
                organization_hex = _stored_uuid_hex(row['organization_uuid'])
                namespace_key = _stored_namespace_key(row['namespace_key'])
                expected = hashlib.sha256(
                    f'{origin}\n{UUID(hex=organization_hex)}'.encode()
                ).hexdigest()
                if not hmac.compare_digest(namespace_key, expected):
                    raise InventoryStoreError('stored inventory namespace is invalid')
                namespaces[cast(int, row['id'])] = (origin, organization_hex, namespace_key)
            generations = connection.execute(
                'SELECT * FROM lot_cache_generations'
            ).fetchall()
            for row in generations:
                if cast(int, row['namespace_id']) not in namespaces:
                    raise InventoryStoreError('stored lot cache is invalid')
                generation = _stored_uuid_hex(row['generation'])
                _stored_datetime(row['refreshed_at'])
                lot_rows = connection.execute(
                    '''SELECT * FROM bean_lots
                       WHERE namespace_id = ? AND generation = ?''',
                    (row['namespace_id'], generation),
                ).fetchall()
                if len(lot_rows) > MAX_CACHED_LOTS:
                    raise InventoryStoreError('stored lot cache is invalid')
                for lot_row in lot_rows:
                    _row_to_lot(lot_row, generation)
            for row in connection.execute('SELECT * FROM roast_inventory'):
                _validate_stored_roast(row, namespaces)
            for row in connection.execute('SELECT * FROM inventory_commands'):
                _validate_stored_command(row, namespaces)
        except InventoryStoreError:
            raise
        except (KeyError, sqlite3.Error, TypeError, UnicodeError, ValueError):
            raise InventoryStoreError('stored inventory rows are invalid') from None

    def _namespace_id(
        self,
        connection: sqlite3.Connection,
        values: tuple[str, str, str],
        *,
        create: bool,
    ) -> int | None:
        origin, organization_hex, namespace_key = values
        row = connection.execute(
            '''SELECT id, origin, organization_uuid, namespace_key
               FROM namespaces WHERE origin = ? AND organization_uuid = ?''',
            (origin, organization_hex),
        ).fetchone()
        if row is not None:
            if row['namespace_key'] != namespace_key:
                raise InventoryStoreError('stored inventory namespace conflicts')
            return cast(int, row['id'])
        key_row = connection.execute(
            'SELECT origin, organization_uuid FROM namespaces WHERE namespace_key = ?',
            (namespace_key,),
        ).fetchone()
        if key_row is not None:
            raise InventoryStoreError('stored inventory namespace conflicts')
        if not create:
            return None
        cursor = connection.execute(
            '''INSERT INTO namespaces(origin, organization_uuid, namespace_key)
               VALUES (?, ?, ?)''',
            (origin, organization_hex, namespace_key),
        )
        return cast(int, cursor.lastrowid)

    def _secure_database_files_before_connect(self) -> None:
        self._ensure_private_database_file(self._database_path, create=True)
        for suffix in ('-wal', '-shm'):
            path = Path(f'{self._database_path}{suffix}')
            if os.path.lexists(path):
                self._ensure_private_database_file(path, create=False)

    def _ensure_private_database_file(self, path: Path, *, create: bool) -> None:
        descriptor: int | None = None
        try:
            if create and not os.path.lexists(path):
                descriptor = secure_filesystem.create_generated_file(self.root, path, 0o600)
            else:
                descriptor = secure_filesystem.open_generated_file(self.root, path)
            secure_filesystem.set_private_permissions(path, 0o600)
            secure_filesystem.verify_private_permissions(path, 0o600)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _harden_database_files(self) -> None:
        for suffix in ('', '-wal', '-shm'):
            path = Path(f'{self._database_path}{suffix}')
            if os.path.lexists(path):
                self._ensure_private_database_file(path, create=False)

    @contextmanager
    def _filesystem_lock(self) -> Iterator[None]:
        try:
            with secure_filesystem.process_lock(self.root, _LOCK_NAME, self._lock):
                yield
        except InventoryStoreError:
            raise
        except (OSError, secure_filesystem.FilesystemError):
            raise InventoryStoreError(_STORAGE_ERROR) from None

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            connection.execute('BEGIN DEFERRED')
            try:
                yield connection
            finally:
                connection.rollback()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._harden_database_files()

    @contextmanager
    def _storage_boundary(self) -> Iterator[None]:
        try:
            yield
        except (InventoryStoreError, ValueError):
            raise
        except (OSError, sqlite3.Error, secure_filesystem.FilesystemError):
            raise InventoryStoreError(_STORAGE_ERROR) from None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise InventoryStoreError('inventory store is not open')
        return self._connection


def _validated_command_request(
    value: object, operation: InventoryOperation
) -> InventoryCommandRequest:
    if not isinstance(value, InventoryCommandRequest) or value.operation != operation:
        raise ValueError('inventory command request is invalid')
    if not all(
        isinstance(item, UUID)
        for item in (
            value.reservation_uuid,
            value.roast_uuid,
            value.lot_id,
            value.client_instance_uuid,
        )
    ):
        raise ValueError('inventory command request is invalid')
    request_json: object = value.request_json
    if not isinstance(request_json, bytes) or not 1 <= len(request_json) <= MAX_JSON_BYTES:
        raise ValueError('inventory command request is invalid')
    try:
        canonical = _canonical_json_bytes(request_json)
    except InventoryStoreError:
        raise ValueError('inventory command request is invalid') from None
    if canonical != request_json:
        raise ValueError('inventory command request is invalid')
    _bounded_text(
        value.idempotency_key,
        _MAX_IDEMPOTENCY_KEY_CHARS,
        None,
        'idempotency key',
    )
    _datetime_text(value.occurred_at)
    if not _bounded_int(value.planned_grams, 1, POSTGRESQL_INTEGER_MAX):
        raise ValueError('inventory command request is invalid')
    actual = value.requested_actual_grams
    if actual is not None and not _bounded_int(actual, 1, POSTGRESQL_INTEGER_MAX):
        raise ValueError('inventory command request is invalid')
    if operation != 'finalize' and actual is not None:
        raise ValueError('inventory command request is invalid')
    try:
        if operation == 'reserve':
            expected = build_reserve_request(
                client_instance_uuid=value.client_instance_uuid,
                reservation_uuid=value.reservation_uuid,
                roast_uuid=value.roast_uuid,
                lot_id=value.lot_id,
                planned_grams=value.planned_grams,
                occurred_at=value.occurred_at,
            )
        elif operation == 'finalize':
            expected = build_finalize_request(
                client_instance_uuid=value.client_instance_uuid,
                reservation_uuid=value.reservation_uuid,
                roast_uuid=value.roast_uuid,
                lot_id=value.lot_id,
                planned_grams=value.planned_grams,
                actual_grams=value.requested_actual_grams,
                occurred_at=value.occurred_at,
            )
        else:
            expected = build_release_request(
                client_instance_uuid=value.client_instance_uuid,
                reservation_uuid=value.reservation_uuid,
                roast_uuid=value.roast_uuid,
                lot_id=value.lot_id,
                planned_grams=value.planned_grams,
                occurred_at=value.occurred_at,
            )
    except (TypeError, ValueError):
        raise ValueError('inventory command request is invalid') from None
    if value != expected:
        raise ValueError('inventory command request is invalid')
    return value


def _require_request_matches_roast(
    row: sqlite3.Row, request: InventoryCommandRequest
) -> None:
    if (
        row['roast_uuid'] != request.roast_uuid.hex
        or row['lot_uuid'] != request.lot_id.hex
        or row['client_reservation_uuid'] != request.reservation_uuid.hex
        or row['planned_grams'] != request.planned_grams
    ):
        raise InventoryStoreError('inventory immutable reservation conflicts')


def _command_body_matches(
    row: sqlite3.Row, request: InventoryCommandRequest
) -> bool:
    return bool(
        row['roast_uuid'] == request.roast_uuid.hex
        and row['lot_uuid'] == request.lot_id.hex
        and row['reservation_uuid'] == request.reservation_uuid.hex
        and row['operation'] == request.operation
        and row['request_json'] == request.request_json
        and row['idempotency_key'] == request.idempotency_key
    )


def _command_identifier(value: object) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise ValueError('inventory command identifier is invalid')
    return value


def _failure_fields(failure: object) -> tuple[str, str]:
    if not isinstance(failure, PublicFailure):
        raise ValueError('failure is invalid')
    kind: object = failure.kind
    retryable: object = failure.retryable
    if not isinstance(kind, FailureKind) or type(retryable) is not bool:
        raise ValueError('failure is invalid')
    code = _bounded_text(
        failure.code, _MAX_ERROR_CODE_CHARS, None, 'failure code'
    )
    _bounded_text(
        failure.message,
        MAX_ERROR_MESSAGE_CODE_POINTS,
        MAX_ERROR_MESSAGE_CODE_POINTS * 4,
        'failure message',
    )
    if kind not in _FAILURE_CODES_BY_KIND or code not in _FAILURE_CODES_BY_KIND[kind]:
        raise ValueError('failure is invalid')
    return code, FAILURE_MESSAGES[kind]


def _pause_code(value: object) -> str:
    code = _bounded_text(value, _MAX_ERROR_CODE_CHARS, None, 'pause code')
    if code not in _PAUSE_CODES:
        raise ValueError('pause code is invalid')
    return code


def _validated_mutation_result(
    value: object, command: sqlite3.Row, roast: sqlite3.Row
) -> InventoryMutationResult:
    if not isinstance(value, InventoryMutationResult):
        raise ValueError('inventory mutation result is invalid')
    reservation_value: object = value.reservation
    balance_value: object = value.balance
    conflict_value: object = value.conflict
    replay_value: object = value.idempotent_replay
    if (
        not isinstance(reservation_value, InventoryReservation)
        or not isinstance(balance_value, InventoryBalance)
        or (
            conflict_value is not None
            and not isinstance(conflict_value, InventoryConflict)
        )
        or type(replay_value) is not bool
    ):
        raise ValueError('inventory mutation result is invalid')
    reservation = reservation_value
    balance = balance_value
    conflict = conflict_value
    operation = command['operation']
    expected_state = {
        'reserve': 'reserved',
        'finalize': 'finalized',
        'release': 'released',
    }.get(operation)
    key_parts = cast(str, command['idempotency_key']).split(':')
    if len(key_parts) != 4:
        raise ValueError('inventory mutation result is invalid')
    try:
        expected_client_uuid = UUID(hex=key_parts[1])
    except ValueError:
        raise ValueError('inventory mutation result is invalid') from None
    expected_actual = (
        roast['actual_grams'] or roast['planned_grams']
        if operation == 'finalize'
        else None
    )
    reservation_uuid_values: tuple[object, ...] = (
        reservation.reservation_id,
        reservation.client_reservation_uuid,
        reservation.lot_id,
        reservation.roast_uuid,
        reservation.client_instance_uuid,
    )
    balance_lot_id: object = balance.lot_id
    if not all(isinstance(item, UUID) for item in reservation_uuid_values) or not isinstance(
        balance_lot_id, UUID
    ):
        raise ValueError('inventory mutation result is invalid')
    if (
        reservation.client_reservation_uuid.hex != command['reservation_uuid']
        or reservation.lot_id.hex != command['lot_uuid']
        or reservation.roast_uuid.hex != command['roast_uuid']
        or reservation.client_instance_uuid != expected_client_uuid
        or reservation.state != expected_state
        or reservation.planned_grams != roast['planned_grams']
        or reservation.actual_grams != expected_actual
        or balance.lot_id != reservation.lot_id
        or reservation.open_conflict_id
        != (None if conflict is None else conflict.conflict_id)
    ):
        raise ValueError('inventory mutation result relationships are invalid')
    for timestamp in (
        reservation.reserved_at,
        reservation.created_at,
        reservation.updated_at,
    ):
        _datetime_text(timestamp)
    if operation == 'reserve':
        if reservation.completed_at is not None:
            raise ValueError('inventory mutation result is invalid')
    elif reservation.completed_at is None:
        raise ValueError('inventory mutation result is invalid')
    else:
        _datetime_text(reservation.completed_at)
    if (
        not _bounded_int(reservation.planned_grams, 1, POSTGRESQL_INTEGER_MAX)
        or (
            reservation.actual_grams is not None
            and not _bounded_int(
                reservation.actual_grams, 1, POSTGRESQL_INTEGER_MAX
            )
        )
        or not _bounded_int(
            balance.on_hand_grams,
            -POSTGRESQL_INTEGER_MAX,
            POSTGRESQL_INTEGER_MAX,
        )
        or not _bounded_int(balance.reserved_grams, 0, POSTGRESQL_INTEGER_MAX)
        or not _bounded_int(
            balance.available_grams,
            -POSTGRESQL_INTEGER_MAX,
            POSTGRESQL_INTEGER_MAX,
        )
        or not _bounded_int(
            balance.unresolved_conflict_count, 0, POSTGRESQL_INTEGER_MAX
        )
        or balance.available_grams != balance.on_hand_grams - balance.reserved_grams
    ):
        raise ValueError('inventory mutation result is invalid')
    if conflict is not None:
        if (
            not all(
                isinstance(item, UUID)
                for item in (
                    conflict.conflict_id,
                    conflict.lot_id,
                    conflict.source_ledger_entry_id,
                )
            )
            or conflict.lot_id != reservation.lot_id
            or conflict.roast_uuid != reservation.roast_uuid
            or conflict.reservation_id != reservation.reservation_id
            or conflict.state != 'open'
            or conflict.resolved_at is not None
            or conflict.resolved_by_user_id is not None
            or conflict.resolution_note is not None
        ):
            raise ValueError('inventory mutation result relationships are invalid')
        _datetime_text(conflict.created_at)
    return value


def _namespace_from_command(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> Namespace:
    namespace = connection.execute(
        'SELECT * FROM namespaces WHERE id = ?', (row['namespace_id'],)
    ).fetchone()
    if namespace is None:
        raise InventoryStoreError('inventory namespace is unavailable')
    return Namespace(
        _stored_bounded_text(namespace['origin'], _MAX_ORIGIN_CHARS, None),
        UUID(hex=_stored_uuid_hex(namespace['organization_uuid'])),
        f"namespace-sha256:{_stored_namespace_key(namespace['namespace_key'])}",
    )


def _row_to_command(row: sqlite3.Row, namespace: Namespace) -> InventoryCommand:
    _validate_stored_command(row, {cast(int, row['namespace_id']): ('', '', '')})
    return InventoryCommand(
        id=_stored_uuid_hex(row['id']),
        namespace=namespace,
        roast_uuid=UUID(hex=_stored_uuid_hex(row['roast_uuid'])),
        lot_id=UUID(hex=_stored_uuid_hex(row['lot_uuid'])),
        reservation_uuid=UUID(hex=_stored_uuid_hex(row['reservation_uuid'])),
        operation=cast(InventoryOperation, row['operation']),
        request_json=cast(bytes, row['request_json']),
        idempotency_key=_stored_bounded_text(
            row['idempotency_key'], _MAX_IDEMPOTENCY_KEY_CHARS, None
        ),
        dependency_id=_stored_optional_uuid_hex(row['dependency_id']),
        state=cast(InventoryCommandState, row['state']),
        attempts=cast(int, row['attempts']),
        next_attempt_at=_optional_stored_datetime(row['next_attempt_at']),
        lease_expires_at=_optional_stored_datetime(row['lease_expires_at']),
        lease_token=_stored_optional_uuid_hex(row['lease_token']),
        error_code=_stored_optional_bounded_text(
            row['error_code'], _MAX_ERROR_CODE_CHARS
        ),
        error_message=_stored_optional_bounded_text(
            row['error_message'], MAX_ERROR_MESSAGE_CODE_POINTS
        ),
        created_at=_stored_datetime(row['created_at']),
        updated_at=_stored_datetime(row['updated_at']),
    )


def _row_to_roast(row: sqlite3.Row, namespace: Namespace) -> InventoryRoastState:
    _validate_stored_roast(row, {cast(int, row['namespace_id']): ('', '', '')})
    balance: InventoryBalance | None
    if row['balance_on_hand_grams'] is None:
        balance = None
    else:
        balance = InventoryBalance(
            UUID(hex=_stored_uuid_hex(row['lot_uuid'])),
            cast(int, row['balance_on_hand_grams']),
            cast(int, row['balance_reserved_grams']),
            cast(int, row['balance_available_grams']),
            cast(int, row['balance_unresolved_conflict_count']),
        )
    return InventoryRoastState(
        namespace=namespace,
        roast_uuid=UUID(hex=_stored_uuid_hex(row['roast_uuid'])),
        lot_id=UUID(hex=_stored_uuid_hex(row['lot_uuid'])),
        lot_name=_stored_bounded_text(
            row['lot_name'], _MAX_LOT_NAME_CHARS, _MAX_LOT_NAME_BYTES
        ),
        reservation_uuid=UUID(
            hex=_stored_uuid_hex(row['client_reservation_uuid'])
        ),
        server_reservation_uuid=(
            None
            if row['server_reservation_uuid'] is None
            else UUID(hex=_stored_uuid_hex(row['server_reservation_uuid']))
        ),
        planned_grams=cast(int, row['planned_grams']),
        actual_grams=cast(int | None, row['actual_grams']),
        lifecycle=cast(InventoryLifecycle, row['lifecycle']),
        terminal_intent=cast(Literal['finalize', 'release'] | None, row['terminal_intent']),
        reserve_occurred_at=_stored_datetime(row['reserve_occurred_at']),
        finalize_occurred_at=_optional_stored_datetime(row['finalize_occurred_at']),
        release_occurred_at=_optional_stored_datetime(row['release_occurred_at']),
        server_state=cast(ReservationState | None, row['server_state']),
        balance=balance,
        conflict_id=(
            None
            if row['conflict_uuid'] is None
            else UUID(hex=_stored_uuid_hex(row['conflict_uuid']))
        ),
        error_code=_stored_optional_bounded_text(
            row['error_code'], _MAX_ERROR_CODE_CHARS
        ),
        error_message=_stored_optional_bounded_text(
            row['error_message'], MAX_ERROR_MESSAGE_CODE_POINTS
        ),
        created_at=_stored_datetime(row['created_at']),
        updated_at=_stored_datetime(row['updated_at']),
    )


def _row_to_interrupted(row: sqlite3.Row) -> InterruptedReservation:
    namespace = Namespace(
        _stored_bounded_text(row['origin'], _MAX_ORIGIN_CHARS, None),
        UUID(hex=_stored_uuid_hex(row['organization_uuid'])),
        f"namespace-sha256:{_stored_namespace_key(row['namespace_key'])}",
    )
    roast = _row_to_roast(row, namespace)
    return InterruptedReservation(
        namespace,
        roast.roast_uuid,
        roast.lot_id,
        roast.lot_name,
        roast.reservation_uuid,
        roast.planned_grams,
        roast.lifecycle,
        roast.updated_at,
    )


def _namespace_values(namespace: object) -> tuple[str, str, str]:
    if not isinstance(namespace, Namespace):
        raise ValueError('inventory namespace is invalid')
    origin = _bounded_text(namespace.origin, _MAX_ORIGIN_CHARS, None, 'namespace')
    if any(ord(character) < 32 or ord(character) == 127 for character in origin):
        raise ValueError('inventory namespace is invalid')
    if not isinstance(namespace.organization_id, UUID):
        raise ValueError('inventory namespace is invalid')
    match = _NAMESPACE_KEY_RE.fullmatch(namespace.key)
    if match is None:
        raise ValueError('inventory namespace is invalid')
    try:
        expected = hashlib.sha256(
            f'{origin}\n{namespace.organization_id}'.encode()
        ).hexdigest()
    except UnicodeEncodeError:
        raise ValueError('inventory namespace is invalid') from None
    if not hmac.compare_digest(match.group(1), expected):
        raise ValueError('inventory namespace is invalid')
    return origin, namespace.organization_id.hex, match.group(1)


def _validated_lots(lots: object) -> tuple[BeanLot, ...]:
    if not isinstance(lots, tuple):
        raise ValueError('inventory lots are invalid')
    raw_lots = cast(tuple[object, ...], lots)
    if len(raw_lots) > MAX_CACHED_LOTS:
        raise ValueError('inventory lots are invalid')
    result = tuple(_validated_lot(item) for item in raw_lots)
    if len({item.lot_id for item in result}) != len(result):
        raise InventoryStoreError('inventory lot UUIDs are duplicated')
    return result


def _validated_lot(value: object) -> BeanLot:
    if not isinstance(value, BeanLot) or not isinstance(value.lot_id, UUID):
        raise ValueError('inventory lot is invalid')
    name = _bounded_text(
        value.name, _MAX_LOT_NAME_CHARS, _MAX_LOT_NAME_BYTES, 'lot'
    )
    origin = (
        None
        if value.origin is None
        else _bounded_text(
            value.origin, _MAX_DESCRIPTOR_CHARS, _MAX_DESCRIPTOR_BYTES, 'lot'
        )
    )
    raw_varietals: object = value.varietals
    if not isinstance(raw_varietals, tuple) or len(raw_varietals) > _MAX_VARIETALS:
        raise ValueError('inventory lot is invalid')
    varietals = tuple(
        _bounded_text(
            item, _MAX_DESCRIPTOR_CHARS, _MAX_DESCRIPTOR_BYTES, 'lot'
        )
        for item in cast(tuple[object, ...], raw_varietals)
    )
    if len(set(varietals)) != len(varietals):
        raise ValueError('inventory lot is invalid')
    raw_processing: object = value.processing_method
    if raw_processing is not None and raw_processing not in _PROCESSING_METHODS:
        raise ValueError('inventory lot is invalid')
    processing = cast(ProcessingMethod | None, raw_processing)
    crop_year = value.crop_year
    if crop_year is not None and not _bounded_int(crop_year, 1_000, 9_999):
        raise ValueError('inventory lot is invalid')
    if (
        not _bounded_int(value.on_hand_grams, -POSTGRESQL_INTEGER_MAX, POSTGRESQL_INTEGER_MAX)
        or not _bounded_int(value.reserved_grams, 0, POSTGRESQL_INTEGER_MAX)
        or not _bounded_int(value.available_grams, -POSTGRESQL_INTEGER_MAX, POSTGRESQL_INTEGER_MAX)
        or not _bounded_int(value.unresolved_conflict_count, 0, POSTGRESQL_INTEGER_MAX)
        or value.available_grams != value.on_hand_grams - value.reserved_grams
    ):
        raise ValueError('inventory lot is invalid')
    return BeanLot(
        lot_id=value.lot_id,
        name=name,
        origin=origin,
        varietals=varietals,
        processing_method=processing,
        crop_year=crop_year,
        on_hand_grams=value.on_hand_grams,
        reserved_grams=value.reserved_grams,
        available_grams=value.available_grams,
        unresolved_conflict_count=value.unresolved_conflict_count,
    )


def _validate_stored_roast(
    row: sqlite3.Row, namespaces: dict[int, tuple[str, str, str]]
) -> None:
    if cast(int, row['namespace_id']) not in namespaces:
        raise InventoryStoreError('stored inventory roast is invalid')
    for field in ('roast_uuid', 'lot_uuid', 'client_reservation_uuid'):
        _stored_uuid_hex(row[field])
    _stored_optional_uuid_hex(row['server_reservation_uuid'])
    _stored_optional_uuid_hex(row['conflict_uuid'])
    _stored_bounded_text(row['lot_name'], _MAX_LOT_NAME_CHARS, _MAX_LOT_NAME_BYTES)
    planned_grams = row['planned_grams']
    actual_grams = row['actual_grams']
    if not _bounded_int(planned_grams, 1, POSTGRESQL_INTEGER_MAX) or (
        actual_grams is not None
        and not _bounded_int(actual_grams, 1, POSTGRESQL_INTEGER_MAX)
    ):
        raise InventoryStoreError('stored inventory roast is invalid')
    lifecycle = row['lifecycle']
    if lifecycle not in {
        'reserve_queued',
        'reserved',
        'finalize_queued',
        'finalized',
        'release_queued',
        'released',
        'paused',
        'failed',
    }:
        raise InventoryStoreError('stored inventory roast is invalid')
    terminal_intent = row['terminal_intent']
    finalize_at = _optional_stored_datetime(row['finalize_occurred_at'])
    release_at = _optional_stored_datetime(row['release_occurred_at'])
    _stored_datetime(row['reserve_occurred_at'])
    _stored_datetime(row['created_at'])
    _stored_datetime(row['updated_at'])
    if not (
        (terminal_intent is None and finalize_at is None and release_at is None)
        or (terminal_intent == 'finalize' and finalize_at is not None and release_at is None)
        or (terminal_intent == 'release' and finalize_at is None and release_at is not None)
    ):
        raise InventoryStoreError('stored inventory roast is invalid')
    server_state = row['server_state']
    server_uuid = row['server_reservation_uuid']
    if (
        server_state not in {None, 'reserved', 'finalized', 'released'}
        or (server_state is None) != (server_uuid is None)
        or (
            server_state == 'finalized'
            and (terminal_intent != 'finalize' or actual_grams is None)
        )
        or (
            server_state == 'released'
            and (terminal_intent != 'release' or actual_grams is not None)
        )
        or (actual_grams is not None and terminal_intent != 'finalize')
    ):
        raise InventoryStoreError('stored inventory roast is invalid')
    balance_values = tuple(
        row[field]
        for field in (
            'balance_on_hand_grams',
            'balance_reserved_grams',
            'balance_available_grams',
            'balance_unresolved_conflict_count',
        )
    )
    if all(value is None for value in balance_values):
        has_balance = False
    elif all(value is not None for value in balance_values):
        on_hand, reserved, available, conflicts = balance_values
        if (
            not _bounded_int(on_hand, -POSTGRESQL_INTEGER_MAX, POSTGRESQL_INTEGER_MAX)
            or not _bounded_int(reserved, 0, POSTGRESQL_INTEGER_MAX)
            or not _bounded_int(available, -POSTGRESQL_INTEGER_MAX, POSTGRESQL_INTEGER_MAX)
            or not _bounded_int(conflicts, 0, POSTGRESQL_INTEGER_MAX)
            or available != on_hand - reserved
        ):
            raise InventoryStoreError('stored inventory roast is invalid')
        has_balance = True
    else:
        raise InventoryStoreError('stored inventory roast is invalid')
    if row['conflict_uuid'] is not None and (not has_balance or server_state is None):
        raise InventoryStoreError('stored inventory roast is invalid')
    error_code = _stored_optional_bounded_text(row['error_code'], _MAX_ERROR_CODE_CHARS)
    error_message = _stored_optional_bounded_text(
        row['error_message'], MAX_ERROR_MESSAGE_CODE_POINTS
    )
    if not (
        (lifecycle not in {'paused', 'failed'} and error_code is None and error_message is None)
        or (
            lifecycle == 'paused'
            and error_code is not None
            and error_message is None
            and _stored_failure_code_is_known(error_code)
        )
        or (
            lifecycle == 'failed'
            and error_code is not None
            and error_message is not None
            and _stored_failure_pair_is_known(error_code, error_message)
        )
    ):
        raise InventoryStoreError('stored inventory roast is invalid')


def _validate_stored_command(
    row: sqlite3.Row, namespaces: dict[int, tuple[str, str, str]]
) -> None:
    if cast(int, row['namespace_id']) not in namespaces:
        raise InventoryStoreError('stored inventory command is invalid')
    command_id = _stored_uuid_hex(row['id'])
    for field in ('roast_uuid', 'lot_uuid', 'reservation_uuid'):
        _stored_uuid_hex(row[field])
    operation = row['operation']
    dependency = _stored_optional_uuid_hex(row['dependency_id'])
    if not (
        (operation == 'reserve' and dependency is None)
        or (
            operation in {'finalize', 'release'}
            and dependency is not None
            and dependency != command_id
        )
    ):
        raise InventoryStoreError('stored inventory command is invalid')
    request = row['request_json']
    if (
        not isinstance(request, bytes)
        or not 1 <= len(request) <= MAX_JSON_BYTES
        or request != _canonical_json_bytes(request)
    ):
        raise InventoryStoreError('stored inventory command is invalid')
    _stored_bounded_text(
        row['idempotency_key'], _MAX_IDEMPOTENCY_KEY_CHARS, None
    )
    attempts = row['attempts']
    if type(attempts) is not int or attempts < 0:
        raise InventoryStoreError('stored inventory command is invalid')
    state = row['state']
    next_attempt = _optional_stored_datetime(row['next_attempt_at'])
    lease_expires = _optional_stored_datetime(row['lease_expires_at'])
    lease_token = _stored_optional_uuid_hex(row['lease_token'])
    error_code = _stored_optional_bounded_text(row['error_code'], _MAX_ERROR_CODE_CHARS)
    error_message = _stored_optional_bounded_text(
        row['error_message'], MAX_ERROR_MESSAGE_CODE_POINTS
    )
    _stored_datetime(row['created_at'])
    _stored_datetime(row['updated_at'])
    completed = _optional_stored_datetime(row['completed_at'])
    if (state == 'leased') != (lease_expires is not None and lease_token is not None):
        raise InventoryStoreError('stored inventory command is invalid')
    if (state == 'complete') != (completed is not None):
        raise InventoryStoreError('stored inventory command is invalid')
    valid_state = (
        (state == 'pending' and next_attempt is None and error_code is None and error_message is None)
        or (state == 'leased' and next_attempt is None and error_code is None and error_message is None)
        or (
            state == 'retry_wait'
            and next_attempt is not None
            and error_code is not None
            and error_message is not None
            and _stored_failure_pair_is_known(error_code, error_message)
        )
        or (
            state == 'paused'
            and error_code is not None
            and error_message is None
            and _stored_failure_code_is_known(error_code)
        )
        or (
            state == 'failed'
            and next_attempt is None
            and error_code is not None
            and error_message is not None
            and _stored_failure_pair_is_known(error_code, error_message)
        )
        or (state == 'complete' and next_attempt is None and error_code is None and error_message is None)
    )
    if not valid_state:
        raise InventoryStoreError('stored inventory command is invalid')


def _stored_failure_code_is_known(code: str) -> bool:
    return (
        code in _PAUSE_CODES
        or code == _DEPENDENCY_FAILURE_CODE
        or any(code in codes for codes in _FAILURE_CODES_BY_KIND.values())
    )


def _stored_failure_pair_is_known(code: str, message: str) -> bool:
    if code == _DEPENDENCY_FAILURE_CODE:
        return message == _DEPENDENCY_FAILURE_MESSAGE
    return any(
        code in codes and message == FAILURE_MESSAGES[kind]
        for kind, codes in _FAILURE_CODES_BY_KIND.items()
    )


def _row_to_lot(row: sqlite3.Row, generation: str) -> BeanLot:
    try:
        if _stored_uuid_hex(row['generation']) != generation:
            raise InventoryStoreError('stored lot cache is invalid')
        varietals = _stored_varietals(row['varietals_json'])
        lot = BeanLot(
            lot_id=UUID(hex=_stored_uuid_hex(row['lot_uuid'])),
            name=_stored_bounded_text(
                row['name'], _MAX_LOT_NAME_CHARS, _MAX_LOT_NAME_BYTES
            ),
            origin=(
                None
                if row['origin'] is None
                else _stored_bounded_text(
                    row['origin'], _MAX_DESCRIPTOR_CHARS, _MAX_DESCRIPTOR_BYTES
                )
            ),
            varietals=varietals,
            processing_method=cast(ProcessingMethod | None, row['processing_method']),
            crop_year=cast(int | None, row['crop_year']),
            on_hand_grams=cast(int, row['on_hand_grams']),
            reserved_grams=cast(int, row['reserved_grams']),
            available_grams=cast(int, row['available_grams']),
            unresolved_conflict_count=cast(int, row['unresolved_conflict_count']),
        )
        return _validated_lot(lot)
    except (KeyError, TypeError, ValueError):
        raise InventoryStoreError('stored lot cache is invalid') from None


def _varietals_json(varietals: tuple[str, ...]) -> str:
    return json.dumps(
        varietals,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
    )


def _stored_varietals(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise InventoryStoreError('stored lot cache is invalid')
    try:
        if len(value.encode('utf-8')) > MAX_JSON_BYTES:
            raise InventoryStoreError('stored lot cache is invalid')
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (RecursionError, UnicodeError, json.JSONDecodeError, ValueError):
        raise InventoryStoreError('stored lot cache is invalid') from None
    if not isinstance(decoded, list):
        raise InventoryStoreError('stored lot cache is invalid')
    raw_varietals = cast(list[object], decoded)
    varietals = tuple(
        _stored_bounded_text(item, _MAX_DESCRIPTOR_CHARS, _MAX_DESCRIPTOR_BYTES)
        for item in raw_varietals
    )
    if (
        len(varietals) > _MAX_VARIETALS
        or len(set(varietals)) != len(varietals)
        or value != _varietals_json(varietals)
    ):
        raise InventoryStoreError('stored lot cache is invalid')
    return varietals


def _bounded_text(
    value: object,
    maximum_chars: int,
    maximum_bytes: int | None,
    label: str,
) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum_chars:
        raise ValueError(f'inventory {label} is invalid')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError(f'inventory {label} is invalid') from None
    if maximum_bytes is not None and len(encoded) > maximum_bytes:
        raise ValueError(f'inventory {label} is invalid')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f'inventory {label} is invalid')
    return value


def _stored_bounded_text(
    value: object, maximum_chars: int, maximum_bytes: int | None
) -> str:
    try:
        return _bounded_text(value, maximum_chars, maximum_bytes, 'stored value')
    except ValueError:
        raise InventoryStoreError('stored inventory text is invalid') from None


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _datetime_text(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('inventory timestamp must be timezone-aware')
    return value.astimezone(UTC).isoformat(timespec='microseconds')


def _stored_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise InventoryStoreError('stored lot cache timestamp is invalid')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InventoryStoreError('stored lot cache timestamp is invalid') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or _datetime_text(parsed) != value:
        raise InventoryStoreError('stored lot cache timestamp is invalid')
    return parsed.astimezone(UTC)


def _optional_stored_datetime(value: object) -> datetime | None:
    return None if value is None else _stored_datetime(value)


def _stored_uuid_hex(value: object) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise InventoryStoreError('stored inventory UUID is invalid')
    return value


def _stored_optional_uuid_hex(value: object) -> str | None:
    return None if value is None else _stored_uuid_hex(value)


def _stored_optional_bounded_text(value: object, maximum_chars: int) -> str | None:
    return (
        None
        if value is None
        else _stored_bounded_text(value, maximum_chars, maximum_chars * 4)
    )


def _stored_namespace_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.casefold() != value
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise InventoryStoreError('stored inventory namespace is invalid')
    return value


def _canonical_json_bytes(value: bytes) -> bytes:
    try:
        decoded = json.loads(
            value.decode('utf-8'),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
        return json.dumps(
            decoded,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        ).encode('utf-8')
    except (RecursionError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        raise InventoryStoreError('stored inventory command is invalid') from None


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError('non-finite JSON number')


def _schema_fingerprint(connection: sqlite3.Connection) -> _SchemaFingerprint:
    objects: tuple[_SchemaObject, ...] = tuple(
        (
            row[0],
            row[1],
            _normalize_schema_sql(row[2]) if isinstance(row[2], str) else row[2],
        )
        for row in connection.execute(
            '''SELECT type, name, sql FROM sqlite_master
               WHERE NOT (type = 'index' AND sql IS NULL
                          AND name LIKE 'sqlite_autoindex_%')
               ORDER BY type, name'''
        ).fetchall()
    )
    tables = tuple(row[1] for row in objects if row[0] == 'table')
    pragma_fingerprints: list[_SchemaTableFingerprint] = []
    for table in tables:
        if not isinstance(table, str):
            raise sqlite3.DatabaseError('invalid schema object name')
        quoted_table = _quote_sqlite_identifier(table)
        columns = tuple(
            tuple(row)
            for row in connection.execute(f'PRAGMA table_info({quoted_table})').fetchall()
        )
        foreign_keys = tuple(
            tuple(row)
            for row in connection.execute(
                f'PRAGMA foreign_key_list({quoted_table})'
            ).fetchall()
        )
        indexes: list[_SchemaIndexFingerprint] = []
        for index in connection.execute(
            f'PRAGMA index_list({quoted_table})'
        ).fetchall():
            index_tuple = tuple(index)
            if len(index_tuple) != 5 or not isinstance(index_tuple[1], str):
                raise sqlite3.DatabaseError('invalid schema index metadata')
            quoted_index = _quote_sqlite_identifier(index_tuple[1])
            index_info = tuple(
                tuple(row)
                for row in connection.execute(
                    f'PRAGMA index_info({quoted_index})'
                ).fetchall()
            )
            indexes.append((index_tuple, index_info))
        pragma_fingerprints.append((table, columns, foreign_keys, tuple(indexes)))
    return objects, tuple(pragma_fingerprints)


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_schema_sql(value: str) -> str:
    result: list[str] = []
    pending_space = False
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            pending_space = bool(result)
            index += 1
            continue
        if pending_space:
            result.append(' ')
            pending_space = False
        if character not in {'\'', '"', '`', '['}:
            result.append(character.casefold())
            index += 1
            continue
        closing = ']' if character == '[' else character
        result.append(character)
        index += 1
        while index < len(value):
            quoted = value[index]
            result.append(quoted)
            index += 1
            if quoted != closing:
                continue
            if index < len(value) and value[index] == closing:
                result.append(value[index])
                index += 1
                continue
            break
    return ''.join(result).strip()


__all__ = [
    'FailedInventoryCommand',
    'InterruptedReservation',
    'InventoryCommand',
    'InventoryCommandState',
    'InventoryLifecycle',
    'InventoryQueueCounts',
    'InventoryRoastState',
    'InventoryStore',
    'InventoryStoreError',
    'LotCacheSnapshot',
]
