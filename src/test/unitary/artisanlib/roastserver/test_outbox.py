from __future__ import annotations

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import errno
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from threading import Barrier, Condition, Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest

from artisanlib.roastserver import outbox as outbox_module
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    MAX_PROFILE_BYTES,
    FailureKind,
    Namespace,
    PublicFailure,
)
from artisanlib.roastserver.outbox import (
    EnqueueResult,
    Job,
    LeaseFailure,
    Outbox,
    OutboxError,
    Snapshot,
)

if TYPE_CHECKING:
    from artisanlib.roastserver.metadata import ProjectedMetadata

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
NAMESPACE_DIGEST = hashlib.sha256(b'primary namespace').hexdigest()
OTHER_NAMESPACE_DIGEST = hashlib.sha256(b'other namespace').hexdigest()
NAMESPACE = Namespace(
    origin='https://archive.example',
    organization_id=UUID('11111111-1111-4111-8111-111111111111'),
    key=f'namespace-sha256:{NAMESPACE_DIGEST}',
)
OTHER_NAMESPACE = Namespace(
    origin='https://other.example',
    organization_id=UUID('22222222-2222-4222-8222-222222222222'),
    key=f'namespace-sha256:{OTHER_NAMESPACE_DIGEST}',
)
ROAST_UUID = UUID('33333333-3333-4333-8333-333333333333')
OTHER_ROAST_UUID = UUID('44444444-4444-4444-8444-444444444444')
CLIENT_UUID = UUID('55555555-5555-4555-8555-555555555555')
PROFILE_BYTES = b'{"title":"saved roast"}\n'


@dataclass(frozen=True, slots=True)
class _Metadata:
    aroast_json: bytes = b'{"date":"2026-08-01T12:00:00+00:00"}'
    revision_json: bytes = b'{"modified_at":"2026-08-01T12:00:00+00:00"}'


METADATA = cast('ProjectedMetadata', _Metadata())
FAILURE = PublicFailure(
    kind=FailureKind.OFFLINE,
    code='offline',
    message='Offline / server unavailable.',
    retryable=True,
)


def opened_outbox(root: Path, *, now: datetime = NOW) -> Outbox:
    result = Outbox(root, clock=lambda: now)
    result.open()
    return result


@pytest.fixture
def saved_profile(tmp_path: Path) -> Path:
    path = tmp_path / 'saved.alog'
    path.write_bytes(PROFILE_BYTES)
    return path


@pytest.fixture
def outbox(tmp_path: Path) -> Generator[Outbox]:
    result = opened_outbox(tmp_path / 'connector')
    try:
        yield result
    finally:
        result.close()


def enqueue_fixture(
    outbox: Outbox,
    *,
    namespace: Namespace = NAMESPACE,
    roast_uuid: UUID = ROAST_UUID,
    source: Path | None = None,
) -> EnqueueResult:
    if source is None:
        source = outbox.root.parent / f'{namespace.organization_id.hex}-{roast_uuid.hex}.alog'
        source.write_bytes(PROFILE_BYTES)
    snapshot = outbox.snapshot_saved_file(namespace, source)
    return outbox.enqueue(namespace, snapshot, roast_uuid, METADATA, CLIENT_UUID)


def database_path(root: Path) -> Path:
    return root / 'outbox.sqlite3'


def create_v1_database(root: Path, statements: tuple[str, ...] | None = None) -> None:
    root.mkdir(mode=0o700)
    connection = sqlite3.connect(database_path(root))
    try:
        for statement in statements or outbox_module._SCHEMA_V1_STATEMENTS:
            connection.execute(statement)
        connection.execute('INSERT INTO schema_version(version) VALUES (1)')
        connection.commit()
    finally:
        connection.close()


def create_v2_database(root: Path, statements: tuple[str, ...]) -> None:
    root.mkdir(mode=0o700)
    connection = sqlite3.connect(database_path(root))
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute('INSERT INTO schema_version(version) VALUES (2)')
        connection.commit()
    finally:
        connection.close()


def test_schema_v1_migration_is_strict_transactional_and_versioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'connector'
    create_v1_database(root)
    original = outbox_module._SCHEMA_V2_STATEMENTS
    monkeypatch.setattr(
        outbox_module,
        '_SCHEMA_V2_STATEMENTS',
        (*original, 'CREATE TABLE deliberately_invalid('),
    )
    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)

    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(1,)]
        columns = connection.execute('PRAGMA table_info(jobs)').fetchall()
    finally:
        connection.close()
    assert 'lease_token' not in {column[1] for column in columns}

    monkeypatch.setattr(outbox_module, '_SCHEMA_V2_STATEMENTS', original)
    recovered = opened_outbox(root)
    recovered.close()
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(2,)]
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()[0]
        staging_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'snapshot_staging'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert 'lease_token TEXT UNIQUE' in sql
    assert 'FOREIGN KEY(namespace_id, snapshot_sha256)' in sql
    assert 'expires_at TEXT NOT NULL' in staging_sql


def without_v1_index(statements: tuple[str, ...]) -> tuple[str, ...]:
    return statements[:-1]


def with_wrong_v1_column_type(statements: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        statement.replace('byte_count INTEGER NOT NULL', 'byte_count TEXT NOT NULL')
        if statement.startswith('CREATE TABLE snapshots')
        else statement
        for statement in statements
    )


def with_wrong_v1_foreign_key(statements: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        statement.replace(
            'FOREIGN KEY(namespace_id, snapshot_sha256)\n      REFERENCES snapshots(namespace_id, sha256)',
            'FOREIGN KEY(namespace_id, snapshot_sha256)\n      REFERENCES snapshots(namespace_id, byte_count)',
        )
        if statement.startswith('CREATE TABLE jobs')
        else statement
        for statement in statements
    )


def with_uppercase_v1_state_literal(statements: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        statement.replace("'pending'", "'PENDING'")
        if statement.startswith('CREATE TABLE jobs')
        else statement
        for statement in statements
    )


def with_wrong_v1_default(statements: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        statement.replace('DEFAULT 0', 'DEFAULT 1')
        if statement.startswith('CREATE TABLE jobs')
        else statement
        for statement in statements
    )


def with_wrong_v1_check(statements: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        statement.replace('attempts >= 0', 'attempts > 0')
        if statement.startswith('CREATE TABLE jobs')
        else statement
        for statement in statements
    )


def with_wrong_v1_index_properties(statements: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        statement.replace(
            'CREATE INDEX jobs_ready_idx',
            'CREATE UNIQUE INDEX jobs_ready_idx',
        ).replace(
            'ON jobs(namespace_id, state, next_attempt_at, created_at)',
            "ON jobs(state, namespace_id, next_attempt_at, created_at) WHERE state = 'pending'",
        )
        if statement.startswith('CREATE INDEX jobs_ready_idx')
        else statement
        for statement in statements
    )


@pytest.mark.parametrize(
    'mutate',
    [
        without_v1_index,
        with_wrong_v1_column_type,
        with_wrong_v1_foreign_key,
        with_uppercase_v1_state_literal,
        with_wrong_v1_default,
        with_wrong_v1_check,
        with_wrong_v1_index_properties,
    ],
    ids=[
        'missing-index',
        'wrong-column-type',
        'wrong-foreign-key',
        'uppercase-state-literal',
        'wrong-default',
        'wrong-check',
        'wrong-index-properties',
    ],
)
def test_malformed_v1_schema_fingerprint_is_rejected_without_migration(
    tmp_path: Path,
    mutate: Callable[[tuple[str, ...]], tuple[str, ...]],
) -> None:
    root = tmp_path / 'connector'
    altered = mutate(outbox_module._SCHEMA_V1_STATEMENTS)
    create_v1_database(root, altered)
    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(1,)]
    finally:
        connection.close()


def test_malformed_v2_index_is_rejected_on_reopen(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    outbox = opened_outbox(root)
    outbox.close()
    connection = sqlite3.connect(database_path(root))
    connection.execute('DROP INDEX snapshot_staging_expiry_idx')
    connection.commit()
    connection.close()
    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)


def test_v2_quoted_state_literal_case_is_exact(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    altered = tuple(
        statement.replace("'pending'", "'PENDING'")
        if statement.startswith('CREATE TABLE jobs')
        else statement
        for statement in outbox_module._SCHEMA_V2_STATEMENTS
    )
    create_v2_database(root, altered)
    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)


@pytest.mark.parametrize(
    'persistent_object',
    [
        'CREATE TABLE unexpected_table (value INTEGER)',
        'CREATE INDEX unexpected_index ON namespaces(origin)',
        'CREATE VIEW unexpected_view AS SELECT id FROM namespaces',
        '''CREATE TRIGGER unexpected_trigger AFTER INSERT ON namespaces
           BEGIN SELECT NEW.id; END''',
    ],
    ids=['table', 'index', 'view', 'trigger'],
)
def test_v2_rejects_every_extra_persistent_schema_object(
    tmp_path: Path, persistent_object: str
) -> None:
    root = tmp_path / 'connector'
    outbox = opened_outbox(root)
    outbox.close()
    connection = sqlite3.connect(database_path(root))
    connection.execute(persistent_object)
    connection.commit()
    connection.close()
    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)


def test_schema_fingerprint_captures_index_uniqueness_origin_partial_and_columns() -> None:
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('CREATE TABLE sample (first TEXT, second TEXT, UNIQUE(first))')
        connection.execute(
            'CREATE UNIQUE INDEX sample_partial ON sample(second) WHERE second IS NOT NULL'
        )
        _objects, tables = outbox_module._schema_fingerprint(connection)
    finally:
        connection.close()
    sample = tables[0]
    indexes = {index[0][1]: index for index in sample[3]}
    automatic = indexes['sqlite_autoindex_sample_1']
    partial = indexes['sample_partial']
    assert automatic[0][2:] == (1, 'u', 0)
    assert automatic[1] == ((0, 0, 'first'),)
    assert partial[0][2:] == (1, 'c', 1)
    assert partial[1] == ((0, 1, 'second'),)


