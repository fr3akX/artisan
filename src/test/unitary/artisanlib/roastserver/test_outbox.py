from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from threading import Event
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest

from artisanlib.roastserver import outbox as outbox_module
from artisanlib.roastserver.contract import FailureKind, Namespace, PublicFailure
from artisanlib.roastserver.outbox import EnqueueResult, Job, Outbox, OutboxError, Snapshot

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
def outbox(tmp_path: Path) -> Generator[Outbox, None, None]:
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


def test_schema_migration_is_transactional_and_versioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'connector'
    original = outbox_module._SCHEMA_STATEMENTS
    monkeypatch.setattr(
        outbox_module,
        '_SCHEMA_STATEMENTS',
        (*original[:2], 'CREATE TABLE deliberately_invalid('),
    )
    with pytest.raises(sqlite3.DatabaseError):
        opened_outbox(root)

    connection = sqlite3.connect(database_path(root))
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    assert tables == []

    monkeypatch.setattr(outbox_module, '_SCHEMA_STATEMENTS', original)
    recovered = opened_outbox(root)
    recovered.close()
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(1,)]
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert 'UNIQUE(namespace_id, roast_uuid, content_sha256)' in sql
    assert "'pending','leased','retry_wait','paused','failed','complete'" in sql.replace('\n', '')


def test_unknown_schema_version_is_rejected_without_changes(tmp_path: Path) -> None:
    root = tmp_path / 'connector'
    root.mkdir(mode=0o700)
    connection = sqlite3.connect(database_path(root))
    connection.execute('CREATE TABLE schema_version (version INTEGER NOT NULL)')
    connection.execute('INSERT INTO schema_version VALUES (2)')
    connection.commit()
    connection.close()

    with pytest.raises(OutboxError, match='schema'):
        opened_outbox(root)

    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(2,)]
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


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
    if os.name != 'nt':
        assert stat.S_IMODE(snapshot.absolute_path.stat().st_mode) == 0o600
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


def test_atomic_publication_failure_cleans_temp_and_rolls_back_index(
    outbox: Outbox,
    saved_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError('simulated publication failure')

    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='publication'):
        outbox.snapshot_saved_file(NAMESPACE, saved_profile)
    assert not list(outbox.root.rglob('.snapshot-*.tmp'))
    connection = sqlite3.connect(database_path(outbox.root))
    try:
        assert connection.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 0
    finally:
        connection.close()


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
    outbox.mark_failed(leased.id, NOW, FAILURE)
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
    first.mark_retry(job.id, NOW, retry_at, FAILURE)
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
    outbox.mark_retry(first.id, NOW, NOW + timedelta(hours=1), FAILURE)
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
    oversized = PublicFailure(
        kind=FailureKind.OFFLINE,
        code='c' * 150,
        message='m' * 700,
        retryable=False,
    )
    outbox.mark_failed(first.id, NOW, oversized)
    failed = outbox.failed_jobs(NAMESPACE)
    assert len(failed) == 1
    assert failed[0].id == first.id
    assert len(failed[0].error_code) == 100
    assert len(failed[0].error_message) == 500
    counts = outbox.counts(NAMESPACE)
    assert counts.pending == 1 and counts.failed == 1

    outbox.retry_now(first.id, NOW)
    assert outbox.failed_jobs(NAMESPACE) == ()
    assert outbox.counts(NAMESPACE).pending == 2


def test_complete_is_idempotent_and_completed_job_deduplicates(outbox: Outbox) -> None:
    first = enqueue_fixture(outbox)
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None
    snapshot_path = leased.snapshot_path
    assert snapshot_path is not None
    outbox.mark_complete(leased.id, NOW)
    outbox.mark_complete(leased.id, NOW + timedelta(seconds=1))
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
    snapshot = outbox.snapshot_saved_file(NAMESPACE, source)
    first = outbox.enqueue(NAMESPACE, snapshot, ROAST_UUID, METADATA, CLIENT_UUID).job
    second = outbox.enqueue(NAMESPACE, snapshot, OTHER_ROAST_UUID, METADATA, CLIENT_UUID).job
    assert outbox.protected_paths(NAMESPACE) == frozenset({snapshot.absolute_path})

    outbox.remove(first.id)
    assert snapshot.absolute_path.exists()
    assert outbox.protected_paths(NAMESPACE) == frozenset({snapshot.absolute_path})
    leased = outbox.lease_next(NAMESPACE, NOW)
    assert leased is not None and leased.id == second.id
    outbox.mark_complete(second.id, NOW)
    assert not snapshot.absolute_path.exists()
    assert outbox.protected_paths(NAMESPACE) == frozenset()


def test_retry_remove_and_state_transitions_reject_unknown_or_unleased_jobs(outbox: Outbox) -> None:
    job = enqueue_fixture(outbox).job
    with pytest.raises(OutboxError, match='state'):
        outbox.mark_retry(job.id, NOW, NOW + timedelta(seconds=1), FAILURE)
    with pytest.raises(KeyError):
        outbox.mark_failed('0' * 32, NOW, FAILURE)
    with pytest.raises(ValueError):
        outbox.mark_retry(job.id, NOW, NOW - timedelta(seconds=1), FAILURE)
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


def test_restart_collects_snapshot_left_unqueued_by_a_crash(
    tmp_path: Path, saved_profile: Path
) -> None:
    root = tmp_path / 'connector'
    first = opened_outbox(root)
    snapshot = first.snapshot_saved_file(NAMESPACE, saved_profile)
    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 0
    finally:
        connection.close()
    first.close()

    second = opened_outbox(root)
    second.close()
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
    assert first.lease_next(NAMESPACE, NOW) is not None
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
            completion = executor.submit(first.mark_complete, job.id, NOW)
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
