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
import logging
import math
from pathlib import Path
import secrets
import threading
import time
from types import TracebackType
from typing import BinaryIO, Self, cast, override
from uuid import UUID

from PyQt6.QtCore import QCoreApplication, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
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
from artisanlib.roastserver.metadata import ProjectedMetadata, project_profile
from artisanlib.roastserver.outbox import (
    EnqueueResult,
    FailedJob,
    Job,
    Outbox,
    QueueCounts,
    Snapshot,
)
from artisanlib.roastserver.settings import (
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    CredentialStoreError,
    namespace_for,
)
from artisanlib.roastserver.worker import (
    BrowseRequest,
    CachedOpenRequest,
    ClearUnusedRequest,
    ConfigurationFence,
    ConnectionTestRequest,
    OnlineOpenRequest,
    OpaqueVault,
    ProtectedPathsRequest,
    PublishRequest,
    RemoveCredentialRequest,
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
PROFILE_BYTES = repr({'roastUUID': str(ROAST_UUID), 'title': 'Worker roast'}).encode('utf-8')
PROFILE_SHA256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
IDENTITY = ServerIdentity(
    user=IdentityUser(USER_ID, 'owner@example.test', 'Owner'),
    organization=IdentityOrganization(ORGANIZATION_ID, 'Roastery', 'roastery'),
    role='admin',
)
OTHER_IDENTITY = ServerIdentity(
    user=IDENTITY.user,
    organization=IdentityOrganization(
        UUID('55555555-5555-4555-8555-555555555555'),
        'Other Roastery',
        'other-roastery',
    ),
    role='member',
)


def secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def assert_secret_absent(secret: str, value: object) -> None:
    if secret in repr(value):
        pytest.fail('runtime secret exposed by public value', pytrace=False)


def assert_secret_absent_from_file(secret: str, path: Path) -> None:
    if secret.encode('utf-8') in path.read_bytes():
        pytest.fail('runtime secret persisted to connector storage', pytrace=False)


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


class WallClock:
    def __init__(self, origin: datetime = NOW) -> None:
        self._origin = origin
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._calls: list[tuple[datetime, int]] = []

    def __call__(self) -> datetime:
        result = self._origin + timedelta(seconds=time.monotonic() - self._started)
        with self._lock:
            self._calls.append((result, int(QThread.currentThreadId())))
        return result

    def last_call_in_thread(self, thread_id: int) -> datetime:
        with self._lock:
            return next(value for value, called_in in reversed(self._calls) if called_in == thread_id)


class FakeCredentialStore:
    def __init__(self, credential_provider: Callable[[], str]) -> None:
        self._credential_provider = credential_provider
        self._stored_credential = credential_provider()
        self._active = True
        self.get_calls: list[tuple[str, int]] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.delete_calls: list[tuple[str, int]] = []
        self.failure: CredentialStoreError | None = None

    def get(self, origin: str) -> str | None:
        self.get_calls.append((origin, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        return self._stored_credential if origin == ORIGIN and self._active else None

    def set(self, origin: str, credential: str) -> None:
        self.set_calls.append(
            (origin, secret_digest(credential), int(QThread.currentThreadId()))
        )
        if self.failure is not None:
            raise self.failure
        if origin == ORIGIN:
            self._stored_credential = credential
            self._active = True

    def delete(self, origin: str) -> None:
        self.delete_calls.append((origin, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        if origin == ORIGIN:
            self._active = False
            self._stored_credential = ''

    def remove_active(self) -> None:
        self._active = False

    def restore_active(self) -> None:
        self._stored_credential = self._credential_provider()
        self._active = True

    def stored_digest(self) -> str:
        return secret_digest(self._stored_credential)


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
        self.calls.append(
            (origin, secret_digest(credential), int(QThread.currentThreadId()))
        )
        return self.client


class RecordingOutbox(Outbox):
    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        super().__init__(root, clock)
        self.enqueued: list[EnqueueResult] = []
        self.leased: list[Job] = []
        self.complete_calls: list[tuple[str, str, datetime, int]] = []
        self.retry_calls: list[tuple[str, str, datetime, datetime, PublicFailure, int]] = []
        self.failed_calls: list[tuple[str, str, datetime, PublicFailure, int]] = []
        self.resume_calls: list[tuple[Namespace, int]] = []
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
    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
        self.resume_calls.append((namespace, int(QThread.currentThreadId())))
        return super().resume_namespace(namespace, now)

    @override
    def lease_next(
        self, namespace: Namespace, now: datetime, lease_seconds: int = 60
    ) -> Job | None:
        result = super().lease_next(namespace, now, lease_seconds)
        if isinstance(result, Job):
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
        self.constructor_thread = int(QThread.currentThreadId())
        super().__init__(root)
        self.open_threads: list[int] = []
        self.discard_calls: list[tuple[Path, int]] = []
        self.publish_calls: list[tuple[Path, int]] = []
        self.clear_calls: list[tuple[Namespace, frozenset[Path], int]] = []
        self.prune_calls: list[tuple[Namespace, frozenset[Path], int]] = []
        self.close_stage_counts: list[int] = []
        self.close_threads: list[int] = []
        self.timer_stopped: Callable[[], bool] = lambda: False
        self.timer_was_stopped_on_close: list[bool] = []
        self.fail_next_discard = False
        self.prune_entered:threading.Event|None = None
        self.allow_prune:threading.Event|None = None

    @override
    def open(self) -> None:
        self.open_threads.append(int(QThread.currentThreadId()))
        super().open()

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
    def prune(
        self,
        namespace: Namespace,
        limit_bytes: int,
        protected_paths: frozenset[Path],
    ) -> CacheStats:
        self.prune_calls.append(
            (namespace, protected_paths, int(QThread.currentThreadId()))
        )
        if self.prune_entered is not None:
            self.prune_entered.set()
        if self.allow_prune is not None:
            assert self.allow_prune.wait(2)
        return super().prune(namespace, limit_bytes, protected_paths)

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
    commit_worker = pyqtSignal(str)
    finalize_worker = pyqtSignal(str)
    acknowledge_activation_worker = pyqtSignal(str)
    rollback_worker = pyqtSignal(str)
    cancel_transaction_worker = pyqtSignal(str)
    enqueue_worker = pyqtSignal(str)
    retry_worker = pyqtSignal(str)
    remove_worker = pyqtSignal(str)
    browse_worker = pyqtSignal(str)
    online_worker = pyqtSignal(str)
    cached_worker = pyqtSignal(str)
    publish_worker = pyqtSignal(str)
    discard_worker = pyqtSignal(str)
    protect_worker = pyqtSignal(str)
    clear_worker = pyqtSignal(str)
    tick_worker = pyqtSignal()
    probe_worker = pyqtSignal()
    complete_worker = pyqtSignal()
    stop_worker = pyqtSignal()


class WorkerCompletionProbe(QObject):
    completed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: list[int] = []

    @pyqtSlot()
    def complete(self) -> None:
        self.thread_ids.append(int(QThread.currentThreadId()))
        self.completed.emit()


class StockTimerProbe(QObject):
    captured = pyqtSignal()

    def __init__(
        self,
        worker: RoastServerWorker,
        outbox: Outbox,
        clock: WallClock,
    ) -> None:
        super().__init__()
        self._worker = worker
        self._outbox = outbox
        self._clock = clock
        self.timer: QTimer | None = None
        self.observation: (
            tuple[int, bool, datetime | None, datetime, int, bool, tuple[str, ...]] | None
        ) = None

    @pyqtSlot()
    def capture(self) -> None:
        timer = self._worker._timer
        if timer is None:
            return
        thread_id = int(QThread.currentThreadId())
        due = self._outbox.next_due_at(NAMESPACE)
        scheduled_at = self._clock.last_call_in_thread(thread_id)
        self.timer = timer
        self.observation = (
            timer.interval(),
            timer.isActive(),
            due,
            scheduled_at,
            thread_id,
            timer.parent() is self._worker,
            tuple(type(child).__name__ for child in self._worker.children()),
        )
        self.captured.emit()


class PermitBoundaryBlocker:
    def __init__(self) -> None:
        self.target: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()

    def block(self, operation: str) -> None:
        if operation != self.target:
            return
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError('permit boundary blocker timed out')
        self.returned.set()

    def select(self, operation: str) -> None:
        self.target = operation
        self.entered.clear()
        self.release.clear()
        self.returned.clear()


class WorkerHarness:
    def __init__(
        self,
        tmp_path: Path,
        app: QCoreApplication,
        clock: MutableClock | None = None,
        *,
        authenticated_identity: ServerIdentity = IDENTITY,
        operation_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.app = app
        self.clock = clock or MutableClock()
        self.ephemeral_secret = secrets.token_urlsafe(32)
        self.credentials = FakeCredentialStore(lambda: self.ephemeral_secret)
        self.client = FakeClient()
        self.client.identity = authenticated_identity
        self._force_identity_mismatch = authenticated_identity != IDENTITY
        self.client_factory = FakeClientFactory(self.client)
        self.outbox = RecordingOutbox(tmp_path / 'outbox', self.clock)
        self.cache = RecordingCache(tmp_path / 'cache')
        self.secret_vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
        self.profile_vault: OpaqueVault[SavedProfileRequest] = OpaqueVault()
        self.command_vault: OpaqueVault[object] = OpaqueVault()
        self.configuration_fence = ConfigurationFence()
        self.timer: DeterministicTimer | None = None
        self.timer_created_thread: int | None = None
        self.thread = QThread()
        self.bus = CommandBus()
        self._queue_spy: QSignalSpy | None = None
        self.completion_probe = WorkerCompletionProbe()
        self._completion_spy = QSignalSpy(self.completion_probe.completed)
        self.configuration_calls: tuple[tuple[object, ...], ...] = ()
        self.worker = RoastServerWorker(
            outbox=self.outbox,
            cache=self.cache,
            credentials=self.credentials,
            client_factory=cast(ClientFactory, self.client_factory),
            clock=self.clock,
            credential_vault=self.secret_vault,
            profile_vault=self.profile_vault,
            command_vault=self.command_vault,
            configuration_fence=self.configuration_fence,
            timer_factory=self._timer_factory,
            operation_hook=operation_hook,
        )
        self.worker.moveToThread(self.thread)
        self.completion_probe.moveToThread(self.thread)
        self.bus.configure_worker.connect(self.worker.configure)
        self.bus.test_worker.connect(self.worker.test_connection)
        self.bus.commit_worker.connect(self.worker.commit_connection)
        self.bus.finalize_worker.connect(self.worker.finalize_connection)
        self.bus.acknowledge_activation_worker.connect(
            self.worker.acknowledge_connection_activation
        )
        self.bus.rollback_worker.connect(self.worker.rollback_connection)
        self.bus.cancel_transaction_worker.connect(
            self.worker.cancel_connection_transaction
        )
        self.bus.enqueue_worker.connect(self.worker.enqueue_saved)
        self.bus.retry_worker.connect(self.worker.retry_job)
        self.bus.remove_worker.connect(self.worker.remove_job)
        self.bus.browse_worker.connect(self.worker.browse)
        self.bus.online_worker.connect(self.worker.open_online)
        self.bus.cached_worker.connect(self.worker.open_cached)
        self.bus.publish_worker.connect(self.worker.publish_staged)
        self.bus.discard_worker.connect(self.worker.discard_staged)
        self.bus.protect_worker.connect(self.worker.update_protected_paths)
        self.bus.clear_worker.connect(self.worker.clear_unused)
        self.bus.tick_worker.connect(self.worker.process_queue_once)
        self.bus.complete_worker.connect(self.completion_probe.complete)
        self.bus.stop_worker.connect(self.worker.stop)
        self.worker.stopped.connect(self.completion_probe.deleteLater)
        self.worker.stopped.connect(self.worker.deleteLater)
        self.worker.destroyed.connect(self.thread.quit)
        self.thread.started.connect(self.worker.start)
        self.thread.start()
        self.wait_until(lambda: self.timer is not None)
        self.cache.timer_stopped = lambda: self.timer is not None and not self.timer.logically_active
        self.configure()
        self.configuration_calls = tuple(self.client.calls)
        self.client.calls.clear()
        self.client.enter_threads.clear()
        self.client.exit_threads.clear()
        self.client_factory.calls.clear()

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
        identity = (
            None
            if namespace is None
            else ServerIdentity(
                IDENTITY.user,
                IdentityOrganization(
                    namespace.organization_id,
                    IDENTITY.organization.name,
                    IDENTITY.organization.slug,
                ),
                IDENTITY.role,
            )
        )
        if identity is not None and not self._force_identity_mismatch:
            self.client.identity = identity
        self.bus.configure_worker.emit(
            WorkerConfiguration(
                origin=origin,
                namespace=namespace,
                enabled=enabled,
                automatic_upload=automatic_upload,
                client_instance_uuid=CLIENT_UUID,
                cache_limit_bytes=64 * 1024 * 1024,
                generation=self.configuration_fence.advance(),
                identity=identity,
            )
        )
        self.wait_until(lambda: len(self.queue_spy) > before)

    def activation_configuration(
        self, transaction_id: str
    ) -> WorkerConfiguration:
        return WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=self.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )

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
        profile = ProfileData(roastUUID=str(ROAST_UUID), title='Worker roast')
        request_id = self.profile_vault.put(
            SavedProfileRequest(NAMESPACE, PROFILE_BYTES, profile, self.clock.now, manual)
        )
        before = len(self.queue_spy)
        self.bus.enqueue_worker.emit(request_id)
        self.wait_until(lambda: len(self.outbox.enqueued) > 0 and len(self.queue_spy) > before)
        return self.outbox.enqueued[-1].job

    def run_one_queue_tick(self) -> None:
        before = len(self.queue_spy)
        self.bus.tick_worker.emit()
        self.wait_until(lambda: len(self.queue_spy) > before)

    def wait_for_worker_completion(self) -> None:
        before = len(self._completion_spy)
        self.bus.complete_worker.emit()
        self.wait_until(lambda: len(self._completion_spy) > before)
        assert self.completion_probe.thread_ids[-1] == self.worker_thread_id

    def request_connection_test(self, credential: str | None = None) -> str:
        configuration = self.worker._configuration
        assert configuration is not None
        request_id = self.secret_vault.put_latest(
            ConnectionTestRequest(
                ORIGIN,
                credential or self.ephemeral_secret,
                configuration.generation,
            )
        )
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
        stopped = QSignalSpy(self.worker.stopped)
        destroyed = QSignalSpy(self.worker.destroyed)
        self.bus.stop_worker.emit()
        self.wait_until(lambda: len(stopped) == 1)
        self.wait_until(lambda: len(destroyed) == 1)
        self.wait_until(lambda: not self.thread.isRunning())
        assert self.thread.wait(2_000)


@pytest.fixture(scope='module')
def qcoreapplication() -> Generator[QCoreApplication]:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def worker_harness(
    tmp_path: Path, qcoreapplication: QCoreApplication
) -> Generator[WorkerHarness]:
    harness = WorkerHarness(tmp_path, qcoreapplication)
    yield harness
    harness.stop()


def test_opaque_vault_and_secret_request_repr_are_redacted() -> None:
    secret = secrets.token_urlsafe(32)
    vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
    request = ConnectionTestRequest(ORIGIN, secret, 1)
    request_id = vault.put(request)

    assert_secret_absent(secret, request)
    assert_secret_absent(secret, vault)
    assert vault.contains(request_id)
    assert vault.size() == 1
    assert vault.take(request_id) is request
    assert not vault.contains(request_id)
    with pytest.raises(KeyError):
        vault.take(request_id)


def test_opaque_vault_latest_generation_replaces_and_fences_consumed_values() -> None:
    first_secret = secrets.token_urlsafe(32)
    second_secret = secrets.token_urlsafe(32)
    vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
    first = ConnectionTestRequest(ORIGIN, first_secret, 1)
    second = ConnectionTestRequest(ORIGIN, second_secret, 2)

    first_id = vault.put_latest(first)
    assert vault.take_if_current(first_id) is first
    assert vault.is_current(first_id)

    second_id = vault.put_latest(second)
    assert not vault.is_current(first_id)
    assert vault.take_if_current(first_id) is None
    assert not vault.run_if_current(first_id, lambda: pytest.fail('stale action ran'))
    assert vault.size() == 1
    assert vault.take_if_current(second_id) is second
    assert vault.is_current(second_id)
    completed: list[str] = []
    assert vault.run_if_current(second_id, lambda: completed.append(second_id))
    assert completed == [second_id]

    vault.clear()
    assert not vault.is_current(second_id)
    assert_secret_absent(first_secret, vault)
    assert_secret_absent(second_secret, vault)


def test_configuration_fence_is_secret_free_and_revocation_is_monotonic() -> None:
    fence = ConfigurationFence()
    secret = secrets.token_urlsafe(32)

    first = fence.advance()
    assert fence.is_current(first)
    assert fence.authorizes(first)
    permit = fence.acquire(first)
    assert permit is not None
    assert fence.active_permits(first) == 1

    revoked = fence.revoke()

    assert revoked > first
    assert fence.is_current(revoked)
    assert not fence.authorizes(first)
    assert not fence.authorizes(revoked)
    assert fence.acquire(first) is None
    assert fence.active_permits(first) == 1
    permit.release()
    assert fence.active_permits(first) == 0
    current = fence.advance()
    assert current > revoked
    assert fence.authorizes(current)
    assert secret not in repr(fence)


def test_direct_wrong_thread_candidate_slot_erases_secret_without_io(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    candidate = secrets.token_urlsafe(32)
    request_id = worker_harness.secret_vault.put_latest(
        ConnectionTestRequest(ORIGIN, candidate, 1)
    )
    client_calls = tuple(worker_harness.client.calls)
    credential_calls = tuple(worker_harness.credentials.get_calls)

    worker_harness.worker.test_connection(request_id)

    assert not worker_harness.secret_vault.contains(request_id)
    assert tuple(worker_harness.client.calls) == client_calls
    assert tuple(worker_harness.credentials.get_calls) == credential_calls
    assert list(failed[-1]) == [
        request_id,
        public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
    ]
    assert_secret_absent(candidate, failed)


def test_every_external_slot_rejects_direct_wrong_thread_use_without_io(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    candidate = secrets.token_urlsafe(32)
    candidate_id = worker_harness.secret_vault.put_latest(
        ConnectionTestRequest(ORIGIN, candidate, 1)
    )
    profile_id = worker_harness.profile_vault.put(
        SavedProfileRequest(
            NAMESPACE,
            PROFILE_BYTES,
            ProfileData(roastUUID=str(ROAST_UUID)),
            NOW,
            False,
        )
    )
    command_ids = tuple(
        worker_harness.command_vault.put(value)
        for value in (
            RemoveCredentialRequest(ORIGIN),
            object(),
            object(),
            object(),
            object(),
            ProtectedPathsRequest(NAMESPACE, frozenset()),
            ClearUnusedRequest(NAMESPACE),
        )
    )
    configuration = WorkerConfiguration(
        origin=ORIGIN,
        namespace=NAMESPACE,
        enabled=True,
        automatic_upload=True,
        client_instance_uuid=CLIENT_UUID,
        cache_limit_bytes=64 * 1024 * 1024,
        generation=worker_harness.configuration_fence.advance(),
        identity=IDENTITY,
    )
    boundary_calls = (
        tuple(worker_harness.credentials.get_calls),
        tuple(worker_harness.credentials.set_calls),
        tuple(worker_harness.credentials.delete_calls),
        tuple(worker_harness.client.calls),
        tuple(worker_harness.outbox.resume_calls),
        tuple(worker_harness.outbox.leased),
        tuple(worker_harness.cache.publish_calls),
        tuple(worker_harness.cache.prune_calls),
        tuple(worker_harness.cache.clear_calls),
    )

    worker_harness.worker.start()
    worker_harness.worker.configure(configuration)
    worker_harness.worker.test_connection(candidate_id)
    worker_harness.worker.commit_connection('a' * 32)
    worker_harness.worker.finalize_connection('b' * 32)
    worker_harness.worker.acknowledge_connection_activation('a' * 32)
    worker_harness.worker.rollback_connection('c' * 32)
    worker_harness.worker.cancel_connection_transaction('f' * 32)
    worker_harness.worker.remove_credential(command_ids[0])
    worker_harness.worker.enqueue_saved(profile_id)
    worker_harness.worker.process_queue_once()
    worker_harness.worker.refresh()
    worker_harness.worker.retry_job('d' * 32)
    worker_harness.worker.remove_job('e' * 32)
    worker_harness.worker.browse(command_ids[1])
    worker_harness.worker.open_online(command_ids[2])
    worker_harness.worker.open_cached(command_ids[3])
    worker_harness.worker.publish_staged(command_ids[4])
    worker_harness.worker.discard_staged('/connector/generated/stage')
    worker_harness.worker.update_protected_paths(command_ids[5])
    worker_harness.worker.clear_unused(command_ids[6])
    worker_harness.worker.stop()

    assert len(failed) == 22
    assert not worker_harness.secret_vault.contains(candidate_id)
    assert not worker_harness.profile_vault.contains(profile_id)
    assert all(
        not worker_harness.command_vault.contains(request_id)
        for request_id in command_ids
    )
    assert boundary_calls == (
        tuple(worker_harness.credentials.get_calls),
        tuple(worker_harness.credentials.set_calls),
        tuple(worker_harness.credentials.delete_calls),
        tuple(worker_harness.client.calls),
        tuple(worker_harness.outbox.resume_calls),
        tuple(worker_harness.outbox.leased),
        tuple(worker_harness.cache.publish_calls),
        tuple(worker_harness.cache.prune_calls),
        tuple(worker_harness.cache.clear_calls),
    )
    assert not worker_harness.worker._stop_event.is_set()
    assert_secret_absent(candidate, failed)


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
    assert_secret_absent(worker_harness.ephemeral_secret, payload)
    assert signal_threads == [worker_harness.worker_thread_id]
    assert signal_threads[0] != worker_harness.ui_thread_id
    assert worker_harness.timer is not None
    assert worker_harness.timer.thread() is worker_harness.thread
    assert worker_harness.cache.constructor_thread == worker_harness.ui_thread_id
    assert worker_harness.cache.open_threads == [worker_harness.worker_thread_id]
    assert worker_harness.credentials.set_calls == []
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads) == 1
    assert worker_harness.client.enter_threads == worker_harness.client.exit_threads
    for root in (worker_harness.outbox.root, worker_harness.cache.root):
        for path in root.rglob('*'):
            if path.is_file():
                assert_secret_absent_from_file(worker_harness.ephemeral_secret, path)


def test_persisted_configuration_is_authenticated_before_queue_authorization(
    worker_harness: WorkerHarness,
) -> None:
    assert worker_harness.configuration_calls == (('test_connection',),)
    assert worker_harness.credentials.get_calls
    assert worker_harness.outbox.resume_calls
    assert worker_harness.client.enter_threads == worker_harness.client.exit_threads


def test_startup_identity_mismatch_never_resumes_or_delivers_namespace(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    harness = WorkerHarness(
        tmp_path,
        qcoreapplication,
        authenticated_identity=OTHER_IDENTITY,
    )
    try:
        assert harness.configuration_calls == (('test_connection',),)
        assert harness.outbox.resume_calls == []
        harness.enqueue_saved_profile()
        before = tuple(harness.client.calls)
        harness.run_one_queue_tick()
        assert tuple(harness.client.calls) == (*before, ('test_connection',))
        assert all(call[0] != 'post_aroast' for call in harness.client.calls)
        assert harness.outbox.counts(NAMESPACE).paused == 1
    finally:
        harness.stop()


def test_delivery_requires_the_exact_authenticated_configuration_fence(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()
    worker_harness.worker._authorized_target = None

    worker_harness.run_one_queue_tick()

    assert all(call[0] != 'post_aroast' for call in worker_harness.client.calls)
    assert all(call[0] != 'upload_revision' for call in worker_harness.client.calls)


def test_generation_revoked_during_keyring_read_never_installs_or_authenticates(
    worker_harness: WorkerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get = worker_harness.credentials.get
    resume_calls = tuple(worker_harness.outbox.resume_calls)
    factory_calls = tuple(worker_harness.client_factory.calls)

    def revoke_after_read(origin: str) -> str | None:
        credential = original_get(origin)
        worker_harness.configuration_fence.revoke()
        return credential

    monkeypatch.setattr(worker_harness.credentials, 'get', revoke_after_read)
    before = len(worker_harness.queue_spy)
    worker_harness.configure()
    worker_harness.wait_until(lambda: len(worker_harness.queue_spy) > before)

    assert tuple(worker_harness.client_factory.calls) == factory_calls
    assert tuple(worker_harness.outbox.resume_calls) == resume_calls
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    assert worker_harness.timer is not None
    assert not worker_harness.timer.logically_active


def test_revoked_generation_timer_entry_never_leases_or_calls_delivery_api(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()
    leased = tuple(worker_harness.outbox.leased)
    worker_harness.configuration_fence.revoke()
    before = tuple(worker_harness.client.calls)

    worker_harness.run_one_queue_tick()

    assert tuple(worker_harness.outbox.leased) == leased
    assert tuple(worker_harness.client.calls) == before
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    assert worker_harness.timer is not None
    assert not worker_harness.timer.logically_active


@pytest.mark.parametrize(
    'operation',
    [
        'install_authorization',
        'resume_namespace',
        'timer_start',
        'lease_next',
        'post_aroast',
        'upload_revision',
        'list_roasts',
        'get_roast',
        'download_revision',
    ],
)
def test_revocation_while_blocked_before_each_permit_has_zero_boundary_side_effect(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
    operation: str,
) -> None:
    blocker = PermitBoundaryBlocker()
    harness = WorkerHarness(
        tmp_path / operation,
        qcoreapplication,
        operation_hook=blocker.block,
    )

    def api_call_count(method: str) -> int:
        return sum(call[0] == method for call in harness.client.calls)

    resume_count = 0
    timer = cast(DeterministicTimer, harness.timer)
    delay_count = 0
    leased_count = 0
    post_count = 0
    upload_count = 0
    list_count = 0
    get_count = 0
    download_count = 0
    try:
        if operation in {'lease_next', 'post_aroast', 'upload_revision'}:
            harness.enqueue_saved_profile()
        blocker.select(operation)
        if operation in {'install_authorization', 'resume_namespace'}:
            resume_count = len(harness.outbox.resume_calls)
            harness.bus.configure_worker.emit(
                WorkerConfiguration(
                    origin=ORIGIN,
                    namespace=NAMESPACE,
                    enabled=True,
                    automatic_upload=True,
                    client_instance_uuid=CLIENT_UUID,
                    cache_limit_bytes=64 * 1024 * 1024,
                    generation=harness.configuration_fence.advance(),
                    identity=IDENTITY,
                )
            )
        elif operation == 'timer_start':
            delay_count = len(timer.delays)
            request_id = harness.profile_vault.put(
                SavedProfileRequest(
                    NAMESPACE,
                    PROFILE_BYTES,
                    ProfileData(roastUUID=str(ROAST_UUID)),
                    NOW,
                    False,
                )
            )
            harness.bus.enqueue_worker.emit(request_id)
        elif operation in {'lease_next', 'post_aroast', 'upload_revision'}:
            leased_count = len(harness.outbox.leased)
            post_count = api_call_count('post_aroast')
            upload_count = api_call_count('upload_revision')
            harness.bus.tick_worker.emit()
        elif operation == 'list_roasts':
            list_count = api_call_count('list_roasts')
            request_id = harness.command_vault.put(
                BrowseRequest(NAMESPACE, ArchiveFilters(), None, True)
            )
            harness.bus.browse_worker.emit(request_id)
        else:
            get_count = api_call_count('get_roast')
            download_count = api_call_count('download_revision')
            request_id = harness.command_vault.put(
                OnlineOpenRequest(NAMESPACE, ROAST_UUID)
            )
            harness.bus.online_worker.emit(request_id)

        assert blocker.entered.wait(timeout=2)
        revoked_generation = harness.configuration_fence.revoke()
        assert harness.configuration_fence.active_permits() == 0
        blocker.release.set()
        harness.wait_for_worker_completion()
        assert blocker.returned.is_set()

        assert harness.configuration_fence.is_current(revoked_generation)
        if operation == 'install_authorization':
            assert harness.worker._credential is None
            assert harness.worker._authorized_target is None
        elif operation == 'resume_namespace':
            assert len(harness.outbox.resume_calls) == resume_count
        elif operation == 'timer_start':
            assert len(timer.delays) == delay_count
        elif operation == 'lease_next':
            assert len(harness.outbox.leased) == leased_count
        elif operation == 'post_aroast':
            assert api_call_count('post_aroast') == post_count
        elif operation == 'upload_revision':
            assert api_call_count('post_aroast') == post_count + 1
            assert api_call_count('upload_revision') == upload_count
        elif operation == 'list_roasts':
            assert api_call_count('list_roasts') == list_count
        elif operation == 'get_roast':
            assert api_call_count('get_roast') == get_count
        else:
            assert api_call_count('get_roast') == get_count + 1
            assert api_call_count('download_revision') == download_count
    finally:
        blocker.release.set()
        harness.stop()


def test_revocation_before_startup_auth_permit_has_zero_http(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    blocker = PermitBoundaryBlocker()
    harness = WorkerHarness(
        tmp_path / 'authenticate-startup',
        qcoreapplication,
        operation_hook=blocker.block,
    )
    try:
        blocker.select('authenticate_startup')
        client_calls = tuple(harness.client.calls)
        factory_calls = tuple(harness.client_factory.calls)
        harness.bus.configure_worker.emit(
            WorkerConfiguration(
                origin=ORIGIN,
                namespace=NAMESPACE,
                enabled=True,
                automatic_upload=True,
                client_instance_uuid=CLIENT_UUID,
                cache_limit_bytes=64 * 1024 * 1024,
                generation=harness.configuration_fence.advance(),
                identity=IDENTITY,
            )
        )
        assert blocker.entered.wait(timeout=2)

        harness.configuration_fence.revoke()
        assert harness.configuration_fence.active_permits() == 0
        blocker.release.set()
        harness.wait_for_worker_completion()
        assert blocker.returned.is_set()

        assert tuple(harness.client_factory.calls) == factory_calls
        assert tuple(harness.client.calls) == client_calls
        assert harness.worker._credential is None
        assert harness.worker._authorized_target is None
    finally:
        blocker.release.set()
        harness.stop()


def test_cancel_before_candidate_auth_permit_has_zero_http_or_keyring(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    blocker = PermitBoundaryBlocker()
    harness = WorkerHarness(
        tmp_path / 'authenticate-candidate',
        qcoreapplication,
        operation_hook=blocker.block,
    )
    tested = QSignalSpy(harness.worker.connectionTested)
    try:
        blocker.select('authenticate_candidate')
        client_calls = tuple(harness.client.calls)
        factory_calls = tuple(harness.client_factory.calls)
        credential_calls = tuple(harness.credentials.get_calls)
        harness.request_connection_test(secrets.token_urlsafe(32))
        assert blocker.entered.wait(timeout=2)

        harness.secret_vault.clear()
        assert harness.configuration_fence.active_permits() == 0
        blocker.release.set()
        harness.wait_for_worker_completion()
        assert blocker.returned.is_set()

        assert tuple(harness.client_factory.calls) == factory_calls
        assert tuple(harness.client.calls) == client_calls
        assert tuple(harness.credentials.get_calls) == credential_calls
        assert len(tested) == 0
        assert harness.worker._credential_transactions == {}
    finally:
        blocker.release.set()
        harness.stop()


def test_revoke_before_final_auth_permit_has_zero_http_and_rolls_back(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    blocker = PermitBoundaryBlocker()
    harness = WorkerHarness(
        tmp_path / 'authenticate-activation',
        qcoreapplication,
        operation_hook=blocker.block,
    )
    activated = QSignalSpy(harness.worker.connectionActivated)
    candidate = secrets.token_urlsafe(32)
    old_digest = harness.credentials.stored_digest()
    try:
        transaction_id = harness.request_connection_test(candidate)
        harness.wait_until(
            lambda: transaction_id in harness.worker._credential_transactions
        )
        harness.bus.commit_worker.emit(transaction_id)
        harness.wait_until(
            lambda: harness.worker._credential_transactions[
                transaction_id
            ].keyring_committed
        )
        harness.bus.configure_worker.emit(
            WorkerConfiguration(
                origin=ORIGIN,
                namespace=NAMESPACE,
                enabled=False,
                automatic_upload=False,
                client_instance_uuid=CLIENT_UUID,
                cache_limit_bytes=64 * 1024 * 1024,
                generation=harness.configuration_fence.advance(),
                identity=IDENTITY,
                activation_id=transaction_id,
            )
        )
        harness.wait_until(
            lambda: harness.worker._configuration is not None
            and harness.worker._configuration.activation_id == transaction_id
        )
        blocker.select('authenticate_activation')
        client_calls = tuple(harness.client.calls)
        factory_calls = tuple(harness.client_factory.calls)
        credential_calls = tuple(harness.credentials.get_calls)
        harness.bus.finalize_worker.emit(transaction_id)
        assert blocker.entered.wait(timeout=2)
        assert tuple(harness.credentials.get_calls) == credential_calls

        harness.configuration_fence.revoke()
        harness.secret_vault.clear()
        assert harness.configuration_fence.active_permits() == 0
        blocker.release.set()
        harness.wait_until(
            lambda: transaction_id not in harness.worker._credential_transactions
        )

        assert tuple(harness.client_factory.calls) == factory_calls
        assert tuple(harness.client.calls) == client_calls
        assert len(activated) == 0
        assert harness.credentials.stored_digest() == old_digest
        assert harness.worker._credential is None
        assert harness.worker._authorized_target is None
    finally:
        blocker.release.set()
        harness.stop()


def test_revocation_before_activation_install_permit_has_zero_authorization(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    blocker = PermitBoundaryBlocker()
    harness = WorkerHarness(
        tmp_path / 'install-activation',
        qcoreapplication,
        operation_hook=blocker.block,
    )
    activated = QSignalSpy(harness.worker.connectionActivated)
    rolled_back = QSignalSpy(harness.worker.connectionRollbackFinished)
    candidate = secrets.token_urlsafe(32)
    old_digest = harness.credentials.stored_digest()
    try:
        transaction_id = harness.request_connection_test(candidate)
        harness.wait_until(
            lambda: transaction_id in harness.worker._credential_transactions
        )
        harness.bus.commit_worker.emit(transaction_id)
        harness.wait_until(
            lambda: harness.worker._credential_transactions[
                transaction_id
            ].keyring_committed
        )
        harness.bus.configure_worker.emit(
            WorkerConfiguration(
                origin=ORIGIN,
                namespace=NAMESPACE,
                enabled=False,
                automatic_upload=False,
                client_instance_uuid=CLIENT_UUID,
                cache_limit_bytes=64 * 1024 * 1024,
                generation=harness.configuration_fence.advance(),
                identity=IDENTITY,
                pending_connection=True,
                activation_id=transaction_id,
            )
        )
        harness.wait_until(
            lambda: harness.worker._configuration is not None
            and harness.worker._configuration.activation_id == transaction_id
        )
        blocker.select('install_activation')
        harness.bus.finalize_worker.emit(transaction_id)
        assert blocker.entered.wait(timeout=2)

        harness.configuration_fence.revoke()
        blocker.release.set()
        harness.wait_until(lambda: len(rolled_back) == 1)

        assert len(activated) == 0
        assert harness.worker._credential is None
        assert harness.worker._authorized_target is None
        assert harness.credentials.stored_digest() == old_digest
    finally:
        blocker.release.set()
        harness.stop()


def test_permit_acquired_before_revocation_is_bounded_in_flight(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()
    configuration = worker_harness.worker._configuration
    assert configuration is not None
    entered = threading.Event()
    release = threading.Event()

    def block_started_post(method: str) -> None:
        if method != 'post_aroast':
            return
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError('in-flight permit test timed out')

    worker_harness.client.callback = block_started_post
    worker_harness.bus.tick_worker.emit()
    assert entered.wait(timeout=2)

    assert worker_harness.configuration_fence.active_permits(
        configuration.generation
    ) == 1
    worker_harness.configuration_fence.revoke()
    assert worker_harness.configuration_fence.active_permits(
        configuration.generation
    ) == 1
    release.set()
    worker_harness.wait_until(
        lambda: worker_harness.configuration_fence.active_permits(
            configuration.generation
        )
        == 0
    )

    assert sum(call[0] == 'post_aroast' for call in worker_harness.client.calls) == 1
    assert all(call[0] != 'upload_revision' for call in worker_harness.client.calls)


def test_candidate_activation_commits_only_between_two_exact_auth_checks(
    worker_harness: WorkerHarness,
) -> None:
    tested = QSignalSpy(worker_harness.worker.connectionTested)
    committed = QSignalSpy(worker_harness.worker.credentialCommitted)
    activated = QSignalSpy(worker_harness.worker.connectionActivated)
    candidate = secrets.token_urlsafe(32)
    candidate_digest = secret_digest(candidate)

    transaction_id = worker_harness.request_connection_test(candidate)
    assert worker_harness.wait_for_spy(tested, 0) == [transaction_id, IDENTITY]
    assert all(call[1] != candidate_digest for call in worker_harness.credentials.set_calls)

    worker_harness.bus.commit_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(committed, 0) == [transaction_id, IDENTITY]
    assert worker_harness.credentials.set_calls[-1][:2] == (
        ORIGIN,
        candidate_digest,
    )
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )
    )
    worker_harness.bus.finalize_worker.emit(transaction_id)

    assert worker_harness.wait_for_spy(activated, 0) == [transaction_id, IDENTITY]
    assert worker_harness.client.calls == [
        ('test_connection',),
        ('test_connection',),
    ]
    assert tuple(worker_harness.worker._credential_transactions) == (transaction_id,)
    retained = worker_harness.worker._credential_transactions[transaction_id]
    assert retained.keyring_committed
    assert retained.activation_emitted

    worker_harness.bus.acknowledge_activation_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: transaction_id not in worker_harness.worker._credential_transactions
    )
    assert worker_harness.worker._credential == candidate
    assert worker_harness.worker._authorized_target == (ORIGIN, IDENTITY)
    signal_payloads = tuple(
        list(spy[index])
        for spy in (tested, committed, activated)
        for index in range(len(spy))
    )
    assert_secret_absent(candidate, signal_payloads)


def test_superseded_tests_bound_worker_transactions_and_cannot_commit_old_id(
    worker_harness: WorkerHarness,
) -> None:
    tested = QSignalSpy(worker_harness.worker.connectionTested)
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    candidates = [secrets.token_urlsafe(32) for _ in range(12)]
    request_ids: list[str] = []

    for index, candidate in enumerate(candidates):
        request_id = worker_harness.request_connection_test(candidate)
        request_ids.append(request_id)
        assert worker_harness.wait_for_spy(tested, index) == [request_id, IDENTITY]
        assert tuple(worker_harness.worker._credential_transactions) == (request_id,)
        assert worker_harness.secret_vault.size() == 0

    set_calls = tuple(worker_harness.credentials.set_calls)
    worker_harness.bus.commit_worker.emit(request_ids[0])
    assert worker_harness.wait_for_spy(failed, 0) == [
        request_ids[0],
        public_failure(FailureKind.KEYRING, retryable=False),
    ]

    assert tuple(worker_harness.credentials.set_calls) == set_calls
    assert tuple(worker_harness.worker._credential_transactions) == (
        request_ids[-1],
    )
    assert worker_harness.credentials.stored_digest() == secret_digest(
        worker_harness.ephemeral_secret
    )
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    for candidate in candidates:
        assert_secret_absent(candidate, worker_harness.worker)
        assert_secret_absent(candidate, tested)


def test_cancel_committed_transaction_restores_old_keyring_and_never_authorizes(
    worker_harness: WorkerHarness,
) -> None:
    committed = QSignalSpy(worker_harness.worker.credentialCommitted)
    candidate = secrets.token_urlsafe(32)
    old_digest = worker_harness.credentials.stored_digest()
    transaction_id = worker_harness.request_connection_test(candidate)
    worker_harness.wait_until(
        lambda: transaction_id in worker_harness.worker._credential_transactions
    )
    worker_harness.bus.commit_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(committed, 0) == [transaction_id, IDENTITY]

    worker_harness.bus.cancel_transaction_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: transaction_id not in worker_harness.worker._credential_transactions
        and worker_harness.credentials.stored_digest() == old_digest
    )

    assert worker_harness.credentials.set_calls[-1][1] == old_digest
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    assert_secret_absent(candidate, worker_harness.worker)
    assert_secret_absent(candidate, committed)


def test_cancel_first_committed_transaction_deletes_candidate_keyring(
    worker_harness: WorkerHarness,
) -> None:
    committed = QSignalSpy(worker_harness.worker.credentialCommitted)
    candidate = secrets.token_urlsafe(32)
    candidate_digest = secret_digest(candidate)
    worker_harness.credentials.remove_active()
    transaction_id = worker_harness.request_connection_test(candidate)
    worker_harness.wait_until(
        lambda: transaction_id in worker_harness.worker._credential_transactions
    )
    worker_harness.bus.commit_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(committed, 0) == [transaction_id, IDENTITY]

    worker_harness.bus.cancel_transaction_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: transaction_id not in worker_harness.worker._credential_transactions
        and bool(worker_harness.credentials.delete_calls)
    )

    assert worker_harness.credentials.set_calls[-1][1] == candidate_digest
    assert worker_harness.credentials.stored_digest() == secret_digest('')
    assert worker_harness.credentials.delete_calls[-1][0] == ORIGIN
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    assert_secret_absent(candidate, worker_harness.worker)
    assert_secret_absent(candidate, committed)


def test_synchronous_vault_cancel_during_final_auth_rolls_keyring_back(
    worker_harness: WorkerHarness,
) -> None:
    activated = QSignalSpy(worker_harness.worker.connectionActivated)
    candidate = secrets.token_urlsafe(32)
    old_digest = worker_harness.credentials.stored_digest()
    transaction_id = worker_harness.request_connection_test(candidate)
    worker_harness.wait_until(
        lambda: transaction_id in worker_harness.worker._credential_transactions
    )
    worker_harness.bus.commit_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: worker_harness.worker._credential_transactions[
            transaction_id
        ].keyring_committed
    )
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )
    )

    def cancel_during_auth(method: str) -> None:
        if method == 'test_connection':
            worker_harness.secret_vault.clear()

    worker_harness.client.callback = cancel_during_auth
    worker_harness.bus.finalize_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: transaction_id not in worker_harness.worker._credential_transactions
    )

    assert len(activated) == 0
    assert worker_harness.credentials.stored_digest() == old_digest
    assert worker_harness.credentials.set_calls[-1][1] == old_digest
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    assert_secret_absent(candidate, worker_harness.worker)


def test_cancel_delayed_completed_transaction_revokes_worker_authorization(
    worker_harness: WorkerHarness,
) -> None:
    activated = QSignalSpy(worker_harness.worker.connectionActivated)
    resume_calls = tuple(worker_harness.outbox.resume_calls)
    transaction_id = worker_harness.request_connection_test(
        secrets.token_urlsafe(32)
    )
    worker_harness.wait_until(
        lambda: transaction_id in worker_harness.worker._credential_transactions
    )
    worker_harness.bus.commit_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: worker_harness.worker._credential_transactions[
            transaction_id
        ].keyring_committed
    )
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )
    )
    worker_harness.bus.finalize_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(activated, 0) == [transaction_id, IDENTITY]
    assert worker_harness.worker._authorized_target == (ORIGIN, IDENTITY)

    worker_harness.bus.cancel_transaction_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: worker_harness.worker._authorized_target is None
    )

    assert worker_harness.worker._credential is None
    assert tuple(worker_harness.outbox.resume_calls) == resume_calls


def test_cancel_emitted_unacknowledged_activation_restores_keyring_and_auth(
    worker_harness: WorkerHarness,
) -> None:
    activated = QSignalSpy(worker_harness.worker.connectionActivated)
    candidate = secrets.token_urlsafe(32)
    old_digest = worker_harness.credentials.stored_digest()
    transaction_id = worker_harness.request_connection_test(candidate)
    worker_harness.wait_until(
        lambda: transaction_id in worker_harness.worker._credential_transactions
    )
    worker_harness.bus.commit_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: worker_harness.worker._credential_transactions[
            transaction_id
        ].keyring_committed
    )
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )
    )
    worker_harness.bus.finalize_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(activated, 0) == [transaction_id, IDENTITY]
    assert tuple(worker_harness.worker._credential_transactions) == (transaction_id,)

    worker_harness.bus.cancel_transaction_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: transaction_id not in worker_harness.worker._credential_transactions
    )

    assert worker_harness.credentials.stored_digest() == old_digest
    assert worker_harness.credentials.set_calls[-1][1] == old_digest
    assert worker_harness.worker._credential is None
    assert worker_harness.worker._authorized_target is None
    assert_secret_absent(candidate, worker_harness.worker)


