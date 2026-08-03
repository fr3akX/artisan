#
# ABOUT
# Tests for the Artisan Roast Server main-thread controller
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
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, IO, cast, override
from uuid import UUID

from PyQt6.QtCore import QByteArray, QCoreApplication, QObject, QSettings, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtTest import QSignalSpy
import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ApiFailure, ClientFactory, DownloadReceipt
from artisanlib.roastserver.cache import CacheStats, CachedPage, CachedRevision, CacheStore
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    ArchiveFilters,
    FailureKind,
    IdentityOrganization,
    IdentityUser,
    Namespace,
    PublicFailure,
    Revision,
    RoastDetail,
    RoastDetailLinks,
    RoastPage,
    RoastSummary,
    ServerIdentity,
    ServerProfileSource,
)
from artisanlib.roastserver.controller import (
    ArchivePageView,
    ControllerError,
    RoastServerController,
)
from artisanlib.roastserver.metadata import project_profile
from artisanlib.roastserver.outbox import FailedJob, Job, Outbox, QueueCounts
from artisanlib.roastserver.settings import (
    SETTINGS_FAILURE_MESSAGE,
    ConnectorSettings,
    CredentialStoreError,
    SettingsError,
    SettingsStore,
    namespace_for,
)
from artisanlib.roastserver.worker import (
    BrowseRequest,
    ClearUnusedRequest,
    ConnectionTestRequest,
    OnlineOpenRequest,
    OpaqueVault,
    PendingConnectionRecovery,
    ProtectedPathsRequest,
    PublishRequest,
    SavedProfileRequest,
    WorkerConfiguration,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
ORIGIN = 'https://example.test'
OTHER_ORIGIN = 'https://other.example.test'
ORGANIZATION_ID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_ORGANIZATION_ID = UUID('22222222-2222-4222-8222-222222222222')
USER_ID = UUID('33333333-3333-4333-8333-333333333333')
ROAST_UUID = UUID('44444444-4444-4444-8444-444444444444')
IDENTITY = ServerIdentity(
    user=IdentityUser(USER_ID, 'owner@example.test', 'Owner'),
    organization=IdentityOrganization(ORGANIZATION_ID, 'Roastery', 'roastery'),
    role='admin',
)
OTHER_IDENTITY = ServerIdentity(
    user=IDENTITY.user,
    organization=IdentityOrganization(
        OTHER_ORGANIZATION_ID, 'Other Roastery', 'other-roastery'
    ),
    role='member',
)
PROFILE_BYTES = repr({'roastUUID': str(ROAST_UUID), 'title': 'Controller roast'}).encode(
    'utf-8'
)


def public_failure(kind: FailureKind) -> PublicFailure:
    return PublicFailure(
        kind=kind,
        code=kind.value,
        message=FAILURE_MESSAGES[kind],
        retryable=kind in {FailureKind.OFFLINE, FailureKind.RATE_LIMITED},
    )


def assert_secret_absent(secret: str, value: object) -> None:
    if secret in repr(value):
        pytest.fail('runtime secret exposed by controller value', pytrace=False)


def revision_for(content: bytes = PROFILE_BYTES, *, number: int = 1) -> Revision:
    return Revision(
        revision_number=number,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        parser_version='controller-test',
        parse_state='parsed',
        parse_diagnostic_code=None,
        parse_diagnostic_message=None,
        uploaded_at=NOW,
        metadata=(),
        reparse_recommended=False,
    )


def summary_for(content: bytes = PROFILE_BYTES, *, number: int = 1) -> RoastSummary:
    revision = revision_for(content, number=number)
    return RoastSummary(
        roast_uuid=ROAST_UUID,
        state='parsed',
        roast_at=NOW,
        title='Controller roast',
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


def detail_for(content: bytes = PROFILE_BYTES, *, number: int = 1) -> RoastDetail:
    summary = summary_for(content, number=number)
    revision = revision_for(content, number=number)
    return RoastDetail(
        roast_uuid=summary.roast_uuid,
        state=summary.state,
        roast_at=summary.roast_at,
        title=summary.title,
        batch_prefix=summary.batch_prefix,
        batch_number=summary.batch_number,
        batch_position=summary.batch_position,
        operator=summary.operator,
        machine=summary.machine,
        machine_setup=summary.machine_setup,
        temperature_unit=summary.temperature_unit,
        duration_seconds=summary.duration_seconds,
        green_weight_kg=summary.green_weight_kg,
        roasted_weight_kg=summary.roasted_weight_kg,
        revision_count=summary.revision_count,
        updated_at=summary.updated_at,
        labels=summary.labels,
        current_metadata=(),
        current_revision=revision,
        links=RoastDetailLinks(
            self_path=f'/api/v1/roasts/{ROAST_UUID.hex}',
            chart=f'/api/v1/roasts/{ROAST_UUID.hex}/chart',
            revisions=f'/api/v1/roasts/{ROAST_UUID.hex}/revisions',
        ),
    )


def cached_revision(path: Path, *, organization_id: UUID = ORGANIZATION_ID) -> CachedRevision:
    return CachedRevision(
        namespace=namespace_for(ORIGIN, organization_id),
        roast=summary_for(),
        revision=revision_for(),
        path=path,
        sidecar_path=path.with_suffix('.json'),
        downloaded_at=NOW,
    )


def publish_request(
    path: Path,
    *,
    content: bytes = PROFILE_BYTES,
    number: int = 1,
) -> PublishRequest:
    detail = detail_for(content, number=number)
    revision = cast(Revision, detail.current_revision)
    return PublishRequest(
        detail=detail,
        receipt=DownloadReceipt(
            roast_uuid=detail.roast_uuid,
            revision_number=revision.revision_number,
            sha256=revision.sha256,
            byte_count=revision.byte_size,
            filename=f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog',
        ),
        staged_path=path,
    )


class RestartAuthenticationRecorder:
    def __init__(
        self,
        credential_a: str,
        credential_b: str,
        *,
        offline: bool = False,
    ) -> None:
        self._credential_a = credential_a
        self._credential_b = credential_b
        self._offline = offline
        self.client_origins: list[str] = []
        self.authenticated_organizations: list[UUID | None] = []
        self.upload_calls = 0

    @override
    def __repr__(self) -> str:
        return '<RestartAuthenticationRecorder credentials=<redacted>>'

    def __call__(self, origin: str, credential: str) -> RestartAuthenticationClient:
        self.client_origins.append(origin)
        if self._offline:
            identity = None
        elif credential == self._credential_a:
            identity = IDENTITY
        elif credential == self._credential_b:
            identity = OTHER_IDENTITY
        else:
            identity = None
        return RestartAuthenticationClient(self, identity)


class RestartAuthenticationClient:
    def __init__(
        self,
        recorder: RestartAuthenticationRecorder,
        identity: ServerIdentity | None,
    ) -> None:
        self._recorder = recorder
        self._identity = identity

    def __enter__(self) -> RestartAuthenticationClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        organization_id = (
            None if self._identity is None else self._identity.organization.id
        )
        self._recorder.authenticated_organizations.append(organization_id)
        if self._identity is None:
            raise ApiFailure(public_failure(FailureKind.OFFLINE), None, None)
        return self._identity

    def post_aroast(self, *_args: object, **_kwargs: object) -> None:
        self._recorder.upload_calls += 1

    def upload_revision(self, *_args: object, **_kwargs: object) -> object:
        self._recorder.upload_calls += 1
        return object()


class DigestAuthenticationRecorder:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.digests: list[str] = []
        self.tested_ids: list[str] = []

    @override
    def __repr__(self) -> str:
        return '<DigestAuthenticationRecorder credentials=<redacted>>'

    def __call__(self, _origin: str, credential: str) -> DigestAuthenticationClient:
        with self._condition:
            self.digests.append(hashlib.sha256(credential.encode('utf-8')).hexdigest())
        return DigestAuthenticationClient()

    def wait_for_tests(self, count: int, timeout: float = 2) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.tested_ids) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    raise AssertionError('worker did not consume delayed-UI test')

    def record_tested(self, request_id: str, _identity: object) -> None:
        with self._condition:
            self.tested_ids.append(request_id)
            self._condition.notify_all()


class DigestAuthenticationClient:
    def __enter__(self) -> DigestAuthenticationClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        return IDENTITY


class SupersessionAuthenticationRecorder:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._factory_calls = 0
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.http_digests: list[str] = []
        self.delivery_calls: list[str] = []
        self.tested_ids: list[str] = []

    @override
    def __repr__(self) -> str:
        return '<SupersessionAuthenticationRecorder credentials=<redacted>>'

    def __call__(
        self, _origin: str, credential: str
    ) -> SupersessionAuthenticationClient:
        digest = hashlib.sha256(credential.encode('utf-8')).hexdigest()
        with self._condition:
            block = self._factory_calls == 0
            self._factory_calls += 1
        return SupersessionAuthenticationClient(self, digest, block)

    def record_http(self, digest: str, block: bool) -> None:
        with self._condition:
            self.http_digests.append(digest)
            self._condition.notify_all()
        if block:
            self.first_entered.set()
            if not self.release_first.wait(timeout=5):
                raise RuntimeError('blocked supersession authentication timed out')

    def record_tested(self, request_id: str, _identity: object) -> None:
        with self._condition:
            self.tested_ids.append(request_id)
            self._condition.notify_all()

    def wait_for_tested(self, count: int, timeout: float = 2) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.tested_ids) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    raise AssertionError('worker did not complete current authentication')


class SupersessionAuthenticationClient:
    def __init__(
        self,
        recorder: SupersessionAuthenticationRecorder,
        digest: str,
        block: bool,
    ) -> None:
        self._recorder = recorder
        self._digest = digest
        self._block = block

    def __enter__(self) -> SupersessionAuthenticationClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        self._recorder.record_http(self._digest, self._block)
        return IDENTITY

    def post_aroast(self, *_args: object, **_kwargs: object) -> None:
        self._recorder.delivery_calls.append('post_aroast')

    def upload_revision(self, *_args: object, **_kwargs: object) -> object:
        self._recorder.delivery_calls.append('upload_revision')
        return object()


class ActivationShutdownRecorder:
    def __init__(self, candidate: str) -> None:
        self._candidate_digest = hashlib.sha256(candidate.encode('utf-8')).hexdigest()
        self._lock = threading.Lock()
        self._candidate_authentications = 0
        self.final_auth_entered = threading.Event()
        self.release_final_auth = threading.Event()
        self.api_calls: list[str] = []

    @override
    def __repr__(self) -> str:
        return '<ActivationShutdownRecorder credential=<redacted>>'

    def __call__(
        self, _origin: str, credential: str
    ) -> ActivationShutdownClient:
        digest = hashlib.sha256(credential.encode('utf-8')).hexdigest()
        return ActivationShutdownClient(self, digest == self._candidate_digest)

    def authenticate(self, candidate: bool) -> ServerIdentity:
        with self._lock:
            if candidate:
                self._candidate_authentications += 1
                final_auth = self._candidate_authentications == 2
            else:
                final_auth = False
            self.api_calls.append('test_connection')
        if not candidate:
            raise ApiFailure(public_failure(FailureKind.CREDENTIAL_REJECTED), 401, None)
        if final_auth:
            self.final_auth_entered.set()
            if not self.release_final_auth.wait(timeout=5):
                raise RuntimeError('blocked activation shutdown timed out')
        return IDENTITY


class ActivationShutdownClient:
    def __init__(
        self, recorder: ActivationShutdownRecorder, candidate: bool
    ) -> None:
        self._recorder = recorder
        self._candidate = candidate

    def __enter__(self) -> ActivationShutdownClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        return self._recorder.authenticate(self._candidate)

    def post_aroast(self, *_args: object, **_kwargs: object) -> None:
        self._recorder.api_calls.append('post_aroast')

    def upload_revision(self, *_args: object, **_kwargs: object) -> object:
        self._recorder.api_calls.append('upload_revision')
        return object()


class BlockingAuthenticationClient:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, int]] = []

    def __enter__(self) -> BlockingAuthenticationClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        self.calls.append(('test_connection', int(QThread.currentThreadId())))
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError('blocked authentication timed out')
        raise ApiFailure(public_failure(FailureKind.OFFLINE), None, None)


class FakeCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls: list[tuple[str, int]] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.delete_calls: list[tuple[str, int]] = []
        self.failure: CredentialStoreError | None = None

    @override
    def __repr__(self) -> str:
        return '<FakeCredentialStore values=<redacted>>'

    def get(self, origin: str) -> str | None:
        self.get_calls.append((origin, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        return self.values.get(origin)

    def set(self, origin: str, credential: str) -> None:
        digest = hashlib.sha256(credential.encode('utf-8')).hexdigest()
        self.set_calls.append((origin, digest, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        self.values[origin] = credential

    def delete(self, origin: str) -> None:
        self.delete_calls.append((origin, int(QThread.currentThreadId())))
        if self.failure is not None:
            raise self.failure
        self.values.pop(origin, None)

    def contains_origin(self, origin: str) -> bool:
        return origin in self.values


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
    stopped = pyqtSignal()

    def __init__(self, **_kwargs: object) -> None:
        super().__init__()
        self.calls: list[tuple[object, ...]] = []
        self.configure_values: list[WorkerConfiguration] = []
        self.test_ids: list[str] = []
        self.commit_ids: list[str] = []
        self.finalize_ids: list[str] = []
        self.acknowledge_ids: list[str] = []
        self.rollback_ids: list[str] = []
        self.cancel_ids: list[str] = []
        self.rollback_succeeds = True
        self.auto_finish_rollback = True
        self.enqueue_ids: list[str] = []
        self.browse_ids: list[str] = []
        self.publish_ids: list[str] = []
        self.protect_ids: list[str] = []
        self.discard_paths: list[str] = []
        self.clear_ids: list[str] = []
        self.start_count = 0
        self.stop_count = 0
        self.start_thread: int | None = None
        self.stop_thread: int | None = None
        self.interrupted_on_stop = False
        self.block_tests = False
        self.test_entered = threading.Event()
        self.test_release = threading.Event()
        self._lock = threading.Lock()

    def _record(self, *call: object) -> None:
        with self._lock:
            self.calls.append((*call, int(QThread.currentThreadId())))

    @pyqtSlot()
    def start(self) -> None:
        self.start_count += 1
        self.start_thread = int(QThread.currentThreadId())
        self._record('start')

    @pyqtSlot(object)
    def configure(self, value: object) -> None:
        assert isinstance(value, WorkerConfiguration)
        self.configure_values.append(value)
        self._record('configure', value)

    @pyqtSlot(str)
    def test_connection(self, request_id: str) -> None:
        self.test_ids.append(request_id)
        self._record('test_connection', request_id)
        if self.block_tests:
            self.test_entered.set()
            if not self.test_release.wait(timeout=5):
                raise RuntimeError('blocked fake worker timed out')

    @pyqtSlot(str)
    def commit_connection(self, request_id: str) -> None:
        self.commit_ids.append(request_id)
        self._record('commit_connection', request_id)

    @pyqtSlot(str)
    def finalize_connection(self, request_id: str) -> None:
        self.finalize_ids.append(request_id)
        self._record('finalize_connection', request_id)

    @pyqtSlot(str)
    def acknowledge_connection_activation(self, request_id: str) -> None:
        self.acknowledge_ids.append(request_id)
        self._record('acknowledge_connection_activation', request_id)

    @pyqtSlot(str)
    def rollback_connection(self, request_id: str) -> None:
        self.rollback_ids.append(request_id)
        self._record('rollback_connection', request_id)
        if self.auto_finish_rollback:
            self.connectionRollbackFinished.emit(request_id, self.rollback_succeeds)

    @pyqtSlot(str)
    def cancel_connection_transaction(self, request_id: str) -> None:
        self.cancel_ids.append(request_id)
        self._record('cancel_connection_transaction', request_id)
        if self.auto_finish_rollback:
            self.connectionRollbackFinished.emit(request_id, self.rollback_succeeds)

    @pyqtSlot(str)
    def remove_credential(self, request_id: str) -> None:
        self._record('remove_credential', request_id)

    @pyqtSlot(str)
    def enqueue_saved(self, request_id: str) -> None:
        self.enqueue_ids.append(request_id)
        self._record('enqueue_saved', request_id)

    @pyqtSlot()
    def refresh(self) -> None:
        self._record('refresh')

    @pyqtSlot(str)
    def retry_job(self, job_id: str) -> None:
        self._record('retry_job', job_id)

    @pyqtSlot(str)
    def remove_job(self, job_id: str) -> None:
        self._record('remove_job', job_id)

    @pyqtSlot(str)
    def browse(self, request_id: str) -> None:
        self.browse_ids.append(request_id)
        self._record('browse', request_id)

    @pyqtSlot(str)
    def open_online(self, request_id: str) -> None:
        self._record('open_online', request_id)

    @pyqtSlot(str)
    def open_cached(self, request_id: str) -> None:
        self._record('open_cached', request_id)

    @pyqtSlot(str)
    def publish_staged(self, request_id: str) -> None:
        self.publish_ids.append(request_id)
        self._record('publish_staged', request_id)

    @pyqtSlot(str)
    def discard_staged(self, path: str) -> None:
        self.discard_paths.append(path)
        self._record('discard_staged', path)

    @pyqtSlot(str)
    def update_protected_paths(self, request_id: str) -> None:
        self.protect_ids.append(request_id)
        self._record('update_protected_paths', request_id)

    @pyqtSlot(str)
    def clear_unused(self, request_id: str) -> None:
        self.clear_ids.append(request_id)
        self._record('clear_unused', request_id)

    @pyqtSlot()
    def stop(self) -> None:
        self.stop_count += 1
        self.stop_thread = int(QThread.currentThreadId())
        thread = QThread.currentThread()
        self.interrupted_on_stop = bool(
            isinstance(thread, QThread) and thread.isInterruptionRequested()
        )
        self._record('stop')
        self.stopped.emit()

    @pyqtSlot(str, object)
    def relay_connection(self, request_id: str, identity: object) -> None:
        self.connectionTested.emit(request_id, identity)

    @pyqtSlot(str, object)
    def relay_committed(self, request_id: str, identity: object) -> None:
        self.credentialCommitted.emit(request_id, identity)

    @pyqtSlot(str, object)
    def relay_activated(self, request_id: str, identity: object) -> None:
        self.connectionActivated.emit(request_id, identity)

    @pyqtSlot(str, bool)
    def relay_rollback(self, request_id: str, succeeded: bool) -> None:
        self.connectionRollbackFinished.emit(request_id, succeeded)

    @pyqtSlot(str, object)
    def relay_recovery(self, request_id: str, failure: object) -> None:
        self.pendingConnectionRecoveryRequired.emit(request_id, failure)

    @pyqtSlot(object)
    def relay_validated(self, configuration: object) -> None:
        self.configurationValidated.emit(configuration)

    @pyqtSlot(str)
    def relay_removed(self, request_id: str) -> None:
        self.credentialRemoved.emit(request_id)

    @pyqtSlot(str, object)
    def relay_failure(self, operation: str, failure: object) -> None:
        self.operationFailed.emit(operation, failure)

    @pyqtSlot(object)
    def relay_queue(self, value: object) -> None:
        self.queueChanged.emit(value)

    @pyqtSlot(object)
    def relay_failed_jobs(self, value: object) -> None:
        self.failedJobsChanged.emit(value)

    @pyqtSlot(object)
    def relay_cache_stats(self, value: object) -> None:
        self.cacheStatsChanged.emit(value)

    @pyqtSlot(str, object)
    def relay_archive(self, request_id: str, value: object) -> None:
        self.archivePageReady.emit(request_id, value)
        self.browseFinished.emit(request_id)

    @pyqtSlot(str)
    def relay_browse_finished(self, request_id: str) -> None:
        self.browseFinished.emit(request_id)

    @pyqtSlot(str, object)
    def relay_staged(self, request_id: str, value: object) -> None:
        self.downloadStaged.emit(request_id, value)

    @pyqtSlot(str, object)
    def relay_cached(self, request_id: str, value: object) -> None:
        self.cachedReady.emit(request_id, value)

    @pyqtSlot(str, object)
    def relay_cached_fallback(self, request_id: str, value: object) -> None:
        self.cachedFallbackReady.emit(request_id, value)

    @pyqtSlot(str, object)
    def relay_published(self, request_id: str, value: object) -> None:
        self.cachePublished.emit(request_id, value)

    @pyqtSlot(bool)
    def relay_online(self, value: bool) -> None:
        self.onlineChanged.emit(value)


class WorkerQueueBlocker(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    @pyqtSlot()
    def block(self) -> None:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError('worker queue blocker timed out')


class WorkerBlockRelay(QObject):
    block = pyqtSignal()


class WorkerRelay(QObject):
    connection = pyqtSignal(str, object)
    committed = pyqtSignal(str, object)
    activated = pyqtSignal(str, object)
    rollback = pyqtSignal(str, bool)
    recovery = pyqtSignal(str, object)
    validated = pyqtSignal(object)
    removed = pyqtSignal(str)
    failure = pyqtSignal(str, object)
    queue = pyqtSignal(object)
    failed_jobs = pyqtSignal(object)
    cache_stats = pyqtSignal(object)
    archive = pyqtSignal(str, object)
    browse_finished = pyqtSignal(str)
    staged = pyqtSignal(str, object)
    cached = pyqtSignal(str, object)
    cached_fallback = pyqtSignal(str, object)
    published = pyqtSignal(str, object)
    online = pyqtSignal(bool)


class ControllerHarness:
    def __init__(
        self,
        tmp_path: Path,
        app: QCoreApplication,
        prepare_settings: Callable[[SettingsStore], None] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.app = app
        qsettings = QSettings(
            str(tmp_path / 'controller.ini'), QSettings.Format.IniFormat
        )
        qsettings.clear()
        self.settings_store = SettingsStore(qsettings)
        self.settings_store.set_origin(ORIGIN)
        if prepare_settings is not None:
            prepare_settings(self.settings_store)
        self.credentials = FakeCredentialStore()
        self.ephemeral_secret = secrets.token_urlsafe(32)
        self.secret_vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
        self.profile_vault: OpaqueVault[SavedProfileRequest] = OpaqueVault()
        self.command_vault: OpaqueVault[object] = OpaqueVault()
        self.validator_calls: list[tuple[Path, int]] = []
        self.validator_failure: Exception | None = None
        self.validator_callback: Callable[[Path], None] | None = None
        self.worker = cast(FakeWorker, None)
        self.worker_protection_registry:object|None = None
        self.relay = WorkerRelay()

        def validate(path: Path) -> None:
            self.validator_calls.append((path, int(QThread.currentThreadId())))
            if self.validator_callback is not None:
                self.validator_callback(path)
            if self.validator_failure is not None:
                raise self.validator_failure

        def worker_factory(**kwargs: object) -> FakeWorker:
            assert kwargs['credential_vault'] is self.secret_vault
            assert repr(kwargs['configuration_fence']) == '<ConfigurationFence>'
            assert kwargs['profile_vault'] is self.profile_vault
            assert kwargs['command_vault'] is self.command_vault
            self.worker_protection_registry = kwargs['protection_registry']
            self.worker = FakeWorker(**kwargs)
            self.relay.connection.connect(self.worker.relay_connection)
            self.relay.committed.connect(self.worker.relay_committed)
            self.relay.activated.connect(self.worker.relay_activated)
            self.relay.rollback.connect(self.worker.relay_rollback)
            self.relay.recovery.connect(self.worker.relay_recovery)
            self.relay.validated.connect(self.worker.relay_validated)
            self.relay.removed.connect(self.worker.relay_removed)
            self.relay.failure.connect(self.worker.relay_failure)
            self.relay.queue.connect(self.worker.relay_queue)
            self.relay.failed_jobs.connect(self.worker.relay_failed_jobs)
            self.relay.cache_stats.connect(self.worker.relay_cache_stats)
            self.relay.archive.connect(self.worker.relay_archive)
            self.relay.browse_finished.connect(self.worker.relay_browse_finished)
            self.relay.staged.connect(self.worker.relay_staged)
            self.relay.cached.connect(self.worker.relay_cached)
            self.relay.cached_fallback.connect(self.worker.relay_cached_fallback)
            self.relay.published.connect(self.worker.relay_published)
            self.relay.online.connect(self.worker.relay_online)
            return self.worker

        self.controller = RoastServerController(
            settings=self.settings_store,
            credentials=self.credentials,
            data_root=tmp_path / 'data',
            client_factory=cast(ClientFactory, lambda *_args: None),
            profile_validator=validate,
            credential_vault=self.secret_vault,
            profile_vault=self.profile_vault,
            command_vault=self.command_vault,
            worker_factory=worker_factory,
            clock=lambda: NOW,
        )
        assert self.worker_protection_registry is self.controller._protection_registry
        self.controller.start()
        self.wait_until(lambda: self.worker.start_count == 1)
        self.wait_until(lambda: bool(self.worker.configure_values))

    @property
    def fake_worker(self) -> FakeWorker:
        return self.worker

    @property
    def ui_thread_id(self) -> int:
        return int(QThread.currentThreadId())

    def wait_until(self, predicate: Callable[[], bool], timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError('bounded Qt event wait timed out')
            time.sleep(0.001)
        self.app.processEvents()

    def wait_for_spy(
        self, spy: QSignalSpy, before: int = 0, timeout: float = 2.0
    ) -> list[object]:
        self.wait_until(lambda: len(spy) > before, timeout)
        return list(spy[-1])

    def confirm(self, identity: ServerIdentity = IDENTITY, *, origin: str = ORIGIN) -> str:
        changed = QSignalSpy(self.controller.identityChanged)
        request_id = self.controller.test_connection(origin, self.ephemeral_secret)
        self.wait_until(lambda: request_id in self.fake_worker.test_ids)
        self.relay.connection.emit(request_id, identity)
        self.wait_until(lambda: request_id in self.fake_worker.commit_ids)
        self.relay.committed.emit(request_id, identity)
        self.wait_until(lambda: request_id in self.fake_worker.finalize_ids)
        self.relay.activated.emit(request_id, identity)
        assert self.wait_for_spy(changed)[0] == identity
        return request_id

    def enable(self, *, automatic_upload: bool = True) -> None:
        before = len(self.fake_worker.configure_values)
        self.controller.apply_options(
            ORIGIN,
            enabled=True,
            automatic_upload=automatic_upload,
            cache_limit_bytes=64 * 1024 * 1024,
        )
        self.wait_until(lambda: len(self.fake_worker.configure_values) > before)

    def stop(self) -> None:
        if self.controller.worker_thread_running:
            assert self.controller.shutdown(2_000)
        self.app.processEvents()


@pytest.fixture(scope='module')
def qcoreapplication() -> Generator[QCoreApplication]:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def controller_harness(
    tmp_path: Path, qcoreapplication: QCoreApplication
) -> Generator[ControllerHarness]:
    harness = ControllerHarness(tmp_path, qcoreapplication)
    yield harness
    harness.stop()


def test_dialog_edit_invalidation_revokes_controller_proof_and_pauses_work(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=True)
    identity_changed = QSignalSpy(controller_harness.controller.identityChanged)
    configurations_before = len(controller_harness.fake_worker.configure_values)

    controller_harness.controller.invalidate_connection_proof()

    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.configure_values)
        > configurations_before
    )
    settings = controller_harness.settings_store.load()
    configuration = controller_harness.fake_worker.configure_values[-1]
    assert list(identity_changed[-1]) == [None]
    assert not settings.enabled
    assert not settings.automatic_upload
    assert not configuration.enabled
    assert not configuration.automatic_upload


def test_dialog_edit_invalidation_cancels_pending_opaque_transaction(
    controller_harness: ControllerHarness,
) -> None:
    request_id = controller_harness.controller.test_connection(
        ORIGIN,
        controller_harness.ephemeral_secret,
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )

    controller_harness.controller.invalidate_connection_proof()

    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.cancel_ids
    )
    assert controller_harness.secret_vault.size() == 0


def test_cancel_connection_test_discards_exact_latest_request_and_late_result(
    controller_harness: ControllerHarness,
) -> None:
    changed = QSignalSpy(controller_harness.controller.identityChanged)
    request_id = controller_harness.controller.test_connection(
        ORIGIN,
        controller_harness.ephemeral_secret,
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )

    controller_harness.controller.cancel_connection_test(request_id)

    assert controller_harness.secret_vault.size() == 0
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.cancel_ids
    )
    controller_harness.relay.connection.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: controller_harness.fake_worker.cancel_ids.count(request_id) >= 2
    )
    assert controller_harness.settings_store.load().identity is None
    assert all(
        list(changed[index]) != [IDENTITY] for index in range(len(changed))
    )
    assert controller_harness.credentials.set_calls == []


def test_dialog_geometries_are_detached_saved_and_preserved_by_controller(
    controller_harness: ControllerHarness,
) -> None:
    geometry = QByteArray(b'bounded-public-geometry')
    browser_geometry = QByteArray(b'bounded-browser-geometry')

    controller_harness.controller.save_configuration_geometry(geometry)
    controller_harness.controller.save_browser_geometry(browser_geometry)

    saved = controller_harness.settings_store.load()
    assert saved.configuration_geometry == geometry
    assert saved.configuration_geometry is not geometry
    assert saved.browser_geometry == browser_geometry
    assert saved.browser_geometry is not browser_geometry


def test_auto_upload_cannot_enable_before_confirmed_test(
    controller_harness: ControllerHarness,
) -> None:
    with pytest.raises(ControllerError, match='Test the connection'):
        controller_harness.controller.apply_options(
            origin=ORIGIN,
            enabled=True,
            automatic_upload=True,
            cache_limit_bytes=512 * 1024 * 1024,
        )

    assert not controller_harness.settings_store.load().automatic_upload


def test_persisted_identity_never_proves_connection_before_worker_validation(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    def prepare(store: SettingsStore) -> None:
        store.save_connection(ORIGIN, IDENTITY)
        store.save_options(True, True, 64 * 1024 * 1024)

    harness = ControllerHarness(tmp_path, qcoreapplication, prepare)
    try:
        configuration = harness.fake_worker.configure_values[-1]
        assert configuration.identity == IDENTITY
        assert configuration.automatic_upload
        assert isinstance(getattr(configuration, 'validation_id', None), str)
        assert len(configuration.validation_id) == 32
        assert type(getattr(configuration, 'generation', None)) is int
        assert configuration.generation > 0
        with pytest.raises(ControllerError, match='Test the connection'):
            harness.controller.apply_options(
                ORIGIN,
                enabled=True,
                automatic_upload=True,
                cache_limit_bytes=64 * 1024 * 1024,
            )

        validated = QSignalSpy(harness.controller.identityChanged)
        harness.relay.validated.emit(configuration)
        harness.wait_until(lambda: len(validated) > 0)
        harness.controller.apply_options(
            ORIGIN,
            enabled=True,
            automatic_upload=True,
            cache_limit_bytes=64 * 1024 * 1024,
        )
    finally:
        harness.stop()


def test_invalidation_rejects_stale_and_fresh_disabled_configuration_proof(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    def prepare(store: SettingsStore) -> None:
        store.save_connection(ORIGIN, IDENTITY)
        store.save_options(True, True, 64 * 1024 * 1024)

    harness = ControllerHarness(tmp_path, qcoreapplication, prepare)
    try:
        startup = harness.fake_worker.configure_values[-1]
        identities = QSignalSpy(harness.controller.identityChanged)

        harness.controller.invalidate_connection_proof()
        harness.wait_until(lambda: len(harness.fake_worker.configure_values) >= 2)
        disabled = harness.fake_worker.configure_values[-1]
        assert startup.validation_id != disabled.validation_id
        assert startup.generation != disabled.generation
        assert not harness.controller._configuration_fence.authorizes(
            startup.generation
        )
        assert not harness.controller._configuration_fence.authorizes(
            disabled.generation
        )
        assert not disabled.enabled and not disabled.automatic_upload

        harness.relay.validated.emit(startup)
        harness.relay.validated.emit(disabled)
        for _ in range(10):
            qcoreapplication.processEvents()

        assert all(
            list(identities[index]) != [IDENTITY]
            for index in range(len(identities))
        )
        with pytest.raises(ControllerError, match='Test the connection'):
            harness.controller.apply_options(
                ORIGIN,
                enabled=True,
                automatic_upload=True,
                cache_limit_bytes=64 * 1024 * 1024,
            )
    finally:
        harness.stop()


def test_restart_recovers_committed_pending_public_identity_before_final_auth(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    def prepare(store: SettingsStore) -> None:
        store.save_connection(ORIGIN, IDENTITY)
        store.save_options(True, False, 64 * 1024 * 1024)
        store.save_pending_connection(ORIGIN, OTHER_IDENTITY)

    harness = ControllerHarness(tmp_path, qcoreapplication, prepare)
    try:
        pending_configuration = harness.fake_worker.configure_values[-1]
        assert pending_configuration.pending_connection
        assert pending_configuration.identity == OTHER_IDENTITY
        transaction_id = 'f' * 32

        harness.relay.committed.emit(transaction_id, OTHER_IDENTITY)
        harness.wait_until(
            lambda: transaction_id in harness.fake_worker.finalize_ids
        )
        journaled = harness.settings_store.load()
        assert journaled.identity == IDENTITY
        assert journaled.pending_connection is not None
        assert journaled.pending_connection.identity == OTHER_IDENTITY
        assert not journaled.enabled and not journaled.automatic_upload
        activation_configuration = harness.fake_worker.configure_values[-1]
        assert activation_configuration.activation_id == transaction_id
        assert activation_configuration.pending_connection
        harness.relay.activated.emit(transaction_id, OTHER_IDENTITY)
        harness.wait_until(
            lambda: harness.fake_worker.configure_values[-1].activation_id is None
        )
        assert harness.settings_store.load().identity == OTHER_IDENTITY
    finally:
        harness.stop()


def test_controller_settings_failure_emits_fixed_failure_and_never_processes(
    controller_harness: ControllerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_harness.confirm()
    failed = QSignalSpy(controller_harness.controller.operationFailed)

    def reject_save(
        _enabled: bool,
        _automatic_upload: bool,
        _cache_limit_bytes: int,
    ) -> object:
        raise SettingsError('backend detail')

    monkeypatch.setattr(
        controller_harness.settings_store,
        'save_options',
        reject_save,
    )
    with pytest.raises(ControllerError) as raised:
        controller_harness.controller.apply_options(
            ORIGIN,
            enabled=True,
            automatic_upload=True,
            cache_limit_bytes=64 * 1024 * 1024,
        )

    assert raised.value.args == (SETTINGS_FAILURE_MESSAGE,)
    assert list(failed[-1]) == [
        'settings',
        public_failure(FailureKind.SETTINGS),
    ]
    assert not controller_harness.fake_worker.configure_values[-1].enabled
    assert not controller_harness.fake_worker.configure_values[-1].automatic_upload


def test_candidate_credential_crosses_only_the_vault(
    controller_harness: ControllerHarness,
) -> None:
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    assert controller_harness.secret_vault.contains(request_id)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )

    assert controller_harness.fake_worker.test_ids == [request_id]
    assert_secret_absent(
        controller_harness.ephemeral_secret, controller_harness.fake_worker.calls
    )
    assert_secret_absent(
        controller_harness.ephemeral_secret, controller_harness.controller
    )
    assert controller_harness.credentials.set_calls == []


def test_new_test_and_every_stale_transaction_event_queue_cancellation(
    controller_harness: ControllerHarness,
) -> None:
    first_id = controller_harness.controller.test_connection(
        ORIGIN, secrets.token_urlsafe(32)
    )
    controller_harness.wait_until(
        lambda: first_id in controller_harness.fake_worker.test_ids
    )

    second_id = controller_harness.controller.test_connection(
        ORIGIN, secrets.token_urlsafe(32)
    )
    controller_harness.wait_until(
        lambda: second_id in controller_harness.fake_worker.test_ids
        and first_id in controller_harness.fake_worker.cancel_ids
    )
    calls = controller_harness.fake_worker.calls
    cancel_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ('cancel_connection_transaction', first_id)
    )
    second_test_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ('test_connection', second_id)
    )
    assert cancel_index < second_test_index

    expected_cancellations = len(controller_harness.fake_worker.cancel_ids) + 3
    controller_harness.relay.connection.emit(first_id, IDENTITY)
    controller_harness.relay.committed.emit(first_id, IDENTITY)
    controller_harness.relay.activated.emit(first_id, IDENTITY)
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.cancel_ids)
        == expected_cancellations
    )

    loaded = controller_harness.settings_store.load()
    assert loaded.identity is None
    assert loaded.pending_connection is None
    assert controller_harness.fake_worker.commit_ids == []
    assert controller_harness.fake_worker.finalize_ids == []


def test_cross_origin_pending_recovery_clear_failure_stays_paused(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prepare(store: SettingsStore) -> None:
        store.save_connection(ORIGIN, IDENTITY)
        store.save_options(True, False, 64 * 1024 * 1024)
        store.save_pending_connection(OTHER_ORIGIN, OTHER_IDENTITY)

    harness = ControllerHarness(tmp_path, qcoreapplication, prepare)
    failed = QSignalSpy(harness.controller.operationFailed)
    transaction_id = 'e' * 32

    def reject_clear() -> object:
        raise SettingsError('backend detail')

    monkeypatch.setattr(
        harness.settings_store,
        'clear_pending_connection',
        reject_clear,
    )
    try:
        failure = public_failure(FailureKind.CREDENTIAL_REJECTED)
        harness.relay.recovery.emit(
            transaction_id,
            PendingConnectionRecovery(None, False, failure),
        )
        harness.wait_until(lambda: len(failed) > 0)

        assert list(failed[-1]) == [
            transaction_id,
            public_failure(FailureKind.SETTINGS),
        ]
        assert harness.settings_store.load().pending_connection is not None
        configuration = harness.fake_worker.configure_values[-1]
        assert configuration.identity == OTHER_IDENTITY
        assert configuration.namespace == namespace_for(
            OTHER_ORIGIN, OTHER_IDENTITY.organization.id
        )
        assert configuration.pending_connection
        assert not configuration.enabled
        assert not configuration.automatic_upload
    finally:
        harness.stop()


def test_identity_persists_only_after_worker_success_and_forwards_on_ui_thread(
    controller_harness: ControllerHarness,
) -> None:
    changed = QSignalSpy(controller_harness.controller.identityChanged)
    signal_threads: list[int] = []
    direct_connect = cast(
        Callable[[Callable[[object], None], Qt.ConnectionType], object],
        controller_harness.controller.identityChanged.connect,
    )
    direct_connect(
        lambda _value: signal_threads.append(int(QThread.currentThreadId())),
        Qt.ConnectionType.DirectConnection,
    )
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    assert controller_harness.settings_store.load().identity is None

    controller_harness.relay.connection.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )
    pending = controller_harness.settings_store.load()
    assert pending.identity is None
    assert pending.pending_connection is not None
    controller_harness.relay.committed.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.finalize_ids
    )
    controller_harness.relay.activated.emit(request_id, IDENTITY)
    assert controller_harness.wait_for_spy(changed) == [IDENTITY]
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.acknowledge_ids
    )

    loaded = controller_harness.settings_store.load()
    assert loaded.identity == IDENTITY
    assert not loaded.automatic_upload
    assert signal_threads == [controller_harness.ui_thread_id]


