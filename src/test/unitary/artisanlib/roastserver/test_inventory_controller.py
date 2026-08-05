#
# ABOUT
# Tests for the Artisan Roast Server inventory controller façade
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

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime
import inspect
from pathlib import Path
import threading
import time
from typing import cast, override
from uuid import UUID

from PyQt6.QtCore import QCoreApplication, QObject, QSettings, pyqtSignal, pyqtSlot
from PyQt6.QtTest import QSignalSpy
import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ClientFactory
from artisanlib.roastserver.cache import CacheStore
from artisanlib.roastserver.contract import (
    IdentityOrganization,
    IdentityUser,
    Namespace,
    ServerIdentity,
)
from artisanlib.roastserver.controller import ControllerError, RoastServerController
from artisanlib.roastserver.inventory import (
    InventoryContext,
    InventoryCoordinator,
    InventoryCoordinatorError,
    InventoryNotice,
    PreparedInventoryCharge,
)
from artisanlib.roastserver.inventory_contract import BeanLot, InventoryProfileLink
from artisanlib.roastserver.inventory_store import (
    FailedInventoryCommand,
    InterruptedReservation,
    InventoryQueueCounts,
    InventoryRoastState,
    InventoryStore,
    InventoryStoreError,
    LotCacheSnapshot,
)
from artisanlib.roastserver.outbox import Outbox
from artisanlib.roastserver.settings import CredentialStore, SettingsStore, namespace_for
from artisanlib.roastserver.worker import InventoryWorkerEvent, WorkerConfiguration

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ORIGIN = 'https://example.test'
OTHER_ORIGIN = 'https://other.example.test'
ORGANIZATION_ID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_ORGANIZATION_ID = UUID('66666666-6666-4666-8666-666666666666')
USER_ID = UUID('22222222-2222-4222-8222-222222222222')
ROAST_UUID = UUID('33333333-3333-4333-8333-333333333333')
LOT_ID = UUID('44444444-4444-4444-8444-444444444444')
RESERVATION_ID = UUID('55555555-5555-4555-8555-555555555555')
IDENTITY = ServerIdentity(
    IdentityUser(USER_ID, 'owner@example.test', 'Owner'),
    IdentityOrganization(ORGANIZATION_ID, 'Roastery', 'roastery'),
    'admin',
)
OTHER_IDENTITY = ServerIdentity(
    IDENTITY.user,
    IdentityOrganization(OTHER_ORGANIZATION_ID, 'Other', 'other'),
    'admin',
)
NAMESPACE = namespace_for(ORIGIN, ORGANIZATION_ID)
LOT = BeanLot(LOT_ID, 'Test lot', None, (), None, None, 1_000, 0, 1_000, 0)
PREPARED = PreparedInventoryCharge(
    True, NAMESPACE, ROAST_UUID, RESERVATION_ID, LOT_ID, LOT.name, 500, False
)
NOTICE = InventoryNotice(
    'inventory_reservation_queued',
    ROAST_UUID,
    RESERVATION_ID,
    LOT_ID,
    None,
    None,
)
STATE = InventoryRoastState(
    NAMESPACE, ROAST_UUID, LOT_ID, LOT.name, RESERVATION_ID, None, 500, None,
    'reserved', None, NOW, None, None, None, None, None, None, None, NOW, NOW,
)
CONFLICT = InventoryRoastState(
    NAMESPACE, ROAST_UUID, LOT_ID, LOT.name, RESERVATION_ID, None, 500, None,
    'reserved', None, NOW, None, None, None, None, RESERVATION_ID, None, None,
    NOW, NOW,
)
FAILED = FailedInventoryCommand(
    'a' * 32, NAMESPACE, ROAST_UUID, LOT_ID, RESERVATION_ID, 'reserve', 1,
    'invalid_inventory_transition', 'Inventory operation rejected.', NOW,
)
RECOVERY = InterruptedReservation(
    NAMESPACE, ROAST_UUID, LOT_ID, LOT.name, RESERVATION_ID, 500, 'reserved', NOW
)