def test_shutdown_preserves_emitted_unacknowledged_activation_for_recovery(
    worker_harness: WorkerHarness,
) -> None:
    activated = QSignalSpy(worker_harness.worker.connectionActivated)
    candidate = secrets.token_urlsafe(32)
    old_digest = worker_harness.credentials.stored_digest()
    transaction_id = worker_harness.request_connection_test(candidate)
    worker_harness.wait_until(
        lambda: transaction_id in worker_harness.worker._credential_transactions
    )
    worker_harness.bus.commit_worker.emit(transaction_id)
    worker_harness.wait_until(
        lambda: worker_harness.worker._credential_transactions[
            transaction_id
        ].keyring_committed
    )
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )
    )
    worker_harness.bus.finalize_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(activated, 0) == [transaction_id, IDENTITY]

    worker_harness.stop()

    assert worker_harness.credentials.stored_digest() == secret_digest(candidate)
    assert worker_harness.credentials.set_calls[-1][1] == secret_digest(candidate)
    assert worker_harness.credentials.stored_digest() != old_digest
    assert_secret_absent(candidate, worker_harness.credentials)


def test_final_auth_failure_rolls_keyring_back_to_old_credential(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    committed = QSignalSpy(worker_harness.worker.credentialCommitted)
    candidate = secrets.token_urlsafe(32)
    old_digest = worker_harness.credentials.stored_digest()
    transaction_id = worker_harness.request_connection_test(candidate)
    worker_harness.bus.commit_worker.emit(transaction_id)
    assert worker_harness.wait_for_spy(committed, 0) == [transaction_id, IDENTITY]
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            identity=IDENTITY,
            activation_id=transaction_id,
        )
    )
    worker_harness.client.identity = OTHER_IDENTITY

    worker_harness.bus.finalize_worker.emit(transaction_id)

    assert worker_harness.wait_for_spy(failed, 0) == [
        transaction_id,
        public_failure(FailureKind.CREDENTIAL_REJECTED, retryable=False),
    ]
    assert worker_harness.credentials.stored_digest() == old_digest
    assert worker_harness.credentials.set_calls[-1][1] == old_digest
    assert worker_harness.worker._credential_transactions == {}
    assert_secret_absent(candidate, failed)