def test_active_settings_failure_rolls_back_worker_and_previous_public_state(
    controller_harness: ControllerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.relay.connection.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )

    def reject_activation(
        _origin: str,
        _identity: ServerIdentity,
    ) -> object:
        raise SettingsError('backend detail')

    monkeypatch.setattr(
        controller_harness.settings_store,
        'activate_pending_connection',
        reject_activation,
    )
    controller_harness.relay.committed.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.finalize_ids
    )
    controller_harness.relay.activated.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.cancel_ids
    )

    loaded = controller_harness.settings_store.load()
    assert loaded.origin == ORIGIN
    assert loaded.identity == IDENTITY
    assert loaded.pending_connection is None
    assert not loaded.enabled and not loaded.automatic_upload
    assert not controller_harness.fake_worker.configure_values[-1].enabled


def test_interactive_cancel_clears_journal_only_after_acknowledged_rollback(
    controller_harness: ControllerHarness,
) -> None:
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.relay.connection.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )
    controller_harness.relay.committed.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.finalize_ids
    )
    controller_harness.fake_worker.auto_finish_rollback = False

    controller_harness.controller.invalidate_connection_proof()
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.cancel_ids
    )

    settling = controller_harness.settings_store.load()
    assert settling.pending_connection is not None
    assert not controller_harness.fake_worker.configure_values[-1].enabled

    controller_harness.relay.rollback.emit(request_id, True)
    controller_harness.wait_until(
        lambda: controller_harness.settings_store.load().pending_connection is None
    )
    assert controller_harness.settings_store.load().identity is None


def test_rollback_failure_preserves_pending_recovery_journal(
    controller_harness: ControllerHarness,
) -> None:
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.relay.connection.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )
    controller_harness.relay.committed.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.finalize_ids
    )
    controller_harness.fake_worker.rollback_succeeds = False
    failed = QSignalSpy(controller_harness.controller.operationFailed)

    controller_harness.controller.invalidate_connection_proof()
    controller_harness.wait_until(
        lambda: _spy_matches(
            failed,
            lambda payload: payload
            == [request_id, public_failure(FailureKind.KEYRING)],
        )
    )

    unresolved = controller_harness.settings_store.load()
    assert unresolved.pending_connection is not None
    assert unresolved.identity is None
    assert not unresolved.enabled and not unresolved.automatic_upload


def test_untracked_activation_signal_cannot_install_connection_proof(
    controller_harness: ControllerHarness,
) -> None:
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.relay.connection.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )
    controller_harness.relay.committed.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.finalize_ids
    )
    forged_id = 'f' * 32
    assert forged_id != request_id

    controller_harness.relay.activated.emit(forged_id, IDENTITY)
    controller_harness.wait_until(
        lambda: forged_id in controller_harness.fake_worker.cancel_ids
    )
    assert controller_harness.fake_worker.acknowledge_ids == []

    with pytest.raises(ControllerError, match='rollback is still settling'):
        controller_harness.controller.apply_options(
            ORIGIN,
            enabled=True,
            automatic_upload=True,
            cache_limit_bytes=64 * 1024 * 1024,
        )


def test_keyring_failure_keeps_old_identity_and_origin_but_turns_auto_off(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    assert controller_harness.settings_store.load().automatic_upload
    identity_changed = QSignalSpy(controller_harness.controller.identityChanged)
    failed = QSignalSpy(controller_harness.controller.operationFailed)

    request_id = controller_harness.controller.test_connection(
        OTHER_ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.relay.failure.emit(
        request_id, public_failure(FailureKind.KEYRING)
    )
    payload = controller_harness.wait_for_spy(failed)

    loaded = controller_harness.settings_store.load()
    assert loaded.origin == ORIGIN
    assert loaded.identity == IDENTITY
    assert not loaded.automatic_upload
    assert payload == [request_id, public_failure(FailureKind.KEYRING)]
    assert list(identity_changed[-1]) == [None]
    assert not controller_harness.fake_worker.configure_values[-1].enabled


def test_origin_and_organization_changes_pause_old_namespace_before_new_one(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    old_namespace = namespace_for(ORIGIN, ORGANIZATION_ID)
    before = len(controller_harness.fake_worker.configure_values)

    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
        and len(controller_harness.fake_worker.configure_values) > before
    )
    paused = controller_harness.fake_worker.configure_values[-1]
    assert paused.namespace == old_namespace
    assert not paused.enabled

    controller_harness.relay.connection.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )
    controller_harness.relay.committed.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: controller_harness.fake_worker.configure_values[-1].namespace
        == namespace_for(ORIGIN, OTHER_ORGANIZATION_ID)
    )
    controller_harness.relay.activated.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.acknowledge_ids
    )
    assert not controller_harness.fake_worker.configure_values[-1].automatic_upload

    before = len(controller_harness.fake_worker.configure_values)
    controller_harness.controller.apply_options(
        OTHER_ORIGIN,
        enabled=True,
        automatic_upload=False,
        cache_limit_bytes=64 * 1024 * 1024,
    )
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.configure_values) >= before + 2
    )
    old, new = controller_harness.fake_worker.configure_values[-2:]
    assert old.namespace == namespace_for(ORIGIN, OTHER_ORGANIZATION_ID)
    assert not old.enabled
    assert new.origin == OTHER_ORIGIN and new.namespace is None
    assert new.enabled