class TrackingStore(InventoryStore):
    def __init__(self, root: Path, events: list[str], label: str) -> None:
        self.root = root
        self._events = events
        self._label = label
        self.opened = False
        self.fail_open = False
        self.lots: tuple[BeanLot, ...] = (LOT,)
        self.failed: tuple[FailedInventoryCommand, ...] = ()
        self.recovery: tuple[InterruptedReservation, ...] = ()
        self.state: InventoryRoastState | None = None

    @override
    def open(self) -> None:
        if self.fail_open:
            raise InventoryStoreError('private detail')
        self.opened = True
        self._events.append(f'{self._label}.open')

    @override
    def close(self) -> None:
        self.opened = False
        self._events.append(f'{self._label}.close')

    @override
    def cached_lots(self, namespace: Namespace) -> tuple[BeanLot, ...]:
        return self.lots if namespace == NAMESPACE else ()

    @override
    def cache_snapshot(self, namespace: Namespace) -> LotCacheSnapshot:
        return LotCacheSnapshot(namespace, self.cached_lots(namespace), NOW)

    @override
    def counts(self, namespace: Namespace) -> InventoryQueueCounts:
        return InventoryQueueCounts(0, 0, 0, 0, 0)

    @override
    def failed_commands(
        self, namespace: Namespace
    ) -> tuple[FailedInventoryCommand, ...]:
        return tuple(item for item in self.failed if item.namespace == namespace)

    @override
    def all_failed_commands(self) -> tuple[FailedInventoryCommand, ...]:
        return self.failed

    @override
    def interrupted_reservations(self) -> tuple[InterruptedReservation, ...]:
        return self.recovery

    @override
    def roast_state(
        self, namespace: Namespace, roast_uuid: UUID
    ) -> InventoryRoastState | None:
        assert namespace == NAMESPACE
        assert roast_uuid == ROAST_UUID
        return self.state


class FakeCoordinator(InventoryCoordinator):
    def __init__(self, _store: object, **kwargs: object) -> None:
        self.wake = cast(Callable[[], None], kwargs['wake'])
        self.calls: list[tuple[object, ...]] = []
        self.error: InventoryCoordinatorError | None = None

    @override
    def prepare_charge(self, *args: object) -> PreparedInventoryCharge:
        self.calls.append(('prepare', *args))
        return PREPARED

    @override
    def commit_charge(self, prepared: PreparedInventoryCharge) -> InventoryNotice:
        if self.error is not None:
            raise self.error
        self.calls.append(('commit', prepared))
        self.wake()
        return NOTICE

    @override
    def finalize_saved_profile(
        self, context: InventoryContext, profile: ProfileData
    ) -> InventoryNotice:
        self.calls.append(('finalize', context, profile))
        return NOTICE

    @override
    def release_for_reset(
        self, context: InventoryContext, roast_uuid: UUID | None
    ) -> InventoryNotice:
        self.calls.append(('release', context, roast_uuid))
        return NOTICE

    @override
    def resolve_interrupted(self, *args: object) -> InventoryNotice:
        self.calls.append(('resolve', *args))
        return NOTICE

    @override
    def is_locked(
        self,
        namespace: Namespace,
        roast_uuid: UUID | None,
        profile_has_charge: bool,
    ) -> bool:
        if self.error is not None:
            raise self.error
        self.calls.append(('locked', namespace, roast_uuid, profile_has_charge))
        return profile_has_charge or roast_uuid is not None


