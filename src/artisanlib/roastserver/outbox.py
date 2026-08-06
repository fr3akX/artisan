#
# ABOUT
# Artisan Roast Server immutable snapshots and durable upload outbox
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

# Portable Win32 ABI tests intentionally consume private filesystem shims and imports.
# Storage boundaries pass domain errors through before normalizing backend failures.
# pylint: disable=protected-access,try-except-raise,unused-import

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes  # noqa: F401  # compatibility seam for portable Win32 ABI tests
from ctypes import wintypes  # noqa: F401  # compatibility seam for portable Win32 ABI tests
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import errno
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID, uuid4

from artisanlib.roastserver import _filesystem as secure_filesystem
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    MAX_ERROR_MESSAGE_CODE_POINTS,
    MAX_METADATA_BYTES,
    MAX_PROFILE_BYTES,
    FailureKind,
    Namespace,
    PublicFailure,
)

if TYPE_CHECKING:
    from artisanlib.roastserver.metadata import ProjectedMetadata

_SCHEMA_VERSION: Final[int] = 2
_DATABASE_NAME: Final[str] = 'outbox.sqlite3'
_BUSY_TIMEOUT_MS: Final[int] = 5000
_COPY_CHUNK_BYTES: Final[int] = 1024 * 1024
_STAGING_SECONDS: Final[int] = 15 * 60
_MAX_LEASE_SECONDS: Final[int] = 24 * 60 * 60
_FAILURE_CODE_CHARS: Final[int] = 100
_OUTBOX_STORAGE_ERROR: Final[str] = 'outbox storage operation failed'
_SNAPSHOT_STORAGE_ERROR: Final[str] = 'saved profile could not be staged'
_STAGE_TOKEN_ERROR: Final[str] = 'snapshot staging token is invalid or expired'
_LEASE_LOST: Final[str] = 'lease_lost'
_IS_WINDOWS: bool = os.name == 'nt'
_NAMESPACE_KEY_RE: Final[re.Pattern[str]] = re.compile(r'^namespace-sha256:([0-9a-f]{64})$')
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_UUID_HEX_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{2}$')
_SNAPSHOT_FILE_RE: Final[re.Pattern[str]] = re.compile(r'^([0-9a-f]{64})\.alog$')
_TEMP_FILE_RE: Final[re.Pattern[str]] = re.compile(r'^\.snapshot-[0-9a-f]{1,128}\.tmp$')
_HAS_DIRECTORY_FDS: Final[bool] = os.name != 'nt' and os.open in os.supports_dir_fd
_JOB_STATES: Final[frozenset[str]] = frozenset(
    {'pending', 'leased', 'retry_wait', 'paused', 'failed', 'complete'}
)
_PUBLIC_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        *(kind.value for kind in FailureKind),
        'archive_unavailable',
        'authentication_required',
        'chart_unavailable',
        'client_closed',
        'connection_error',
        'connector_disabled',
        'credential_removed',
        'idempotency_conflict',
        'internal_error',
        'invalid_metadata',
        'invalid_profile',
        'invalid_request',
        'not_found',
        'object_store_unavailable',
        'parser_busy',
        'parser_timeout',
        'payload_too_large',
        'quota_exceeded',
        'request_error',
        'roast_uuid_mismatch',
        'roast_uuid_missing',
        'service_unavailable',
        'timeout',
        'tls_error',
    }
)
_FAILURE_CODES_BY_KIND: Final[dict[FailureKind, frozenset[str]]] = {
    FailureKind.OFFLINE: frozenset(
        {
            'archive_unavailable',
            'connection_error',
            'internal_error',
            'object_store_unavailable',
            'offline',
            'parser_busy',
            'parser_timeout',
            'service_unavailable',
            'timeout',
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
    FailureKind.PROFILE_REJECTED: frozenset(
        {
            'chart_unavailable',
            'checksum_mismatch',
            'idempotency_conflict',
            'invalid_metadata',
            'invalid_profile',
            'invalid_request',
            'not_found',
            'payload_too_large',
            'profile_rejected',
            'quota_exceeded',
            'roast_uuid_mismatch',
            'roast_uuid_missing',
        }
    ),
    FailureKind.LOCAL_PROFILE: frozenset({'local_profile'}),
    FailureKind.CHECKSUM_MISMATCH: frozenset({'checksum_mismatch'}),
    FailureKind.CACHE_CORRUPT: frozenset({'cache_corrupt'}),
    FailureKind.KEYRING: frozenset({'keyring'}),
    FailureKind.SETTINGS: frozenset({'settings'}),
}
_PAUSE_CODES: Final[frozenset[str]] = frozenset(
    {'connector_disabled', 'credential_rejected', 'credential_removed'}
)
_UNSUPPORTED_DIRECTORY_SYNC_ERRNOS: Final[frozenset[int]] = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, 'ENOTSUP', None),
        getattr(errno, 'EOPNOTSUPP', None),
    )
    if isinstance(value, int)
)

_SCHEMA_V1_STATEMENTS: tuple[str, ...] = (
    '''CREATE TABLE schema_version (
    version INTEGER NOT NULL CHECK (version = 1)
)''',
    '''CREATE TABLE namespaces (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    organization_uuid TEXT NOT NULL CHECK (length(organization_uuid) = 32),
    namespace_key TEXT NOT NULL UNIQUE CHECK (length(namespace_key) = 64),
    UNIQUE(origin, organization_uuid)
)''',
    '''CREATE TABLE snapshots (
    namespace_id INTEGER NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    relative_path TEXT NOT NULL UNIQUE,
    byte_count INTEGER NOT NULL CHECK (byte_count BETWEEN 1 AND 16777216),
    created_at TEXT NOT NULL,
    PRIMARY KEY(namespace_id, sha256)
)''',
    '''CREATE TABLE jobs (
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
)''',
    '''CREATE INDEX jobs_ready_idx
  ON jobs(namespace_id, state, next_attempt_at, created_at)''',
)

_SCHEMA_V2_STATEMENTS: tuple[str, ...] = (
    '''CREATE TABLE schema_version (
    version INTEGER NOT NULL CHECK (version = 2)
)''',
    _SCHEMA_V1_STATEMENTS[1],
    _SCHEMA_V1_STATEMENTS[2],
    '''CREATE TABLE snapshot_staging (
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
)''',
    '''CREATE TABLE jobs (
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
    lease_token TEXT UNIQUE CHECK (lease_token IS NULL OR length(lease_token) = 32),
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(namespace_id, roast_uuid, content_sha256),
    FOREIGN KEY(namespace_id, snapshot_sha256)
      REFERENCES snapshots(namespace_id, sha256),
    CHECK ((state = 'complete' AND snapshot_sha256 IS NULL
                              AND snapshot_relative_path IS NULL
                              AND snapshot_byte_count IS NULL
                              AND completed_at IS NOT NULL)
        OR (state != 'complete' AND snapshot_sha256 IS NOT NULL
                                AND snapshot_relative_path IS NOT NULL
                                AND snapshot_byte_count IS NOT NULL
                                AND completed_at IS NULL)),
    CHECK ((state = 'leased' AND lease_expires_at IS NOT NULL
                              AND lease_token IS NOT NULL)
        OR (state != 'leased' AND lease_expires_at IS NULL
                               AND lease_token IS NULL)),
    CHECK ((state = 'pending' AND next_attempt_at IS NULL
                              AND error_code IS NULL AND error_message IS NULL)
        OR (state = 'leased' AND next_attempt_at IS NULL
                             AND error_code IS NULL AND error_message IS NULL)
        OR (state = 'retry_wait' AND next_attempt_at IS NOT NULL
                                 AND error_code IS NOT NULL
                                 AND error_message IS NOT NULL)
        OR (state = 'paused' AND error_code IS NOT NULL
                             AND error_message IS NULL)
        OR (state = 'failed' AND next_attempt_at IS NULL
                             AND error_code IS NOT NULL
                             AND error_message IS NOT NULL)
        OR (state = 'complete' AND next_attempt_at IS NULL
                               AND error_code IS NULL AND error_message IS NULL))
)''',
    '''CREATE INDEX jobs_ready_idx
  ON jobs(namespace_id, state, next_attempt_at, created_at)''',
    '''CREATE INDEX snapshot_staging_expiry_idx
  ON snapshot_staging(expires_at)''',
)
_CANONICAL_SCHEMA_V2_STATEMENTS: Final[tuple[str, ...]] = _SCHEMA_V2_STATEMENTS


type _SchemaPragmaRow = tuple[object, ...]
type _SchemaObject = tuple[object, object, object]
type _SchemaIndexFingerprint = tuple[
    _SchemaPragmaRow, tuple[_SchemaPragmaRow, ...]
]
type _SchemaTableFingerprint = tuple[
    str,
    tuple[_SchemaPragmaRow, ...],
    tuple[_SchemaPragmaRow, ...],
    tuple[_SchemaIndexFingerprint, ...],
]
type _SchemaFingerprint = tuple[
    tuple[_SchemaObject, ...], tuple[_SchemaTableFingerprint, ...]
]


class OutboxError(RuntimeError):
    pass


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


