#
# ABOUT
# Tests for Roast Server worker inventory lifecycle and fair scheduling
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from uuid import UUID

from PyQt6.QtCore import QCoreApplication, QObject, QTimer
from PyQt6.QtTest import QSignalSpy
import pytest

from artisanlib.roastserver.api import ClientFactory
from artisanlib.roastserver.cache import CacheStore
from artisanlib.roastserver.contract import (
    IdentityOrganization,
    IdentityUser,
    Namespace,
    ServerIdentity,
)
from artisanlib.roastserver.inventory_store import (
    InventoryCommand,
    InventoryStore,
    InventoryStoreError,
)
from artisanlib.roastserver.outbox import Job, Outbox, QueueCounts
from artisanlib.roastserver.settings import CredentialStore, namespace_for
from artisanlib.roastserver.worker import (
    ConfigurationFence,
    ConnectionTestRequest,
    OpaqueVault,
    RoastServerWorker,
    SavedProfileRequest,
    WorkerConfiguration,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ORIGIN = 'https://example.test'
ORGANIZATION_ID = UUID('11111111-1111-4111-8111-111111111111')
USER_ID = UUID('22222222-2222-4222-8222-222222222222')
ROAST_UUID = UUID('33333333-3333-4333-8333-333333333333')
LOT_ID = UUID('44444444-4444-4444-8444-444444444444')
RESERVATION_UUID = UUID('55555555-5555-4555-8555-555555555555')
CLIENT_UUID = UUID('66666666-6666-4666-8666-666666666666')
NAMESPACE = namespace_for(ORIGIN, ORGANIZATION_ID)
IDENTITY = ServerIdentity(
    user=IdentityUser(USER_ID, 'owner@example.test', 'Owner'),
    organization=IdentityOrganization(ORGANIZATION_ID, 'Roastery', 'roastery'),
    role='admin',
)

pytestmark = pytest.mark.usefixtures('qcoreapplication')


class FakeOutbox:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.due: datetime | None = None
        self.lease_calls: list[datetime] = []
        self.pause_calls: list[tuple[Namespace, datetime, str]] = []
        self.resume_calls: list[tuple[Namespace, datetime]] = []
        self.close_error = False

    def open(self) -> None:
        self.log.append('outbox.open')

    def recover_expired_leases(self, _now: datetime) -> int:
        self.log.append('outbox.recover')
        return 0

    def next_due_at(self, _namespace: Namespace) -> datetime | None:
        return self.due

    def lease_next(
        self, namespace: Namespace, now: datetime, _lease_seconds: int
    ) -> Job | None:
        self.lease_calls.append(now)
        return profile_job(namespace, now)

    def pause_namespace(self, namespace: Namespace, now: datetime, code: str) -> int:
        self.pause_calls.append((namespace, now, code))
        return 0

    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
        self.resume_calls.append((namespace, now))
        return 0

    def counts(self, _namespace: Namespace) -> QueueCounts:
        return QueueCounts(0, 0, 0, 0, 0)

    def failed_jobs(self, _namespace: Namespace) -> tuple[()]:
        return ()

    def close(self) -> None:
        self.log.append('outbox.close')
        if self.close_error:
            raise RuntimeError('outbox close failed')


class FakeInventoryStore:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.due: datetime | None = None
        self.lease_calls: list[datetime] = []
        self.pause_calls: list[tuple[Namespace, datetime, str]] = []
        self.resume_calls: list[tuple[Namespace, datetime]] = []
        self.open_error = False
        self.close_error = False

    def open(self) -> None:
        self.log.append('inventory.open')
        if self.open_error:
            raise InventoryStoreError('inventory open failed')

    def recover_expired_leases(self, _now: datetime) -> int:
        self.log.append('inventory.recover')
        return 0

    def next_due_at(self, _namespace: Namespace) -> datetime | None:
        return self.due

    def lease_next(
        self, namespace: Namespace, now: datetime, _lease_seconds: int
    ) -> InventoryCommand | None:
        self.lease_calls.append(now)
        return inventory_command(namespace, now)

    def pause_namespace(self, namespace: Namespace, now: datetime, code: str) -> int:
        self.pause_calls.append((namespace, now, code))
        return 0

    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
        self.resume_calls.append((namespace, now))
        return 0

    def close(self) -> None:
        self.log.append('inventory.close')
        if self.close_error:
            raise InventoryStoreError('inventory close failed')


class FakeCache:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.open_error = False

    def open(self) -> None:
        self.log.append('cache.open')
        if self.open_error:
            raise OSError('cache open failed')

    def stats(self, _namespace: Namespace) -> object:
        return object()

    def close(self) -> None:
        self.log.append('cache.close')


class FakeTimer(QTimer):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.delays: list[int] = []

    @override
    def start(self, msec: int | None = None) -> None:
        self.delays.append(self.interval() if msec is None else msec)


class FakeCredentials:
    def get(self, _origin: str) -> str | None:
        return None

    def set(self, _origin: str, _credential: str) -> None:
        pass

    def delete(self, _origin: str) -> None:
        pass


class FakeClientFactory:
    def __call__(self, _origin: str, _credential: str) -> object:
        raise AssertionError('inventory scheduling must not contact HTTP')


class RecordingWorker(RoastServerWorker):
    def __init__(
        self,
        *,
        outbox: FakeOutbox,
        inventory_store: FakeInventoryStore,
        cache: FakeCache,
        fence: ConfigurationFence,
        timer_factory: Callable[[QObject], QTimer],
        operation_hook: Callable[[str], None] | None = None,
        inventory_delivery: bool = True,
    ) -> None:
        self.queue_classes_delivered: list[str] = []
        super().__init__(
            outbox=cast(Outbox, outbox),
            inventory_store=cast(InventoryStore, inventory_store),
            cache=cast(CacheStore, cache),
            credentials=cast(CredentialStore, FakeCredentials()),
            client_factory=cast(ClientFactory, FakeClientFactory()),
            clock=lambda: NOW,
            credential_vault=OpaqueVault[ConnectionTestRequest](),
            profile_vault=OpaqueVault[SavedProfileRequest](),
            command_vault=OpaqueVault[object](),
            configuration_fence=fence,
            timer_factory=timer_factory,
            operation_hook=operation_hook,
            _inventory_delivery=(
                self._record_inventory_delivery if inventory_delivery else None
            ),
        )

    @override
    def _deliver_job(self, configuration: WorkerConfiguration, job: Job) -> None:
        self.queue_classes_delivered.append('profile')
        self._schedule_next(job.namespace)

    def _record_inventory_delivery(
        self, _configuration: WorkerConfiguration, _command: InventoryCommand
    ) -> None:
        self.queue_classes_delivered.append('inventory')


def profile_job(namespace: Namespace, now: datetime) -> Job:
    return Job(
        id='a' * 32,
        namespace=namespace,
        roast_uuid=ROAST_UUID,
        content_sha256='b' * 64,
        snapshot_sha256='b' * 64,
        snapshot_path=Path('/not-opened'),
        snapshot_byte_count=1,
        aroast_json='{}',
        revision_json='{}',
        idempotency_key='profile-test',
        state='leased',
        attempts=1,
        next_attempt_at=None,
        lease_expires_at=now + timedelta(seconds=60),
        lease_token='c' * 32,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def inventory_command(namespace: Namespace, now: datetime) -> InventoryCommand:
    return InventoryCommand(
        id='d' * 32,
        namespace=namespace,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_ID,
        reservation_uuid=RESERVATION_UUID,
        operation='reserve',
        request_json=b'{}',
        idempotency_key='inventory-test',
        dependency_id=None,
        state='leased',
        attempts=1,
        next_attempt_at=None,
        lease_expires_at=now + timedelta(seconds=60),
        lease_token='e' * 32,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(scope='module')
def qcoreapplication() -> Generator[QCoreApplication]:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


def make_worker(
    *,
    operation_hook: Callable[[str], None] | None = None,
    inventory_delivery: bool = True,
    cache_open_error: bool = False,
    inventory_close_error: bool = False,
) -> tuple[
    RecordingWorker,
    FakeOutbox,
    FakeInventoryStore,
    FakeCache,
    FakeTimer,
    ConfigurationFence,
    list[str],
]:
    log: list[str] = []
    outbox = FakeOutbox(log)
    inventory = FakeInventoryStore(log)
    inventory.close_error = inventory_close_error
    cache = FakeCache(log)
    cache.open_error = cache_open_error
    fence = ConfigurationFence()
    timers: list[FakeTimer] = []

    def timer_factory(parent: QObject) -> QTimer:
        timer = FakeTimer(parent)
        timers.append(timer)
        return timer

    worker = RecordingWorker(
        outbox=outbox,
        inventory_store=inventory,
        cache=cache,
        fence=fence,
        timer_factory=timer_factory,
        operation_hook=operation_hook,
        inventory_delivery=inventory_delivery,
    )
    worker.start()
    assert timers
    generation = fence.advance()
    configuration = WorkerConfiguration(
        origin=ORIGIN,
        namespace=NAMESPACE,
        enabled=True,
        automatic_upload=True,
        client_instance_uuid=CLIENT_UUID,
        cache_limit_bytes=64 * 1024 * 1024,
        generation=generation,
        identity=IDENTITY,
    )
    worker._configuration = configuration
    worker._credential = 'test-only'
    worker._authorized_target = (ORIGIN, IDENTITY)
    return worker, outbox, inventory, cache, timers[0], fence, log


def test_startup_and_shutdown_order_closes_both_stores_before_stopped() -> None:
    worker, _outbox, _inventory, _cache, _timer, _fence, log = make_worker()
    stopped = QSignalSpy(worker.stopped)
    worker.stopped.connect(lambda: log.append('stopped'))

    worker.stop()

    assert len(stopped) == 1
    assert log == [
        'outbox.open',
        'outbox.recover',
        'inventory.open',
        'inventory.recover',
        'cache.open',
        'cache.close',
        'inventory.close',
        'outbox.close',
        'stopped',
    ]


def test_partial_open_is_cleaned_up_and_inventory_close_failure_still_stops() -> None:
    worker, _outbox, _inventory, _cache, _timer, _fence, log = make_worker(
        cache_open_error=True,
        inventory_close_error=True,
    )
    failed = QSignalSpy(worker.operationFailed)
    stopped = QSignalSpy(worker.stopped)

    worker.stop()

    assert len(stopped) == 1
    assert log[-3:] == ['cache.close', 'inventory.close', 'outbox.close']
    assert any(failed[index][0] == 'stop' for index in range(len(failed)))


def test_earliest_due_queue_wins_and_only_one_store_is_leased_per_callback() -> None:
    worker, outbox, inventory, _cache, _timer, _fence, _log = make_worker()
    outbox.due = NOW + timedelta(seconds=1)
    inventory.due = NOW

    worker.process_queue_once()

    assert worker.queue_classes_delivered == ['inventory']
    assert len(inventory.lease_calls) == 1
    assert outbox.lease_calls == []
    worker.stop()


def test_equal_due_times_alternate_queue_class_without_starvation() -> None:
    worker, outbox, inventory, _cache, _timer, _fence, _log = make_worker()
    outbox.due = NOW
    inventory.due = NOW

    for _ in range(20):
        worker.process_queue_once()

    assert worker.queue_classes_delivered[:4] == [
        'profile',
        'inventory',
        'profile',
        'inventory',
    ]
    assert worker.queue_classes_delivered.count('profile') == 10
    assert worker.queue_classes_delivered.count('inventory') == 10
    assert len(outbox.lease_calls) + len(inventory.lease_calls) == 20
    worker.stop()


def test_rearms_timer_from_minimum_next_due() -> None:
    worker, outbox, inventory, _cache, timer, _fence, _log = make_worker()
    outbox.due = NOW + timedelta(seconds=7, microseconds=1)
    inventory.due = NOW + timedelta(seconds=3, microseconds=1)

    worker.wake_inventory()

    assert timer.delays[-1] == 3_001
    assert outbox.lease_calls == []
    assert inventory.lease_calls == []
    worker.stop()


def test_stale_configuration_is_rejected_before_either_store_can_lease() -> None:
    fence_ref: list[ConfigurationFence] = []

    def revoke_at_lease(operation: str) -> None:
        if operation == 'lease_next':
            fence_ref[0].revoke()

    worker, outbox, inventory, _cache, _timer, fence, _log = make_worker(
        operation_hook=revoke_at_lease
    )
    fence_ref.append(fence)
    outbox.due = NOW
    inventory.due = NOW

    worker.process_queue_once()

    assert outbox.lease_calls == []
    assert inventory.lease_calls == []
    assert worker.queue_classes_delivered == []
    worker.stop()


def test_pause_and_resume_apply_to_profile_and_inventory_with_same_time_and_reason() -> None:
    worker, outbox, inventory, _cache, _timer, _fence, _log = make_worker()
    configuration = cast(WorkerConfiguration, worker._configuration)

    worker._pause_namespace(NAMESPACE, 'connector_disabled')
    worker._apply_authorized_configuration(configuration)

    assert outbox.pause_calls[-1] == inventory.pause_calls[-1]
    assert outbox.pause_calls[-1] == (NAMESPACE, NOW, 'connector_disabled')
    assert outbox.resume_calls[-1] == inventory.resume_calls[-1]
    assert outbox.resume_calls[-1] == (NAMESPACE, NOW)
    worker.stop()


def test_unwired_inventory_dispatch_never_leases_or_mutates_commands() -> None:
    worker, outbox, inventory, _cache, _timer, _fence, _log = make_worker(
        inventory_delivery=False
    )
    outbox.due = None
    inventory.due = NOW

    worker.process_queue_once()

    assert inventory.lease_calls == []
    assert worker.queue_classes_delivered == []
    worker.stop()
