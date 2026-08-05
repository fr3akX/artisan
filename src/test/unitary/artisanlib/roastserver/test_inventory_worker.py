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

from collections.abc import Callable, Generator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import inspect
import json
from typing import cast, override
from urllib.parse import urlsplit
from uuid import UUID

from PyQt6.QtCore import QCoreApplication, QObject, QTimer
from PyQt6.QtTest import QSignalSpy
import pytest
import requests
from requests.adapters import HTTPAdapter

from artisanlib.roastserver.api import ApiFailure, ClientFactory, RoastServerClient
from artisanlib.roastserver.cache import CacheStore
from artisanlib.roastserver.contract import (
    IdentityOrganization,
    IdentityUser,
    FAILURE_MESSAGES,
    FailureKind,
    Namespace,
    PublicFailure,
    ServerIdentity,
)
from artisanlib.roastserver.inventory_contract import (
    BeanLot,
    BeanLotPage,
    InventoryBalance,
    InventoryCommandRequest,
    InventoryConflict,
    InventoryMutationResult,
    InventoryOperation,
    InventoryReservation,
    ReservationState,
    build_finalize_request,
    build_release_request,
    build_reserve_request,
)
from artisanlib.roastserver.inventory_store import (
    FailedInventoryCommand,
    InventoryCommand,
    InventoryLifecycle,
    InventoryQueueCounts,
    InventoryRoastState,
    InventoryStore,
    InventoryStoreError,
    InterruptedReservation,
    LotCacheSnapshot,
)
from artisanlib.roastserver.outbox import Job, Outbox, QueueCounts
from artisanlib.roastserver.settings import CredentialStore, namespace_for
from artisanlib.roastserver.worker import (
    ConfigurationFence,
    ConnectionTestRequest,
    InventoryRefreshRequest,
    InventoryWorkerEvent,
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
DURABLE_CLIENT_UUID = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
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

    def counts(self, _namespace: Namespace) -> InventoryQueueCounts:
        return InventoryQueueCounts(0, 0, 0, 0, 0)

    def failed_commands(
        self, _namespace: Namespace
    ) -> tuple[FailedInventoryCommand, ...]:
        return ()

    def interrupted_reservations(self) -> tuple[InterruptedReservation, ...]:
        return ()

    def cache_snapshot(self, namespace: Namespace) -> LotCacheSnapshot:
        return LotCacheSnapshot(namespace, (), None)

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
        self.inventory_delivery = inventory_delivery
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
        )

    @override
    def _deliver_job(self, configuration: WorkerConfiguration, job: Job) -> None:
        self.queue_classes_delivered.append('profile')
        self._schedule_next(job.namespace)

    @override
    def _deliver_inventory_command(
        self, configuration: WorkerConfiguration, command: InventoryCommand
    ) -> None:
        del configuration, command
        if self.inventory_delivery:
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


def canonical_command_request(
    operation: InventoryOperation = 'reserve',
) -> InventoryCommandRequest:
    if operation == 'reserve':
        return build_reserve_request(
            client_instance_uuid=DURABLE_CLIENT_UUID,
            reservation_uuid=RESERVATION_UUID,
            roast_uuid=ROAST_UUID,
            lot_id=LOT_ID,
            planned_grams=1_000,
            occurred_at=NOW,
        )
    if operation == 'finalize':
        return build_finalize_request(
            client_instance_uuid=DURABLE_CLIENT_UUID,
            reservation_uuid=RESERVATION_UUID,
            roast_uuid=ROAST_UUID,
            lot_id=LOT_ID,
            planned_grams=1_000,
            actual_grams=900,
            occurred_at=NOW,
        )
    return build_release_request(
        client_instance_uuid=DURABLE_CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_ID,
        planned_grams=1_000,
        occurred_at=NOW,
    )


