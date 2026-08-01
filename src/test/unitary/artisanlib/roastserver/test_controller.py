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
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import secrets
import threading
import time
from typing import cast, override
from uuid import UUID

from PyQt6.QtCore import QCoreApplication, QObject, QSettings, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtTest import QSignalSpy
import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ClientFactory, DownloadReceipt
from artisanlib.roastserver.cache import CacheStats, CachedRevision, CacheStore
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    ArchiveFilters,
    FailureKind,
    IdentityOrganization,
    IdentityUser,
    PublicFailure,
    Revision,
    RoastDetail,
    RoastDetailLinks,
    RoastPage,
    RoastSummary,
    ServerIdentity,
    ServerProfileSource,
)
from artisanlib.roastserver.controller import ControllerError, RoastServerController
from artisanlib.roastserver.outbox import FailedJob, Outbox, QueueCounts
from artisanlib.roastserver.settings import CredentialStoreError, SettingsStore, namespace_for
from artisanlib.roastserver.worker import (
    BrowseRequest,
    ClearUnusedRequest,
    ConnectionTestRequest,
    OpaqueVault,
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


def revision_for(content: bytes = PROFILE_BYTES) -> Revision:
    return Revision(
        revision_number=1,
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


def summary_for(content: bytes = PROFILE_BYTES) -> RoastSummary:
    revision = revision_for(content)
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


def detail_for(content: bytes = PROFILE_BYTES) -> RoastDetail:
    summary = summary_for(content)
    revision = revision_for(content)
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


def publish_request(path: Path) -> PublishRequest:
    detail = detail_for()
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


class FakeCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
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


class FakeWorker(QObject):
    connectionTested = pyqtSignal(str, object)
    credentialRemoved = pyqtSignal(str)
    operationFailed = pyqtSignal(str, object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    archivePageReady = pyqtSignal(str, object)
    downloadStaged = pyqtSignal(str, object)
    cachedReady = pyqtSignal(str, object)
    cachePublished = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    stopped = pyqtSignal()

    def __init__(self, **_kwargs: object) -> None:
        super().__init__()
        self.calls: list[tuple[object, ...]] = []
        self.configure_values: list[WorkerConfiguration] = []
        self.test_ids: list[str] = []
        self.enqueue_ids: list[str] = []
        self.publish_ids: list[str] = []
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

    @pyqtSlot(str, object)
    def relay_staged(self, request_id: str, value: object) -> None:
        self.downloadStaged.emit(request_id, value)

    @pyqtSlot(str, object)
    def relay_cached(self, request_id: str, value: object) -> None:
        self.cachedReady.emit(request_id, value)

    @pyqtSlot(str, object)
    def relay_published(self, request_id: str, value: object) -> None:
        self.cachePublished.emit(request_id, value)

    @pyqtSlot(bool)
    def relay_online(self, value: bool) -> None:
        self.onlineChanged.emit(value)


class WorkerRelay(QObject):
    connection = pyqtSignal(str, object)
    removed = pyqtSignal(str)
    failure = pyqtSignal(str, object)
    queue = pyqtSignal(object)
    failed_jobs = pyqtSignal(object)
    cache_stats = pyqtSignal(object)
    archive = pyqtSignal(str, object)
    staged = pyqtSignal(str, object)
    cached = pyqtSignal(str, object)
    published = pyqtSignal(str, object)
    online = pyqtSignal(bool)


class ControllerHarness:
    def __init__(self, tmp_path: Path, app: QCoreApplication) -> None:
        self.tmp_path = tmp_path
        self.app = app
        qsettings = QSettings(
            str(tmp_path / 'controller.ini'), QSettings.Format.IniFormat
        )
        qsettings.clear()
        self.settings_store = SettingsStore(qsettings)
        self.settings_store.set_origin(ORIGIN)
        self.credentials = FakeCredentialStore()
        self.ephemeral_secret = secrets.token_urlsafe(32)
        self.secret_vault: OpaqueVault[ConnectionTestRequest] = OpaqueVault()
        self.profile_vault: OpaqueVault[SavedProfileRequest] = OpaqueVault()
        self.command_vault: OpaqueVault[object] = OpaqueVault()
        self.validator_calls: list[tuple[Path, int]] = []
        self.validator_failure: Exception | None = None
        self.worker = cast(FakeWorker, None)
        self.relay = WorkerRelay()

        def validate(path: Path) -> None:
            self.validator_calls.append((path, int(QThread.currentThreadId())))
            if self.validator_failure is not None:
                raise self.validator_failure

        def worker_factory(**kwargs: object) -> FakeWorker:
            assert kwargs['credential_vault'] is self.secret_vault
            assert kwargs['profile_vault'] is self.profile_vault
            assert kwargs['command_vault'] is self.command_vault
            self.worker = FakeWorker(**kwargs)
            self.relay.connection.connect(self.worker.relay_connection)
            self.relay.removed.connect(self.worker.relay_removed)
            self.relay.failure.connect(self.worker.relay_failure)
            self.relay.queue.connect(self.worker.relay_queue)
            self.relay.failed_jobs.connect(self.worker.relay_failed_jobs)
            self.relay.cache_stats.connect(self.worker.relay_cache_stats)
            self.relay.archive.connect(self.worker.relay_archive)
            self.relay.staged.connect(self.worker.relay_staged)
            self.relay.cached.connect(self.worker.relay_cached)
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
def qcoreapplication() -> Generator[QCoreApplication, None, None]:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def controller_harness(
    tmp_path: Path, qcoreapplication: QCoreApplication
) -> Generator[ControllerHarness, None, None]:
    harness = ControllerHarness(tmp_path, qcoreapplication)
    yield harness
    harness.stop()


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
    assert controller_harness.wait_for_spy(changed) == [IDENTITY]

    loaded = controller_harness.settings_store.load()
    assert loaded.identity == IDENTITY
    assert not loaded.automatic_upload
    assert signal_threads == [controller_harness.ui_thread_id]


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
        lambda: controller_harness.fake_worker.configure_values[-1].namespace
        == namespace_for(ORIGIN, OTHER_ORGANIZATION_ID)
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


def test_saved_profile_detaches_without_file_or_worker_io_on_ui_thread(
    controller_harness: ControllerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller_harness.confirm()
    controller_harness.enable()
    profile = ProfileData(
        roastUUID=str(ROAST_UUID),
        title='Controller roast',
        flavors=[1.0, 2.0],
    )
    source_path = controller_harness.tmp_path / 'profile.alog'
    opened: list[None] = []

    def reject_open(*_args: object, **_kwargs: object) -> object:
        opened.append(None)
        raise AssertionError('controller attempted UI-thread file I/O')

    monkeypatch.setattr(Path, 'open', reject_open)
    started = time.monotonic()
    controller_harness.controller.record_saved_profile(source_path, profile)
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
    assert request.path == source_path.absolute()
    assert request.profile == {
        'roastUUID': str(ROAST_UUID),
        'title': 'Controller roast',
        'flavors': [1.0, 2.0],
    }
    assert not request.manual


def test_manual_and_automatic_queue_rules_are_exact(
    controller_harness: ControllerHarness,
) -> None:
    path = controller_harness.tmp_path / 'saved.alog'
    profile = ProfileData(roastUUID=str(ROAST_UUID))

    controller_harness.controller.saved_profile(path, profile)
    assert controller_harness.profile_vault.size() == 0
    with pytest.raises(ControllerError, match='Enable Roast Server'):
        controller_harness.controller.manual_upload(path)

    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    controller_harness.controller.saved_profile(path, profile)
    assert controller_harness.profile_vault.size() == 0
    controller_harness.controller.manual_upload(path)
    controller_harness.wait_until(
        lambda: controller_harness.profile_vault.size() == 1
        and bool(controller_harness.fake_worker.enqueue_ids)
    )
    manual_id = controller_harness.fake_worker.enqueue_ids[-1]
    manual = controller_harness.profile_vault.take(manual_id)
    assert manual == SavedProfileRequest(
        namespace_for(ORIGIN, ORGANIZATION_ID), path.absolute(), None, True
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
    assert controller_harness.wait_for_spy(page_spy) == [first_id, page]
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


def test_validation_precedes_publication_and_profile_ready(
    controller_harness: ControllerHarness,
) -> None:
    controller_harness.confirm()
    controller_harness.enable(automatic_upload=False)
    ready = QSignalSpy(controller_harness.controller.profileReady)
    open_id = controller_harness.controller.open_roast(ROAST_UUID)
    controller_harness.command_vault.take(open_id)
    staged_path = controller_harness.tmp_path / 'hidden.part'
    request = publish_request(staged_path)

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
    controller_harness.command_vault.take(open_id)
    request = publish_request(controller_harness.tmp_path / 'invalid.part')
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
    controller_harness.command_vault.take(second_id)
    second = publish_request(controller_harness.tmp_path / 'vault-loss.part')
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

    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.clear_ids)
    )
    first_clear_id = controller_harness.fake_worker.clear_ids[-1]
    first_clear = cast(
        ClearUnusedRequest, controller_harness.command_vault.take(first_clear_id)
    )
    assert first_clear.open_paths == frozenset()

    controller_harness.controller.record_open_source(cached.path, source)
    before = len(controller_harness.fake_worker.clear_ids)
    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.clear_ids) > before
    )
    second_clear_id = controller_harness.fake_worker.clear_ids[-1]
    second_clear = cast(
        ClearUnusedRequest, controller_harness.command_vault.take(second_clear_id)
    )
    assert second_clear.open_paths == frozenset({cached.path.absolute()})

    controller_harness.controller.record_local_save(
        controller_harness.tmp_path / 'local.alog'
    )
    before = len(controller_harness.fake_worker.clear_ids)
    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: len(controller_harness.fake_worker.clear_ids) > before
    )
    third_clear_id = controller_harness.fake_worker.clear_ids[-1]
    third_clear = cast(
        ClearUnusedRequest, controller_harness.command_vault.take(third_clear_id)
    )
    assert third_clear.open_paths == frozenset()


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
    controller_harness.controller.record_open_source(forged.path, forged.source)
    controller_harness.controller.clear_unused_cache()
    controller_harness.wait_until(
        lambda: bool(controller_harness.fake_worker.clear_ids)
    )
    clear_id = controller_harness.fake_worker.clear_ids[-1]
    request = cast(ClearUnusedRequest, controller_harness.command_vault.take(clear_id))
    assert request.open_paths == frozenset()


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

    worker.test_release.set()
    controller_harness.wait_until(lambda: worker.stop_count == 1)
    controller_harness.wait_until(
        lambda: not controller_harness.controller.worker_thread_running
    )
    assert controller_harness.controller.shutdown(2_000)


class RecordingOutbox(Outbox):
    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        super().__init__(root, clock)
        self.open_threads: list[int] = []

    @override
    def open(self) -> None:
        self.open_threads.append(int(QThread.currentThreadId()))
        super().open()


class RecordingCache(CacheStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.open_threads: list[int] = []

    @override
    def open(self) -> None:
        self.open_threads.append(int(QThread.currentThreadId()))
        super().open()


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
    assert controller.shutdown(2_000)


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
    assert ORIGIN not in credentials.values
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
