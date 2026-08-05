from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from uuid import UUID

import pytest

from artisanlib.roastserver import inventory_store as store_module
from artisanlib.roastserver.contract import Namespace
from artisanlib.roastserver.inventory_contract import BeanLot
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


def opened_store(root: Path) -> InventoryStore:
    result = InventoryStore(root)
    result.open()
    return result


def database_path(root: Path) -> Path:
    return root / 'inventory.sqlite3'


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
