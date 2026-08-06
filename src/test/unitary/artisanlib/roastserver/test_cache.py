from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any, BinaryIO, cast
from uuid import UUID

import pytest

from artisanlib.roastserver import _filesystem as filesystem_module
from artisanlib.roastserver import cache as cache_module
from artisanlib.roastserver.api import DownloadReceipt
from artisanlib.roastserver.cache import CacheError, CachedRevision, CacheStore
from artisanlib.roastserver.contract import (
    ArchiveFilters,
    FAILURE_MESSAGES,
    FailureKind,
    Namespace,
    RoastDetail,
    parse_roast_detail,
)

NOW = datetime(2026, 8, 1, 12, 34, 56, 123456, tzinfo=UTC)
ORGANIZATION_UUID = UUID('22222222-2222-4222-8222-222222222222')
ROAST_UUID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_ROAST_UUID = UUID('33333333-3333-4333-8333-333333333333')
PROFILE_BYTES = b"{'roastUUID':'11111111111141118111111111111111','title':'cached'}"


class PortableWindowsCacheNative:
    def __init__(self) -> None:
        self.permissions: list[tuple[Path, int]] = []
        self.flushes: list[tuple[str, object]] = []
        self.replacements: list[tuple[Path, Path]] = []
        self.quarantine_moves: list[tuple[Path, Path]] = []
        self.removals: list[Path] = []
        self.verified_removals: list[Path] = []
        self.reparse_path: Path | None = None

    def open_readonly(self, path: Path, *, directory: bool = False) -> int:
        if path == self.reparse_path:
            raise OSError('injected reparse point')
        flags = os.O_RDONLY
        if directory:
            flags |= getattr(os, 'O_DIRECTORY', 0)
        return os.open(path, flags)

    @staticmethod
    def open_lock(path: Path) -> int:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

    def set_private_permissions(self, path: Path, mode: int) -> None:
        self.permissions.append((path, mode))
        os.chmod(path, mode)

    @staticmethod
    def verify_private_permissions(path: Path, mode: int) -> None:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            raise OSError('injected ACL mismatch')

    def flush(self, descriptor: int, *, directory: bool) -> None:
        self.flushes.append(('descriptor', directory))
        os.fsync(descriptor)

    def flush_directory(self, path: Path) -> None:
        self.flushes.append(('directory', path))

    @staticmethod
    def publish(source: Path, destination: Path) -> None:
        os.rename(source, destination)

    def replace(self, source: Path, destination: Path) -> None:
        self.replacements.append((source, destination))
        os.replace(source, destination)

    def move_no_replace(self, source: Path, destination: Path) -> None:
        self.quarantine_moves.append((source, destination))
        if destination.exists():
            raise FileExistsError(errno.EEXIST, 'destination exists')
        os.rename(source, destination)

    def unlink(self, path: Path) -> None:
        self.removals.append(path)
        path.unlink()

    def unlink_if_identity(self, path: Path, expected_identity: tuple[int, int]) -> bool:
        path_stat = path.stat()
        if (path_stat.st_dev, path_stat.st_ino) != expected_identity:
            return False
        self.verified_removals.append(path)
        path.unlink()
        return True


def namespace_for_test(
    origin: str = 'https://archive.example',
    organization_uuid: UUID = ORGANIZATION_UUID,
) -> Namespace:
    digest = hashlib.sha256(f'{origin}\n{organization_uuid}'.encode()).hexdigest()
    return Namespace(origin, organization_uuid, f'namespace-sha256:{digest}')


NAMESPACE = namespace_for_test()
OTHER_ORIGIN_NAMESPACE = namespace_for_test('https://other.example')
OTHER_ORGANIZATION_NAMESPACE = namespace_for_test(
    organization_uuid=UUID('44444444-4444-4444-8444-444444444444')
)