def test_connection_writes_keyring_only_after_success_and_fixed_keyring_failure(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    tested = QSignalSpy(worker_harness.worker.connectionTested)
    worker_harness.client.failure_method = 'test_connection'
    worker_harness.client.failure = api_failure(401)

    first_candidate = secrets.token_urlsafe(32)
    first_id = worker_harness.request_connection_test(first_candidate)
    first_payload = worker_harness.wait_for_spy(failed, 0)
    assert first_payload == [first_id, public_failure(FailureKind.CREDENTIAL_REJECTED, retryable=False)]
    if any(
        call[1] == secret_digest(first_candidate)
        for call in worker_harness.credentials.set_calls
    ):
        pytest.fail('rejected runtime credential was persisted', pytrace=False)
    assert worker_harness.credentials.delete_calls == []

    worker_harness.client.failure_method = None
    worker_harness.credentials.failure = CredentialStoreError('backend leaked detail')
    second_candidate = secrets.token_urlsafe(32)
    second_id = worker_harness.request_connection_test(second_candidate)
    second_payload = worker_harness.wait_for_spy(failed, 1)
    assert len(tested) == 0
    assert second_payload == [second_id, public_failure(FailureKind.KEYRING, retryable=False)]
    assert 'backend leaked detail' not in repr(second_payload)
    assert_secret_absent(second_candidate, second_payload)
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads)