def test_401_clears_connected_ui_state_without_deleting_keyring_entry(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    changed = QSignalSpy(controller_harness.controller.identityChanged)
    failed = QSignalSpy(controller_harness.controller.operationFailed)

    controller_harness.relay.failure.emit(
        'queue', public_failure(FailureKind.CREDENTIAL_REJECTED)
    )
    assert controller_harness.wait_for_spy(failed) == [
        'queue',
        public_failure(FailureKind.CREDENTIAL_REJECTED),
    ]

    assert list(changed[-1]) == [None]
    assert controller_harness.credentials.delete_calls == []
    assert not controller_harness.settings_store.load().automatic_upload


def test_remove_credential_is_worker_queued_and_pauses_before_removal(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    changed = QSignalSpy(controller_harness.controller.identityChanged)
    before = len(controller_harness.fake_worker.calls)

    controller_harness.controller.remove_credential()
    controller_harness.wait_until(
        lambda: any(call[0] == 'remove_credential' for call in controller_harness.fake_worker.calls)
    )
    calls = controller_harness.fake_worker.calls[before:]
    configure_index = next(index for index, call in enumerate(calls) if call[0] == 'configure')
    remove_index = next(
        index for index, call in enumerate(calls) if call[0] == 'remove_credential'
    )
    assert configure_index < remove_index
    request_id = cast(str, calls[remove_index][1])
    controller_harness.relay.removed.emit(request_id)
    assert controller_harness.wait_for_spy(changed)[0] is None
    assert controller_harness.credentials.delete_calls == []
    assert not controller_harness.settings_store.load().automatic_upload


def test_saved_profile_detaches_exact_bytes_profile_and_timestamp_without_ui_io(
    controller_harness: ControllerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    profile = ProfileData(
        roastUUID=str(ROAST_UUID),
        title='Controller roast',
        flavors=[1.0, 2.0],
    )
    serialized = bytes(PROFILE_BYTES)
    opened: list[None] = []

    def reject_open(*_args: object, **_kwargs: object) -> object:
        opened.append(None)
        raise AssertionError('controller attempted UI-thread file I/O')

    monkeypatch.setattr(Path, 'open', reject_open)
    started = time.monotonic()
    controller_harness.controller.record_saved_profile(serialized, profile, NOW)
    elapsed = time.monotonic() - started
    profile['title'] = 'mutated after save hook'
    flavors = profile.get('flavors')
    assert flavors is not None
    flavors.append(3.0)

    assert elapsed < 0.05
    assert opened == []
    assert controller_harness.profile_vault.size() == 1
    controller_harness.wait_until(
        lambda: any(
            controller_harness.profile_vault.contains(request_id)
            for request_id in controller_harness.fake_worker.enqueue_ids
        )
    )
    request_id = next(
        request_id
        for request_id in controller_harness.fake_worker.enqueue_ids
        if controller_harness.profile_vault.contains(request_id)
    )
    request = controller_harness.profile_vault.take(request_id)
    assert request.serialized_profile == serialized
    assert request.profile == {
        'roastUUID': str(ROAST_UUID),
        'title': 'Controller roast',
        'flavors': [1.0, 2.0],
    }
    assert request.modified_at == NOW
    assert not request.manual
    assert serialized.decode('utf-8') not in repr(request)


def test_two_queued_saves_retain_distinct_exact_causal_revisions(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    second_uuid = UUID('55555555-5555-4555-8555-555555555555')
    first_profile = ProfileData(roastUUID=str(ROAST_UUID), title='first')
    second_profile = ProfileData(roastUUID=str(second_uuid), title='second')
    first_bytes = repr(dict(first_profile)).encode('utf-8')
    second_bytes = repr(dict(second_profile)).encode('utf-8')
    second_modified = NOW.replace(microsecond=1)

    controller_harness.controller.saved_profile(first_bytes, first_profile, NOW)
    controller_harness.controller.saved_profile(
        second_bytes, second_profile, second_modified
    )
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.enqueue_ids) >= 2
    )

    requests = tuple(
        controller_harness.profile_vault.take(request_id)
        for request_id in controller_harness.fake_worker.enqueue_ids[-2:]
    )
    assert tuple(request.serialized_profile for request in requests) == (
        first_bytes,
        second_bytes,
    )
    assert tuple(request.profile for request in requests) == (
        first_profile,
        second_profile,
    )
    assert tuple(request.modified_at for request in requests) == (
        NOW,
        second_modified,
    )


def test_manual_and_automatic_queue_rules_are_exact(
    controller_harness: ControllerHarness,
) -> None:
    profile = ProfileData(roastUUID=str(ROAST_UUID))

    controller_harness.controller.saved_profile(PROFILE_BYTES, profile, NOW)
    assert controller_harness.profile_vault.size() == 0
    with pytest.raises(ControllerError, match='Enable Roast Server'):
        controller_harness.controller.manual_upload(PROFILE_BYTES, profile, NOW)

    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    controller_harness.controller.saved_profile(PROFILE_BYTES, profile, NOW)
    assert controller_harness.profile_vault.size() == 0
    controller_harness.controller.manual_upload(PROFILE_BYTES, profile, NOW)
    controller_harness.wait_until(
        lambda: controller_harness.profile_vault.size() == 1
        and bool(controller_harness.fake_worker.enqueue_ids)
    )
    manual_id = controller_harness.fake_worker.enqueue_ids[-1]
    manual = controller_harness.profile_vault.take(manual_id)
    assert manual == SavedProfileRequest(
        namespace_for(ORIGIN, ORGANIZATION_ID), PROFILE_BYTES, profile, NOW, True
    )


def test_commands_and_immutable_worker_results_are_forwarded_on_main_thread(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    queue_spy = QSignalSpy(controller_harness.controller.queueChanged)
    failed_spy = QSignalSpy(controller_harness.controller.failedJobsChanged)
    stats_spy = QSignalSpy(controller_harness.controller.cacheStatsChanged)
    online_spy = QSignalSpy(controller_harness.controller.onlineChanged)
    signal_threads: list[int] = []
    direct_connect = cast(
        Callable[[Callable[[object], None], Qt.ConnectionType], object],
        controller_harness.controller.queueChanged.connect,
    )
    direct_connect(
        lambda _value: signal_threads.append(int(QThread.currentThreadId())),
        Qt.ConnectionType.DirectConnection,
    )
    counts = QueueCounts(1, 2, 3, 4, 5)
    failed_jobs = (
        FailedJob(
            id='a' * 32,
            roast_uuid=ROAST_UUID,
            sha256='b' * 64,
            attempts=2,
            next_attempt_at=None,
            error_code='invalid_profile',
            error_message='Profile rejected by server.',
            updated_at=NOW,
        ),
    )
    stats = CacheStats(123, 1)

    controller_harness.relay.queue.emit(counts)
    controller_harness.relay.failed_jobs.emit(failed_jobs)
    controller_harness.relay.cache_stats.emit(stats)
    controller_harness.relay.online.emit(True)

    assert controller_harness.wait_for_spy(queue_spy) == [counts]
    assert controller_harness.wait_for_spy(failed_spy) == [failed_jobs]
    assert controller_harness.wait_for_spy(stats_spy) == [stats]
    assert controller_harness.wait_for_spy(online_spy) == [True]
    assert signal_threads == [controller_harness.ui_thread_id]

    controller_harness.controller.refresh_queue()
    controller_harness.controller.retry_job('a' * 32)
    controller_harness.controller.remove_job('a' * 32)
    controller_harness.wait_until(
        lambda: any(call[0] == 'remove_job' for call in controller_harness.fake_worker.calls)
    )


def test_browse_tracks_cursor_and_ignores_stale_pages(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    page_spy = QSignalSpy(controller_harness.controller.archivePageReady)
    filters = ArchiveFilters(search='controller')
    first_id = controller_harness.controller.browse(filters)
    first_request = cast(BrowseRequest, controller_harness.command_vault.take(first_id))
    assert first_request.cursor is None and first_request.refresh
    assert controller_harness.controller.load_more() is None

    page = RoastPage((summary_for(),), 'next-cursor')
    controller_harness.relay.archive.emit(first_id, page)
    first_payload = controller_harness.wait_for_spy(page_spy)
    first_view = cast(ArchivePageView, first_payload[1])
    assert first_payload[0] == first_id
    assert first_view.rows[0].roast == page.items[0]
    assert first_view.next_cursor == page.next_cursor
    more_id = controller_harness.controller.load_more()
    assert more_id is not None
    more_request = cast(BrowseRequest, controller_harness.command_vault.take(more_id))
    assert more_request.cursor == 'next-cursor'
    assert not more_request.refresh

    newer_id = controller_harness.controller.browse(ArchiveFilters(search='new'))
    controller_harness.command_vault.take(newer_id)
    controller_harness.relay.archive.emit(more_id, RoastPage((), None))
    time.sleep(0.01)
    controller_harness.app.processEvents()
    assert len(page_spy) == 1


def test_blocked_browse_coalesces_one_active_and_only_latest_pending_filters(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    vault_before = controller_harness.command_vault.size()
    first_id = controller_harness.controller.browse(ArchiveFilters(search='first'))
    latest_id = first_id
    for number in range(100):
        latest_id = controller_harness.controller.browse(
            ArchiveFilters(search=f'latest-{number}')
        )

    controller_harness.wait_until(
        lambda: first_id in controller_harness.fake_worker.browse_ids
    )
    assert controller_harness.fake_worker.browse_ids == [first_id]
    assert controller_harness.command_vault.size() <= vault_before + 2

    controller_harness.relay.browse_finished.emit(first_id)
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.browse_ids) == 2
    )
    assert controller_harness.fake_worker.browse_ids == [first_id, latest_id]
    latest = cast(BrowseRequest, controller_harness.command_vault.take(latest_id))
    assert latest.filters.search == 'latest-99'


def test_browse_emits_immutable_page_views_and_failed_next_cursor_can_retry(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.archivePageReady)
    failed = QSignalSpy(controller_harness.controller.operationFailed)
    first_id = controller_harness.controller.browse(ArchiveFilters())
    controller_harness.command_vault.take(first_id)
    online = RoastPage((summary_for(),), 'next-cursor')

    controller_harness.relay.archive.emit(first_id, online)

    first_payload = controller_harness.wait_for_spy(ready)
    first_view = cast(ArchivePageView, first_payload[1])
    assert first_payload[0] == first_id
    assert first_view.online
    assert first_view.next_cursor == 'next-cursor'
    assert first_view.retained_error is None
    assert first_view.rows[0].roast == summary_for()
    assert first_view.rows[0].cached_revision is None

    more_id = controller_harness.controller.load_more()
    assert more_id is not None
    controller_harness.command_vault.take(more_id)
    failure = public_failure(FailureKind.OFFLINE)
    controller_harness.relay.failure.emit(more_id, failure)
    assert controller_harness.wait_for_spy(failed) == [more_id, failure]
    controller_harness.relay.browse_finished.emit(more_id)
    controller_harness.wait_until(
        lambda: controller_harness.controller._browse_active_id is None
    )

    retry_id = controller_harness.controller.load_more()
    assert retry_id is not None
    retry = cast(BrowseRequest, controller_harness.command_vault.take(retry_id))
    assert retry.cursor == 'next-cursor'


@pytest.mark.parametrize(
    'returned_cursors',
    [('A', 'A'), ('A', 'B', 'A')],
    ids=['A-A', 'A-B-A'],
)
def test_browse_rejects_successful_cursor_cycles_and_stops_paging(
    controller_harness: ControllerHarness,
    returned_cursors: tuple[str, ...],
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    failed = QSignalSpy(controller_harness.controller.operationFailed)
    first_id = controller_harness.controller.browse(ArchiveFilters())
    controller_harness.command_vault.take(first_id)
    controller_harness.relay.archive.emit(
        first_id, RoastPage((summary_for(),), returned_cursors[0])
    )
    controller_harness.wait_until(
        lambda: controller_harness.controller._browse_active_id is None
    )

    for next_cursor in returned_cursors[1:]:
        more_id = controller_harness.controller.load_more()
        assert more_id is not None
        request = cast(BrowseRequest, controller_harness.command_vault.take(more_id))
        assert request.cursor is not None
        controller_harness.relay.archive.emit(
            more_id, RoastPage((), next_cursor)
        )
        controller_harness.wait_until(
            lambda: controller_harness.controller._browse_active_id is None
        )
        if next_cursor == returned_cursors[0]:
            assert list(failed[-1]) == [
                more_id,
                public_failure(FailureKind.INVALID_RESPONSE),
            ]

    assert controller_harness.controller._next_cursor is None
    assert controller_harness.controller.load_more() is None


def test_failed_cursor_is_not_consumed_and_can_retry(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    first_id = controller_harness.controller.browse(ArchiveFilters())
    controller_harness.command_vault.take(first_id)
    controller_harness.relay.archive.emit(first_id, RoastPage((), 'A'))
    controller_harness.wait_until(
        lambda: controller_harness.controller._browse_active_id is None
    )
    failed_id = controller_harness.controller.load_more()
    assert failed_id is not None
    controller_harness.command_vault.take(failed_id)
    controller_harness.relay.failure.emit(
        failed_id, public_failure(FailureKind.OFFLINE)
    )
    controller_harness.relay.browse_finished.emit(failed_id)
    controller_harness.wait_until(
        lambda: controller_harness.controller._browse_active_id is None
    )

    retry_id = controller_harness.controller.load_more()
    assert retry_id is not None
    retry = cast(BrowseRequest, controller_harness.command_vault.take(retry_id))
    assert retry.cursor == 'A'


def test_cached_page_view_carries_verified_revision_and_retained_failure(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.archivePageReady)
    first_id = controller_harness.controller.browse(ArchiveFilters())
    controller_harness.command_vault.take(first_id)
    failure = public_failure(FailureKind.OFFLINE)
    cached = cached_revision(controller_harness.tmp_path / 'offline.alog')

    controller_harness.relay.failure.emit(first_id, failure)
    controller_harness.relay.archive.emit(first_id, CachedPage((cached,)))

    payload = controller_harness.wait_for_spy(ready)
    view = cast(ArchivePageView, payload[1])
    assert not view.online
    assert view.retained_error == failure
    assert view.next_cursor is None
    assert len(view.rows) == 1
    assert view.rows[0].cached_revision == cached.revision.revision_number
    assert view.rows[0].cached_sha256 == cached.revision.sha256
    assert view.rows[0].cached == cached
    assert view.rows[0].stale

    online_id = controller_harness.controller.browse(ArchiveFilters())
    controller_harness.command_vault.take(online_id)
    controller_harness.relay.archive.emit(
        online_id, RoastPage((cached.roast,), None)
    )
    controller_harness.wait_until(lambda: len(ready) == 2)
    open_id = controller_harness.controller.open_roast(cached.roast.roast_uuid)
    open_request = cast(
        OnlineOpenRequest, controller_harness.command_vault.take(open_id)
    )
    assert open_request.cached_fallback == cached


def test_controller_bounds_cached_revision_lru_to_visible_model_capacity(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    first_id = controller_harness.controller.browse(ArchiveFilters())
    controller_harness.command_vault.take(first_id)
    cached_items = tuple(
        CachedRevision(
            namespace=namespace_for(ORIGIN, ORGANIZATION_ID),
            roast=replace(summary_for(), roast_uuid=UUID(int=index + 1)),
            revision=revision_for(),
            path=controller_harness.tmp_path / f'{index}.alog',
            sidecar_path=controller_harness.tmp_path / f'{index}.json',
            downloaded_at=NOW,
        )
        for index in range(5_025)
    )

    controller_harness.relay.archive.emit(first_id, CachedPage(cached_items))
    controller_harness.wait_until(
        lambda: controller_harness.controller._browse_active_id is None
    )

    known = controller_harness.controller._known_cached_revisions
    assert len(known) <= 5_000
    assert cached_items[-1].roast.roast_uuid in known


def test_ready_cache_paths_keep_current_and_only_a_small_pending_set(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    current = cached_revision(controller_harness.tmp_path / 'current.alog')
    first_id = controller_harness.controller.open_cached(current)
    controller_harness.command_vault.take(first_id)
    controller_harness.relay.cached.emit(first_id, current)
    controller_harness.wait_until(
        lambda: current.path.absolute()
        in controller_harness.controller._ready_cache_paths
    )
    controller_harness.controller.record_open_source(
        current.path, current.source, expected=None)

    for index in range(20):
        pending = replace(
            current,
            path=controller_harness.tmp_path / f'pending-{index}.alog',
            sidecar_path=controller_harness.tmp_path / f'pending-{index}.json',
        )
        request_id = controller_harness.controller.open_cached(pending)
        controller_harness.command_vault.take(request_id)
        controller_harness.relay.cached.emit(request_id, pending)
        pending_path = pending.path.absolute()
        controller_harness.wait_until(
            partial(
                controller_harness.controller._ready_cache_paths.__contains__,
                pending_path,
            )
        )

    ready_paths = controller_harness.controller._ready_cache_paths
    assert len(ready_paths) <= 8
    assert current.path.absolute() in ready_paths


def test_online_cached_fallback_and_cancel_are_exact_request_generation(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    fallback_spy = QSignalSpy(controller_harness.controller.cachedFallbackReady)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    cached = cached_revision(controller_harness.tmp_path / 'fallback.alog')
    first_id = controller_harness.controller.open_roast(ROAST_UUID)
    first_online = cast(
        OnlineOpenRequest, controller_harness.command_vault.take(first_id)
    )

    controller_harness.relay.cached_fallback.emit(first_id, cached)

    assert controller_harness.wait_for_spy(fallback_spy) == [first_id, cached]
    controller_harness.controller.cancel_open(first_id)
    controller_harness.relay.staged.emit(
        first_id,
        replace(
            publish_request(controller_harness.tmp_path / 'cancelled.part'),
            token=first_online.token,
        ),
    )
    time.sleep(0.01)
    controller_harness.app.processEvents()
    assert len(ready) == 0
    assert not controller_harness.fake_worker.publish_ids

    second_id = controller_harness.controller.open_roast(ROAST_UUID)
    controller_harness.command_vault.take(second_id)
    controller_harness.controller.browse(ArchiveFilters(search='new revision'))
    controller_harness.relay.cached_fallback.emit(second_id, cached)
    assert controller_harness.wait_for_spy(fallback_spy, 1) == [second_id, cached]


def test_refresh_during_open_does_not_cancel_detail_authoritative_revision(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    open_id = controller_harness.controller.open_roast(ROAST_UUID)
    online = cast(OnlineOpenRequest, controller_harness.command_vault.take(open_id))
    refresh_id = controller_harness.controller.browse(
        ArchiveFilters(search='newer list')
    )
    staged = replace(
        publish_request(
            controller_harness.tmp_path / 'advanced.part',
            content=b'advanced-profile',
            number=2,
        ),
        token=online.token,
    )

    controller_harness.relay.staged.emit(open_id, staged)
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.publish_ids)
    )
    publish_id = controller_harness.fake_worker.publish_ids[-1]
    controller_harness.command_vault.take(publish_id)
    advanced = CachedRevision(
        namespace=namespace_for(ORIGIN, ORGANIZATION_ID),
        roast=summary_for(b'advanced-profile', number=2),
        revision=revision_for(b'advanced-profile', number=2),
        path=controller_harness.tmp_path / 'advanced.alog',
        sidecar_path=controller_harness.tmp_path / 'advanced.json',
        downloaded_at=NOW,
    )
    controller_harness.relay.published.emit(publish_id, advanced)

    payload = controller_harness.wait_for_spy(ready)
    source = cast(ServerProfileSource, payload[1])
    assert source.revision_number == 2
    assert not source.stale
    assert controller_harness.controller._known_cached_revisions[ROAST_UUID] == advanced
    assert refresh_id != open_id


def test_cancel_during_validation_discards_exact_stage_before_publication(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    open_id = controller_harness.controller.open_roast(ROAST_UUID)
    online = cast(OnlineOpenRequest, controller_harness.command_vault.take(open_id))
    staged = replace(
        publish_request(controller_harness.tmp_path / 'cancel-after-validation.part'),
        token=online.token,
    )
    controller_harness.validator_callback = (
        lambda _path: controller_harness.controller.cancel_open(open_id)
    )

    controller_harness.relay.staged.emit(open_id, staged)
    controller_harness.wait_until(
        lambda: str(staged.staged_path) in controller_harness.fake_worker.discard_paths
    )

    assert online.token.is_cancelled()
    assert controller_harness.fake_worker.publish_ids == []
    assert staged.staged_path not in (
        tracked.request.staged_path
        for tracked in controller_harness.controller._publish_requests.values()
    )


def test_cancel_after_publication_handoff_suppresses_open_completion(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    open_id = controller_harness.controller.open_roast(ROAST_UUID)
    online = cast(OnlineOpenRequest, controller_harness.command_vault.take(open_id))
    staged = replace(
        publish_request(controller_harness.tmp_path / 'linearized.part'),
        token=online.token,
    )
    controller_harness.relay.staged.emit(open_id, staged)
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.publish_ids)
    )
    publish_id = controller_harness.fake_worker.publish_ids[-1]
    controller_harness.command_vault.take(publish_id)

    controller_harness.controller.cancel_open(open_id)
    valid_cached = cached_revision(controller_harness.tmp_path / 'linearized.alog')
    controller_harness.relay.published.emit(publish_id, valid_cached)
    time.sleep(0.01)
    controller_harness.app.processEvents()

    assert online.token.is_cancelled()
    assert len(ready) == 0
    assert valid_cached.path not in controller_harness.controller._ready_cache_paths


def test_validation_precedes_publication_and_profile_ready(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    open_id = controller_harness.controller.open_roast(ROAST_UUID)
    online = cast(OnlineOpenRequest, controller_harness.command_vault.take(open_id))
    staged_path = controller_harness.tmp_path / 'hidden.part'
    request = replace(publish_request(staged_path), token=online.token)

    controller_harness.relay.staged.emit(open_id, request)
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.publish_ids)
    )
    assert controller_harness.validator_calls == [
        (staged_path, controller_harness.ui_thread_id)
    ]
    assert len(ready) == 0
    publish_id = controller_harness.fake_worker.publish_ids[-1]
    assert controller_harness.command_vault.take(publish_id) == request

    cached = cached_revision(controller_harness.tmp_path / 'published.alog')
    controller_harness.relay.published.emit(publish_id, cached)
    payload = controller_harness.wait_for_spy(ready)
    source = cast(ServerProfileSource, payload[1])
    assert payload[0] == str(cached.path)
    assert source.namespace == cached.namespace
    assert not source.stale


def test_validator_and_publish_vault_failures_explicitly_discard_stage(
    controller_harness: ControllerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    failed = QSignalSpy(controller_harness.controller.operationFailed)
    open_id = controller_harness.controller.open_roast(ROAST_UUID)
    online = cast(OnlineOpenRequest, controller_harness.command_vault.take(open_id))
    request = replace(
        publish_request(controller_harness.tmp_path / 'invalid.part'),
        token=online.token,
    )
    controller_harness.validator_failure = ValueError('profile parser detail')

    controller_harness.relay.staged.emit(open_id, request)
    payload = controller_harness.wait_for_spy(failed)
    controller_harness.wait_until(
        lambda: str(request.staged_path) in controller_harness.fake_worker.discard_paths
    )
    assert payload == [open_id, public_failure(FailureKind.INVALID_RESPONSE)]
    assert 'profile parser detail' not in repr(payload)

    controller_harness.validator_failure = None
    second_id = controller_harness.controller.open_roast(ROAST_UUID)
    second_online = cast(
        OnlineOpenRequest, controller_harness.command_vault.take(second_id)
    )
    second = replace(
        publish_request(controller_harness.tmp_path / 'vault-loss.part'),
        token=second_online.token,
    )
    original_put = controller_harness.command_vault.put

    def fail_put(_value: object) -> str:
        raise RuntimeError('opaque vault unavailable')

    monkeypatch.setattr(controller_harness.command_vault, 'put', fail_put)
    controller_harness.relay.staged.emit(second_id, second)
    controller_harness.wait_until(
        lambda: str(second.staged_path) in controller_harness.fake_worker.discard_paths
    )
    monkeypatch.setattr(controller_harness.command_vault, 'put', original_put)
    assert list(failed[-1]) == [
        second_id,
        public_failure(FailureKind.INVALID_RESPONSE),
    ]


def test_cached_open_is_stale_and_only_successful_open_paths_are_protected(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    cached = cached_revision(controller_harness.tmp_path / 'cached.alog')
    open_id = controller_harness.controller.open_cached(cached)
    controller_harness.command_vault.take(open_id)

    controller_harness.relay.cached.emit(open_id, cached)
    payload = controller_harness.wait_for_spy(ready)
    source = cast(ServerProfileSource, payload[1])
    assert payload == [str(cached.path), source]
    assert source.stale
    assert controller_harness.controller.is_expected_open_source(
        cached.path, source)
    assert not controller_harness.controller.is_expected_open_source(
        cached.path.with_name('other.alog'), source)
    assert not controller_harness.controller.is_expected_open_source(
        cached.path, replace(source, stale=False))
    assert controller_harness.controller._current_open_cache_path is None
    assert controller_harness.controller._current_open_cache_source is None

    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.clear_ids)
    )
    first_clear_id = controller_harness.fake_worker.clear_ids[-1]
    first_clear = cast(
        ClearUnusedRequest, controller_harness.command_vault.take(first_clear_id)
    )
    assert first_clear.namespace == cached.namespace

    before_protect = len(controller_harness.fake_worker.protect_ids)
    token = controller_harness.controller.record_open_source(
        cached.path, source, expected=None)
    assert token is not None
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.protect_ids) > before_protect
    )
    protected_id = controller_harness.fake_worker.protect_ids[-1]
    protected = cast(
        ProtectedPathsRequest,
        controller_harness.command_vault.take(protected_id),
    )
    assert protected.namespace == cached.namespace
    assert protected.open_paths == frozenset({cached.path.absolute()})
    before = len(controller_harness.fake_worker.clear_ids)
    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.clear_ids) > before
    )
    second_clear_id = controller_harness.fake_worker.clear_ids[-1]
    second_clear = cast(
        ClearUnusedRequest, controller_harness.command_vault.take(second_clear_id)
    )
    assert second_clear.namespace == cached.namespace

    before_protect = len(controller_harness.fake_worker.protect_ids)
    controller_harness.controller.record_local_save(
        controller_harness.tmp_path / 'local.alog', expected=token
    )
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.protect_ids) > before_protect
    )
    released = cast(
        ProtectedPathsRequest,
        controller_harness.command_vault.take(
            controller_harness.fake_worker.protect_ids[-1]
        ),
    )
    assert released.open_paths == frozenset()
    before = len(controller_harness.fake_worker.clear_ids)
    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.clear_ids) > before
    )
    third_clear_id = controller_harness.fake_worker.clear_ids[-1]
    third_clear = cast(
        ClearUnusedRequest, controller_harness.command_vault.take(third_clear_id)
    )
    assert third_clear.namespace == cached.namespace


