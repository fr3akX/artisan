from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import queue
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from typing import Any, cast, override
from uuid import UUID

import pytest

from artisanlib.roastserver import inventory_store as store_module
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    FailureKind,
    Namespace,
    PublicFailure,
)
from artisanlib.roastserver.inventory_contract import (
    BeanLot,
    InventoryBalance,
    InventoryConflict,
    InventoryMutationResult,
    InventoryReservation,
    build_finalize_request,
    build_release_request,
    build_reserve_request,
)
from artisanlib.roastserver.inventory_store import (
    InventoryStore,
    InventoryStoreError,
    LotCacheSnapshot,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, 123456, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
LOT_UUID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_LOT_UUID = UUID('22222222-2222-4222-8222-222222222222')
ORGANIZATION_UUID = UUID('33333333-3333-4333-8333-333333333333')
OTHER_ORGANIZATION_UUID = UUID('44444444-4444-4444-8444-444444444444')
ROAST_UUID = UUID('55555555-5555-4555-8555-555555555555')
RESERVATION_UUID = UUID('66666666-6666-4666-8666-666666666666')
CLIENT_UUID = UUID('77777777-7777-4777-8777-777777777777')
SERVER_RESERVATION_UUID = UUID('88888888-8888-4888-8888-888888888888')
OTHER_SERVER_RESERVATION_UUID = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
CONFLICT_UUID = UUID('99999999-9999-4999-8999-999999999999')
LEDGER_UUID = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
RESERVE_REQUEST = build_reserve_request(
    client_instance_uuid=CLIENT_UUID,
    reservation_uuid=RESERVATION_UUID,
    roast_uuid=ROAST_UUID,
    lot_id=LOT_UUID,
    planned_grams=1_250,
    occurred_at=NOW,
)
FINALIZE_REQUEST = build_finalize_request(
    client_instance_uuid=CLIENT_UUID,
    reservation_uuid=RESERVATION_UUID,
    roast_uuid=ROAST_UUID,
    lot_id=LOT_UUID,
    planned_grams=1_250,
    actual_grams=1_200,
    occurred_at=LATER,
)
RELEASE_REQUEST = build_release_request(
    client_instance_uuid=CLIENT_UUID,
    reservation_uuid=RESERVATION_UUID,
    roast_uuid=ROAST_UUID,
    lot_id=LOT_UUID,
    planned_grams=1_250,
    occurred_at=LATER,
)
OFFLINE_FAILURE = PublicFailure(
    FailureKind.OFFLINE,
    'connection_error',
    FAILURE_MESSAGES[FailureKind.OFFLINE],
    True,
)
REJECTED_FAILURE = PublicFailure(
    FailureKind.INVENTORY_REJECTED,
    'invalid_inventory_transition',
    'Invalid inventory transition',
    False,
)


def namespace_for_test(
    origin: str = 'https://archive.example',
    organization_uuid: UUID = ORGANIZATION_UUID,
) -> Namespace:
    digest = hashlib.sha256(f'{origin}\n{organization_uuid}'.encode()).hexdigest()
    return Namespace(origin, organization_uuid, f'namespace-sha256:{digest}')


NAMESPACE = namespace_for_test()
OTHER_NAMESPACE = namespace_for_test(
    'https://other.example', OTHER_ORGANIZATION_UUID
)


def lot(
    *,
    lot_id: UUID = LOT_UUID,
    name: str = 'Lot',
    on_hand_grams: int = 1_500,
    reserved_grams: int = 200,
    available_grams: int = 1_300,
) -> BeanLot:
    return BeanLot(
        lot_id=lot_id,
        name=name,
        origin='Ethiopia',
        varietals=('Heirloom', '74110'),
        processing_method='washed',
        crop_year=2025,
        on_hand_grams=on_hand_grams,
        reserved_grams=reserved_grams,
        available_grams=available_grams,
        unresolved_conflict_count=0,
    )


class PortableWindowsInventoryNative:
    def __init__(self) -> None:
        self.reparse_path: Path | None = None
        self.inherited_path: Path | None = None
        self.permission_checks: list[tuple[Path, int]] = []

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
        os.chmod(path, mode)

    def verify_private_permissions(self, path: Path, mode: int) -> None:
        self.permission_checks.append((path, mode))
        if path == self.inherited_path:
            raise OSError('injected inherited DACL')
        if stat.S_IMODE(path.stat().st_mode) != mode:
            raise OSError('injected ACL mismatch')

    @staticmethod
    def flush(descriptor: int, *, directory: bool) -> None:
        del directory
        os.fsync(descriptor)

    @staticmethod
    def flush_directory(_path: Path) -> None:
        pass

    @staticmethod
    def publish(source: Path, destination: Path) -> None:
        os.rename(source, destination)

    @staticmethod
    def replace(source: Path, destination: Path) -> None:
        os.replace(source, destination)

    @staticmethod
    def replace_with_backup(
        replacement: Path, destination: Path, backup: Path
    ) -> None:
        os.replace(destination, backup)
        os.replace(replacement, destination)

    @staticmethod
    def move_no_replace(source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError
        os.rename(source, destination)

    @staticmethod
    def unlink(path: Path) -> None:
        path.unlink()

    @staticmethod
    def unlink_if_identity(path: Path, expected_identity: tuple[int, int]) -> bool:
        path_stat = path.stat()
        if (path_stat.st_dev, path_stat.st_ino) != expected_identity:
            return False
        path.unlink()
        return True


def enable_portable_windows_filesystem(
    monkeypatch: pytest.MonkeyPatch, native: PortableWindowsInventoryNative
) -> None:
    original_import = store_module.secure_filesystem.importlib.import_module
    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1,
        LK_NBLCK=2,
        LK_UNLCK=3,
        locking=lambda *_args: None,
    )
    monkeypatch.setattr(store_module.secure_filesystem, '_IS_WINDOWS', True)
    monkeypatch.setattr(store_module.secure_filesystem, '_HAS_DIRECTORY_FDS', False)
    monkeypatch.setattr(store_module.secure_filesystem, '_WINDOWS_NATIVE', native)
    monkeypatch.setattr(
        store_module.secure_filesystem.importlib,
        'import_module',
        lambda name: fake_msvcrt if name == 'msvcrt' else original_import(name),
    )


def opened_store(root: Path) -> InventoryStore:
    result = InventoryStore(root)
    result.open()
    return result


def database_path(root: Path) -> Path:
    return root / 'inventory.sqlite3'


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InventoryStore]:
    value = opened_store(tmp_path / 'inventory')
    try:
        yield value
    finally:
        value.close()