def test_ui_interruption_cancels_blocked_call_and_ignores_queued_backlog(
    worker_harness: WorkerHarness,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    tested = QSignalSpy(worker_harness.worker.connectionTested)
    staged = QSignalSpy(worker_harness.worker.downloadStaged)
    archived = QSignalSpy(worker_harness.worker.archivePageReady)
    stopped = QSignalSpy(worker_harness.worker.stopped)

    def block(method: str) -> None:
        if method == 'test_connection':
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('blocked fake timed out')

    blocked_candidate = secrets.token_urlsafe(32)
    queued_candidate = secrets.token_urlsafe(32)
    worker_harness.client.callback = block
    worker_harness.request_connection_test(blocked_candidate)
    assert entered.wait(timeout=2)

    configuration = worker_harness.worker._configuration
    assert configuration is not None
    followup_connection = worker_harness.secret_vault.put_latest(
        ConnectionTestRequest(
            ORIGIN,
            queued_candidate,
            configuration.generation,
        )
    )
    followup_profile = worker_harness.profile_vault.put(
        SavedProfileRequest(
            NAMESPACE,
            PROFILE_BYTES,
            ProfileData(roastUUID=str(ROAST_UUID)),
            NOW,
            False,
        )
    )
    browse_id = worker_harness.command_vault.put(
        BrowseRequest(NAMESPACE, ArchiveFilters(), None, True)
    )
    online_id = worker_harness.command_vault.put(
        OnlineOpenRequest(NAMESPACE, ROAST_UUID)
    )
    clear_id = worker_harness.command_vault.put(ClearUnusedRequest(NAMESPACE))
    worker_harness.bus.test_worker.emit(followup_connection)
    worker_harness.bus.enqueue_worker.emit(followup_profile)
    worker_harness.bus.browse_worker.emit(browse_id)
    worker_harness.bus.online_worker.emit(online_id)
    worker_harness.bus.clear_worker.emit(clear_id)
    worker_harness.thread.requestInterruption()
    worker_harness.bus.stop_worker.emit()
    release.set()

    worker_harness.wait_until(lambda: len(stopped) == 1)
    assert len(tested) == 0
    assert len(staged) == 0
    assert len(archived) == 0
    assert worker_harness.credentials.set_calls == []
    assert worker_harness.outbox.enqueued == []
    assert worker_harness.client.calls == [('test_connection',)]
    assert worker_harness.client.enter_threads == worker_harness.client.exit_threads
    assert len(worker_harness.client.exit_threads) == 1
    assert worker_harness.secret_vault.size() == 0
    assert worker_harness.profile_vault.size() == 0
    assert worker_harness.command_vault.size() == 0
    worker_harness.thread.quit()
    assert worker_harness.thread.wait(2_000)


def test_delivery_posts_aroast_before_exact_snapshot_upload(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    assert worker_harness.profile_vault.size() == 0
    assert PROFILE_BYTES.decode('utf-8') not in repr(worker_harness.worker)
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
    profile = ProfileData(roastUUID=str(ROAST_UUID), title='Worker roast')
    request_id = worker_harness.profile_vault.put(
        SavedProfileRequest(NAMESPACE, PROFILE_BYTES, profile, NOW, True)
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

    restarted = WorkerHarness(
        worker_harness.tmp_path, qcoreapplication, worker_harness.clock
    )
    try:
        assert restarted.timer is not None
        restarted.wait_until(lambda: bool(restarted.timer and restarted.timer.delays))
        assert restarted.timer.delays[-1][0] == 5_000
        worker_harness.clock.advance(5)
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


def test_real_snapshot_corruption_updates_failed_counts_and_stops_polling(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    assert job.snapshot_path is not None
    job.snapshot_path.unlink()
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    timer = worker_harness.timer
    assert timer is not None
    worker_harness.wait_until(lambda: len(timer.delays) > 0)
    delays_before = len(timer.delays)

    worker_harness.run_one_queue_tick()

    assert worker_harness.wait_for_spy(failed, 0) == [
        'queue',
        public_failure(FailureKind.LOCAL_PROFILE, retryable=False),
    ]
    counts = worker_harness.outbox.counts(NAMESPACE)
    assert counts.pending == 0 and counts.failed == 1
    failed_jobs = worker_harness.outbox.failed_jobs(NAMESPACE)
    assert len(failed_jobs) == 1 and failed_jobs[0].id == job.id
    assert worker_harness.client.calls == []
    assert len(timer.delays) == delays_before


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


def test_api_delivery_failure_logs_only_fixed_diagnostics(
    worker_harness: WorkerHarness, caplog: pytest.LogCaptureFixture
) -> None:
    worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'upload_revision'
    worker_harness.client.failure = ApiFailure(
        public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
        None,
        None,
    )

    with caplog.at_level(logging.ERROR, logger='artisanlib.roastserver.worker'):
        worker_harness.run_one_queue_tick()

    assert caplog.messages == [
        'Roast Server delivery API failure: kind=invalid_response '
        'code=invalid_response status=none'
    ]
    assert worker_harness.outbox.counts(NAMESPACE).failed == 1


def test_unexpected_delivery_failure_logs_only_exception_type(
    worker_harness: WorkerHarness, caplog: pytest.LogCaptureFixture
) -> None:
    worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'upload_revision'
    secret_detail = 'private-profile-diagnostic'
    worker_harness.client.failure = RuntimeError(secret_detail)

    with caplog.at_level(logging.ERROR, logger='artisanlib.roastserver.worker'):
        worker_harness.run_one_queue_tick()

    assert caplog.messages == [
        'Unexpected Roast Server delivery failure: RuntimeError'
    ]
    assert secret_detail not in caplog.text
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
    worker_harness.bus.tick_worker.emit()
    worker_harness.wait_until(lambda: len(worker_harness.client.exit_threads) == 1)

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

    worker_harness.credentials.remove_active()
    worker_harness.configure(enabled=True)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    worker_harness.credentials.restore_active()
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
        generation=worker_harness.configuration_fence.advance(),
    )
    worker_harness.bus.configure_worker.emit(invalid)
    payload = worker_harness.wait_for_spy(failed, 0)
    assert payload[0] == 'configure'
    assert cast(PublicFailure, payload[1]).kind is FailureKind.INVALID_RESPONSE
    assert all(call[0] != 'https://other.example.test' for call in worker_harness.credentials.get_calls)
    assert worker_harness.outbox.counts(NAMESPACE).paused == 1
    assert job.id


def test_configuration_rejects_missing_opaque_validation_correlation(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=True,
            automatic_upload=True,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=64 * 1024 * 1024,
            generation=worker_harness.configuration_fence.advance(),
            validation_id='',
            identity=IDENTITY,
        )
    )

    payload = worker_harness.wait_for_spy(failed, 0)
    assert payload == [
        'configure',
        public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
    ]


@pytest.mark.parametrize(
    'cache_limit_bytes',
    [MIN_CACHE_LIMIT_BYTES - 1, MAX_CACHE_LIMIT_BYTES + 1],
)
def test_configuration_rejects_cache_limits_outside_settings_bounds(
    worker_harness: WorkerHarness,
    cache_limit_bytes: int,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    worker_harness.bus.configure_worker.emit(
        WorkerConfiguration(
            origin=ORIGIN,
            namespace=NAMESPACE,
            enabled=True,
            automatic_upload=True,
            client_instance_uuid=CLIENT_UUID,
            cache_limit_bytes=cache_limit_bytes,
            generation=worker_harness.configuration_fence.advance(),
        )
    )

    payload = worker_harness.wait_for_spy(failed, 0)
    assert payload == [
        'configure',
        public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
    ]


def test_failed_job_retry_remove_and_immutable_aggregate_signals(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    worker_harness.client.failure_method = 'post_aroast'
    worker_harness.client.failure = api_failure(400)
    failed_jobs = QSignalSpy(worker_harness.worker.failedJobsChanged)
    worker_harness.run_one_queue_tick()
    worker_harness.wait_until(lambda: len(failed_jobs) > 0)

    failed_payload = cast(tuple[FailedJob, ...], failed_jobs[-1][0])
    assert len(failed_payload) == 1
    assert failed_payload[0].id == job.id
    assert_secret_absent(worker_harness.ephemeral_secret, failed_payload)

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
    finished_spy = QSignalSpy(worker_harness.worker.browseFinished)
    online_spy = QSignalSpy(worker_harness.worker.onlineChanged)
    filters = ArchiveFilters(search='Worker')
    first_id = worker_harness.command_vault.put(
        BrowseRequest(NAMESPACE, filters, None, True)
    )
    worker_harness.bus.browse_worker.emit(first_id)
    online_payload = worker_harness.wait_for_spy(page_spy, 0)

    assert online_payload == [first_id, worker_harness.client.page]
    assert worker_harness.wait_for_spy(finished_spy, 0) == [first_id]
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
    assert worker_harness.wait_for_spy(finished_spy, 1) == [second_id]
    assert worker_harness.wait_for_spy(failed, 0) == [second_id, worker_harness.client.failure.failure]
    assert online_spy[-1] == [False]
    assert len(worker_harness.client.enter_threads) == len(worker_harness.client.exit_threads)


def test_browse_rejects_unbounded_filters_and_cursor_before_api(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    calls_before = list(worker_harness.client.calls)
    requests = (
        BrowseRequest(
            NAMESPACE,
            ArchiveFilters(search='x' * 201),
            None,
            True,
        ),
        BrowseRequest(
            NAMESPACE,
            ArchiveFilters(),
            'c' * 513,
            False,
        ),
    )

    for expected_failures, request in enumerate(requests, start=1):
        request_id = worker_harness.command_vault.put(request)
        worker_harness.bus.browse_worker.emit(request_id)
        worker_harness.wait_until(
            lambda expected=expected_failures: len(failed) >= expected  # type: ignore[misc]
        )

    assert worker_harness.client.calls == calls_before
    assert [list(failed[index])[1] for index in range(len(failed))] == [
        public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
        public_failure(FailureKind.INVALID_RESPONSE, retryable=False),
    ]


def test_retryable_download_failure_offers_only_exact_verified_cached_revision(
    worker_harness: WorkerHarness,
) -> None:
    published = QSignalSpy(worker_harness.worker.cachePublished)
    _online_id, publish_request = worker_harness.open_online()
    publish_id = worker_harness.command_vault.put(publish_request)
    worker_harness.bus.publish_worker.emit(publish_id)
    cached = cast(CachedRevision, worker_harness.wait_for_spy(published, 0)[1])
    fallback = QSignalSpy(worker_harness.worker.cachedFallbackReady)
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    worker_harness.client.failure_method = 'download_revision'
    worker_harness.client.failure = api_failure(503)
    request_id = worker_harness.command_vault.put(
        OnlineOpenRequest(NAMESPACE, ROAST_UUID)
    )

    worker_harness.bus.online_worker.emit(request_id)

    assert worker_harness.wait_for_spy(failed, 0) == [
        request_id,
        worker_harness.client.failure.failure,
    ]
    assert worker_harness.wait_for_spy(fallback, 0) == [request_id, cached]
    assert len(worker_harness.cache.discard_calls) == 1
    assert not worker_harness.cache.discard_calls[-1][0].exists()

    cached.path.write_bytes(b'corrupt cached profile')
    second_id = worker_harness.command_vault.put(
        OnlineOpenRequest(NAMESPACE, ROAST_UUID)
    )
    worker_harness.bus.online_worker.emit(second_id)
    worker_harness.wait_for_spy(failed, 1)
    worker_harness.wait_until(lambda: len(worker_harness.cache.discard_calls) == 2)
    assert len(fallback) == 1


def test_retryable_detail_failure_revalidates_last_known_current_cache_fallback(
    worker_harness: WorkerHarness,
) -> None:
    published = QSignalSpy(worker_harness.worker.cachePublished)
    _online_id, publish_request = worker_harness.open_online()
    publish_id = worker_harness.command_vault.put(publish_request)
    worker_harness.bus.publish_worker.emit(publish_id)
    cached = cast(CachedRevision, worker_harness.wait_for_spy(published, 0)[1])
    fallback = QSignalSpy(worker_harness.worker.cachedFallbackReady)
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    worker_harness.client.failure_method = 'get_roast'
    worker_harness.client.failure = api_failure(503)
    request_id = worker_harness.command_vault.put(
        OnlineOpenRequest(NAMESPACE, ROAST_UUID, cached)
    )

    worker_harness.bus.online_worker.emit(request_id)

    worker_harness.wait_for_spy(failed, 0)
    assert worker_harness.wait_for_spy(fallback, 0) == [request_id, cached]


def test_cancel_fence_after_validation_before_publish_discards_only_exact_stage(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    blocker = PermitBoundaryBlocker()
    harness = WorkerHarness(
        tmp_path,
        qcoreapplication,
        operation_hook=blocker.block,
    )
    published = QSignalSpy(harness.worker.cachePublished)
    try:
        _first_id, first = harness.open_online()
        _second_id, second = harness.open_online()
        blocker.select('publish_staged')
        first_publish_id = harness.command_vault.put(first)
        harness.bus.publish_worker.emit(first_publish_id)
        assert blocker.entered.wait(timeout=2)

        first.token.cancel()
        blocker.release.set()
        harness.wait_until(
            lambda: any(path == first.staged_path for path, _thread in harness.cache.discard_calls)
        )

        assert first.token.is_cancelled()
        assert len(published) == 0
        assert not first.staged_path.exists()
        assert second.staged_path.exists()

        second_publish_id = harness.command_vault.put(second)
        harness.bus.publish_worker.emit(second_publish_id)
        payload = harness.wait_for_spy(published, 0)
        assert payload[0] == second_publish_id
        assert cast(CachedRevision, payload[1]).path.exists()
    finally:
        blocker.release.set()
        harness.stop()


def test_cancel_after_publication_linearizes_preserves_cache_and_skips_prune(
    worker_harness: WorkerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original_publish = worker_harness.cache.publish

    def blocking_publish(
        namespace: Namespace,
        detail: RoastDetail,
        receipt: DownloadReceipt,
        staged_path: Path,
        validated_at: datetime,
    ) -> CachedRevision:
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError('blocked cache publication timed out')
        return original_publish(
            namespace,
            detail,
            receipt,
            staged_path,
            validated_at,
        )

    monkeypatch.setattr(worker_harness.cache, 'publish', blocking_publish)
    published = QSignalSpy(worker_harness.worker.cachePublished)
    _open_id, request = worker_harness.open_online()
    publish_id = worker_harness.command_vault.put(request)
    worker_harness.bus.publish_worker.emit(publish_id)
    assert entered.wait(timeout=2)

    request.token.cancel()
    release.set()
    payload = worker_harness.wait_for_spy(published, 0)

    cached = cast(CachedRevision, payload[1])
    assert cached.path.exists()
    assert cached.path.read_bytes() == PROFILE_BYTES
    assert worker_harness.cache.prune_calls == []


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


@pytest.mark.parametrize('enabled', [False, True], ids=['disable', 'switch'])
def test_configuration_transition_discards_and_fences_downloaded_stage(
    worker_harness: WorkerHarness,
    enabled: bool,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    published = QSignalSpy(worker_harness.worker.cachePublished)
    _online_id, request = worker_harness.open_online()
    assert request.staged_path.exists()
    other_namespace = namespace_for(
        ORIGIN, UUID('55555555-5555-4555-8555-555555555555')
    )

    worker_harness.configure(
        namespace=other_namespace if enabled else NAMESPACE,
        enabled=enabled,
    )

    assert not request.staged_path.exists()
    assert worker_harness.cache.discard_calls[-1][0] == request.staged_path
    publish_id = worker_harness.command_vault.put(request)
    worker_harness.bus.publish_worker.emit(publish_id)
    payload = worker_harness.wait_for_spy(failed, 0)
    assert payload == [publish_id, public_failure(FailureKind.CACHE_CORRUPT, retryable=False)]
    assert len(published) == 0


def test_unknown_and_malformed_publish_ids_do_not_consume_pending_stages(
    worker_harness: WorkerHarness,
) -> None:
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    _first_id, first = worker_harness.open_online()
    _second_id, second = worker_harness.open_online()

    worker_harness.bus.publish_worker.emit('f' * 32)
    worker_harness.wait_for_spy(failed, 0)
    malformed_id = worker_harness.command_vault.put(object())
    worker_harness.bus.publish_worker.emit(malformed_id)
    worker_harness.wait_for_spy(failed, 1)

    assert first.staged_path.exists()
    assert second.staged_path.exists()
    assert worker_harness.cache.discard_calls == []


def test_delayed_duplicate_publish_and_discard_affect_only_the_exact_stage(
    worker_harness: WorkerHarness,
) -> None:
    published = QSignalSpy(worker_harness.worker.cachePublished)
    failed = QSignalSpy(worker_harness.worker.operationFailed)
    _first_id, first = worker_harness.open_online()
    _second_id, second = worker_harness.open_online()
    first_publish_id = worker_harness.command_vault.put(first)
    worker_harness.bus.publish_worker.emit(first_publish_id)
    worker_harness.wait_for_spy(published, 0)

    duplicate_publish_id = worker_harness.command_vault.put(first)
    worker_harness.bus.publish_worker.emit(duplicate_publish_id)
    worker_harness.wait_for_spy(failed, 0)
    assert second.staged_path.exists()

    before = len(worker_harness.cache.discard_calls)
    worker_harness.bus.discard_worker.emit(str(second.staged_path))
    worker_harness.wait_until(lambda: len(worker_harness.cache.discard_calls) > before)
    worker_harness.bus.discard_worker.emit(str(second.staged_path))
    worker_harness.wait_for_spy(failed, 1)

    assert len(worker_harness.cache.discard_calls) == before + 1
    assert not second.staged_path.exists()


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


def test_publication_prune_always_uses_latest_queued_protected_paths(
    worker_harness: WorkerHarness,
) -> None:
    open_path = worker_harness.tmp_path / 'still-open-current.alog'
    open_path.write_bytes(b'open')
    protect_id = worker_harness.command_vault.put(
        ProtectedPathsRequest(NAMESPACE, frozenset({open_path}))
    )
    worker_harness.bus.protect_worker.emit(protect_id)
    _online_id, request = worker_harness.open_online()
    publish_id = worker_harness.command_vault.put(request)

    worker_harness.bus.publish_worker.emit(publish_id)
    worker_harness.wait_until(lambda: bool(worker_harness.cache.prune_calls))

    namespace, protected, thread_id = worker_harness.cache.prune_calls[-1]
    assert namespace == NAMESPACE
    assert protected == frozenset({open_path})
    assert thread_id == worker_harness.worker_thread_id


def test_publication_prune_always_unions_synchronous_registry_snapshot(
    worker_harness: WorkerHarness,
) -> None:
    open_path = worker_harness.tmp_path / 'registry-open.alog'
    open_path.write_bytes(b'open')
    worker_harness.worker._protection_registry.protect(NAMESPACE, open_path)
    _online_id, request = worker_harness.open_online()
    publish_id = worker_harness.command_vault.put(request)

    worker_harness.bus.publish_worker.emit(publish_id)
    worker_harness.wait_until(lambda: bool(worker_harness.cache.prune_calls))

    _namespace, protected, _thread_id = worker_harness.cache.prune_calls[-1]
    assert protected == frozenset({open_path})


def test_worker_holds_registry_guard_through_prune_against_token_transition(
    worker_harness: WorkerHarness,
) -> None:
    first_path = worker_harness.tmp_path / 'first-protected.alog'
    second_path = worker_harness.tmp_path / 'second-protected.alog'
    first_path.write_bytes(b'first')
    second_path.write_bytes(b'second')
    registry = worker_harness.worker._protection_registry
    first = registry.protect(NAMESPACE, first_path)
    prune_entered = threading.Event()
    allow_prune = threading.Event()
    transition_finished = threading.Event()
    _online_id, request = worker_harness.open_online()
    worker_harness.cache.prune_entered = prune_entered
    worker_harness.cache.allow_prune = allow_prune
    publish_id = worker_harness.command_vault.put(request)

    worker_harness.bus.publish_worker.emit(publish_id)
    assert prune_entered.wait(2)

    def transition() -> None:
        second = registry.protect(NAMESPACE, second_path, expected=first)
        assert registry.release(second)
        transition_finished.set()

    transition_thread = threading.Thread(target=transition)
    transition_thread.start()
    try:
        time.sleep(0.02)
        assert not transition_finished.is_set()
    finally:
        allow_prune.set()
        transition_thread.join(2)
    worker_harness.wait_until(transition_finished.is_set)

    assert worker_harness.cache.prune_calls[-1][1] == frozenset({first_path})
    assert registry.current() is None


def test_clear_unused_unions_open_and_outbox_protected_paths(
    worker_harness: WorkerHarness,
) -> None:
    job = worker_harness.enqueue_saved_profile()
    assert job.snapshot_path is not None
    open_path = worker_harness.tmp_path / 'currently-open.alog'
    open_path.write_bytes(b'open')
    stats = QSignalSpy(worker_harness.worker.cacheStatsChanged)
    protect_id = worker_harness.command_vault.put(
        ProtectedPathsRequest(NAMESPACE, frozenset({open_path}))
    )
    worker_harness.bus.protect_worker.emit(protect_id)
    request_id = worker_harness.command_vault.put(ClearUnusedRequest(NAMESPACE))

    worker_harness.bus.clear_worker.emit(request_id)
    worker_harness.wait_until(
        lambda: len(stats) > 0 and len(worker_harness.cache.clear_calls) > 0
    )

    namespace, protected, thread_id = worker_harness.cache.clear_calls[-1]
    assert namespace == NAMESPACE
    assert protected == frozenset({open_path, job.snapshot_path})
    assert thread_id == worker_harness.worker_thread_id


def test_stop_stops_timer_then_closes_all_stages_and_sqlite_on_worker_thread(
    worker_harness: WorkerHarness,
) -> None:
    worker_harness.enqueue_saved_profile()
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


def test_stock_qtimer_automatically_delivers_persisted_retry_and_destroys_in_affinity(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    clock = WallClock()
    root = tmp_path / 'outbox'
    setup = Outbox(root, clock)
    setup.open()
    first_source = tmp_path / 'first.alog'
    first_source.write_bytes(PROFILE_BYTES)
    first_profile = ProfileData(roastUUID=str(ROAST_UUID), title='Worker roast')
    first_snapshot = setup.snapshot_saved_file(NAMESPACE, first_source)
    first = setup.enqueue(
        NAMESPACE,
        first_snapshot,
        ROAST_UUID,
        project_profile(first_profile, first_snapshot.source_modified_at),
        CLIENT_UUID,
    ).job
    second_uuid = UUID('66666666-6666-4666-8666-666666666666')
    second_profile = ProfileData(roastUUID=str(second_uuid), title='Later roast')
    second_source = tmp_path / 'second.alog'
    second_source.write_bytes(repr(second_profile).encode('utf-8'))
    second_snapshot = setup.snapshot_saved_file(NAMESPACE, second_source)
    second = setup.enqueue(
        NAMESPACE,
        second_snapshot,
        second_uuid,
        project_profile(second_profile, second_snapshot.source_modified_at),
        CLIENT_UUID,
    ).job
    retry_failure = public_failure(FailureKind.OFFLINE, retryable=True)
    leased_first = setup.lease_next(NAMESPACE, clock())
    assert isinstance(leased_first, Job) and leased_first.lease_token is not None
    first_due = clock() + timedelta(seconds=6)
    setup.mark_retry(
        first.id,
        leased_first.lease_token,
        clock(),
        first_due,
        retry_failure,
    )
    leased_second = setup.lease_next(NAMESPACE, clock())
    assert isinstance(leased_second, Job) and leased_second.lease_token is not None
    setup.mark_retry(
        second.id,
        leased_second.lease_token,
        clock(),
        clock() + timedelta(seconds=12),
        retry_failure,
    )
    setup.close()

    secret = secrets.token_urlsafe(32)
    credentials = FakeCredentialStore(lambda: secret)
    client = FakeClient()
    outbox = RecordingOutbox(root, clock)
    cache = RecordingCache(tmp_path / 'cache')
    credential_vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
    profile_vault: OpaqueVault[SavedProfileRequest] = OpaqueVault()
    command_vault: OpaqueVault[object] = OpaqueVault()
    configuration_fence = ConfigurationFence()
    worker = RoastServerWorker(
        outbox=outbox,
        cache=cache,
        credentials=credentials,
        client_factory=cast(ClientFactory, FakeClientFactory(client)),
        clock=clock,
        credential_vault=credential_vault,
        profile_vault=profile_vault,
        command_vault=command_vault,
        configuration_fence=configuration_fence,
    )
    thread = QThread()
    bus = CommandBus()
    probe = StockTimerProbe(worker, outbox, clock)
    worker.moveToThread(thread)
    probe.moveToThread(thread)
    bus.configure_worker.connect(worker.configure)
    bus.probe_worker.connect(probe.capture)
    bus.stop_worker.connect(worker.stop)
    worker.stopped.connect(worker.deleteLater)
    worker.stopped.connect(probe.deleteLater)
    probe.destroyed.connect(thread.quit)
    thread.started.connect(worker.start)
    changed = QSignalSpy(worker.queueChanged)
    stopped = QSignalSpy(worker.stopped)
    destroyed = QSignalSpy(worker.destroyed)
    probe_destroyed = QSignalSpy(probe.destroyed)
    captured = QSignalSpy(probe.captured)
    cache.timer_stopped = lambda: worker._timer is not None and not worker._timer.isActive()

    def wait_until(predicate: Callable[[], bool], message: str, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            qcoreapplication.processEvents()
            if time.monotonic() >= deadline:
                pytest.fail(message)
            time.sleep(0.001)
        qcoreapplication.processEvents()

    timer_destroyed: QSignalSpy | None = None
    try:
        thread.start()
        bus.configure_worker.emit(
            WorkerConfiguration(
                origin=ORIGIN,
                namespace=NAMESPACE,
                enabled=True,
                automatic_upload=True,
                client_instance_uuid=CLIENT_UUID,
                cache_limit_bytes=MIN_CACHE_LIMIT_BYTES,
                generation=configuration_fence.advance(),
                identity=IDENTITY,
            )
        )
        wait_until(lambda: len(changed) > 0, 'stock timer worker did not configure')
        # Probe well after scheduling so the test stays load-safe and does not
        # depend on a narrow remaining-time/window assertion.
        time.sleep(2.0)
        bus.probe_worker.emit()
        wait_until(lambda: len(captured) == 1, 'stock timer probe did not run')
        assert probe.observation is not None and probe.timer is not None
        (
            interval,
            active,
            due,
            scheduled_at,
            probe_thread,
            timer_parented,
            child_types,
        ) = probe.observation
        expected_interval = math.ceil(
            max(0.0, (first_due - scheduled_at).total_seconds()) * 1_000
        )
        assert due == first_due
        assert interval == expected_interval
        assert active
        assert probe_thread == cache.open_threads[0]
        assert timer_parented
        assert child_types == ('QTimer',)
        timer_destroyed = QSignalSpy(probe.timer.destroyed)

        wait_until(
            lambda: any(
                isinstance(changed[index][0], QueueCounts)
                and changed[index][0].complete == 1
                for index in range(len(changed))
            ),
            'stock timer did not deliver persisted retry within generous bound',
        )
        bus.stop_worker.emit()
        wait_until(lambda: len(stopped) == 1, 'stock timer worker did not stop')
        wait_until(lambda: len(destroyed) == 1, 'stock timer worker was not destroyed')
        wait_until(lambda: len(probe_destroyed) == 1, 'stock timer probe was not destroyed')
        wait_until(lambda: not thread.isRunning(), 'stock timer thread did not quit')
    finally:
        if thread.isRunning() and len(stopped) == 0:
            bus.stop_worker.emit()
        if thread.isRunning():
            deadline = time.monotonic() + 8
            while (
                thread.isRunning()
                and (len(destroyed) == 0 or len(probe_destroyed) == 0)
                and time.monotonic() < deadline
            ):
                qcoreapplication.processEvents()
                time.sleep(0.001)
        if thread.isRunning():
            thread.quit()
        assert thread.wait(8_000)

    assert len(destroyed) == 1
    assert len(probe_destroyed) == 1
    assert len(timer_destroyed) == 1
    assert thread.children() == []
    assert outbox.complete_calls[-1][0] == first.id
    assert outbox.complete_calls[-1][3] == cache.open_threads[0]
    assert client.enter_threads == client.exit_threads == [
        cache.open_threads[0],
        cache.open_threads[0],
    ]
    assert outbox._connection is None
    reopened = Outbox(root, clock)
    reopened.open()
    try:
        persisted = reopened.counts(NAMESPACE)
        assert persisted.complete == 1 and persisted.retrying == 1
    finally:
        reopened.close()
    assert cache.timer_was_stopped_on_close == [True]
    assert_secret_absent(secret, changed)


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
