#
# ABOUT
# Artisan Roast Server main-thread lifecycle and command controller
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
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# OpenAI, 2026

from __future__ import annotations

from collections.abc import Callable
import copy
from dataclasses import replace
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from typing import Final, Protocol, cast, override
from uuid import UUID

from PyQt6.QtCore import QByteArray, QObject, QThread, Qt, pyqtSignal, pyqtSlot

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ClientFactory
from artisanlib.roastserver.cache import (
    CacheStats,
    CachedPage,
    CachedRevision,
    CacheStore,
)
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    MAX_PROFILE_BYTES,
    ArchiveFilters,
    FailureKind,
    Namespace,
    PublicFailure,
    RoastPage,
    ServerIdentity,
    ServerProfileSource,
    validate_archive_filters,
)
from artisanlib.roastserver.origin import SettingsError, canonical_origin
from artisanlib.roastserver.outbox import FailedJob, Outbox, QueueCounts
from artisanlib.roastserver.settings import (
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    SETTINGS_FAILURE_MESSAGE,
    ConnectorSettings,
    CredentialStore,
    SettingsStore,
    namespace_for,
)
from artisanlib.roastserver.worker import (
    BrowseRequest,
    CachedOpenRequest,
    ClearUnusedRequest,
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

_log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_MESSAGE: Final[str] = (
    'Roast Server worker did not stop within the shutdown timeout.'
)
_UI_THREAD_MESSAGE: Final[str] = 'Roast Server controller methods require the UI thread.'
_INACTIVE_MESSAGE: Final[str] = 'Roast Server controller is shutting down.'
_TEST_CONNECTION_MESSAGE: Final[str] = (
    'Test the connection before enabling automatic upload.'
)
_ENABLE_MESSAGE: Final[str] = 'Enable Roast Server before using this command.'
_CONNECTION_MESSAGE: Final[str] = 'Test the connection before using this command.'
_INVALID_OPTIONS_MESSAGE: Final[str] = 'Invalid Roast Server options.'
_INVALID_REQUEST_MESSAGE: Final[str] = 'Invalid Roast Server request.'


class ControllerError(RuntimeError):
    pass


class _SignalConnect(Protocol):
    def __call__(
        self,
        slot: object,
        connection_type: Qt.ConnectionType,
    ) -> object: ...


class _Signal(Protocol):
    @property
    def connect(self) -> _SignalConnect: ...


type WorkerFactory = Callable[..., QObject]
type OutboxFactory = Callable[[Path, Callable[[], datetime]], Outbox]
type CacheFactory = Callable[[Path], CacheStore]


class RoastServerController(QObject):
    """UI-thread-only façade over the connector worker and persistent settings."""

    settingsChanged = pyqtSignal(object)
    identityChanged = pyqtSignal(object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    archivePageReady = pyqtSignal(str, object)
    operationFailed = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    profileReady = pyqtSignal(str, object)

    _configureWorker = pyqtSignal(object)
    _testWorker = pyqtSignal(str)
    _commitWorker = pyqtSignal(str)
    _finalizeWorker = pyqtSignal(str)
    _rollbackWorker = pyqtSignal(str)
    _cancelConnectionWorker = pyqtSignal(str)
    _removeCredentialWorker = pyqtSignal(str)
    _enqueueWorker = pyqtSignal(str)
    _refreshWorker = pyqtSignal()
    _retryWorker = pyqtSignal(str)
    _removeJobWorker = pyqtSignal(str)
    _browseWorker = pyqtSignal(str)
    _openOnlineWorker = pyqtSignal(str)
    _openCachedWorker = pyqtSignal(str)
    _publishWorker = pyqtSignal(str)
    _discardWorker = pyqtSignal(str)
    _protectWorker = pyqtSignal(str)
    _clearWorker = pyqtSignal(str)
    _stopWorker = pyqtSignal()

    def __init__(
        self,
        *,
        settings: SettingsStore,
        credentials: CredentialStore,
        data_root: Path,
        client_factory: ClientFactory,
        profile_validator: Callable[[Path], None],
        credential_vault: OpaqueVault[ConnectionTestRequest] | None = None,
        profile_vault: OpaqueVault[SavedProfileRequest] | None = None,
        command_vault: OpaqueVault[object] | None = None,
        worker_factory: WorkerFactory = RoastServerWorker,
        outbox_factory: OutboxFactory = Outbox,
        cache_factory: CacheFactory = CacheStore,
        clock: Callable[[], datetime] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings
        self._profile_validator = profile_validator
        self._clock = clock or _utc_now
        self._credential_vault = credential_vault or OpaqueVault()
        self._profile_vault = profile_vault or OpaqueVault()
        self._command_vault = command_vault or OpaqueVault()
        self._settings = self._settings_store.load()
        self._identity: ServerIdentity | None = None
        self._known_namespace = _settings_namespace(self._settings)
        self._proof: tuple[str, UUID] | None = None
        self._connection_tests: dict[str, str] = {}
        self._activation_previous: dict[str, ConnectorSettings] = {}
        self._active_connection_test: str | None = None
        self._credential_removals: set[str] = set()
        self._browse_epoch = 0
        self._browse_filters: ArchiveFilters | None = None
        self._next_cursor: str | None = None
        self._browse_requests: dict[str, int] = {}
        self._online_requests: dict[str, tuple[Namespace, UUID, int]] = {}
        self._cached_requests: dict[str, tuple[CachedRevision, int]] = {}
        self._publish_requests: dict[
            str, tuple[str, Namespace, UUID, PublishRequest, int]
        ] = {}
        self._ready_cache_paths: dict[Path, ServerProfileSource] = {}
        self._open_cache_paths: set[Path] = set()
        self._current_open_cache_path: Path | None = None
        self._current_open_cache_source: ServerProfileSource | None = None
        self._started = False
        self._stop_requested = False
        self._shutdown_complete = False

        root = _absolute_path(data_root)
        outbox = outbox_factory(root / 'outbox', self._clock)
        cache = cache_factory(root / 'cache')
        worker_object = worker_factory(
            outbox=outbox,
            cache=cache,
            credentials=credentials,
            client_factory=client_factory,
            clock=self._clock,
            credential_vault=self._credential_vault,
            profile_vault=self._profile_vault,
            command_vault=self._command_vault,
        )
        self._thread = QThread(self)
        self._worker_object = worker_object
        self._worker = cast(RoastServerWorker, worker_object)
        worker_object.moveToThread(self._thread)
        self._connect_worker()

    @override
    def __repr__(self) -> str:
        return '<RoastServerController credential=<redacted>>'

    @property
    def worker_thread_running(self) -> bool:
        return self._thread.isRunning()

    def _connect_worker(self) -> None:
        queued = Qt.ConnectionType.QueuedConnection
        direct = Qt.ConnectionType.DirectConnection
        worker = self._worker
        worker_object = self._worker_object

        _connect(self._configureWorker, worker.configure, queued)
        _connect(self._testWorker, worker.test_connection, queued)
        _connect(self._commitWorker, worker.commit_connection, queued)
        _connect(self._finalizeWorker, worker.finalize_connection, queued)
        _connect(self._rollbackWorker, worker.rollback_connection, queued)
        _connect(
            self._cancelConnectionWorker,
            worker.cancel_connection_transaction,
            queued,
        )
        _connect(self._removeCredentialWorker, worker.remove_credential, queued)
        _connect(self._enqueueWorker, worker.enqueue_saved, queued)
        _connect(self._refreshWorker, worker.refresh, queued)
        _connect(self._retryWorker, worker.retry_job, queued)
        _connect(self._removeJobWorker, worker.remove_job, queued)
        _connect(self._browseWorker, worker.browse, queued)
        _connect(self._openOnlineWorker, worker.open_online, queued)
        _connect(self._openCachedWorker, worker.open_cached, queued)
        _connect(self._publishWorker, worker.publish_staged, queued)
        _connect(self._discardWorker, worker.discard_staged, queued)
        _connect(self._protectWorker, worker.update_protected_paths, queued)
        _connect(self._clearWorker, worker.clear_unused, queued)
        _connect(self._stopWorker, worker.stop, queued)

        _connect(worker.connectionTested, self._on_connection_tested, queued)
        _connect(worker.credentialCommitted, self._on_credential_committed, queued)
        _connect(worker.connectionActivated, self._on_connection_activated, queued)
        _connect(
            worker.pendingConnectionRecoveryRequired,
            self._on_pending_connection_recovery_required,
            queued,
        )
        _connect(worker.configurationValidated, self._on_configuration_validated, queued)
        _connect(worker.credentialRemoved, self._on_credential_removed, queued)
        _connect(worker.operationFailed, self._on_operation_failed, queued)
        _connect(worker.queueChanged, self._on_queue_changed, queued)
        _connect(worker.failedJobsChanged, self._on_failed_jobs_changed, queued)
        _connect(worker.cacheStatsChanged, self._on_cache_stats_changed, queued)
        _connect(worker.archivePageReady, self._on_archive_page, queued)
        _connect(worker.downloadStaged, self._on_download_staged, queued)
        _connect(worker.cachedReady, self._on_cached_ready, queued)
        _connect(worker.cachePublished, self._on_cache_published, queued)
        _connect(worker.onlineChanged, self._on_online_changed, queued)
        _connect(worker.stopped, worker_object.deleteLater, direct)
        _connect(worker.stopped, self._thread.quit, direct)
        self._thread.started.connect(worker.start)

    def start(self) -> None:
        self._require_ui_thread()
        if self._started or self._shutdown_complete or self._stop_requested:
            return
        self._started = True
        self._thread.start()
        self._queue_configuration(self._configuration())
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(self._identity)

    def shutdown(self, timeout_ms: int = 15_000) -> bool:
        self._require_ui_thread()
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        if self._shutdown_complete or (self._started and not self._thread.isRunning()):
            self._shutdown_complete = True
            return True
        if not self._started:
            self._credential_vault.clear()
            self._profile_vault.clear()
            self._command_vault.clear()
            self._shutdown_complete = True
            return True
        if not self._stop_requested:
            self._stop_requested = True
            self._invalidate_requests()
            self._credential_vault.clear()
            self._profile_vault.clear()
            self._command_vault.clear()
            self._thread.requestInterruption()
            self._stopWorker.emit()
        stopped = self._thread.wait(timeout_ms)
        if not stopped:
            _log.error(SHUTDOWN_TIMEOUT_MESSAGE)
            return False
        self._shutdown_complete = True
        return True

    def test_connection(self, origin: str, candidate: str) -> str:
        self._require_command_state()
        canonical = _canonical_origin(origin)
        candidate_value: object = candidate
        if not isinstance(candidate_value, str) or candidate_value == '':
            raise ControllerError(_INVALID_REQUEST_MESSAGE)

        self._cancel_connection_transactions()
        try:
            self._settings = self._settings_store.save_options(
                False,
                False,
                self._settings.cache_limit_bytes,
            )
        except SettingsError:
            self._settings_failure('connection')
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self.settingsChanged.emit(self._settings)
        self._invalidate_identity()
        self._queue_configuration(self._configuration(enabled=False))
        try:
            request_id = self._credential_vault.put_latest(
                ConnectionTestRequest(canonical, candidate)
            )
        except Exception as error:
            raise ControllerError(_INVALID_REQUEST_MESSAGE) from error
        self._connection_tests[request_id] = canonical
        self._active_connection_test = request_id
        self._testWorker.emit(request_id)
        return request_id

    def apply_options(
        self,
        origin: str,
        enabled: bool,
        automatic_upload: bool,
        cache_limit_bytes: int,
    ) -> None:
        self._require_command_state()
        enabled_value: object = enabled
        automatic_upload_value: object = automatic_upload
        cache_limit_value: object = cache_limit_bytes
        if (
            type(enabled_value) is not bool
            or type(automatic_upload_value) is not bool
            or type(cache_limit_value) is not int
            or not MIN_CACHE_LIMIT_BYTES
            <= cache_limit_value
            <= MAX_CACHE_LIMIT_BYTES
        ):
            raise ControllerError(_INVALID_OPTIONS_MESSAGE)
        canonical = _canonical_origin(origin)
        if canonical != self._settings.origin:
            old_configuration = self._configuration(enabled=False)
            self._queue_configuration(old_configuration)
            self._invalidate_identity()
            try:
                self._settings = self._settings_store.set_origin(canonical)
                self._known_namespace = _settings_namespace(self._settings)
            except SettingsError:
                self._settings_failure('settings')
                raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        if automatic_upload and not self._has_current_proof(canonical):
            raise ControllerError(_TEST_CONNECTION_MESSAGE)
        try:
            self._settings = self._settings_store.save_options(
                enabled, automatic_upload, cache_limit_bytes
            )
        except SettingsError:
            self._settings_failure('settings')
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self.settingsChanged.emit(self._settings)
        self._queue_configuration(self._configuration())

    def invalidate_connection_proof(self) -> None:
        self._require_command_state()
        paused = self._configuration(enabled=False)
        self._credential_vault.clear()
        self._cancel_connection_transactions()
        try:
            self._settings = self._settings_store.save_options(
                False,
                False,
                self._settings.cache_limit_bytes,
            )
        except SettingsError:
            self._settings_failure('settings')
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self.settingsChanged.emit(self._settings)
        self._invalidate_identity()
        self._queue_configuration(paused)

    def save_configuration_geometry(self, geometry: QByteArray) -> None:
        self._require_command_state()
        geometry_value: object = geometry
        if not isinstance(geometry_value, QByteArray) or geometry_value.isEmpty():
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        detached = QByteArray(geometry_value)
        try:
            self._settings_store.save_geometry(
                detached,
                self._settings.browser_geometry,
            )
        except SettingsError:
            self._settings_failure('geometry')
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self._settings = replace(
            self._settings,
            configuration_geometry=detached,
        )

    def remove_credential(self) -> None:
        self._require_command_state()
        paused = self._configuration(enabled=False)
        if not self._set_automatic_upload_off('remove'):
            raise ControllerError(_INVALID_OPTIONS_MESSAGE)
        self._invalidate_identity()
        self._queue_configuration(paused)
        try:
            request_id = self._command_vault.put(
                RemoveCredentialRequest(self._settings.origin)
            )
        except Exception as error:
            self._emit_failure('request', FailureKind.KEYRING)
            raise ControllerError(_INVALID_REQUEST_MESSAGE) from error
        self._credential_removals.add(request_id)
        self._removeCredentialWorker.emit(request_id)

    def saved_profile(
        self,
        serialized_profile: bytes,
        profile: ProfileData,
        modified_at: datetime,
    ) -> None:
        self.record_saved_profile(serialized_profile, profile, modified_at)

    def record_saved_profile(
        self,
        serialized_profile: bytes,
        profile: ProfileData,
        modified_at: datetime,
    ) -> None:
        self._require_command_state()
        namespace = self._configured_namespace(require_enabled=True)
        if namespace is None or not self._settings.automatic_upload:
            return
        profile_value: object = profile
        content_value: object = serialized_profile
        try:
            canonical_modified_at = _aware_utc(modified_at)
        except (TypeError, ValueError):
            self._emit_failure('queue', FailureKind.LOCAL_PROFILE)
            return
        if (
            not isinstance(profile_value, dict)
            or not isinstance(content_value, bytes)
            or not 1 <= len(content_value) <= MAX_PROFILE_BYTES
        ):
            self._emit_failure('queue', FailureKind.LOCAL_PROFILE)
            return
        try:
            detached: ProfileData = copy.deepcopy(profile)
            request = SavedProfileRequest(
                namespace=namespace,
                serialized_profile=content_value,
                profile=detached,
                modified_at=canonical_modified_at,
                manual=False,
            )
            request_id = self._profile_vault.put(request)
        except (MemoryError, RecursionError, TypeError, ValueError):
            self._emit_failure('queue', FailureKind.LOCAL_PROFILE)
            return
        self._enqueueWorker.emit(request_id)

    def manual_upload(
        self,
        serialized_profile: bytes,
        profile: ProfileData,
        modified_at: datetime,
    ) -> None:
        self._require_command_state()
        namespace = self._configured_namespace(require_enabled=True)
        if namespace is None:
            if not self._settings.enabled:
                raise ControllerError(_ENABLE_MESSAGE)
            raise ControllerError(_CONNECTION_MESSAGE)
        content_value: object = serialized_profile
        profile_value: object = profile
        if (
            not isinstance(content_value, bytes)
            or not 1 <= len(content_value) <= MAX_PROFILE_BYTES
            or not isinstance(profile_value, dict)
        ):
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        try:
            request_id = self._profile_vault.put(
                SavedProfileRequest(
                    namespace=namespace,
                    serialized_profile=content_value,
                    profile=copy.deepcopy(profile),
                    modified_at=_aware_utc(modified_at),
                    manual=True,
                )
            )
        except (MemoryError, RecursionError, TypeError, ValueError) as error:
            raise ControllerError(_INVALID_REQUEST_MESSAGE) from error
        self._enqueueWorker.emit(request_id)

    def refresh_queue(self) -> None:
        self._require_command_state()
        self._refreshWorker.emit()

    def retry_job(self, job_id: str) -> None:
        self._require_command_state()
        self._require_job_id(job_id)
        self._retryWorker.emit(job_id)

    def remove_job(self, job_id: str) -> None:
        self._require_command_state()
        self._require_job_id(job_id)
        self._removeJobWorker.emit(job_id)

    def browse(self, filters: ArchiveFilters, refresh: bool = True) -> str:
        self._require_command_state()
        namespace = self._require_active_namespace()
        if type(refresh) is not bool:
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        try:
            validated = validate_archive_filters(filters)
        except ValueError as error:
            raise ControllerError(_INVALID_REQUEST_MESSAGE) from error
        self._browse_epoch += 1
        self._browse_filters = validated
        self._next_cursor = None
        self._browse_requests.clear()
        request_id = self._put_command(
            BrowseRequest(namespace, validated, None, refresh)
        )
        self._browse_requests[request_id] = self._browse_epoch
        self._browseWorker.emit(request_id)
        return request_id

    def load_more(self) -> str | None:
        self._require_command_state()
        namespace = self._active_namespace(require_enabled=True)
        filters = self._browse_filters
        cursor = self._next_cursor
        if namespace is None or filters is None or cursor is None:
            return None
        self._next_cursor = None
        request_id = self._put_command(
            BrowseRequest(namespace, filters, cursor, False)
        )
        self._browse_requests[request_id] = self._browse_epoch
        self._browseWorker.emit(request_id)
        return request_id

    def open_roast(self, roast_uuid: UUID) -> str:
        self._require_command_state()
        namespace = self._require_active_namespace()
        if not isinstance(roast_uuid, UUID):
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        request_id = self._put_command(OnlineOpenRequest(namespace, roast_uuid))
        self._online_requests[request_id] = (
            namespace,
            roast_uuid,
            self._browse_epoch,
        )
        self._openOnlineWorker.emit(request_id)
        return request_id

    def open_cached(self, cached: CachedRevision) -> str:
        self._require_command_state()
        namespace = self._require_active_namespace()
        cached_value: object = cached
        if (
            not isinstance(cached_value, CachedRevision)
            or cached_value.namespace != namespace
        ):
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        request_id = self._put_command(CachedOpenRequest(cached))
        self._cached_requests[request_id] = (cached, self._browse_epoch)
        self._openCachedWorker.emit(request_id)
        return request_id

    def clear_unused_cache(self) -> None:
        self._require_command_state()
        namespace = self._require_active_namespace()
        request_id = self._put_command(ClearUnusedRequest(namespace))
        self._clearWorker.emit(request_id)

    def record_open_source(
        self, path: Path, source: ServerProfileSource
    ) -> None:
        self._require_command_state()
        try:
            canonical_path = _absolute_path(path)
        except (OSError, TypeError, ValueError):
            return
        expected = self._ready_cache_paths.get(canonical_path)
        if expected is None or expected != source:
            return
        namespace = self._active_namespace(require_enabled=False)
        if namespace is None or source.namespace != namespace:
            return
        if self._current_open_cache_path is not None:
            self._open_cache_paths.discard(self._current_open_cache_path)
        self._current_open_cache_path = canonical_path
        self._current_open_cache_source = source
        self._open_cache_paths.add(canonical_path)
        self._queue_current_protected_paths()

    def record_local_save(self, path: Path) -> None:
        self._require_command_state()
        try:
            _absolute_path(path)
        except (OSError, TypeError, ValueError):
            return
        if self._current_open_cache_path is not None:
            self._open_cache_paths.discard(self._current_open_cache_path)
            self._ready_cache_paths.pop(self._current_open_cache_path, None)
            self._current_open_cache_path = None
            self._current_open_cache_source = None
            self._queue_current_protected_paths()

    @pyqtSlot(str, object)
    def _on_connection_tested(self, request_id: str, value: object) -> None:
        if (
            self._stop_requested
            or request_id != self._active_connection_test
            or not isinstance(value, ServerIdentity)
        ):
            self._cancelConnectionWorker.emit(request_id)
            if request_id == self._active_connection_test:
                self._connection_tests.pop(request_id, None)
                self._active_connection_test = None
                self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        origin = self._connection_tests.pop(request_id, None)
        if origin is None:
            self._cancelConnectionWorker.emit(request_id)
            return
        previous = self._settings
        try:
            self._settings = self._settings_store.save_pending_connection(
                origin, value
            )
        except SettingsError:
            self._active_connection_test = None
            self._cancelConnectionWorker.emit(request_id)
            self._settings_failure(request_id)
            return
        self._activation_previous[request_id] = previous
        self.settingsChanged.emit(self._settings)
        self._commitWorker.emit(request_id)

    @pyqtSlot(str, object)
    def _on_credential_committed(self, request_id: str, value: object) -> None:
        if self._stop_requested or not isinstance(value, ServerIdentity):
            self._cancelConnectionWorker.emit(request_id)
            return
        pending = self._settings.pending_connection
        if self._active_connection_test is None:
            if pending is None or self._activation_previous:
                self._cancelConnectionWorker.emit(request_id)
                return
            self._active_connection_test = request_id
        elif request_id != self._active_connection_test:
            self._cancelConnectionWorker.emit(request_id)
            return
        if pending is None or pending.identity != value:
            self._active_connection_test = None
            self._activation_previous.pop(request_id, None)
            self._cancelConnectionWorker.emit(request_id)
            self._emit_failure(request_id, FailureKind.CREDENTIAL_REJECTED)
            return
        previous = self._activation_previous.get(
            request_id,
            replace(
                self._settings,
                enabled=False,
                automatic_upload=False,
                pending_connection=None,
            ),
        )
        try:
            self._settings = self._settings_store.activate_pending_connection(
                pending.origin, value
            )
            self._known_namespace = _settings_namespace(self._settings)
        except SettingsError:
            self._active_connection_test = None
            self._activation_previous.pop(request_id, None)
            try:
                self._settings = self._settings_store.restore_connection_state(
                    previous
                )
            except SettingsError:
                self._settings = replace(
                    previous,
                    enabled=False,
                    automatic_upload=False,
                    pending_connection=None,
                )
            self._known_namespace = _settings_namespace(self._settings)
            self._rollbackWorker.emit(request_id)
            self._settings_failure(request_id)
            return
        self._activation_previous[request_id] = previous
        self._identity = None
        self._proof = None
        self._invalidate_archive_state()
        self.settingsChanged.emit(self._settings)
        self._queue_configuration(
            self._configuration(enabled=False, activation_id=request_id)
        )
        self._finalizeWorker.emit(request_id)

    @pyqtSlot(str, object)
    def _on_connection_activated(self, request_id: str, value: object) -> None:
        if (
            self._stop_requested
            or request_id != self._active_connection_test
            or not isinstance(value, ServerIdentity)
        ):
            self._cancelConnectionWorker.emit(request_id)
            return
        if request_id not in self._activation_previous:
            self._active_connection_test = None
            self._cancelConnectionWorker.emit(request_id)
            self._emit_failure(request_id, FailureKind.CREDENTIAL_REJECTED)
            return
        if (
            self._settings.pending_connection is not None
            or self._settings.identity != value
        ):
            self._active_connection_test = None
            self._activation_previous.pop(request_id, None)
            self._cancelConnectionWorker.emit(request_id)
            self._emit_failure(request_id, FailureKind.CREDENTIAL_REJECTED)
            return
        self._active_connection_test = None
        self._activation_previous.pop(request_id, None)
        self._identity = value
        self._proof = (self._settings.origin, value.organization.id)
        self._invalidate_archive_state()
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(value)
        self._queue_configuration(self._configuration())

    @pyqtSlot(str, object)
    def _on_pending_connection_recovery_required(
        self, transaction_id: str, value: object
    ) -> None:
        expected = _failure(FailureKind.CREDENTIAL_REJECTED)
        if (
            self._stop_requested
            or self._active_connection_test is not None
            or self._settings.pending_connection is None
            or value != expected
        ):
            self._cancelConnectionWorker.emit(transaction_id)
            return
        self._proof = None
        self._identity = None
        self._invalidate_archive_state()
        try:
            self._settings = self._settings_store.clear_pending_connection()
            self._known_namespace = _settings_namespace(self._settings)
        except SettingsError:
            self._settings = replace(
                self._settings,
                enabled=False,
                automatic_upload=False,
            )
            self.settingsChanged.emit(self._settings)
            self.identityChanged.emit(None)
            self._queue_configuration(self._prior_active_configuration())
            self._cancelConnectionWorker.emit(transaction_id)
            self._emit_failure(transaction_id, FailureKind.SETTINGS)
            return
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(None)
        self._queue_configuration(self._configuration(enabled=False))
        self._cancelConnectionWorker.emit(transaction_id)
        self.operationFailed.emit(transaction_id, expected)

    @pyqtSlot(object)
    def _on_configuration_validated(self, value: object) -> None:
        if self._stop_requested or not isinstance(value, WorkerConfiguration):
            return
        identity = self._settings.identity
        if (
            self._settings.pending_connection is not None
            or identity is None
            or value.activation_id is not None
            or value.origin != self._settings.origin
            or value.identity != identity
            or value.namespace
            != namespace_for(self._settings.origin, identity.organization.id)
        ):
            return
        self._identity = identity
        self._proof = (self._settings.origin, identity.organization.id)
        self.identityChanged.emit(identity)

    @pyqtSlot(str)
    def _on_credential_removed(self, request_id: str) -> None:
        if request_id not in self._credential_removals:
            return
        self._credential_removals.discard(request_id)
        self.onlineChanged.emit(False)

    @pyqtSlot(str, object)
    def _on_operation_failed(self, operation: str, value: object) -> None:
        if not isinstance(value, PublicFailure):
            failure = _failure(FailureKind.INVALID_RESPONSE)
        else:
            failure = value
        if operation == self._active_connection_test:
            self._connection_tests.pop(operation, None)
            self._active_connection_test = None
            self._cancelConnectionWorker.emit(operation)
        previous = self._activation_previous.pop(operation, None)
        if previous is not None:
            try:
                self._settings = self._settings_store.restore_connection_state(
                    previous
                )
                self._known_namespace = _settings_namespace(self._settings)
            except SettingsError:
                self._settings_failure(operation)
                return
            self._identity = None
            self._proof = None
            self._invalidate_archive_state()
            self.settingsChanged.emit(self._settings)
            self._queue_configuration(self._configuration(enabled=False))
        elif (
            operation == 'configure'
            and failure.kind is FailureKind.CREDENTIAL_REJECTED
            and self._settings.pending_connection is not None
        ):
            try:
                self._settings = self._settings_store.clear_pending_connection()
                self._known_namespace = _settings_namespace(self._settings)
            except SettingsError:
                self._settings_failure(operation)
                return
            self._invalidate_identity()
            self.settingsChanged.emit(self._settings)
            self._queue_configuration(self._configuration(enabled=False))
        if failure.kind is FailureKind.CREDENTIAL_REJECTED:
            paused = self._configuration(enabled=False)
            if not self._set_automatic_upload_off(operation):
                return
            self._invalidate_identity()
            self._queue_configuration(paused)
        self.operationFailed.emit(operation, failure)

    @pyqtSlot(object)
    def _on_queue_changed(self, value: object) -> None:
        if isinstance(value, QueueCounts):
            self.queueChanged.emit(value)

    @pyqtSlot(object)
    def _on_failed_jobs_changed(self, value: object) -> None:
        if isinstance(value, tuple) and all(isinstance(item, FailedJob) for item in value):
            self.failedJobsChanged.emit(value)

    @pyqtSlot(object)
    def _on_cache_stats_changed(self, value: object) -> None:
        if isinstance(value, CacheStats):
            self.cacheStatsChanged.emit(value)

    @pyqtSlot(str, object)
    def _on_archive_page(self, request_id: str, value: object) -> None:
        epoch = self._browse_requests.pop(request_id, None)
        if epoch is None or epoch != self._browse_epoch:
            return
        if isinstance(value, RoastPage):
            self._next_cursor = value.next_cursor
        elif isinstance(value, CachedPage):
            self._next_cursor = None
        else:
            self.operationFailed.emit(
                request_id, _failure(FailureKind.INVALID_RESPONSE)
            )
            return
        self.archivePageReady.emit(request_id, value)

    @pyqtSlot(str, object)
    def _on_download_staged(self, request_id: str, value: object) -> None:
        tracked = self._online_requests.pop(request_id, None)
        if not isinstance(value, PublishRequest):
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        if self._stop_requested or tracked is None:
            self._discard_stage(value.staged_path)
            return
        namespace, roast_uuid, epoch = tracked
        if (
            epoch != self._browse_epoch
            or self._active_namespace(require_enabled=True) != namespace
            or value.detail.roast_uuid != roast_uuid
        ):
            self._discard_stage(value.staged_path)
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        try:
            self._profile_validator(value.staged_path)
        except Exception:  # pylint: disable=broad-exception-caught
            self._discard_stage(value.staged_path)
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        try:
            publish_id = self._command_vault.put(value)
        except Exception:  # pylint: disable=broad-exception-caught
            self._discard_stage(value.staged_path)
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        self._publish_requests[publish_id] = (
            request_id,
            namespace,
            roast_uuid,
            value,
            epoch,
        )
        self._publishWorker.emit(publish_id)

    @pyqtSlot(str, object)
    def _on_cached_ready(self, request_id: str, value: object) -> None:
        tracked = self._cached_requests.pop(request_id, None)
        if tracked is None or not isinstance(value, CachedRevision):
            return
        expected, epoch = tracked
        if (
            epoch != self._browse_epoch
            or value != expected
            or self._active_namespace(require_enabled=True) != value.namespace
        ):
            return
        self._emit_profile_ready(value, stale=True)

    @pyqtSlot(str, object)
    def _on_cache_published(self, request_id: str, value: object) -> None:
        tracked = self._publish_requests.pop(request_id, None)
        if tracked is None or not isinstance(value, CachedRevision):
            return
        _, namespace, roast_uuid, publish, epoch = tracked
        revision = publish.detail.current_revision
        if (
            epoch != self._browse_epoch
            or revision is None
            or value.namespace != namespace
            or value.roast.roast_uuid != roast_uuid
            or value.revision.sha256 != revision.sha256
            or value.revision.revision_number != revision.revision_number
            or self._active_namespace(require_enabled=True) != namespace
        ):
            return
        self._emit_profile_ready(value, stale=False)

    @pyqtSlot(bool)
    def _on_online_changed(self, value: bool) -> None:
        if type(value) is bool:
            self.onlineChanged.emit(value)

    def _emit_profile_ready(self, cached: CachedRevision, *, stale: bool) -> None:
        source = replace(cached.source, stale=stale)
        path = _absolute_path(cached.path)
        self._ready_cache_paths[path] = source
        self.profileReady.emit(str(path), source)

    def _queue_configuration(self, configuration: WorkerConfiguration) -> None:
        self._configureWorker.emit(configuration)
        self._queue_protected_paths(configuration.namespace)

    def _queue_current_protected_paths(self) -> None:
        configuration = self._configuration()
        self._queue_protected_paths(configuration.namespace)

    def _queue_protected_paths(self, namespace: Namespace | None) -> None:
        if namespace is None or self._stop_requested:
            return
        paths: frozenset[Path] = frozenset()
        if (
            self._current_open_cache_path is not None
            and self._current_open_cache_source is not None
            and self._current_open_cache_source.namespace == namespace
        ):
            paths = frozenset({self._current_open_cache_path})
        try:
            request_id = self._command_vault.put(
                ProtectedPathsRequest(namespace, paths)
            )
        except Exception:  # pylint: disable=broad-exception-caught
            self._emit_failure('cache', FailureKind.CACHE_CORRUPT)
            return
        self._protectWorker.emit(request_id)

    def _configuration(
        self,
        *,
        enabled: bool | None = None,
        activation_id: str | None = None,
    ) -> WorkerConfiguration:
        pending = self._settings.pending_connection
        identity: ServerIdentity | None
        namespace: Namespace | None
        if pending is not None:
            origin = pending.origin
            identity = pending.identity
            namespace = namespace_for(origin, identity.organization.id)
            active_enabled = False
        else:
            origin = self._settings.origin
            identity = self._settings.identity
            namespace = self._configured_namespace(require_enabled=False)
            active_enabled = self._settings.enabled if enabled is None else enabled
        return WorkerConfiguration(
            origin=origin,
            namespace=namespace,
            enabled=active_enabled,
            automatic_upload=(
                self._settings.automatic_upload if active_enabled else False
            ),
            client_instance_uuid=self._settings.client_instance_uuid,
            cache_limit_bytes=self._settings.cache_limit_bytes,
            identity=identity,
            pending_connection=pending is not None,
            activation_id=activation_id,
        )

    def _prior_active_configuration(self) -> WorkerConfiguration:
        identity = self._settings.identity
        namespace = (
            None
            if identity is None
            else namespace_for(self._settings.origin, identity.organization.id)
        )
        return WorkerConfiguration(
            origin=self._settings.origin,
            namespace=namespace,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=self._settings.client_instance_uuid,
            cache_limit_bytes=self._settings.cache_limit_bytes,
            identity=identity,
        )

    def _configured_namespace(self, *, require_enabled: bool) -> Namespace | None:
        if (
            self._settings.identity is None
            or self._settings.pending_connection is not None
        ):
            return None
        if require_enabled and not self._settings.enabled:
            return None
        return self._known_namespace

    def _active_namespace(self, *, require_enabled: bool) -> Namespace | None:
        identity = self._identity
        proof = self._proof
        if identity is None or proof is None:
            return None
        if proof != (self._settings.origin, identity.organization.id):
            return None
        if self._settings.pending_connection is not None:
            return None
        if require_enabled and not self._settings.enabled:
            return None
        return namespace_for(self._settings.origin, identity.organization.id)

    def _require_active_namespace(self) -> Namespace:
        namespace = self._active_namespace(require_enabled=True)
        if namespace is not None:
            return namespace
        if not self._settings.enabled:
            raise ControllerError(_ENABLE_MESSAGE)
        raise ControllerError(_CONNECTION_MESSAGE)

    def _has_current_proof(self, origin: str) -> bool:
        identity = self._identity
        return (
            identity is not None
            and self._proof == (origin, identity.organization.id)
            and self._settings.origin == origin
        )

    def _set_automatic_upload_off(self, operation: str) -> bool:
        if not self._settings.automatic_upload:
            return True
        try:
            self._settings = self._settings_store.save_options(
                self._settings.enabled,
                False,
                self._settings.cache_limit_bytes,
            )
        except SettingsError:
            self._settings_failure(operation)
            return False
        self.settingsChanged.emit(self._settings)
        return True

    def _settings_failure(self, operation: str) -> None:
        self._proof = None
        self._identity = None
        self._settings = replace(
            self._settings,
            enabled=False,
            automatic_upload=False,
        )
        self._invalidate_archive_state()
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(None)
        self._queue_configuration(self._configuration(enabled=False))
        self._emit_failure(operation, FailureKind.SETTINGS)

    def _invalidate_identity(self) -> None:
        had_identity = self._identity is not None or self._proof is not None
        self._identity = None
        self._proof = None
        self._invalidate_archive_state()
        if had_identity:
            self.identityChanged.emit(None)

    def _invalidate_archive_state(self) -> None:
        self._browse_epoch += 1
        self._browse_filters = None
        self._next_cursor = None
        self._browse_requests.clear()
        self._online_requests.clear()
        self._cached_requests.clear()
        self._publish_requests.clear()
        self._ready_cache_paths.clear()
        self._open_cache_paths = (
            {self._current_open_cache_path}
            if self._current_open_cache_path is not None
            else set()
        )

    def _invalidate_requests(self) -> None:
        self._connection_tests.clear()
        self._active_connection_test = None
        self._credential_removals.clear()
        self._activation_previous.clear()
        self._invalidate_archive_state()

    def _cancel_connection_transactions(self) -> None:
        transactions = set(self._activation_previous)
        if self._active_connection_test is not None:
            transactions.add(self._active_connection_test)
        for transaction_id in transactions:
            self._cancelConnectionWorker.emit(transaction_id)
        self._connection_tests.clear()
        self._activation_previous.clear()
        self._active_connection_test = None

    def _put_command(self, command: object) -> str:
        try:
            return self._command_vault.put(command)
        except Exception as error:
            raise ControllerError(_INVALID_REQUEST_MESSAGE) from error

    def _discard_stage(self, path: Path) -> None:
        self._discardWorker.emit(str(path))

    def _emit_failure(self, operation: str, kind: FailureKind) -> None:
        self.operationFailed.emit(operation, _failure(kind))

    def _require_job_id(self, job_id: str) -> None:
        value: object = job_id
        if not isinstance(value, str) or len(value) != 32:
            raise ControllerError(_INVALID_REQUEST_MESSAGE)

    def _require_command_state(self) -> None:
        self._require_ui_thread()
        if self._stop_requested or self._shutdown_complete:
            raise ControllerError(_INACTIVE_MESSAGE)

    def _require_ui_thread(self) -> None:
        if QThread.currentThread() is not self.thread():
            raise ControllerError(_UI_THREAD_MESSAGE)


def _connect(
    signal: object,
    slot: object,
    connection_type: Qt.ConnectionType,
) -> None:
    connect = cast(_Signal, signal).connect
    connect(slot, connection_type)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_origin(origin: object) -> str:
    if not isinstance(origin, str):
        raise ControllerError(_INVALID_OPTIONS_MESSAGE)
    try:
        return canonical_origin(origin)
    except SettingsError:
        raise ControllerError(_INVALID_OPTIONS_MESSAGE) from None


def _absolute_path(path: object) -> Path:
    if not isinstance(path, Path):
        raise TypeError
    return Path(os.path.abspath(os.fspath(path)))


def _aware_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError
    return value.astimezone(UTC)


def _settings_namespace(settings: ConnectorSettings) -> Namespace | None:
    identity = settings.identity
    if identity is None:
        return None
    return namespace_for(settings.origin, identity.organization.id)


def _failure(kind: FailureKind) -> PublicFailure:
    return PublicFailure(
        kind=kind,
        code=kind.value,
        message=FAILURE_MESSAGES[kind],
        retryable=kind in {FailureKind.OFFLINE, FailureKind.RATE_LIMITED},
    )


__all__ = [
    'ControllerError',
    'RoastServerController',
    'SHUTDOWN_TIMEOUT_MESSAGE',
]