def mutation_result(
    operation: str = 'reserve',
    *,
    actual_grams: int | None = None,
    conflict: bool = False,
) -> InventoryMutationResult:
    state = {'reserve': 'reserved', 'finalize': 'finalized', 'release': 'released'}[
        operation
    ]
    completed_at = None if operation == 'reserve' else LATER
    conflict_id = CONFLICT_UUID if conflict else None
    return InventoryMutationResult(
        reservation=InventoryReservation(
            reservation_id=SERVER_RESERVATION_UUID,
            client_reservation_uuid=RESERVATION_UUID,
            lot_id=LOT_UUID,
            roast_uuid=ROAST_UUID,
            client_instance_uuid=CLIENT_UUID,
            state=cast(Any, state),
            planned_grams=1_250,
            actual_grams=actual_grams,
            reserved_at=NOW,
            completed_at=completed_at,
            created_at=NOW,
            updated_at=completed_at or NOW,
            open_conflict_id=conflict_id,
        ),
        balance=InventoryBalance(
            lot_id=LOT_UUID,
            on_hand_grams=8_750,
            reserved_grams=0 if operation != 'reserve' else 1_250,
            available_grams=8_750 if operation != 'reserve' else 7_500,
            unresolved_conflict_count=1 if conflict else 0,
        ),
        conflict=(
            InventoryConflict(
                conflict_id=CONFLICT_UUID,
                lot_id=LOT_UUID,
                source_ledger_entry_id=LEDGER_UUID,
                roast_uuid=ROAST_UUID,
                reservation_id=SERVER_RESERVATION_UUID,
                trigger_operation='consumption',
                available_grams_snapshot=-125,
                state='open',
                resolution_note=None,
                resolved_by_user_id=None,
                resolved_at=None,
                created_at=LATER,
            )
            if conflict
            else None
        ),
        idempotent_replay=False,
    )


def test_fresh_schema_is_exact_versioned_and_task_3_ready(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    try:
        assert store.database_pragmas() == ('wal', True, 5_000, 2)
    finally:
        store.close()

    connection = sqlite3.connect(database_path(root))
    try:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(1,)]
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        assert objects == [
            ('index', 'bean_lots_name_idx'),
            ('index', 'inventory_commands_dependency_idx'),
            ('index', 'inventory_commands_ready_idx'),
            ('index', 'roast_inventory_interrupted_idx'),
            ('table', 'bean_lots'),
            ('table', 'inventory_commands'),
            ('table', 'lot_cache_generations'),
            ('table', 'namespaces'),
            ('table', 'roast_inventory'),
            ('table', 'schema_version'),
        ]
        command_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'inventory_commands'"
        ).fetchone()[0]
        roast_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'roast_inventory'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "state IN ('pending','leased','retry_wait','paused','failed','complete')" in command_sql
    assert 'dependency_id IS NULL' in command_sql
    assert "terminal_intent IN ('finalize','release')" in roast_sql
    assert 'balance_available_grams = balance_on_hand_grams - balance_reserved_grams' in roast_sql


@pytest.mark.parametrize(
    'persistent_object',
    [
        'CREATE TABLE unexpected_table (value INTEGER)',
        'CREATE INDEX unexpected_index ON namespaces(origin)',
        '''CREATE TRIGGER unexpected_trigger AFTER INSERT ON namespaces
           BEGIN SELECT NEW.id; END''',
    ],
    ids=['table', 'index', 'trigger'],
)
def test_reopen_rejects_every_extra_schema_object(
    tmp_path: Path, persistent_object: str
) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    store.close()
    connection = sqlite3.connect(database_path(root))
    try:
        connection.execute(persistent_object)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(InventoryStoreError, match='schema'):
        opened_store(root)


def test_reopen_rejects_changed_schema_and_unknown_version(tmp_path: Path) -> None:
    root = tmp_path / 'changed'
    store = opened_store(root)
    store.close()
    connection = sqlite3.connect(database_path(root))
    try:
        connection.execute('DROP INDEX inventory_commands_ready_idx')
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(InventoryStoreError, match='schema'):
        opened_store(root)

    version_root = tmp_path / 'version'
    version_root.mkdir(mode=0o700)
    connection = sqlite3.connect(database_path(version_root))
    try:
        connection.execute('CREATE TABLE schema_version (version INTEGER NOT NULL)')
        connection.execute('INSERT INTO schema_version VALUES (2)')
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(InventoryStoreError, match='schema'):
        opened_store(version_root)


