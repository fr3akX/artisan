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

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import errno
import hashlib
import importlib
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID, uuid4

from artisanlib.roastserver.contract import (
    MAX_ERROR_MESSAGE_CODE_POINTS,
    MAX_METADATA_BYTES,
    MAX_PROFILE_BYTES,
    Namespace,
    PublicFailure,
)

if TYPE_CHECKING:
    from artisanlib.roastserver.metadata import ProjectedMetadata

_SCHEMA_VERSION: Final[int] = 1
_DATABASE_NAME: Final[str] = 'outbox.sqlite3'
_BUSY_TIMEOUT_MS: Final[int] = 5000
_COPY_CHUNK_BYTES: Final[int] = 1024 * 1024
_FAILURE_CODE_CHARS: Final[int] = 100
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

_SCHEMA_STATEMENTS: tuple[str, ...] = (
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
            self._prepare_root()
            connection: sqlite3.Connection | None = None
            with self._filesystem_lock():
                self._reject_database_symlink()
                try:
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
                    connection.execute('PRAGMA foreign_keys=ON')
                    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                        raise OutboxError('SQLite foreign keys are unavailable')
                    self._connection = connection
                    self._migrate()
                    self._collect_unindexed_files()
                    self._harden_database_files()
                except BaseException:
                    self._connection = None
                    if connection is not None:
                        connection.close()
                    raise

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()
            self._harden_database_files()

    def database_pragmas(self) -> tuple[str, bool, int]:
        with self._lock:
            connection = self._require_connection()
            journal = cast(str, connection.execute('PRAGMA journal_mode').fetchone()[0]).lower()
            foreign_keys = bool(connection.execute('PRAGMA foreign_keys').fetchone()[0])
            busy_timeout = cast(int, connection.execute('PRAGMA busy_timeout').fetchone()[0])
            return journal, foreign_keys, busy_timeout

    def recover_expired_leases(self, now: datetime) -> int:
        now_text = _datetime_text(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = 'pending', next_attempt_at = NULL,
                       lease_expires_at = NULL, error_code = NULL,
                       error_message = NULL, updated_at = ?
                   WHERE state = 'leased' AND lease_expires_at <= ?''',
                (now_text, now_text),
            )
            return cursor.rowcount

    def snapshot_saved_file(self, namespace: Namespace, source: Path) -> Snapshot:
        with self._filesystem_lock():
            return self._snapshot_saved_file_locked(namespace, source)

    def _snapshot_saved_file_locked(self, namespace: Namespace, source: Path) -> Snapshot:
        namespace_key = _namespace_key(namespace)
        source_path = Path(source)
        source_fd = _open_path_readonly(source_path)
        temporary_path: Path | None = None
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
            self._publish_temporary(temporary_path, final_path)
            temporary_path = None
            return Snapshot(
                namespace=namespace,
                sha256=sha256,
                relative_path=relative_path,
                absolute_path=final_path,
                byte_count=byte_count,
                source_modified_at=source_modified_at,
            )
        finally:
            os.close(source_fd)
            if temporary_path is not None:
                self._discard_temporary(temporary_path)

    def enqueue(
        self,
        namespace: Namespace,
        snapshot: Snapshot,
        roast_uuid: UUID,
        metadata: ProjectedMetadata,
        client_uuid: UUID,
    ) -> EnqueueResult:
        with self._filesystem_lock():
            return self._enqueue_locked(namespace, snapshot, roast_uuid, metadata, client_uuid)

    def _enqueue_locked(
        self,
        namespace: Namespace,
        snapshot: Snapshot,
        roast_uuid: UUID,
        metadata: ProjectedMetadata,
        client_uuid: UUID,
    ) -> EnqueueResult:
        namespace_key = _namespace_key(namespace)
        self._validate_snapshot(namespace, namespace_key, snapshot)
        aroast_json = _metadata_text(metadata.aroast_json, 'aroast')
        revision_json = _metadata_text(metadata.revision_json, 'revision')
        roast_hex = _uuid_hex(roast_uuid, 'roast UUID')
        client_hex = _uuid_hex(client_uuid, 'client UUID')
        now_text = _datetime_text(self._clock())
        idempotency_key = f'archive-v1:{client_hex}:{roast_hex}:{snapshot.sha256}'
        path_to_unlink: str | None = None
        with self._transaction() as connection:
            namespace_id = self._namespace_id(connection, namespace, create=True)
            if namespace_id is None:
                raise OutboxError('namespace was not persisted')
            connection.execute(
                '''INSERT OR IGNORE INTO snapshots
                   (namespace_id, sha256, relative_path, byte_count, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (
                    namespace_id,
                    snapshot.sha256,
                    snapshot.relative_path,
                    snapshot.byte_count,
                    now_text,
                ),
            )
            indexed = connection.execute(
                '''SELECT relative_path, byte_count FROM snapshots
                   WHERE namespace_id = ? AND sha256 = ?''',
                (namespace_id, snapshot.sha256),
            ).fetchone()
            if (
                indexed is None
                or indexed['relative_path'] != snapshot.relative_path
                or indexed['byte_count'] != snapshot.byte_count
            ):
                raise OutboxError('snapshot index conflicts with generated content')
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
                        attempts, next_attempt_at, lease_expires_at,
                        error_code, error_message, created_at, updated_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                               0, NULL, NULL, NULL, NULL, ?, ?, NULL)''',
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

    def lease_next(
        self, namespace: Namespace, now: datetime, lease_seconds: int = 60
    ) -> Job | None:
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError('lease_seconds must be a positive integer')
        now_text = _datetime_text(now)
        try:
            lease_expires_text = _datetime_text(now + timedelta(seconds=lease_seconds))
        except OverflowError as exc:
            raise ValueError('lease duration is out of range') from exc
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
            connection.execute(
                '''UPDATE jobs
                   SET state = 'leased', attempts = attempts + 1,
                       next_attempt_at = NULL, lease_expires_at = ?,
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE id = ?''',
                (lease_expires_text, now_text, row['id']),
            )
            leased = connection.execute('SELECT * FROM jobs WHERE id = ?', (row['id'],)).fetchone()
            if leased is None:
                raise OutboxError('leased outbox job disappeared')
            return self._row_to_job(leased, namespace)

    def mark_complete(self, job_id: str, now: datetime) -> None:
        with self._filesystem_lock():
            self._mark_complete_locked(job_id, now)

    def _mark_complete_locked(self, job_id: str, now: datetime) -> None:
        job_hex = _job_id(job_id)
        now_text = _datetime_text(now)
        path_to_unlink: str | None = None
        with self._transaction() as connection:
            row = self._job_for_transition(connection, job_hex)
            if row['state'] == 'complete':
                return
            self._require_state(row, 'leased')
            snapshot_sha256 = cast(str, row['snapshot_sha256'])
            connection.execute(
                '''UPDATE jobs
                   SET snapshot_sha256 = NULL, snapshot_relative_path = NULL,
                       snapshot_byte_count = NULL, state = 'complete',
                       next_attempt_at = NULL, lease_expires_at = NULL,
                       error_code = NULL, error_message = NULL,
                       updated_at = ?, completed_at = ?
                   WHERE id = ?''',
                (now_text, now_text, job_hex),
            )
            path_to_unlink = self._release_snapshot_if_unreferenced(
                connection, cast(int, row['namespace_id']), snapshot_sha256
            )
        if path_to_unlink is not None:
            self._unlink_generated_snapshot(path_to_unlink)

    def mark_retry(
        self,
        job_id: str,
        now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> None:
        job_hex = _job_id(job_id)
        now_text = _datetime_text(now)
        next_attempt_text = _datetime_text(next_attempt_at)
        if next_attempt_at.astimezone(UTC) < now.astimezone(UTC):
            raise ValueError('next attempt cannot be before now')
        error_code, error_message = _failure_fields(failure)
        with self._transaction() as connection:
            row = self._job_for_transition(connection, job_hex)
            self._require_state(row, 'leased')
            connection.execute(
                '''UPDATE jobs
                   SET state = 'retry_wait', next_attempt_at = ?,
                       lease_expires_at = NULL, error_code = ?,
                       error_message = ?, updated_at = ?
                   WHERE id = ?''',
                (next_attempt_text, error_code, error_message, now_text, job_hex),
            )

    def mark_failed(self, job_id: str, now: datetime, failure: PublicFailure) -> None:
        job_hex = _job_id(job_id)
        now_text = _datetime_text(now)
        error_code, error_message = _failure_fields(failure)
        with self._transaction() as connection:
            row = self._job_for_transition(connection, job_hex)
            self._require_state(row, 'leased')
            connection.execute(
                '''UPDATE jobs
                   SET state = 'failed', next_attempt_at = NULL,
                       lease_expires_at = NULL, error_code = ?,
                       error_message = ?, updated_at = ?
                   WHERE id = ?''',
                (error_code, error_message, now_text, job_hex),
            )

    def pause_namespace(self, namespace: Namespace, now: datetime, code: str) -> int:
        now_text = _datetime_text(now)
        bounded_code = _bounded_text(code, _FAILURE_CODE_CHARS, 'pause code')
        with self._transaction() as connection:
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return 0
            cursor = connection.execute(
                '''UPDATE jobs
                   SET state = 'paused', lease_expires_at = NULL,
                       error_code = ?, error_message = NULL, updated_at = ?
                   WHERE namespace_id = ?
                     AND state IN ('pending', 'leased', 'retry_wait')''',
                (bounded_code, now_text, namespace_id),
            )
            return cursor.rowcount

    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
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
                       error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE namespace_id = ? AND state = 'paused' ''',
                (now_text, now_text, namespace_id),
            )
            return cursor.rowcount

    def retry_now(self, job_id: str, now: datetime) -> None:
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
                       lease_expires_at = NULL, error_code = NULL,
                       error_message = NULL, updated_at = ?
                   WHERE id = ?''',
                (now_text, job_hex),
            )

    def remove(self, job_id: str) -> None:
        with self._filesystem_lock():
            self._remove_locked(job_id)

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
                    connection, namespace_id, snapshot_sha256
                )
        if path_to_unlink is not None:
            self._unlink_generated_snapshot(path_to_unlink)

    def counts(self, namespace: Namespace) -> QueueCounts:
        with self._lock:
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
        with self._lock:
            connection = self._require_connection()
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return ()
            rows = connection.execute(
                '''SELECT id, roast_uuid, content_sha256, attempts,
                          next_attempt_at, error_code, error_message, updated_at
                   FROM jobs
                   WHERE namespace_id = ? AND state = 'failed'
                   ORDER BY updated_at DESC, id''',
                (namespace_id,),
            ).fetchall()
            failed: list[FailedJob] = []
            for row in rows:
                error_code = row['error_code']
                error_message = row['error_message']
                if not isinstance(error_code, str) or not isinstance(error_message, str):
                    raise OutboxError('failed outbox job has incomplete details')
                failed.append(
                    FailedJob(
                        id=_stored_job_id(row['id']),
                        roast_uuid=_stored_uuid(row['roast_uuid']),
                        sha256=_stored_sha256(row['content_sha256']),
                        attempts=_stored_nonnegative_int(row['attempts'], 'attempts'),
                        next_attempt_at=_optional_stored_datetime(row['next_attempt_at']),
                        error_code=error_code[:_FAILURE_CODE_CHARS],
                        error_message=error_message[:MAX_ERROR_MESSAGE_CODE_POINTS],
                        updated_at=_stored_datetime(row['updated_at']),
                    )
                )
            return tuple(failed)

    def protected_paths(self, namespace: Namespace) -> frozenset[Path]:
        with self._filesystem_lock():
            return self._protected_paths_locked(namespace)

    def _protected_paths_locked(self, namespace: Namespace) -> frozenset[Path]:
        namespace_key = _namespace_key(namespace)
        with self._lock:
            connection = self._require_connection()
            namespace_id = self._namespace_id(connection, namespace, create=False)
            if namespace_id is None:
                return frozenset()
            rows = connection.execute(
                '''SELECT DISTINCT snapshot_sha256, snapshot_relative_path
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
                path = self.root / relative_path
                _verify_regular_path(path, 'snapshot')
                paths.add(path)
            return frozenset(paths)

    def _prepare_root(self) -> None:
        if os.path.lexists(self.root):
            root_stat = os.lstat(self.root)
            if stat.S_ISLNK(root_stat.st_mode):
                raise OutboxError('connector root must not be a symlink')
            if not stat.S_ISDIR(root_stat.st_mode):
                raise OutboxError('connector root must be a directory')
        else:
            self.root.mkdir(parents=True, mode=0o700)
            root_stat = os.lstat(self.root)
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise OutboxError('connector root must be a private directory')
        _set_private_permissions(self.root, 0o700)

    def _reject_database_symlink(self) -> None:
        if not os.path.lexists(self._database_path):
            return
        database_stat = os.lstat(self._database_path)
        if stat.S_ISLNK(database_stat.st_mode):
            raise OutboxError('outbox database must not be a symlink')
        if not stat.S_ISREG(database_stat.st_mode):
            raise OutboxError('outbox database must be a regular file')

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
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute('INSERT INTO schema_version(version) VALUES (?)', (_SCHEMA_VERSION,))
            else:
                versions = connection.execute('SELECT version FROM schema_version').fetchall()
                if len(versions) != 1 or versions[0]['version'] != _SCHEMA_VERSION:
                    raise OutboxError('unsupported outbox schema version')
                expected = {'schema_version', 'namespaces', 'snapshots', 'jobs'}
                if tables != expected:
                    raise OutboxError('outbox schema is incomplete')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _collect_unindexed_files(self) -> None:
        with self._transaction() as connection:
            rows = connection.execute(
                '''SELECT n.namespace_key, s.sha256, s.relative_path
                   FROM snapshots AS s
                   JOIN namespaces AS n ON n.id = s.namespace_id'''
            ).fetchall()
            indexed: set[str] = set()
            for row in rows:
                namespace_key = cast(str, row['namespace_key'])
                if _SHA256_RE.fullmatch(namespace_key) is None:
                    raise OutboxError('stored namespace key is invalid')
                sha256 = _stored_sha256(row['sha256'])
                indexed.add(_stored_snapshot_path(row['relative_path'], namespace_key, sha256))
            changed_directories: set[Path] = set()
            for namespace_entry in os.scandir(self.root):
                if not _SHA256_RE.fullmatch(namespace_entry.name):
                    continue
                if namespace_entry.is_symlink():
                    raise OutboxError('generated namespace directory must not be a symlink')
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
                    if prefix_entry.is_symlink():
                        raise OutboxError('snapshot directory entries must not be symlinks')
                    if (
                        prefix_entry.is_file(follow_symlinks=False)
                        and _TEMP_FILE_RE.fullmatch(prefix_entry.name) is not None
                    ):
                        Path(prefix_entry.path).unlink()
                        changed_directories.add(snapshots_path)
                        continue
                    if _PREFIX_RE.fullmatch(prefix_entry.name) is None:
                        continue
                    if not prefix_entry.is_dir(follow_symlinks=False):
                        continue
                    prefix_path = Path(prefix_entry.path)
                    self._harden_directory(prefix_path)
                    for file_entry in os.scandir(prefix_path):
                        if file_entry.is_symlink():
                            raise OutboxError('snapshot files must not be symlinks')
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
                            Path(file_entry.path).unlink()
                            changed_directories.add(prefix_path)
            for directory in changed_directories:
                _fsync_directory(directory)

    def _namespace_id(
        self, connection: sqlite3.Connection, namespace: Namespace, *, create: bool
    ) -> int | None:
        namespace_key = _namespace_key(namespace)
        organization_uuid = _uuid_hex(namespace.organization_id, 'organization UUID')
        row = connection.execute(
            '''SELECT id, origin, organization_uuid, namespace_key
               FROM namespaces
               WHERE (origin = ? AND organization_uuid = ?) OR namespace_key = ?''',
            (namespace.origin, organization_uuid, namespace_key),
        ).fetchall()
        if row:
            if len(row) != 1:
                raise OutboxError('namespace index is inconsistent')
            existing = row[0]
            if (
                existing['origin'] != namespace.origin
                or existing['organization_uuid'] != organization_uuid
                or existing['namespace_key'] != namespace_key
            ):
                raise OutboxError('namespace identity conflicts with stored data')
            return cast(int, existing['id'])
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
            os.fsync(temporary_fd)
        except BaseException:
            if temporary_fd is not None:
                os.close(temporary_fd)
                temporary_fd = None
            with suppress(OSError):
                if _HAS_DIRECTORY_FDS:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                else:
                    temporary_path.unlink()
            raise
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            os.close(directory_fd)
        return digest.hexdigest(), byte_count, temporary_path

    def _discard_temporary(self, temporary_path: Path) -> None:
        try:
            directory_fd = self._open_generated_directory(temporary_path.parent)
        except (OSError, OutboxError):
            return
        try:
            with suppress(OSError):
                if _HAS_DIRECTORY_FDS:
                    os.unlink(temporary_path.name, dir_fd=directory_fd)
                else:
                    temporary_path.unlink()
                _fsync_descriptor(directory_fd)
        finally:
            os.close(directory_fd)

    def _publish_temporary(self, temporary_path: Path, final_path: Path) -> None:
        self._ensure_generated_directory(final_path.parent)
        try:
            temporary_path.relative_to(self.root)
            final_path.relative_to(self.root)
        except ValueError as exc:
            raise OutboxError('snapshot publication path is invalid') from exc
        source_directory_fd = self._open_generated_directory(temporary_path.parent)
        destination_directory_fd = self._open_generated_directory(final_path.parent)
        try:
            if _HAS_DIRECTORY_FDS:
                try:
                    existing = os.stat(
                        final_path.name,
                        dir_fd=destination_directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    existing = None
            else:
                existing = os.lstat(final_path) if os.path.lexists(final_path) else None
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise OutboxError('generated snapshot path must not be a symlink')
                if not stat.S_ISREG(existing.st_mode):
                    raise OutboxError('generated snapshot path must be a regular file')
            if _HAS_DIRECTORY_FDS:
                os.replace(
                    temporary_path.name,
                    final_path.name,
                    src_dir_fd=source_directory_fd,
                    dst_dir_fd=destination_directory_fd,
                )
            else:
                os.replace(temporary_path, final_path)
            _fsync_descriptor(source_directory_fd)
            _fsync_descriptor(destination_directory_fd)
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
        descriptor = _open_path_readonly(expected_absolute)
        digest = hashlib.sha256()
        byte_count = 0
        try:
            descriptor_stat = os.fstat(descriptor)
            _require_regular_file(descriptor_stat, 'snapshot')
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > MAX_PROFILE_BYTES:
                    raise OutboxError('snapshot size is invalid')
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if byte_count != snapshot.byte_count or digest.hexdigest() != sha256:
            raise OutboxError('snapshot content does not match its address')

    def _row_to_job(self, row: sqlite3.Row, namespace: Namespace) -> Job:
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
            _verify_regular_path(snapshot_path, 'snapshot')
            snapshot_byte_count = _stored_positive_int(
                row['snapshot_byte_count'], 'snapshot byte count', MAX_PROFILE_BYTES
            )
        return Job(
            id=_stored_job_id(row['id']),
            namespace=namespace,
            roast_uuid=_stored_uuid(row['roast_uuid']),
            content_sha256=_stored_sha256(row['content_sha256']),
            snapshot_sha256=snapshot_sha256,
            snapshot_path=snapshot_path,
            snapshot_byte_count=snapshot_byte_count,
            aroast_json=_stored_text(row['aroast_json'], 'aroast metadata'),
            revision_json=_stored_text(row['revision_json'], 'revision metadata'),
            idempotency_key=_stored_text(row['idempotency_key'], 'idempotency key'),
            state=state,
            attempts=_stored_nonnegative_int(row['attempts'], 'attempts'),
            next_attempt_at=_optional_stored_datetime(row['next_attempt_at']),
            lease_expires_at=_optional_stored_datetime(row['lease_expires_at']),
            error_code=_optional_stored_text(row['error_code'], 'error code'),
            error_message=_optional_stored_text(row['error_message'], 'error message'),
            created_at=_stored_datetime(row['created_at']),
            updated_at=_stored_datetime(row['updated_at']),
        )

    def _job_for_transition(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
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
        if references != 0:
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
                    path_stat = os.lstat(path)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(path_stat.st_mode):
                raise OutboxError('snapshot path must not be a symlink')
            if not stat.S_ISREG(path_stat.st_mode):
                raise OutboxError('snapshot must be a regular file')
            if _HAS_DIRECTORY_FDS:
                os.unlink(path.name, dir_fd=directory_fd)
            else:
                path.unlink()
            _fsync_descriptor(directory_fd)
        finally:
            os.close(directory_fd)

    def _open_generated_directory(self, directory: Path) -> int:
        try:
            relative_parts = directory.relative_to(self.root).parts
        except ValueError as exc:
            raise OutboxError('generated directory escapes connector root') from exc
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
            try:
                path_stat = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(path_stat.st_mode):
                raise OutboxError('SQLite files must not be symlinks')
            if not stat.S_ISREG(path_stat.st_mode):
                raise OutboxError('SQLite files must be regular files')
            _set_private_permissions(path, 0o600)

    @contextmanager
    def _filesystem_lock(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.root / '.outbox.lock'
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0)
            flags |= getattr(os, 'O_NOFOLLOW', 0)
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise OutboxError('outbox lock must not be a symlink') from exc
                raise OutboxError('outbox process lock is unavailable') from exc
            try:
                descriptor_stat = os.fstat(descriptor)
                path_stat = os.lstat(lock_path)
                if (
                    not stat.S_ISREG(descriptor_stat.st_mode)
                    or stat.S_ISLNK(path_stat.st_mode)
                    or (descriptor_stat.st_dev, descriptor_stat.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)
                ):
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


def _namespace_key(namespace: Namespace) -> str:
    if namespace.origin == '' or '\x00' in namespace.origin:
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
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    directory_flags = flags | getattr(os, 'O_DIRECTORY', 0)
    if os.name != 'nt' and os.open in os.supports_dir_fd:
        components = absolute.parts[1:]
        if not components:
            raise OutboxError('saved profile path is invalid')
        directory_fd = os.open(os.sep, directory_flags)
        try:
            for component in components[:-1]:
                if component in {'', '.', '..'}:
                    raise OutboxError('saved profile path is invalid')
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return os.open(components[-1], flags, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise OutboxError('saved profile path contains a symlink') from exc
            raise OutboxError('saved profile is unavailable') from exc
        finally:
            os.close(directory_fd)
    _reject_symlink_components(absolute)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OutboxError('saved profile path contains a symlink') from exc
        raise OutboxError('saved profile is unavailable') from exc
    path_stat = os.lstat(absolute)
    descriptor_stat = os.fstat(descriptor)
    if (path_stat.st_dev, path_stat.st_ino) != (descriptor_stat.st_dev, descriptor_stat.st_ino):
        os.close(descriptor)
        raise OutboxError('saved profile changed while it was opened')
    return descriptor


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except OSError as exc:
            raise OutboxError('saved profile is unavailable') from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise OutboxError('saved profile path contains a symlink')


def _verify_regular_path(path: Path, label: str) -> None:
    try:
        descriptor = _open_path_readonly(path)
    except OutboxError as exc:
        if os.path.lexists(path) and stat.S_ISLNK(os.lstat(path).st_mode):
            raise OutboxError(f'{label} path must not be a symlink') from exc
        raise
    try:
        _require_regular_file(os.fstat(descriptor), label)
    finally:
        os.close(descriptor)


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
    if os.name == 'nt':
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'0')
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        locking(descriptor, cast(int, module.__dict__['LK_LOCK']), 1)
    else:
        module = importlib.import_module('fcntl')
        flock = cast(Callable[[int, int], None], module.__dict__['flock'])
        flock(descriptor, cast(int, module.__dict__['LOCK_EX']))


def _release_file_lock(descriptor: int) -> None:
    if os.name == 'nt':
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        locking(descriptor, cast(int, module.__dict__['LK_UNLCK']), 1)
    else:
        module = importlib.import_module('fcntl')
        flock = cast(Callable[[int, int], None], module.__dict__['flock'])
        flock(descriptor, cast(int, module.__dict__['LOCK_UN']))


def _set_private_permissions(path: Path, mode: int) -> None:
    if os.name == 'nt':
        return
    try:
        os.chmod(path, mode, follow_symlinks=False)
        actual_mode = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError as exc:
        raise OutboxError('private connector permissions could not be applied') from exc
    if actual_mode != mode:
        raise OutboxError('private connector permissions could not be applied')


def _fsync_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        _fsync_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _metadata_text(value: object, label: str) -> str:
    if not isinstance(value, bytes) or not value or len(value) > MAX_METADATA_BYTES:
        raise ValueError(f'{label} metadata has an invalid size')
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'{label} metadata is not UTF-8') from exc


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
    if not isinstance(value, str):
        raise OutboxError(f'stored {label} is invalid')
    return value


def _optional_stored_text(value: object, label: str) -> str | None:
    return None if value is None else _stored_text(value, label)


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
    code = _bounded_text(failure.code, _FAILURE_CODE_CHARS, 'failure code')
    message = _bounded_text(
        failure.message, MAX_ERROR_MESSAGE_CODE_POINTS, 'failure message'
    )
    return code, message


def _bounded_text(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str) or value == '':
        raise ValueError(f'{label} is invalid')
    bounded = value[:maximum]
    if '\x00' in bounded:
        raise ValueError(f'{label} is invalid')
    try:
        bounded.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError(f'{label} is invalid') from exc
    return bounded


__all__ = [
    'EnqueueResult',
    'FailedJob',
    'Job',
    'Outbox',
    'OutboxError',
    'QueueCounts',
    'Snapshot',
]