def test_protection_updates_are_synchronous_and_restore_exact_previous_token(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    cached = cached_revision(controller_harness.tmp_path / 'transactional-open.alog')
    open_id = controller_harness.controller.open_cached(cached)
    controller_harness.command_vault.take(open_id)
    controller_harness.relay.cached.emit(open_id, cached)
    source = cast(ServerProfileSource, controller_harness.wait_for_spy(ready)[1])

    token = controller_harness.controller.record_open_source(
        cached.path, source, expected=None)

    assert token is not None
    assert controller_harness.controller.current_protection_token() is token
    assert controller_harness.controller.owns_protection_token(token)
    assert not controller_harness.controller.owns_protection_token(
        replace(token, serial=token.serial + 1))
    assert controller_harness.controller.record_open_source(
        cached.path, replace(source, stale=False), expected=token) is None
    assert controller_harness.controller.current_protection_token() is token

    released = controller_harness.controller.record_local_save(
        controller_harness.tmp_path / 'local.alog', expected=token)
    assert released is token
    assert controller_harness.controller.current_protection_token() is None
    assert controller_harness.controller.restore_protection(token, None)
    assert controller_harness.controller.current_protection_token() is token
    assert controller_harness.controller._current_open_cache_path == cached.path.absolute()
    assert controller_harness.controller._current_open_cache_source == source


def test_namespace_transition_releases_then_restores_current_open_protection(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    cached = cached_revision(controller_harness.tmp_path / 'namespace-open.alog')
    open_id = controller_harness.controller.open_cached(cached)
    controller_harness.command_vault.take(open_id)
    controller_harness.relay.cached.emit(open_id, cached)
    source = cast(
        ServerProfileSource,
        controller_harness.wait_for_spy(ready)[1],
    )
    controller_harness.controller.record_open_source(
        cached.path, source, expected=None)
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.protect_ids)
    )

    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.relay.connection.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )
    before = len(controller_harness.fake_worker.protect_ids)
    controller_harness.relay.committed.emit(request_id, OTHER_IDENTITY)
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.protect_ids) > before
    )
    switched = cast(
        ProtectedPathsRequest,
        controller_harness.command_vault.take(
            controller_harness.fake_worker.protect_ids[-1]
        ),
    )
    assert switched.namespace == namespace_for(ORIGIN, OTHER_ORGANIZATION_ID)
    assert switched.open_paths == frozenset()

    before = len(controller_harness.fake_worker.protect_ids)
    controller_harness.relay.failure.emit(
        request_id,
        public_failure(FailureKind.CREDENTIAL_REJECTED),
    )
    controller_harness.wait_until(
        lambda: controller_harness.settings_store.load().pending_connection is None
        and len(controller_harness.fake_worker.protect_ids) > before
    )
    restored = cast(
        ProtectedPathsRequest,
        controller_harness.command_vault.take(
            controller_harness.fake_worker.protect_ids[-1]
        ),
    )
    assert restored.namespace == cached.namespace
    assert restored.open_paths == frozenset({cached.path.absolute()})