def test_task_3_relational_nullability_checks_are_enforced_by_sql(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    store.replace_lots(NAMESPACE, (), NOW)
    store.close()
    connection = sqlite3.connect(database_path(root))
    connection.execute('PRAGMA foreign_keys=ON')
    namespace_id = connection.execute('SELECT id FROM namespaces').fetchone()[0]
    timestamp = NOW.isoformat(timespec='microseconds')
    roast_uuid = UUID('55555555-5555-4555-8555-555555555555')
    reservation_uuid = UUID('66666666-6666-4666-8666-666666666666')
    connection.execute(
        '''INSERT INTO roast_inventory
           (namespace_id, roast_uuid, lot_uuid, lot_name,
            client_reservation_uuid, planned_grams, lifecycle,
            reserve_occurred_at, created_at, updated_at)
           VALUES (?, ?, ?, 'Lot', ?, 1000, 'reserve_queued', ?, ?, ?)''',
        (
            namespace_id,
            roast_uuid.hex,
            LOT_UUID.hex,
            reservation_uuid.hex,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            '''UPDATE roast_inventory SET balance_on_hand_grams = 1000
               WHERE namespace_id = ? AND roast_uuid = ?''',
            (namespace_id, roast_uuid.hex),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            '''INSERT INTO inventory_commands
               (id, namespace_id, roast_uuid, lot_uuid, reservation_uuid,
                operation, request_json, idempotency_key, state,
                lease_expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'reserve', X'7b7d', 'key', 'leased', ?, ?, ?)''',
            (
                UUID('77777777-7777-4777-8777-777777777777').hex,
                namespace_id,
                roast_uuid.hex,
                LOT_UUID.hex,
                reservation_uuid.hex,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            '''INSERT INTO inventory_commands
               (id, namespace_id, roast_uuid, lot_uuid, reservation_uuid,
                operation, request_json, idempotency_key, state,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'finalize', X'7b7d', 'key', 'pending', ?, ?)''',
            (
                UUID('88888888-8888-4888-8888-888888888888').hex,
                namespace_id,
                roast_uuid.hex,
                LOT_UUID.hex,
                reservation_uuid.hex,
                timestamp,
                timestamp,
            ),
        )
    connection.close()


def test_open_hardens_private_root_database_lock_and_sidecars(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    store.replace_lots(NAMESPACE, (lot(),), NOW)
    if os.name != 'nt':
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for path in (database_path(root), root / '.inventory.lock'):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for suffix in ('-wal', '-shm'):
            path = Path(f'{database_path(root)}{suffix}')
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.close()


def test_root_database_and_sidecar_symlinks_are_rejected(tmp_path: Path) -> None:
    target_root = tmp_path / 'target-root'
    target_root.mkdir()
    linked_root = tmp_path / 'linked-root'
    try:
        linked_root.symlink_to(target_root, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(InventoryStoreError):
        opened_store(linked_root)

    root = tmp_path / 'inventory'
    root.mkdir(mode=0o700)
    outside = tmp_path / 'outside.sqlite3'
    outside.write_bytes(b'outside')
    database_path(root).symlink_to(outside)
    with pytest.raises(InventoryStoreError):
        opened_store(root)
    assert outside.read_bytes() == b'outside'

    database_path(root).unlink()
    initial = opened_store(root)
    initial.close()
    sidecar_target = tmp_path / 'outside-sidecar'
    sidecar_target.write_bytes(b'outside')
    wal = Path(f'{database_path(root)}-wal')
    wal.symlink_to(sidecar_target)
    with pytest.raises(InventoryStoreError):
        opened_store(root)
    assert sidecar_target.read_bytes() == b'outside'


def test_two_connections_publish_and_read_without_thread_warnings(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    first = opened_store(root)
    second = opened_store(root)
    try:
        first.replace_lots(NAMESPACE, (lot(name='First'),), NOW)
        assert [item.name for item in second.cached_lots(NAMESPACE)] == ['First']
        second.replace_lots(NAMESPACE, (lot(name='Second'),), LATER)
        assert first.cache_snapshot(NAMESPACE).refreshed_at == LATER
        assert [item.name for item in first.cached_lots(NAMESPACE)] == ['Second']
    finally:
        first.close()
        second.close()


def test_cache_snapshot_uses_one_generation_across_concurrent_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_read = threading.Event()
    release_reader = threading.Event()
    pause_reads = threading.Event()
    original_connect = store_module.sqlite3.connect

    class PausingConnection(sqlite3.Connection):
        @override
        def execute(  # type: ignore[override]
            self, sql: str, parameters: tuple[object, ...] = ()
        ) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if pause_reads.is_set() and 'SELECT generation, refreshed_at' in sql:
                generation_read.set()
                if not release_reader.wait(timeout=5):
                    raise RuntimeError('timed out waiting to resume cache read')
            return cursor

    def pausing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:  # noqa: ANN401
        return cast(
            sqlite3.Connection,
            original_connect(*args, factory=PausingConnection, **kwargs),
        )

    monkeypatch.setattr(store_module.sqlite3, 'connect', pausing_connect)
    root = tmp_path / 'inventory'
    reader = opened_store(root)
    writer = opened_store(root)
    old_lots = (
        lot(name='Old A'),
        lot(lot_id=OTHER_LOT_UUID, name='Old B'),
    )
    new_lots = (lot(name='New'),)
    reader.replace_lots(NAMESPACE, old_lots, NOW)
    snapshots: list[LotCacheSnapshot] = []
    failures: list[BaseException] = []

    def read_snapshot() -> None:
        try:
            snapshots.append(reader.cache_snapshot(NAMESPACE))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            failures.append(exc)

    pause_reads.set()
    thread = threading.Thread(target=read_snapshot)
    thread.start()
    try:
        assert generation_read.wait(timeout=5)
        writer.replace_lots(NAMESPACE, new_lots, LATER)
    finally:
        release_reader.set()
        thread.join(timeout=5)
        reader.close()
        writer.close()
    assert not thread.is_alive()
    assert failures == []
    assert snapshots in [
        [LotCacheSnapshot(NAMESPACE, old_lots, NOW)],
        [LotCacheSnapshot(NAMESPACE, new_lots, LATER)],
    ]


def test_replace_lots_waits_for_another_instances_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'inventory'
    writer = opened_store(root)
    owner = InventoryStore(root)
    owner_has_lock = threading.Event()
    release_owner = threading.Event()
    outcomes: queue.Queue[str] = queue.Queue()
    failures: list[BaseException] = []
    original_process_lock = store_module.secure_filesystem.process_lock

    @contextmanager
    def pausing_process_lock(
        lock_root: Path, lock_name: str, thread_lock: threading.RLock
    ) -> Iterator[None]:
        if threading.current_thread().name == 'cache-writer':
            outcomes.put('attempted-lock')
        with original_process_lock(lock_root, lock_name, thread_lock):
            if threading.current_thread().name == 'open-owner':
                owner_has_lock.set()
                if not release_owner.wait(timeout=5):
                    raise RuntimeError('timed out waiting to release process lock')
            yield

    monkeypatch.setattr(
        store_module.secure_filesystem, 'process_lock', pausing_process_lock
    )

    def open_owner() -> None:
        try:
            owner.open()
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            failures.append(exc)

    def publish_lots() -> None:
        try:
            writer.replace_lots(NAMESPACE, (lot(),), NOW)
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            failures.append(exc)
        finally:
            outcomes.put('write-finished')

    owner_thread = threading.Thread(target=open_owner, name='open-owner')
    owner_thread.start()
    assert owner_has_lock.wait(timeout=5)
    writer_thread = threading.Thread(target=publish_lots, name='cache-writer')
    writer_thread.start()
    first_outcome = outcomes.get(timeout=5)
    try:
        assert first_outcome == 'attempted-lock'
        assert writer_thread.is_alive()
    finally:
        release_owner.set()
        owner_thread.join(timeout=5)
        writer_thread.join(timeout=5)
        owner.close()
        writer.close()
    assert not owner_thread.is_alive()
    assert not writer_thread.is_alive()
    assert failures == []
    assert outcomes.get_nowait() == 'write-finished'


def test_replace_lots_is_complete_atomic_and_namespace_isolated(tmp_path: Path) -> None:
    store = opened_store(tmp_path / 'inventory')
    try:
        store.replace_lots(
            NAMESPACE,
            (lot(name='Old'), lot(lot_id=OTHER_LOT_UUID, name='Removed')),
            NOW,
        )
        store.replace_lots(NAMESPACE, (lot(name='New'),), LATER)
        store.replace_lots(OTHER_NAMESPACE, (lot(name='Other'),), NOW)
        assert [item.name for item in store.cached_lots(NAMESPACE)] == ['New']
        assert [item.name for item in store.cached_lots(OTHER_NAMESPACE)] == ['Other']

        with pytest.raises(InventoryStoreError):
            store.replace_lots(
                NAMESPACE,
                (lot(name='Duplicate'), lot(name='Duplicate again')),
                LATER + timedelta(hours=1),
            )
        assert [item.name for item in store.cached_lots(NAMESPACE)] == ['New']
        assert store.cache_snapshot(NAMESPACE).refreshed_at == LATER
    finally:
        store.close()


def test_all_lots_are_validated_before_begin_immediate(tmp_path: Path) -> None:
    store = opened_store(tmp_path / 'inventory')
    try:
        store.replace_lots(NAMESPACE, (lot(name='Valid'),), NOW)
        connection = store_module.sqlite3.connect(database_path(store.root))
        try:
            connection.execute('BEGIN IMMEDIATE')
            with pytest.raises(ValueError, match='lot'):
                store.replace_lots(
                    NAMESPACE,
                    (replace(lot(), available_grams=1_299),),
                    LATER,
                )
        finally:
            connection.rollback()
            connection.close()
        assert [item.name for item in store.cached_lots(NAMESPACE)] == ['Valid']
    finally:
        store.close()


@pytest.mark.parametrize(
    'invalid',
    [
        replace(lot(), name=''),
        replace(lot(), varietals=('Heirloom', 'Heirloom')),
        replace(lot(), crop_year=999),
        replace(lot(), reserved_grams=-1, available_grams=1_501),
        replace(lot(), unresolved_conflict_count=-1),
    ],
)
def test_replace_rejects_invalid_lot_projections(
    tmp_path: Path, invalid: BeanLot
) -> None:
    store = opened_store(tmp_path / 'inventory')
    try:
        with pytest.raises(ValueError, match='lot'):
            store.replace_lots(NAMESPACE, (invalid,), NOW)
        assert store.cached_lots(NAMESPACE) == ()
    finally:
        store.close()


def test_cache_snapshot_retains_timestamp_and_empty_state(tmp_path: Path) -> None:
    store = opened_store(tmp_path / 'inventory')
    try:
        assert store.cache_snapshot(NAMESPACE) == LotCacheSnapshot(NAMESPACE, (), None)
        store.replace_lots(NAMESPACE, (), NOW)
        assert store.cached_lots(NAMESPACE) == ()
        assert store.cache_snapshot(NAMESPACE) == LotCacheSnapshot(NAMESPACE, (), NOW)
        store.replace_lots(NAMESPACE, (lot(),), LATER)
        assert store.cache_snapshot(NAMESPACE).refreshed_at == LATER
    finally:
        store.close()


def test_cached_lots_are_immutable_canonical_and_deterministically_ordered(
    tmp_path: Path,
) -> None:
    store = opened_store(tmp_path / 'inventory')
    upper_uuid = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    lower_uuid = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
    accented_uuid = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
    lots = (
        lot(lot_id=lower_uuid, name='beta'),
        lot(lot_id=accented_uuid, name='Älpha'),
        replace(lot(lot_id=upper_uuid, name='Beta'), varietals=('SL28', 'SL34')),
    )
    try:
        store.replace_lots(NAMESPACE, lots, NOW)
        cached = store.cached_lots(NAMESPACE)
        assert isinstance(cached, tuple)
        assert [(item.name, item.lot_id) for item in cached] == [
            ('Beta', upper_uuid),
            ('beta', lower_uuid),
            ('Älpha', accented_uuid),
        ]
        connection = sqlite3.connect(database_path(store.root))
        try:
            varietals = connection.execute(
                'SELECT varietals_json FROM bean_lots WHERE lot_uuid = ?',
                (upper_uuid.hex,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert varietals == '["SL28","SL34"]'
    finally:
        store.close()


def test_read_rejects_noncanonical_varietals_and_timestamp(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    try:
        store.replace_lots(NAMESPACE, (lot(),), NOW)
        connection = sqlite3.connect(database_path(root))
        try:
            connection.execute("UPDATE bean_lots SET varietals_json = '[ \"Heirloom\", \"74110\" ]'")
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(InventoryStoreError, match='cache'):
            store.cached_lots(NAMESPACE)

        connection = sqlite3.connect(database_path(root))
        try:
            connection.execute(
                'UPDATE bean_lots SET varietals_json = ?',
                ('["Heirloom","74110"]',),
            )
            connection.execute(
                'UPDATE lot_cache_generations SET refreshed_at = ?',
                ('2026-08-05 12:00:00',),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(InventoryStoreError, match='cache'):
            store.cache_snapshot(NAMESPACE)
    finally:
        store.close()


def test_failed_publication_retains_old_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    try:
        store.replace_lots(NAMESPACE, (lot(name='Old'),), NOW)
        store_connection = store._connection
        assert store_connection is not None
        store_connection.execute(
            '''CREATE TEMP TRIGGER fail_new_lot BEFORE INSERT ON bean_lots
               BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END'''
        )
        with pytest.raises(InventoryStoreError):
            store.replace_lots(NAMESPACE, (lot(name='New'),), LATER)
        assert [item.name for item in store.cached_lots(NAMESPACE)] == ['Old']
        assert store.cache_snapshot(NAMESPACE).refreshed_at == NOW
    finally:
        store.close()


def test_duplicate_reserve_is_idempotent_and_immutable_body_conflicts(
    store: InventoryStore,
) -> None:
    first = store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    assert store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', LATER) == first
    changed = build_reserve_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1_251,
        occurred_at=NOW,
    )
    with pytest.raises(InventoryStoreError, match='immutable'):
        store.enqueue_reserve(NAMESPACE, changed, 'Lot', LATER)
    assert store.counts(NAMESPACE).pending == 1


def test_terminal_command_waits_for_reserve_and_only_one_intent_wins(
    store: InventoryStore,
) -> None:
    reserve = store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    finalized = store.enqueue_finalize(NAMESPACE, FINALIZE_REQUEST, 1_200, NOW)
    assert finalized.terminal_intent == 'finalize'
    with pytest.raises(InventoryStoreError, match='terminal intent'):
        store.enqueue_release(NAMESPACE, RELEASE_REQUEST, NOW)
    leased = store.lease_next(NAMESPACE, NOW, 30)
    assert leased is not None and leased.operation == 'reserve'
    assert store.lease_next(NAMESPACE, NOW, 30) is None
    assert leased.lease_token is not None
    after_reserve = store.mark_complete(
        leased.id,
        leased.lease_token,
        mutation_result(),
        NOW + timedelta(seconds=1),
    )
    assert after_reserve.lifecycle == 'finalize_queued'
    assert after_reserve.actual_grams == 1_200
    terminal = store.lease_next(NAMESPACE, NOW + timedelta(seconds=1), 30)
    assert terminal is not None and terminal.operation == 'finalize'
    assert reserve.roast_uuid == finalized.roast_uuid


def test_lease_token_cas_expiry_retry_and_next_due(store: InventoryStore) -> None:
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    assert store.next_due_at(NAMESPACE) == NOW
    command = store.lease_next(NAMESPACE, NOW, 30)
    assert command is not None and command.lease_token is not None
    assert len(command.lease_token) == 32 and command.attempts == 1
    with pytest.raises(InventoryStoreError, match='^lease_lost$'):
        store.mark_retry(command.id, '0' * 32, NOW, LATER, OFFLINE_FAILURE)
    with pytest.raises(InventoryStoreError, match='^lease_lost$'):
        store.mark_retry(
            command.id,
            command.lease_token,
            NOW + timedelta(seconds=31),
            LATER,
            OFFLINE_FAILURE,
        )
    assert store.recover_expired_leases(NOW + timedelta(seconds=30)) == 1
    recovered = store.lease_next(NAMESPACE, NOW + timedelta(seconds=30), 30)
    assert recovered is not None and recovered.attempts == 2
    assert recovered.lease_token not in {None, command.lease_token}
    assert recovered.lease_token is not None
    store.mark_retry(
        recovered.id,
        recovered.lease_token,
        NOW + timedelta(seconds=31),
        LATER,
        OFFLINE_FAILURE,
    )
    assert store.next_due_at(NAMESPACE) == LATER
    assert store.counts(NAMESPACE).retrying == 1


def test_completion_atomically_updates_roast_balance_cache_and_conflict(
    store: InventoryStore,
) -> None:
    store.replace_lots(NAMESPACE, (lot(),), NOW)
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    command = store.lease_next(NAMESPACE, NOW, 30)
    assert command is not None and command.lease_token is not None
    result = mutation_result(conflict=True)
    state = store.mark_complete(
        command.id, command.lease_token, result, NOW + timedelta(seconds=10)
    )
    assert state.lifecycle == 'reserved'
    assert state.server_reservation_uuid == SERVER_RESERVATION_UUID
    assert state.balance == result.balance and state.conflict_id == CONFLICT_UUID
    assert store.cached_lots(NAMESPACE)[0].reserved_grams == 1_250
    assert store.cached_lots(NAMESPACE)[0].unresolved_conflict_count == 1
    assert store.counts(NAMESPACE).complete == 1


def test_finalize_without_requested_actual_uses_verified_planned_grams(
    store: InventoryStore,
) -> None:
    finalize = build_finalize_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1_250,
        actual_grams=None,
        occurred_at=LATER,
    )
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    store.enqueue_finalize(NAMESPACE, finalize, None, NOW)
    reserve = store.lease_next(NAMESPACE, NOW, 30)
    assert reserve is not None and reserve.lease_token is not None
    store.mark_complete(
        reserve.id, reserve.lease_token, mutation_result(), NOW + timedelta(seconds=1)
    )
    terminal = store.lease_next(NAMESPACE, NOW + timedelta(seconds=1), 30)
    assert terminal is not None and terminal.lease_token is not None
    state = store.mark_complete(
        terminal.id,
        terminal.lease_token,
        mutation_result('finalize', actual_grams=1_250),
        NOW + timedelta(seconds=2),
    )
    assert state.lifecycle == 'finalized' and state.actual_grams == 1_250


def test_terminal_completion_rejects_changed_server_reservation_without_updates(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'inventory'
    store = opened_store(root)
    try:
        store.replace_lots(NAMESPACE, (lot(),), NOW)
        store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
        store.enqueue_finalize(NAMESPACE, FINALIZE_REQUEST, 1_200, NOW)
        reserve = store.lease_next(NAMESPACE, NOW, 30)
        assert reserve is not None and reserve.lease_token is not None
        store.mark_complete(
            reserve.id,
            reserve.lease_token,
            mutation_result(),
            NOW + timedelta(seconds=1),
        )
        terminal = store.lease_next(NAMESPACE, NOW + timedelta(seconds=1), 30)
        assert terminal is not None and terminal.lease_token is not None

        connection = sqlite3.connect(database_path(root))
        try:
            before = (
                connection.execute(
                    'SELECT * FROM inventory_commands WHERE id = ?', (terminal.id,)
                ).fetchone(),
                connection.execute(
                    'SELECT * FROM roast_inventory WHERE roast_uuid = ?',
                    (ROAST_UUID.hex,),
                ).fetchone(),
                connection.execute(
                    'SELECT * FROM bean_lots WHERE lot_uuid = ?', (LOT_UUID.hex,)
                ).fetchone(),
            )
        finally:
            connection.close()
        before_roast = store.roast_state(NAMESPACE, ROAST_UUID)
        before_cache = store.cache_snapshot(NAMESPACE)
        mismatched = replace(
            mutation_result('finalize', actual_grams=1_200),
            reservation=replace(
                mutation_result('finalize', actual_grams=1_200).reservation,
                reservation_id=OTHER_SERVER_RESERVATION_UUID,
            ),
        )

        with pytest.raises(InventoryStoreError, match='reservation'):
            store.mark_complete(
                terminal.id,
                terminal.lease_token,
                mismatched,
                NOW + timedelta(seconds=2),
            )

        connection = sqlite3.connect(database_path(root))
        try:
            after = (
                connection.execute(
                    'SELECT * FROM inventory_commands WHERE id = ?', (terminal.id,)
                ).fetchone(),
                connection.execute(
                    'SELECT * FROM roast_inventory WHERE roast_uuid = ?',
                    (ROAST_UUID.hex,),
                ).fetchone(),
                connection.execute(
                    'SELECT * FROM bean_lots WHERE lot_uuid = ?', (LOT_UUID.hex,)
                ).fetchone(),
            )
        finally:
            connection.close()
        assert before[0] is not None and before[0][9] == 'leased'
        assert after == before
        assert store.roast_state(NAMESPACE, ROAST_UUID) == before_roast
        assert store.cache_snapshot(NAMESPACE) == before_cache
    finally:
        store.close()


def test_invalid_completion_result_is_rejected_without_partial_update(
    store: InventoryStore,
) -> None:
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    command = store.lease_next(NAMESPACE, NOW, 30)
    assert command is not None and command.lease_token is not None
    invalid = replace(
        mutation_result(),
        balance=replace(mutation_result().balance, lot_id=OTHER_LOT_UUID),
    )
    with pytest.raises(ValueError, match='result'):
        store.mark_complete(
            command.id,
            command.lease_token,
            invalid,
            NOW + timedelta(seconds=10),
        )
    assert store.roast_state(NAMESPACE, ROAST_UUID).server_state is None  # type: ignore[union-attr]
    assert store.counts(NAMESPACE).pending == 1


def test_reserve_failure_cascades_dependency_and_manual_retries_are_exact(
    store: InventoryStore,
) -> None:
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    store.enqueue_finalize(NAMESPACE, FINALIZE_REQUEST, 1_200, NOW)
    reserve = store.lease_next(NAMESPACE, NOW, 30)
    assert reserve is not None and reserve.lease_token is not None
    store.mark_failed(
        reserve.id,
        reserve.lease_token,
        NOW + timedelta(seconds=10),
        REJECTED_FAILURE,
    )
    failed = store.failed_commands(NAMESPACE)
    assert [(item.operation, item.error_code, item.error_message) for item in failed] == [
        ('reserve', 'invalid_inventory_transition', 'Inventory operation rejected.'),
        ('finalize', 'dependency_failed', 'Inventory reservation dependency failed.'),
    ]
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None and state.lifecycle == 'failed'
    store.retry_same(reserve.id, LATER)
    retry = store.lease_next(NAMESPACE, LATER, 30)
    assert retry is not None
    assert retry.request_json == RESERVE_REQUEST.request_json
    assert retry.idempotency_key == RESERVE_REQUEST.idempotency_key
    assert retry.attempts == 2


def test_all_failed_commands_is_global_and_deterministic(
    store: InventoryStore,
) -> None:
    for namespace, failed_at in (
        (NAMESPACE, NOW + timedelta(seconds=20)),
        (OTHER_NAMESPACE, NOW + timedelta(seconds=10)),
    ):
        store.enqueue_reserve(namespace, RESERVE_REQUEST, 'Lot', NOW)
        command = store.lease_next(namespace, NOW, 30)
        assert command is not None and command.lease_token is not None
        store.mark_failed(
            command.id, command.lease_token, failed_at, REJECTED_FAILURE
        )

    failed = store.all_failed_commands()

    assert tuple(item.namespace for item in failed) == (NAMESPACE, OTHER_NAMESPACE)
    expected_ids = tuple(item.id for item in failed)
    assert expected_ids == tuple(
        item.id
        for namespace in (NAMESPACE, OTHER_NAMESPACE)
        for item in store.failed_commands(namespace)
    )
    root = store.root
    store.close()
    reopened = opened_store(root)
    try:
        assert tuple(item.id for item in reopened.all_failed_commands()) == expected_ids
    finally:
        reopened.close()
        store.open()


def test_pause_resume_preserves_namespace_and_derives_lifecycle(
    store: InventoryStore,
) -> None:
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    assert store.pause_namespace(NAMESPACE, NOW, 'credential_removed') == 1
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None and state.lifecycle == 'paused'
    assert store.counts(NAMESPACE).paused == 1
    assert store.resume_namespace(NAMESPACE, LATER) == 1
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None and state.lifecycle == 'reserve_queued'
    command = store.lease_next(NAMESPACE, LATER, 30)
    assert command is not None and command.lease_token is not None
    store.mark_paused(command.id, command.lease_token, LATER, OFFLINE_FAILURE)
    assert store.roast_state(NAMESPACE, ROAST_UUID).lifecycle == 'paused'  # type: ignore[union-attr]


def test_portable_windows_store_fails_closed_on_inherited_private_dacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'inventory'
    root.mkdir()
    native = PortableWindowsInventoryNative()
    native.inherited_path = root
    enable_portable_windows_filesystem(monkeypatch, native)

    with pytest.raises(InventoryStoreError, match='inventory storage operation failed'):
        opened_store(root)
    assert native.permission_checks == [(root, 0o700)]


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm'], ids=['database', 'wal', 'shm'])
def test_portable_windows_store_rejects_database_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    root = tmp_path / 'inventory'
    bootstrap = opened_store(root)
    bootstrap.close()
    reparse_path = Path(f'{database_path(root)}{suffix}')
    if suffix:
        reparse_path.write_bytes(b'injected')
    native = PortableWindowsInventoryNative()
    native.reparse_path = reparse_path
    enable_portable_windows_filesystem(monkeypatch, native)

    with pytest.raises(InventoryStoreError, match='inventory storage operation failed'):
        opened_store(root)


@pytest.mark.win32
@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows ACL behavior')
def test_windows_runtime_store_applies_private_protected_dacls(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    value = opened_store(root)
    try:
        secure = store_module.secure_filesystem
        secure.verify_private_permissions(root, 0o700)
        secure.verify_private_permissions(database_path(root), 0o600)
    finally:
        value.close()


@pytest.mark.win32
@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows reparse behavior')
@pytest.mark.parametrize('suffix', ['', '-wal', '-shm'], ids=['database', 'wal', 'shm'])
def test_windows_runtime_store_rejects_database_reparse_points(
    tmp_path: Path, suffix: str
) -> None:
    root = tmp_path / 'inventory'
    bootstrap = opened_store(root)
    bootstrap.close()
    path = Path(f'{database_path(root)}{suffix}')
    target = tmp_path / f'target{suffix or ".db"}'
    target.write_bytes(b'not inventory data')
    if path.exists():
        path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip('Windows reparse-point creation unavailable')
    with pytest.raises(InventoryStoreError, match='inventory storage operation failed'):
        opened_store(root)


def test_restart_recovery_and_interrupted_reservation_discovery(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    first = opened_store(root)
    first.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    leased = first.lease_next(NAMESPACE, NOW, 30)
    assert leased is not None
    first.close()
    reopened = opened_store(root)
    try:
        assert reopened.interrupted_reservations() == (
            store_module.InterruptedReservation(
                NAMESPACE,
                ROAST_UUID,
                LOT_UUID,
                'Lot',
                RESERVATION_UUID,
                1_250,
                'reserve_queued',
                NOW,
            ),
        )
        assert reopened.recover_expired_leases(NOW + timedelta(seconds=30)) == 1
        assert reopened.lease_next(NAMESPACE, NOW + timedelta(seconds=30), 30) is not None
        reopened.enqueue_release(NAMESPACE, RELEASE_REQUEST, LATER)
        assert reopened.interrupted_reservations() == ()
    finally:
        reopened.close()


@pytest.mark.win32
@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows restart behavior')
def test_windows_runtime_restart_recovers_expired_inventory_lease(tmp_path: Path) -> None:
    root = tmp_path / 'inventory'
    first = opened_store(root)
    first.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    assert first.lease_next(NAMESPACE, NOW, 30) is not None
    first.close()

    restarted = opened_store(root)
    try:
        assert restarted.recover_expired_leases(NOW + timedelta(seconds=30)) == 1
        assert restarted.counts(NAMESPACE).pending == 1
    finally:
        restarted.close()


def test_completed_history_prunes_only_old_terminal_roasts(
    store: InventoryStore,
) -> None:
    store.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW)
    store.enqueue_release(NAMESPACE, RELEASE_REQUEST, NOW)
    reserve = store.lease_next(NAMESPACE, NOW, 30)
    assert reserve is not None and reserve.lease_token is not None
    store.mark_complete(reserve.id, reserve.lease_token, mutation_result(), NOW)
    release = store.lease_next(NAMESPACE, NOW, 30)
    assert release is not None and release.lease_token is not None
    store.mark_complete(
        release.id,
        release.lease_token,
        mutation_result('release'),
        NOW,
    )
    assert store.counts(NAMESPACE).complete == 2
    other_roast = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
    other_reservation = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
    later_request = build_reserve_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=other_reservation,
        roast_uuid=other_roast,
        lot_id=LOT_UUID,
        planned_grams=500,
        occurred_at=NOW + timedelta(days=30),
    )
    store.enqueue_reserve(NAMESPACE, later_request, 'Lot', NOW + timedelta(days=30))
    assert store.counts(NAMESPACE).complete == 0


_CROSS_PROCESS_SCRIPT = r'''
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import UUID

from artisanlib.roastserver.contract import Namespace
from artisanlib.roastserver.inventory_contract import (
    BeanLot, build_finalize_request, build_release_request, build_reserve_request,
)
from artisanlib.roastserver.inventory_store import InventoryStore, InventoryStoreError

root = Path(sys.argv[1])
action = sys.argv[2]
label = sys.argv[3]
gate = Path(sys.argv[4])
now = datetime(2026, 8, 5, 12, 0, 0, 123456, tzinfo=UTC)
later = now + timedelta(hours=1)
lot_id = UUID('11111111-1111-4111-8111-111111111111')
organization_id = UUID('33333333-3333-4333-8333-333333333333')
roast_id = UUID('55555555-5555-4555-8555-555555555555')
reservation_id = UUID('66666666-6666-4666-8666-666666666666')
client_id = UUID('77777777-7777-4777-8777-777777777777')
origin = 'https://archive.example'
digest = hashlib.sha256(f'{origin}\n{organization_id}'.encode()).hexdigest()
namespace = Namespace(origin, organization_id, f'namespace-sha256:{digest}')
reserve = build_reserve_request(
    client_instance_uuid=client_id, reservation_uuid=reservation_id,
    roast_uuid=roast_id, lot_id=lot_id, planned_grams=1_250, occurred_at=now,
)
finalize = build_finalize_request(
    client_instance_uuid=client_id, reservation_uuid=reservation_id,
    roast_uuid=roast_id, lot_id=lot_id, planned_grams=1_250,
    actual_grams=1_200, occurred_at=later,
)
release = build_release_request(
    client_instance_uuid=client_id, reservation_uuid=reservation_id,
    roast_uuid=roast_id, lot_id=lot_id, planned_grams=1_250, occurred_at=later,
)
store = InventoryStore(root)
store.open()
Path(f'{gate}.{label}.ready').touch()
while not gate.exists():
    time.sleep(0.001)
try:
    if action == 'replace':
        amount = 2_000 if label == 'first' else 3_000
        store.replace_lots(
            namespace,
            (BeanLot(lot_id, label, None, (), None, None, amount, 0, amount, 0),),
            now,
        )
        result = label
    elif action == 'reserve':
        result = store.enqueue_reserve(namespace, reserve, 'Lot', now).reservation_uuid.hex
    elif action == 'finalize':
        store.enqueue_finalize(namespace, finalize, 1_200, later)
        result = 'finalize'
    elif action == 'release':
        store.enqueue_release(namespace, release, later)
        result = 'release'
    elif action == 'lease':
        command = store.lease_next(namespace, now, 30)
        result = None if command is None else command.id
    else:
        raise AssertionError(action)
except InventoryStoreError:
    result = 'rejected'
finally:
    store.close()
print(json.dumps(result))
'''


def run_cross_process_pair(
    root: Path, first_action: str, second_action: str
) -> tuple[object, object]:
    gate = root.parent / f'{first_action}-{second_action}.gate'
    environment = os.environ.copy()
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                '-c',
                textwrap.dedent(_CROSS_PROCESS_SCRIPT),
                str(root),
                action,
                label,
                str(gate),
            ],
            cwd=Path.cwd(),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for action, label in ((first_action, 'first'), (second_action, 'second'))
    ]
    deadline = time.monotonic() + 10
    ready = tuple(Path(f'{gate}.{label}.ready') for label in ('first', 'second'))
    while not all(path.exists() for path in ready):
        if time.monotonic() >= deadline:
            for process in processes:
                process.kill()
            outputs = [process.communicate() for process in processes]
            raise AssertionError(f'cross-process workers did not become ready: {outputs!r}')
        time.sleep(0.005)
    gate.touch()
    results: list[object] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        results.append(__import__('json').loads(stdout.strip().splitlines()[-1]))
    return results[0], results[1]


def test_cross_process_cache_replacement_publishes_one_complete_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'inventory'
    bootstrap = opened_store(root)
    bootstrap.close()

    assert set(run_cross_process_pair(root, 'replace', 'replace')) == {'first', 'second'}

    reopened = opened_store(root)
    try:
        snapshot = reopened.cache_snapshot(NAMESPACE)
        assert len(snapshot.lots) == 1
        cached = snapshot.lots[0]
        assert (cached.name, cached.on_hand_grams) in {('first', 2_000), ('second', 3_000)}
        assert cached.available_grams == cached.on_hand_grams
    finally:
        reopened.close()


def test_cross_process_reserve_lease_recovery_and_terminal_intent_are_serialized(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'inventory'
    bootstrap = opened_store(root)
    bootstrap.close()

    reserve_results = run_cross_process_pair(root, 'reserve', 'reserve')
    assert reserve_results == (RESERVATION_UUID.hex, RESERVATION_UUID.hex)

    lease_result, rejected_lease = run_cross_process_pair(root, 'lease', 'lease')
    assert sorted((lease_result is None, rejected_lease is None)) == [False, True]
    reopened = opened_store(root)
    try:
        assert reopened.recover_expired_leases(NOW + timedelta(seconds=30)) == 1
        assert reopened.counts(NAMESPACE).pending == 1
    finally:
        reopened.close()

    terminal_results = run_cross_process_pair(root, 'finalize', 'release')
    assert sorted(cast(tuple[str, str], terminal_results)) in [
        ['finalize', 'rejected'],
        ['rejected', 'release'],
    ]
    reopened = opened_store(root)
    try:
        state = reopened.roast_state(NAMESPACE, ROAST_UUID)
        assert state is not None
        assert state.terminal_intent in {'finalize', 'release'}
        assert reopened.counts(NAMESPACE).pending == 2
    finally:
        reopened.close()


def test_two_store_reserve_and_terminal_intent_races_are_serialized(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'inventory'
    first = opened_store(root)
    second = opened_store(root)
    barrier = threading.Barrier(2)
    reserve_results: list[object] = []
    terminal_results: list[str] = []

    def reserve_with(value: InventoryStore) -> None:
        barrier.wait(timeout=5)
        reserve_results.append(value.enqueue_reserve(NAMESPACE, RESERVE_REQUEST, 'Lot', NOW))

    threads = [threading.Thread(target=reserve_with, args=(item,)) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(reserve_results) == 2 and reserve_results[0] == reserve_results[1]

    barrier = threading.Barrier(2)

    def terminal_with(value: InventoryStore, operation: str) -> None:
        barrier.wait(timeout=5)
        try:
            if operation == 'finalize':
                value.enqueue_finalize(NAMESPACE, FINALIZE_REQUEST, 1_200, LATER)
            else:
                value.enqueue_release(NAMESPACE, RELEASE_REQUEST, LATER)
            terminal_results.append(operation)
        except InventoryStoreError:
            terminal_results.append('rejected')

    threads = [
        threading.Thread(target=terminal_with, args=(first, 'finalize')),
        threading.Thread(target=terminal_with, args=(second, 'release')),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    try:
        assert sorted(terminal_results) in [
            ['finalize', 'rejected'],
            ['rejected', 'release'],
        ]
        assert first.counts(NAMESPACE).pending == 2
    finally:
        first.close()
        second.close()