def detail_payload(
    *,
    roast_uuid: UUID = ROAST_UUID,
    revision_number: int = 1,
    profile_bytes: bytes = PROFILE_BYTES,
    roast_at: datetime = NOW - timedelta(hours=1),
    title: str | None = 'Morning roast',
    machine: str | None = 'Sample Roaster',
    state: str = 'parsed',
    labels: list[dict[str, object]] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    sha256 = hashlib.sha256(profile_bytes).hexdigest()
    roast_hex = roast_uuid.hex
    parse_state = 'parsed' if state == 'parsed' else 'failed'
    return {
        'roast_uuid': roast_hex,
        'state': state,
        'roast_at': roast_at.isoformat(),
        'title': title,
        'batch_prefix': 'A',
        'batch_number': revision_number,
        'batch_position': 1,
        'operator': 'Operator',
        'machine': machine,
        'machine_setup': 'Gas',
        'temperature_unit': 'C',
        'duration_seconds': 600,
        'green_weight_kg': 1.0,
        'roasted_weight_kg': 0.85,
        'revision_count': revision_number,
        'updated_at': (roast_at + timedelta(minutes=10)).isoformat(),
        'labels': labels
        if labels is not None
        else [
            {
                'label_uuid': 'aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa',
                'name': 'Filter',
                'color': 'green',
                'archived': False,
            }
        ],
        'current_metadata': {'source': 'desktop'} if metadata is None else metadata,
        'current_revision': {
            'revision_number': revision_number,
            'sha256': sha256,
            'byte_size': len(profile_bytes),
            'parser_version': '1.0',
            'parse_state': parse_state,
            'parse_diagnostic_code': None,
            'parse_diagnostic_message': None,
            'uploaded_at': (roast_at + timedelta(minutes=11)).isoformat(),
            'metadata': {'source': 'desktop'} if metadata is None else metadata,
            'reparse_recommended': False,
        },
        'links': {
            'self': f'/api/v1/roasts/{roast_hex}',
            'chart': f'/api/v1/roasts/{roast_hex}/chart',
            'revisions': f'/api/v1/roasts/{roast_hex}/revisions',
        },
    }


def make_detail(**kwargs: Any) -> RoastDetail:
    return parse_roast_detail(detail_payload(**kwargs))


DETAIL = make_detail()
RECEIPT = DownloadReceipt(
    roast_uuid=ROAST_UUID,
    revision_number=1,
    sha256=hashlib.sha256(PROFILE_BYTES).hexdigest(),
    byte_count=len(PROFILE_BYTES),
    filename=f'{ROAST_UUID.hex}-r1.alog',
)


def opened_cache(root: Path) -> CacheStore:
    store = CacheStore(root)
    store.open()
    return store


@pytest.fixture
def cache(tmp_path: Path) -> CacheStore:
    return opened_cache(tmp_path / 'cache')


def stage_bytes(cache: CacheStore, namespace: Namespace, data: bytes) -> Path:
    path, destination = cache.new_staging_file(namespace)
    with destination:
        destination.write(data)
        destination.flush()
    return path


@pytest.fixture
def staged_download(cache: CacheStore) -> Path:
    return stage_bytes(cache, NAMESPACE, PROFILE_BYTES)


@pytest.fixture
def cached_revision(cache: CacheStore, staged_download: Path) -> CachedRevision:
    return cache.publish(NAMESPACE, DETAIL, RECEIPT, staged_download, NOW)


def publish_revision(
    cache: CacheStore,
    *,
    namespace: Namespace = NAMESPACE,
    roast_uuid: UUID = ROAST_UUID,
    revision_number: int = 1,
    profile_bytes: bytes = PROFILE_BYTES,
    roast_at: datetime = NOW - timedelta(hours=1),
    downloaded_at: datetime = NOW,
    title: str | None = 'Morning roast',
    machine: str | None = 'Sample Roaster',
    state: str = 'parsed',
    labels: list[dict[str, object]] | None = None,
) -> CachedRevision:
    detail = make_detail(
        roast_uuid=roast_uuid,
        revision_number=revision_number,
        profile_bytes=profile_bytes,
        roast_at=roast_at,
        title=title,
        machine=machine,
        state=state,
        labels=labels,
    )
    receipt = DownloadReceipt(
        roast_uuid=roast_uuid,
        revision_number=revision_number,
        sha256=hashlib.sha256(profile_bytes).hexdigest(),
        byte_count=len(profile_bytes),
        filename=f'{roast_uuid.hex}-r{revision_number}.alog',
    )
    return cache.publish(
        namespace,
        detail,
        receipt,
        stage_bytes(cache, namespace, profile_bytes),
        downloaded_at,
    )


@pytest.fixture
def three_cached_revisions(cache: CacheStore) -> tuple[CachedRevision, ...]:
    return (
        publish_revision(
            cache,
            roast_uuid=ROAST_UUID,
            profile_bytes=b'oldest',
            downloaded_at=NOW,
            roast_at=NOW - timedelta(days=3),
        ),
        publish_revision(
            cache,
            roast_uuid=OTHER_ROAST_UUID,
            profile_bytes=b'middle-sized',
            downloaded_at=NOW + timedelta(seconds=1),
            roast_at=NOW - timedelta(days=2),
        ),
        publish_revision(
            cache,
            roast_uuid=UUID('55555555-5555-4555-8555-555555555555'),
            profile_bytes=b'newest-profile',
            downloaded_at=NOW + timedelta(seconds=2),
            roast_at=NOW - timedelta(days=1),
        ),
    )


def assert_no_temporary_files(cache: CacheStore) -> None:
    assert not list(cache.root.rglob('*.part'))
    assert not [path for path in cache.root.rglob('*.lock') if path.name != '.cache.lock']


def test_constructor_is_memory_only_and_open_is_explicit_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'lazy-cache'
    calls: list[int] = []
    original = filesystem_module.prepare_private_root

    def record_open(path: Path) -> None:
        calls.append(threading.get_ident())
        original(path)

    monkeypatch.setattr(filesystem_module, 'prepare_private_root', record_open)
    constructor_thread = threading.get_ident()

    store = CacheStore(root)

    assert calls == []
    assert not root.exists()
    with pytest.raises(CacheError):
        store.stats(NAMESPACE)
    opener = threading.Thread(target=store.open)
    opener.start()
    opener.join(timeout=5)
    assert not opener.is_alive()
    assert len(calls) == 1
    assert calls[0] != constructor_thread
    store.open()
    assert len(calls) == 1
    assert store.stats(NAMESPACE).revision_count == 0
    store.close()


def test_publish_uses_generated_path_and_public_canonical_sidecar(
    cache: CacheStore, staged_download: Path
) -> None:
    cached = cache.publish(NAMESPACE, DETAIL, RECEIPT, staged_download, NOW)
    assert cached.path.name == f'1-{RECEIPT.sha256}.alog'
    assert cached.path.parent.name == ROAST_UUID.hex
    assert cached.sidecar_path == cached.path.with_suffix('.json')
    sidecar_bytes = cached.sidecar_path.read_bytes()
    sidecar = json.loads(sidecar_bytes)
    assert sidecar['schema_version'] == 1
    assert set(sidecar) == {
        'schema_version',
        'origin',
        'organization_uuid',
        'roast',
        'revision',
        'downloaded_at',
    }
    assert sidecar_bytes == json.dumps(
        sidecar,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    assert 'credential' not in sidecar_bytes.decode('utf-8').casefold()
    assert NAMESPACE.origin not in cached.path.as_posix()
    assert cached.path.read_bytes() == PROFILE_BYTES
    assert not staged_download.exists()
    assert_no_temporary_files(cache)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode assertion')
def test_cache_root_directories_and_files_are_private(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700
    for directory in (
        cached_revision.path.parent,
        cached_revision.path.parent.parent,
        cache.root / NAMESPACE.key.removeprefix('namespace-sha256:') / 'tmp',
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(cached_revision.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cached_revision.sidecar_path.stat().st_mode) == 0o600


def test_publish_rejects_static_or_cross_namespace_staging_paths(cache: CacheStore) -> None:
    static = cache.root.parent / 'server-supplied-name.alog'
    static.write_bytes(PROFILE_BYTES)
    with pytest.raises(CacheError) as raised:
        cache.publish(NAMESPACE, DETAIL, RECEIPT, static, NOW)
    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT

    other_stage = stage_bytes(cache, OTHER_ORIGIN_NAMESPACE, PROFILE_BYTES)
    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, DETAIL, RECEIPT, other_stage, NOW)
    assert static.name not in repr(raised.value)


def test_publish_requires_exact_detail_receipt_and_profile_identity(cache: CacheStore) -> None:
    mismatches = (
        replace(RECEIPT, roast_uuid=OTHER_ROAST_UUID),
        replace(RECEIPT, revision_number=2),
        replace(RECEIPT, sha256='f' * 64),
        replace(RECEIPT, byte_count=RECEIPT.byte_count + 1),
        replace(RECEIPT, filename='../../server-control\nname.alog'),
    )
    for receipt in mismatches:
        staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)
        with pytest.raises(CacheError) as raised:
            cache.publish(NAMESPACE, DETAIL, receipt, staged, NOW)
        assert str(raised.value) == FAILURE_MESSAGES[FailureKind.CACHE_CORRUPT]
        assert raised.value.__cause__ is None
        assert_no_temporary_files(cache)
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES + b'changed')
    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)
    assert_no_temporary_files(cache)


def test_publish_streams_exact_16_mib_boundary(cache: CacheStore) -> None:
    maximum = b'x' * cache_module.MAX_PROFILE_BYTES
    cached = publish_revision(cache, profile_bytes=maximum)
    assert cached.revision.byte_size == cache_module.MAX_PROFILE_BYTES
    too_large = maximum + b'x'
    staged = stage_bytes(cache, NAMESPACE, too_large)
    detail = make_detail(profile_bytes=maximum)
    receipt = replace(RECEIPT, sha256=hashlib.sha256(maximum).hexdigest(), byte_count=len(maximum))
    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, detail, receipt, staged, NOW)
    assert_no_temporary_files(cache)


def test_publish_rejects_replaced_staging_inode_without_deleting_replacement_or_alias(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)
    alias = tmp_path / 'owned-stage-alias.part'
    try:
        os.link(staged, alias)
    except OSError:
        pytest.skip('hard-link creation unavailable')
    replacement_bytes = b'replacement-not-owned-by-cache'
    replacement = tmp_path / 'replacement.part'
    replacement.write_bytes(replacement_bytes)
    original = cache_module._read_chunk
    replaced = False

    def replace_after_read(descriptor: int) -> bytes:
        nonlocal replaced
        chunk = original(descriptor)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, staged)
        return chunk

    monkeypatch.setattr(cache_module, '_read_chunk', replace_after_read)
    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)
    assert not list(cache.root.rglob('*.alog'))
    assert staged.read_bytes() == replacement_bytes
    assert alias.read_bytes() == PROFILE_BYTES
    assert not staged.with_suffix('.lock').exists()


def test_publish_rolls_back_profile_when_sidecar_replace_fails(
    cache: CacheStore, staged_download: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cache_module._replace_generated

    def fail_sidecar(root: Path, source: Path, destination: Path) -> None:
        if destination.suffix == '.json':
            raise OSError('/private/archive/server-controlled\npath')
        original(root, source, destination)

    monkeypatch.setattr(cache_module, '_replace_generated', fail_sidecar)
    with pytest.raises(CacheError) as raised:
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged_download, NOW)
    assert str(raised.value) == FAILURE_MESSAGES[FailureKind.CACHE_CORRUPT]
    assert raised.value.__cause__ is None
    assert not list(cache.root.rglob('*.alog'))
    assert not list(cache.root.rglob('*.json'))
    assert_no_temporary_files(cache)


def test_publication_cleanup_attempts_every_owned_artifact_after_first_failure(
    cache: CacheStore, staged_download: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_replace = cache_module._replace_generated
    original_discard = cache._discard_owned_path
    attempts: list[Path] = []
    failed_existing_path: Path | None = None

    def fail_sidecar(root: Path, source: Path, destination: Path) -> None:
        if destination.suffix == '.json':
            raise OSError('injected sidecar publication failure')
        original_replace(root, source, destination)

    def fail_first_cleanup(path: Path, identity: tuple[int, int]) -> None:
        nonlocal failed_existing_path
        attempts.append(path)
        if failed_existing_path is None and path.exists():
            failed_existing_path = path
            raise CacheError
        original_discard(path, identity)

    monkeypatch.setattr(cache_module, '_replace_generated', fail_sidecar)
    monkeypatch.setattr(cache, '_discard_owned_path', fail_first_cleanup)

    with pytest.raises(CacheError) as raised:
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged_download, NOW)

    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert any(path.suffix == '.json' for path in attempts)
    assert any(path.suffix == '.alog' for path in attempts)
    assert any(path.suffix == '.part' for path in attempts)
    assert any(path.suffix == '.lock' for path in attempts)
    lock_index = next(index for index, path in enumerate(attempts) if path.suffix == '.lock')
    assert all(
        index < lock_index
        for index, path in enumerate(attempts)
        if path.suffix in {'.alog', '.json'}
    )
    assert failed_existing_path is not None
    assert failed_existing_path.exists()
    assert not list(cache.root.rglob('*.alog'))
    assert not list(cache.root.rglob('*.json'))
    assert not staged_download.exists()
    assert not staged_download.with_suffix('.lock').exists()