@dataclass(frozen=True, slots=True, init=False)
class LeaseFailure(Job):
    failure: PublicFailure

    def __init__(self, job: Job, failure: PublicFailure) -> None:
        Job.__init__(
            self,
            id=job.id,
            namespace=job.namespace,
            roast_uuid=job.roast_uuid,
            content_sha256=job.content_sha256,
            snapshot_sha256=job.snapshot_sha256,
            snapshot_path=job.snapshot_path,
            snapshot_byte_count=job.snapshot_byte_count,
            aroast_json=job.aroast_json,
            revision_json=job.revision_json,
            idempotency_key=job.idempotency_key,
            state=job.state,
            attempts=job.attempts,
            next_attempt_at=job.next_attempt_at,
            lease_expires_at=job.lease_expires_at,
            lease_token=job.lease_token,
            error_code=job.error_code,
            error_message=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        object.__setattr__(self, 'failure', failure)

    @property
    def job(self) -> Job:
        return self


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


_WindowsAclSizeInformation = secure_filesystem._WindowsAclSizeInformation
_WindowsAceHeader = secure_filesystem._WindowsAceHeader
_WindowsAccessAllowedAce = secure_filesystem._WindowsAccessAllowedAce
_WindowsFileDispositionInfo = secure_filesystem._WindowsFileDispositionInfo
_WindowsNativeLayer = secure_filesystem._WindowsNativeLayer
type _WindowsNativeApi = secure_filesystem._WindowsNativeApi
_WINDOWS_NATIVE: _WindowsNativeApi | None = secure_filesystem._WINDOWS_NATIVE


@contextmanager
def _storage_boundary() -> Iterator[None]:
    try:
        yield
    except OutboxError:
        raise
    except (OSError, sqlite3.Error):
        raise OutboxError(_OUTBOX_STORAGE_ERROR) from None


class Outbox:
    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._database_path = self.root / _DATABASE_NAME
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            _datetime_text(self._clock())
            connection: sqlite3.Connection | None = None
            try:
                self._prepare_root()
                with self._filesystem_lock():
                    self._secure_database_files_before_connect()
                    connection = sqlite3.connect(
                        self._database_path,
                        timeout=_BUSY_TIMEOUT_MS / 1000,
                        isolation_level=None,
                        check_same_thread=False,
                    )
                    connection.row_factory = sqlite3.Row
                    connection.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
                    journal_mode = cast(
                        str, connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
                    )
                    if journal_mode.lower() != 'wal':
                        raise OutboxError('SQLite WAL mode is unavailable')
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute('PRAGMA foreign_keys=ON')
                    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                        raise OutboxError('SQLite foreign keys are unavailable')
                    self._connection = connection
                    self._migrate()
                    self._validate_durable_rows()
                    self._expire_stages_and_collect(self._clock())
                    self._harden_indexed_snapshots()
                    self._harden_database_files()
            except OutboxError:
                self._connection = None
                if connection is not None:
                    connection.close()
                raise
            except (OSError, sqlite3.Error):
                self._connection = None
                if connection is not None:
                    connection.close()
                raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

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
            except OutboxError:
                raise
            except (OSError, sqlite3.Error):
                raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

    def database_pragmas(self) -> tuple[str, bool, int]:
        with _storage_boundary(), self._lock:
            connection = self._require_connection()
            journal = cast(str, connection.execute('PRAGMA journal_mode').fetchone()[0]).lower()
            foreign_keys = bool(connection.execute('PRAGMA foreign_keys').fetchone()[0])
            busy_timeout = cast(int, connection.execute('PRAGMA busy_timeout').fetchone()[0])
            return journal, foreign_keys, busy_timeout

    def recover_expired_leases(self, now: datetime) -> int:
        now_text = _datetime_text(now)
        with _storage_boundary(), self._filesystem_lock(), self._transaction() as connection:
            cursor = connection.execute(
                '''UPDATE jobs
                       SET state = 'pending', next_attempt_at = NULL,
                           lease_expires_at = NULL, lease_token = NULL,
                           error_code = NULL, error_message = NULL, updated_at = ?
                       WHERE state = 'leased' AND lease_expires_at <= ?''',
                (now_text, now_text),
            )
            return cursor.rowcount

    def snapshot_saved_file(self, namespace: Namespace, source: Path) -> Snapshot:
        try:
            with self._filesystem_lock():
                return self._snapshot_saved_file_locked(namespace, source)
        except OutboxError:
            raise
        except (OSError, sqlite3.Error):
            raise OutboxError(_SNAPSHOT_STORAGE_ERROR) from None

    def snapshot_bytes(
        self,
        namespace: Namespace,
        content: bytes,
        source_modified_at: datetime,
    ) -> Snapshot:
        content_value: object = content
        if not isinstance(content_value, bytes):
            raise ValueError('saved profile content must be immutable bytes')
        if not 1 <= len(content_value) <= MAX_PROFILE_BYTES:
            raise OutboxError('saved profile size is outside the supported range')
        canonical_modified_at = datetime.fromisoformat(
            _datetime_text(source_modified_at)
        )
        try:
            with self._filesystem_lock():
                return self._snapshot_bytes_locked(
                    namespace, content_value, canonical_modified_at
                )
        except (OSError, sqlite3.Error):
            raise OutboxError(_SNAPSHOT_STORAGE_ERROR) from None

    def _snapshot_bytes_locked(
        self,
        namespace: Namespace,
        content: bytes,
        source_modified_at: datetime,
    ) -> Snapshot:
        namespace_key = _namespace_key(namespace)
        destination_directory = self._snapshot_directory(namespace_key, None)
        self._ensure_generated_directory(destination_directory)
        temporary_path: Path | None = None
        published_path: Path | None = None
        published_created = False
        try:
            sha256, byte_count, temporary_path = self._write_bytes_to_temporary(
                content, destination_directory
            )
            relative_path = _snapshot_relative_path(namespace_key, sha256)
            final_path = self.root / relative_path
            published_created = self._publish_temporary(
                temporary_path, final_path, sha256, byte_count
            )
            temporary_path = None
            published_path = final_path
            token = uuid4().hex
            created_at = self._clock()
            created_text = _datetime_text(created_at)
            expires_text = _datetime_text(
                created_at + timedelta(seconds=_STAGING_SECONDS)
            )
            source_modified_text = _datetime_text(source_modified_at)
            with self._transaction() as connection:
                namespace_id = self._namespace_id(connection, namespace, create=True)
                if namespace_id is None:
                    raise OutboxError('namespace was not persisted')
                connection.execute(
                    '''INSERT OR IGNORE INTO snapshots
                       (namespace_id, sha256, relative_path, byte_count, created_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (namespace_id, sha256, relative_path, byte_count, created_text),
                )
                row = connection.execute(
                    '''SELECT relative_path, byte_count FROM snapshots
                       WHERE namespace_id = ? AND sha256 = ?''',
                    (namespace_id, sha256),
                ).fetchone()
                if (
                    row is None
                    or row['relative_path'] != relative_path
                    or row['byte_count'] != byte_count
                ):
                    raise OutboxError('snapshot index conflicts with generated content')
                connection.execute(
                    '''INSERT INTO snapshot_staging
                       (token, namespace_id, sha256, relative_path, byte_count,
                        source_modified_at, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        token,
                        namespace_id,
                        sha256,
                        relative_path,
                        byte_count,
                        source_modified_text,
                        created_text,
                        expires_text,
                    ),
                )
            return Snapshot(
                namespace=namespace,
                sha256=sha256,
                relative_path=relative_path,
                absolute_path=final_path,
                byte_count=byte_count,
                source_modified_at=source_modified_at,
                staging_token=token,
            )
        except BaseException:
            if published_created and published_path is not None:
                self._discard_unowned_publication(namespace, published_path)
            raise
        finally:
            if temporary_path is not None:
                try:
                    self._discard_temporary(temporary_path)
                except OSError:
                    pass

    def _snapshot_saved_file_locked(self, namespace: Namespace, source: Path) -> Snapshot:
        namespace_key = _namespace_key(namespace)
        source_path = Path(source)
        try:
            source_fd = _open_path_readonly(source_path)
        except OutboxError:
            raise
        temporary_path: Path | None = None
        published_path: Path | None = None
        published_created = False
        try:
            before = os.fstat(source_fd)
            _require_regular_file(before, 'saved profile')
            if before.st_size < 1 or before.st_size > MAX_PROFILE_BYTES:
                raise OutboxError('saved profile size is outside the supported range')
            try:
                source_modified_at = datetime.fromtimestamp(before.st_mtime, tz=UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise OutboxError('saved profile modification time is invalid') from exc
            destination_directory = self._snapshot_directory(namespace_key, None)
            self._ensure_generated_directory(destination_directory)
            sha256, byte_count, temporary_path = self._copy_to_temporary(
                source_fd, destination_directory
            )
            after = os.fstat(source_fd)
            if _file_identity(before) != _file_identity(after) or byte_count != before.st_size:
                raise OutboxError('saved profile changed while it was copied')
            reopened_fd = _open_path_readonly(source_path)
            try:
                reopened = os.fstat(reopened_fd)
            finally:
                os.close(reopened_fd)
            if _file_identity(before) != _file_identity(reopened):
                raise OutboxError('saved profile changed while it was copied')

            relative_path = _snapshot_relative_path(namespace_key, sha256)
            final_path = self.root / relative_path
            published_created = self._publish_temporary(
                temporary_path, final_path, sha256, byte_count
            )
            temporary_path = None
            published_path = final_path
            token = uuid4().hex
            created_at = self._clock()
            created_text = _datetime_text(created_at)
            expires_text = _datetime_text(created_at + timedelta(seconds=_STAGING_SECONDS))
            source_modified_text = _datetime_text(source_modified_at)
            with self._transaction() as connection:
                namespace_id = self._namespace_id(connection, namespace, create=True)
                if namespace_id is None:
                    raise OutboxError('namespace was not persisted')
                connection.execute(
                    '''INSERT OR IGNORE INTO snapshots
                       (namespace_id, sha256, relative_path, byte_count, created_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (namespace_id, sha256, relative_path, byte_count, created_text),
                )
                row = connection.execute(
                    '''SELECT relative_path, byte_count FROM snapshots
                       WHERE namespace_id = ? AND sha256 = ?''',
                    (namespace_id, sha256),
                ).fetchone()
                if (
                    row is None
                    or row['relative_path'] != relative_path
                    or row['byte_count'] != byte_count
                ):
                    raise OutboxError('snapshot index conflicts with generated content')
                connection.execute(
                    '''INSERT INTO snapshot_staging
                       (token, namespace_id, sha256, relative_path, byte_count,
                        source_modified_at, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        token,
                        namespace_id,
                        sha256,
                        relative_path,
                        byte_count,
                        source_modified_text,
                        created_text,
                        expires_text,
                    ),
                )
            return Snapshot(
                namespace=namespace,
                sha256=sha256,
                relative_path=relative_path,
                absolute_path=final_path,
                byte_count=byte_count,
                source_modified_at=source_modified_at,
                staging_token=token,
            )
        except BaseException:
            if published_created and published_path is not None:
                self._discard_unowned_publication(namespace, published_path)
            raise
        finally:
            os.close(source_fd)
            if temporary_path is not None:
                try:
                    self._discard_temporary(temporary_path)
                except OSError:
                    pass

    def discard_staged_snapshot(self, snapshot: Snapshot) -> None:
        try:
            namespace_key = _namespace_key(snapshot.namespace)
            sha256 = _stored_sha256(snapshot.sha256)
            relative_path = _snapshot_relative_path(namespace_key, sha256)
            if (
                snapshot.relative_path != relative_path
                or snapshot.absolute_path != self.root / relative_path
                or snapshot.byte_count < 1
                or snapshot.byte_count > MAX_PROFILE_BYTES
            ):
                raise OutboxError(_STAGE_TOKEN_ERROR)
            _stored_job_id(snapshot.staging_token)
            path_to_unlink: str | None = None
            with self._filesystem_lock():
                with self._transaction() as connection:
                    namespace_id = self._namespace_id(
                        connection, snapshot.namespace, create=False
                    )
                    if namespace_id is None:
                        return
                    row = connection.execute(
                        '''SELECT * FROM snapshot_staging
                           WHERE token = ? AND namespace_id = ?''',
                        (snapshot.staging_token, namespace_id),
                    ).fetchone()
                    if row is None:
                        return
                    if not self._stage_matches_snapshot(row, snapshot):
                        raise OutboxError(_STAGE_TOKEN_ERROR)
                    consumed = connection.execute(
                        '''DELETE FROM snapshot_staging
                           WHERE token = ? AND namespace_id = ?''',
                        (snapshot.staging_token, namespace_id),
                    )
                    if consumed.rowcount != 1:
                        raise OutboxError(_STAGE_TOKEN_ERROR)
                    path_to_unlink = self._release_snapshot_if_unreferenced(
                        connection, namespace_id, sha256
                    )
                if path_to_unlink is not None:
                    self._unlink_generated_snapshot(path_to_unlink)
        except OutboxError:
            raise
        except (OSError, sqlite3.Error):
            raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

    def enqueue(
        self,
        namespace: Namespace,
        snapshot: Snapshot,
        roast_uuid: UUID,
        metadata: ProjectedMetadata,
        client_uuid: UUID,
    ) -> EnqueueResult:
        try:
            with self._filesystem_lock():
                return self._enqueue_locked(namespace, snapshot, roast_uuid, metadata, client_uuid)
        except OutboxError:
            raise
        except (OSError, sqlite3.Error):
            raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

    def _enqueue_locked(
        self,
        namespace: Namespace,
        snapshot: Snapshot,
        roast_uuid: UUID,
        metadata: ProjectedMetadata,
        client_uuid: UUID,
    ) -> EnqueueResult:
        namespace_key = _namespace_key(namespace)
        aroast_json = _metadata_text(metadata.aroast_json, 'aroast')
        revision_json = _metadata_text(metadata.revision_json, 'revision')
        roast_hex = _uuid_hex(roast_uuid, 'roast UUID')
        client_hex = _uuid_hex(client_uuid, 'client UUID')
        now_text = _datetime_text(self._clock())
        idempotency_key = f'archive-v1:{client_hex}:{roast_hex}:{snapshot.sha256}'
        path_to_unlink: str | None = None
        with self._transaction() as connection:
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                raise OutboxError(_STAGE_TOKEN_ERROR)
            stage = connection.execute(
                '''SELECT * FROM snapshot_staging
                   WHERE token = ? AND namespace_id = ? AND expires_at > ?''',
                (snapshot.staging_token, namespace_id, now_text),
            ).fetchone()
            if stage is None or not self._stage_matches_snapshot(stage, snapshot):
                raise OutboxError(_STAGE_TOKEN_ERROR)
            self._validate_snapshot(namespace, namespace_key, snapshot)
            consumed = connection.execute(
                '''DELETE FROM snapshot_staging
                   WHERE token = ? AND namespace_id = ? AND expires_at > ?''',
                (snapshot.staging_token, namespace_id, now_text),
            )
            if consumed.rowcount != 1:
                raise OutboxError(_STAGE_TOKEN_ERROR)
            existing = connection.execute(
                '''SELECT * FROM jobs
                   WHERE namespace_id = ? AND roast_uuid = ? AND content_sha256 = ?''',
                (namespace_id, roast_hex, snapshot.sha256),
            ).fetchone()
            if existing is not None:
                if existing['state'] == 'complete':
                    path_to_unlink = self._release_snapshot_if_unreferenced(
                        connection, namespace_id, snapshot.sha256
                    )
                result = EnqueueResult(self._row_to_job(existing, namespace), False)
            else:
                job_id = uuid4().hex
                connection.execute(
                    '''INSERT INTO jobs
                       (id, namespace_id, roast_uuid, content_sha256,
                        snapshot_sha256, snapshot_relative_path, snapshot_byte_count,
                        aroast_json, revision_json, idempotency_key, state,
                        attempts, next_attempt_at, lease_expires_at, lease_token,
                        error_code, error_message, created_at, updated_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                               0, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL)''',
                    (
                        job_id,
                        namespace_id,
                        roast_hex,
                        snapshot.sha256,
                        snapshot.sha256,
                        snapshot.relative_path,
                        snapshot.byte_count,
                        aroast_json,
                        revision_json,
                        idempotency_key,
                        now_text,
                        now_text,
                    ),
                )
                row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
                if row is None:
                    raise OutboxError('new outbox job was not persisted')
                result = EnqueueResult(self._row_to_job(row, namespace), True)
        if path_to_unlink is not None:
            self._unlink_generated_snapshot(path_to_unlink)
        return result

    @staticmethod
    def _stage_matches_snapshot(row: sqlite3.Row, snapshot: Snapshot) -> bool:
        try:
            return (
                _stored_job_id(row['token']) == snapshot.staging_token
                and _stored_sha256(row['sha256']) == snapshot.sha256
                and row['relative_path'] == snapshot.relative_path
                and _stored_positive_int(
                    row['byte_count'], 'staged snapshot byte count', MAX_PROFILE_BYTES
                )
                == snapshot.byte_count
                and _stored_datetime(row['source_modified_at']) == snapshot.source_modified_at
            )
        except OutboxError:
            return False

    def lease_next(
        self, namespace: Namespace, now: datetime, lease_seconds: int = 60
    ) -> Job | None:
        with _storage_boundary(), self._filesystem_lock():
            return self._lease_next_locked(namespace, now, lease_seconds)

    def _lease_next_locked(
        self, namespace: Namespace, now: datetime, lease_seconds: int
    ) -> Job | None:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError(
                f'lease_seconds must be an integer between 1 and {_MAX_LEASE_SECONDS}'
            )
        now_text = _datetime_text(now)
        try:
            lease_expires_text = _datetime_text(now + timedelta(seconds=lease_seconds))
        except OverflowError as exc:
            raise ValueError('lease_seconds is out of range') from exc
        with self._transaction() as connection:
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return None
            row = connection.execute(
                '''SELECT * FROM jobs
                   WHERE namespace_id = ?
                     AND (state = 'pending'
                          OR (state = 'retry_wait' AND next_attempt_at <= ?))
                   ORDER BY created_at, rowid
                   LIMIT 1''',
                (namespace_id, now_text),
            ).fetchone()
            if row is None:
                return None
            candidate = self._row_to_job(row, namespace)
            if (
                candidate.snapshot_path is None
                or candidate.snapshot_sha256 is None
                or candidate.snapshot_byte_count is None
            ):
                raise OutboxError('queued snapshot is unavailable')
            lease_token = uuid4().hex
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = 'leased', attempts = attempts + 1,
                       next_attempt_at = NULL, lease_expires_at = ?, lease_token = ?,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE id = ? AND state IN ('pending', 'retry_wait')''',
                (lease_expires_text, lease_token, now_text, row['id']),
            )
            if cursor.rowcount != 1:
                raise OutboxError(_LEASE_LOST)
            leased = connection.execute('SELECT * FROM jobs WHERE id = ?', (row['id'],)).fetchone()
            if leased is None:
                raise OutboxError('leased outbox job disappeared')
            leased_job = self._row_to_job(leased, namespace)
            try:
                _verify_snapshot_content(
                    candidate.snapshot_path,
                    candidate.snapshot_sha256,
                    candidate.snapshot_byte_count,
                    require_private=True,
                )
            except OutboxError:
                failure = PublicFailure(
                    kind=FailureKind.LOCAL_PROFILE,
                    code=FailureKind.LOCAL_PROFILE.value,
                    message=FAILURE_MESSAGES[FailureKind.LOCAL_PROFILE],
                    retryable=False,
                )
                error_code, error_message = _failure_fields(failure)
                failed = connection.execute(
                    '''UPDATE jobs
                       SET state = 'failed', next_attempt_at = NULL,
                           lease_expires_at = NULL, lease_token = NULL,
                           error_code = ?, error_message = ?, updated_at = ?
                       WHERE id = ? AND state = 'leased' AND lease_token = ?
                         AND lease_expires_at > ?''',
                    (
                        error_code,
                        error_message,
                        now_text,
                        row['id'],
                        lease_token,
                        now_text,
                    ),
                )
                if failed.rowcount != 1:
                    raise OutboxError(_LEASE_LOST) from None
                return LeaseFailure(leased_job, failure)
            return leased_job

    def next_due_at(self, namespace: Namespace) -> datetime | None:
        with _storage_boundary(), self._lock:
            connection = self._require_connection()
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return None
            row = connection.execute(
                '''SELECT CASE state
                         WHEN 'pending' THEN created_at
                         WHEN 'retry_wait' THEN next_attempt_at
                         ELSE lease_expires_at
                       END AS due_at
                   FROM jobs
                   WHERE namespace_id = ?
                     AND state IN ('pending', 'retry_wait', 'leased')
                   ORDER BY due_at, created_at, rowid
                   LIMIT 1''',
                (namespace_id,),
            ).fetchone()
            if row is None:
                return None
            return _stored_datetime(row['due_at'])

    def mark_complete(self, job_id: str, lease_token: str, now: datetime) -> None:
        try:
            with self._filesystem_lock():
                self._mark_complete_locked(job_id, lease_token, now)
        except OutboxError:
            raise
        except (OSError, sqlite3.Error):
            raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

    def _mark_complete_locked(self, job_id: str, lease_token: str, now: datetime) -> None:
        job_hex = _job_id(job_id)
        token_hex = _lease_token(lease_token)
        now_text = _datetime_text(now)
        path_to_unlink: str | None = None
        with self._transaction() as connection:
            row = self._leased_job_for_transition(connection, job_hex, token_hex, now_text)
            snapshot_sha256 = _stored_sha256(row['snapshot_sha256'])
            cursor = connection.execute(
                '''UPDATE jobs
                   SET snapshot_sha256 = NULL, snapshot_relative_path = NULL,
                       snapshot_byte_count = NULL, state = 'complete',
                       next_attempt_at = NULL, lease_expires_at = NULL, lease_token = NULL,
                       error_code = NULL, error_message = NULL,
                       updated_at = ?, completed_at = ?
                   WHERE id = ? AND state = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?''',
                (now_text, now_text, job_hex, token_hex, now_text),
            )
            if cursor.rowcount != 1:
                raise OutboxError(_LEASE_LOST)
            path_to_unlink = self._release_snapshot_if_unreferenced(
                connection, cast(int, row['namespace_id']), snapshot_sha256
            )
        if path_to_unlink is not None:
            self._unlink_generated_snapshot(path_to_unlink)

    def mark_retry(
        self,
        job_id: str,
        lease_token: str,
        now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> None:
        with _storage_boundary(), self._filesystem_lock():
            self._mark_retry_locked(job_id, lease_token, now, next_attempt_at, failure)

    def _mark_retry_locked(
        self,
        job_id: str,
        lease_token: str,
        now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> None:
        job_hex = _job_id(job_id)
        token_hex = _lease_token(lease_token)
        now_text = _datetime_text(now)
        next_attempt_text = _datetime_text(next_attempt_at)
        if next_attempt_at.astimezone(UTC) < now.astimezone(UTC):
            raise ValueError('next attempt cannot be before now')
        error_code, error_message = _failure_fields(failure)
        with self._transaction() as connection:
            self._leased_job_for_transition(connection, job_hex, token_hex, now_text)
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = 'retry_wait', next_attempt_at = ?,
                       lease_expires_at = NULL, lease_token = NULL, error_code = ?,
                       error_message = ?, updated_at = ?
                   WHERE id = ? AND state = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?''',
                (
                    next_attempt_text,
                    error_code,
                    error_message,
                    now_text,
                    job_hex,
                    token_hex,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise OutboxError(_LEASE_LOST)

    def mark_failed(
        self,
        job_id: str,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
    ) -> None:
        with _storage_boundary(), self._filesystem_lock():
            self._mark_failed_locked(job_id, lease_token, now, failure)

    def _mark_failed_locked(
        self,
        job_id: str,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
    ) -> None:
        job_hex = _job_id(job_id)
        token_hex = _lease_token(lease_token)
        now_text = _datetime_text(now)
        error_code, error_message = _failure_fields(failure)
        with self._transaction() as connection:
            self._leased_job_for_transition(connection, job_hex, token_hex, now_text)
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = 'failed', next_attempt_at = NULL,
                       lease_expires_at = NULL, lease_token = NULL, error_code = ?,
                       error_message = ?, updated_at = ?
                   WHERE id = ? AND state = 'leased' AND lease_token = ?
                     AND lease_expires_at > ?''',
                (error_code, error_message, now_text, job_hex, token_hex, now_text),
            )
            if cursor.rowcount != 1:
                raise OutboxError(_LEASE_LOST)

    def pause_namespace(self, namespace: Namespace, now: datetime, code: str) -> int:
        with _storage_boundary(), self._filesystem_lock():
            return self._pause_namespace_locked(namespace, now, code)

    def _pause_namespace_locked(
        self, namespace: Namespace, now: datetime, code: str
    ) -> int:
        now_text = _datetime_text(now)
        bounded_code = _pause_code(code)
        with self._transaction() as connection:
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return 0
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = 'paused', lease_expires_at = NULL, lease_token = NULL,
                       error_code = ?, error_message = NULL, updated_at = ?
                   WHERE namespace_id = ?
                     AND state IN ('pending', 'leased', 'retry_wait')''',
                (bounded_code, now_text, namespace_id),
            )
            return cursor.rowcount

    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
        with _storage_boundary(), self._filesystem_lock():
            return self._resume_namespace_locked(namespace, now)

    def _resume_namespace_locked(self, namespace: Namespace, now: datetime) -> int:
        now_text = _datetime_text(now)
        with self._transaction() as connection:
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return 0
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = CASE
                         WHEN next_attempt_at IS NOT NULL AND next_attempt_at > ?
                           THEN 'retry_wait'
                         ELSE 'pending'
                       END,
                       error_code = CASE
                         WHEN next_attempt_at IS NOT NULL AND next_attempt_at > ?
                           THEN 'offline'
                         ELSE NULL
                       END,
                       error_message = CASE
                         WHEN next_attempt_at IS NOT NULL AND next_attempt_at > ?
                           THEN ?
                         ELSE NULL
                       END,
                       updated_at = ?
                   WHERE namespace_id = ? AND state = 'paused' ''',
                (
                    now_text,
                    now_text,
                    now_text,
                    FAILURE_MESSAGES[FailureKind.OFFLINE],
                    now_text,
                    namespace_id,
                ),
            )
            return cursor.rowcount

    def retry_now(self, job_id: str, now: datetime) -> None:
        with _storage_boundary(), self._filesystem_lock():
            self._retry_now_locked(job_id, now)

    def _retry_now_locked(self, job_id: str, now: datetime) -> None:
        job_hex = _job_id(job_id)
        now_text = _datetime_text(now)
        with self._transaction() as connection:
            row = self._job_for_transition(connection, job_hex)
            if row['state'] == 'pending':
                return
            self._require_state(row, 'retry_wait', 'paused', 'failed')
            connection.execute(
                '''UPDATE jobs
                   SET state = 'pending', next_attempt_at = NULL,
                       lease_expires_at = NULL, lease_token = NULL, error_code = NULL,
                       error_message = NULL, updated_at = ?
                   WHERE id = ?''',
                (now_text, job_hex),
            )

    def remove(self, job_id: str) -> None:
        try:
            with self._filesystem_lock():
                self._remove_locked(job_id)
        except OutboxError:
            raise
        except (OSError, sqlite3.Error):
            raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

    def _remove_locked(self, job_id: str) -> None:
        job_hex = _job_id(job_id)
        path_to_unlink: str | None = None
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_hex,)).fetchone()
            if row is None:
                return
            snapshot_sha256 = cast(str | None, row['snapshot_sha256'])
            namespace_id = cast(int, row['namespace_id'])
            connection.execute('DELETE FROM jobs WHERE id = ?', (job_hex,))
            if snapshot_sha256 is not None:
                path_to_unlink = self._release_snapshot_if_unreferenced(
                    connection, namespace_id, _stored_sha256(snapshot_sha256)
                )
        if path_to_unlink is not None:
            self._unlink_generated_snapshot(path_to_unlink)

    def counts(self, namespace: Namespace) -> QueueCounts:
        with _storage_boundary(), self._lock:
            connection = self._require_connection()
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return QueueCounts(0, 0, 0, 0, 0)
            row = connection.execute(
                '''SELECT
                     COALESCE(SUM(state IN ('pending', 'leased')), 0) AS pending,
                     COALESCE(SUM(state = 'retry_wait'), 0) AS retrying,
                     COALESCE(SUM(state = 'paused'), 0) AS paused,
                     COALESCE(SUM(state = 'failed'), 0) AS failed,
                     COALESCE(SUM(state = 'complete'), 0) AS complete
                   FROM jobs WHERE namespace_id = ?''',
                (namespace_id,),
            ).fetchone()
            if row is None:
                raise OutboxError('outbox count query failed')
            return QueueCounts(
                pending=cast(int, row['pending']),
                retrying=cast(int, row['retrying']),
                paused=cast(int, row['paused']),
                failed=cast(int, row['failed']),
                complete=cast(int, row['complete']),
            )

    def failed_jobs(self, namespace: Namespace) -> tuple[FailedJob, ...]:
        with _storage_boundary(), self._lock:
            connection = self._require_connection()
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return ()
            rows = connection.execute(
                '''SELECT * FROM jobs
                   WHERE namespace_id = ? AND state = 'failed'
                   ORDER BY updated_at DESC, id''',
                (namespace_id,),
            ).fetchall()
            failed: list[FailedJob] = []
            for row in rows:
                job = self._row_to_job(row, namespace)
                if job.error_code is None or job.error_message is None:
                    raise OutboxError('failed outbox job has incomplete details')
                failed.append(
                    FailedJob(
                        id=job.id,
                        roast_uuid=job.roast_uuid,
                        sha256=job.content_sha256,
                        attempts=job.attempts,
                        next_attempt_at=job.next_attempt_at,
                        error_code=job.error_code,
                        error_message=job.error_message,
                        updated_at=job.updated_at,
                    )
                )
            return tuple(failed)

    def protected_paths(self, namespace: Namespace) -> frozenset[Path]:
        try:
            with self._filesystem_lock():
                return self._protected_paths_locked(namespace)
        except OutboxError:
            raise
        except (OSError, sqlite3.Error):
            raise OutboxError(_OUTBOX_STORAGE_ERROR) from None

    def _protected_paths_locked(self, namespace: Namespace) -> frozenset[Path]:
        namespace_key = _namespace_key(namespace)
        with self._lock:
            connection = self._require_connection()
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return frozenset()
            rows = connection.execute(
                '''SELECT DISTINCT snapshot_sha256, snapshot_relative_path,
                                  snapshot_byte_count
                   FROM jobs
                   WHERE namespace_id = ? AND snapshot_sha256 IS NOT NULL''',
                (namespace_id,),
            ).fetchall()
            paths: set[Path] = set()
            for row in rows:
                sha256 = _stored_sha256(row['snapshot_sha256'])
                relative_path = _stored_snapshot_path(
                    row['snapshot_relative_path'], namespace_key, sha256
                )
                byte_count = _stored_positive_int(
                    row['snapshot_byte_count'], 'snapshot byte count', MAX_PROFILE_BYTES
                )
                path = self.root / relative_path
                _verify_snapshot_content(path, sha256, byte_count, require_private=True)
                paths.add(path)
            return frozenset(paths)

    def _prepare_root(self) -> None:
        creation_observed = not os.path.lexists(self.root)
        if creation_observed:
            try:
                self.root.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                pass
        root_stat = os.lstat(self.root)
        if stat.S_ISLNK(root_stat.st_mode) or _path_is_junction(self.root):
            raise OutboxError('connector root must not be a symlink or reparse point')
        if not stat.S_ISDIR(root_stat.st_mode):
            raise OutboxError('connector root must be a directory')
        _set_private_permissions(self.root, 0o700)
        if creation_observed:
            _fsync_directory(self.root.parent)

    def _secure_database_files_before_connect(self) -> None:
        self._ensure_private_database_file(self._database_path, create=True)
        for suffix in ('-wal', '-shm'):
            path = Path(f'{self._database_path}{suffix}')
            if os.path.lexists(path):
                self._ensure_private_database_file(path, create=False)

    def _ensure_private_database_file(self, path: Path, *, create: bool) -> None:
        flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        created = False
        if create and not os.path.lexists(path):
            flags |= os.O_CREAT | os.O_EXCL
            created = True
        try:
            if _IS_WINDOWS:
                if created:
                    descriptor = os.open(path, flags, 0o600)
                else:
                    descriptor = _open_path_readonly(path)
            else:
                root_fd = self._open_generated_directory(self.root)
                try:
                    descriptor = os.open(path.name, flags, 0o600, dir_fd=root_fd)
                finally:
                    os.close(root_fd)
        except FileExistsError:
            self._ensure_private_database_file(path, create=False)
            return
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OutboxError('SQLite files must not be symlinks') from None
            raise
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OutboxError('SQLite files must be regular files')
        finally:
            os.close(descriptor)
        _set_private_permissions(path, 0o600)
        if created:
            _fsync_directory(self.root)

    def _migrate(self) -> None:
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
                    raise OutboxError('outbox schema is unversioned')
                for statement in _SCHEMA_V2_STATEMENTS:
                    connection.execute(statement)
                connection.execute('INSERT INTO schema_version(version) VALUES (2)')
            else:
                versions = connection.execute('SELECT version FROM schema_version').fetchall()
                if len(versions) != 1 or type(versions[0]['version']) is not int:
                    raise OutboxError('unsupported outbox schema version')
                version = versions[0]['version']
                if version == 1:
                    self._validate_schema_fingerprint(_SCHEMA_V1_STATEMENTS)
                    self._validate_v1_rows()
                    self._migrate_v1_to_v2(connection)
                elif version == 2:
                    self._validate_schema_fingerprint(_SCHEMA_V2_STATEMENTS)
                else:
                    raise OutboxError('unsupported outbox schema version')
            self._validate_schema_fingerprint(_SCHEMA_V2_STATEMENTS)
            foreign_key_errors = connection.execute('PRAGMA foreign_key_check').fetchall()
            if foreign_key_errors:
                raise OutboxError('outbox schema foreign keys are invalid')
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, OutboxError):
                raise
            raise OutboxError('outbox schema migration failed') from None

    def _validate_schema_fingerprint(self, statements: tuple[str, ...]) -> None:
        try:
            canonical = sqlite3.connect(':memory:')
            try:
                for statement in statements:
                    canonical.execute(statement)
                expected = _schema_fingerprint(canonical)
            finally:
                canonical.close()
            actual = _schema_fingerprint(self._require_connection())
        except sqlite3.Error:
            raise OutboxError('outbox schema definition is invalid') from None
        if actual != expected:
            raise OutboxError('outbox schema fingerprint is invalid')

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        if _SCHEMA_V2_STATEMENTS != _CANONICAL_SCHEMA_V2_STATEMENTS:
            raise OutboxError('outbox schema migration failed')
        connection.execute('DROP INDEX jobs_ready_idx')
        connection.execute('ALTER TABLE jobs RENAME TO jobs_v1')
        connection.execute(_schema_statement(_SCHEMA_V2_STATEMENTS, 'TABLE', 'jobs'))
        connection.execute(
            '''INSERT INTO jobs
               (id, namespace_id, roast_uuid, content_sha256,
                snapshot_sha256, snapshot_relative_path, snapshot_byte_count,
                aroast_json, revision_json, idempotency_key, state, attempts,
                next_attempt_at, lease_expires_at, lease_token, error_code,
                error_message, created_at, updated_at, completed_at)
               SELECT id, namespace_id, roast_uuid, content_sha256,
                      snapshot_sha256, snapshot_relative_path, snapshot_byte_count,
                      aroast_json, revision_json, idempotency_key,
                      CASE WHEN state = 'leased' THEN 'pending' ELSE state END,
                      attempts,
                      CASE WHEN state = 'leased' THEN NULL ELSE next_attempt_at END,
                      NULL, NULL,
                      CASE WHEN state = 'leased' THEN NULL ELSE error_code END,
                      CASE WHEN state = 'leased' THEN NULL ELSE error_message END,
                      created_at, updated_at, completed_at
               FROM jobs_v1'''
        )
        connection.execute('DROP TABLE jobs_v1')
        connection.execute(_schema_statement(_SCHEMA_V2_STATEMENTS, 'INDEX', 'jobs_ready_idx'))
        connection.execute('ALTER TABLE schema_version RENAME TO schema_version_v1')
        connection.execute(_schema_statement(_SCHEMA_V2_STATEMENTS, 'TABLE', 'schema_version'))
        connection.execute('INSERT INTO schema_version(version) VALUES (2)')
        connection.execute('DROP TABLE schema_version_v1')
        connection.execute(_schema_statement(_SCHEMA_V2_STATEMENTS, 'TABLE', 'snapshot_staging'))
        connection.execute(
            _schema_statement(_SCHEMA_V2_STATEMENTS, 'INDEX', 'snapshot_staging_expiry_idx')
        )

    def _validate_v1_rows(self) -> None:
        connection = self._require_connection()
        namespaces = self._stored_namespaces(connection)
        snapshots = self._validate_snapshot_rows(connection, namespaces)
        for row in connection.execute('SELECT * FROM jobs').fetchall():
            namespace_id = _stored_positive_int(row['namespace_id'], 'namespace id', 2**63 - 1)
            namespace = namespaces.get(namespace_id)
            if namespace is None:
                raise OutboxError('stored namespace reference is invalid')
            self._validate_job_row(row, namespace, schema_version=1)
            self._validate_job_snapshot_owner(row, namespace_id, snapshots)

    def _validate_durable_rows(self) -> None:
        connection = self._require_connection()
        namespaces = self._stored_namespaces(connection)
        snapshots = self._validate_snapshot_rows(connection, namespaces)
        for row in connection.execute('SELECT * FROM snapshot_staging').fetchall():
            token = _stored_job_id(row['token'])
            namespace_id = _stored_positive_int(row['namespace_id'], 'namespace id', 2**63 - 1)
            namespace = namespaces.get(namespace_id)
            sha256 = _stored_sha256(row['sha256'])
            snapshot = snapshots.get((namespace_id, sha256))
            if namespace is None or snapshot is None:
                raise OutboxError('stored stage reference is invalid')
            relative_path = _stored_snapshot_path(
                row['relative_path'], _namespace_key(namespace), sha256
            )
            byte_count = _stored_positive_int(
                row['byte_count'], 'staged snapshot byte count', MAX_PROFILE_BYTES
            )
            if token == '' or (relative_path, byte_count) != snapshot:
                raise OutboxError('stored stage snapshot ownership is invalid')
            _stored_datetime(row['source_modified_at'])
            created = _stored_datetime(row['created_at'])
            expires = _stored_datetime(row['expires_at'])
            if expires <= created:
                raise OutboxError('stored stage expiry is invalid')
        for row in connection.execute('SELECT * FROM jobs').fetchall():
            namespace_id = _stored_positive_int(row['namespace_id'], 'namespace id', 2**63 - 1)
            namespace = namespaces.get(namespace_id)
            if namespace is None:
                raise OutboxError('stored namespace reference is invalid')
            self._validate_job_row(row, namespace, schema_version=2)
            self._validate_job_snapshot_owner(row, namespace_id, snapshots)

    def _stored_namespaces(self, connection: sqlite3.Connection) -> dict[int, Namespace]:
        namespaces: dict[int, Namespace] = {}
        for row in connection.execute('SELECT * FROM namespaces').fetchall():
            namespace_id = _stored_positive_int(row['id'], 'namespace id', 2**63 - 1)
            origin = _stored_text(row['origin'], 'namespace origin')
            organization = _stored_uuid(row['organization_uuid'])
            key = _stored_sha256(row['namespace_key'])
            namespace = Namespace(origin, organization, f'namespace-sha256:{key}')
            if _namespace_key(namespace) != key or namespace_id in namespaces:
                raise OutboxError('stored namespace is invalid')
            namespaces[namespace_id] = namespace
        return namespaces

    def _validate_snapshot_rows(
        self, connection: sqlite3.Connection, namespaces: dict[int, Namespace]
    ) -> dict[tuple[int, str], tuple[str, int]]:
        snapshots: dict[tuple[int, str], tuple[str, int]] = {}
        for row in connection.execute('SELECT * FROM snapshots').fetchall():
            namespace_id = _stored_positive_int(row['namespace_id'], 'namespace id', 2**63 - 1)
            namespace = namespaces.get(namespace_id)
            sha256 = _stored_sha256(row['sha256'])
            if namespace is None:
                raise OutboxError('stored snapshot namespace is invalid')
            relative_path = _stored_snapshot_path(
                row['relative_path'], _namespace_key(namespace), sha256
            )
            byte_count = _stored_positive_int(
                row['byte_count'], 'snapshot byte count', MAX_PROFILE_BYTES
            )
            _stored_datetime(row['created_at'])
            key = (namespace_id, sha256)
            if key in snapshots:
                raise OutboxError('stored snapshot is duplicated')
            snapshots[key] = (relative_path, byte_count)
        return snapshots

    @staticmethod
    def _validate_job_snapshot_owner(
        row: sqlite3.Row,
        namespace_id: int,
        snapshots: dict[tuple[int, str], tuple[str, int]],
    ) -> None:
        if row['snapshot_sha256'] is None:
            return
        sha256 = _stored_sha256(row['snapshot_sha256'])
        snapshot = snapshots.get((namespace_id, sha256))
        if snapshot is None:
            raise OutboxError('stored snapshot reference is invalid')
        relative_path = row['snapshot_relative_path']
        byte_count = _stored_positive_int(
            row['snapshot_byte_count'], 'snapshot byte count', MAX_PROFILE_BYTES
        )
        if (relative_path, byte_count) != snapshot:
            raise OutboxError('stored job snapshot ownership is invalid')

    def _validate_job_row(
        self, row: sqlite3.Row, namespace: Namespace, *, schema_version: int
    ) -> None:
        state = row['state']
        if not isinstance(state, str) or state not in _JOB_STATES:
            raise OutboxError('stored outbox state is invalid')
        snapshot_values = (
            row['snapshot_sha256'],
            row['snapshot_relative_path'],
            row['snapshot_byte_count'],
        )
        if state == 'complete':
            if any(value is not None for value in snapshot_values) or row['completed_at'] is None:
                raise OutboxError('completed outbox ownership is invalid')
        elif any(value is None for value in snapshot_values) or row['completed_at'] is not None:
            raise OutboxError('active outbox ownership is invalid')
        lease_token = row['lease_token'] if schema_version == 2 else None
        if state == 'leased':
            if row['lease_expires_at'] is None or (schema_version == 2 and lease_token is None):
                raise OutboxError('stored lease is invalid')
        elif row['lease_expires_at'] is not None or lease_token is not None:
            raise OutboxError('stored lease is invalid')
        if state == 'retry_wait' and (
            row['next_attempt_at'] is None
            or row['error_code'] is None
            or row['error_message'] is None
        ):
            raise OutboxError('stored retry state is invalid')
        if state == 'failed' and (
            row['next_attempt_at'] is not None
            or row['error_code'] is None
            or row['error_message'] is None
        ):
            raise OutboxError('stored failed state is invalid')
        self._row_to_job(row, namespace, schema_version=schema_version)

    def _harden_indexed_snapshots(self) -> None:
        connection = self._require_connection()
        rows = connection.execute(
            '''SELECT n.namespace_key, s.sha256, s.relative_path, s.byte_count
               FROM snapshots AS s
               JOIN namespaces AS n ON n.id = s.namespace_id'''
        ).fetchall()
        for row in rows:
            namespace_key = _stored_sha256(row['namespace_key'])
            sha256 = _stored_sha256(row['sha256'])
            relative_path = _stored_snapshot_path(
                row['relative_path'], namespace_key, sha256
            )
            byte_count = _stored_positive_int(
                row['byte_count'], 'snapshot byte count', MAX_PROFILE_BYTES
            )
            path = self.root / relative_path
            _set_private_permissions(path, 0o400)
            _verify_snapshot_content(path, sha256, byte_count, require_private=True)

    def _expire_stages_and_collect(self, now: datetime) -> None:
        now_text = _datetime_text(now)
        paths_to_unlink: list[str] = []
        with self._transaction() as connection:
            connection.execute('DELETE FROM snapshot_staging WHERE expires_at <= ?', (now_text,))
            rows = connection.execute(
                '''SELECT s.namespace_id, s.sha256, s.relative_path
                   FROM snapshots AS s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM jobs AS j
                       WHERE j.namespace_id = s.namespace_id
                         AND j.snapshot_sha256 = s.sha256)
                     AND NOT EXISTS (
                       SELECT 1 FROM snapshot_staging AS st
                       WHERE st.namespace_id = s.namespace_id
                         AND st.sha256 = s.sha256
                         AND st.expires_at > ?)''',
                (now_text,),
            ).fetchall()
            for row in rows:
                namespace_row = connection.execute(
                    'SELECT namespace_key FROM namespaces WHERE id = ?',
                    (row['namespace_id'],),
                ).fetchone()
                if namespace_row is None:
                    raise OutboxError('stored snapshot namespace is invalid')
                path = _stored_snapshot_path(
                    row['relative_path'],
                    _stored_sha256(namespace_row['namespace_key']),
                    _stored_sha256(row['sha256']),
                )
                paths_to_unlink.append(path)
                connection.execute(
                    'DELETE FROM snapshots WHERE namespace_id = ? AND sha256 = ?',
                    (row['namespace_id'], row['sha256']),
                )
        for path in paths_to_unlink:
            self._unlink_generated_snapshot(path)
        self._collect_unindexed_files()

    def _collect_unindexed_files(self) -> None:
        connection = self._require_connection()
        rows = connection.execute(
            '''SELECT n.namespace_key, s.sha256, s.relative_path
               FROM snapshots AS s
               JOIN namespaces AS n ON n.id = s.namespace_id'''
        ).fetchall()
        indexed: set[str] = set()
        for row in rows:
            namespace_key = _stored_sha256(row['namespace_key'])
            sha256 = _stored_sha256(row['sha256'])
            indexed.add(_stored_snapshot_path(row['relative_path'], namespace_key, sha256))
        changed_directories: set[Path] = set()
        for namespace_entry in os.scandir(self.root):
            if not _SHA256_RE.fullmatch(namespace_entry.name):
                continue
            if _directory_entry_is_reparse(namespace_entry):
                raise OutboxError('generated namespace directory must not be a reparse point')
            if not namespace_entry.is_dir(follow_symlinks=False):
                continue
            namespace_path = Path(namespace_entry.path)
            self._harden_directory(namespace_path)
            snapshots_path = namespace_path / 'snapshots'
            if not os.path.lexists(snapshots_path):
                continue
            self._require_directory_path(snapshots_path)
            self._harden_directory(snapshots_path)
            for prefix_entry in os.scandir(snapshots_path):
                if _directory_entry_is_reparse(prefix_entry):
                    raise OutboxError('snapshot directory entries must not be reparse points')
                if (
                    prefix_entry.is_file(follow_symlinks=False)
                    and _TEMP_FILE_RE.fullmatch(prefix_entry.name) is not None
                ):
                    _secure_unlink(Path(prefix_entry.path))
                    changed_directories.add(snapshots_path)
                    continue
                if _PREFIX_RE.fullmatch(prefix_entry.name) is None:
                    continue
                if not prefix_entry.is_dir(follow_symlinks=False):
                    continue
                prefix_path = Path(prefix_entry.path)
                self._harden_directory(prefix_path)
                for file_entry in os.scandir(prefix_path):
                    if _directory_entry_is_reparse(file_entry):
                        raise OutboxError('snapshot files must not be reparse points')
                    relative = (
                        f'{namespace_entry.name}/snapshots/'
                        f'{prefix_entry.name}/{file_entry.name}'
                    )
                    snapshot_match = _SNAPSHOT_FILE_RE.fullmatch(file_entry.name)
                    is_generated = (
                        snapshot_match is not None
                        and snapshot_match.group(1).startswith(prefix_entry.name)
                    )
                    is_temporary = _TEMP_FILE_RE.fullmatch(file_entry.name) is not None
                    if not file_entry.is_file(follow_symlinks=False):
                        continue
                    if is_temporary or (is_generated and relative not in indexed):
                        _secure_unlink(Path(file_entry.path))
                        changed_directories.add(prefix_path)
        for directory in changed_directories:
            _fsync_directory(directory)

    def _namespace_id(
        self, connection: sqlite3.Connection, namespace: Namespace, *, create: bool
    ) -> int | None:
        namespace_key = _namespace_key(namespace)
        organization_uuid = _uuid_hex(namespace.organization_id, 'organization UUID')
        rows = connection.execute(
            '''SELECT id, origin, organization_uuid, namespace_key
               FROM namespaces
               WHERE (origin = ? AND organization_uuid = ?) OR namespace_key = ?''',
            (namespace.origin, organization_uuid, namespace_key),
        ).fetchall()
        if rows:
            if len(rows) != 1:
                raise OutboxError('namespace index is inconsistent')
            existing = rows[0]
            if (
                existing['origin'] != namespace.origin
                or existing['organization_uuid'] != organization_uuid
                or existing['namespace_key'] != namespace_key
            ):
                raise OutboxError('namespace identity conflicts with stored data')
            return _stored_positive_int(existing['id'], 'namespace id', 2**63 - 1)
        if not create:
            return None
        cursor = connection.execute(
            '''INSERT INTO namespaces(origin, organization_uuid, namespace_key)
               VALUES (?, ?, ?)''',
            (namespace.origin, organization_uuid, namespace_key),
        )
        return cast(int, cursor.lastrowid)

    def _snapshot_directory(self, namespace_key: str, sha256: str | None) -> Path:
        base = self.root / namespace_key / 'snapshots'
        return base if sha256 is None else base / sha256[:2]

    def _ensure_generated_directory(self, directory: Path) -> None:
        try:
            relative_parts = directory.relative_to(self.root).parts
        except ValueError as exc:
            raise OutboxError('generated directory escapes connector root') from exc
        current = self.root
        for part in relative_parts:
            if part in {'', '.', '..'} or '/' in part or '\\' in part:
                raise OutboxError('generated directory path is invalid')
            parent = current
            current /= part
            created = False
            try:
                os.mkdir(current, 0o700)
                created = True
            except FileExistsError:
                pass
            self._require_directory_path(current)
            self._harden_directory(current)
            if created:
                _fsync_directory(parent)

    def _write_bytes_to_temporary(
        self, content: bytes, directory: Path
    ) -> tuple[str, int, Path]:
        temporary_name = f'.snapshot-{uuid4().hex}.tmp'
        temporary_path = directory / temporary_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        directory_fd = self._open_generated_directory(directory)
        temporary_fd: int | None = None
        try:
            if _HAS_DIRECTORY_FDS:
                temporary_fd = os.open(
                    temporary_name, flags, 0o600, dir_fd=directory_fd
                )
            else:
                temporary_fd = os.open(temporary_path, flags, 0o600)
            _write_all(temporary_fd, content)
            _fsync_descriptor(temporary_fd)
        except BaseException:
            if temporary_fd is not None:
                os.close(temporary_fd)
                temporary_fd = None
            try:
                if _HAS_DIRECTORY_FDS:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                else:
                    _secure_unlink(temporary_path)
                _fsync_directory(directory)
            except FileNotFoundError:
                pass
            raise
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            os.close(directory_fd)
        return hashlib.sha256(content).hexdigest(), len(content), temporary_path

    def _copy_to_temporary(self, source_fd: int, directory: Path) -> tuple[str, int, Path]:
        temporary_name = f'.snapshot-{uuid4().hex}.tmp'
        temporary_path = directory / temporary_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        directory_fd = self._open_generated_directory(directory)
        temporary_fd: int | None = None
        try:
            if _HAS_DIRECTORY_FDS:
                temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
            else:
                temporary_fd = os.open(temporary_path, flags, 0o600)
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > MAX_PROFILE_BYTES:
                    raise OutboxError('saved profile size is outside the supported range')
                digest.update(chunk)
                _write_all(temporary_fd, chunk)
            if byte_count < 1:
                raise OutboxError('saved profile size is outside the supported range')
            _fsync_descriptor(temporary_fd)
        except BaseException:
            if temporary_fd is not None:
                os.close(temporary_fd)
                temporary_fd = None
            try:
                if _HAS_DIRECTORY_FDS:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                else:
                    _secure_unlink(temporary_path)
                _fsync_directory(directory)
            except FileNotFoundError:
                pass
            raise
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            os.close(directory_fd)
        return digest.hexdigest(), byte_count, temporary_path

    def _discard_temporary(self, temporary_path: Path) -> None:
        directory_fd = self._open_generated_directory(temporary_path.parent)
        try:
            if _HAS_DIRECTORY_FDS:
                os.unlink(temporary_path.name, dir_fd=directory_fd)
            else:
                _secure_unlink(temporary_path)
            _fsync_directory(temporary_path.parent)
        except FileNotFoundError:
            return
        finally:
            os.close(directory_fd)

    def _publish_temporary(
        self, temporary_path: Path, final_path: Path, sha256: str, byte_count: int
    ) -> bool:
        self._ensure_generated_directory(final_path.parent)
        try:
            temporary_path.relative_to(self.root)
            final_path.relative_to(self.root)
        except ValueError as exc:
            raise OutboxError('snapshot publication path is invalid') from exc
        source_directory_fd = self._open_generated_directory(temporary_path.parent)
        destination_directory_fd = self._open_generated_directory(final_path.parent)
        created = False
        try:
            try:
                if _HAS_DIRECTORY_FDS:
                    os.link(
                        temporary_path.name,
                        final_path.name,
                        src_dir_fd=source_directory_fd,
                        dst_dir_fd=destination_directory_fd,
                        follow_symlinks=False,
                    )
                elif _IS_WINDOWS:
                    _require_windows_native().publish(temporary_path, final_path)
                else:
                    os.link(temporary_path, final_path, follow_symlinks=False)
                created = True
            except FileExistsError:
                _verify_snapshot_content(
                    final_path, sha256, byte_count, require_private=True
                )
                self._discard_temporary(temporary_path)
                return False
            try:
                _set_private_permissions(final_path, 0o400)
                if not _IS_WINDOWS:
                    descriptor = _open_path_readonly(final_path)
                    try:
                        _fsync_descriptor(descriptor)
                    finally:
                        os.close(descriptor)
                    _fsync_directory(final_path.parent)
                    if _HAS_DIRECTORY_FDS:
                        os.unlink(temporary_path.name, dir_fd=source_directory_fd)
                    else:
                        _secure_unlink(temporary_path)
                    _fsync_directory(temporary_path.parent)
                return True
            except BaseException:
                if created:
                    try:
                        if _HAS_DIRECTORY_FDS:
                            os.unlink(final_path.name, dir_fd=destination_directory_fd)
                        else:
                            _secure_unlink(final_path)
                        _fsync_directory(final_path.parent)
                    except OSError:
                        pass
                raise
        finally:
            os.close(source_directory_fd)
            os.close(destination_directory_fd)

    def _validate_snapshot(
        self, namespace: Namespace, namespace_key: str, snapshot: Snapshot
    ) -> None:
        if snapshot.namespace != namespace:
            raise ValueError('snapshot namespace does not match enqueue namespace')
        sha256 = _stored_sha256(snapshot.sha256)
        expected_relative = _snapshot_relative_path(namespace_key, sha256)
        if snapshot.relative_path != expected_relative:
            raise OutboxError('snapshot path is invalid')
        expected_absolute = self.root / expected_relative
        if snapshot.absolute_path != expected_absolute:
            raise OutboxError('snapshot absolute path is invalid')
        if snapshot.byte_count < 1 or snapshot.byte_count > MAX_PROFILE_BYTES:
            raise OutboxError('snapshot size is invalid')
        _datetime_text(snapshot.source_modified_at)
        _stored_job_id(snapshot.staging_token)
        _verify_snapshot_content(
            expected_absolute, sha256, snapshot.byte_count, require_private=True
        )

    def _row_to_job(
        self, row: sqlite3.Row, namespace: Namespace, *, schema_version: int = 2
    ) -> Job:
        namespace_key = _namespace_key(namespace)
        state_value = row['state']
        if not isinstance(state_value, str) or state_value not in _JOB_STATES:
            raise OutboxError('stored outbox state is invalid')
        state = cast(
            Literal['pending', 'leased', 'retry_wait', 'paused', 'failed', 'complete'],
            state_value,
        )
        snapshot_values = (
            row['snapshot_sha256'],
            row['snapshot_relative_path'],
            row['snapshot_byte_count'],
        )
        snapshot_sha256: str | None
        snapshot_path: Path | None
        snapshot_byte_count: int | None
        if all(value is None for value in snapshot_values):
            if state != 'complete':
                raise OutboxError('active outbox job has no snapshot')
            snapshot_sha256 = None
            snapshot_path = None
            snapshot_byte_count = None
        elif any(value is None for value in snapshot_values):
            raise OutboxError('stored snapshot ownership is incomplete')
        else:
            if state == 'complete':
                raise OutboxError('completed outbox job still owns a snapshot')
            snapshot_sha256 = _stored_sha256(row['snapshot_sha256'])
            relative_path = _stored_snapshot_path(
                row['snapshot_relative_path'], namespace_key, snapshot_sha256
            )
            snapshot_path = self.root / relative_path
            snapshot_byte_count = _stored_positive_int(
                row['snapshot_byte_count'], 'snapshot byte count', MAX_PROFILE_BYTES
            )
        content_sha256 = _stored_sha256(row['content_sha256'])
        if snapshot_sha256 is not None and snapshot_sha256 != content_sha256:
            raise OutboxError('stored snapshot address is inconsistent')
        aroast_json = _stored_metadata_text(row['aroast_json'], 'aroast')
        revision_json = _stored_metadata_text(row['revision_json'], 'revision')
        roast_uuid = _stored_uuid(row['roast_uuid'])
        idempotency_key = _stored_text(row['idempotency_key'], 'idempotency key')
        expected_suffix = f':{roast_uuid.hex}:{content_sha256}'
        if (
            not idempotency_key.startswith('archive-v1:')
            or not idempotency_key.endswith(expected_suffix)
            or len(idempotency_key) != len('archive-v1:') + 32 + len(expected_suffix)
            or _UUID_HEX_RE.fullmatch(idempotency_key.split(':')[1]) is None
        ):
            raise OutboxError('stored idempotency key is invalid')
        lease_token_value = row['lease_token'] if schema_version == 2 else None
        lease_token = None if lease_token_value is None else _stored_job_id(lease_token_value)
        next_attempt_at = _optional_stored_datetime(row['next_attempt_at'])
        lease_expires_at = _optional_stored_datetime(row['lease_expires_at'])
        completed_at = _optional_stored_datetime(row['completed_at'])
        error_code = _optional_stored_failure_code(row['error_code'])
        error_message = _optional_stored_failure_message(row['error_message'])
        if state == 'leased':
            if lease_expires_at is None or (schema_version == 2 and lease_token is None):
                raise OutboxError('stored lease is invalid')
        elif lease_expires_at is not None or lease_token is not None:
            raise OutboxError('stored lease is invalid')
        if state == 'complete' and completed_at is None:
            raise OutboxError('stored completion is invalid')
        if state != 'complete' and completed_at is not None:
            raise OutboxError('stored completion is invalid')
        if state in {'pending', 'leased', 'complete'} and (
            next_attempt_at is not None or error_code is not None or error_message is not None
        ):
            raise OutboxError('stored outbox state details are invalid')
        if state == 'retry_wait' and (
            next_attempt_at is None or error_code is None or error_message is None
        ):
            raise OutboxError('stored retry state is invalid')
        if state == 'paused' and (
            error_code not in _PAUSE_CODES or error_message is not None
        ):
            raise OutboxError('stored pause state is invalid')
        if (
            state in {'retry_wait', 'failed'}
            and error_code is not None
            and error_message is not None
            and not _stored_failure_pair_is_valid(error_code, error_message)
        ):
            raise OutboxError('stored failure details are invalid')
        if state == 'failed' and (
            next_attempt_at is not None or error_code is None or error_message is None
        ):
            raise OutboxError('stored failed state is invalid')
        return Job(
            id=_stored_job_id(row['id']),
            namespace=namespace,
            roast_uuid=roast_uuid,
            content_sha256=content_sha256,
            snapshot_sha256=snapshot_sha256,
            snapshot_path=snapshot_path,
            snapshot_byte_count=snapshot_byte_count,
            aroast_json=aroast_json,
            revision_json=revision_json,
            idempotency_key=idempotency_key,
            state=state,
            attempts=_stored_nonnegative_int(row['attempts'], 'attempts'),
            next_attempt_at=next_attempt_at,
            lease_expires_at=lease_expires_at,
            lease_token=lease_token,
            error_code=error_code,
            error_message=error_message,
            created_at=_stored_datetime(row['created_at']),
            updated_at=_stored_datetime(row['updated_at']),
        )

    def _job_for_transition(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return cast(sqlite3.Row, row)

    def _leased_job_for_transition(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        lease_token: str,
        now_text: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            '''SELECT * FROM jobs
               WHERE id = ? AND state = 'leased' AND lease_token = ?
                 AND lease_expires_at > ?''',
            (job_id, lease_token, now_text),
        ).fetchone()
        if row is None:
            raise OutboxError(_LEASE_LOST)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_state(row: sqlite3.Row, *states: str) -> None:
        if row['state'] not in states:
            raise OutboxError('outbox job is not in a valid state for this transition')

    def _release_snapshot_if_unreferenced(
        self, connection: sqlite3.Connection, namespace_id: int, sha256: str
    ) -> str | None:
        references = connection.execute(
            '''SELECT count(*) FROM jobs
               WHERE namespace_id = ? AND snapshot_sha256 = ?''',
            (namespace_id, sha256),
        ).fetchone()[0]
        stages = connection.execute(
            '''SELECT count(*) FROM snapshot_staging
               WHERE namespace_id = ? AND sha256 = ? AND expires_at > ?''',
            (namespace_id, sha256, _datetime_text(self._clock())),
        ).fetchone()[0]
        if references != 0 or stages != 0:
            return None
        row = connection.execute(
            '''SELECT n.namespace_key, s.relative_path
               FROM snapshots AS s
               JOIN namespaces AS n ON n.id = s.namespace_id
               WHERE s.namespace_id = ? AND s.sha256 = ?''',
            (namespace_id, sha256),
        ).fetchone()
        if row is None:
            return None
        relative_path = _stored_snapshot_path(row['relative_path'], row['namespace_key'], sha256)
        connection.execute(
            'DELETE FROM snapshots WHERE namespace_id = ? AND sha256 = ?',
            (namespace_id, sha256),
        )
        return relative_path

    def _discard_unowned_publication(self, namespace: Namespace, path: Path) -> None:
        try:
            relative_path = path.relative_to(self.root).as_posix()
            sha256 = path.stem
            with self._transaction() as connection:
                namespace_id = self._namespace_id(connection, namespace, create=False)
                if namespace_id is None:
                    should_unlink = True
                else:
                    jobs = connection.execute(
                        '''SELECT count(*) FROM jobs
                           WHERE namespace_id = ? AND snapshot_sha256 = ?''',
                        (namespace_id, sha256),
                    ).fetchone()[0]
                    stages = connection.execute(
                        '''SELECT count(*) FROM snapshot_staging
                           WHERE namespace_id = ? AND sha256 = ?''',
                        (namespace_id, sha256),
                    ).fetchone()[0]
                    should_unlink = jobs == 0 and stages == 0
                    if should_unlink:
                        connection.execute(
                            'DELETE FROM snapshots WHERE namespace_id = ? AND sha256 = ?',
                            (namespace_id, sha256),
                        )
            if should_unlink:
                self._unlink_generated_snapshot(relative_path)
        except (OSError, OutboxError, sqlite3.Error):
            pass

    def _unlink_generated_snapshot(self, relative_path: str) -> None:
        parts = PurePosixPath(relative_path).parts
        if len(parts) != 4:
            raise OutboxError('stored snapshot path is invalid')
        namespace_key, snapshots, prefix, filename = parts
        match = _SNAPSHOT_FILE_RE.fullmatch(filename)
        if (
            _SHA256_RE.fullmatch(namespace_key) is None
            or snapshots != 'snapshots'
            or _PREFIX_RE.fullmatch(prefix) is None
            or match is None
            or not match.group(1).startswith(prefix)
        ):
            raise OutboxError('stored snapshot path is invalid')
        path = self.root / relative_path
        directory_fd = self._open_generated_directory(path.parent)
        try:
            try:
                if _HAS_DIRECTORY_FDS:
                    path_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                else:
                    descriptor = _open_path_readonly(path)
                    try:
                        path_stat = os.fstat(descriptor)
                    finally:
                        os.close(descriptor)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(path_stat.st_mode):
                raise OutboxError('snapshot path must not be a symlink')
            if not stat.S_ISREG(path_stat.st_mode):
                raise OutboxError('snapshot must be a regular file')
            if _HAS_DIRECTORY_FDS:
                os.unlink(path.name, dir_fd=directory_fd)
            else:
                _secure_unlink(path)
            _fsync_directory(path.parent)
        finally:
            os.close(directory_fd)

    def _open_generated_directory(self, directory: Path) -> int:
        try:
            relative_parts = directory.relative_to(self.root).parts
        except ValueError as exc:
            raise OutboxError('generated directory escapes connector root') from exc
        if _IS_WINDOWS:
            native = _require_windows_native()
            try:
                return native.open_readonly(directory, directory=True)
            except OSError:
                raise OutboxError('generated directory contains a reparse point') from None
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if not _HAS_DIRECTORY_FDS:
            self._require_directory_path(directory)
            return os.open(directory, flags)
        directory_fd = os.open(self.root, flags)
        try:
            for component in relative_parts:
                if component in {'', '.', '..'} or '/' in component or '\\' in component:
                    raise OutboxError('generated directory path is invalid')
                next_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    def _require_directory_path(self, path: Path) -> None:
        if _IS_WINDOWS:
            descriptor = _require_windows_native().open_readonly(path, directory=True)
            os.close(descriptor)
            return
        path_stat = os.lstat(path)
        if stat.S_ISLNK(path_stat.st_mode):
            raise OutboxError('generated directory must not be a symlink')
        if not stat.S_ISDIR(path_stat.st_mode):
            raise OutboxError('generated path must be a directory')

    @staticmethod
    def _harden_directory(path: Path) -> None:
        _set_private_permissions(path, 0o700)

    def _harden_database_files(self) -> None:
        for suffix in ('', '-wal', '-shm'):
            path = Path(f'{self._database_path}{suffix}')
            if not os.path.lexists(path):
                continue
            self._ensure_private_database_file(path, create=False)

    @contextmanager
    def _filesystem_lock(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.root / '.outbox.lock'
            try:
                if _IS_WINDOWS:
                    descriptor = _require_windows_native().open_lock(lock_path)
                else:
                    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0)
                    flags |= getattr(os, 'O_NOFOLLOW', 0)
                    descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise OutboxError('outbox lock must not be a symlink') from None
                raise OutboxError('outbox process lock is unavailable') from None
            try:
                descriptor_stat = os.fstat(descriptor)
                if not stat.S_ISREG(descriptor_stat.st_mode):
                    raise OutboxError('outbox lock must be a regular private file')
                _set_private_permissions(lock_path, 0o600)
                _acquire_file_lock(descriptor)
                try:
                    yield
                finally:
                    _release_file_lock(descriptor)
            finally:
                os.close(descriptor)

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

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise OutboxError('outbox is not open')
        return self._connection


def _schema_statement(statements: tuple[str, ...], kind: str, name: str) -> str:
    prefix = f'CREATE {kind} {name}'
    matches = [statement for statement in statements if statement.startswith(prefix)]
    if len(matches) != 1:
        raise OutboxError('outbox schema definition is invalid')
    return matches[0]


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
        index_rows = tuple(
            tuple(row)
            for row in connection.execute(f'PRAGMA index_list({quoted_table})').fetchall()
        )
        indexes: list[_SchemaIndexFingerprint] = []
        for index in index_rows:
            if len(index) != 5 or not isinstance(index[1], str):
                raise sqlite3.DatabaseError('invalid schema index metadata')
            quoted_index = _quote_sqlite_identifier(index[1])
            index_info = tuple(
                tuple(row)
                for row in connection.execute(
                    f'PRAGMA index_info({quoted_index})'
                ).fetchall()
            )
            indexes.append((index, index_info))
        pragma_fingerprints.append(
            (table, columns, foreign_keys, tuple(indexes))
        )
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


def _namespace_key(namespace: Namespace) -> str:
    if namespace.origin == '' or _has_control_character(namespace.origin):
        raise ValueError('namespace origin is invalid')
    match = _NAMESPACE_KEY_RE.fullmatch(namespace.key)
    if match is None:
        raise ValueError('namespace key is invalid')
    _uuid_hex(namespace.organization_id, 'organization UUID')
    return match.group(1)


def _snapshot_relative_path(namespace_key: str, sha256: str) -> str:
    if _SHA256_RE.fullmatch(namespace_key) is None or _SHA256_RE.fullmatch(sha256) is None:
        raise OutboxError('generated snapshot address is invalid')
    return f'{namespace_key}/snapshots/{sha256[:2]}/{sha256}.alog'


def _stored_snapshot_path(value: object, namespace_key: str, sha256: str) -> str:
    if not isinstance(value, str) or value == '' or '\\' in value or '\x00' in value:
        raise OutboxError('stored snapshot path is invalid')
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or '..' in parsed.parts or parsed.as_posix() != value:
        raise OutboxError('stored snapshot path is invalid')
    expected = _snapshot_relative_path(namespace_key, sha256)
    if value != expected:
        raise OutboxError('stored snapshot path is invalid')
    return value


def _open_path_readonly(path: Path) -> int:
    try:
        return secure_filesystem.open_path_readonly(
            path, is_windows=_IS_WINDOWS, native=_WINDOWS_NATIVE
        )
    except secure_filesystem.FilesystemError as exc:
        if 'link' in str(exc) or 'reparse' in str(exc):
            raise OutboxError(
                'saved profile path contains a symlink or reparse point'
            ) from None
        if 'changed' in str(exc):
            raise OutboxError('saved profile changed while it was opened') from None
        raise OutboxError('saved profile is unavailable') from None


def _reject_symlink_components(path: Path) -> None:
    try:
        descriptor = secure_filesystem.open_path_readonly(path, is_windows=False)
    except secure_filesystem.FilesystemError as exc:
        if 'link' in str(exc) or 'reparse' in str(exc):
            raise OutboxError(
                'saved profile path contains a symlink or reparse point'
            ) from None
        raise OutboxError('saved profile is unavailable') from None
    else:
        os.close(descriptor)


def _verify_snapshot_content(
    path: Path, sha256: str, byte_count: int, *, require_private: bool
) -> None:
    descriptor = _open_path_readonly(path)
    digest = hashlib.sha256()
    actual_count = 0
    try:
        descriptor_stat = os.fstat(descriptor)
        _require_regular_file(descriptor_stat, 'snapshot')
        if require_private:
            _verify_private_permissions(path, 0o400)
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            actual_count += len(chunk)
            if actual_count > MAX_PROFILE_BYTES:
                raise OutboxError('snapshot size is invalid')
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if actual_count != byte_count or digest.hexdigest() != sha256:
        raise OutboxError('snapshot content does not match its address')


def _require_regular_file(value: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise OutboxError(f'{label} must be a regular file')


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError('snapshot write made no progress')
        view = view[written:]


def _acquire_file_lock(descriptor: int) -> None:
    if _IS_WINDOWS:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'0')
            _fsync_descriptor(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        locking(descriptor, cast(int, module.__dict__['LK_LOCK']), 1)
    else:
        module = importlib.import_module('fcntl')
        flock = cast(Callable[[int, int], None], module.__dict__['flock'])
        flock(descriptor, cast(int, module.__dict__['LOCK_EX']))


def _release_file_lock(descriptor: int) -> None:
    if _IS_WINDOWS:
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        locking(descriptor, cast(int, module.__dict__['LK_UNLCK']), 1)
    else:
        module = importlib.import_module('fcntl')
        flock = cast(Callable[[int, int], None], module.__dict__['flock'])
        flock(descriptor, cast(int, module.__dict__['LOCK_UN']))


def _require_windows_native() -> _WindowsNativeApi:
    try:
        return secure_filesystem.require_windows_native(native=_WINDOWS_NATIVE)
    except secure_filesystem.FilesystemError:
        raise OutboxError('Windows secure filesystem APIs are unavailable') from None


def _set_private_permissions(path: Path, mode: int) -> None:
    try:
        secure_filesystem.set_private_permissions(
            path, mode, is_windows=_IS_WINDOWS, native=_WINDOWS_NATIVE
        )
    except secure_filesystem.FilesystemError:
        raise OutboxError('private connector permissions could not be applied') from None


def _verify_private_permissions(path: Path, mode: int) -> None:
    try:
        secure_filesystem.verify_private_permissions(
            path, mode, is_windows=_IS_WINDOWS, native=_WINDOWS_NATIVE
        )
    except secure_filesystem.FilesystemError:
        message = (
            'snapshot private read-only permissions are invalid'
            if mode == 0o400
            else 'private connector permissions are invalid'
        )
        raise OutboxError(message) from None


def _fsync_descriptor(descriptor: int, *, directory: bool = False) -> None:
    secure_filesystem.fsync_descriptor(
        descriptor,
        directory=directory,
        is_windows=_IS_WINDOWS,
        native=_WINDOWS_NATIVE,
    )


def _fsync_directory(path: Path) -> None:
    if _IS_WINDOWS:
        _require_windows_native().flush_directory(path)
        return
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        _fsync_descriptor(descriptor, directory=True)
    finally:
        os.close(descriptor)


def _secure_unlink(path: Path) -> None:
    try:
        secure_filesystem.secure_unlink(
            path, is_windows=_IS_WINDOWS, native=_WINDOWS_NATIVE
        )
    except secure_filesystem.FilesystemError:
        raise OutboxError('generated removal failed') from None


def _metadata_text(value: object, label: str) -> str:
    if not isinstance(value, bytes) or not value or len(value) > MAX_METADATA_BYTES:
        raise ValueError(f'{label} metadata has an invalid size')
    try:
        text = value.decode('utf-8')
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f'{label} metadata is invalid JSON') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'{label} metadata must be a JSON object')
    try:
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f'{label} metadata is invalid JSON') from exc
    if canonical != text:
        raise ValueError(f'{label} metadata is not canonical')
    return text


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError('non-finite JSON number')


def _datetime_text(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('datetime must be timezone-aware')
    return value.astimezone(UTC).isoformat(timespec='microseconds')


def _stored_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise OutboxError('stored timestamp is invalid')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OutboxError('stored timestamp is invalid') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or _datetime_text(parsed) != value:
        raise OutboxError('stored timestamp is not canonical UTC')
    return parsed.astimezone(UTC)


def _optional_stored_datetime(value: object) -> datetime | None:
    return None if value is None else _stored_datetime(value)


def _uuid_hex(value: object, label: str) -> str:
    if not isinstance(value, UUID):
        raise ValueError(f'{label} is invalid')
    return value.hex


def _job_id(value: object) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise ValueError('job id is invalid')
    return value


def _lease_token(value: object) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise ValueError('lease token is invalid')
    return value


def _stored_job_id(value: object) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise OutboxError('stored job id is invalid')
    return value


def _stored_uuid(value: object) -> UUID:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise OutboxError('stored UUID is invalid')
    try:
        parsed = UUID(hex=value)
    except ValueError as exc:
        raise OutboxError('stored UUID is invalid') from exc
    if parsed.hex != value:
        raise OutboxError('stored UUID is invalid')
    return parsed


def _stored_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OutboxError('stored SHA-256 is invalid')
    return value


def _stored_text(value: object, label: str) -> str:
    if not isinstance(value, str) or _has_control_character(value):
        raise OutboxError(f'stored {label} is invalid')
    return value


def _stored_metadata_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OutboxError(f'stored {label} metadata is invalid')
    try:
        return _metadata_text(value.encode('utf-8'), label)
    except (UnicodeEncodeError, ValueError):
        raise OutboxError(f'stored {label} metadata is invalid') from None


def _stored_failure_code(value: object) -> str:
    if not isinstance(value, str) or value not in _PUBLIC_FAILURE_CODES:
        raise OutboxError('stored failure code is invalid')
    return value


def _optional_stored_failure_code(value: object) -> str | None:
    return None if value is None else _stored_failure_code(value)


def _stored_failure_message(value: object) -> str:
    if not isinstance(value, str) or value not in FAILURE_MESSAGES.values():
        raise OutboxError('stored failure message is invalid')
    return value


def _optional_stored_failure_message(value: object) -> str | None:
    return None if value is None else _stored_failure_message(value)


def _stored_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OutboxError(f'stored {label} is invalid')
    return value


def _stored_positive_int(value: object, label: str, maximum: int) -> int:
    result = _stored_nonnegative_int(value, label)
    if result < 1 or result > maximum:
        raise OutboxError(f'stored {label} is invalid')
    return result


def _failure_fields(failure: PublicFailure) -> tuple[str, str]:
    if not isinstance(failure, PublicFailure):
        raise ValueError('failure is invalid')
    if not isinstance(failure.kind, FailureKind):
        raise ValueError('failure kind is invalid')
    if type(failure.retryable) is not bool:
        raise ValueError('failure retryable value is invalid')
    code = _bounded_text(failure.code, _FAILURE_CODE_CHARS, 'failure code')
    _bounded_text(
        failure.message,
        MAX_ERROR_MESSAGE_CODE_POINTS,
        'failure message',
        truncate=False,
    )
    if code not in _FAILURE_CODES_BY_KIND[failure.kind]:
        raise ValueError('failure code is invalid')
    return code, FAILURE_MESSAGES[failure.kind]


def _stored_failure_pair_is_valid(code: str, message: str) -> bool:
    return any(
        message == FAILURE_MESSAGES[kind] and code in codes
        for kind, codes in _FAILURE_CODES_BY_KIND.items()
    )


def _pause_code(value: object) -> str:
    code = _bounded_text(value, _FAILURE_CODE_CHARS, 'pause code', truncate=False)
    if code not in _PAUSE_CODES:
        raise ValueError('pause code is invalid')
    return code


def _bounded_text(
    value: object, maximum: int, label: str, *, truncate: bool = True
) -> str:
    if not isinstance(value, str) or value == '':
        raise ValueError(f'{label} is invalid')
    bounded = value[:maximum] if truncate else value
    if len(bounded) > maximum or _has_control_character(bounded):
        raise ValueError(f'{label} is invalid')
    try:
        bounded.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError(f'{label} is invalid') from exc
    return bounded


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _path_is_junction(path: Path) -> bool:
    return secure_filesystem.path_is_junction(path)


def _directory_entry_is_reparse(entry: os.DirEntry[str]) -> bool:
    return secure_filesystem.directory_entry_is_reparse(entry)


__all__ = [
    'EnqueueResult',
    'FailedJob',
    'Job',
    'Outbox',
    'OutboxError',
    'QueueCounts',
    'Snapshot',
]