def inventory_command(
    namespace: Namespace,
    now: datetime,
    operation: InventoryOperation = 'reserve',
) -> InventoryCommand:
    request = canonical_command_request(operation)
    return InventoryCommand(
        id='d' * 32,
        namespace=namespace,
        roast_uuid=request.roast_uuid,
        lot_id=request.lot_id,
        reservation_uuid=request.reservation_uuid,
        operation=request.operation,
        request_json=request.request_json,
        idempotency_key=request.idempotency_key,
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


def inventory_roast_state(
    operation: InventoryOperation = 'reserve',
) -> InventoryRoastState:
    return InventoryRoastState(
        namespace=NAMESPACE,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_ID,
        lot_name='Test lot',
        reservation_uuid=RESERVATION_UUID,
        server_reservation_uuid=None,
        planned_grams=1_000,
        actual_grams=900 if operation == 'finalize' else None,
        lifecycle=cast(InventoryLifecycle, f'{operation}_queued'),
        terminal_intent=None if operation == 'reserve' else operation,
        reserve_occurred_at=NOW,
        finalize_occurred_at=NOW if operation == 'finalize' else None,
        release_occurred_at=NOW if operation == 'release' else None,
        server_state=None,
        balance=None,
        conflict_id=None,
        error_code=None,
        error_message=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _failure_value(
    kind: FailureKind, code: str, retryable: bool
) -> PublicFailure:
    return PublicFailure(kind, code, FAILURE_MESSAGES[kind], retryable)


def bean_lot(lot_id: UUID = LOT_ID, name: str = 'Test lot') -> BeanLot:
    return BeanLot(lot_id, name, None, (), None, None, 2_000, 0, 2_000, 0)


def mutation_result(
    *,
    conflict: bool = False,
    operation: InventoryOperation = 'reserve',
) -> InventoryMutationResult:
    server_reservation_uuid = UUID('77777777-7777-4777-8777-777777777777')
    conflict_id = UUID('88888888-8888-4888-8888-888888888888')
    inventory_conflict = (
        InventoryConflict(
            conflict_id,
            LOT_ID,
            UUID('99999999-9999-4999-8999-999999999999'),
            ROAST_UUID,
            server_reservation_uuid,
            'reservation',
            -100,
            'open',
            None,
            None,
            None,
            NOW,
        )
        if conflict
        else None
    )
    return InventoryMutationResult(
        InventoryReservation(
            server_reservation_uuid,
            RESERVATION_UUID,
            LOT_ID,
            ROAST_UUID,
            DURABLE_CLIENT_UUID,
            cast(
                ReservationState,
                {
                    'reserve': 'reserved',
                    'finalize': 'finalized',
                    'release': 'released',
                }[operation],
            ),
            1_000,
            900 if operation == 'finalize' else None,
            NOW,
            None if operation == 'reserve' else NOW,
            NOW,
            NOW,
            conflict_id if conflict else None,
        ),
        InventoryBalance(LOT_ID, 2_000, 1_000, 1_000, int(conflict)),
        inventory_conflict,
        False,
    )


class DeliveryInventoryStore(FakeInventoryStore):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.due = NOW
        self.command_value = inventory_command(NAMESPACE, NOW)
        self.roast = inventory_roast_state()
        self.transitions: list[tuple[str, object]] = []
        self.replacements: list[tuple[BeanLot, ...]] = []
        self.snapshot = LotCacheSnapshot(NAMESPACE, (bean_lot(),), NOW)
        self.retry_same_calls: list[str] = []

    @override
    def lease_next(
        self, namespace: Namespace, now: datetime, _lease_seconds: int
    ) -> InventoryCommand | None:
        self.lease_calls.append(now)
        self.due = None
        return self.command_value

    def roast_state(
        self, _namespace: Namespace, _roast_uuid: UUID
    ) -> InventoryRoastState:
        return self.roast

    def mark_complete(
        self,
        _command_id: str,
        _lease_token: str,
        result: InventoryMutationResult,
        _now: datetime,
    ) -> InventoryRoastState:
        self.transitions.append(('complete', result))
        self.command_value = replace(
            self.command_value,
            state='complete',
            lease_token=None,
            lease_expires_at=None,
        )
        self.roast = replace(
            self.roast,
            lifecycle='reserved',
            server_reservation_uuid=result.reservation.reservation_id,
            server_state='reserved',
            balance=result.balance,
            conflict_id=(
                None if result.conflict is None else result.conflict.conflict_id
            ),
        )
        return self.roast

    def mark_retry(
        self,
        _command_id: str,
        _lease_token: str,
        _now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> None:
        self.transitions.append(('retry', (next_attempt_at, failure)))

    def mark_paused(
        self,
        _command_id: str,
        _lease_token: str,
        _now: datetime,
        failure: PublicFailure,
    ) -> None:
        self.transitions.append(('paused', failure))

    def mark_failed(
        self,
        _command_id: str,
        _lease_token: str,
        _now: datetime,
        failure: PublicFailure,
    ) -> None:
        self.transitions.append(('failed', failure))

    def replace_lots(
        self, namespace: Namespace, lots: tuple[BeanLot, ...], refreshed_at: datetime
    ) -> None:
        self.replacements.append(lots)
        self.snapshot = LotCacheSnapshot(namespace, lots, refreshed_at)

    @override
    def cache_snapshot(self, namespace: Namespace) -> LotCacheSnapshot:
        assert namespace == NAMESPACE
        return self.snapshot

    @override
    def counts(self, _namespace: Namespace) -> InventoryQueueCounts:
        return InventoryQueueCounts(0, 0, 0, 0, 1)

    @override
    def failed_commands(
        self, _namespace: Namespace
    ) -> tuple[FailedInventoryCommand, ...]:
        if self.command_value.state != 'failed':
            return ()
        return (
            FailedInventoryCommand(
                self.command_value.id,
                NAMESPACE,
                ROAST_UUID,
                LOT_ID,
                RESERVATION_UUID,
                'reserve',
                1,
                'invalid_request',
                'Invalid request',
                NOW,
            ),
        )

    def retry_same(self, command_id: str, _now: datetime) -> None:
        self.retry_same_calls.append(command_id)
        self.command_value = replace(self.command_value, state='pending')


class InventoryResponseAdapter(HTTPAdapter):
    def __init__(self, operation: InventoryOperation) -> None:
        super().__init__(max_retries=0)
        self.operation = operation
        self.calls: list[requests.PreparedRequest] = []

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: object = None,
        verify: object = True,
        cert: object = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        del stream, timeout, verify, cert, proxies
        self.calls.append(request)
        state = {
            'reserve': 'reserved',
            'finalize': 'finalized',
            'release': 'released',
        }[self.operation]
        completed_at = None if self.operation == 'reserve' else '2026-08-05T12:00:00.000000Z'
        payload = {
            'reservation': {
                'reservation_id': UUID('77777777-7777-4777-8777-777777777777').hex,
                'client_reservation_uuid': RESERVATION_UUID.hex,
                'lot_id': LOT_ID.hex,
                'roast_uuid': ROAST_UUID.hex,
                'client_instance_uuid': DURABLE_CLIENT_UUID.hex,
                'state': state,
                'planned_grams': 1_000,
                'actual_grams': 900 if self.operation == 'finalize' else None,
                'reserved_at': '2026-08-05T12:00:00.000000Z',
                'completed_at': completed_at,
                'created_at': '2026-08-05T12:00:00.000000Z',
                'updated_at': '2026-08-05T12:00:00.000000Z',
                'open_conflict_id': None,
            },
            'balance': {
                'lot_id': LOT_ID.hex,
                'on_hand_grams': 2_000,
                'reserved_grams': 1_000 if self.operation == 'reserve' else 0,
                'available_grams': 1_000 if self.operation == 'reserve' else 2_000,
                'unresolved_conflict_count': 0,
            },
            'conflict': None,
            'idempotent_replay': False,
        }
        body = json.dumps(payload, separators=(',', ':')).encode()
        response = requests.Response()
        response.status_code = 201 if self.operation == 'reserve' else 200
        response.headers.update(
            {
                'Content-Type': 'application/json',
                'Content-Length': str(len(body)),
            }
        )
        response._content = body
        vars(response)['_content_consumed'] = True
        response.request = request
        response.url = request.url or ''
        return response


def real_inventory_client(
    operation: InventoryOperation,
) -> tuple[RoastServerClient, InventoryResponseAdapter]:
    client = RoastServerClient(ORIGIN, 'test-only')
    session = cast(requests.Session, vars(client)['_session'])
    replaced = session.get_adapter('https://')
    adapter = InventoryResponseAdapter(operation)
    session.mount('https://', adapter)
    replaced.close()
    return client, adapter


class FakeInventoryClient:
    def __init__(
        self,
        *,
        result: object | None = None,
        failure: ApiFailure | None = None,
        pages: list[BeanLotPage] | None = None,
        on_execute: Callable[[], None] | None = None,
        on_list: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.pages = [] if pages is None else pages
        self.on_execute = on_execute
        self.on_list = on_list
        self.requests: list[object] = []
        self.cursors: list[str | None] = []

    def __enter__(self) -> FakeInventoryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def execute_inventory_command(self, request: object) -> object:
        self.requests.append(request)
        if self.on_execute is not None:
            self.on_execute()
        if self.failure is not None:
            raise self.failure
        return self.result

    def list_inventory_lots(
        self, cursor: str | None = None, limit: int = 100
    ) -> BeanLotPage:
        assert limit == 100
        self.cursors.append(cursor)
        if self.on_list is not None:
            self.on_list()
        if self.failure is not None:
            raise self.failure
        return self.pages.pop(0)


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


def make_delivery_worker(
    client: FakeInventoryClient | RoastServerClient,
    *,
    operation_hook: Callable[[str], None] | None = None,
) -> tuple[
    RoastServerWorker,
    DeliveryInventoryStore,
    FakeOutbox,
    ConfigurationFence,
    OpaqueVault[object],
]:
    log: list[str] = []
    outbox = FakeOutbox(log)
    inventory = DeliveryInventoryStore(log)
    cache = FakeCache(log)
    fence = ConfigurationFence()
    command_vault = OpaqueVault[object]()
    worker = RoastServerWorker(
        outbox=cast(Outbox, outbox),
        inventory_store=cast(InventoryStore, inventory),
        cache=cast(CacheStore, cache),
        credentials=cast(CredentialStore, FakeCredentials()),
        client_factory=cast(ClientFactory, lambda _origin, _credential: client),
        clock=lambda: NOW,
        credential_vault=OpaqueVault[ConnectionTestRequest](),
        profile_vault=OpaqueVault[SavedProfileRequest](),
        command_vault=command_vault,
        configuration_fence=fence,
        timer_factory=FakeTimer,
        operation_hook=operation_hook,
    )
    worker.start()
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
    return worker, inventory, outbox, fence, command_vault


def test_task7_inventory_worker_surface_replaces_temporary_delivery_callback() -> None:
    parameters = inspect.signature(RoastServerWorker.__init__).parameters

    assert '_inventory_delivery' not in parameters
    assert hasattr(RoastServerWorker, 'inventoryLotsChanged')
    assert hasattr(RoastServerWorker, 'inventoryQueueChanged')
    assert hasattr(RoastServerWorker, 'inventoryFailedChanged')
    assert hasattr(RoastServerWorker, 'inventoryReservationChanged')
    assert hasattr(RoastServerWorker, 'inventoryRecoveryChanged')
    assert hasattr(RoastServerWorker, 'refresh_inventory')
    assert hasattr(RoastServerWorker, 'retry_inventory_command')


def test_delivery_completes_atomically_with_exact_stored_bytes_and_signals() -> None:
    result = mutation_result()
    client = FakeInventoryClient(result=result)
    worker, inventory, _outbox, _fence, _vault = make_delivery_worker(client)
    reservations = QSignalSpy(worker.inventoryReservationChanged)

    worker.process_queue_once()

    assert inventory.transitions == [('complete', result)]
    assert len(client.requests) == 1
    request = cast(InventoryCommandRequest, client.requests[0])
    expected = canonical_command_request()
    assert request.client_instance_uuid == DURABLE_CLIENT_UUID
    assert request.request_json == expected.request_json
    assert request.idempotency_key == expected.idempotency_key
    assert len(reservations) == 1
    worker.stop()


def test_revoked_mutation_response_cannot_commit() -> None:
    client = FakeInventoryClient(result=mutation_result())
    worker, inventory, _outbox, fence, _vault = make_delivery_worker(client)

    def revoke() -> None:
        fence.revoke()

    client.on_execute = revoke
    worker.process_queue_once()

    assert inventory.command_value.state == 'leased'
    assert inventory.transitions == []
    worker.stop()


@pytest.mark.parametrize(
    'outcome',
    ['success', 'retry', 'terminal', 'unsupported', 'credential'],
)
def test_revocation_at_mutation_transition_has_zero_response_side_effects(
    outcome: str,
) -> None:
    failures = {
        'retry': ApiFailure(
            _failure_value(FailureKind.RATE_LIMITED, 'rate_limited', True),
            429,
            30,
        ),
        'terminal': ApiFailure(
            PublicFailure(
                FailureKind.INVENTORY_REJECTED,
                'invalid_request',
                'Invalid request',
                False,
            ),
            422,
            None,
        ),
        'unsupported': ApiFailure(
            _failure_value(
                FailureKind.INVENTORY_UNSUPPORTED,
                'inventory_unsupported',
                False,
            ),
            404,
            None,
        ),
        'credential': ApiFailure(
            _failure_value(
                FailureKind.CREDENTIAL_REJECTED,
                'credential_rejected',
                False,
            ),
            403,
            None,
        ),
    }
    client = FakeInventoryClient(
        result=mutation_result() if outcome == 'success' else None,
        failure=failures.get(outcome),
    )
    fence_ref: list[ConfigurationFence] = []

    def revoke_at_transition(operation: str) -> None:
        if operation == 'inventory_transition':
            fence_ref[0].revoke()

    worker, inventory, outbox, fence, _vault = make_delivery_worker(
        client, operation_hook=revoke_at_transition
    )
    fence_ref.append(fence)
    operation_failed = QSignalSpy(worker.operationFailed)
    reservation_changed = QSignalSpy(worker.inventoryReservationChanged)
    lots_changed = QSignalSpy(worker.inventoryLotsChanged)
    online_changed = QSignalSpy(worker.onlineChanged)

    worker.process_queue_once()

    assert inventory.command_value.state == 'leased'
    assert inventory.transitions == []
    assert inventory.pause_calls == []
    assert outbox.pause_calls == []
    assert worker._credential == 'test-only'
    assert not operation_failed
    assert not reservation_changed
    assert not lots_changed
    assert not online_changed
    worker.stop()


@pytest.mark.parametrize(
    'idempotency_key',
    [
        f'inventory-v2:{DURABLE_CLIENT_UUID.hex}:{RESERVATION_UUID.hex}:reserve',
        f'inventory-v1:{DURABLE_CLIENT_UUID.hex}:{RESERVATION_UUID.hex}',
        f'inventory-v1:{DURABLE_CLIENT_UUID.hex.upper()}:{RESERVATION_UUID.hex}:reserve',
        f'inventory-v1:{DURABLE_CLIENT_UUID.hex}:{UUID(int=1).hex}:reserve',
        f'inventory-v1:{DURABLE_CLIENT_UUID.hex}:{RESERVATION_UUID.hex}:release',
    ],
    ids=['prefix', 'segments', 'client-canonical', 'reservation', 'operation'],
)
def test_malformed_durable_idempotency_key_is_fixed_local_terminal_failure(
    idempotency_key: str,
) -> None:
    client = FakeInventoryClient(result=mutation_result())
    worker, inventory, _outbox, _fence, _vault = make_delivery_worker(client)
    inventory.command_value = replace(
        inventory.command_value, idempotency_key=idempotency_key
    )

    worker.process_queue_once()

    assert client.requests == []
    assert inventory.transitions[0][0] == 'failed'
    persisted = cast(PublicFailure, inventory.transitions[0][1])
    assert persisted.kind is FailureKind.LOCAL_INVENTORY
    assert persisted.code == 'local_inventory'
    worker.stop()


def test_malformed_durable_key_transition_is_generation_fenced() -> None:
    fence_ref: list[ConfigurationFence] = []

    def revoke_at_transition(operation: str) -> None:
        if operation == 'inventory_transition':
            fence_ref[0].revoke()

    client = FakeInventoryClient(result=mutation_result())
    worker, inventory, _outbox, fence, _vault = make_delivery_worker(
        client, operation_hook=revoke_at_transition
    )
    fence_ref.append(fence)
    inventory.command_value = replace(
        inventory.command_value, idempotency_key='inventory-v1:malformed'
    )
    failures = QSignalSpy(worker.operationFailed)

    worker.process_queue_once()

    assert client.requests == []
    assert inventory.transitions == []
    assert not failures
    worker.stop()


@pytest.mark.parametrize('operation', ['reserve', 'finalize', 'release'])
def test_durable_client_uuid_reaches_real_api_transport_after_configuration_drift(
    operation: InventoryOperation,
) -> None:
    client, adapter = real_inventory_client(operation)
    worker, inventory, _outbox, _fence, _vault = make_delivery_worker(client)
    inventory.command_value = inventory_command(NAMESPACE, NOW, operation)
    inventory.roast = inventory_roast_state(operation)
    expected = canonical_command_request(operation)

    worker.process_queue_once()

    assert inventory.transitions[0][0] == 'complete'
    assert CLIENT_UUID != DURABLE_CLIENT_UUID
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    expected_path = (
        '/api/v1/inventory/reservations'
        if operation == 'reserve'
        else (
            f'/api/v1/inventory/reservations/{RESERVATION_UUID.hex}/'
            f'{operation}'
        )
    )
    assert urlsplit(call.url).path == expected_path
    assert call.body == expected.request_json
    assert call.headers['Idempotency-Key'] == expected.idempotency_key
    assert DURABLE_CLIENT_UUID.hex.encode() in expected.request_json or operation != 'reserve'
    worker.stop()


@pytest.mark.parametrize(
    ('failure', 'transition', 'expected_code'),
    [
        (
            ApiFailure(
                PublicFailure(
                    FailureKind.RATE_LIMITED,
                    'rate_limited',
                    FAILURE_MESSAGES[FailureKind.RATE_LIMITED],
                    True,
                ),
                429,
                45,
            ),
            'retry',
            'rate_limited',
        ),
        (
            ApiFailure(
                PublicFailure(
                    FailureKind.INVENTORY_REJECTED,
                    'invalid_request',
                    'Invalid request',
                    False,
                ),
                422,
                None,
            ),
            'failed',
            'invalid_request',
        ),
        (
            ApiFailure(
                PublicFailure(
                    FailureKind.INVENTORY_UNSUPPORTED,
                    'inventory_unsupported',
                    FAILURE_MESSAGES[FailureKind.INVENTORY_UNSUPPORTED],
                    False,
                ),
                404,
                None,
            ),
            'paused',
            'inventory_unsupported',
        ),
    ],
)
def test_delivery_classifies_retry_terminal_and_unsupported_failures(
    failure: ApiFailure, transition: str, expected_code: str
) -> None:
    client = FakeInventoryClient(failure=failure)
    worker, inventory, outbox, _fence, _vault = make_delivery_worker(client)

    worker.process_queue_once()

    assert inventory.transitions[0][0] == transition
    transition_value = inventory.transitions[0][1]
    if transition == 'retry':
        retry_at, persisted = cast(
            tuple[datetime, PublicFailure], transition_value
        )
        assert retry_at == NOW + timedelta(seconds=45)
    else:
        persisted = cast(PublicFailure, transition_value)
    assert persisted.code == expected_code
    if transition == 'paused':
        assert outbox.pause_calls == []
        assert inventory.pause_calls[-1][2] == 'inventory_unsupported'
    worker.stop()


def test_credential_failure_pauses_both_queues_and_clears_authorization() -> None:
    failure = ApiFailure(
        PublicFailure(
            FailureKind.CREDENTIAL_REJECTED,
            'credential_rejected',
            FAILURE_MESSAGES[FailureKind.CREDENTIAL_REJECTED],
            False,
        ),
        403,
        None,
    )
    worker, inventory, outbox, _fence, _vault = make_delivery_worker(
        FakeInventoryClient(failure=failure)
    )

    worker.process_queue_once()

    assert inventory.transitions[0][0] == 'paused'
    assert outbox.pause_calls[-1][2] == 'credential_rejected'
    assert inventory.pause_calls[-1][2] == 'credential_rejected'
    assert worker._credential is None
    worker.stop()


def test_malformed_success_is_terminal_and_successful_conflict_is_prominent() -> None:
    worker, inventory, _outbox, _fence, _vault = make_delivery_worker(
        FakeInventoryClient(result=object())
    )
    failures = QSignalSpy(worker.operationFailed)

    worker.process_queue_once()

    assert inventory.transitions[0][0] == 'failed'
    assert cast(PublicFailure, inventory.transitions[0][1]).code == 'invalid_response'
    worker.stop()

    worker, inventory, _outbox, _fence, _vault = make_delivery_worker(
        FakeInventoryClient(result=mutation_result(conflict=True))
    )
    failures = QSignalSpy(worker.operationFailed)
    worker.process_queue_once()
    assert inventory.transitions[0][0] == 'complete'
    assert failures[-1][1].kind is FailureKind.INVENTORY_CONFLICT
    worker.stop()


def test_refresh_publishes_only_complete_multi_page_snapshot() -> None:
    second_id = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    client = FakeInventoryClient(
        pages=[
            BeanLotPage((bean_lot(),), 'next'),
            BeanLotPage((bean_lot(second_id, 'Second'),), None),
        ]
    )
    worker, inventory, _outbox, _fence, vault = make_delivery_worker(client)
    lots_changed = QSignalSpy(worker.inventoryLotsChanged)
    generation = cast(WorkerConfiguration, worker._configuration).generation
    request_id = vault.put(InventoryRefreshRequest(NAMESPACE, generation))

    worker.refresh_inventory(request_id)

    assert client.cursors == [None, 'next']
    assert inventory.replacements == [
        (bean_lot(), bean_lot(second_id, 'Second'))
    ]
    events = tuple(
        cast(InventoryWorkerEvent, lots_changed[index][0])
        for index in range(len(lots_changed))
    )
    assert any(
        event.generation == generation
        and event.namespace == NAMESPACE
        and event.refresh_id == request_id
        and cast(LotCacheSnapshot, event.value).lots == inventory.replacements[0]
        for event in events
    )
    worker.stop()


@pytest.mark.parametrize(
    'pages',
    [
        [BeanLotPage((bean_lot(),), 'more'), BeanLotPage((bean_lot(),), None)],
        [
            BeanLotPage((), f'cursor-{index}')
            for index in range(1, 101)
        ],
        [
            BeanLotPage(
                tuple(
                    bean_lot(UUID(int=index), f'Lot {index}')
                    for index in range(1, 10_002)
                ),
                None,
            )
        ],
    ],
    ids=['duplicate-lot', 'page-101', 'lot-10001'],
)
def test_refresh_bounds_retain_old_cache(pages: list[BeanLotPage]) -> None:
    client = FakeInventoryClient(pages=pages)
    worker, inventory, _outbox, _fence, vault = make_delivery_worker(client)
    old_snapshot = inventory.snapshot
    generation = cast(WorkerConfiguration, worker._configuration).generation

    worker.refresh_inventory(
        vault.put(InventoryRefreshRequest(NAMESPACE, generation))
    )

    assert inventory.replacements == []
    assert inventory.snapshot == old_snapshot
    worker.stop()


def test_refresh_partial_api_error_retains_old_cache() -> None:
    failure = ApiFailure(
        PublicFailure(
            FailureKind.OFFLINE,
            'connection_error',
            FAILURE_MESSAGES[FailureKind.OFFLINE],
            True,
        ),
        None,
        None,
    )
    class PartialErrorClient(FakeInventoryClient):
        @override
        def list_inventory_lots(
            self, cursor: str | None = None, limit: int = 100
        ) -> BeanLotPage:
            if cursor is not None:
                raise failure
            return super().list_inventory_lots(cursor, limit)

    client = PartialErrorClient(
        pages=[BeanLotPage((bean_lot(),), 'more')]
    )
    worker, inventory, _outbox, _fence, vault = make_delivery_worker(client)
    generation = cast(WorkerConfiguration, worker._configuration).generation

    worker.refresh_inventory(
        vault.put(InventoryRefreshRequest(NAMESPACE, generation))
    )

    assert inventory.replacements == []
    worker.stop()


@pytest.mark.parametrize(
    'failure',
    [
        ApiFailure(
            _failure_value(
                FailureKind.CREDENTIAL_REJECTED,
                'credential_rejected',
                False,
            ),
            403,
            None,
        ),
        ApiFailure(
            _failure_value(
                FailureKind.INVENTORY_UNSUPPORTED,
                'inventory_unsupported',
                False,
            ),
            404,
            None,
        ),
        ApiFailure(
            _failure_value(FailureKind.RATE_LIMITED, 'rate_limited', True),
            429,
            60,
        ),
    ],
    ids=['credential', 'unsupported', 'retryable'],
)
@pytest.mark.parametrize('revoke_point', ['http', 'transition'])
def test_revoked_refresh_error_has_zero_stale_side_effects(
    failure: ApiFailure,
    revoke_point: str,
) -> None:
    fence_ref: list[ConfigurationFence] = []

    def revoke_on_list() -> None:
        if revoke_point == 'http':
            fence_ref[0].revoke()

    def revoke_at_transition(operation: str) -> None:
        if (
            revoke_point == 'transition'
            and operation == 'inventory_refresh_transition'
        ):
            fence_ref[0].revoke()

    client = FakeInventoryClient(failure=failure, on_list=revoke_on_list)
    worker, inventory, outbox, fence, vault = make_delivery_worker(
        client, operation_hook=revoke_at_transition
    )
    fence_ref.append(fence)
    old_snapshot = inventory.snapshot
    configuration = cast(WorkerConfiguration, worker._configuration)
    operation_failed = QSignalSpy(worker.operationFailed)
    online_changed = QSignalSpy(worker.onlineChanged)
    queue_changed = QSignalSpy(worker.inventoryQueueChanged)
    lots_changed = QSignalSpy(worker.inventoryLotsChanged)

    worker.refresh_inventory(
        vault.put(InventoryRefreshRequest(NAMESPACE, configuration.generation))
    )

    assert inventory.snapshot == old_snapshot
    assert inventory.replacements == []
    assert inventory.pause_calls == []
    assert outbox.pause_calls == []
    assert worker._credential == 'test-only'
    assert not operation_failed
    assert not online_changed
    assert not queue_changed
    assert not lots_changed
    worker.stop()


@pytest.mark.parametrize('revoke_point', ['http', 'transition'])
def test_revoked_partial_page_error_has_zero_stale_side_effects(
    revoke_point: str,
) -> None:
    failure = ApiFailure(
        _failure_value(FailureKind.OFFLINE, 'connection_error', True),
        None,
        None,
    )
    fence_ref: list[ConfigurationFence] = []

    class RevokedPartialErrorClient(FakeInventoryClient):
        @override
        def list_inventory_lots(
            self, cursor: str | None = None, limit: int = 100
        ) -> BeanLotPage:
            if cursor is not None:
                if revoke_point == 'http':
                    fence_ref[0].revoke()
                raise failure
            return super().list_inventory_lots(cursor, limit)

    def revoke_at_transition(operation: str) -> None:
        if (
            revoke_point == 'transition'
            and operation == 'inventory_refresh_transition'
        ):
            fence_ref[0].revoke()

    client = RevokedPartialErrorClient(
        pages=[BeanLotPage((bean_lot(),), 'more')]
    )
    worker, inventory, outbox, fence, vault = make_delivery_worker(
        client, operation_hook=revoke_at_transition
    )
    fence_ref.append(fence)
    old_snapshot = inventory.snapshot
    configuration = cast(WorkerConfiguration, worker._configuration)
    operation_failed = QSignalSpy(worker.operationFailed)
    online_changed = QSignalSpy(worker.onlineChanged)
    queue_changed = QSignalSpy(worker.inventoryQueueChanged)
    lots_changed = QSignalSpy(worker.inventoryLotsChanged)

    worker.refresh_inventory(
        vault.put(InventoryRefreshRequest(NAMESPACE, configuration.generation))
    )

    assert inventory.snapshot == old_snapshot
    assert inventory.replacements == []
    assert inventory.pause_calls == []
    assert outbox.pause_calls == []
    assert worker._credential == 'test-only'
    assert not operation_failed
    assert not online_changed
    assert not queue_changed
    assert not lots_changed
    worker.stop()


def test_refresh_cycle_retains_old_cache_and_wake_emits_all_aggregates() -> None:
    client = FakeInventoryClient(
        pages=[
            BeanLotPage((bean_lot(),), 'repeat'),
            BeanLotPage((), 'repeat'),
        ]
    )
    worker, inventory, _outbox, _fence, vault = make_delivery_worker(client)
    queue_changed = QSignalSpy(worker.inventoryQueueChanged)
    failed_changed = QSignalSpy(worker.inventoryFailedChanged)
    recovery_changed = QSignalSpy(worker.inventoryRecoveryChanged)
    old_snapshot = inventory.snapshot
    generation = cast(WorkerConfiguration, worker._configuration).generation

    worker.refresh_inventory(
        vault.put(InventoryRefreshRequest(NAMESPACE, generation))
    )
    worker.wake_inventory()

    assert inventory.replacements == []
    assert inventory.snapshot == old_snapshot
    assert queue_changed
    assert failed_changed
    assert recovery_changed
    worker.stop()


def test_manual_retry_preserves_command_bytes_and_idempotency_key() -> None:
    worker, inventory, _outbox, _fence, _vault = make_delivery_worker(
        FakeInventoryClient(result=mutation_result())
    )
    original = inventory.command_value
    inventory.command_value = replace(original, state='failed')

    worker.retry_inventory_command(original.id)

    assert inventory.retry_same_calls == [original.id]
    assert inventory.command_value.request_json == original.request_json
    assert inventory.command_value.idempotency_key == original.idempotency_key
    worker.stop()


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


def test_inventory_dispatch_uses_contained_internal_delivery_path() -> None:
    worker, outbox, inventory, _cache, _timer, _fence, _log = make_worker()
    outbox.due = None
    inventory.due = NOW

    worker.process_queue_once()

    assert len(inventory.lease_calls) == 1
    assert worker.queue_classes_delivered == ['inventory']
    worker.stop()