def test_publication_fsync_failure_leaves_no_visible_or_temporary_artifact(
    cache: CacheStore, staged_download: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cache_module._fsync_descriptor
    calls = 0

    def fail_second_file(descriptor: int, *, directory: bool = False) -> None:
        nonlocal calls
        if not directory:
            calls += 1
            if calls == 2:
                raise OSError('sensitive profile path')
        original(descriptor, directory=directory)

    monkeypatch.setattr(cache_module, '_fsync_descriptor', fail_second_file)
    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged_download, NOW)
    assert not list(cache.root.rglob('*.alog'))
    assert not list(cache.root.rglob('*.json'))
    assert_no_temporary_files(cache)


def test_corrupt_cached_profile_is_not_openable(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    cached_revision.path.write_bytes(b'corrupt')
    with pytest.raises(CacheError) as raised:
        cache.validate(cached_revision)
    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT
    assert str(raised.value) == FAILURE_MESSAGES[FailureKind.CACHE_CORRUPT]


@pytest.mark.parametrize(
    'replacement',
    (
        b'{}',
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
    ),
)
def test_validate_fails_closed_for_malformed_or_duplicate_sidecar(
    cache: CacheStore,
    cached_revision: CachedRevision,
    replacement: bytes,
) -> None:
    cached_revision.sidecar_path.write_bytes(replacement)
    with pytest.raises(CacheError):
        cache.validate(cached_revision)


def test_validate_requires_strict_canonical_sidecar_bytes(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    parsed = json.loads(cached_revision.sidecar_path.read_bytes())
    cached_revision.sidecar_path.write_text(json.dumps(parsed, indent=2), encoding='utf-8')
    with pytest.raises(CacheError):
        cache.validate(cached_revision)


def test_validate_accepts_schema_v1_integral_duration_sidecar(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    parsed = json.loads(cached_revision.sidecar_path.read_bytes())
    assert isinstance(parsed['roast']['duration_seconds'], float)
    parsed['roast']['duration_seconds'] = 600
    cached_revision.sidecar_path.write_bytes(
        json.dumps(parsed, sort_keys=True, separators=(',', ':')).encode()
    )

    validated = cache.validate(cached_revision)

    assert validated.roast.duration_seconds == 600.0


def test_validate_rejects_sidecar_replacement_during_the_same_operation(
    cache: CacheStore,
    cached_revision: CachedRevision,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_content = cached_revision.sidecar_path.read_bytes()
    original_read = cache_module._read_chunk
    replaced = False

    def replace_after_read(descriptor: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor)
        if chunk and not replaced:
            replaced = True
            moved = cached_revision.sidecar_path.with_suffix('.moved')
            cached_revision.sidecar_path.replace(moved)
            cached_revision.sidecar_path.write_bytes(original_content)
        return chunk

    monkeypatch.setattr(cache_module, '_read_chunk', replace_after_read)
    with pytest.raises(CacheError):
        cache.validate(cached_revision)


def test_validate_rejects_sidecar_identity_not_matching_generated_path(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    sidecar = json.loads(cached_revision.sidecar_path.read_bytes())
    sidecar['revision']['sha256'] = 'f' * 64
    cached_revision.sidecar_path.write_bytes(
        json.dumps(sidecar, sort_keys=True, separators=(',', ':')).encode()
    )
    with pytest.raises(CacheError):
        cache.validate(cached_revision)


def test_validate_rejects_forged_cached_object_and_namespace_crossing(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    forged = replace(cached_revision, namespace=OTHER_ORIGIN_NAMESPACE)
    with pytest.raises(CacheError):
        cache.validate(forged)


def test_find_current_is_exact_and_namespace_isolated(cache: CacheStore) -> None:
    primary = publish_revision(cache)
    other_origin = publish_revision(cache, namespace=OTHER_ORIGIN_NAMESPACE)
    other_organization = publish_revision(cache, namespace=OTHER_ORGANIZATION_NAMESPACE)
    assert primary.path != other_origin.path != other_organization.path
    assert cache.find_current(NAMESPACE, ROAST_UUID, 1, RECEIPT.sha256) == primary
    assert cache.find_current(NAMESPACE, ROAST_UUID, 2, RECEIPT.sha256) is None
    assert cache.find_current(OTHER_ORIGIN_NAMESPACE, ROAST_UUID, 1, RECEIPT.sha256) == other_origin
    assert cache.stats(NAMESPACE).revision_count == 1
    assert cache.stats(OTHER_ORGANIZATION_NAMESPACE).revision_count == 1


def test_offline_rows_are_latest_per_roast_newest_first_filtered_and_stale(
    cache: CacheStore,
) -> None:
    archived_labels: list[dict[str, object]] = [
        {
            'label_uuid': 'bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb',
            'name': 'Read only label',
            'color': 'violet',
            'archived': True,
        }
    ]
    publish_revision(
        cache,
        roast_uuid=ROAST_UUID,
        revision_number=1,
        profile_bytes=b'first revision',
        roast_at=NOW - timedelta(days=2),
        downloaded_at=NOW,
    )
    latest = publish_revision(
        cache,
        roast_uuid=ROAST_UUID,
        revision_number=2,
        profile_bytes=b'second revision',
        roast_at=NOW - timedelta(days=2),
        downloaded_at=NOW + timedelta(seconds=1),
        title='Needle Search',
        labels=archived_labels,
    )
    newest = publish_revision(
        cache,
        roast_uuid=OTHER_ROAST_UUID,
        profile_bytes=b'other roast',
        roast_at=NOW - timedelta(days=1),
        downloaded_at=NOW + timedelta(seconds=2),
        machine='Other Machine',
        state='parse_failed',
    )

    page = cache.list_offline(NAMESPACE, ArchiveFilters())
    assert page.items == (newest, latest)
    assert latest.roast.labels[0].name == 'Read only label'
    assert latest.roast.labels[0].archived is True
    assert isinstance(latest.roast.labels, tuple)
    assert all(item.source.stale for item in page.items)
    assert cache.list_offline(NAMESPACE, ArchiveFilters(search='needle')).items == (latest,)
    assert cache.list_offline(NAMESPACE, ArchiveFilters(search=' needle ')).items == ()
    assert cache.list_offline(
        NAMESPACE, ArchiveFilters(state='parse_failed', machine='Other Machine')
    ).items == (newest,)
    assert cache.list_offline(
        NAMESPACE,
        ArchiveFilters(
            roast_at_from=NOW - timedelta(days=1, seconds=1),
            roast_at_to=NOW - timedelta(days=1) + timedelta(seconds=1),
        ),
    ).items == (newest,)


def test_cache_reopens_from_sidecars_after_restart(tmp_path: Path) -> None:
    root = tmp_path / 'cache'
    first = opened_cache(root)
    cached = publish_revision(first)
    second = opened_cache(root)
    assert second.list_offline(NAMESPACE, ArchiveFilters()).items == (cached,)
    assert second.validate(cached) == cached


def test_scan_rejects_symlink_reparse_and_unknown_static_entries(
    cache: CacheStore, cached_revision: CachedRevision, tmp_path: Path
) -> None:
    target = tmp_path / 'outside.alog'
    target.write_bytes(PROFILE_BYTES)
    cached_revision.path.unlink()
    try:
        cached_revision.path.symlink_to(target)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(CacheError):
        cache.stats(NAMESPACE)
    cached_revision.path.unlink()
    cached_revision.path.write_bytes(PROFILE_BYTES)
    unknown = cached_revision.path.parent / 'server-name.txt'
    unknown.write_text('static', encoding='utf-8')
    with pytest.raises(CacheError):
        cache.stats(NAMESPACE)


def test_generated_directory_symlink_is_rejected_without_escape(tmp_path: Path) -> None:
    root = tmp_path / 'cache'
    outside = tmp_path / 'outside'
    outside.mkdir()
    root.mkdir(mode=0o700)
    namespace_directory = root / NAMESPACE.key.removeprefix('namespace-sha256:')
    try:
        namespace_directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(CacheError):
        opened_cache(root)
    assert not list(outside.iterdir())


def test_root_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / 'target'
    target.mkdir()
    root = tmp_path / 'cache-link'
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(CacheError):
        opened_cache(root)


def test_prune_is_deterministic_lru_and_never_deletes_protected_identity(
    cache: CacheStore, three_cached_revisions: tuple[CachedRevision, ...]
) -> None:
    protected = frozenset({Path(os.path.relpath(three_cached_revisions[0].path))})
    remaining_limit = (
        three_cached_revisions[0].revision.byte_size
        + three_cached_revisions[2].revision.byte_size
    )
    stats = cache.prune(NAMESPACE, remaining_limit, protected)
    assert stats.revision_count == 2
    assert three_cached_revisions[0].path.exists()
    assert not three_cached_revisions[1].path.exists()
    assert three_cached_revisions[2].path.exists()


def test_protected_paths_use_canonical_file_identity(
    cache: CacheStore, cached_revision: CachedRevision, tmp_path: Path
) -> None:
    alias = tmp_path / 'open-profile-alias.alog'
    try:
        os.link(cached_revision.path, alias)
    except OSError:
        pytest.skip('hard-link creation unavailable')
    stats = cache.clear_unused(NAMESPACE, frozenset({alias}))
    assert stats.revision_count == 1
    assert cached_revision.path.exists()
    assert cached_revision.sidecar_path.exists()


def test_prune_holds_protected_identity_while_open_descriptor_remains_usable(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    with cached_revision.path.open('rb') as opened:
        stats = cache.clear_unused(NAMESPACE, frozenset({cached_revision.path}))
        assert stats.revision_count == 1
        assert opened.read() == PROFILE_BYTES
    assert cached_revision.path.exists()


@pytest.mark.parametrize('kind', ('missing', 'symlink', 'directory'))
def test_prune_aborts_before_deletion_when_any_protected_path_is_unverifiable(
    cache: CacheStore,
    three_cached_revisions: tuple[CachedRevision, ...],
    tmp_path: Path,
    kind: str,
) -> None:
    protected = tmp_path / 'protected'
    if kind == 'symlink':
        try:
            protected.symlink_to(three_cached_revisions[0].path)
        except OSError:
            pytest.skip('symlink creation unavailable')
    elif kind == 'directory':
        protected.mkdir()

    with pytest.raises(CacheError) as raised:
        cache.clear_unused(NAMESPACE, frozenset({protected}))

    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert all(item.path.exists() and item.sidecar_path.exists() for item in three_cached_revisions)


def test_prune_aborts_before_deletion_for_inaccessible_protected_path(
    cache: CacheStore,
    three_cached_revisions: tuple[CachedRevision, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = three_cached_revisions[0].path

    def inaccessible(_path: Path) -> int:
        raise filesystem_module.FilesystemError('injected access denial')

    monkeypatch.setattr(filesystem_module, 'open_path_readonly', inaccessible)
    with pytest.raises(CacheError):
        cache.clear_unused(NAMESPACE, frozenset({protected}))
    assert all(item.path.exists() and item.sidecar_path.exists() for item in three_cached_revisions)


def test_prune_detects_protected_path_replacement_before_deleting_candidates(
    cache: CacheStore,
    three_cached_revisions: tuple[CachedRevision, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = three_cached_revisions[0].path
    moved = tmp_path / 'original-open-profile.alog'
    replacement = tmp_path / 'replacement.alog'
    replacement.write_bytes(b'replacement')
    os.chmod(replacement, 0o600)
    original_open = filesystem_module.open_path_readonly
    replaced = False

    def replace_after_open(path: Path) -> int:
        nonlocal replaced
        descriptor = original_open(path)
        if path == protected and not replaced:
            replaced = True
            os.replace(protected, moved)
            os.replace(replacement, protected)
        return descriptor

    monkeypatch.setattr(filesystem_module, 'open_path_readonly', replace_after_open)
    with pytest.raises(CacheError):
        cache.clear_unused(NAMESPACE, frozenset({protected}))
    assert three_cached_revisions[1].path.exists()
    assert three_cached_revisions[2].path.exists()
    assert protected.read_bytes() == b'replacement'
    assert moved.read_bytes() == b'oldest'


def test_pair_cleanup_attempts_profile_after_sidecar_removal_failure(
    cache: CacheStore,
    cached_revision: CachedRevision,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_discard = cache._discard_owned_path
    attempted: list[Path] = []

    def fail_sidecar(path: Path, identity: tuple[int, int]) -> None:
        attempted.append(path)
        if path == cached_revision.sidecar_path:
            raise CacheError
        original_discard(path, identity)

    monkeypatch.setattr(cache, '_discard_owned_path', fail_sidecar)
    with pytest.raises(CacheError) as raised:
        cache.clear_unused(NAMESPACE, frozenset())
    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert attempted[:2] == [cached_revision.sidecar_path, cached_revision.path]
    assert cached_revision.sidecar_path.exists()
    assert not cached_revision.path.exists()


def test_prune_lru_tie_breaks_by_generated_path(
    cache: CacheStore,
) -> None:
    first = publish_revision(
        cache, roast_uuid=ROAST_UUID, profile_bytes=b'a', downloaded_at=NOW
    )
    second = publish_revision(
        cache, roast_uuid=OTHER_ROAST_UUID, profile_bytes=b'b', downloaded_at=NOW
    )
    expected_removed = min((first, second), key=lambda item: item.path.as_posix())
    cache.prune(NAMESPACE, limit_bytes=1, protected_paths=frozenset())
    assert not expected_removed.path.exists()


def test_clear_unused_removes_every_unprotected_pair(
    cache: CacheStore, three_cached_revisions: tuple[CachedRevision, ...]
) -> None:
    protected = frozenset({three_cached_revisions[1].path.resolve()})
    stats = cache.clear_unused(NAMESPACE, protected)
    assert stats.byte_count == three_cached_revisions[1].revision.byte_size
    assert stats.revision_count == 1
    assert three_cached_revisions[1].path.exists()
    assert three_cached_revisions[1].sidecar_path.exists()
    for index in (0, 2):
        assert not three_cached_revisions[index].path.exists()
        assert not three_cached_revisions[index].sidecar_path.exists()


def test_prune_rejects_bool_negative_and_noninteger_limits(cache: CacheStore) -> None:
    for value in (True, -1, 1.5, '1'):
        with pytest.raises(ValueError, match='limit'):
            cache.prune(NAMESPACE, value, frozenset())  # type: ignore[arg-type]


def test_concurrent_staging_creation_and_close_are_linearized_without_descriptor_leaks(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(cache._state_lock, type(threading.RLock()))
    registered = threading.Event()
    close_started = threading.Event()
    allow_creation_to_return = threading.Event()
    captured: list[cache_module._OwnedStage] = []
    created: list[tuple[Path, BinaryIO]] = []
    thread_failures: list[BaseException] = []
    original_create = cache._new_staging_file_locked
    original_cleanup = cache._cleanup_owned_stage

    def state_lock_is_owned() -> bool:
        is_owned = getattr(cache._state_lock, '_is_owned', None)
        return callable(is_owned) and bool(is_owned())

    def capture_registered_stage(namespace_key: str) -> tuple[Path, BinaryIO]:
        assert state_lock_is_owned()
        path, output = original_create(namespace_key)
        captured.append(cache._staging[path])
        registered.set()
        assert allow_creation_to_return.wait(timeout=5)
        return path, output

    def verify_closed_cleanup(stage: cache_module._OwnedStage) -> bool:
        assert state_lock_is_owned()
        assert cache._closed
        return original_cleanup(stage)

    def create() -> None:
        try:
            created.append(cache.new_staging_file(NAMESPACE))
        except BaseException as exc:
            thread_failures.append(exc)

    def close() -> None:
        try:
            close_started.set()
            cache.close()
        except BaseException as exc:
            thread_failures.append(exc)

    monkeypatch.setattr(cache, '_new_staging_file_locked', capture_registered_stage)
    monkeypatch.setattr(cache, '_cleanup_owned_stage', verify_closed_cleanup)
    creator = threading.Thread(target=create)
    creator.start()
    assert registered.wait(timeout=5)
    closer = threading.Thread(target=close)
    closer.start()
    assert close_started.wait(timeout=5)
    allow_creation_to_return.set()
    creator.join(timeout=10)
    closer.join(timeout=10)

    assert not creator.is_alive() and not closer.is_alive()
    assert thread_failures == []
    assert len(created) == 1 and len(captured) == 1
    path, output = created[0]
    assert output.closed
    assert not path.exists()
    assert not path.with_suffix('.lock').exists()
    with pytest.raises(OSError):
        os.fstat(captured[0].lock_descriptor)
    with pytest.raises(CacheError):
        cache.new_staging_file(NAMESPACE)
    cache.close()


def test_discard_restores_replacement_swapped_after_identity_observation_before_move(
    cache: CacheStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged, output = cache.new_staging_file(NAMESPACE)
    output.write(PROFILE_BYTES)
    output.flush()
    alias = tmp_path / 'owned-stage-hardlink.part'
    try:
        os.link(staged, alias)
    except OSError:
        pytest.skip('hard-link creation unavailable')
    replacement_bytes = b'replacement-after-identity-observation'
    replacement = tmp_path / 'replacement.part'
    replacement.write_bytes(replacement_bytes)
    original_move = filesystem_module._move_generated_no_replace
    replaced = False

    def replace_before_atomic_move(
        source: Path, destination: Path, directory_descriptor: int
    ) -> None:
        nonlocal replaced
        if source == staged and not replaced:
            replaced = True
            os.replace(replacement, staged)
        original_move(source, destination, directory_descriptor)

    monkeypatch.setattr(
        filesystem_module, '_move_generated_no_replace', replace_before_atomic_move
    )
    with pytest.raises(CacheError) as raised:
        cache.discard_staging(staged)

    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert replaced
    assert output.closed
    assert staged.read_bytes() == replacement_bytes
    assert alias.read_bytes() == PROFILE_BYTES
    assert not staged.with_suffix('.lock').exists()
    assert not list(cache.root.rglob('.artisan-quarantine-*'))


def test_discard_does_not_remove_replacement_installed_after_atomic_move(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged, output = cache.new_staging_file(NAMESPACE)
    output.write(PROFILE_BYTES)
    output.flush()
    replacement_bytes = b'replacement-after-atomic-move'
    original_unlink = filesystem_module._unlink_quarantined_generated_file
    installed = False

    def install_replacement_then_unlink(
        root: Path,
        quarantine_path: Path,
        expected_identity: tuple[int, int],
        directory_descriptor: int,
    ) -> bool:
        nonlocal installed
        if not installed and expected_identity == cache._staging[staged].identity:
            assert not staged.exists()
            staged.write_bytes(replacement_bytes)
            os.chmod(staged, 0o600)
            installed = True
        return original_unlink(
            root, quarantine_path, expected_identity, directory_descriptor
        )

    monkeypatch.setattr(
        filesystem_module,
        '_unlink_quarantined_generated_file',
        install_replacement_then_unlink,
    )
    cache.discard_staging(staged)

    assert installed
    assert output.closed
    assert staged.read_bytes() == replacement_bytes
    assert not staged.with_suffix('.lock').exists()
    assert not list(cache.root.rglob('.artisan-quarantine-*'))


def test_secure_removal_failure_restores_owned_path_and_still_releases_stage_lock(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged, output = cache.new_staging_file(NAMESPACE)
    output.write(PROFILE_BYTES)
    output.flush()
    stage_identity = cache._staging[staged].identity
    original_unlink = filesystem_module._unlink_quarantined_generated_file
    failed = False

    def fail_first_unlink(
        root: Path,
        quarantine_path: Path,
        expected_identity: tuple[int, int],
        directory_descriptor: int,
    ) -> bool:
        nonlocal failed
        if not failed and expected_identity == stage_identity:
            failed = True
            raise OSError('injected quarantine removal failure')
        return original_unlink(
            root, quarantine_path, expected_identity, directory_descriptor
        )

    monkeypatch.setattr(
        filesystem_module, '_unlink_quarantined_generated_file', fail_first_unlink
    )
    with pytest.raises(CacheError):
        cache.discard_staging(staged)

    assert failed
    assert output.closed
    assert staged.read_bytes() == PROFILE_BYTES
    assert not staged.with_suffix('.lock').exists()
    assert not list(cache.root.rglob('.artisan-quarantine-*'))


def test_staging_discard_and_close_remove_all_owned_lock_pairs(cache: CacheStore) -> None:
    first_path, first_output = cache.new_staging_file(NAMESPACE)
    second_path, second_output = cache.new_staging_file(NAMESPACE)
    first_output.write(b'first')
    second_output.write(b'second')
    assert first_path.with_suffix('.lock').exists()
    assert second_path.with_suffix('.lock').exists()

    cache.discard_staging(first_path)
    assert first_output.closed
    assert not first_path.exists()
    assert not first_path.with_suffix('.lock').exists()

    cache.close()
    assert second_output.closed
    assert not second_path.exists()
    assert not second_path.with_suffix('.lock').exists()
    cache.close()
    with pytest.raises(CacheError):
        cache.new_staging_file(NAMESPACE)


def test_close_attempts_every_stage_after_first_owned_removal_failure(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path, first_output = cache.new_staging_file(NAMESPACE)
    second_path, second_output = cache.new_staging_file(NAMESPACE)
    first_output.write(b'first')
    second_output.write(b'second')
    original_discard = cache._discard_owned_path
    failed = False

    def fail_first(path: Path, identity: tuple[int, int]) -> None:
        nonlocal failed
        if not failed and path.suffix == '.part':
            failed = True
            raise CacheError
        original_discard(path, identity)

    monkeypatch.setattr(cache, '_discard_owned_path', fail_first)
    with pytest.raises(CacheError) as raised:
        cache.close()
    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert first_output.closed and second_output.closed
    assert first_path.exists()
    assert not first_path.with_suffix('.lock').exists()
    assert not second_path.exists()
    assert not second_path.with_suffix('.lock').exists()
    assert cache._staging == {}
    cache.close()
    assert first_path.exists()


def test_restart_collects_orphan_profile_and_temp(cache: CacheStore) -> None:
    namespace_directory = cache.root / NAMESPACE.key.removeprefix('namespace-sha256:')
    roast_directory = namespace_directory / 'roasts' / ROAST_UUID.hex
    roast_directory.mkdir(parents=True, mode=0o700)
    orphan = roast_directory / f'1-{"a" * 64}.alog'
    orphan.write_bytes(b'orphan')
    temporary_directory = namespace_directory / 'tmp'
    temporary_directory.mkdir(mode=0o700)
    temporary = temporary_directory / f'{UUID(int=7).hex}.part'
    temporary.write_bytes(b'temporary')
    stale_time = (NOW - timedelta(days=2)).timestamp()
    os.utime(temporary, (stale_time, stale_time))
    restarted = opened_cache(cache.root)
    assert not orphan.exists()
    assert not temporary.exists()
    assert restarted.stats(NAMESPACE).revision_count == 0


def test_restart_removes_only_identity_matching_quarantine_residue(
    cache: CacheStore,
) -> None:
    temporary_directory = (
        cache.root / NAMESPACE.key.removeprefix('namespace-sha256:') / 'tmp'
    )
    temporary_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned = temporary_directory / 'owned-residue'
    owned.write_bytes(PROFILE_BYTES)
    owned_stat = owned.stat()
    owned_quarantine = temporary_directory / (
        f'.artisan-quarantine-{owned_stat.st_dev:x}-{owned_stat.st_ino:x}-'
        f'{UUID(int=9).hex}'
    )
    owned.rename(owned_quarantine)
    restarted = opened_cache(cache.root)
    assert not owned_quarantine.exists()
    restarted.close()

    expected = temporary_directory / 'expected-identity'
    expected.write_bytes(b'expected')
    expected_stat = expected.stat()
    expected.unlink()
    replacement_bytes = b'unowned-quarantine-replacement'
    mismatched_quarantine = temporary_directory / (
        f'.artisan-quarantine-{expected_stat.st_dev:x}-{expected_stat.st_ino:x}-'
        f'{UUID(int=10).hex}'
    )
    mismatched_quarantine.write_bytes(replacement_bytes)

    with pytest.raises(CacheError) as raised:
        opened_cache(cache.root)

    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert mismatched_quarantine.read_bytes() == replacement_bytes


def test_restart_cleanup_never_converts_untrusted_temporary_mtime(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace_directory = cache.root / NAMESPACE.key.removeprefix('namespace-sha256:')
    temporary_directory = namespace_directory / 'tmp'
    temporary_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = temporary_directory / f'{UUID(int=8).hex}.part'
    temporary.write_bytes(b'abandoned')
    old = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
    os.utime(temporary, (old, old))

    class RaisingDateTime(datetime):
        @classmethod
        def fromtimestamp(cls, *_args: object, **_kwargs: object) -> datetime:
            raise OverflowError('injected untrusted timestamp')

    monkeypatch.setattr(cache_module, 'datetime', RaisingDateTime)
    restarted = opened_cache(cache.root)
    assert not temporary.exists()
    restarted.close()


def test_active_old_stage_survives_other_process_maintenance_then_discard_and_crash_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'cache'
    script = r'''
import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID
from artisanlib.roastserver.cache import CacheStore
from artisanlib.roastserver.contract import Namespace
root, namespace_json, ready, release = sys.argv[1:]
namespace_data = json.loads(namespace_json)
namespace = Namespace(namespace_data[0], UUID(namespace_data[1]), namespace_data[2])
store = CacheStore(Path(root))
store.open()
path, output = store.new_staging_file(namespace)
with output:
    output.write(b'active-stage')
Path(ready).write_text(str(path), encoding='utf-8')
while not Path(release).exists():
    time.sleep(0.01)
if Path(release).read_text(encoding='utf-8') == 'discard':
    store.discard_staging(path)
    store.close()
else:
    os._exit(17)
'''
    namespace_json = json.dumps([NAMESPACE.origin, str(NAMESPACE.organization_id), NAMESPACE.key])

    def start_owner(name: str) -> tuple[subprocess.Popen[bytes], Path, Path, Path]:
        ready = tmp_path / f'{name}.ready'
        release = tmp_path / f'{name}.release'
        process = subprocess.Popen(
            [
                sys.executable,
                '-c',
                script,
                os.fspath(root),
                namespace_json,
                os.fspath(ready),
                os.fspath(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _attempt in range(500):
            if ready.exists():
                break
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                pytest.fail(stderr.decode('utf-8', errors='replace'))
            import time
            time.sleep(0.01)
        else:
            process.kill()
            pytest.fail('stage owner did not reach barrier')
        return process, Path(ready.read_text(encoding='utf-8')), ready, release

    owner, active_path, _ready, release = start_owner('active')
    old = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
    os.utime(active_path, (old, old))
    os.utime(active_path.with_suffix('.lock'), (old, old))
    observer = opened_cache(root)
    assert active_path.exists()
    assert active_path.with_suffix('.lock').exists()
    observer.close()
    release.write_text('discard', encoding='utf-8')
    stdout, stderr = owner.communicate(timeout=20)
    assert owner.returncode == 0, (stdout + stderr).decode('utf-8', errors='replace')
    assert not active_path.exists()
    assert not active_path.with_suffix('.lock').exists()

    crashed, abandoned_path, _ready, release = start_owner('crashed')
    release.write_text('crash', encoding='utf-8')
    crashed.communicate(timeout=20)
    assert crashed.returncode == 17
    assert abandoned_path.exists()
    recovered = opened_cache(root)
    assert not abandoned_path.exists()
    assert not abandoned_path.with_suffix('.lock').exists()
    recovered.close()


def test_two_processes_publish_same_destination_after_first_root_and_stage_barriers(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'cache'
    script = r'''
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID
from artisanlib.roastserver.api import DownloadReceipt
from artisanlib.roastserver.cache import CacheStore
from artisanlib.roastserver.contract import Namespace, parse_roast_detail
root, namespace_json, payload_json, profile_hex, start, ready, go = sys.argv[1:]
while not Path(start).exists():
    time.sleep(0.01)
namespace_data = json.loads(namespace_json)
namespace = Namespace(namespace_data[0], UUID(namespace_data[1]), namespace_data[2])
payload = json.loads(payload_json)
profile = bytes.fromhex(profile_hex)
detail = parse_roast_detail(payload)
revision = detail.current_revision
assert revision is not None
store = CacheStore(Path(root))
store.open()
path, output = store.new_staging_file(namespace)
with output:
    output.write(profile)
Path(ready).touch()
while not Path(go).exists():
    time.sleep(0.01)
receipt = DownloadReceipt(detail.roast_uuid, revision.revision_number, revision.sha256,
                          revision.byte_size,
                          f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog')
store.publish(namespace, detail, receipt, path,
              datetime.fromisoformat('2026-08-01T12:34:56.123456+00:00'))
store.close()
'''
    start = tmp_path / 'start'
    go = tmp_path / 'go'
    processes: list[subprocess.Popen[bytes]] = []
    namespace_json = json.dumps([NAMESPACE.origin, str(NAMESPACE.organization_id), NAMESPACE.key])
    profile = b'same destination bytes'
    payload_json = json.dumps(detail_payload(profile_bytes=profile), separators=(',', ':'))
    ready_paths = [tmp_path / f'ready-{index}' for index in range(2)]
    for ready in ready_paths:
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    script,
                    os.fspath(root),
                    namespace_json,
                    payload_json,
                    profile.hex(),
                    os.fspath(start),
                    os.fspath(ready),
                    os.fspath(go),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    start.touch()
    for _attempt in range(500):
        if all(path.exists() for path in ready_paths):
            break
        import time
        time.sleep(0.01)
    else:
        for process in processes:
            process.kill()
        pytest.fail('publishers did not reach stage barrier')
    go.touch()
    failures: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode != 0:
            failures.append((stdout + stderr).decode('utf-8', errors='replace'))
    assert not failures
    reopened = opened_cache(root)
    assert reopened.stats(NAMESPACE).revision_count == 1
    cached = reopened.list_offline(NAMESPACE, ArchiveFilters()).items[0]
    assert reopened.validate(cached).path.read_bytes() == profile
    reopened.close()


def test_canonical_sidecar_requires_cached_revision_to_equal_roast_revision_count(
    cache: CacheStore, cached_revision: CachedRevision
) -> None:
    sidecar = json.loads(cached_revision.sidecar_path.read_bytes())
    sidecar['roast']['revision_count'] = 2
    cached_revision.sidecar_path.write_bytes(
        json.dumps(sidecar, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    )
    with pytest.raises(CacheError):
        cache.stats(NAMESPACE)


def test_cache_metadata_round_trip_distinguishes_objects_empty_and_pair_arrays(
    cache: CacheStore,
) -> None:
    metadata: dict[str, object] = {
        'empty_array': [],
        'empty_object': {},
        'pair_array': [['key', 1]],
        'nested': [[], {}, [['nested-key', 2]]],
    }
    detail = make_detail(metadata=metadata)
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)
    cached = cache.publish(NAMESPACE, detail, RECEIPT, staged, NOW)

    sidecar = json.loads(cached.sidecar_path.read_bytes())
    assert sidecar['revision']['metadata'] == metadata
    assert cache.validate(cached).revision.metadata == detail.current_metadata


def test_cache_and_api_share_exact_filter_validation_semantics(cache: CacheStore) -> None:
    publish_revision(cache, title='Needle')
    assert cache.list_offline(NAMESPACE, ArchiveFilters(search=' Needle ')).items == ()
    invalid = (
        ArchiveFilters(search=''),
        ArchiveFilters(search='x' * 201),
        ArchiveFilters(state='unknown'),  # type: ignore[arg-type]
        ArchiveFilters(machine=''),
        ArchiveFilters(machine='x' * 101),
    )
    for filters in invalid:
        with pytest.raises(ValueError):
            cache.list_offline(NAMESPACE, filters)


def test_replace_generated_closes_source_descriptor_when_destination_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'root'
    source_directory = root / 'source'
    destination_directory = root / 'destination'
    source_directory.mkdir(parents=True)
    destination_directory.mkdir()
    source = source_directory / 'source.part'
    destination = destination_directory / 'destination.alog'
    source.write_bytes(b'profile')
    opened: list[int] = []
    original_open = filesystem_module.open_generated_directory

    def fail_destination(open_root: Path, directory: Path, **kwargs: Any) -> int:
        if directory == destination_directory:
            raise filesystem_module.FilesystemError('injected destination open failure')
        descriptor = original_open(open_root, directory, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(filesystem_module, 'open_generated_directory', fail_destination)
    with pytest.raises(filesystem_module.FilesystemError):
        filesystem_module.replace_generated(root, source, destination)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_cache_error_redacts_os_paths_controls_and_server_strings(
    cache: CacheStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)

    def fail_replace(_root: Path, _source: Path, _destination: Path) -> None:
        raise OSError('/private/customer/server-name\ncontrol')

    monkeypatch.setattr(cache_module, '_replace_generated', fail_replace)
    caplog.set_level(logging.ERROR, logger='artisanlib.roastserver.cache')
    with pytest.raises(CacheError) as raised:
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)
    assert caplog.messages == [
        'Roast Server cache publication failure: phase=publish_profile '
        'cleanup=ok release=ok'
    ]
    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert str(raised.value) == FAILURE_MESSAGES[FailureKind.CACHE_CORRUPT]
    assert raised.value.__cause__ is None
    assert '/private' not in repr(raised.value)
    assert 'server-name' not in repr(raised.value)


@pytest.mark.parametrize(
    ('changed_field', 'accepted'),
    (('st_ctime_ns', True), ('st_mtime_ns', False)),
)
def test_windows_cache_entry_comparison_ignores_only_ctime_discrepancy(
    cache: CacheStore,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
    accepted: bool,
) -> None:
    original_entry_stat = filesystem_module.generated_entry_stat

    def shifted_entry_stat(root: Path, path: Path) -> os.stat_result:
        value = original_entry_stat(root, path)
        fields = {
            'st_dev': value.st_dev,
            'st_ino': value.st_ino,
            'st_size': value.st_size,
            'st_mtime_ns': value.st_mtime_ns,
            'st_ctime_ns': value.st_ctime_ns,
        }
        fields[changed_field] += 1
        return cast(os.stat_result, SimpleNamespace(**fields))

    monkeypatch.setattr(cache_module, '_ENTRY_CTIME_RELIABLE', False, raising=False)
    monkeypatch.setattr(
        filesystem_module, 'generated_entry_stat', shifted_entry_stat
    )
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)

    if accepted:
        cached = cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)
        assert cache.validate(cached) == cached
    else:
        with pytest.raises(CacheError):
            cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)


def test_cache_copy_failure_logs_fixed_subphase(
    cache: CacheStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)
    original_verify = filesystem_module.verify_private_permissions

    def fail_stage_permissions(path: Path, mode: int) -> None:
        if path == staged:
            raise filesystem_module.FilesystemError('/private/customer/profile')
        original_verify(path, mode)

    monkeypatch.setattr(
        filesystem_module, 'verify_private_permissions', fail_stage_permissions
    )
    caplog.set_level(logging.ERROR, logger='artisanlib.roastserver.cache')

    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)

    assert caplog.messages == [
        'Roast Server cache copy failure: phase=verify_source_permissions',
        'Roast Server cache publication failure: phase=copy_profile '
        'cleanup=ok release=ok',
    ]
    assert '/private' not in caplog.text
    assert 'customer' not in caplog.text


def test_portable_windows_seam_runs_complete_cache_publish_validate_and_remove_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = PortableWindowsCacheNative()
    monkeypatch.setattr(filesystem_module, '_IS_WINDOWS', True)
    monkeypatch.setattr(filesystem_module, '_HAS_DIRECTORY_FDS', False)
    monkeypatch.setattr(filesystem_module, '_WINDOWS_NATIVE', native)
    def acquire(_descriptor: int) -> None:
        return

    def release(_descriptor: int) -> None:
        return

    def try_acquire(_descriptor: int) -> bool:
        return True

    monkeypatch.setattr(filesystem_module, 'acquire_file_lock', acquire)
    monkeypatch.setattr(filesystem_module, 'release_file_lock', release)
    monkeypatch.setattr(filesystem_module, 'try_acquire_file_lock', try_acquire)

    store = opened_cache(tmp_path / 'cache')
    cached = publish_revision(store)
    assert store.validate(cached) == cached
    stats = store.clear_unused(NAMESPACE, frozenset())
    store.close()

    assert stats.revision_count == 0
    assert {destination.suffix for _source, destination in native.replacements} == {
        '.alog',
        '.json',
    }
    assert any(mode == 0o700 for _path, mode in native.permissions)
    assert any(mode == 0o600 for _path, mode in native.permissions)
    assert any(kind == 'descriptor' for kind, _value in native.flushes)
    assert any(kind == 'directory' for kind, _value in native.flushes)
    assert native.removals == []
    assert len(native.verified_removals) >= 4
    assert all(path.name.startswith('.artisan-quarantine-') for path in native.verified_removals)
    assert any(source == cached.path for source, _destination in native.quarantine_moves)
    assert any(source == cached.sidecar_path for source, _destination in native.quarantine_moves)


def test_portable_windows_full_store_rejects_native_reparse_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'cache'
    namespace_directory = root / ('a' * 64)
    namespace_directory.mkdir(parents=True)
    native = PortableWindowsCacheNative()
    native.reparse_path = namespace_directory
    monkeypatch.setattr(filesystem_module, '_IS_WINDOWS', True)
    monkeypatch.setattr(filesystem_module, '_HAS_DIRECTORY_FDS', False)
    monkeypatch.setattr(filesystem_module, '_WINDOWS_NATIVE', native)

    def acquire(_descriptor: int) -> None:
        return

    def release(_descriptor: int) -> None:
        return

    monkeypatch.setattr(filesystem_module, 'acquire_file_lock', acquire)
    monkeypatch.setattr(filesystem_module, 'release_file_lock', release)

    with pytest.raises(CacheError):
        opened_cache(root)


@pytest.mark.win32
@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows cache filesystem')
def test_windows_runtime_cache_applies_acl_replaces_flushes_validates_and_deletes(
    tmp_path: Path,
) -> None:
    store = opened_cache(tmp_path / 'cache')
    cached = publish_revision(store)
    filesystem_module.verify_private_permissions(store.root, 0o700)
    filesystem_module.verify_private_permissions(cached.path, 0o600)
    filesystem_module.verify_private_permissions(cached.sidecar_path, 0o600)
    assert store.validate(cached) == cached
    assert store.clear_unused(NAMESPACE, frozenset()).revision_count == 0
    assert not cached.path.exists()
    assert not cached.sidecar_path.exists()
    store.close()


@pytest.mark.win32
@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows reparse behavior')
def test_windows_runtime_cache_rejects_reparse_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'cache'
    outside = tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    namespace_directory = root / ('a' * 64)
    try:
        namespace_directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink creation is unavailable')
    with pytest.raises(CacheError):
        opened_cache(root)
    assert not list(outside.iterdir())


def test_windows_nonblocking_stage_lock_uses_exact_contention_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / 'stage.lock'
    lock_path.write_bytes(b'0')
    descriptor = os.open(lock_path, os.O_RDWR)
    calls: list[tuple[int, int, int]] = []
    blocked = True

    def locking(fd: int, mode: int, count: int) -> None:
        calls.append((fd, mode, count))
        if blocked:
            raise OSError(errno.EACCES, 'lock is held')

    fake_msvcrt = type(
        'Msvcrt', (), {'locking': staticmethod(locking), 'LK_NBLCK': 7}
    )
    original_import = filesystem_module.importlib.import_module

    def import_module(name: str) -> object:
        if name == 'msvcrt':
            return fake_msvcrt
        return original_import(name)

    monkeypatch.setattr(filesystem_module.importlib, 'import_module', import_module)
    try:
        assert not filesystem_module.try_acquire_file_lock(descriptor, is_windows=True)
        blocked = False
        assert filesystem_module.try_acquire_file_lock(descriptor, is_windows=True)
    finally:
        os.close(descriptor)
    assert calls == [(descriptor, 7, 1), (descriptor, 7, 1)]


def test_windows_quarantine_move_is_write_through_and_never_replaces() -> None:
    calls: list[tuple[object, ...]] = []
    result = True
    last_error = 0

    def move_file(*arguments: object) -> bool:
        calls.append(arguments)
        return result

    layer = object.__new__(filesystem_module._WindowsNativeLayer)
    layer._kernel32 = type('Kernel', (), {'MoveFileExW': staticmethod(move_file)})()
    layer._ctypes = type(
        'Ctypes', (), {'get_last_error': staticmethod(lambda: last_error)}
    )()
    source = Path('profile.alog')
    quarantine = Path('.artisan-quarantine-11111111111141118111111111111111')
    layer.move_no_replace(source, quarantine)
    assert calls == [
        (os.fspath(source), os.fspath(quarantine), layer._MOVEFILE_WRITE_THROUGH)
    ]

    result = False
    last_error = 183
    with pytest.raises(FileExistsError):
        layer.move_no_replace(source, quarantine)


def test_windows_quarantine_unlink_verifies_and_deletes_through_same_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_access: list[int] = []
    deleted_handles: list[int] = []
    closed_handles: list[int] = []
    closed_descriptors: list[int] = []
    identity = (17, 23)
    layer = object.__new__(filesystem_module._WindowsNativeLayer)

    def open_chain(
        _path: Path,
        *,
        final_access: int,
        final_disposition: int = layer._OPEN_EXISTING,
    ) -> list[int]:
        del final_disposition
        opened_access.append(final_access)
        return [10, 20, 30]

    def open_osfhandle(handle: int, _flags: int) -> int:
        return handle + 10

    def get_osfhandle(descriptor: int) -> int:
        return descriptor - 10

    fake_msvcrt = type(
        'Msvcrt',
        (),
        {
            'open_osfhandle': staticmethod(open_osfhandle),
            'get_osfhandle': staticmethod(get_osfhandle),
        },
    )
    original_import = filesystem_module.importlib.import_module

    def import_module(name: str) -> object:
        if name == 'msvcrt':
            return fake_msvcrt
        return original_import(name)

    monkeypatch.setattr(layer, '_open_chain', open_chain)
    monkeypatch.setattr(layer, '_unlink_handle', deleted_handles.append)
    monkeypatch.setattr(layer, '_close', closed_handles.append)
    monkeypatch.setattr(filesystem_module.importlib, 'import_module', import_module)
    def fstat(_descriptor: int) -> object:
        return type('Stat', (), {'st_dev': 17, 'st_ino': 23})()

    monkeypatch.setattr(filesystem_module.os, 'fstat', fstat)
    monkeypatch.setattr(filesystem_module.os, 'close', closed_descriptors.append)

    assert layer.unlink_if_identity(Path('quarantine.alog'), identity)
    assert deleted_handles == [30]
    assert closed_descriptors == [40]
    assert closed_handles == [20, 10]
    assert opened_access == [
        layer._DELETE
        | layer._GENERIC_READ
        | layer._FILE_READ_ATTRIBUTES
        | layer._FILE_WRITE_ATTRIBUTES
    ]

    assert not layer.unlink_if_identity(Path('quarantine.alog'), (17, 24))
    assert deleted_handles == [30]


def test_windows_replace_seam_is_write_through_and_replaces_atomically() -> None:
    calls: list[tuple[object, ...]] = []
    result = True
    last_error = 0

    def move_file(*arguments: object) -> bool:
        calls.append(arguments)
        return result

    layer = object.__new__(filesystem_module._WindowsNativeLayer)
    layer._kernel32 = type('Kernel', (), {'MoveFileExW': staticmethod(move_file)})()
    layer._ctypes = type(
        'Ctypes', (), {'get_last_error': staticmethod(lambda: last_error)}
    )()
    source = Path('profile.part')
    destination = Path('profile.alog')
    layer.replace(source, destination)
    assert calls == [
        (
            os.fspath(source),
            os.fspath(destination),
            layer._MOVEFILE_REPLACE_EXISTING | layer._MOVEFILE_WRITE_THROUGH,
        )
    ]

    result = False
    last_error = 5
    with pytest.raises(OSError) as raised:
        layer.replace(source, destination)
    assert raised.value.errno == 5


def test_windows_replacefile_seam_captures_backup_and_writes_through() -> None:
    calls: list[tuple[object, ...]] = []
    result = True
    last_error = 0

    def replace_file(*arguments: object) -> bool:
        calls.append(arguments)
        return result

    layer = object.__new__(filesystem_module._WindowsNativeLayer)
    layer._kernel32 = type(
        'Kernel', (), {'ReplaceFileW': staticmethod(replace_file)})()
    layer._ctypes = type(
        'Ctypes', (), {'get_last_error': staticmethod(lambda: last_error)}
    )()
    replacement = Path('profile.part')
    destination = Path('profile.alog')
    backup = Path('.artisan-backup.alog')

    layer.replace_with_backup(replacement, destination, backup)

    assert calls == [(
        os.fspath(destination),
        os.fspath(replacement),
        os.fspath(backup),
        layer._REPLACEFILE_WRITE_THROUGH,
        None,
        None,
    )]

    result = False
    last_error = 5
    with pytest.raises(OSError) as raised:
        layer.replace_with_backup(replacement, destination, backup)
    assert raised.value.errno == 5


@pytest.mark.parametrize(
    'outcome',
    ['no-changes', 'destination-missing-backup', 'replacement-installed-backup'],
)
def test_windows_replacefile_false_reports_every_documented_observed_outcome(
    tmp_path: Path, outcome: str
) -> None:
    replacement = tmp_path / 'profile.part'
    destination = tmp_path / 'profile.alog'
    backup = tmp_path / '.artisan-backup.alog'
    replacement.write_bytes(b'replacement')
    destination.write_bytes(b'destination')
    replacement_identity = (
        replacement.stat().st_dev, replacement.stat().st_ino)
    destination_identity = (
        destination.stat().st_dev, destination.stat().st_ino)

    def replace_file(*_arguments: object) -> bool:
        if outcome != 'no-changes':
            os.replace(destination, backup)
        if outcome == 'replacement-installed-backup':
            os.replace(replacement, destination)
        return False

    layer = object.__new__(filesystem_module._WindowsNativeLayer)
    layer._kernel32 = type(
        'Kernel', (), {'ReplaceFileW': staticmethod(replace_file)})()
    layer._ctypes = type(
        'Ctypes', (), {'get_last_error': staticmethod(lambda: 5)})()

    with pytest.raises(filesystem_module.WindowsReplaceFileError) as raised:
        layer.replace_with_backup(replacement, destination, backup)

    observation = raised.value.observation
    assert observation.error_code == 5
    assert observation.destination.path == destination
    assert observation.replacement.path == replacement
    assert observation.backup.path == backup
    if outcome == 'no-changes':
        assert observation.destination.identity == destination_identity
        assert observation.replacement.identity == replacement_identity
        assert observation.backup.exists is False
    elif outcome == 'destination-missing-backup':
        assert observation.destination.exists is False
        assert observation.replacement.identity == replacement_identity
        assert observation.backup.identity == destination_identity
    else:
        assert observation.destination.identity == replacement_identity
        assert observation.replacement.exists is False
        assert observation.backup.identity == destination_identity


def test_shared_windows_generated_replace_holds_verified_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingNative:
        def __init__(self) -> None:
            self.replacements: list[tuple[Path, Path]] = []
            self.flushes: list[Path] = []

        @staticmethod
        def open_readonly(path: Path, *, directory: bool = False) -> int:
            flags = os.O_RDONLY
            if directory:
                flags |= getattr(os, 'O_DIRECTORY', 0)
            return os.open(path, flags)

        def replace(self, source: Path, destination: Path) -> None:
            self.replacements.append((source, destination))
            os.replace(source, destination)

        def flush_directory(self, path: Path) -> None:
            self.flushes.append(path)

    root = tmp_path / 'root'
    directory = root / 'generated'
    directory.mkdir(parents=True)
    source = directory / 'source.part'
    destination = directory / 'destination.alog'
    source.write_bytes(b'new')
    destination.write_bytes(b'old')
    native = RecordingNative()
    monkeypatch.setattr(filesystem_module, '_IS_WINDOWS', True)
    monkeypatch.setattr(filesystem_module, '_HAS_DIRECTORY_FDS', False)
    monkeypatch.setattr(filesystem_module, '_WINDOWS_NATIVE', native)
    filesystem_module.replace_generated(root, source, destination)
    assert native.replacements == [(source, destination)]
    assert native.flushes == [directory]
    assert destination.read_bytes() == b'new'


def test_cache_runtime_import_boundary_excludes_qt_plus_network_and_keyring() -> None:
    script = r'''
import sys
from artisanlib.roastserver.cache import CacheStore
assert CacheStore
blocked = ('PyQt6', 'plus', 'requests', 'keyring')
assert not any(name.startswith(blocked) for name in sys.modules)
'''
    completed = subprocess.run(
        [sys.executable, '-c', script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