def test_stale_or_forged_open_results_never_publish_or_protect_paths(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    forged = cached_revision(
        controller_harness.tmp_path / 'other.alog',
        organization_id=OTHER_ORGANIZATION_ID,
    )

    controller_harness.relay.cached.emit('f' * 32, forged)
    time.sleep(0.01)
    controller_harness.app.processEvents()
    assert len(ready) == 0
    controller_harness.controller.record_open_source(
        forged.path, forged.source, expected=None)
    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.clear_ids)
    )
    clear_id = controller_harness.fake_worker.clear_ids[-1]
    request = cast(ClearUnusedRequest, controller_harness.command_vault.take(clear_id))
    assert request.namespace == namespace_for(ORIGIN, ORGANIZATION_ID)


def test_start_is_idempotent_and_controller_rejects_cross_thread_calls(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.controller.start()
    controller_harness.controller.start()
    assert controller_harness.fake_worker.start_count == 1
    errors: list[BaseException] = []
    finished = threading.Event()

    class Caller(QObject):
        @pyqtSlot()
        def call(self) -> None:
            try:
                controller_harness.controller.refresh_queue()
            except BaseException as error:  # test captures the boundary result
                errors.append(error)
            finally:
                finished.set()

    thread = QThread()
    caller = Caller()
    caller.moveToThread(thread)
    thread.started.connect(caller.call)
    thread.start()
    try:
        assert finished.wait(timeout=2)
        assert len(errors) == 1 and isinstance(errors[0], ControllerError)
    finally:
        thread.quit()
        assert thread.wait(2_000)


def test_shutdown_before_keyring_commit_preserves_pending_journal(
    controller_harness: ControllerHarness,
) -> None:
    request_id = controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.test_ids
    )
    controller_harness.fake_worker.block_tests = False
    controller_harness.relay.connection.emit(request_id, IDENTITY)
    controller_harness.wait_until(
        lambda: request_id in controller_harness.fake_worker.commit_ids
    )

    assert controller_harness.controller.shutdown(2_000)

    pending = controller_harness.settings_store.load()
    assert pending.identity is None
    assert pending.pending_connection is not None
    assert pending.pending_connection.identity == IDENTITY
    assert controller_harness.credentials.set_calls == []


def test_shutdown_requests_interruption_then_queued_stop_without_terminate(
    controller_harness: ControllerHarness,
) -> None:
    assert controller_harness.controller.shutdown(2_000)

    assert controller_harness.fake_worker.stop_count == 1
    assert controller_harness.fake_worker.interrupted_on_stop
    assert controller_harness.fake_worker.stop_thread == controller_harness.fake_worker.start_thread
    assert not controller_harness.controller.worker_thread_running
    assert controller_harness.controller.shutdown(2_000)


def test_shutdown_timeout_is_bounded_and_worker_can_finish_later(
    controller_harness: ControllerHarness,
) -> None:
    worker = controller_harness.fake_worker
    destroyed = QSignalSpy(worker.destroyed)
    worker.block_tests = True
    controller_harness.controller.test_connection(
        ORIGIN, controller_harness.ephemeral_secret
    )
    assert worker.test_entered.wait(timeout=2)

    started = time.monotonic()
    assert not controller_harness.controller.shutdown(25)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert controller_harness.controller.worker_thread_running
    assert controller_harness.secret_vault.size() == 0
    assert controller_harness.profile_vault.size() == 0
    assert controller_harness.command_vault.size() == 0
    assert_secret_absent(
        controller_harness.ephemeral_secret, controller_harness.controller
    )

    worker.test_release.set()
    controller_harness.wait_until(lambda: worker.stop_count == 1)
    controller_harness.wait_until(
        lambda: not controller_harness.controller.worker_thread_running
    )
    controller_harness.wait_until(lambda: len(destroyed) == 1)
    assert controller_harness.controller.shutdown(2_000)


class RecordingOutbox(Outbox):
    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        super().__init__(root, clock)
        self.open_threads: list[int] = []
        self.resume_calls: list[Namespace] = []
        self.leased: list[Job] = []

    @override
    def open(self) -> None:
        self.open_threads.append(int(QThread.currentThreadId()))
        super().open()

    @override
    def resume_namespace(self, namespace: Namespace, now: datetime) -> int:
        self.resume_calls.append(namespace)
        return super().resume_namespace(namespace, now)

    @override
    def lease_next(
        self, namespace: Namespace, now: datetime, lease_seconds: int = 60
    ) -> Job | None:
        outcome = super().lease_next(namespace, now, lease_seconds)
        if isinstance(outcome, Job):
            self.leased.append(outcome)
        return outcome

    @property
    def closed(self) -> bool:
        return self._connection is None