class FakeWorker(QObject):
    connectionTested = pyqtSignal(str, object)
    credentialCommitted = pyqtSignal(str, object)
    connectionActivated = pyqtSignal(str, object)
    connectionRollbackFinished = pyqtSignal(str, bool)
    pendingConnectionRecoveryRequired = pyqtSignal(str, object)
    configurationValidated = pyqtSignal(object)
    credentialRemoved = pyqtSignal(str)
    operationFailed = pyqtSignal(str, object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    archivePageReady = pyqtSignal(str, object)
    browseFinished = pyqtSignal(str)
    downloadStaged = pyqtSignal(str, object)
    cachedReady = pyqtSignal(str, object)
    cachedFallbackReady = pyqtSignal(str, object)
    cachePublished = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    inventoryLotsChanged = pyqtSignal(object)
    inventoryQueueChanged = pyqtSignal(object)
    inventoryFailedChanged = pyqtSignal(object)
    inventoryReservationChanged = pyqtSignal(object)
    inventoryRecoveryChanged = pyqtSignal(object)
    stopped = pyqtSignal()

    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self.store = cast(TrackingStore, kwargs['inventory_store'])
        self.events = self.store._events
        self.configurations: list[WorkerConfiguration] = []
        self.inventory_refreshes: list[str] = []
        self.inventory_retries: list[str] = []
        self.inventory_wakes = 0
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()
        self.stop_release.set()

    @pyqtSlot()
    def start(self) -> None:
        self.store.open()
        self.events.append('worker.start')

    @pyqtSlot(object)
    def configure(self, value: object) -> None:
        assert isinstance(value, WorkerConfiguration)
        self.configurations.append(value)

    @pyqtSlot(str)
    def refresh_inventory(self, request_id: str) -> None:
        self.inventory_refreshes.append(request_id)

    @pyqtSlot(str)
    def retry_inventory_command(self, command_id: str) -> None:
        self.inventory_retries.append(command_id)

    @pyqtSlot()
    def wake_inventory(self) -> None:
        self.inventory_wakes += 1

    @pyqtSlot()
    def stop(self) -> None:
        self.stop_entered.set()
        self.stop_release.wait(timeout=2)
        self.events.append('worker.stop')
        self.store.close()
        self.stopped.emit()

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_args, **_kwargs: None


class Harness:
    def __init__(self, tmp_path: Path, app: QCoreApplication) -> None:
        self.app = app
        qsettings = QSettings(str(tmp_path / 'inventory.ini'), QSettings.Format.IniFormat)
        self.settings = SettingsStore(qsettings)
        self.settings.set_origin(ORIGIN)
        self.settings.save_connection(ORIGIN, IDENTITY)
        self.settings.save_options(True, False, 64 * 1024 * 1024)
        self.events: list[str] = []
        self.stores: list[TrackingStore] = []
        self.coordinator = cast(FakeCoordinator, None)
        self.worker = cast(FakeWorker, None)

        def store_factory(root: Path) -> InventoryStore:
            label = 'ui' if not self.stores else 'worker'
            store = TrackingStore(root, self.events, label)
            self.stores.append(store)
            return store

        def coordinator_factory(
            store: InventoryStore, **kwargs: object
        ) -> InventoryCoordinator:
            self.coordinator = FakeCoordinator(store, **kwargs)
            return self.coordinator

        def worker_factory(**kwargs: object) -> FakeWorker:
            self.worker = FakeWorker(**kwargs)
            return self.worker

        self.controller = RoastServerController(
            settings=self.settings,
            credentials=cast(CredentialStore, object()),
            data_root=tmp_path / 'data',
            client_factory=cast(ClientFactory, lambda *_args: None),
            profile_validator=lambda _path: None,
            worker_factory=worker_factory,
            outbox_factory=cast(
                Callable[[Path, Callable[[], datetime]], Outbox],
                lambda *_args: object(),
            ),
            cache_factory=cast(Callable[[Path], CacheStore], lambda *_args: object()),
            inventory_store_factory=store_factory,
            inventory_coordinator_factory=coordinator_factory,
            clock=lambda: NOW,
        )

    def wait(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 2
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError('bounded Qt event wait timed out')
            time.sleep(0.001)
        self.app.processEvents()

    def start(self) -> None:
        self.controller.start()
        self.wait(lambda: 'worker.start' in self.events)
        self.wait(lambda: bool(self.worker.configurations))

    def stop(self) -> None:
        if not self.controller.shutdown(2_000):
            raise AssertionError('controller did not stop')
        self.app.processEvents()


@pytest.fixture(scope='module')
def qcoreapplication() -> Generator[QCoreApplication]:
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture
def harness(tmp_path: Path, qcoreapplication: QCoreApplication) -> Generator[Harness]:
    value = Harness(tmp_path, qcoreapplication)
    yield value
    if value.controller.worker_thread_running:
        value.stop()


def test_inventory_controller_exposes_binding_facade_and_factories() -> None:
    signature = inspect.signature(RoastServerController)
    assert 'inventory_store_factory' in signature.parameters
    assert 'inventory_coordinator_factory' in signature.parameters
    for member in (
        'inventory_context', 'inventory_cache_snapshot', 'inventory_lots',
        'refresh_inventory_lots',
        'inventory_lot_locked', 'prepare_inventory_charge', 'commit_inventory_charge',
        'finalize_inventory_profile', 'release_inventory_roast',
        'resolve_interrupted_inventory', 'retry_inventory_command',
    ):
        assert callable(getattr(RoastServerController, member))


def test_inventory_controller_exposes_public_signals() -> None:
    for signal in (
        'inventoryLotsChanged', 'inventoryStateChanged', 'inventoryQueueChanged',
        'inventoryFailedChanged', 'inventoryRecoveryRequired', 'inventoryConflict',
    ):
        assert hasattr(RoastServerController, signal)


def test_two_stores_share_root_and_ui_owns_outer_lifetime(harness: Harness) -> None:
    startup_recovery = QSignalSpy(harness.controller.inventoryRecoveryRequired)
    assert len(harness.stores) == 2
    assert harness.stores[0] is not harness.stores[1]
    assert harness.stores[0].root == harness.stores[1].root

    harness.start()
    assert harness.events[:3] == ['ui.open', 'worker.open', 'worker.start']
    assert list(startup_recovery[-1]) == [()]
    harness.stop()
    assert harness.events[-3:] == ['worker.stop', 'worker.close', 'ui.close']


def test_context_offline_queueing_facade_and_queued_wake(harness: Harness) -> None:
    harness.start()
    context = harness.controller.inventory_context()
    assert context == InventoryContext(
        ORIGIN, NAMESPACE, True, True, harness.settings.load().client_instance_uuid
    )
    link = InventoryProfileLink(NAMESPACE, LOT_ID, LOT.name)
    assert harness.controller.inventory_lots() == (LOT,)
    assert harness.controller.prepare_inventory_charge(link, None, 500, 'g') == PREPARED
    assert harness.controller.commit_inventory_charge(PREPARED) == NOTICE
    harness.wait(lambda: harness.worker.inventory_wakes == 1)
    assert harness.controller.finalize_inventory_profile(ProfileData()) == NOTICE
    assert harness.controller.release_inventory_roast(ROAST_UUID) == NOTICE
    assert [call[0] for call in harness.coordinator.calls] == [
        'prepare', 'commit', 'finalize', 'release'
    ]


def test_inventory_cache_snapshot_is_atomic_for_current_namespace_or_none(
    harness: Harness,
) -> None:
    harness.start()
    expected = LotCacheSnapshot(NAMESPACE, (LOT,), NOW)
    assert harness.controller.inventory_cache_snapshot() == expected
    assert harness.controller.inventory_lots() == expected.lots

    harness.controller.apply_options(
        OTHER_ORIGIN, False, False, 64 * 1024 * 1024
    )
    assert harness.controller.inventory_cache_snapshot() is None
    assert harness.controller.inventory_lots() == ()


def test_inventory_lot_lock_facade_short_circuits_without_link_and_delegates(
    harness: Harness,
) -> None:
    harness.start()
    assert not harness.controller.inventory_lot_locked(None, ROAST_UUID, True)
    assert not any(call[0] == 'locked' for call in harness.coordinator.calls)

    link = InventoryProfileLink(NAMESPACE, LOT_ID, LOT.name)
    assert harness.controller.inventory_lot_locked(link, ROAST_UUID, False)
    assert harness.coordinator.calls[-1] == (
        'locked', NAMESPACE, ROAST_UUID, False
    )

    harness.coordinator.error = InventoryCoordinatorError('inventory_storage_failed')
    with pytest.raises(ControllerError, match='inventory_storage_failed'):
        harness.controller.inventory_lot_locked(link, None, False)


def test_refresh_is_opaque_generation_bound_and_cleared_on_origin_change(
    harness: Harness,
) -> None:
    harness.start()
    request_id = harness.controller.refresh_inventory_lots()
    assert len(request_id) == 32
    assert request_id in harness.controller._inventory_refresh_requests
    harness.wait(lambda: request_id in harness.worker.inventory_refreshes)

    harness.controller.apply_options(
        OTHER_ORIGIN, False, False, 64 * 1024 * 1024
    )
    assert request_id not in harness.controller._inventory_refresh_requests
    assert not harness.controller._command_vault.contains(request_id)


def test_inventory_signal_namespace_filtering_and_safe_frozen_payloads(
    harness: Harness,
) -> None:
    harness.start()
    lots = QSignalSpy(harness.controller.inventoryLotsChanged)
    other = namespace_for(OTHER_ORIGIN, ORGANIZATION_ID)
    generation = harness.controller._inventory_generation
    assert generation is not None
    harness.worker.inventoryLotsChanged.emit(
        InventoryWorkerEvent(generation, other, LotCacheSnapshot(other, (), NOW))
    )
    harness.app.processEvents()
    before = len(lots)
    harness.worker.inventoryLotsChanged.emit(
        InventoryWorkerEvent(
            generation, NAMESPACE, LotCacheSnapshot(NAMESPACE, (LOT,), NOW)
        )
    )
    harness.wait(lambda: len(lots) > before)
    assert list(lots[-1]) == [(LOT,)]
    assert b'request_json' not in repr(lots).encode()


def test_same_namespace_old_generation_events_are_rejected_and_refresh_is_exact(
    harness: Harness,
) -> None:
    harness.start()
    old_generation = harness.controller._inventory_generation
    assert old_generation is not None
    old_request = harness.controller.refresh_inventory_lots()
    harness.controller.apply_options(ORIGIN, True, False, 64 * 1024 * 1024)
    generation = harness.controller._inventory_generation
    assert generation is not None and generation != old_generation
    request = harness.controller.refresh_inventory_lots()
    lots = QSignalSpy(harness.controller.inventoryLotsChanged)
    states = QSignalSpy(harness.controller.inventoryStateChanged)

    harness.worker.inventoryLotsChanged.emit(
        InventoryWorkerEvent(
            old_generation,
            NAMESPACE,
            LotCacheSnapshot(NAMESPACE, (), NOW),
            old_request,
        )
    )
    harness.worker.inventoryReservationChanged.emit(
        InventoryWorkerEvent(old_generation, NAMESPACE, CONFLICT)
    )
    harness.app.processEvents()
    assert len(lots) == 0
    assert len(states) == 0
    assert request in harness.controller._inventory_refresh_requests

    harness.worker.inventoryLotsChanged.emit(
        InventoryWorkerEvent(
            generation,
            NAMESPACE,
            LotCacheSnapshot(NAMESPACE, (LOT,), NOW),
            old_request,
        )
    )
    harness.app.processEvents()
    assert request in harness.controller._inventory_refresh_requests
    harness.worker.inventoryLotsChanged.emit(
        InventoryWorkerEvent(
            generation,
            NAMESPACE,
            LotCacheSnapshot(NAMESPACE, (LOT,), NOW),
            request,
        )
    )
    harness.wait(lambda: request not in harness.controller._inventory_refresh_requests)
    assert list(lots[-1]) == [(LOT,)]


def test_global_failed_and_recovery_remain_visible_across_context_changes(
    harness: Harness,
) -> None:
    harness.stores[0].failed = (FAILED,)
    harness.stores[0].recovery = (RECOVERY,)
    failed = QSignalSpy(harness.controller.inventoryFailedChanged)
    recovery = QSignalSpy(harness.controller.inventoryRecoveryRequired)
    lots = QSignalSpy(harness.controller.inventoryLotsChanged)
    queues = QSignalSpy(harness.controller.inventoryQueueChanged)
    harness.start()
    assert list(failed[-1]) == [(FAILED,)]
    assert list(recovery[-1]) == [(RECOVERY,)]

    harness.controller.apply_options(
        OTHER_ORIGIN, False, False, 64 * 1024 * 1024
    )
    assert list(lots[-1]) == [()]
    assert list(queues[-1]) == [InventoryQueueCounts(0, 0, 0, 0, 0)]
    assert list(failed[-1]) == [(FAILED,)]
    assert list(recovery[-1]) == [(RECOVERY,)]
    with pytest.raises(ControllerError, match='inventory_namespace_inactive'):
        harness.controller.retry_inventory_command(FAILED.id)

    harness.controller.apply_options(
        OTHER_ORIGIN, False, False, 64 * 1024 * 1024
    )
    assert list(failed[-1]) == [(FAILED,)]


def test_all_public_inventory_signal_payloads_are_frozen_and_redacted(
    harness: Harness,
) -> None:
    harness.start()
    harness.stores[0].failed = (FAILED,)
    harness.stores[0].recovery = (RECOVERY,)
    generation = harness.controller._inventory_generation
    assert generation is not None
    spies = {
        'lots': QSignalSpy(harness.controller.inventoryLotsChanged),
        'state': QSignalSpy(harness.controller.inventoryStateChanged),
        'queue': QSignalSpy(harness.controller.inventoryQueueChanged),
        'failed': QSignalSpy(harness.controller.inventoryFailedChanged),
        'recovery': QSignalSpy(harness.controller.inventoryRecoveryRequired),
        'conflict': QSignalSpy(harness.controller.inventoryConflict),
    }
    harness.worker.inventoryLotsChanged.emit(
        InventoryWorkerEvent(
            generation, NAMESPACE, LotCacheSnapshot(NAMESPACE, (LOT,), NOW)
        )
    )
    harness.worker.inventoryQueueChanged.emit(
        InventoryWorkerEvent(
            generation, NAMESPACE, InventoryQueueCounts(9, 9, 9, 9, 9)
        )
    )
    harness.worker.inventoryFailedChanged.emit(
        InventoryWorkerEvent(generation - 1, NAMESPACE, ())
    )
    harness.worker.inventoryRecoveryChanged.emit(
        InventoryWorkerEvent(generation - 1, None, ())
    )
    harness.worker.inventoryReservationChanged.emit(
        InventoryWorkerEvent(generation, NAMESPACE, CONFLICT)
    )
    harness.wait(lambda: all(len(spy) for spy in spies.values()))
    assert list(spies['lots'][-1]) == [(LOT,)]
    assert list(spies['state'][-1]) == [CONFLICT]
    assert list(spies['queue'][-1]) == [InventoryQueueCounts(0, 0, 0, 0, 0)]
    assert list(spies['failed'][-1]) == [(FAILED,)]
    assert list(spies['recovery'][-1]) == [(RECOVERY,)]
    assert list(spies['conflict'][-1]) == [CONFLICT]
    assert b'request_json' not in repr(spies).encode()
    assert b'credential' not in repr(spies).encode()


def test_open_failure_and_coordinator_error_are_fixed_public_errors(
    harness: Harness,
) -> None:
    harness.stores[0].fail_open = True
    with pytest.raises(ControllerError) as raised:
        harness.controller.start()
    assert str(raised.value) == 'inventory_storage_failed'
    assert not harness.controller.worker_thread_running


def test_coordinator_errors_translate_without_private_detail(harness: Harness) -> None:
    harness.start()
    harness.coordinator.error = InventoryCoordinatorError('inventory_lot_unavailable')
    with pytest.raises(ControllerError) as raised:
        harness.controller.commit_inventory_charge(PREPARED)
    assert str(raised.value) == 'inventory_lot_unavailable'


def test_resolve_and_retry_are_current_namespace_only(harness: Harness) -> None:
    harness.stores[0].failed = (FAILED,)
    harness.stores[0].recovery = (RECOVERY,)
    harness.start()
    assert harness.controller.resolve_interrupted_inventory(
        ROAST_UUID, 'keep'
    ) == NOTICE
    harness.controller.retry_inventory_command(FAILED.id)
    harness.wait(lambda: FAILED.id in harness.worker.inventory_retries)
    harness.controller.apply_options(
        OTHER_ORIGIN, False, False, 64 * 1024 * 1024
    )
    with pytest.raises(ControllerError, match='inventory_namespace_inactive'):
        harness.controller.resolve_interrupted_inventory(ROAST_UUID, 'keep')
    with pytest.raises(ControllerError, match='inventory_namespace_inactive'):
        harness.controller.retry_inventory_command(FAILED.id)


def test_effective_disabled_pending_and_other_organization_context_snapshots(
    harness: Harness,
) -> None:
    harness.stores[0].failed = (FAILED,)
    harness.start()
    lots = QSignalSpy(harness.controller.inventoryLotsChanged)
    failed = QSignalSpy(harness.controller.inventoryFailedChanged)
    recovery = QSignalSpy(harness.controller.inventoryRecoveryRequired)

    harness.controller.apply_options(ORIGIN, False, False, 64 * 1024 * 1024)
    assert harness.controller.inventory_context().namespace == NAMESPACE
    assert list(lots[-1]) == [(LOT,)]

    harness.controller._settings = harness.settings.save_pending_connection(
        ORIGIN, OTHER_IDENTITY
    )
    harness.controller._queue_configuration(harness.controller._configuration())
    assert harness.controller.inventory_context().namespace is None
    assert list(lots[-1]) == [()]
    assert list(failed[-1]) == [(FAILED,)]
    assert list(recovery[-1]) == [()]

    harness.settings.clear_pending_connection()
    harness.settings.save_connection(ORIGIN, OTHER_IDENTITY)
    harness.controller._settings = harness.settings.save_options(
        True, False, 64 * 1024 * 1024
    )
    other = namespace_for(ORIGIN, OTHER_ORGANIZATION_ID)
    harness.controller._known_namespace = other
    harness.controller._identity = OTHER_IDENTITY
    harness.controller._proof = (ORIGIN, OTHER_ORGANIZATION_ID)
    harness.controller._queue_configuration(harness.controller._configuration())
    assert harness.controller.inventory_context().namespace == other
    assert list(lots[-1]) == [()]
    assert list(failed[-1]) == [(FAILED,)]
    with pytest.raises(ControllerError, match='inventory_command_unavailable'):
        harness.controller.retry_inventory_command(FAILED.id)


def test_worker_timeout_keeps_ui_store_open_until_worker_finishes(
    harness: Harness,
) -> None:
    harness.start()
    harness.worker.stop_release.clear()
    assert not harness.controller.shutdown(10)
    assert harness.worker.stop_entered.wait(timeout=1)
    assert harness.stores[0].opened
    assert 'ui.close' not in harness.events
    harness.worker.stop_release.set()
    harness.wait(lambda: not harness.controller.worker_thread_running)
    assert harness.controller.shutdown(2_000)
    assert harness.events[-3:] == ['worker.stop', 'worker.close', 'ui.close']


def test_worker_start_failure_closes_ui_store(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_start() -> None:
        raise RuntimeError('private worker detail')

    monkeypatch.setattr(harness.controller._thread, 'start', reject_start)
    with pytest.raises(ControllerError) as raised:
        harness.controller.start()
    assert str(raised.value) == 'worker_start_failed'
    assert not harness.stores[0].opened
    assert harness.events == ['ui.open', 'ui.close']
