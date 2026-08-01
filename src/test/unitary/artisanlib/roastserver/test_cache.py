from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
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
        'current_metadata': {'source': 'desktop'},
        'current_revision': {
            'revision_number': revision_number,
            'sha256': sha256,
            'byte_size': len(profile_bytes),
            'parser_version': '1.0',
            'parse_state': parse_state,
            'parse_diagnostic_code': None,
            'parse_diagnostic_message': None,
            'uploaded_at': (roast_at + timedelta(minutes=11)).isoformat(),
            'metadata': {'source': 'desktop'},
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


@pytest.fixture
def cache(tmp_path: Path) -> CacheStore:
    return CacheStore(tmp_path / 'cache')


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


def test_publish_rejects_replaced_staging_inode_during_copy(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)
    original = cache_module._read_chunk
    replaced = False

    def replace_after_read(descriptor: int) -> bytes:
        nonlocal replaced
        chunk = original(descriptor)
        if chunk and not replaced:
            replaced = True
            moved = staged.with_name('moved.part')
            staged.replace(moved)
            staged.write_bytes(PROFILE_BYTES)
        return chunk

    monkeypatch.setattr(cache_module, '_read_chunk', replace_after_read)
    with pytest.raises(CacheError):
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)
    assert not list(cache.root.rglob('*.alog'))
    assert_no_temporary_files(cache)


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
    assert cache.list_offline(NAMESPACE, ArchiveFilters(search=' needle ')).items == (latest,)
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
    first = CacheStore(root)
    cached = publish_revision(first)
    second = CacheStore(root)
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
        CacheStore(root)
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
        CacheStore(root)


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
    restarted = CacheStore(cache.root)
    assert not orphan.exists()
    assert not temporary.exists()
    assert restarted.stats(NAMESPACE).revision_count == 0


def test_two_processes_publish_without_partial_pairs(tmp_path: Path) -> None:
    root = tmp_path / 'cache'
    script = r'''
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID
from artisanlib.roastserver.api import DownloadReceipt
from artisanlib.roastserver.cache import CacheStore
from artisanlib.roastserver.contract import Namespace, parse_roast_detail
root, namespace_json, payload_json, profile_hex, go = sys.argv[1:]
namespace_data = json.loads(namespace_json)
namespace = Namespace(namespace_data[0], UUID(namespace_data[1]), namespace_data[2])
payload = json.loads(payload_json)
profile = bytes.fromhex(profile_hex)
detail = parse_roast_detail(payload)
revision = detail.current_revision
assert revision is not None
store = CacheStore(Path(root))
path, output = store.new_staging_file(namespace)
with output:
    output.write(profile)
while not Path(go).exists():
    pass
receipt = DownloadReceipt(detail.roast_uuid, revision.revision_number, revision.sha256,
                          revision.byte_size,
                          f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog')
store.publish(namespace, detail, receipt, path,
              datetime.fromisoformat('2026-08-01T12:34:56.123456+00:00'))
'''
    go = tmp_path / 'go'
    processes: list[subprocess.Popen[bytes]] = []
    namespace_json = json.dumps([NAMESPACE.origin, str(NAMESPACE.organization_id), NAMESPACE.key])
    variants = (
        (ROAST_UUID, b'process one'),
        (OTHER_ROAST_UUID, b'process two'),
    )
    for roast_uuid, profile in variants:
        payload_json = json.dumps(
            detail_payload(roast_uuid=roast_uuid, profile_bytes=profile),
            separators=(',', ':'),
        )
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
                    os.fspath(go),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    go.touch()
    failures: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode != 0:
            failures.append((stdout + stderr).decode('utf-8', errors='replace'))
    assert not failures
    reopened = CacheStore(root)
    assert reopened.stats(NAMESPACE).revision_count == 2
    for cached in reopened.list_offline(NAMESPACE, ArchiveFilters()).items:
        assert reopened.validate(cached) == cached


def test_cache_error_redacts_os_paths_controls_and_server_strings(
    cache: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_bytes(cache, NAMESPACE, PROFILE_BYTES)

    def fail_replace(_root: Path, _source: Path, _destination: Path) -> None:
        raise OSError('/private/customer/server-name\ncontrol')

    monkeypatch.setattr(cache_module, '_replace_generated', fail_replace)
    with pytest.raises(CacheError) as raised:
        cache.publish(NAMESPACE, DETAIL, RECEIPT, staged, NOW)
    assert raised.value.failure == cache_module.CACHE_FAILURE
    assert str(raised.value) == FAILURE_MESSAGES[FailureKind.CACHE_CORRUPT]
    assert raised.value.__cause__ is None
    assert '/private' not in repr(raised.value)
    assert 'server-name' not in repr(raised.value)


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