class RecordingCache(CacheStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.open_threads: list[int] = []

    @override
    def open(self) -> None:
        self.open_threads.append(int(QThread.currentThreadId()))
        super().open()

    @property
    def closed(self) -> bool:
        return self._closed and not self._opened and not self._staging


def _wait_for_qt(
    app: QCoreApplication,
    predicate: Callable[[], bool],
    message: str,
    *,
    timeout: float = 3,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.001)
    app.processEvents()


def _spy_matches(
    spy: QSignalSpy,
    predicate: Callable[[list[object]], bool],
) -> bool:
    return any(predicate(list(spy[index])) for index in range(len(spy)))


def _seed_crash_cut_jobs(data_root: Path, settings: SettingsStore) -> None:
    outbox = Outbox(data_root / 'outbox', lambda: NOW)
    outbox.open()
    try:
        client_uuid = settings.load().client_instance_uuid
        for identity, roast_uuid in (
            (IDENTITY, ROAST_UUID),
            (
                OTHER_IDENTITY,
                UUID('77777777-7777-4777-8777-777777777777'),
            ),
        ):
            namespace = namespace_for(ORIGIN, identity.organization.id)
            profile = ProfileData(
                roastUUID=str(roast_uuid),
                title=f'crash-cut-{identity.organization.id.hex}',
            )
            content = repr(dict(profile)).encode('utf-8')
            snapshot = outbox.snapshot_bytes(namespace, content, NOW)
            outbox.enqueue(
                namespace,
                snapshot,
                roast_uuid,
                project_profile(profile, NOW),
                client_uuid,
            )
    finally:
        outbox.close()


def _blocked_first_activation(
    tmp_path: Path,
    app: QCoreApplication,
    name: str,
    *,
    seed_jobs: bool = False,
) -> tuple[
    RoastServerController,
    SettingsStore,
    FakeCredentialStore,
    ActivationShutdownRecorder,
    str,
]:
    settings = SettingsStore(
        QSettings(str(tmp_path / f'{name}.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    credentials = FakeCredentialStore()
    candidate = secrets.token_urlsafe(32)
    recorder = ActivationShutdownRecorder(candidate)
    data_root = tmp_path / f'{name}-data'
    if seed_jobs:
        _seed_crash_cut_jobs(data_root, settings)
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=data_root,
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    controller.start()
    transaction_id = controller.test_connection(ORIGIN, candidate)
    _wait_for_qt(
        app,
        recorder.final_auth_entered.is_set,
        'activation did not reach blocked final authentication',
    )
    journaled = settings.load()
    assert journaled.identity is None
    assert journaled.pending_connection is not None
    assert journaled.pending_connection.identity == IDENTITY
    assert transaction_id in controller._activation_previous
    return controller, settings, credentials, recorder, candidate


def _assert_first_activation_restart_matches(
    tmp_path: Path,
    app: QCoreApplication,
    name: str,
    settings: SettingsStore,
    credentials: FakeCredentialStore,
    recorder: ActivationShutdownRecorder,
    candidate: str,
    *,
    accepted: bool,
) -> None:
    restarted = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / f'{name}-data',
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    restarted.start()
    try:
        if accepted:
            _wait_for_qt(
                app,
                lambda: restarted._identity == IDENTITY,
                'restart did not validate accepted candidate settings and keyring',
            )
            assert credentials.values.get(ORIGIN) == candidate
            assert settings.load().identity == IDENTITY
        else:
            for _ in range(20):
                app.processEvents()
                time.sleep(0.001)
            assert restarted._identity is None
            assert ORIGIN not in credentials.values
            assert settings.load().identity is None
    finally:
        assert restarted.shutdown(2_000)


def test_real_shutdown_after_keyring_commit_preserves_journal_and_candidate(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    controller, settings, credentials, recorder, candidate = (
        _blocked_first_activation(tmp_path, qcoreapplication, 'shutdown-before-final')
    )
    try:
        assert not controller.shutdown(10)
        pending = settings.load()
        assert pending.identity is None
        assert pending.pending_connection is not None
        recorder.release_final_auth.set()
        _wait_for_qt(
            qcoreapplication,
            lambda: not controller.worker_thread_running,
            'worker did not stop after blocked final authentication returned',
        )
        assert controller.shutdown(2_000)
        _assert_first_activation_restart_matches(
            tmp_path,
            qcoreapplication,
            'shutdown-before-final',
            settings,
            credentials,
            recorder,
            candidate,
            accepted=True,
        )
    finally:
        recorder.release_final_auth.set()
        if controller.worker_thread_running:
            assert controller.shutdown(2_000)


def test_real_shutdown_after_final_auth_emission_recovers_committed_candidate(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    controller, settings, credentials, recorder, candidate = (
        _blocked_first_activation(tmp_path, qcoreapplication, 'shutdown-before-process')
    )
    emitted = threading.Event()
    direct_connect = cast(
        Callable[[Callable[[str, object], None], Qt.ConnectionType], object],
        controller._worker.connectionActivated.connect,
    )
    direct_connect(
        lambda _transaction_id, _identity: emitted.set(),
        Qt.ConnectionType.DirectConnection,
    )
    identities = QSignalSpy(controller.identityChanged)
    try:
        recorder.release_final_auth.set()
        assert emitted.wait(timeout=2)
        assert controller._activation_previous
        assert controller.shutdown(2_000)
        for _ in range(20):
            qcoreapplication.processEvents()
        assert all(list(identities[index]) != [IDENTITY] for index in range(len(identities)))
        _assert_first_activation_restart_matches(
            tmp_path,
            qcoreapplication,
            'shutdown-before-process',
            settings,
            credentials,
            recorder,
            candidate,
            accepted=True,
        )
    finally:
        recorder.release_final_auth.set()
        if controller.worker_thread_running:
            assert controller.shutdown(2_000)


def test_real_shutdown_after_acceptance_executes_queued_ack_before_stop(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    controller, settings, credentials, recorder, candidate = (
        _blocked_first_activation(tmp_path, qcoreapplication, 'shutdown-after-accept')
    )
    activation_blocked = threading.Event()
    release_activation = threading.Event()

    def block_after_emit(_transaction_id: str, _identity: object) -> None:
        activation_blocked.set()
        if not release_activation.wait(timeout=5):
            raise RuntimeError('accepted activation blocker timed out')

    direct_connect = cast(
        Callable[[Callable[[str, object], None], Qt.ConnectionType], object],
        controller._worker.connectionActivated.connect,
    )
    direct_connect(block_after_emit, Qt.ConnectionType.DirectConnection)
    try:
        recorder.release_final_auth.set()
        assert activation_blocked.wait(timeout=2)
        _wait_for_qt(
            qcoreapplication,
            lambda: controller._identity == IDENTITY
            and not controller._activation_previous,
            'controller did not accept activation while worker was blocked',
        )
        assert controller._worker._credential_transactions
        assert settings.load().pending_connection is None
        assert credentials.values.get(ORIGIN) == candidate
        assert not controller.shutdown(10)
        assert settings.load().identity == IDENTITY
        release_activation.set()
        _wait_for_qt(
            qcoreapplication,
            lambda: not controller.worker_thread_running,
            'acknowledgement and stop did not drain in sender order',
        )
        assert controller.shutdown(2_000)
        _assert_first_activation_restart_matches(
            tmp_path,
            qcoreapplication,
            'shutdown-after-accept',
            settings,
            credentials,
            recorder,
            candidate,
            accepted=True,
        )
    finally:
        recorder.release_final_auth.set()
        release_activation.set()
        if controller.worker_thread_running:
            assert controller.shutdown(2_000)


def test_real_shutdown_after_worker_ack_keeps_finalized_candidate(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    controller, settings, credentials, recorder, candidate = (
        _blocked_first_activation(tmp_path, qcoreapplication, 'shutdown-post-ack')
    )
    try:
        recorder.release_final_auth.set()
        _wait_for_qt(
            qcoreapplication,
            lambda: settings.load().identity == IDENTITY
            and settings.load().pending_connection is None
            and not controller._worker._credential_transactions,
            'activation acknowledgement did not finalize the candidate',
        )
        assert controller.shutdown(2_000)
        _assert_first_activation_restart_matches(
            tmp_path,
            qcoreapplication,
            'shutdown-post-ack',
            settings,
            credentials,
            recorder,
            candidate,
            accepted=True,
        )
    finally:
        recorder.release_final_auth.set()
        if controller.worker_thread_running:
            assert controller.shutdown(2_000)


def test_real_rollback_failure_keeps_journal_and_restarts_without_cross_org(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    controller, settings, credentials, recorder, candidate = (
        _blocked_first_activation(
            tmp_path,
            qcoreapplication,
            'rollback-failure-recovery',
            seed_jobs=True,
        )
    )
    failed = QSignalSpy(controller.operationFailed)
    try:
        credentials.failure = CredentialStoreError('fixed fake failure')
        controller.invalidate_connection_proof()
        recorder.release_final_auth.set()
        _wait_for_qt(
            qcoreapplication,
            lambda: _spy_matches(
                failed,
                lambda payload: payload
                == [
                    next(iter(controller._activation_previous)),
                    public_failure(FailureKind.KEYRING),
                ],
            ),
            'rollback failure was not reported',
        )
        unresolved = settings.load()
        assert unresolved.pending_connection is not None
        assert unresolved.identity is None
        assert credentials.values.get(ORIGIN) == candidate
        assert controller.shutdown(2_000)

        credentials.failure = None
        _assert_first_activation_restart_matches(
            tmp_path,
            qcoreapplication,
            'rollback-failure-recovery',
            settings,
            credentials,
            recorder,
            candidate,
            accepted=True,
        )
        assert recorder.api_calls.count('post_aroast') == 0
        assert recorder.api_calls.count('upload_revision') == 0
    finally:
        credentials.failure = None
        recorder.release_final_auth.set()
        if controller.worker_thread_running:
            assert controller.shutdown(2_000)


def test_shutdown_never_attempts_cross_store_settings_rollback(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, settings, _credentials, recorder, _candidate = (
        _blocked_first_activation(
            tmp_path,
            qcoreapplication,
            'shutdown-restore-failure',
            seed_jobs=True,
        )
    )
    restore_calls: list[ConnectorSettings] = []

    def reject_restore(previous: ConnectorSettings) -> ConnectorSettings:
        restore_calls.append(previous)
        raise SettingsError('backend detail')

    monkeypatch.setattr(settings, 'restore_connection_state', reject_restore)
    try:
        assert not controller.shutdown(10)
        assert restore_calls == []
        pending = settings.load()
        assert pending.pending_connection is not None
        assert not pending.enabled and not pending.automatic_upload
        recorder.release_final_auth.set()
        _wait_for_qt(
            qcoreapplication,
            lambda: not controller.worker_thread_running,
            'journal-preserving shutdown worker did not stop',
        )
        assert settings.load().pending_connection is not None
    finally:
        recorder.release_final_auth.set()
        if controller.worker_thread_running:
            assert controller.shutdown(2_000)


@pytest.mark.parametrize(
    ('cut', 'active_identity', 'pending_identity', 'keyring_identity'),
    [
        ('candidate-tested', IDENTITY, None, IDENTITY),
        ('pending-settings-durable', IDENTITY, OTHER_IDENTITY, IDENTITY),
        ('candidate-keyring-durable', IDENTITY, OTHER_IDENTITY, OTHER_IDENTITY),
        ('active-settings-durable', OTHER_IDENTITY, None, OTHER_IDENTITY),
        ('final-auth-returned', OTHER_IDENTITY, None, OTHER_IDENTITY),
        ('active-keyring-mismatch', IDENTITY, None, OTHER_IDENTITY),
    ],
)
def test_real_restart_at_every_credential_activation_cut_never_uploads(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
    cut: str,
    active_identity: ServerIdentity,
    pending_identity: ServerIdentity | None,
    keyring_identity: ServerIdentity,
) -> None:
    qsettings = QSettings(
        str(tmp_path / f'crash-{cut}.ini'), QSettings.Format.IniFormat
    )
    settings = SettingsStore(qsettings)
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, active_identity)
    mismatch = cut == 'active-keyring-mismatch'
    settings.save_options(
        mismatch,
        mismatch,
        64 * 1024 * 1024,
    )
    if pending_identity is not None:
        settings.save_pending_connection(ORIGIN, pending_identity)

    credential_a = secrets.token_urlsafe(32)
    credential_b = secrets.token_urlsafe(32)
    credentials = FakeCredentialStore()
    credentials.values[ORIGIN] = (
        credential_a if keyring_identity == IDENTITY else credential_b
    )
    recorder = RestartAuthenticationRecorder(credential_a, credential_b)
    data_root = tmp_path / f'crash-data-{cut}'
    _seed_crash_cut_jobs(data_root, settings)
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=data_root,
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    controller.start()
    try:
        _wait_for_qt(
            qcoreapplication,
            lambda: bool(recorder.authenticated_organizations),
            'restart did not authenticate the persisted credential',
        )
        if pending_identity is not None or mismatch:
            _wait_for_qt(
                qcoreapplication,
                lambda: settings.load().pending_connection is None
                and not settings.load().automatic_upload,
                'restart did not settle the interrupted activation safely',
            )
        for _ in range(20):
            qcoreapplication.processEvents()
            time.sleep(0.001)
        assert recorder.upload_calls == 0
        assert not settings.load().automatic_upload
        assert_secret_absent(credential_a, controller)
        assert_secret_absent(credential_b, controller)
    finally:
        assert controller.shutdown(2_000)
    assert recorder.upload_calls == 0


def test_real_blocked_startup_edit_fences_auth_resume_timer_and_due_delivery(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'blocked-validation.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(True, True, 64 * 1024 * 1024)
    data_root = tmp_path / 'blocked-validation-data'
    _seed_crash_cut_jobs(data_root, settings)
    credentials = FakeCredentialStore()
    persisted_secret = secrets.token_urlsafe(32)
    credentials.values[ORIGIN] = persisted_secret
    recorder = SupersessionAuthenticationRecorder()
    outboxes: list[RecordingOutbox] = []

    def outbox_factory(root: Path, clock: Callable[[], datetime]) -> RecordingOutbox:
        outbox = RecordingOutbox(root, clock)
        outboxes.append(outbox)
        return outbox

    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=data_root,
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        outbox_factory=outbox_factory,
        clock=lambda: NOW,
    )
    validated = QSignalSpy(controller._worker.configurationValidated)
    identities = QSignalSpy(controller.identityChanged)
    blocker = WorkerQueueBlocker()
    blocker.moveToThread(controller._thread)
    relay = WorkerBlockRelay()
    block_connect = cast(
        Callable[[Callable[[], None], Qt.ConnectionType], object],
        relay.block.connect,
    )
    block_connect(blocker.block, Qt.ConnectionType.QueuedConnection)
    stop_connect = cast(
        Callable[[Callable[[], None], Qt.ConnectionType], object],
        controller._worker.stopped.connect,
    )
    stop_connect(blocker.deleteLater, Qt.ConnectionType.DirectConnection)
    controller.start()
    assert recorder.first_entered.wait(timeout=2)
    try:
        relay.block.emit()
        controller.invalidate_connection_proof()
        recorder.release_first.set()
        assert blocker.entered.wait(timeout=2)

        namespace = namespace_for(ORIGIN, ORGANIZATION_ID)
        configuration = controller._worker._configuration
        assert configuration is not None and configuration.enabled
        assert controller._worker._credential is None
        assert controller._worker._authorized_target is None
        assert outboxes[0].resume_calls == []
        assert outboxes[0].leased == []
        assert outboxes[0].counts(namespace).paused == 1
        assert recorder.http_digests and len(recorder.http_digests) == 1
        assert recorder.delivery_calls == []
        assert len(validated) == 0
        assert controller._worker._timer is not None
        assert not controller._worker._timer.isActive()

        blocker.release.set()
        _wait_for_qt(
            qcoreapplication,
            lambda: controller._worker._configuration is not None
            and not controller._worker._configuration.enabled,
            'worker did not install the revoked disabled configuration',
        )
        for _ in range(20):
            qcoreapplication.processEvents()
            time.sleep(0.001)

        assert len(validated) == 0
        assert all(
            list(identities[index]) != [IDENTITY]
            for index in range(len(identities))
        )
        assert outboxes[0].resume_calls == []
        assert outboxes[0].leased == []
        assert recorder.delivery_calls == []
        with pytest.raises(ControllerError, match='Test the connection'):
            controller.apply_options(
                ORIGIN,
                enabled=True,
                automatic_upload=True,
                cache_limit_bytes=64 * 1024 * 1024,
            )
        assert not settings.load().automatic_upload
        assert_secret_absent(persisted_secret, controller)
        assert_secret_absent(persisted_secret, recorder)
    finally:
        recorder.release_first.set()
        blocker.release.set()
        assert controller.shutdown(2_000)


def test_real_blocked_worker_many_tests_only_authenticate_newest_credential(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'delayed-tests.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    credentials = FakeCredentialStore()
    persisted_secret = secrets.token_urlsafe(32)
    credentials.values[ORIGIN] = persisted_secret
    recorder = DigestAuthenticationRecorder()
    outboxes: list[BlockingOpenOutbox] = []

    class BlockingOpenOutbox(RecordingOutbox):
        def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
            super().__init__(root, clock)
            self.entered = threading.Event()
            self.release = threading.Event()

        @override
        def open(self) -> None:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError('blocked worker startup timed out')
            super().open()

    def outbox_factory(root: Path, clock: Callable[[], datetime]) -> RecordingOutbox:
        outbox = BlockingOpenOutbox(root, clock)
        outboxes.append(outbox)
        return outbox

    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'delayed-tests-data',
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        outbox_factory=outbox_factory,
        clock=lambda: NOW,
    )
    direct_connect = cast(
        Callable[[Callable[[str, object], None], Qt.ConnectionType], object],
        controller._worker.connectionTested.connect,
    )
    direct_connect(recorder.record_tested, Qt.ConnectionType.DirectConnection)
    controller.start()
    assert outboxes[0].entered.wait(timeout=2)
    candidates = [secrets.token_urlsafe(32) for _ in range(10)]
    candidate_digests = [
        hashlib.sha256(candidate.encode('utf-8')).hexdigest()
        for candidate in candidates
    ]
    request_ids: list[str] = []
    try:
        for candidate in candidates:
            request_ids.append(controller.test_connection(ORIGIN, candidate))

        assert controller._credential_vault.size() == 1
        assert all(
            not controller._credential_vault.contains(request_id)
            for request_id in request_ids[:-1]
        )
        assert controller._credential_vault.contains(request_ids[-1])
        assert controller._worker._credential_transactions == {}

        outboxes[0].release.set()
        recorder.wait_for_tests(1)
        assert recorder.tested_ids == [request_ids[-1]]
        assert recorder.digests == [candidate_digests[-1]]
        assert controller._credential_vault.size() == 0
        assert tuple(controller._worker._credential_transactions) == (
            request_ids[-1],
        )
        assert len(credentials.get_calls) == 1
        assert credentials.set_calls == []
        assert credentials.delete_calls == []
        assert credentials.values[ORIGIN] == persisted_secret

        _wait_for_qt(
            qcoreapplication,
            lambda: settings.load().identity == IDENTITY
            and not controller._worker._credential_transactions,
            'newest blocked-worker transaction did not activate and acknowledge',
        )
        assert controller._worker._credential_transactions == {}
        assert [call[1] for call in credentials.set_calls] == [candidate_digests[-1]]
        assert credentials.delete_calls == []
        assert recorder.digests == [candidate_digests[-1]] * 3
        for candidate in candidates:
            assert_secret_absent(candidate, controller)
            assert_secret_absent(candidate, recorder)
    finally:
        outboxes[0].release.set()
        assert controller.shutdown(2_000)


def test_real_mid_http_supersession_discards_old_response_before_keyring(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'mid-http.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    credentials = FakeCredentialStore()
    persisted_secret = secrets.token_urlsafe(32)
    credentials.values[ORIGIN] = persisted_secret
    recorder = SupersessionAuthenticationRecorder()
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'mid-http-data',
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    direct_connect = cast(
        Callable[[Callable[[str, object], None], Qt.ConnectionType], object],
        controller._worker.connectionTested.connect,
    )
    direct_connect(recorder.record_tested, Qt.ConnectionType.DirectConnection)
    controller.start()
    first_candidate = secrets.token_urlsafe(32)
    newest_candidate = secrets.token_urlsafe(32)
    first_digest = hashlib.sha256(first_candidate.encode('utf-8')).hexdigest()
    newest_digest = hashlib.sha256(newest_candidate.encode('utf-8')).hexdigest()
    try:
        first_id = controller.test_connection(ORIGIN, first_candidate)
        assert recorder.first_entered.wait(timeout=2)
        assert credentials.get_calls == []

        newest_id = controller.test_connection(ORIGIN, newest_candidate)
        assert controller._credential_vault.size() == 1
        assert not controller._credential_vault.contains(first_id)
        assert controller._credential_vault.contains(newest_id)
        recorder.release_first.set()
        recorder.wait_for_tested(1)

        assert recorder.http_digests == [first_digest, newest_digest]
        assert recorder.tested_ids == [newest_id]
        assert tuple(controller._worker._credential_transactions) == (newest_id,)
        assert len(credentials.get_calls) == 1
        assert credentials.set_calls == []
        assert credentials.delete_calls == []
        assert credentials.values[ORIGIN] == persisted_secret

        _wait_for_qt(
            qcoreapplication,
            lambda: settings.load().identity == IDENTITY
            and not controller._worker._credential_transactions,
            'newest mid-HTTP transaction did not activate and acknowledge',
        )
        assert controller._worker._credential_transactions == {}
        assert [call[1] for call in credentials.set_calls] == [newest_digest]
        assert credentials.delete_calls == []
        assert recorder.http_digests == [
            first_digest,
            newest_digest,
            newest_digest,
            newest_digest,
        ]
        assert_secret_absent(first_candidate, controller)
        assert_secret_absent(newest_candidate, controller)
        assert_secret_absent(first_candidate, recorder)
        assert_secret_absent(newest_candidate, recorder)
    finally:
        recorder.release_first.set()
        assert controller.shutdown(2_000)


def test_real_restart_cross_origin_pre_keyring_cut_restores_active_disabled(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'cross-origin-cut.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(True, True, 64 * 1024 * 1024)
    settings.save_pending_connection(OTHER_ORIGIN, OTHER_IDENTITY)
    credential_a = secrets.token_urlsafe(32)
    credential_b = secrets.token_urlsafe(32)
    credentials = FakeCredentialStore()
    credentials.values[ORIGIN] = credential_a
    recorder = RestartAuthenticationRecorder(credential_a, credential_b)
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'cross-origin-cut-data',
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    controller.start()
    try:
        _wait_for_qt(
            qcoreapplication,
            lambda: settings.load().pending_connection is None,
            'cross-origin pre-keyring restart did not clear its journal',
        )
        _wait_for_qt(
            qcoreapplication,
            lambda: controller._identity == IDENTITY,
            'prior active connection was not startup validated after recovery',
        )

        recovered = settings.load()
        assert recovered.origin == ORIGIN
        assert recovered.identity == IDENTITY
        assert recovered.pending_connection is None
        assert not recovered.enabled and not recovered.automatic_upload
        controller.apply_options(
            ORIGIN,
            enabled=False,
            automatic_upload=False,
            cache_limit_bytes=64 * 1024 * 1024,
        )
        for _ in range(20):
            qcoreapplication.processEvents()
            time.sleep(0.001)
        assert recorder.client_origins
        assert set(recorder.client_origins) == {ORIGIN}
        assert set(recorder.authenticated_organizations) == {ORGANIZATION_ID}
        assert recorder.upload_calls == 0
        assert any(call[0] == OTHER_ORIGIN for call in credentials.get_calls)
        assert credentials.set_calls == []
        assert credentials.delete_calls == []
    finally:
        assert controller.shutdown(2_000)


def test_real_restart_first_activation_pre_keyring_cut_recovers_and_can_retest(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'first-cut.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_pending_connection(ORIGIN, IDENTITY)
    credentials = FakeCredentialStore()
    candidate = secrets.token_urlsafe(32)
    unused = secrets.token_urlsafe(32)
    recorder = RestartAuthenticationRecorder(candidate, unused)
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'first-cut-data',
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    failed = QSignalSpy(controller.operationFailed)
    identities = QSignalSpy(controller.identityChanged)
    controller.start()
    try:
        _wait_for_qt(
            qcoreapplication,
            lambda: settings.load().pending_connection is None,
            'pre-keyring restart did not durably clear pending state',
        )
        recovered = settings.load()
        assert recovered.identity is None
        assert not recovered.enabled and not recovered.automatic_upload
        assert recorder.authenticated_organizations == []
        assert _spy_matches(
            failed,
            lambda payload: isinstance(payload[0], str)
            and len(payload[0]) == 32
            and payload[1] == public_failure(FailureKind.CREDENTIAL_REJECTED),
        )

        request_id = controller.test_connection(ORIGIN, candidate)
        _wait_for_qt(
            qcoreapplication,
            lambda: settings.load().identity == IDENTITY,
            'clean recovery could not activate a later credential',
        )
        _wait_for_qt(
            qcoreapplication,
            lambda: _spy_matches(
                identities, lambda payload: payload[0] == IDENTITY
            ),
            'later activation did not establish connection proof',
        )
        assert request_id
        activated = settings.load()
        assert activated.pending_connection is None
        assert not activated.enabled and not activated.automatic_upload
        assert recorder.upload_calls == 0
        assert_secret_absent(candidate, controller)
        assert_secret_absent(unused, controller)
    finally:
        assert controller.shutdown(2_000)


def test_real_startup_offline_authentication_still_enqueues_but_never_uploads(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'offline.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(True, True, 64 * 1024 * 1024)
    credential_a = secrets.token_urlsafe(32)
    credential_b = secrets.token_urlsafe(32)
    credentials = FakeCredentialStore()
    credentials.values[ORIGIN] = credential_a
    recorder = RestartAuthenticationRecorder(
        credential_a,
        credential_b,
        offline=True,
    )
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'offline-data',
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    changed = QSignalSpy(controller.queueChanged)
    controller.start()
    try:
        _wait_for_qt(
            qcoreapplication,
            lambda: bool(recorder.authenticated_organizations),
            'startup did not attempt credential authentication',
        )
        controller.saved_profile(
            PROFILE_BYTES,
            ProfileData(roastUUID=str(ROAST_UUID), title='offline queue'),
            NOW,
        )
        _wait_for_qt(
            qcoreapplication,
            lambda: _spy_matches(
                changed,
                lambda payload: isinstance(payload[0], QueueCounts)
                and payload[0].paused == 1,
            ),
            'offline save was not durably paused',
        )
        assert recorder.upload_calls == 0
        assert settings.load().automatic_upload
    finally:
        assert controller.shutdown(2_000)
    assert recorder.upload_calls == 0


def test_real_settings_failure_keeps_seeded_work_paused_without_upload(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'real-settings.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(False, False, 64 * 1024 * 1024)
    credential_a = secrets.token_urlsafe(32)
    credential_b = secrets.token_urlsafe(32)
    credentials = FakeCredentialStore()
    credentials.values[ORIGIN] = credential_a
    recorder = RestartAuthenticationRecorder(credential_a, credential_b)
    data_root = tmp_path / 'real-settings-data'
    _seed_crash_cut_jobs(data_root, settings)
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=data_root,
        client_factory=cast(ClientFactory, recorder),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    identities = QSignalSpy(controller.identityChanged)
    failed = QSignalSpy(controller.operationFailed)
    controller.start()
    try:
        _wait_for_qt(
            qcoreapplication,
            lambda: _spy_matches(
                identities, lambda payload: payload[0] == IDENTITY
            ),
            'real worker did not validate the persisted configuration',
        )

        def reject_save(
            _enabled: bool,
            _automatic_upload: bool,
            _cache_limit_bytes: int,
        ) -> object:
            raise SettingsError('backend detail')

        monkeypatch.setattr(settings, 'save_options', reject_save)
        with pytest.raises(ControllerError) as raised:
            controller.apply_options(
                ORIGIN,
                enabled=True,
                automatic_upload=True,
                cache_limit_bytes=64 * 1024 * 1024,
            )
        _wait_for_qt(
            qcoreapplication,
            lambda: _spy_matches(
                failed,
                lambda payload: payload[0] == 'settings'
                and payload[1] == public_failure(FailureKind.SETTINGS),
            ),
            'real controller did not report fixed settings failure',
        )
        for _ in range(20):
            qcoreapplication.processEvents()
            time.sleep(0.001)
        assert raised.value.args == (SETTINGS_FAILURE_MESSAGE,)
        assert recorder.upload_calls == 0
    finally:
        assert controller.shutdown(2_000)
    assert recorder.upload_calls == 0


def test_real_worker_opens_outbox_and_cache_only_after_moving_threads(
    tmp_path: Path, qcoreapplication: QCoreApplication
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'real.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    credentials = FakeCredentialStore()
    outboxes: list[RecordingOutbox] = []
    caches: list[RecordingCache] = []

    def outbox_factory(root: Path, clock: Callable[[], datetime]) -> RecordingOutbox:
        result = RecordingOutbox(root, clock)
        outboxes.append(result)
        return result

    def cache_factory(root: Path) -> RecordingCache:
        result = RecordingCache(root)
        caches.append(result)
        return result

    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'real-data',
        client_factory=cast(ClientFactory, lambda *_args: None),
        profile_validator=lambda _path: None,
        outbox_factory=outbox_factory,
        cache_factory=cache_factory,
        clock=lambda: NOW,
    )
    assert outboxes and caches
    assert outboxes[0].open_threads == []
    assert caches[0].open_threads == []
    ui_thread = int(QThread.currentThreadId())

    controller.start()
    deadline = time.monotonic() + 2
    while not caches[0].open_threads:
        qcoreapplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError('real worker did not open stores')
        time.sleep(0.001)
    assert outboxes[0].open_threads == caches[0].open_threads
    assert outboxes[0].open_threads[0] != ui_thread
    destroyed = QSignalSpy(controller._worker_object.destroyed)
    assert controller.shutdown(2_000)
    while len(destroyed) == 0:
        qcoreapplication.processEvents()
    assert len(destroyed) == 1
    assert outboxes[0].closed
    assert caches[0].closed


def test_real_delayed_shutdown_destroys_production_worker_timer_and_stores(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'delayed.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(True, False, 64 * 1024 * 1024)
    credentials = FakeCredentialStore()
    credentials.values[ORIGIN] = secrets.token_urlsafe(32)
    client = BlockingAuthenticationClient()
    outboxes: list[RecordingOutbox] = []
    caches: list[RecordingCache] = []

    def outbox_factory(root: Path, clock: Callable[[], datetime]) -> RecordingOutbox:
        result = RecordingOutbox(root, clock)
        outboxes.append(result)
        return result

    def cache_factory(root: Path) -> RecordingCache:
        result = RecordingCache(root)
        caches.append(result)
        return result

    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'delayed-data',
        client_factory=cast(ClientFactory, lambda *_args: client),
        profile_validator=lambda _path: None,
        outbox_factory=outbox_factory,
        cache_factory=cache_factory,
        clock=lambda: NOW,
    )
    worker = controller._worker_object
    worker_destroyed = QSignalSpy(worker.destroyed)
    controller.start()
    assert client.entered.wait(timeout=2)
    timer = controller._worker._timer
    assert timer is not None
    timer_destroyed = QSignalSpy(timer.destroyed)

    assert not controller.shutdown(10)
    assert controller.worker_thread_running
    client.release.set()
    _wait_for_qt(
        qcoreapplication,
        lambda: not controller.worker_thread_running,
        'delayed production worker did not finish shutdown',
    )
    _wait_for_qt(
        qcoreapplication,
        lambda: len(worker_destroyed) == 1 and len(timer_destroyed) == 1,
        'production worker or timer handle survived shutdown',
    )

    assert controller.shutdown(2_000)
    assert outboxes[0].closed
    assert caches[0].closed
    assert controller._thread.children() == []


def test_real_blocked_worker_queues_two_exact_revisions_from_same_save_target(
    tmp_path: Path,
    qcoreapplication: QCoreApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'causal.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(True, True, 64 * 1024 * 1024)
    credentials = FakeCredentialStore()
    runtime_secret = secrets.token_urlsafe(32)
    credentials.values[ORIGIN] = runtime_secret
    client = BlockingAuthenticationClient()
    ui_thread = int(QThread.currentThreadId())
    original_open = Path.open
    original_stat = Path.stat
    original_sha256 = hashlib.sha256

    def guarded_open(
        path: Path,
        mode: str = 'r',
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        if int(QThread.currentThreadId()) == ui_thread and not any(
            flag in mode for flag in ('w', 'a', 'x')
        ):
            raise AssertionError('save hook read a path on the UI thread')
        return original_open(path, mode, buffering, encoding, errors, newline)

    def guarded_stat(
        path: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        if int(QThread.currentThreadId()) == ui_thread:
            raise AssertionError('save hook statted a path on the UI thread')
        return original_stat(path, follow_symlinks=follow_symlinks)

    def guarded_sha256(
        content: bytes = b'', *, usedforsecurity: bool = True
    ) -> Any:
        if int(QThread.currentThreadId()) == ui_thread:
            raise AssertionError('save hook hashed bytes on the UI thread')
        return original_sha256(content, usedforsecurity=usedforsecurity)

    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'causal-data',
        client_factory=cast(ClientFactory, lambda *_args: client),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    changed = QSignalSpy(controller.queueChanged)
    controller.start()
    assert client.entered.wait(timeout=2)
    first_uuid = ROAST_UUID
    second_uuid = UUID('66666666-6666-4666-8666-666666666666')
    first_profile = ProfileData(roastUUID=str(first_uuid), title='same-path first')
    second_profile = ProfileData(roastUUID=str(second_uuid), title='same-path second')
    first_bytes = repr(dict(first_profile)).encode('utf-8')
    second_bytes = repr(dict(second_profile)).encode('utf-8')
    second_modified = NOW.replace(microsecond=2)
    save_target = tmp_path / 'same-save-target.alog'
    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(Path, 'stat', guarded_stat)
    monkeypatch.setattr(hashlib, 'sha256', guarded_sha256)

    save_target.write_bytes(first_bytes)
    controller.saved_profile(first_bytes, first_profile, NOW)
    save_target.write_bytes(second_bytes)
    controller.saved_profile(second_bytes, second_profile, second_modified)
    client.release.set()
    deadline = time.monotonic() + 3
    while not _spy_matches(
        changed,
        lambda payload: isinstance(payload[0], QueueCounts)
        and payload[0].paused == 2,
    ):
        qcoreapplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError('real worker did not durably queue both revisions')
        time.sleep(0.001)
    assert controller.shutdown(2_000)
    monkeypatch.setattr(Path, 'open', original_open)
    monkeypatch.setattr(Path, 'stat', original_stat)
    monkeypatch.setattr(hashlib, 'sha256', original_sha256)

    outbox = Outbox(tmp_path / 'causal-data' / 'outbox', lambda: NOW)
    outbox.open()
    namespace = namespace_for(ORIGIN, ORGANIZATION_ID)
    outbox.resume_namespace(namespace, NOW)
    jobs = (outbox.lease_next(namespace, NOW), outbox.lease_next(namespace, NOW))
    assert all(isinstance(job, Job) for job in jobs)
    exact_jobs = sorted(
        (job for job in jobs if isinstance(job, Job)),
        key=lambda job: job.roast_uuid.hex,
    )
    expected = {
        first_uuid: (first_bytes, NOW),
        second_uuid: (second_bytes, second_modified),
    }
    for job in exact_jobs:
        assert job.snapshot_path is not None
        content, modified_at = expected[job.roast_uuid]
        assert job.snapshot_path.read_bytes() == content
        assert job.content_sha256 == original_sha256(content).hexdigest()
        aroast = json.loads(job.aroast_json)
        assert aroast['roast_id'] == job.roast_uuid.hex
        assert aroast['modified_at'] == modified_at.isoformat()
    outbox.close()


def test_real_worker_removes_credential_on_worker_thread_without_secret_leak(
    tmp_path: Path, qcoreapplication: QCoreApplication
) -> None:
    settings = SettingsStore(
        QSettings(str(tmp_path / 'remove.ini'), QSettings.Format.IniFormat)
    )
    settings.set_origin(ORIGIN)
    settings.save_connection(ORIGIN, IDENTITY)
    settings.save_options(True, True, 64 * 1024 * 1024)
    credentials = FakeCredentialStore()
    ephemeral_secret = secrets.token_urlsafe(32)
    credentials.values[ORIGIN] = ephemeral_secret
    controller = RoastServerController(
        settings=settings,
        credentials=credentials,
        data_root=tmp_path / 'remove-data',
        client_factory=cast(ClientFactory, lambda *_args: None),
        profile_validator=lambda _path: None,
        clock=lambda: NOW,
    )
    ui_thread = int(QThread.currentThreadId())
    controller.start()
    deadline = time.monotonic() + 2
    while not credentials.get_calls:
        qcoreapplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError('real worker did not load its credential')
        time.sleep(0.001)

    controller.remove_credential()
    deadline = time.monotonic() + 2
    while not credentials.delete_calls:
        qcoreapplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError('real worker did not remove its credential')
        time.sleep(0.001)

    assert credentials.delete_calls == [(ORIGIN, credentials.delete_calls[0][1])]
    assert credentials.delete_calls[0][1] != ui_thread
    assert not credentials.contains_origin(ORIGIN)
    assert not settings.load().automatic_upload
    assert_secret_absent(ephemeral_secret, controller)
    assert controller.shutdown(2_000)
    for path in tmp_path.rglob('*'):
        if path.is_file() and ephemeral_secret.encode('utf-8') in path.read_bytes():
            pytest.fail('runtime secret persisted to connector storage', pytrace=False)


def test_controller_source_has_no_plus_import_secret_signal_or_terminate() -> None:
    source_path = Path(__file__).parents[4] / 'artisanlib' / 'roastserver' / 'controller.py'
    source = source_path.read_text(encoding='utf-8')

    assert 'import plus' not in source
    assert 'from plus' not in source
    assert '.terminate(' not in source
    assert 'pyqtSignal(str, str)' not in source