def test_unknown_schema_version_is_rejected_without_changes(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    root.mkdir(mode=0o700)
    connection = sqlite3.connect(database_path(root))
    connection.execute('CREATE TABLE schema_version (version INTEGER NOT NULL)')
    connection.execute('INSERT INTO schema_version VALUES (3)')
    connection.commit()
    connection.close()

    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)

    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(3,)]
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def insert_v1_pending_job(root: Path, *, aroast_json: str = '{}') -> Path:
    sha256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
    relative_path = f'{NAMESPACE_DIGEST}/snapshots/{sha256[:2]}/{sha256}.alog'
    snapshot_path = root / relative_path
    snapshot_path.parent.mkdir(parents=True, mode=0o700)
    snapshot_path.write_bytes(PROFILE_BYTES)
    snapshot_path.chmod(0o600)
    timestamp = NOW.isoformat(timespec='microseconds')
    connection = sqlite3.connect(database_path(root))
    try:
        connection.execute(
            '''INSERT INTO namespaces(id, origin, organization_uuid, namespace_key)
               VALUES (1, ?, ?, ?)''',
            (NAMESPACE.origin, NAMESPACE.organization_id.hex, NAMESPACE_DIGEST),
        )
        connection.execute(
            '''INSERT INTO snapshots
               (namespace_id, sha256, relative_path, byte_count, created_at)
               VALUES (1, ?, ?, ?, ?)''',
            (sha256, relative_path, len(PROFILE_BYTES), timestamp),
        )
        connection.execute(
            '''INSERT INTO jobs
               (id, namespace_id, roast_uuid, content_sha256,
                snapshot_sha256, snapshot_relative_path, snapshot_byte_count,
                aroast_json, revision_json, idempotency_key, state, attempts,
                next_attempt_at, lease_expires_at, error_code, error_message,
                created_at, updated_at, completed_at)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, '{}', ?, 'pending', 0,
                       NULL, NULL, NULL, NULL, ?, ?, NULL)''',
            (
                'a' * 32,
                ROAST_UUID.hex,
                sha256,
                sha256,
                relative_path,
                len(PROFILE_BYTES),
                aroast_json,
                f'archive-v1:{CLIENT_UUID.hex}:{ROAST_UUID.hex}:{sha256}',
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return snapshot_path


def test_populated_canonical_v1_migrates_and_hardens_snapshot(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    create_v1_database(root)
    snapshot_path = insert_v1_pending_job(root)
    migrated = opened_outbox(root)
    try:
        leased = migrated.lease_next(NAMESPACE, NOW)
        assert leased is not None and leased.id == 'a' * 32
        assert leased.lease_token is not None
        if os.name != 'nt':
            assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o400
    finally:
        migrated.close()


def test_malformed_v1_json_is_rejected_without_migration(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    create_v1_database(root)
    insert_v1_pending_job(root, aroast_json='[]')
    with pytest.raises(OutboxError, match='metadata'):
        opened_outbox(root)
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchone() == (1,)
    finally:
        connection.close()


def test_malformed_v1_state_details_are_rejected_without_migration(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    create_v1_database(root)
    insert_v1_pending_job(root)
    connection = sqlite3.connect(database_path(root))
    connection.execute(
        "UPDATE jobs SET error_code = 'offline', error_message = ?",
        (FAILURE_MESSAGES[FailureKind.OFFLINE],),
    )
    connection.commit()
    connection.close()
    with pytest.raises(OutboxError, match='state'):
        opened_outbox(root)
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
def test_sqlite_sidecar_symlinks_are_rejected_before_connection(
    tmp_path: Path, suffix: str
) -> None:
    root = tmp_path / 'connector'
    root.mkdir(mode=0o700)
    target = tmp_path / 'outside-sidecar'
    target.write_bytes(b'outside')
    try:
        Path(f'{database_path(root)}{suffix}').symlink_to(target)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(OutboxError, match='SQLite|storage'):
        opened_outbox(root)
    assert target.read_bytes() == b'outside'


def test_first_open_root_creation_race_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'connector'
    original_lexists = os.path.lexists
    root_checks = Barrier(2)

    def racing_lexists(path: str | os.PathLike[str]) -> bool:
        if Path(path) == root:
            root_checks.wait(timeout=5)
            return False
        return original_lexists(path)

    def open_instance(_index: int) -> Outbox:
        return opened_outbox(root)

    monkeypatch.setattr(os.path, 'lexists', racing_lexists)
    with ThreadPoolExecutor(max_workers=2) as executor:
        instances = tuple(executor.map(open_instance, range(2)))
    for instance in instances:
        instance.close()
    assert root.is_dir()


def test_open_enables_wal_foreign_keys_busy_timeout_and_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    result = opened_outbox(root)
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    finally:
        connection.close()
    assert result.database_pragmas() == ('wal', True, 5000)
    result.close()

    if os.name != 'nt':
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(database_path(root).stat().st_mode) == 0o600


def test_open_and_operations_reject_naive_clocks_and_times(tmp_path: Path) -> None:
    naive = datetime(2026, 8, 1, 12, 0)  # noqa: DTZ001
    result = Outbox(tmp_path / 'connector', clock=lambda: naive)
    with pytest.raises(ValueError, match='timezone-aware'):
        result.open()

    aware = opened_outbox(tmp_path / 'aware')
    try:
        with pytest.raises(ValueError, match='timezone-aware'):
            aware.recover_expired_leases(naive)
    finally:
        aware.close()


def test_root_database_and_source_symlinks_are_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / 'real-root'
    real_root.mkdir()
    root_link = tmp_path / 'root-link'
    try:
        root_link.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(OutboxError, match='symlink'):
        opened_outbox(root_link)

    root = tmp_path / 'connector'
    root.mkdir()
    target_db = tmp_path / 'target.sqlite3'
    target_db.touch()
    database_path(root).symlink_to(target_db)
    with pytest.raises(OutboxError, match='symlink'):
        opened_outbox(root)

    database_path(root).unlink()
    result = opened_outbox(root)
    source = tmp_path / 'source.alog'
    source.write_bytes(PROFILE_BYTES)
    source_link = tmp_path / 'source-link.alog'
    source_link.symlink_to(source)
    try:
        with pytest.raises(OutboxError, match='symlink'):
            result.snapshot_saved_file(NAMESPACE, source_link)
    finally:
        result.close()

    lock_path = root / '.outbox.lock'
    lock_path.unlink()
    lock_path.symlink_to(tmp_path / 'external-lock')
    with pytest.raises(OutboxError, match='lock.*symlink'):
        opened_outbox(root)


def test_snapshot_open_uses_no_follow_where_available(
    outbox: Outbox, saved_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_follow = getattr(os, 'O_NOFOLLOW', 0)
    if no_follow == 0:
        pytest.skip('O_NOFOLLOW unavailable')
    original_open = os.open
    source_flags: list[int] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path in (saved_profile, saved_profile.name):
            source_flags.append(flags)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'open', recording_open)
    outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert source_flags
    assert all(flags & no_follow for flags in source_flags)


def test_snapshot_bytes_owns_exact_content_and_caller_timestamp(outbox: Outbox) -> None:
    content = repr(
        {'roastUUID': str(ROAST_UUID), 'title': 'serializer-owned revision'}
    ).encode('utf-8')
    modified_at = NOW + timedelta(microseconds=321)

    snapshot = outbox.snapshot_bytes(NAMESPACE, content, modified_at)

    assert snapshot.absolute_path.read_bytes() == content
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert snapshot.byte_count == len(content)
    assert snapshot.source_modified_at == modified_at


@pytest.mark.parametrize('content', [b'x', b'x' * MAX_PROFILE_BYTES])
def test_snapshot_bytes_accepts_both_exact_content_bounds(
    outbox: Outbox, content: bytes
) -> None:
    snapshot = outbox.snapshot_bytes(NAMESPACE, content, NOW)

    assert snapshot.byte_count == len(content)
    assert snapshot.absolute_path.read_bytes() == content


@pytest.mark.parametrize('content', [b'', b'x' * (MAX_PROFILE_BYTES + 1)])
def test_snapshot_bytes_rejects_content_outside_exact_bounds(
    outbox: Outbox, content: bytes
) -> None:
    with pytest.raises(OutboxError, match='supported range'):
        outbox.snapshot_bytes(NAMESPACE, content, NOW)


@pytest.mark.parametrize(
    'modified_at',
    [datetime(2026, 8, 1), '2026-08-01'],  # noqa: DTZ001 - invalid input case
)
def test_snapshot_bytes_requires_aware_caller_timestamp(
    outbox: Outbox, modified_at: object
) -> None:
    with pytest.raises((OutboxError, ValueError)):
        outbox.snapshot_bytes(NAMESPACE, PROFILE_BYTES, cast(datetime, modified_at))



def test_snapshot_is_exact_and_immune_to_source_edits(
    outbox: Outbox, saved_profile: Path
) -> None:
    before = saved_profile.stat()
    snapshot = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    original = snapshot.absolute_path.read_bytes()
    saved_profile.write_bytes(b'changed later')
    assert snapshot.absolute_path.read_bytes() == original == PROFILE_BYTES
    assert snapshot.sha256 == hashlib.sha256(original).hexdigest()
    assert snapshot.byte_count == len(original)
    assert snapshot.source_modified_at == datetime.fromtimestamp(before.st_mtime, tz=UTC)
    assert snapshot.relative_path == (
        f'{NAMESPACE_DIGEST}/snapshots/{snapshot.sha256[:2]}/{snapshot.sha256}.alog'
    )
    assert snapshot.absolute_path == outbox.root / snapshot.relative_path
    assert len(snapshot.staging_token) == 32
    if os.name != 'nt':
        assert stat.S_IMODE(snapshot.absolute_path.stat().st_mode) == 0o400
        assert stat.S_IMODE(snapshot.absolute_path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize('byte_count', [1, 16 * 1024 * 1024])
def test_snapshot_accepts_exact_size_bounds(
    outbox: Outbox, tmp_path: Path, byte_count: int
) -> None:
    source = tmp_path / f'{byte_count}.alog'
    source.write_bytes(b'x' * byte_count)
    snapshot = outbox.snapshot_saved_file(NAMESPACE, source)
    assert snapshot.byte_count == byte_count
    assert snapshot.absolute_path.stat().st_size == byte_count


@pytest.mark.parametrize('byte_count', [0, 16 * 1024 * 1024 + 1])
def test_snapshot_rejects_empty_and_overflow_without_residue(
    outbox: Outbox, tmp_path: Path, byte_count: int
) -> None:
    source = tmp_path / f'{byte_count}.alog'
    source.write_bytes(b'x' * byte_count)
    with pytest.raises(OutboxError, match='size'):
        outbox.snapshot_saved_file(NAMESPACE, source)
    assert not list(outbox.root.rglob('.snapshot-*.tmp'))


def test_snapshot_rejects_source_replacement_during_copy(
    outbox: Outbox,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = os.read
    source_identity = (saved_profile.stat().st_dev, saved_profile.stat().st_ino)
    replacement = saved_profile.with_suffix('.replacement')
    replacement.write_bytes(PROFILE_BYTES)
    replaced = False

    def replacing_read(fd: int, length: int) -> bytes:
        nonlocal replaced
        data = original_read(fd, length)
        descriptor = os.fstat(fd)
        if not replaced and (descriptor.st_dev, descriptor.st_ino) == source_identity:
            replacement.replace(saved_profile)
            replaced = True
        return data

    monkeypatch.setattr(os, 'read', replacing_read)
    with pytest.raises(OutboxError, match='changed'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert replaced
    assert not list(outbox.root.rglob('.snapshot-*.tmp'))


def test_snapshot_rejects_size_change_during_copy(
    outbox: Outbox,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = os.read
    source_identity = (saved_profile.stat().st_dev, saved_profile.stat().st_ino)
    changed = False

    def changing_read(fd: int, length: int) -> bytes:
        nonlocal changed
        data = original_read(fd, length)
        descriptor = os.fstat(fd)
        if not changed and (descriptor.st_dev, descriptor.st_ino) == source_identity:
            with saved_profile.open('ab') as stream:
                stream.write(b'x')
                stream.flush()
                os.fsync(stream.fileno())
            changed = True
        return data

    monkeypatch.setattr(os, 'read', changing_read)
    with pytest.raises(OutboxError, match='changed'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert changed


def test_snapshot_rejects_mtime_change_during_copy(
    outbox: Outbox,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = os.read
    source_identity = (saved_profile.stat().st_dev, saved_profile.stat().st_ino)
    changed = False

    def changing_read(fd: int, length: int) -> bytes:
        nonlocal changed
        data = original_read(fd, length)
        descriptor = os.fstat(fd)
        if not changed and (descriptor.st_dev, descriptor.st_ino) == source_identity:
            os.utime(saved_profile, ns=(descriptor.st_atime_ns, descriptor.st_mtime_ns + 1_000_000_000))
            changed = True
        return data

    monkeypatch.setattr(os, 'read', changing_read)
    with pytest.raises(OutboxError, match='changed'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert changed


def test_atomic_publication_failure_is_redacted_and_rolls_back_stage(
    outbox: Outbox,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = '/private/customer/profile.alog'

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(private_path)

    monkeypatch.setattr(os, 'link', fail_link)
    with pytest.raises(OutboxError) as raised:
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert str(raised.value) == 'saved profile could not be staged'
    assert raised.value.__cause__ is None
    assert private_path not in repr(raised.value)
    assert not list(outbox.root.rglob('.snapshot-*.tmp'))
    connection = sqlite3.connect(database_path(outbox.root))
    try:
        assert connection.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM snapshot_staging').fetchone()[0] == 0
    finally:
        connection.close()


def test_existing_wrong_content_snapshot_is_never_replaced(
    outbox: Outbox, saved_profile: Path
) -> None:
    sha256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
    existing = (
        outbox.root
        / NAMESPACE_DIGEST
        / 'snapshots'
        / sha256[:2]
        / f'{sha256}.alog'
    )
    existing.parent.mkdir(parents=True, mode=0o700)
    existing.write_bytes(b'x' * len(PROFILE_BYTES))
    existing.chmod(0o400)
    before = existing.stat()
    with pytest.raises(OutboxError, match='snapshot content'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    after = existing.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert existing.read_bytes() == b'x' * len(PROFILE_BYTES)


def test_generated_destination_symlink_is_rejected(outbox: Outbox, saved_profile: Path) -> None:
    namespace_directory = outbox.root / NAMESPACE_DIGEST
    target = outbox.root / 'elsewhere'
    target.mkdir()
    try:
        namespace_directory.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(OutboxError, match='symlink'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)


def test_invalid_namespace_keys_are_rejected(outbox: Outbox, saved_profile: Path) -> None:
    for key in (
        '../escape',
        'namespace-sha256:' + 'a' * 63,
        'namespace-sha256:' + 'A' * 64,
        'namespace-sha256:' + 'g' * 64,
    ):
        malformed = Namespace(NAMESPACE.origin, NAMESPACE.organization_id, key)
        with pytest.raises(ValueError, match='namespace'):
            outbox.snapshot_saved_file(malformed, saved_profile)
    assert not (outbox.root.parent / 'escape').exists()


def test_duplicate_uuid_hash_resolves_one_job_and_one_snapshot(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox)
    second = enqueue_fixture(outbox)
    assert first.job.id == second.job.id
    assert first.created and not second.created
    assert outbox.counts(NAMESPACE).pending == 1
    connection = sqlite3.connect(database_path(outbox.root))
    try:
        assert connection.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 1
    finally:
        connection.close()


def test_idempotency_key_and_uuid_storage_are_exact(outbox: Outbox) -> None:
    result = enqueue_fixture(outbox)
    job = result.job
    assert job.idempotency_key == (
        f'archive-v1:{CLIENT_UUID.hex}:{ROAST_UUID.hex}:{hashlib.sha256(PROFILE_BYTES).hexdigest()}'
    )
    assert len(job.id) == 32 and job.id == job.id.lower()
    connection = sqlite3.connect(database_path(outbox.root))
    try:
        stored = connection.execute(
            'SELECT roast_uuid, idempotency_key FROM jobs WHERE id = ?', (job.id,)
        ).fetchone()
    finally:
        connection.close()
    assert stored == (ROAST_UUID.hex, job.idempotency_key)


def test_namespace_isolation_for_dedup_leases_counts_and_failures(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox)
    second = enqueue_fixture(outbox, namespace=OTHER_NAMESPACE)
    assert first.job.id != second.job.id
    assert outbox.counts(NAMESPACE).pending == 1
    assert outbox.counts(OTHER_NAMESPACE).pending == 1
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.id == first.job.id
    assert outbox.lease_next(NAMESPACE, NOW) is None
    other = outbox.lease_next(OTHER_NAMESPACE, NOW)
    assert other is not None and other.id == second.job.id
    assert leased.lease_token is not None
    outbox.mark_failed(leased.id, leased.lease_token, NOW, FAILURE)
    assert len(outbox.failed_jobs(NAMESPACE)) == 1
    assert outbox.failed_jobs(OTHER_NAMESPACE) == ()


def test_expired_lease_recovers_after_restart(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    job = enqueue_fixture(first).job
    leased = first.lease_next(NAMESPACE, NOW, lease_seconds=60)
    assert leased is not None and leased.id == job.id and leased.attempts == 1
    first.close()
    second = opened_outbox(root)
    try:
        assert second.recover_expired_leases(NOW + timedelta(seconds=61)) == 1
        recovered = second.lease_next(NAMESPACE, NOW + timedelta(seconds=61))
        assert recovered is not None and recovered.id == job.id and recovered.attempts == 2
    finally:
        second.close()


def test_retry_fields_and_attempts_persist_across_restart(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    job = enqueue_fixture(first).job
    leased = first.lease_next(NAMESPACE, NOW)
    assert leased is not None
    retry_at = NOW + timedelta(minutes=5)
    assert leased.lease_token is not None
    first.mark_retry(job.id, leased.lease_token, NOW, retry_at, FAILURE)
    first.close()

    second = opened_outbox(root)
    try:
        assert second.lease_next(NAMESPACE, retry_at - timedelta(microseconds=1)) is None
        retried = second.lease_next(NAMESPACE, retry_at)
        assert retried is not None
        assert retried.id == job.id
        assert retried.attempts == 2
        assert retried.next_attempt_at is None
        assert retried.error_code is None
    finally:
        second.close()


def test_pause_resume_retry_now_failed_details_and_counts(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox).job
    second = enqueue_fixture(outbox, roast_uuid=OTHER_ROAST_UUID).job
    leased_first = outbox.lease_next(NAMESPACE, NOW)
    assert leased_first is not None and leased_first.id == first.id
    assert leased_first.lease_token is not None
    outbox.mark_retry(
        first.id, leased_first.lease_token, NOW, NOW + timedelta(hours=1), FAILURE
    )
    leased_second = outbox.lease_next(NAMESPACE, NOW)
    assert leased_second is not None and leased_second.id == second.id

    assert outbox.pause_namespace(NAMESPACE, NOW, 'credential_rejected') == 2
    counts = outbox.counts(NAMESPACE)
    assert counts.pending == 0 and counts.retrying == 0 and counts.paused == 2
    assert outbox.resume_namespace(NAMESPACE, NOW) == 2
    counts = outbox.counts(NAMESPACE)
    assert counts.pending == 1 and counts.retrying == 1 and counts.paused == 0

    outbox.retry_now(first.id, NOW)
    retried = outbox.lease_next(NAMESPACE, NOW)
    assert retried is not None and retried.id == first.id
    untrusted = PublicFailure(
        kind=FailureKind.OFFLINE,
        code='offline',
        message='/private/customer/profile.alog',
        retryable=False,
    )
    assert retried.lease_token is not None
    outbox.mark_failed(first.id, retried.lease_token, NOW, untrusted)
    failed = outbox.failed_jobs(NAMESPACE)
    assert len(failed) == 1
    assert failed[0].id == first.id
    assert failed[0].error_code == 'offline'
    assert failed[0].error_message == FAILURE_MESSAGES[FailureKind.OFFLINE]
    counts = outbox.counts(NAMESPACE)
    assert counts.pending == 1 and counts.failed == 1

    outbox.retry_now(first.id, NOW)
    assert outbox.failed_jobs(NAMESPACE) == ()
    assert outbox.counts(NAMESPACE).pending == 2


def test_complete_releases_ownership_and_completed_job_deduplicates(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox)
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None
    snapshot_path = leased.snapshot_path
    assert snapshot_path is not None
    assert leased.lease_token is not None
    outbox.mark_complete(leased.id, leased.lease_token, NOW)
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_complete(leased.id, leased.lease_token, NOW + timedelta(seconds=1))
    counts = outbox.counts(NAMESPACE)
    assert counts.complete == 1 and counts.pending == 0
    assert not snapshot_path.exists()

    duplicate = enqueue_fixture(outbox)
    assert not duplicate.created
    assert duplicate.job.id == first.job.id
    assert duplicate.job.state == 'complete'
    assert duplicate.job.snapshot_sha256 is None
    assert duplicate.job.snapshot_path is None
    assert duplicate.job.snapshot_byte_count is None


def test_remove_and_complete_preserve_shared_snapshot_references(outbox: Outbox) -> None:
    source = outbox.root.parent / 'shared.alog'
    source.write_bytes(PROFILE_BYTES)
    first_stage = outbox.snapshot_saved_file(NAMESPACE, source)
    second_stage = outbox.snapshot_saved_file(NAMESPACE, source)
    first = outbox.enqueue(NAMESPACE, first_stage, ROAST_UUID, METADATA, CLIENT_UUID).job
    second = outbox.enqueue(
        NAMESPACE, second_stage, OTHER_ROAST_UUID, METADATA, CLIENT_UUID
    ).job
    snapshot = first_stage
    assert outbox.protected_paths(NAMESPACE) == frozenset({snapshot.absolute_path})

    outbox.remove(first.id)
    assert snapshot.absolute_path.exists()
    assert outbox.protected_paths(NAMESPACE) == frozenset({snapshot.absolute_path})
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.id == second.id
    assert leased.lease_token is not None
    outbox.mark_complete(second.id, leased.lease_token, NOW)
    assert not snapshot.absolute_path.exists()
    assert outbox.protected_paths(NAMESPACE) == frozenset()


def test_retry_remove_and_state_transitions_reject_unknown_or_unleased_jobs(outbox: Outbox) -> None:
    job = enqueue_fixture(outbox).job
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_retry(job.id, '0' * 32, NOW, NOW + timedelta(seconds=1), FAILURE)
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_failed('0' * 32, '0' * 32, NOW, FAILURE)
    with pytest.raises(ValueError):
        outbox.mark_retry(job.id, '0' * 32, NOW, NOW - timedelta(seconds=1), FAILURE)
    outbox.remove(job.id)
    outbox.remove(job.id)
    assert outbox.counts(NAMESPACE).pending == 0


def test_stored_traversal_and_snapshot_symlink_are_rejected(outbox: Outbox) -> None:
    job = enqueue_fixture(outbox).job
    connection = sqlite3.connect(database_path(outbox.root))
    connection.execute(
        "UPDATE jobs SET snapshot_relative_path = '../escape.alog' WHERE id = ?", (job.id,)
    )
    connection.commit()
    connection.close()
    with pytest.raises(OutboxError, match='path'):
        outbox.lease_next(NAMESPACE, NOW)

    assert job.snapshot_path is not None
    connection = sqlite3.connect(database_path(outbox.root))
    connection.execute(
        'UPDATE jobs SET snapshot_relative_path = ? WHERE id = ?',
        (job.snapshot_path.relative_to(outbox.root).as_posix(), job.id),
    )
    connection.commit()
    connection.close()
    job.snapshot_path.unlink()
    target = outbox.root.parent / 'outside.alog'
    target.write_bytes(PROFILE_BYTES)
    job.snapshot_path.symlink_to(target)
    with pytest.raises(OutboxError, match='symlink'):
        outbox.protected_paths(NAMESPACE)


def test_restart_preserves_unqueued_stage_until_expiry_then_collects_it(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    snapshot = first.snapshot_saved_file(NAMESPACE, saved_profile)
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM snapshot_staging').fetchone()[0] == 1
    finally:
        connection.close()
    first.close()

    second = opened_outbox(root)
    second.close()
    assert snapshot.absolute_path.exists()

    expired = opened_outbox(root, now=NOW + timedelta(days=1))
    expired.close()
    assert not snapshot.absolute_path.exists()


def test_startup_collects_unindexed_generated_files_and_atomic_temps(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    first.close()
    digest = 'a' * 64
    generated = root / NAMESPACE_DIGEST / 'snapshots' / digest[:2] / f'{digest}.alog'
    generated.parent.mkdir(parents=True, mode=0o700)
    generated.write_bytes(b'orphan')
    temporary = generated.parent / '.snapshot-deadbeef.tmp'
    temporary.write_bytes(b'partial')

    second = opened_outbox(root)
    second.close()
    assert not generated.exists()
    assert not temporary.exists()


def test_cleanup_cannot_unlink_a_concurrently_recreated_snapshot(
    tmp_path: Path,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)
    initial = first.snapshot_saved_file(NAMESPACE, saved_profile)
    job = first.enqueue(NAMESPACE, initial, ROAST_UUID, METADATA, CLIENT_UUID).job
    leased = first.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.lease_token is not None
    unlink_entered = Event()
    allow_unlink = Event()
    snapshot_attempted = Event()
    original_unlink = first._unlink_generated_snapshot

    def blocking_unlink(relative_path: str) -> None:
        unlink_entered.set()
        assert allow_unlink.wait(timeout=5)
        original_unlink(relative_path)

    def resnapshot() -> Snapshot:
        snapshot_attempted.set()
        return second.snapshot_saved_file(NAMESPACE, saved_profile)

    monkeypatch.setattr(first, '_unlink_generated_snapshot', blocking_unlink)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            completion = executor.submit(
                first.mark_complete, job.id, leased.lease_token, NOW
            )
            assert unlink_entered.wait(timeout=5)
            recreated_future = executor.submit(resnapshot)
            assert snapshot_attempted.wait(timeout=5)
            assert not recreated_future.done()
            allow_unlink.set()
            completion.result(timeout=5)
            recreated = recreated_future.result(timeout=5)
        new_job = second.enqueue(
            NAMESPACE, recreated, OTHER_ROAST_UUID, METADATA, CLIENT_UUID
        ).job
        assert new_job.snapshot_path == recreated.absolute_path
        assert recreated.absolute_path.exists()
    finally:
        allow_unlink.set()
        first.close()
        second.close()


def test_process_open_between_snapshot_and_enqueue_cannot_collect_live_stage(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    snapshot = first.snapshot_saved_file(NAMESPACE, saved_profile)
    ready = tmp_path / 'opened'
    release = tmp_path / 'release'
    script = f'''\
from datetime import UTC, datetime
from pathlib import Path
import sys
import time
from artisanlib.roastserver.outbox import Outbox
root, ready, release = map(Path, sys.argv[1:])
now = datetime.fromisoformat({NOW.isoformat()!r}).astimezone(UTC)
outbox = Outbox(root, clock=lambda: now)
outbox.open()
ready.write_text('open', encoding='ascii')
try:
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not release.exists():
        raise RuntimeError('barrier timed out')
finally:
    outbox.close()
'''
    process = subprocess.Popen(
        [sys.executable, '-c', script, os.fspath(root), os.fspath(ready), os.fspath(release)],
        text=True,
    )
    try:
        for _ in range(500):
            if ready.exists():
                break
            if process.poll() is not None:
                raise AssertionError(f'barrier process exited {process.returncode}')
            Event().wait(0.01)
        assert ready.exists()
        assert snapshot.absolute_path.exists()
        result = first.enqueue(NAMESPACE, snapshot, ROAST_UUID, METADATA, CLIENT_UUID)
        assert result.created
    finally:
        release.touch()
        assert process.wait(timeout=10) == 0
        first.close()


def test_processes_contending_to_publish_same_hash_keep_both_stage_owners(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    setup = opened_outbox(root)
    setup.close()
    go = tmp_path / 'go'
    script = f'''\
from datetime import UTC, datetime
from pathlib import Path
import sys
import time
from uuid import UUID
from artisanlib.roastserver.contract import Namespace
from artisanlib.roastserver.outbox import Outbox
root, source, ready, go, result = map(Path, sys.argv[1:])
namespace = Namespace(
    origin={NAMESPACE.origin!r},
    organization_id=UUID({str(NAMESPACE.organization_id)!r}),
    key={NAMESPACE.key!r},
)
now = datetime.fromisoformat({NOW.isoformat()!r}).astimezone(UTC)
outbox = Outbox(root, clock=lambda: now)
outbox.open()
ready.touch()
try:
    deadline = time.monotonic() + 10
    while not go.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not go.exists():
        raise RuntimeError('barrier timed out')
    snapshot = outbox.snapshot_saved_file(namespace, source)
    result.write_text(snapshot.staging_token, encoding='ascii')
finally:
    outbox.close()
'''
    processes: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    result_paths: list[Path] = []
    for index in range(2):
        ready = tmp_path / f'ready-{index}'
        result = tmp_path / f'result-{index}'
        ready_paths.append(ready)
        result_paths.append(result)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    script,
                    os.fspath(root),
                    os.fspath(saved_profile),
                    os.fspath(ready),
                    os.fspath(go),
                    os.fspath(result),
                ],
                text=True,
            )
        )
    try:
        for _ in range(1_000):
            if all(path.exists() for path in ready_paths):
                break
            if any(process.poll() is not None for process in processes):
                raise AssertionError('publication process exited before barrier')
            Event().wait(0.01)
        assert all(path.exists() for path in ready_paths)
        go.touch()
        assert [process.wait(timeout=10) for process in processes] == [0, 0]
        tokens = {path.read_text(encoding='ascii') for path in result_paths}
        assert len(tokens) == 2
        connection = sqlite3.connect(database_path(root))
        try:
            assert connection.execute('SELECT count(*) FROM snapshot_staging').fetchone()[0] == 2
            relative_path = connection.execute(
                'SELECT relative_path FROM snapshots'
            ).fetchone()[0]
        finally:
            connection.close()
        assert (root / relative_path).read_bytes() == PROFILE_BYTES
    finally:
        go.touch(exist_ok=True)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_concurrent_same_hash_stages_have_distinct_owners_and_preserve_inode(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)
    try:
        initial = first.snapshot_saved_file(NAMESPACE, saved_profile)
        initial_stat = initial.absolute_path.stat()
        descriptor = os.open(initial.absolute_path, os.O_RDONLY)
        try:
            concurrent = second.snapshot_saved_file(NAMESPACE, saved_profile)
            assert concurrent.staging_token != initial.staging_token
            assert concurrent.sha256 == initial.sha256
            assert concurrent.absolute_path.stat().st_ino == initial_stat.st_ino
            assert os.read(descriptor, len(PROFILE_BYTES)) == PROFILE_BYTES
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(database_path(root))
        try:
            assert connection.execute('SELECT count(*) FROM snapshot_staging').fetchone()[0] == 2
        finally:
            connection.close()
    finally:
        first.close()
        second.close()


def test_completion_does_not_collect_snapshot_owned_by_an_unexpired_stage(
    outbox: Outbox, saved_profile: Path
) -> None:
    first_stage = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    second_stage = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    first = outbox.enqueue(NAMESPACE, first_stage, ROAST_UUID, METADATA, CLIENT_UUID).job
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.lease_token is not None
    outbox.mark_complete(first.id, leased.lease_token, NOW)
    assert second_stage.absolute_path.exists()
    second = outbox.enqueue(
        NAMESPACE, second_stage, OTHER_ROAST_UUID, METADATA, CLIENT_UUID
    )
    assert second.created and second.job.snapshot_path == second_stage.absolute_path


def test_discard_staged_snapshot_consumes_exact_token_and_releases_reference(
    outbox: Outbox,
    saved_profile: Path,
) -> None:
    first = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    second = outbox.snapshot_saved_file(NAMESPACE, saved_profile)

    outbox.discard_staged_snapshot(first)
    assert first.absolute_path.exists()
    outbox.discard_staged_snapshot(first)
    assert first.absolute_path.exists()
    outbox.discard_staged_snapshot(second)
    assert not first.absolute_path.exists()


def test_discard_holds_process_lock_through_unlink_before_same_hash_republishes(
    tmp_path: Path,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)
    snapshot = first.snapshot_saved_file(NAMESPACE, saved_profile)
    unlink_reached = Event()
    second_lock_attempted = Event()
    unlink_finished = Event()
    first_lock_held = False
    second_acquired_after_unlink: list[bool] = []
    original_first_lock = first._filesystem_lock
    original_second_lock = second._filesystem_lock
    original_unlink = first._unlink_generated_snapshot

    @contextmanager
    def tracked_first_lock() -> Generator[None]:
        nonlocal first_lock_held
        with original_first_lock():
            first_lock_held = True
            try:
                yield
            finally:
                first_lock_held = False

    @contextmanager
    def observed_second_lock() -> Generator[None]:
        second_lock_attempted.set()
        with original_second_lock():
            second_acquired_after_unlink.append(unlink_finished.is_set())
            yield

    def blocked_unlink(relative_path: str) -> None:
        unlink_reached.set()
        assert second_lock_attempted.wait(5)
        assert first_lock_held
        connection = sqlite3.connect(database_path(root))
        try:
            assert connection.execute('SELECT count(*) FROM snapshot_staging').fetchone() == (0,)
            assert connection.execute('SELECT count(*) FROM snapshots').fetchone() == (0,)
        finally:
            connection.close()
        original_unlink(relative_path)
        unlink_finished.set()

    monkeypatch.setattr(first, '_filesystem_lock', tracked_first_lock)
    monkeypatch.setattr(second, '_filesystem_lock', observed_second_lock)
    monkeypatch.setattr(first, '_unlink_generated_snapshot', blocked_unlink)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            discarded = executor.submit(first.discard_staged_snapshot, snapshot)
            assert unlink_reached.wait(5)
            restaged = executor.submit(
                second.snapshot_saved_file, NAMESPACE, saved_profile
            )
            discarded.result(timeout=5)
            replacement = restaged.result(timeout=5)

        assert second_acquired_after_unlink == [True]
        assert replacement.absolute_path.read_bytes() == PROFILE_BYTES
        second.discard_staged_snapshot(replacement)
    finally:
        first.close()
        second.close()


def test_enqueue_consumes_exact_unexpired_stage_token_once(
    outbox: Outbox, saved_profile: Path
) -> None:
    snapshot = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    outbox.enqueue(NAMESPACE, snapshot, ROAST_UUID, METADATA, CLIENT_UUID)
    with pytest.raises(OutboxError, match='staging token'):
        outbox.enqueue(NAMESPACE, snapshot, OTHER_ROAST_UUID, METADATA, CLIENT_UUID)


def test_expired_stage_token_cannot_enqueue(tmp_path: Path, saved_profile: Path) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    snapshot = first.snapshot_saved_file(NAMESPACE, saved_profile)
    first.close()
    expired = opened_outbox(root, now=NOW + timedelta(days=1))
    try:
        with pytest.raises(OutboxError, match='staging token'):
            expired.enqueue(NAMESPACE, snapshot, ROAST_UUID, METADATA, CLIENT_UUID)
    finally:
        expired.close()


def test_stale_worker_cannot_commit_recovered_and_released_attempt(outbox: Outbox) -> None:
    job = enqueue_fixture(outbox).job
    attempt_a = outbox.lease_next(NAMESPACE, NOW, lease_seconds=10)
    assert attempt_a is not None and attempt_a.lease_token is not None
    later = NOW + timedelta(seconds=11)
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_retry(
            job.id,
            attempt_a.lease_token,
            later,
            later + timedelta(seconds=1),
            FAILURE,
        )
    assert outbox.recover_expired_leases(later) == 1
    attempt_b = outbox.lease_next(NAMESPACE, later)
    assert attempt_b is not None and attempt_b.lease_token is not None
    assert attempt_b.lease_token != attempt_a.lease_token
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_complete(job.id, attempt_a.lease_token, later)
    outbox.mark_complete(job.id, attempt_b.lease_token, later)
    assert outbox.counts(NAMESPACE).complete == 1


def test_pause_and_remove_invalidate_active_lease(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox).job
    attempt = outbox.lease_next(NAMESPACE, NOW)
    assert attempt is not None and attempt.lease_token is not None
    assert outbox.pause_namespace(NAMESPACE, NOW, 'credential_rejected') == 1
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_failed(first.id, attempt.lease_token, NOW, FAILURE)
    outbox.remove(first.id)
    with pytest.raises(OutboxError, match='^lease_lost$'):
        outbox.mark_complete(first.id, attempt.lease_token, NOW)


@pytest.mark.parametrize('lease_seconds', [True, 1.5, '60', 0, -1, 86_401])
def test_lease_seconds_requires_exact_bounded_integer(
    outbox: Outbox, lease_seconds: object
) -> None:
    with pytest.raises(ValueError, match='lease_seconds'):
        outbox.lease_next(NAMESPACE, NOW, cast('int', lease_seconds))


@pytest.mark.parametrize(
    'corruption',
    ['missing', 'hash', 'size', 'permission'],
)
def test_snapshot_corruption_atomically_fails_exact_fenced_candidate(
    outbox: Outbox,
    corruption: str,
) -> None:
    job = enqueue_fixture(outbox).job
    assert job.snapshot_path is not None
    if corruption == 'missing':
        job.snapshot_path.unlink()
    elif corruption == 'permission':
        if os.name == 'nt':
            pytest.skip('POSIX mode corruption case')
        job.snapshot_path.chmod(0o600)
    else:
        if os.name != 'nt':
            job.snapshot_path.chmod(0o600)
        replacement = b'x' * (
            len(PROFILE_BYTES) if corruption == 'hash' else len(PROFILE_BYTES) + 1
        )
        job.snapshot_path.write_bytes(replacement)
        if os.name != 'nt':
            job.snapshot_path.chmod(0o400)

    outcome = outbox.lease_next(NAMESPACE, NOW)

    assert isinstance(outcome, LeaseFailure)
    assert outcome.job.id == job.id
    assert outcome.job.lease_token is not None
    assert outcome.failure == PublicFailure(
        kind=FailureKind.LOCAL_PROFILE,
        code=FailureKind.LOCAL_PROFILE.value,
        message=FAILURE_MESSAGES[FailureKind.LOCAL_PROFILE],
        retryable=False,
    )
    assert outcome.job.attempts == 1
    counts = outbox.counts(NAMESPACE)
    assert counts.pending == 0 and counts.failed == 1
    failed = outbox.failed_jobs(NAMESPACE)
    assert len(failed) == 1
    assert failed[0].id == job.id
    assert failed[0].error_code == FailureKind.LOCAL_PROFILE.value
    assert failed[0].error_message == FAILURE_MESSAGES[FailureKind.LOCAL_PROFILE]
    outbox.remove(job.id)
    assert outbox.counts(NAMESPACE).failed == 0
    assert not job.snapshot_path.exists()


def test_corrupt_candidate_failure_preserves_shared_snapshot_reference(
    outbox: Outbox,
) -> None:
    source = outbox.root.parent / 'shared-corrupt.alog'
    source.write_bytes(PROFILE_BYTES)
    first_stage = outbox.snapshot_saved_file(NAMESPACE, source)
    second_stage = outbox.snapshot_saved_file(NAMESPACE, source)
    first = outbox.enqueue(
        NAMESPACE, first_stage, ROAST_UUID, METADATA, CLIENT_UUID
    ).job
    second = outbox.enqueue(
        NAMESPACE, second_stage, OTHER_ROAST_UUID, METADATA, CLIENT_UUID
    ).job
    assert first.snapshot_path is not None
    if os.name != 'nt':
        first.snapshot_path.chmod(0o600)
    first.snapshot_path.write_bytes(b'x' * len(PROFILE_BYTES))
    if os.name != 'nt':
        first.snapshot_path.chmod(0o400)

    outcome = outbox.lease_next(NAMESPACE, NOW)
    assert isinstance(outcome, LeaseFailure)
    assert outcome.job.id == first.id
    outbox.remove(first.id)

    assert first.snapshot_path.exists()
    assert outbox.counts(NAMESPACE).pending == 1
    second_outcome = outbox.lease_next(NAMESPACE, NOW)
    assert isinstance(second_outcome, LeaseFailure)
    assert second_outcome.job.id == second.id


def test_next_due_at_is_namespace_scoped_and_uses_persisted_state_times(
    outbox: Outbox,
) -> None:
    assert outbox.next_due_at(NAMESPACE) is None
    job = enqueue_fixture(outbox).job
    assert outbox.next_due_at(NAMESPACE) == job.created_at
    assert outbox.next_due_at(OTHER_NAMESPACE) is None

    leased = outbox.lease_next(NAMESPACE, NOW, lease_seconds=60)
    assert isinstance(leased, Job)
    assert leased.lease_token is not None
    assert outbox.next_due_at(NAMESPACE) == NOW + timedelta(seconds=60)
    retry_at = NOW + timedelta(minutes=7, microseconds=321)
    outbox.mark_retry(job.id, leased.lease_token, NOW, retry_at, FAILURE)
    assert outbox.next_due_at(NAMESPACE) == retry_at


@pytest.mark.parametrize(
    ('aroast', 'revision'),
    [
        (b'{"a":1,"a":2}', METADATA.revision_json),
        (b'[]', METADATA.revision_json),
        (b'{"b":1,"a":2}', METADATA.revision_json),
        (METADATA.aroast_json, b'{"value":NaN}'),
    ],
)
def test_enqueue_rejects_noncanonical_or_duplicate_metadata_json(
    outbox: Outbox, saved_profile: Path, aroast: bytes, revision: bytes
) -> None:
    snapshot = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    malformed = cast('ProjectedMetadata', _Metadata(aroast, revision))
    with pytest.raises(ValueError, match='metadata'):
        outbox.enqueue(NAMESPACE, snapshot, ROAST_UUID, malformed, CLIENT_UUID)


def test_malformed_stored_json_is_rejected_on_read(outbox: Outbox) -> None:
    job = enqueue_fixture(outbox).job
    connection = sqlite3.connect(database_path(outbox.root))
    connection.execute('UPDATE jobs SET aroast_json = ? WHERE id = ?', ('[]', job.id))
    connection.commit()
    connection.close()
    with pytest.raises(OutboxError, match='metadata'):
        outbox.lease_next(NAMESPACE, NOW)


@pytest.mark.parametrize('owner', ['stage', 'job'])
def test_durable_owner_byte_count_must_match_snapshot_row(
    outbox: Outbox, saved_profile: Path, owner: str
) -> None:
    snapshot = outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    if owner == 'job':
        outbox.enqueue(NAMESPACE, snapshot, ROAST_UUID, METADATA, CLIENT_UUID)
    outbox.close()
    connection = sqlite3.connect(database_path(outbox.root))
    connection.execute(
        f'UPDATE {"snapshot_staging" if owner == "stage" else "jobs"} '
        f'SET {"byte_count" if owner == "stage" else "snapshot_byte_count"} = ?',
        (snapshot.byte_count + 1,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(OutboxError, match='snapshot|byte count|ownership'):
        opened_outbox(outbox.root)


def test_failure_fields_and_pause_codes_reject_nonallowlisted_controls(outbox: Outbox) -> None:
    job = enqueue_fixture(outbox).job
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.lease_token is not None
    bad = PublicFailure(
        kind=FailureKind.OFFLINE,
        code='../private-path',
        message='line\nsecret',
        retryable=True,
    )
    with pytest.raises(ValueError, match='failure'):
        outbox.mark_failed(job.id, leased.lease_token, NOW, bad)
    with pytest.raises(ValueError, match='pause code'):
        outbox.pause_namespace(NAMESPACE, NOW, 'credential\nrejected')


def test_directory_fsync_failure_before_stage_commit_is_propagated_and_cleaned(
    outbox: Outbox, saved_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = outbox_module._fsync_descriptor

    def fail_directory(descriptor: int, *, directory: bool = False) -> None:
        if directory:
            raise OSError(errno.EIO, '/private/customer/profile.alog')
        original(descriptor, directory=directory)

    monkeypatch.setattr(outbox_module, '_fsync_descriptor', fail_directory)
    with pytest.raises(OutboxError, match='saved profile could not be staged'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert not list(outbox.root.rglob('.snapshot-*.tmp'))


def test_permission_failure_during_publication_fails_closed(
    outbox: Outbox, saved_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = outbox_module._set_private_permissions

    def fail_snapshot(path: Path, mode: int) -> None:
        if mode == 0o400:
            raise OutboxError('private connector permissions could not be applied')
        original(path, mode)

    monkeypatch.setattr(outbox_module, '_set_private_permissions', fail_snapshot)
    with pytest.raises(OutboxError, match='permissions'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)


def test_directory_fsync_failure_after_unlink_is_propagated(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = enqueue_fixture(outbox).job
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.lease_token is not None
    original = outbox_module._fsync_descriptor

    def fail_directory(descriptor: int, *, directory: bool = False) -> None:
        if directory:
            raise OSError(errno.EIO, 'directory sync failed')
        original(descriptor, directory=directory)

    monkeypatch.setattr(outbox_module, '_fsync_descriptor', fail_directory)
    with pytest.raises(OutboxError, match='outbox storage operation failed'):
        outbox.mark_complete(job.id, leased.lease_token, NOW)
    assert outbox.counts(NAMESPACE).complete == 1


def test_windows_native_directory_flush_uses_write_access_flags_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_calls: list[tuple[object, ...]] = []
    closed: list[int] = []
    flush_result = True
    last_error = 0

    def create_file(*arguments: object) -> int:
        create_calls.append(arguments)
        return 100 + len(create_calls)

    def close_handle(handle: int) -> bool:
        closed.append(handle)
        return True

    def flush_file_buffers(_handle: int) -> bool:
        return flush_result

    kernel32 = SimpleNamespace(
        CreateFileW=create_file,
        CloseHandle=close_handle,
        FlushFileBuffers=flush_file_buffers,
    )
    layer = object.__new__(outbox_module._WindowsNativeLayer)
    layer._kernel32 = kernel32
    layer._ctypes = SimpleNamespace(get_last_error=lambda: last_error)
    layer._invalid_handle = -1

    def directory_attributes(_handle: int) -> int:
        return stat.FILE_ATTRIBUTE_DIRECTORY

    monkeypatch.setattr(layer, '_attributes', directory_attributes)

    layer.flush_directory(tmp_path)
    assert create_calls
    assert create_calls[-1][1] == layer._GENERIC_WRITE | layer._SYNCHRONIZE
    assert all(
        call[5]
        == layer._FILE_FLAG_BACKUP_SEMANTICS | layer._FILE_FLAG_OPEN_REPARSE_POINT
        for call in create_calls
    )
    assert closed

    flush_result = False
    last_error = 5
    with pytest.raises(OSError) as raised:
        layer.flush_directory(tmp_path)
    assert raised.value.errno == 5


def test_windows_native_api_prototypes_match_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeFunction:
        argtypes: list[object] | None = None
        restype: object = None

    class FakeLibrary:
        def __init__(self) -> None:
            self.functions: dict[str, FakeFunction] = {}

        def __getattr__(self, name: str) -> FakeFunction:
            return self.functions.setdefault(name, FakeFunction())

    libraries = {'kernel32': FakeLibrary(), 'advapi32': FakeLibrary()}

    def win_dll(name: str, **_kwargs: object) -> FakeLibrary:
        return libraries[name]

    monkeypatch.setitem(ctypes.__dict__, 'WinDLL', win_dll)
    layer = outbox_module._WindowsNativeLayer()

    set_information = libraries['kernel32'].SetFileInformationByHandle
    assert set_information.argtypes == [
        outbox_module.wintypes.HANDLE,
        ctypes.c_int,
        outbox_module.wintypes.LPVOID,
        outbox_module.wintypes.DWORD,
    ]
    assert set_information.restype is outbox_module.wintypes.BOOL
    assert libraries['advapi32'].GetLengthSid.argtypes == [outbox_module.wintypes.LPVOID]
    assert libraries['advapi32'].GetLengthSid.restype is outbox_module.wintypes.DWORD
    assert libraries['advapi32'].IsValidSid.argtypes == [outbox_module.wintypes.LPVOID]
    assert libraries['advapi32'].IsValidSid.restype is outbox_module.wintypes.BOOL
    assert libraries['kernel32'].GetFinalPathNameByHandleW.argtypes == [
        outbox_module.wintypes.HANDLE,
        outbox_module.wintypes.LPWSTR,
        outbox_module.wintypes.DWORD,
        outbox_module.wintypes.DWORD,
    ]
    assert libraries['kernel32'].GetFinalPathNameByHandleW.restype is (
        outbox_module.wintypes.DWORD)
    assert layer._file_disposition_info is outbox_module._WindowsFileDispositionInfo


def test_windows_native_canonical_path_uses_retained_handle_and_volume_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = r'\\?\Volume{11111111-1111-1111-1111-111111111111}\safe\parent'
    calls: list[tuple[int, int, int]] = []

    def final_path(
        handle: int,
        buffer: Any,
        size: int,
        flags: int,
    ) -> int:
        calls.append((handle, size, flags))
        if buffer is None:
            return len(canonical)
        buffer.value = canonical
        return len(canonical)

    layer = object.__new__(outbox_module._WindowsNativeLayer)
    layer._kernel32 = SimpleNamespace(GetFinalPathNameByHandleW=final_path)
    layer._ctypes = ctypes
    monkeypatch.setattr(layer, '_attributes', lambda _handle: 0)
    fake_msvcrt = SimpleNamespace(get_osfhandle=lambda descriptor: descriptor + 100)
    original_import = outbox_module.importlib.import_module
    monkeypatch.setattr(
        outbox_module.importlib,
        'import_module',
        lambda name: fake_msvcrt if name == 'msvcrt' else original_import(name),
    )

    assert layer.canonical_path(23) == Path(canonical)
    expected_flags = layer._FILE_NAME_NORMALIZED | layer._VOLUME_NAME_GUID
    assert calls == [(123, 0, expected_flags), (123, len(canonical) + 1, expected_flags)]

    monkeypatch.setattr(
        layer,
        '_attributes',
        lambda _handle: layer._FILE_ATTRIBUTE_REPARSE_POINT,
    )
    with pytest.raises(OSError, match='reparse'):
        layer.canonical_path(23)


def test_windows_native_unlink_uses_exact_file_disposition_info_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    information_calls: list[tuple[int, int, int]] = []
    readonly_calls: list[tuple[int, bool]] = []
    closed: list[int] = []
    set_result = True
    last_error = 0

    def set_file_information(
        _handle: int, information_class: int, information: Any, size: int
    ) -> bool:
        parsed = ctypes.cast(
            information, ctypes.POINTER(outbox_module._WindowsFileDispositionInfo)
        ).contents
        information_calls.append((information_class, size, int(parsed.DeleteFile)))
        return set_result

    def close_handle(handle: int) -> bool:
        closed.append(handle)
        return True

    layer = object.__new__(outbox_module._WindowsNativeLayer)
    layer._kernel32 = SimpleNamespace(
        SetFileInformationByHandle=set_file_information,
        CloseHandle=close_handle,
    )
    layer._ctypes = SimpleNamespace(
        byref=ctypes.byref,
        sizeof=ctypes.sizeof,
        get_last_error=lambda: last_error,
    )
    layer._file_disposition_info = outbox_module._WindowsFileDispositionInfo

    def open_chain(
        _path: Path, *, final_access: int, final_disposition: int = layer._OPEN_EXISTING
    ) -> list[int]:
        del final_access, final_disposition
        return [10, 20, 30]

    def set_readonly(handle: int, value: bool) -> None:
        readonly_calls.append((handle, value))

    monkeypatch.setattr(layer, '_open_chain', open_chain)
    monkeypatch.setattr(layer, '_set_readonly', set_readonly)

    assert outbox_module._WindowsFileDispositionInfo._fields_ == [
        ('DeleteFile', outbox_module.wintypes.BOOLEAN)
    ]
    assert ctypes.sizeof(outbox_module._WindowsFileDispositionInfo) == 1
    assert ctypes.alignment(outbox_module._WindowsFileDispositionInfo) == 1
    assert outbox_module._WindowsFileDispositionInfo.DeleteFile.offset == 0
    layer.unlink(Path('snapshot.alog'))
    assert information_calls == [(layer._FILE_DISPOSITION_INFO_CLASS, 1, 1)]
    assert readonly_calls == [(30, False)]
    assert closed == [30, 20, 10]

    set_result = False
    last_error = 5
    with pytest.raises(OSError) as raised:
        layer.unlink(Path('snapshot.alog'))
    assert raised.value.errno == 5
    assert information_calls[-1] == (layer._FILE_DISPOSITION_INFO_CLASS, 1, 1)
    assert readonly_calls[-1] == (30, False)
    assert closed[-3:] == [30, 20, 10]


def test_windows_native_publication_is_write_through_no_replace_and_fail_closed() -> None:
    move_calls: list[tuple[object, ...]] = []
    move_result = True
    last_error = 0

    def move_file(*arguments: object) -> bool:
        move_calls.append(arguments)
        return move_result

    layer = object.__new__(outbox_module._WindowsNativeLayer)
    layer._kernel32 = SimpleNamespace(MoveFileExW=move_file)
    layer._ctypes = SimpleNamespace(get_last_error=lambda: last_error)
    source = Path('temporary.alog')
    destination = Path('published.alog')

    layer.publish(source, destination)
    assert move_calls == [(os.fspath(source), os.fspath(destination), layer._MOVEFILE_WRITE_THROUGH)]
    publication_flags = cast(int, move_calls[0][2])
    assert not publication_flags & layer._MOVEFILE_REPLACE_EXISTING

    move_result = False
    last_error = 183
    with pytest.raises(FileExistsError):
        layer.publish(source, destination)
    last_error = 5
    with pytest.raises(OSError) as raised:
        layer.publish(source, destination)
    assert raised.value.errno == 5


def test_windows_fake_native_publication_reuses_eexist_without_snapshot_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingNative:
        def __init__(self) -> None:
            self.publications: list[tuple[Path, Path]] = []
            self.file_flushes: list[int] = []
            self.directory_flushes: list[Path] = []

        @staticmethod
        def open_readonly(path: Path, *, directory: bool = False) -> int:
            flags = os.O_RDONLY
            if directory:
                flags |= getattr(os, 'O_DIRECTORY', 0)
            return os.open(path, flags)

        @staticmethod
        def open_lock(path: Path) -> int:
            return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

        @staticmethod
        def set_private_permissions(path: Path, mode: int) -> None:
            path.chmod(mode)

        @staticmethod
        def verify_private_permissions(path: Path, mode: int) -> None:
            if stat.S_IMODE(path.stat().st_mode) != mode:
                raise OSError(errno.EACCES, 'permissions')

        def flush(self, descriptor: int, *, directory: bool) -> None:
            del directory
            self.file_flushes.append(descriptor)

        def flush_directory(self, path: Path) -> None:
            self.directory_flushes.append(path)

        def publish(self, source: Path, destination: Path) -> None:
            self.publications.append((source, destination))
            if destination.exists():
                raise FileExistsError(errno.EEXIST, 'exists')
            source.rename(destination)

        @staticmethod
        def unlink(path: Path) -> None:
            path.chmod(0o600)
            path.unlink()

    root = tmp_path / 'connector'
    source_directory = root / NAMESPACE_DIGEST / 'snapshots'
    source_directory.mkdir(parents=True, mode=0o700)
    sha256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
    final_path = source_directory / sha256[:2] / f'{sha256}.alog'
    first_temporary = source_directory / '.snapshot-first.tmp'
    first_temporary.write_bytes(PROFILE_BYTES)
    native = RecordingNative()
    monkeypatch.setattr(outbox_module, '_IS_WINDOWS', True)
    monkeypatch.setattr(outbox_module, '_HAS_DIRECTORY_FDS', False)
    monkeypatch.setattr(outbox_module, '_WINDOWS_NATIVE', native)
    outbox = Outbox(root, clock=lambda: NOW)

    assert outbox._publish_temporary(
        first_temporary, final_path, sha256, len(PROFILE_BYTES)
    )
    before = final_path.stat()
    second_temporary = source_directory / '.snapshot-second.tmp'
    second_temporary.write_bytes(PROFILE_BYTES)
    assert not outbox._publish_temporary(
        second_temporary, final_path, sha256, len(PROFILE_BYTES)
    )
    after = final_path.stat()

    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert final_path.read_bytes() == PROFILE_BYTES
    assert not second_temporary.exists()
    assert len(native.publications) == 2
    assert native.file_flushes == []
    assert native.directory_flushes


def _synthetic_windows_sid(subauthority: int = 21, *, valid: bool = True) -> bytes:
    revision = 1 if valid else 2
    return bytes((revision, 1, 0, 0, 0, 0, 0, 5)) + subauthority.to_bytes(4, 'little')


def _windows_acl_layer(
    specs: tuple[tuple[int, int, int, bool, int, bool], ...],
) -> tuple[outbox_module._WindowsNativeLayer, ctypes.c_void_p]:
    sid_offset = outbox_module._WindowsAccessAllowedAce.SidStart.offset
    buffers: list[ctypes.Array[ctypes.c_char]] = []
    for ace_type, ace_flags, mask, matches_sid, size_adjustment, valid_sid in specs:
        sid = _synthetic_windows_sid(21 if matches_sid else 22, valid=valid_sid)
        exact_size = sid_offset + len(sid)
        buffer = ctypes.create_string_buffer(max(exact_size + max(size_adjustment, 0), exact_size))
        ace = ctypes.cast(
            buffer, ctypes.POINTER(outbox_module._WindowsAccessAllowedAce)
        ).contents
        ace.Header.AceType = ace_type
        ace.Header.AceFlags = ace_flags
        ace.Header.AceSize = exact_size + size_adjustment
        ace.Mask = mask
        ctypes.memmove(ctypes.addressof(buffer) + sid_offset, sid, len(sid))
        buffers.append(buffer)

    expected_sid_buffer = ctypes.create_string_buffer(_synthetic_windows_sid())
    expected_sid = ctypes.c_void_p(ctypes.addressof(expected_sid_buffer))

    def sid_bytes(pointer: Any) -> bytes:
        address = ctypes.cast(pointer, ctypes.c_void_p).value
        assert address is not None
        header = ctypes.string_at(address, 8)
        return ctypes.string_at(address, 8 + 4 * header[1])

    def is_valid_sid(pointer: Any) -> bool:
        value = sid_bytes(pointer)
        return value[0] == 1 and value[1] <= 15 and len(value) == 8 + 4 * value[1]

    def get_length_sid(pointer: Any) -> int:
        return len(sid_bytes(pointer))

    def get_acl_information(
        _dacl: Any, information: Any, _size: int, _information_class: int
    ) -> bool:
        parsed = ctypes.cast(
            information, ctypes.POINTER(outbox_module._WindowsAclSizeInformation)
        ).contents
        parsed.AceCount = len(buffers)
        return True

    def get_ace(_dacl: Any, index: int, pointer: Any) -> bool:
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(
            ctypes.addressof(buffers[index])
        )
        return True

    def equal_sid(left: Any, right: Any) -> bool:
        # Keep the synthetic expected SID storage alive with the API seam.
        assert expected_sid_buffer
        return is_valid_sid(left) and is_valid_sid(right) and sid_bytes(left) == sid_bytes(right)

    layer = object.__new__(outbox_module._WindowsNativeLayer)
    layer._ctypes = ctypes
    layer._advapi32 = SimpleNamespace(
        GetAclInformation=get_acl_information,
        GetAce=get_ace,
        GetLengthSid=get_length_sid,
        IsValidSid=is_valid_sid,
        EqualSid=equal_sid,
    )
    return layer, expected_sid


def test_windows_private_acl_parser_requires_exact_valid_sid_ace() -> None:
    assert ctypes.sizeof(outbox_module._WindowsAceHeader) == 4
    assert outbox_module._WindowsAccessAllowedAce.SidStart.offset == 8
    assert ctypes.sizeof(outbox_module._WindowsAccessAllowedAce) == 12

    layer_type = outbox_module._WindowsNativeLayer
    directory_flags = (
        layer_type._OBJECT_INHERIT_ACE | layer_type._CONTAINER_INHERIT_ACE
    )
    directory_ace = (
        layer_type._ACCESS_ALLOWED_ACE_TYPE,
        directory_flags,
        layer_type._FILE_ALL_ACCESS,
        True,
        0,
        True,
    )
    file_ace = (
        layer_type._ACCESS_ALLOWED_ACE_TYPE,
        0,
        layer_type._FILE_ALL_ACCESS,
        True,
        0,
        True,
    )
    for ace, expected_flags in (
        (directory_ace, directory_flags),
        (file_ace, 0),
    ):
        layer, expected_sid = _windows_acl_layer((ace,))
        layer._verify_private_dacl(
            ctypes.c_void_p(1),
            expected_sid,
            protected=True,
            expected_flags=expected_flags,
        )

    for ace, expected_flags in (
        (directory_ace, 0),
        (file_ace, directory_flags),
    ):
        layer, expected_sid = _windows_acl_layer((ace,))
        with pytest.raises(OSError, match='ACL'):
            layer._verify_private_dacl(
                ctypes.c_void_p(1),
                expected_sid,
                protected=True,
                expected_flags=expected_flags,
            )

    good = directory_ace
    bad_acls = (
        (),
        (good, good),
        ((layer_type._ACCESS_DENIED_ACE_TYPE, *good[1:]),),
        ((17, *good[1:]),),
        ((good[0], good[1], good[2] ^ 1, *good[3:]),),
        ((good[0], good[1] | layer_type._INHERITED_ACE, *good[2:]),),
        ((good[0], good[1], good[2], False, good[4], good[5]),),
        ((good[0], good[1], good[2], good[3], 4, good[5]),),
        ((good[0], good[1], good[2], good[3], -4, good[5]),),
        ((good[0], good[1], good[2], good[3], good[4], False),),
    )
    for specs in bad_acls:
        bad_layer, bad_expected_sid = _windows_acl_layer(specs)
        with pytest.raises(OSError, match='ACL'):
            bad_layer._verify_private_dacl(
                ctypes.c_void_p(1),
                bad_expected_sid,
                protected=True,
                expected_flags=directory_flags,
            )
    layer, expected_sid = _windows_acl_layer((directory_ace,))
    with pytest.raises(OSError, match='ACL'):
        layer._verify_private_dacl(
            ctypes.c_void_p(1),
            expected_sid,
            protected=False,
            expected_flags=directory_flags,
        )


def test_windows_native_reparse_and_flush_failure_seams_are_causal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    layer = object.__new__(outbox_module._WindowsNativeLayer)

    def create_file(*_arguments: object) -> int:
        return 42

    def close_handle(handle: int) -> bool:
        closed.append(handle)
        return True

    layer._kernel32 = SimpleNamespace(
        CreateFileW=create_file,
        CloseHandle=close_handle,
    )
    layer._ctypes = SimpleNamespace(get_last_error=lambda: 0)
    layer._invalid_handle = -1

    def reparse_attributes(_handle: int) -> int:
        return layer._FILE_ATTRIBUTE_REPARSE_POINT

    monkeypatch.setattr(layer, '_attributes', reparse_attributes)
    with pytest.raises(OSError, match='reparse'):
        layer._open_one(tmp_path, layer._GENERIC_READ, layer._OPEN_EXISTING)
    assert closed == [42]


def test_windows_lock_seam_blocks_a_contender_until_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / 'windows.lock'
    lock_path.write_bytes(b'0')
    first = os.open(lock_path, os.O_RDWR)
    second = os.open(lock_path, os.O_RDWR)
    condition = Condition()
    locked = False
    contender_waiting = Event()
    lk_lock = 1
    lk_unlock = 2

    def locking(_descriptor: int, operation: int, _length: int) -> None:
        nonlocal locked
        with condition:
            if operation == lk_lock:
                while locked:
                    contender_waiting.set()
                    condition.wait(timeout=5)
                locked = True
            else:
                assert operation == lk_unlock
                locked = False
                condition.notify_all()

    fake_msvcrt = SimpleNamespace(LK_LOCK=lk_lock, LK_UNLCK=lk_unlock, locking=locking)
    original_import = outbox_module.importlib.import_module

    def import_module(name: str) -> object:
        return fake_msvcrt if name == 'msvcrt' else original_import(name)

    monkeypatch.setattr(outbox_module, '_IS_WINDOWS', True)
    monkeypatch.setattr(outbox_module.importlib, 'import_module', import_module)
    try:
        outbox_module._acquire_file_lock(first)
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(outbox_module._acquire_file_lock, second)
            assert contender_waiting.wait(timeout=5)
            assert not contender.done()
            outbox_module._release_file_lock(first)
            contender.result(timeout=5)
        outbox_module._release_file_lock(second)
    finally:
        os.close(first)
        os.close(second)


def test_windows_native_failure_seams_fail_closed_without_sensitive_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = 'C:\\private\\customer\\profile.alog'

    class FailingNative:
        @staticmethod
        def set_private_permissions(_path: Path, _mode: int) -> None:
            raise OSError(private_path)

    monkeypatch.setattr(outbox_module, '_IS_WINDOWS', True)
    monkeypatch.setattr(outbox_module, '_WINDOWS_NATIVE', FailingNative())
    with pytest.raises(OutboxError) as raised:
        outbox_module._set_private_permissions(tmp_path, 0o700)
    assert str(raised.value) == 'private connector permissions could not be applied'
    assert raised.value.__cause__ is None
    assert private_path not in repr(raised.value)

    class ReparseNative:
        @staticmethod
        def open_readonly(_path: Path, *, directory: bool = False) -> int:
            del directory
            raise OSError(errno.ELOOP, private_path)

    monkeypatch.setattr(outbox_module, '_WINDOWS_NATIVE', ReparseNative())
    with pytest.raises(OutboxError, match='reparse') as reparse:
        outbox_module._open_path_readonly(tmp_path / 'source.alog')
    assert reparse.value.__cause__ is None
    assert private_path not in repr(reparse.value)


@pytest.mark.win32
def test_windows_runtime_private_acl_accepts_normalized_file_flags(
    tmp_path: Path,
) -> None:
    directory = tmp_path / 'private'
    directory.mkdir()
    regular_file = directory / 'lock'
    regular_file.touch()
    native = outbox_module._WINDOWS_NATIVE
    assert native is not None

    native.set_private_permissions(directory, 0o700)
    native.set_private_permissions(regular_file, 0o600)

    native.verify_private_permissions(directory, 0o700)
    native.verify_private_permissions(regular_file, 0o600)


@pytest.mark.win32
def test_windows_runtime_native_unlink_uses_file_disposition_info(tmp_path: Path) -> None:
    path = tmp_path / 'native-unlink.alog'
    path.write_bytes(PROFILE_BYTES)
    native = outbox_module._WINDOWS_NATIVE
    assert native is not None
    native.set_private_permissions(path, 0o400)
    native.unlink(path)
    assert not path.exists()


@pytest.mark.win32
def test_windows_runtime_private_acl_publication_and_unlink(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)
    try:
        stage_a = first.snapshot_saved_file(NAMESPACE, saved_profile)
        before = stage_a.absolute_path.stat()
        descriptor = os.open(stage_a.absolute_path, os.O_RDONLY)
        try:
            stage_b = second.snapshot_saved_file(NAMESPACE, saved_profile)
            after = stage_b.absolute_path.stat()
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert os.read(descriptor, len(PROFILE_BYTES)) == PROFILE_BYTES
        finally:
            os.close(descriptor)
        native = outbox_module._WINDOWS_NATIVE
        assert native is not None
        native.verify_private_permissions(root, 0o700)
        native.verify_private_permissions(stage_a.absolute_path, 0o400)
        job_a = first.enqueue(NAMESPACE, stage_a, ROAST_UUID, METADATA, CLIENT_UUID).job
        job_b = second.enqueue(
            NAMESPACE, stage_b, OTHER_ROAST_UUID, METADATA, CLIENT_UUID
        ).job
        leased_a = first.lease_next(NAMESPACE, NOW)
        assert leased_a is not None and leased_a.lease_token is not None
        first.mark_complete(job_a.id, leased_a.lease_token, NOW)
        assert stage_a.absolute_path.exists()
        second.remove(job_b.id)
        assert not stage_a.absolute_path.exists()
    finally:
        first.close()
        second.close()


@pytest.mark.win32
def test_windows_runtime_rejects_reparse_source(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    source_directory = tmp_path / 'source'
    source_directory.mkdir()
    source = source_directory / 'saved.alog'
    source.write_bytes(PROFILE_BYTES)
    link = tmp_path / 'source-link'
    try:
        link.symlink_to(source_directory, target_is_directory=True)
    except OSError:
        pytest.skip('Windows reparse-point creation unavailable')
    outbox = opened_outbox(root)
    try:
        with pytest.raises(OutboxError, match='reparse'):
            outbox.snapshot_saved_file(NAMESPACE, link / source.name)
    finally:
        outbox.close()


@pytest.mark.win32
def test_windows_runtime_rejects_generated_junction(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    outbox = opened_outbox(root)
    target = tmp_path / 'junction-target'
    target.mkdir()
    junction = root / NAMESPACE_DIGEST
    completed = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', os.fspath(junction), os.fspath(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        outbox.close()
        pytest.skip('Windows junction creation unavailable')
    try:
        with pytest.raises(OutboxError, match='reparse'):
            outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    finally:
        outbox.close()


@pytest.mark.win32
def test_windows_runtime_process_lock_blocks_a_second_instance(
    tmp_path: Path, saved_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)
    first_inside_lock = Event()
    release_first = Event()
    contender_attempted_lock = Event()
    acquire_calls = 0
    original_locked_snapshot = first._snapshot_saved_file_locked
    original_acquire = outbox_module._acquire_file_lock

    def blocked_snapshot(namespace: Namespace, source: Path) -> Snapshot:
        first_inside_lock.set()
        assert release_first.wait(timeout=5)
        return original_locked_snapshot(namespace, source)

    def recording_acquire(descriptor: int) -> None:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 2:
            contender_attempted_lock.set()
        original_acquire(descriptor)

    monkeypatch.setattr(first, '_snapshot_saved_file_locked', blocked_snapshot)
    monkeypatch.setattr(outbox_module, '_acquire_file_lock', recording_acquire)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                first.snapshot_saved_file, NAMESPACE, saved_profile
            )
            assert first_inside_lock.wait(timeout=5)
            second_future = executor.submit(
                second.snapshot_saved_file, NAMESPACE, saved_profile
            )
            assert contender_attempted_lock.wait(timeout=5)
            assert not second_future.done()
            release_first.set()
            first_future.result(timeout=5)
            second_future.result(timeout=5)
    finally:
        release_first.set()
        first.close()
        second.close()


def test_two_instances_deduplicate_concurrent_enqueue(tmp_path: Path, saved_profile: Path) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)

    def snapshot_and_enqueue(instance: Outbox) -> EnqueueResult:
        snapshot = instance.snapshot_saved_file(NAMESPACE, saved_profile)
        return instance.enqueue(NAMESPACE, snapshot, ROAST_UUID, METADATA, CLIENT_UUID)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(snapshot_and_enqueue, (first, second)))
        assert {result.created for result in results} == {False, True}
        assert len({result.job.id for result in results}) == 1
        assert first.counts(NAMESPACE).pending == 1
    finally:
        first.close()
        second.close()


def test_two_instances_lease_distinct_jobs_concurrently(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    second = opened_outbox(root)
    enqueue_fixture(first)
    enqueue_fixture(first, roast_uuid=OTHER_ROAST_UUID)

    def lease(instance: Outbox) -> Job | None:
        return instance.lease_next(NAMESPACE, NOW)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            leased = tuple(executor.map(lease, (first, second)))
        assert all(job is not None for job in leased)
        assert len({job.id for job in leased if job is not None}) == 2
    finally:
        first.close()
        second.close()


def test_separate_processes_lease_distinct_jobs_concurrently(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    setup = opened_outbox(root)
    enqueue_fixture(setup)
    enqueue_fixture(setup, roast_uuid=OTHER_ROAST_UUID)
    setup.close()
    script = f'''\
from datetime import UTC, datetime
from pathlib import Path
import sys
from uuid import UUID
from artisanlib.roastserver.contract import Namespace
from artisanlib.roastserver.outbox import Outbox
namespace = Namespace(
    origin={NAMESPACE.origin!r},
    organization_id=UUID({str(NAMESPACE.organization_id)!r}),
    key={NAMESPACE.key!r},
)
now = datetime.fromisoformat({NOW.isoformat()!r}).astimezone(UTC)
outbox = Outbox(Path(sys.argv[1]), clock=lambda: now)
outbox.open()
try:
    job = outbox.lease_next(namespace, now)
    print('none' if job is None else job.id)
finally:
    outbox.close()
'''

    def run_worker(_index: int) -> str:
        completed = subprocess.run(
            [sys.executable, '-c', script, os.fspath(root)],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as executor:
        job_ids = tuple(executor.map(run_worker, range(2)))
    assert 'none' not in job_ids
    assert len(set(job_ids)) == 2
