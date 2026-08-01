#
# ABOUT
# Tests for the Artisan Roast Server QObject worker
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

import ast
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import threading
import time
from types import TracebackType
from typing import BinaryIO, Self, cast, override
from uuid import UUID, uuid4

from PyQt6.QtCore import QCoreApplication, QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtTest import QSignalSpy
import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ApiFailure, ClientFactory, DownloadReceipt
from artisanlib.roastserver.cache import (
    CacheError,
    CacheStats,
    CachedPage,
    CachedRevision,
    CacheStore,
)
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    ArchiveFilters,
    FailureKind,
    IdentityOrganization,
    IdentityUser,
    Namespace,
    PublicFailure,
    Revision,
    RevisionUpload,
    RevisionUploadLinks,
    RoastDetail,
    RoastDetailLinks,
    RoastPage,
    RoastSummary,
    ServerIdentity,
)
from artisanlib.roastserver.metadata import ProjectedMetadata
from artisanlib.roastserver.outbox import EnqueueResult, FailedJob, Job, Outbox, Snapshot
from artisanlib.roastserver.settings import CredentialStoreError, namespace_for
from artisanlib.roastserver.worker import (
    BrowseRequest,
    CachedOpenRequest,
    ClearUnusedRequest,
    ConnectionTestRequest,
    OnlineOpenRequest,
    OpaqueVault,
    PublishRequest,
    RoastServerWorker,
    SavedProfileRequest,
    WorkerConfiguration,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
ORIGIN = 'https://example.test'
ORGANIZATION_ID = UUID('11111111-1111-4111-8111-111111111111')
ROAST_UUID = UUID('22222222-2222-4222-8222-222222222222')
CLIENT_UUID = UUID('33333333-3333-4333-8333-333333333333')
USER_ID = UUID('44444444-4444-4444-8444-444444444444')
NAMESPACE = namespace_for(ORIGIN, ORGANIZATION_ID)
SECRET = 'ephemeral-worker-secret-do-not-expose'
PROFILE_BYTES = repr({'roastUUID': str(ROAST_UUID), 'title': 'Worker roast'}).encode('utf-8')
PROFILE_SHA256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
IDENTITY = ServerIdentity(
    user=IdentityUser(USER_ID, 'owner@example.test', 'Owner'),
    organization=IdentityOrganization(ORGANIZATION_ID, 'Roastery', 'roastery'),
    role='admin',
)


def public_failure(
    kind: FailureKind,
    *,
    retryable: bool,
    code: str | None = None,
) -> PublicFailure:
    return PublicFailure(
        kind=kind,
        code=kind.value if code is None else code,
        message=FAILURE_MESSAGES[kind],
        retryable=retryable,
    )


def api_failure(status_code: int, *, retry_after: int | None = None) -> ApiFailure:
    if status_code == 401:
        kind = FailureKind.CREDENTIAL_REJECTED
        retryable = False
    elif status_code == 429:
        kind = FailureKind.RATE_LIMITED
        retryable = True
    elif 500 <= status_code <= 599:
        kind = FailureKind.OFFLINE
        retryable = True
    elif 400 <= status_code <= 499:
        kind = FailureKind.PROFILE_REJECTED
        retryable = False
    else:
        kind = FailureKind.INVALID_RESPONSE
        retryable = False
    return ApiFailure(public_failure(kind, retryable=retryable), status_code, retry_after)


def revision_for(content: bytes, *, number: int = 1) -> Revision:
    return Revision(
        revision_number=number,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        parser_version='worker-test',
        parse_state='parsed',
        parse_diagnostic_code=None,
        parse_diagnostic_message=None,
        uploaded_at=NOW,
        metadata=(),
        reparse_recommended=False,
    )


def summary_for(revision: Revision) -> RoastSummary:
    return RoastSummary(
        roast_uuid=ROAST_UUID,
        state='parsed',
        roast_at=NOW,
        title='Worker roast',
        batch_prefix=None,
        batch_number=None,
        batch_position=None,
        operator=None,
        machine='Test drum',
        machine_setup=None,
        temperature_unit='C',
        duration_seconds=600,
        green_weight_kg=1.0,
        roasted_weight_kg=0.85,
        revision_count=revision.revision_number,
        updated_at=NOW,
        labels=(),
    )


def detail_for(content: bytes) -> RoastDetail:
    revision = revision_for(content)
    return RoastDetail(
        roast_uuid=ROAST_UUID,
        state='parsed',
        roast_at=NOW,
        title='Worker roast',
        batch_prefix=None,
        batch_number=None,
        batch_position=None,
        operator=None,
        machine='Test drum',
        machine_setup=None,
        temperature_unit='C',
        duration_seconds=600,
        green_weight_kg=1.0,
        roasted_weight_kg=0.85,
        revision_count=1,
        updated_at=NOW,
        labels=(),
        current_metadata=(),
        current_revision=revision,
        links=RoastDetailLinks(
            self_path=f'/api/v1/roasts/{ROAST_UUID.hex}',
            chart=f'/api/v1/roasts/{ROAST_UUID.hex}/chart',
            revisions=f'/api/v1/roasts/{ROAST_UUID.hex}/revisions',
        ),
    )


def upload_for(roast_uuid: UUID, content: bytes) -> RevisionUpload:
    revision = revision_for(content)
    return RevisionUpload(
        roast_uuid=roast_uuid,
        state='parsed',
        revision=revision,
        links=RevisionUploadLinks(
            roast=f'/api/v1/roasts/{roast_uuid.hex}',
            chart=f'/api/v1/roasts/{roast_uuid.hex}/chart',
            revisions=f'/api/v1/roasts/{roast_uuid.hex}/revisions',
            download=(
                f'/api/v1/roasts/{roast_uuid.hex}/revisions/'
                f'{revision.revision_number}/download'
            ),
        ),
    )


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    @property
    def now(self) -> datetime:
        return self()

    def advance(self, seconds: int) -> None:
        with self._lock:
            self._now += timedelta(seconds=seconds)


class FakeCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {ORIGIN: SECRET}
        self.get_calls: list[tuple[str, int]] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.delete_calls: list[tuple[str, int]] = []
        self.failure: CredentialStoreError | None = None

    def get(self, origin: str) -> str | None:
        self.get_calls.append((origin, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        return self.values.get(origin)

    def set(self, origin: str, credential: str) -> None:
        self.set_calls.append((origin, credential, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        self.values[origin] = credential

    def delete(self, origin: str) -> None:
        self.delete_calls.append((origin, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        self.values.pop(origin, None)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.enter_threads: list[int] = []
        self.exit_threads: list[int] = []
        self.failure_method: str | None = None
        self.failure: BaseException | None = None
        self.callback: Callable[[str], None] | None = None
        self.download_content = PROFILE_BYTES
        self.page = RoastPage((summary_for(revision_for(PROFILE_BYTES)),), None)
        self.identity = IDENTITY
        self.upload_override: RevisionUpload | None = None

    def __enter__(self) -> Self:
        self.enter_threads.append(int(QThread.currentThreadId()))
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.exit_threads.append(int(QThread.currentThreadId()))

    def _before_result(self, method: str) -> None:
        if self.callback is not None:
            self.callback(method)
        if self.failure_method == method and self.failure is not None:
            raise self.failure

    def test_connection(self) -> ServerIdentity:
        self.calls.append(('test_connection',))
        self._before_result('test_connection')
        return self.identity

    def post_aroast(self, roast_uuid: UUID, aroast_json: bytes) -> None:
        self.calls.append(('post_aroast', roast_uuid, aroast_json))
        self._before_result('post_aroast')

    def upload_revision(
        self,
        roast_uuid: UUID,
        sha256: str,
        idempotency_key: str,
        metadata_json: bytes,
        snapshot: BinaryIO,
    ) -> RevisionUpload:
        content = snapshot.read()
        self.calls.append(
            (
                'upload_revision',
                roast_uuid,
                sha256,
                idempotency_key,
                metadata_json,
                hashlib.sha256(content).hexdigest(),
            )
        )
        self._before_result('upload_revision')
        return self.upload_override or upload_for(roast_uuid, content)

    def list_roasts(
        self,
        filters: ArchiveFilters,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RoastPage:
        self.calls.append(('list_roasts', filters, cursor, limit))
        self._before_result('list_roasts')
        return self.page

    def get_roast(self, roast_uuid: UUID) -> RoastDetail:
        self.calls.append(('get_roast', roast_uuid))
        self._before_result('get_roast')
        return detail_for(self.download_content)

    def download_revision(
        self, detail: RoastDetail, destination: BinaryIO
    ) -> DownloadReceipt:
        self.calls.append(('download_revision', detail.roast_uuid))
        destination.write(self.download_content)
        destination.flush()
        self._before_result('download_revision')
        revision = cast(Revision, detail.current_revision)
        return DownloadReceipt(
            detail.roast_uuid,
            revision.revision_number,
            revision.sha256,
            revision.byte_size,
            f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog',
        )


class FakeClientFactory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, origin: str, credential: str) -> FakeClient:
        self.calls.append((origin, credential, int(QThread.currentThreadId())))
        return self.client


class RecordingOutbox(Outbox):
    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        super().__init__(root, clock)
        self.enqueued: list[EnqueueResult] = []
        self.leased: list[Job] = []
        self.complete_calls: list[tuple[str, str, datetime, int]] = []
        self.retry_calls: list[tuple[str, str, datetime, datetime, PublicFailure, int]] = []
        self.failed_calls: list[tuple[str, str, datetime, PublicFailure, int]] = []
        self.closed_threads: list[int] = []

    @override
    def enqueue(
        self,
        namespace: Namespace,
        snapshot: Snapshot,
        roast_uuid: UUID,
        metadata: ProjectedMetadata,
        client_uuid: UUID,
    ) -> EnqueueResult:
        result = super().enqueue(namespace, snapshot, roast_uuid, metadata, client_uuid)
        self.enqueued.append(result)
        return result

    @override
    def lease_next(
        self, namespace: Namespace, now: datetime, lease_seconds: int = 60
    ) -> Job | None:
        result = super().lease_next(namespace, now, lease_seconds)
        if result is not None:
            self.leased.append(result)
        return result

    @override
    def mark_complete(self, job_id: str, lease_token: str, now: datetime) -> None:
        self.complete_calls.append((job_id, lease_token, now, int(QThread.currentThreadId())))
        super().mark_complete(job_id, lease_token, now)

    @override
    def mark_retry(
        self,
        job_id: str,
        lease_token: str,
        now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> None:
        self.retry_calls.append(
            (
                job_id,
                lease_token,
                now,
                next_attempt_at,
                failure,
                int(QThread.currentThreadId()),
            )
        )
        super().mark_retry(job_id, lease_token, now, next_attempt_at, failure)

    @override
    def mark_failed(
        self,
        job_id: str,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
    ) -> None:
        self.failed_calls.append(
            (job_id, lease_token, now, failure, int(QThread.currentThreadId()))
        )
        super().mark_failed(job_id, lease_token, now, failure)

    @override
    def close(self) -> None:
        self.closed_threads.append(int(QThread.currentThreadId()))
        super().close()


class RecordingCache(CacheStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.discard_calls: list[tuple[Path, int]] = []
        self.publish_calls: list[tuple[Path, int]] = []
        self.clear_calls: list[tuple[Namespace, frozenset[Path], int]] = []
        self.close_stage_counts: list[int] = []
        self.close_threads: list[int] = []
        self.timer_stopped: Callable[[], bool] = lambda: False
        self.timer_was_stopped_on_close: list[bool] = []
        self.fail_next_discard = False

    @override
    def discard_staging(self, path: Path) -> None:
        self.discard_calls.append((Path(path), int(QThread.currentThreadId())))
        if self.fail_next_discard:
            self.fail_next_discard = False
            raise CacheError
        super().discard_staging(path)

    @override
    def publish(
        self,
        namespace: Namespace,
        detail: RoastDetail,
        receipt: DownloadReceipt,
        staged_path: Path,
        validated_at: datetime,
    ) -> CachedRevision:
        self.publish_calls.append((Path(staged_path), int(QThread.currentThreadId())))
        return super().publish(namespace, detail, receipt, staged_path, validated_at)

    @override
    def clear_unused(
        self, namespace: Namespace, protected_paths: frozenset[Path]
    ) -> CacheStats:
        self.clear_calls.append(
            (namespace, protected_paths, int(QThread.currentThreadId()))
        )
        return super().clear_unused(namespace, protected_paths)

    @override
    def close(self) -> None:
        self.close_stage_counts.append(len(self._staging))
        self.close_threads.append(int(QThread.currentThreadId()))
        self.timer_was_stopped_on_close.append(self.timer_stopped())
        super().close()


class DeterministicTimer(QTimer):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.delays: list[tuple[int, int]] = []
        self.stop_threads: list[int] = []
        self.logically_active = False

    @override
    def start(self, msec: int | None = None) -> None:
        delay = self.interval() if msec is None else msec
        self.delays.append((delay, int(QThread.currentThreadId())))
        self.logically_active = True

    @override
    def stop(self) -> None:
        self.stop_threads.append(int(QThread.currentThreadId()))
        self.logically_active = False


class CommandBus(QObject):
    configure_worker = pyqtSignal(object)
    test_worker = pyqtSignal(str)
    enqueue_worker = pyqtSignal(str)
    retry_worker = pyqtSignal(str)
    remove_worker = pyqtSignal(str)
    browse_worker = pyqtSignal(str)
    online_worker = pyqtSignal(str)
    cached_worker = pyqtSignal(str)
    publish_worker = pyqtSignal(str)
    discard_worker = pyqtSignal(str)
    clear_worker = pyqtSignal(str)
    tick_worker = pyqtSignal()
    stop_worker = pyqtSignal()


class WorkerHarness:
    def __init__(
        self,
        tmp_path: Path,
        app: QCoreApplication,
        clock: MutableClock | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.app = app
        self.clock = clock or MutableClock()
        self.credentials = FakeCredentialStore()
        self.client = FakeClient()
        self.client_factory = FakeClientFactory(self.client)
        self.outbox = RecordingOutbox(tmp_path / 'outbox', self.clock)
        self.cache = RecordingCache(tmp_path / 'cache')
        self.secret_vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
        self.profile_vault: OpaqueVault[SavedProfileRequest] = OpaqueVault()
        self.command_vault: OpaqueVault[object] = OpaqueVault()
        self.timer: DeterministicTimer | None = None
        self.timer_created_thread: int | None = None
        self.thread = QThread()
        self.bus = CommandBus()
        self._queue_spy: QSignalSpy | None = None
        self.worker = RoastServerWorker(
            outbox=self.outbox,
            cache=self.cache,
            credentials=self.credentials,
            client_factory=cast(ClientFactory, self.client_factory),
            clock=self.clock,
            credential_vault=self.secret_vault,
            profile_vault=self.profile_vault,
            command_vault=self.command_vault,
            timer_factory=self._timer_factory,
        )
        self.worker.moveToThread(self.thread)
        self.bus.configure_worker.connect(self.worker.configure)
        self.bus.test_worker.connect(self.worker.test_connection)
        self.bus.enqueue_worker.connect(self.worker.enqueue_saved)
        self.bus.retry_worker.connect(self.worker.retry_job)
        self.bus.remove_worker.connect(self.worker.remove_job)
        self.bus.browse_worker.connect(self.worker.browse)
        self.bus.online_worker.connect(self.worker.open_online)
        self.bus.cached_worker.connect(self.worker.open_cached)
        self.bus.publish_worker.connect(self.worker.publish_staged)
        self.bus.discard_worker.connect(self.worker.discard_staged)
        self.bus.clear_worker.connect(self.worker.clear_unused)
        self.bus.tick_worker.connect(self.worker.process_queue_once)
        self.bus.stop_worker.connect(self.worker.stop)
        self.thread.started.connect(self.worker.start)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.start()
        self.wait_until(lambda: self.timer is not None)
        self.cache.timer_stopped = lambda: self.timer is not None and not self.timer.logically_active
        self.configure()

    @property
    def ui_thread_id(self) -> int:
        return int(QThread.currentThreadId())

    @property
    def worker_thread_id(self) -> int:
        assert self.timer_created_thread is not None
        return self.timer_created_thread

    def _timer_factory(self, parent: QObject) -> QTimer:
        self.timer_created_thread = int(QThread.currentThreadId())
        self.timer = DeterministicTimer(parent)
        return self.timer

    def configure(
        self,
        *,
        origin: str = ORIGIN,
        namespace: Namespace | None = NAMESPACE,
        enabled: bool = True,
        automatic_upload: bool = True,
    ) -> None:
        before = len(self.queue_spy)
        self.bus.configure_worker.emit(
            WorkerConfiguration(
                origin=origin,
                namespace=namespace,
                enabled=enabled,
                automatic_upload=automatic_upload,
                client_instance_uuid=CLIENT_UUID,
                cache_limit_bytes=64 * 1024 * 1024,
            )
        )
        self.wait_until(lambda: len(self.queue_spy) > before)

    @property
    def queue_spy(self) -> QSignalSpy:
        if self._queue_spy is None:
            self._queue_spy = QSignalSpy(self.worker.queueChanged)
        return self._queue_spy

    def wait_until(self, predicate: Callable[[], bool], timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError('bounded Qt event wait timed out')
            time.sleep(0.001)
        self.app.processEvents()

    def wait_for_spy(self, spy: QSignalSpy, before: int, timeout: float = 2.0) -> list[object]:
        self.wait_until(lambda: len(spy) > before, timeout)
        return list(spy[-1])

    def enqueue_saved_profile(self, *, manual: bool = False) -> Job:
        path = self.tmp_path / f'{uuid4().hex}.alog'
        path.write_bytes(PROFILE_BYTES)
        profile = ProfileData(roastUUID=str(ROAST_UUID), title='Worker roast')
        request_id = self.profile_vault.put(
            SavedProfileRequest(NAMESPACE, path, profile, manual)
        )
        before = len(self.queue_spy)
        self.bus.enqueue_worker.emit(request_id)
        self.wait_until(lambda: len(self.outbox.enqueued) > 0 and len(self.queue_spy) > before)
        return self.outbox.enqueued[-1].job

    def run_one_queue_tick(self) -> None:
        before = len(self.queue_spy)
        self.bus.tick_worker.emit()
        self.wait_until(lambda: len(self.queue_spy) > before)

    def request_connection_test(self, credential: str = SECRET) -> str:
        request_id = self.secret_vault.put(ConnectionTestRequest(ORIGIN, credential))
        self.bus.test_worker.emit(request_id)
        return request_id

    def open_online(self) -> tuple[str, PublishRequest]:
        spy = QSignalSpy(self.worker.downloadStaged)
        request_id = self.command_vault.put(OnlineOpenRequest(NAMESPACE, ROAST_UUID))
        self.bus.online_worker.emit(request_id)
        payload = self.wait_for_spy(spy, 0)
        assert payload[0] == request_id
        request = cast(PublishRequest, payload[1])
        return request_id, request

    def stop(self) -> None:
        if not self.thread.isRunning():
            return
        spy = QSignalSpy(self.worker.stopped)
        self.bus.stop_worker.emit()
        self.wait_until(lambda: len(spy) == 1)
        self.thread.quit()
        assert self.thread.wait(2_000)


@pytest.fixture(scope='module')
def qcoreapplication() -> Generator[QCoreApplication, None, None]:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def worker_harness(
    tmp_path: Path, qcoreapplication: QCoreApplication
) -> Generator[WorkerHarness, None, None]:
    harness = WorkerHarness(tmp_path, qcoreapplication)
    yield harness
    harness.stop()


def test_opaque_vault_and_secret_request_repr_are_redacted() -> None:
    vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
    request = ConnectionTestRequest(ORIGIN, SECRET)
    request_id = vault.put(request)

    assert SECRET not in repr(request)
    assert SECRET not in repr(vault)
    assert vault.contains(request_id)
    assert vault.size() == 1
    assert vault.take(request_id) is request
    assert not vault.contains(request_id)
    with pytest.raises(KeyError):
        vault.take(request_id)


def test_start_timer_and_public_connection_signal_run_on_worker_thread(
    worker_harness: WorkerHarness,
) -> None:
    spy = QSignalSpy(worker_harness.worker.connectionTested)
    signal_threads: list[int] = []
    direct_connect = cast(
        Callable[[Callable[[str, object], None], Qt.ConnectionType], object],
        worker_harness.worker.connectionTested.connect,
    )
    direct_connect(
        lambda _request_id, _identity: signal_threads.append(int(QThread.currentThreadId())),
        Qt.ConnectionType.DirectConnection,
    )

    request_id = worker_harness.request_connection_test()
    payload = worker_harness.wait_for_spy(spy, 0)

    assert payload == [request_id, IDENTITY]
    assert SECRET not in repr(payload)
    assert signal_threads == [worker_harness.worker_thread_id]
    assert signal_threads[0] != worker_harness.ui_thread_id
    assert worker_harness.timer is not None
    assert worker_harness.timer.thread() is worker_harness.thread
    assert worker_harness.credentials.set_calls[-1][:2] == (ORIGIN, SECRET)
    assert worker_harness.credentials.set_calls[-1][2] == worker_harness.worker_thread_id
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads) == 1
    assert worker_harness.client.enter_threads == worker_harness.client.exit_threads
    for root in (worker_harness.outbox.root, worker_harness.cache.root):
        for path in root.rglob('*'):
            if path.is_file():
                assert SECRET.encode('utf-8') not in path.read_bytes()


def test_connection_writes_keyring_only_after_success_and_fixed_keyring_failure(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    tested = QSignalSpy(worker_harness.worker.connectionTested)
    worker_harness.client.failure_method = 'test_connection'
    worker_harness.client.failure = api_failure(401)

    first_id = worker_harness.request_connection_test('first-candidate')
    first_payload = worker_harness.wait_for_spy(failed, 0)
    assert first_payload == [first_id, public_failure(FailureKind.CREDENTIAL_REJECTED, retryable=False)]
    assert all(call[1] != 'first-candidate' for call in worker_harness.credentials.set_calls)
    assert worker_harness.credentials.delete_calls == []

    worker_harness.client.failure_method = None
    worker_harness.credentials.failure = CredentialStoreError('backend leaked detail')
    second_id = worker_harness.request_connection_test('second-candidate')
    second_payload = worker_harness.wait_for_spy(failed, 1)
    assert len(tested) == 0
    assert second_payload == [second_id, public_failure(FailureKind.KEYRING, retryable=False)]
    assert 'backend leaked detail' not in repr(second_payload)
    assert 'second-candidate' not in repr(second_payload)
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads)


def test_delivery_posts_aroast_before_exact_snapshot_upload(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    worker_harness.run_one_queue_tick()

    assert worker_harness.client.calls == [
        ('post_aroast', job.roast_uuid, job.aroast_json.encode('utf-8')),
        (
            'upload_revision',
            job.roast_uuid,
            job.content_sha256,
            job.idempotency_key,
            job.revision_json.encode('utf-8'),
            job.snapshot_sha256,
        ),
    ]
    leased = worker_harness.outbox.leased[-1]
    assert leased.lease_token is not None
    assert worker_harness.outbox.complete_calls[-1][:3] == (
        leased.id,
        leased.lease_token,
        worker_harness.clock.now,
    )
    assert worker_harness.outbox.complete_calls[-1][3] == worker_harness.worker_thread_id
    assert worker_harness.outbox.counts(NAMESPACE).complete == 1
    assert job.snapshot_path is not None and not job.snapshot_path.exists()
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads)


def test_manual_enqueue_loads_snapshot_when_automatic_upload_is_off(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.configure(enabled=True, automatic_upload=False)
    path = worker_harness.tmp_path / 'manual.alog'
    path.write_bytes(PROFILE_BYTES)
    request_id = worker_harness.profile_vault.put(
        SavedProfileRequest(NAMESPACE, path, None, True)
    )
    before = len(worker_harness.queue_spy)

    worker_harness.bus.enqueue_worker.emit(request_id)
    worker_harness.wait_until(
        lambda: len(worker_harness.queue_spy) > before
        and len(worker_harness.outbox.enqueued) == 1
    )

    assert worker_harness.outbox.enqueued[0].created
    assert worker_harness.outbox.enqueued[0].job.roast_uuid == ROAST_UUID
    worker_harness.run_one_queue_tick()
    assert worker_harness.outbox.counts(NAMESPACE).complete == 1


def test_completed_current_hash_duplicate_does_not_upload_again(
    worker_harness: WorkerHarness,
) -> None:
    first = worker_harness.enqueue_saved_profile()
    worker_harness.run_one_queue_tick()
    calls_after_first = tuple(worker_harness.client.calls)

    duplicate = worker_harness.enqueue_saved_profile()
    assert duplicate.id == first.id
    assert not worker_harness.outbox.enqueued[-1].created
    assert duplicate.state == 'complete'
    assert duplicate.snapshot_path is None
    worker_harness.run_one_queue_tick()

    assert tuple(worker_harness.client.calls) == calls_after_first
    assert worker_harness.outbox.counts(NAMESPACE).complete == 1


def test_transient_backoff_retry_after_and_exact_tokens(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'post_aroast'
    worker_harness.client.failure = api_failure(503, retry_after=42)

    expected_delays = [42, 10, 20, 40, 80, 160, 300]
    for attempt, expected_delay in enumerate(expected_delays, start=1):
        worker_harness.run_one_queue_tick()
        leased = worker_harness.outbox.leased[-1]
        retry = worker_harness.outbox.retry_calls[-1]
        assert leased.attempts == attempt
        assert leased.lease_token is not None
        assert retry[:3] == (leased.id, leased.lease_token, worker_harness.clock.now)
        assert retry[3] == worker_harness.clock.now + timedelta(seconds=expected_delay)
        assert retry[4].retryable
        assert retry[5] == worker_harness.worker_thread_id
        assert worker_harness.timer is not None
        assert worker_harness.timer.delays[-1][0] == expected_delay * 1_000
        worker_harness.clock.advance(expected_delay)
        worker_harness.client.failure = api_failure(503)

    assert worker_harness.outbox.counts(NAMESPACE).retrying == 1


def test_retry_timestamp_survives_worker_restart(
    worker_harness: WorkerHarness, qcoreapplication: QCoreApplication
) -> None:
    job = worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'post_aroast'
    worker_harness.client.failure = api_failure(503)
    worker_harness.run_one_queue_tick()
    retry_at = worker_harness.outbox.retry_calls[-1][3]
    assert retry_at == worker_harness.clock.now + timedelta(seconds=5)
    worker_harness.stop()

    worker_harness.clock.advance(5)
    restarted = WorkerHarness(
        worker_harness.tmp_path, qcoreapplication, worker_harness.clock
    )
    try:
        restarted.run_one_queue_tick()
        leased = restarted.outbox.leased[-1]
        assert leased.id == job.id
        assert leased.attempts == 2
        assert leased.lease_token is not None
        assert restarted.outbox.complete_calls[-1][:3] == (
            job.id,
            leased.lease_token,
            worker_harness.clock.now,
        )
        assert restarted.outbox.counts(NAMESPACE).complete == 1
    finally:
        restarted.stop()


def test_401_fences_attempt_then_pauses_without_deleting_credential(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'post_aroast'
    worker_harness.client.failure = api_failure(401)

    worker_harness.run_one_queue_tick()

    leased = worker_harness.outbox.leased[-1]
    retry = worker_harness.outbox.retry_calls[-1]
    assert leased.lease_token is not None
    assert retry[:3] == (leased.id, leased.lease_token, worker_harness.clock.now)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    assert worker_harness.credentials.delete_calls == []


@pytest.mark.parametrize(
    'failure',
    [
        api_failure(400),
        ApiFailure(public_failure(FailureKind.LOCAL_PROFILE, retryable=False), None, None),
    ],
)
def test_permanent_server_and_local_failures_use_exact_lease_token(
    worker_harness: WorkerHarness, failure: ApiFailure
) -> None:
    worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'upload_revision'
    worker_harness.client.failure = failure

    worker_harness.run_one_queue_tick()

    leased = worker_harness.outbox.leased[-1]
    failed = worker_harness.outbox.failed_calls[-1]
    assert leased.lease_token is not None
    assert failed[:3] == (leased.id, leased.lease_token, worker_harness.clock.now)
    assert failed[3] == failure.failure
    assert worker_harness.outbox.counts(NAMESPACE).failed == 1


@pytest.mark.parametrize('outcome', ['complete', 'retry', 'failed'])
def test_stale_attempt_cannot_commit_after_recovery_and_new_lease(
    worker_harness: WorkerHarness, outcome: str
) -> None:
    job = worker_harness.enqueue_saved_profile()
    contender = Outbox(worker_harness.outbox.root, worker_harness.clock)
    contender.open()
    attempt_b: list[Job] = []

    def recover_and_release(method: str) -> None:
        if method != 'upload_revision' or attempt_b:
            return
        later = worker_harness.clock.now + timedelta(seconds=61)
        assert contender.recover_expired_leases(later) == 1
        recovered = contender.lease_next(NAMESPACE, later)
        assert recovered is not None and recovered.lease_token is not None
        attempt_b.append(recovered)
        if outcome == 'retry':
            worker_harness.client.failure_method = 'upload_revision'
            worker_harness.client.failure = api_failure(503)
        elif outcome == 'failed':
            worker_harness.client.failure_method = 'upload_revision'
            worker_harness.client.failure = api_failure(400)

    worker_harness.client.callback = recover_and_release
    worker_harness.run_one_queue_tick()

    attempt_a = worker_harness.outbox.leased[-1]
    assert attempt_b and attempt_a.lease_token is not None
    assert attempt_b[0].lease_token is not None
    assert attempt_a.lease_token != attempt_b[0].lease_token
    if outcome == 'complete':
        assert worker_harness.outbox.complete_calls[-1][1] == attempt_a.lease_token
    elif outcome == 'retry':
        assert worker_harness.outbox.retry_calls[-1][1] == attempt_a.lease_token
    else:
        assert worker_harness.outbox.failed_calls[-1][1] == attempt_a.lease_token
    assert contender.counts(NAMESPACE).pending == 1

    later = worker_harness.clock.now + timedelta(seconds=61)
    contender.mark_complete(job.id, attempt_b[0].lease_token, later)
    assert contender.counts(NAMESPACE).complete == 1
    contender.close()


def test_interruption_leaves_lease_for_expiry_recovery(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()

    def interrupt(method: str) -> None:
        if method == 'upload_revision':
            thread = QThread.currentThread()
            assert thread is not None
            thread.requestInterruption()

    worker_harness.client.callback = interrupt
    worker_harness.run_one_queue_tick()

    leased = worker_harness.outbox.leased[-1]
    assert leased.lease_token is not None
    assert worker_harness.outbox.complete_calls == []
    assert worker_harness.outbox.retry_calls == []
    assert worker_harness.outbox.failed_calls == []
    assert worker_harness.outbox.counts(NAMESPACE).pending == 1

    worker_harness.stop()
    reopened = Outbox(worker_harness.outbox.root, worker_harness.clock)
    reopened.open()
    worker_harness.clock.advance(61)
    assert reopened.recover_expired_leases(worker_harness.clock.now) == 1
    recovered = reopened.lease_next(NAMESPACE, worker_harness.clock.now)
    assert recovered is not None and recovered.lease_token is not None
    assert recovered.lease_token != leased.lease_token
    reopened.close()


def test_disable_missing_credential_restore_and_namespace_switch_are_isolated(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    worker_harness.configure(enabled=False, automatic_upload=False)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    calls_before = len(worker_harness.client.calls)
    worker_harness.run_one_queue_tick()
    assert len(worker_harness.client.calls) == calls_before

    worker_harness.credentials.values.pop(ORIGIN)
    worker_harness.configure(enabled=True)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    worker_harness.credentials.values[ORIGIN] = SECRET
    worker_harness.configure(enabled=True)
    assert worker_harness.outbox.counts(NAMESPACE).pending == 1

    other_namespace = namespace_for(
        ORIGIN, UUID('55555555-5555-4555-8555-555555555555')
    )
    worker_harness.configure(namespace=other_namespace)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    before = len(worker_harness.client.calls)
    worker_harness.run_one_queue_tick()
    assert len(worker_harness.client.calls) == before
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1

    failed = QSignalSpy(worker_harness.worker.operationFailed)
    invalid = WorkerConfiguration(
        origin='https://other.example.test',
        namespace=NAMESPACE,
        enabled=True,
        automatic_upload=True,
        client_instance_uuid=CLIENT_UUID,
        cache_limit_bytes=64 * 1024 * 1024,
    )
    worker_harness.bus.configure_worker.emit(invalid)
    payload = worker_harness.wait_for_spy(failed, 0)
    assert payload[0] == 'configure'
    assert cast(PublicFailure, payload[1]).kind is FailureKind.INVALID_RESPONSE
    assert all(call[0] != 'https://other.example.test' for call in worker_harness.credentials.get_calls)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    assert job.id


def test_failed_job_retry_remove_and_immutable_aggregate_signals(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'post_aroast'
    worker_harness.client.failure = api_failure(400)
    failed_jobs = QSignalSpy(worker_harness.worker.failedJobsChanged)
    worker_harness.run_one_queue_tick()

    failed_payload = cast(tuple[FailedJob, ...], failed_jobs[-1][0])
    assert len(failed_payload) == 1
    assert failed_payload[0].id == job.id
    assert SECRET not in repr(failed_payload)

    before = len(worker_harness.queue_spy)
    worker_harness.bus.retry_worker.emit(job.id)
    worker_harness.wait_until(lambda: len(worker_harness.queue_spy) > before)
    assert worker_harness.outbox.counts(NAMESPACE).pending == 1

    worker_harness.run_one_queue_tick()
    assert worker_harness.outbox.counts(NAMESPACE).failed == 1
    before = len(worker_harness.queue_spy)
    worker_harness.bus.remove_worker.emit(job.id)
    worker_harness.wait_until(lambda: len(worker_harness.queue_spy) > before)
    assert worker_harness.outbox.counts(NAMESPACE).failed == 0
    assert job.snapshot_path is not None and not job.snapshot_path.exists()


def test_browse_online_then_retained_offline_cache_fallback(
    worker_harness: WorkerHarness,
) -> None:
    published = QSignalSpy(worker_harness.worker.cachePublished)
    _online_id, publish_request = worker_harness.open_online()
    publish_id = worker_harness.command_vault.put(publish_request)
    worker_harness.bus.publish_worker.emit(publish_id)
    cached = cast(CachedRevision, worker_harness.wait_for_spy(published, 0)[1])

    page_spy = QSignalSpy(worker_harness.worker.archivePageReady)
    online_spy = QSignalSpy(worker_harness.worker.onlineChanged)
    filters = ArchiveFilters(search='Worker')
    first_id = worker_harness.command_vault.put(
        BrowseRequest(NAMESPACE, filters, None, True)
    )
    worker_harness.bus.browse_worker.emit(first_id)
    online_payload = worker_harness.wait_for_spy(page_spy, 0)

    assert online_payload == [first_id, worker_harness.client.page]
    assert online_spy[-1] == [True]
    assert worker_harness.client.calls[-1] == ('list_roasts', filters, None, 50)

    worker_harness.client.failure_method = 'list_roasts'
    worker_harness.client.failure = api_failure(503)
    second_id = worker_harness.command_vault.put(
        BrowseRequest(NAMESPACE, filters, None, True)
    )
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    worker_harness.bus.browse_worker.emit(second_id)
    fallback = worker_harness.wait_for_spy(page_spy, 1)

    assert fallback == [second_id, CachedPage((cached,))]
    assert worker_harness.wait_for_spy(failed, 0) == [second_id, worker_harness.client.failure.failure]
    assert online_spy[-1] == [False]
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads)


def test_online_download_publish_cached_validation_and_cache_signals(
    worker_harness: WorkerHarness,
) -> None:
    cache_spy = QSignalSpy(worker_harness.worker.cachePublished)
    cached_spy = QSignalSpy(worker_harness.worker.cachedReady)
    stats_spy = QSignalSpy(worker_harness.worker.cacheStatsChanged)
    _online_id, request = worker_harness.open_online()
    assert request.staged_path.exists()
    assert request.staged_path.read_bytes() == PROFILE_BYTES

    publish_id = worker_harness.command_vault.put(request)
    worker_harness.bus.publish_worker.emit(publish_id)
    published_payload = worker_harness.wait_for_spy(cache_spy, 0)
    cached = cast(CachedRevision, published_payload[1])

    assert published_payload[0] == publish_id
    assert cached.path.exists() and cached.path.read_bytes() == PROFILE_BYTES
    assert not request.staged_path.exists()
    assert worker_harness.cache.publish_calls[-1] == (
        request.staged_path,
        worker_harness.worker_thread_id,
    )
    worker_harness.wait_until(lambda: len(stats_spy) > 0)
    assert stats_spy[-1][0].revision_count == 1

    cached_id = worker_harness.command_vault.put(CachedOpenRequest(cached))
    worker_harness.bus.cached_worker.emit(cached_id)
    assert worker_harness.wait_for_spy(cached_spy, 0) == [cached_id, cached]


@pytest.mark.parametrize(
    'download_failure',
    [
        api_failure(503),
        ApiFailure(
            public_failure(FailureKind.CHECKSUM_MISMATCH, retryable=False),
            200,
            None,
        ),
        ApiFailure(
            public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
            200,
            None,
        ),
    ],
)
def test_online_download_failures_and_ui_discard_consume_every_stage(
    worker_harness: WorkerHarness, download_failure: ApiFailure
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    worker_harness.client.failure_method = 'download_revision'
    worker_harness.client.failure = download_failure
    request_id = worker_harness.command_vault.put(
        OnlineOpenRequest(NAMESPACE, ROAST_UUID)
    )
    worker_harness.bus.online_worker.emit(request_id)
    worker_harness.wait_for_spy(failed, 0)
    assert len(worker_harness.cache.discard_calls) == 1
    assert not worker_harness.cache.discard_calls[-1][0].exists()

    worker_harness.client.failure_method = None
    _online_id, request = worker_harness.open_online()
    before = len(worker_harness.cache.discard_calls)
    worker_harness.bus.discard_worker.emit(str(request.staged_path))
    worker_harness.wait_until(lambda: len(worker_harness.cache.discard_calls) > before)
    assert not request.staged_path.exists()
    assert worker_harness.cache.discard_calls[-1][1] == worker_harness.worker_thread_id


def test_online_interruption_discards_stage_before_ui_handoff(
    worker_harness: WorkerHarness,
) -> None:
    staged = QSignalSpy(worker_harness.worker.downloadStaged)

    def interrupt(method: str) -> None:
        if method == 'download_revision':
            thread = QThread.currentThread()
            assert thread is not None
            thread.requestInterruption()

    worker_harness.client.callback = interrupt
    request_id = worker_harness.command_vault.put(
        OnlineOpenRequest(NAMESPACE, ROAST_UUID)
    )
    worker_harness.bus.online_worker.emit(request_id)
    worker_harness.wait_until(
        lambda: len(worker_harness.cache.discard_calls) == 1
        and not worker_harness.cache.discard_calls[0][0].exists()
    )

    assert len(staged) == 0
    assert not worker_harness.cache.discard_calls[0][0].exists()
    assert worker_harness.cache.discard_calls[0][1] == worker_harness.worker_thread_id


def test_publish_vault_loss_and_invalid_publish_discard_pending_stage(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    _online_id, request = worker_harness.open_online()

    worker_harness.bus.publish_worker.emit('f' * 32)
    payload = worker_harness.wait_for_spy(failed, 0)

    assert payload[0] == 'f' * 32
    assert cast(PublicFailure, payload[1]).kind is FailureKind.CACHE_CORRUPT
    assert not request.staged_path.exists()
    assert worker_harness.cache.discard_calls[-1][0] == request.staged_path


def test_publish_failure_is_consumed_by_cache_without_double_discard(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    _online_id, request = worker_harness.open_online()
    request.staged_path.write_bytes(b'corrupted after validation handoff')
    discard_before = len(worker_harness.cache.discard_calls)
    publish_id = worker_harness.command_vault.put(request)

    worker_harness.bus.publish_worker.emit(publish_id)
    payload = worker_harness.wait_for_spy(failed, 0)

    assert payload == [publish_id, public_failure(FailureKind.CACHE_CORRUPT, retryable=False)]
    assert len(worker_harness.cache.discard_calls) == discard_before
    assert worker_harness.cache.publish_calls[-1][0] == request.staged_path
    assert not request.staged_path.exists()


def test_clear_unused_unions_open_and_outbox_protected_paths(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    assert job.snapshot_path is not None
    open_path = worker_harness.tmp_path / 'currently-open.alog'
    open_path.write_bytes(b'open')
    stats = QSignalSpy(worker_harness.worker.cacheStatsChanged)
    request_id = worker_harness.command_vault.put(
        ClearUnusedRequest(NAMESPACE, frozenset({open_path}))
    )

    worker_harness.bus.clear_worker.emit(request_id)
    worker_harness.wait_for_spy(stats, 0)

    namespace, protected, thread_id = worker_harness.cache.clear_calls[-1]
    assert namespace == NAMESPACE
    assert protected == frozenset({open_path, job.snapshot_path})
    assert thread_id == worker_harness.worker_thread_id


def test_stop_stops_timer_then_closes_all_stages_and_sqlite_on_worker_thread(
    worker_harness: WorkerHarness,
) -> None:
    _online_id, request = worker_harness.open_online()
    assert request.staged_path.exists()
    assert worker_harness.timer is not None and worker_harness.timer.logically_active
    worker_harness.cache.fail_next_discard = True

    worker_harness.stop()

    assert worker_harness.timer.stop_threads[-1] == worker_harness.worker_thread_id
    assert worker_harness.cache.timer_was_stopped_on_close == [True]
    assert worker_harness.cache.close_stage_counts == [1]
    assert worker_harness.cache.close_threads == [worker_harness.worker_thread_id]
    assert worker_harness.outbox.closed_threads[-1] == worker_harness.worker_thread_id
    assert not request.staged_path.exists()
    assert worker_harness.cache.discard_calls[-1][0] == request.staged_path


def test_worker_module_has_no_plus_dependency() -> None:
    worker_path = Path(__file__).parents[4] / 'artisanlib' / 'roastserver' / 'worker.py'
    tree = ast.parse(worker_path.read_text(encoding='utf-8'))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not any(name == 'plus' or name.startswith('plus.') for name in imported)
