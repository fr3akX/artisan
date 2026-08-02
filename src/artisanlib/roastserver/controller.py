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

from collections import OrderedDict
from collections.abc import Callable
import copy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from typing import Final, Protocol, cast, override
from uuid import UUID

from PyQt6.QtCore import QByteArray, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot

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
    RoastSummary,
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
    ConfigurationFence,
    ConnectionTestRequest,
    OnlineOpenRequest,
    OpenCancellationToken,
    OpaqueVault,
    PendingConnectionRecovery,
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
_SETTLEMENT_MESSAGE: Final[str] = 'Roast Server credential rollback is still settling.'
_ROLLBACK_SETTLEMENT_TIMEOUT_MS: Final[int] = 15_000
_MAX_ARCHIVE_ROWS: Final[int] = 5_000
_MAX_READY_CACHE_PATHS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class ArchiveRow:
    roast: RoastSummary
    cached_revision: int | None
    cached_sha256: str | None
    stale: bool
    cached: CachedRevision | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ArchivePageView:
    rows: tuple[ArchiveRow, ...]
    next_cursor: str | None
    online: bool
    retained_error: PublicFailure | None


@dataclass(frozen=True, slots=True)
class _BrowseTracking:
    epoch: int
    cursor: str | None
    refresh: bool


@dataclass(frozen=True, slots=True)
class _OnlineTracking:
    namespace: Namespace
    roast_uuid: UUID
    generation: int
    token: OpenCancellationToken


@dataclass(frozen=True, slots=True)
class _CachedTracking:
    cached: CachedRevision
    generation: int
    token: OpenCancellationToken


@dataclass(frozen=True, slots=True)
class _PublishTracking:
    open_request_id: str
    namespace: Namespace
    roast_uuid: UUID
    request: PublishRequest
    generation: int
    token: OpenCancellationToken


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
    cachedFallbackReady = pyqtSignal(str, object)
    operationFailed = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    profileReady = pyqtSignal(str, object)

    _configureWorker = pyqtSignal(object)
    _testWorker = pyqtSignal(str)
    _commitWorker = pyqtSignal(str)
    _finalizeWorker = pyqtSignal(str)
    _acknowledgeConnectionWorker = pyqtSignal(str)
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
        self._configuration_fence = ConfigurationFence()
        self._latest_configuration_validation: str | None = None
        self._authorized_startup_validation: str | None = None
        self._connection_tests: dict[str, str] = {}
        self._activation_previous: dict[str, ConnectorSettings] = {}
        self._rollback_settlements: set[str] = set()
        self._active_connection_test: str | None = None
        self._credential_removals: set[str] = set()
        self._browse_epoch = 0
        self._browse_filters: ArchiveFilters | None = None
        self._next_cursor: str | None = None
        self._browse_active_id: str | None = None
        self._pending_browse_id: str | None = None
        self._browse_requests: dict[str, _BrowseTracking] = {}
        self._browse_failures: dict[str, PublicFailure] = {}
        self._consumed_browse_cursors: set[str] = set()
        self._known_cached_revisions: OrderedDict[
            UUID, CachedRevision
        ] = OrderedDict()
        self._current_archive_revisions: dict[UUID, int] = {}
        self._open_generation = 0
        self._online_requests: dict[str, _OnlineTracking] = {}
        self._cached_requests: dict[str, _CachedTracking] = {}
        self._publish_requests: dict[str, _PublishTracking] = {}
        self._ready_cache_paths: OrderedDict[
            Path, ServerProfileSource
        ] = OrderedDict()
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
            configuration_fence=self._configuration_fence,
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
        _connect(
            self._acknowledgeConnectionWorker,
            worker.acknowledge_connection_activation,
            queued,
        )
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
            worker.connectionRollbackFinished,
            self._on_connection_rollback_finished,
            queued,
        )
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
        _connect(worker.browseFinished, self._on_browse_finished, queued)
        _connect(worker.downloadStaged, self._on_download_staged, queued)
        _connect(worker.cachedReady, self._on_cached_ready, queued)
        _connect(worker.cachedFallbackReady, self._on_cached_fallback, queued)
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
        self._queue_configuration(self._configuration(), authorize_startup=True)
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
            self._thread.requestInterruption()
            self._revoke_configuration()
            self._invalidate_requests()
            self._credential_vault.clear()
            self._profile_vault.clear()
            self._command_vault.clear()
            self._stopWorker.emit()
        stopped = self._thread.wait(timeout_ms)
        if not stopped:
            _log.error(SHUTDOWN_TIMEOUT_MESSAGE)
            return False
        self._shutdown_complete = True
        return True

    def test_connection(self, origin: str, candidate: str) -> str:
        self._require_command_state()
        generation = self._revoke_configuration()
        canonical = _canonical_origin(origin)
        candidate_value: object = candidate
        if not isinstance(candidate_value, str) or candidate_value == '':
            raise ControllerError(_INVALID_REQUEST_MESSAGE)

        self._cancel_connection_transactions()
        if (
            self._activation_previous
            or self._settings.pending_connection is not None
        ):
            raise ControllerError(_SETTLEMENT_MESSAGE)
        try:
            self._settings = self._settings_store.save_options(
                False,
                False,
                self._settings.cache_limit_bytes,
            )
        except SettingsError:
            self._settings_failure('connection', generation=generation)
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self.settingsChanged.emit(self._settings)
        self._invalidate_identity()
        candidate_generation = self._configuration_fence.advance()
        candidate_configuration = self._configuration(enabled=False)
        if candidate_configuration.origin != canonical:
            candidate_configuration = replace(
                candidate_configuration,
                origin=canonical,
                namespace=None,
                identity=None,
                pending_connection=False,
                activation_id=None,
            )
        self._queue_configuration(
            candidate_configuration, generation=candidate_generation
        )
        try:
            request_id = self._credential_vault.put_latest(
                ConnectionTestRequest(
                    canonical, candidate, candidate_generation
                )
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
        if self._settings.pending_connection is not None:
            self._cancel_connection_transactions()
            raise ControllerError(_SETTLEMENT_MESSAGE)
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
            generation = self._revoke_configuration()
            old_configuration = self._configuration(enabled=False)
            self._queue_configuration(old_configuration, generation=generation)
            self._invalidate_identity()
            try:
                self._settings = self._settings_store.set_origin(canonical)
                self._known_namespace = _settings_namespace(self._settings)
            except SettingsError:
                self._settings_failure('settings', generation=generation)
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
        generation = self._revoke_configuration()
        self._cancel_connection_transactions()
        try:
            self._settings = self._settings_store.save_options(
                False,
                False,
                self._settings.cache_limit_bytes,
            )
        except SettingsError:
            self._settings_failure('settings', generation=generation)
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self.settingsChanged.emit(self._settings)
        self._invalidate_identity()
        self._queue_configuration(
            self._configuration(enabled=False), generation=generation
        )

    def cancel_connection_test(self, request_id: str) -> None:
        self._require_command_state()
        request_value: object = request_id
        if not isinstance(request_value, str):
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        if request_id != self._active_connection_test:
            return
        generation = self._revoke_configuration()
        self._credential_vault.clear()
        self._cancel_connection_transaction(request_id)
        self._queue_configuration(
            self._configuration(enabled=False), generation=generation
        )

    def save_configuration_geometry(self, geometry: QByteArray) -> None:
        self._save_geometry(geometry, browser=False)

    def save_browser_geometry(self, geometry: QByteArray) -> None:
        self._save_geometry(geometry, browser=True)

    def _save_geometry(self, geometry: QByteArray, *, browser: bool) -> None:
        self._require_command_state()
        geometry_value: object = geometry
        if not isinstance(geometry_value, QByteArray) or geometry_value.isEmpty():
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        detached = QByteArray(geometry_value)
        configuration_geometry = (
            self._settings.configuration_geometry if browser else detached
        )
        browser_geometry = detached if browser else self._settings.browser_geometry
        try:
            self._settings_store.save_geometry(
                configuration_geometry,
                browser_geometry,
            )
        except SettingsError:
            self._settings_failure('geometry')
            raise ControllerError(SETTINGS_FAILURE_MESSAGE) from None
        self._settings = replace(
            self._settings,
            configuration_geometry=configuration_geometry,
            browser_geometry=browser_geometry,
        )

    def remove_credential(self) -> None:
        self._require_command_state()
        if self._settings.pending_connection is not None:
            self._cancel_connection_transactions()
            raise ControllerError(_SETTLEMENT_MESSAGE)
        generation = self._revoke_configuration()
        paused = self._configuration(enabled=False)
        if not self._set_automatic_upload_off('remove'):
            raise ControllerError(_INVALID_OPTIONS_MESSAGE)
        self._invalidate_identity()
        self._queue_configuration(paused, generation=generation)
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
        self._consumed_browse_cursors.clear()
        self._browse_failures.clear()
        self._replace_pending_browse()
        request_id = self._put_command(
            BrowseRequest(namespace, validated, None, refresh)
        )
        self._browse_requests[request_id] = _BrowseTracking(
            self._browse_epoch, None, refresh
        )
        if self._browse_active_id is None:
            self._launch_browse(request_id)
        else:
            self._pending_browse_id = request_id
        self._prune_auxiliary_state()
        return request_id

    def load_more(self) -> str | None:
        self._require_command_state()
        namespace = self._active_namespace(require_enabled=True)
        filters = self._browse_filters
        cursor = self._next_cursor
        if (
            namespace is None
            or filters is None
            or cursor is None
            or self._browse_active_id is not None
            or self._pending_browse_id is not None
        ):
            return None
        self._next_cursor = None
        request_id = self._put_command(
            BrowseRequest(namespace, filters, cursor, False)
        )
        self._browse_requests[request_id] = _BrowseTracking(
            self._browse_epoch, cursor, False
        )
        self._launch_browse(request_id)
        return request_id

    def open_roast(self, roast_uuid: UUID) -> str:
        self._require_command_state()
        namespace = self._require_active_namespace()
        if not isinstance(roast_uuid, UUID):
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        generation, token = self._begin_open()
        cached = self._known_cached_revisions.get(roast_uuid)
        if cached is not None:
            self._known_cached_revisions.move_to_end(roast_uuid)
        current_revision = self._current_archive_revisions.get(roast_uuid)
        fallback = (
            cached
            if cached is not None
            and cached.revision.revision_number == current_revision
            else None
        )
        request_id = self._put_command(
            OnlineOpenRequest(namespace, roast_uuid, fallback, token)
        )
        self._online_requests[request_id] = _OnlineTracking(
            namespace, roast_uuid, generation, token
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
        generation, token = self._begin_open()
        request_id = self._put_command(CachedOpenRequest(cached, token))
        self._cached_requests[request_id] = _CachedTracking(
            cached, generation, token
        )
        self._openCachedWorker.emit(request_id)
        return request_id

    def cancel_open(self, request_id: str) -> None:
        self._require_command_state()
        request_value: object = request_id
        if not isinstance(request_value, str):
            raise ControllerError(_INVALID_REQUEST_MESSAGE)
        online = self._online_requests.pop(request_id, None)
        if online is not None:
            online.token.cancel()
            self._take_queued_command(request_id)
        cached = self._cached_requests.pop(request_id, None)
        if cached is not None:
            cached.token.cancel()
            self._take_queued_command(request_id)
        for publish_id, tracked in tuple(self._publish_requests.items()):
            if tracked.open_request_id == request_id:
                tracked.token.cancel()
                self._publish_requests.pop(publish_id, None)
        self._prune_auxiliary_state()

    def close_browser(self) -> None:
        self._require_command_state()
        self._browse_epoch += 1
        self._browse_filters = None
        self._next_cursor = None
        self._consumed_browse_cursors.clear()
        self._replace_pending_browse()
        self._prune_auxiliary_state()

    def clear_unused_cache(self) -> None:
        self._require_command_state()
        namespace = self._require_active_namespace()
        request_id = self._put_command(ClearUnusedRequest(namespace))
        self._clearWorker.emit(request_id)

    def cached_revision_for(
        self, source: ServerProfileSource
    ) -> CachedRevision | None:
        self._require_ui_thread()
        cached = self._known_cached_revisions.get(source.roast_uuid)
        if (
            self._active_namespace(require_enabled=False) != source.namespace
            or cached is None
            or cached.namespace != source.namespace
            or cached.revision.revision_number != source.revision_number
            or cached.revision.sha256 != source.sha256
        ):
            return None
        self._known_cached_revisions.move_to_end(source.roast_uuid)
        return cached

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
        self._ready_cache_paths.move_to_end(canonical_path)
        self._prune_ready_cache_paths()
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
        if self._stop_requested:
            return
        if (
            request_id != self._active_connection_test
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
        if self._stop_requested:
            return
        if not isinstance(value, ServerIdentity):
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
            self._begin_rollback_settlement(request_id)
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
        if self._stop_requested:
            return
        if (
            request_id != self._active_connection_test
            or not isinstance(value, ServerIdentity)
        ):
            self._cancelConnectionWorker.emit(request_id)
            return
        previous = self._activation_previous.get(request_id)
        pending = self._settings.pending_connection
        if previous is None or pending is None or pending.identity != value:
            self._active_connection_test = None
            self._begin_rollback_settlement(request_id)
            self._emit_failure(request_id, FailureKind.CREDENTIAL_REJECTED)
            return
        try:
            self._settings = self._settings_store.activate_pending_connection(
                pending.origin, value
            )
            self._known_namespace = _settings_namespace(self._settings)
        except SettingsError:
            self._active_connection_test = None
            try:
                self._settings = self._settings_store.save_pending_connection(
                    pending.origin, value
                )
            except SettingsError:
                pass
            else:
                self._begin_rollback_settlement(request_id)
            self._identity = None
            self._proof = None
            self._invalidate_archive_state()
            self.settingsChanged.emit(self._settings)
            self.identityChanged.emit(None)
            self._queue_configuration(
                self._configuration(enabled=False, activation_id=request_id)
            )
            self._emit_failure(request_id, FailureKind.SETTINGS)
            return
        self._active_connection_test = None
        self._activation_previous.pop(request_id, None)
        self._rollback_settlements.discard(request_id)
        self._identity = value
        self._proof = (self._settings.origin, value.organization.id)
        self._invalidate_archive_state()
        self._acknowledgeConnectionWorker.emit(request_id)
        self._queue_configuration(self._configuration())
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(value)

    @pyqtSlot(str, bool)
    def _on_connection_rollback_finished(
        self, transaction_id: str, succeeded: bool
    ) -> None:
        if self._stop_requested or type(succeeded) is not bool:
            return
        if (
            transaction_id not in self._rollback_settlements
            and transaction_id not in self._activation_previous
        ):
            return
        self._rollback_settlements.discard(transaction_id)
        if transaction_id == self._active_connection_test:
            self._active_connection_test = None
        previous = self._activation_previous.get(transaction_id)
        if not succeeded or previous is None:
            self._identity = None
            self._proof = None
            self._invalidate_archive_state()
            self.settingsChanged.emit(self._settings)
            self.identityChanged.emit(None)
            self._queue_configuration(self._configuration(enabled=False))
            self._emit_failure(transaction_id, FailureKind.KEYRING)
            return
        expected = self._settings.pending_connection
        try:
            self._settings = self._settings_store.rollback_pending_connection(
                previous, expected
            )
            self._known_namespace = _settings_namespace(self._settings)
        except SettingsError:
            try:
                self._settings = self._settings_store.load()
            except SettingsError:
                pass
            self._identity = None
            self._proof = None
            self._invalidate_archive_state()
            self.settingsChanged.emit(self._settings)
            self.identityChanged.emit(None)
            self._queue_configuration(self._configuration(enabled=False))
            self._emit_failure(transaction_id, FailureKind.SETTINGS)
            return
        self._activation_previous.pop(transaction_id, None)
        self._identity = None
        self._proof = None
        self._invalidate_archive_state()
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(None)
        self._queue_configuration(self._configuration(enabled=False))

    @pyqtSlot(str, object)
    def _on_pending_connection_recovery_required(
        self, transaction_id: str, value: object
    ) -> None:
        if self._stop_requested:
            return
        recovery = value if isinstance(value, PendingConnectionRecovery) else None
        pending = self._settings.pending_connection
        if (
            self._active_connection_test is not None
            or pending is None
            or recovery is None
        ):
            return
        self._proof = None
        self._identity = None
        self._invalidate_archive_state()
        prior_matches = (
            recovery.authenticated_identity is not None
            and pending.origin == self._settings.origin
            and recovery.authenticated_identity == self._settings.identity
        )
        target_credential_absent = not recovery.credential_present
        if not prior_matches and not target_credential_absent:
            self.settingsChanged.emit(self._settings)
            self.identityChanged.emit(None)
            self._queue_configuration(self._configuration(enabled=False))
            self.operationFailed.emit(transaction_id, recovery.failure)
            return
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
            self._queue_configuration(self._configuration(enabled=False))
            self._emit_failure(transaction_id, FailureKind.SETTINGS)
            return
        self.settingsChanged.emit(self._settings)
        self.identityChanged.emit(None)
        self._queue_configuration(
            self._configuration(enabled=False), authorize_startup=True
        )
        self.operationFailed.emit(transaction_id, recovery.failure)

    @pyqtSlot(object)
    def _on_configuration_validated(self, value: object) -> None:
        if self._stop_requested or not isinstance(value, WorkerConfiguration):
            return
        validation_id = value.validation_id
        if (
            validation_id != self._latest_configuration_validation
            or validation_id != self._authorized_startup_validation
            or not self._configuration_fence.authorizes(value.generation)
        ):
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
        self._authorized_startup_validation = None
        self._identity = identity
        self._proof = (self._settings.origin, identity.organization.id)
        self.identityChanged.emit(identity)

    @pyqtSlot(str)
    def _on_credential_removed(self, request_id: str) -> None:
        if self._stop_requested or request_id not in self._credential_removals:
            return
        self._credential_removals.discard(request_id)
        self.onlineChanged.emit(False)

    @pyqtSlot(str, object)
    def _on_operation_failed(self, operation: str, value: object) -> None:
        if self._stop_requested:
            return
        if not isinstance(value, PublicFailure):
            failure = _failure(FailureKind.INVALID_RESPONSE)
        else:
            failure = value
        public_operation = operation
        browse = self._browse_requests.get(operation)
        if browse is not None and browse.epoch == self._browse_epoch:
            self._browse_failures[operation] = failure
            if browse.cursor is not None:
                self._next_cursor = browse.cursor
        published = self._publish_requests.pop(operation, None)
        if published is not None:
            if published.token.is_cancelled():
                return
            public_operation = published.open_request_id
        cached = self._cached_requests.get(operation)
        if cached is not None:
            if cached.token.is_cancelled():
                return
            cached.token.cancel()
            self._cached_requests.pop(operation, None)
        online = self._online_requests.get(operation)
        if online is not None:
            if online.token.is_cancelled():
                return
            if not failure.retryable:
                online.token.cancel()
                self._online_requests.pop(operation, None)
        if operation == self._active_connection_test:
            self._connection_tests.pop(operation, None)
            self._active_connection_test = None
            if operation in self._activation_previous:
                self._begin_rollback_settlement(operation)
            else:
                self._cancelConnectionWorker.emit(operation)
        if failure.kind is FailureKind.CREDENTIAL_REJECTED:
            paused = self._configuration(enabled=False)
            if not self._set_automatic_upload_off(public_operation):
                return
            self._invalidate_identity()
            self._queue_configuration(paused)
        self.operationFailed.emit(public_operation, failure)

    @pyqtSlot(object)
    def _on_queue_changed(self, value: object) -> None:
        if not self._stop_requested and isinstance(value, QueueCounts):
            self.queueChanged.emit(value)

    @pyqtSlot(object)
    def _on_failed_jobs_changed(self, value: object) -> None:
        if (
            not self._stop_requested
            and isinstance(value, tuple)
            and all(isinstance(item, FailedJob) for item in value)
        ):
            self.failedJobsChanged.emit(value)

    @pyqtSlot(object)
    def _on_cache_stats_changed(self, value: object) -> None:
        if not self._stop_requested and isinstance(value, CacheStats):
            self.cacheStatsChanged.emit(value)

    @pyqtSlot(str, object)
    def _on_archive_page(self, request_id: str, value: object) -> None:
        if self._stop_requested:
            return
        tracked = self._browse_requests.get(request_id)
        if (
            tracked is None
            or request_id != self._browse_active_id
            or tracked.epoch != self._browse_epoch
        ):
            return
        retained_error = self._browse_failures.pop(request_id, None)
        if isinstance(value, RoastPage):
            if value.next_cursor is not None and (
                value.next_cursor == tracked.cursor
                or value.next_cursor in self._consumed_browse_cursors
            ):
                self._next_cursor = None
                self.operationFailed.emit(
                    request_id, _failure(FailureKind.INVALID_RESPONSE)
                )
                return
            if tracked.cursor is not None:
                self._consumed_browse_cursors.add(tracked.cursor)
            self._next_cursor = value.next_cursor
            if tracked.cursor is None:
                self._current_archive_revisions.clear()
            for roast in value.items:
                self._current_archive_revisions[roast.roast_uuid] = roast.revision_count
            self._bound_current_archive_revisions()
            rows = tuple(self._online_archive_row(roast) for roast in value.items)
            view = ArchivePageView(
                rows=_newest_first(rows)[:_MAX_ARCHIVE_ROWS],
                next_cursor=value.next_cursor,
                online=True,
                retained_error=None,
            )
        elif isinstance(value, CachedPage):
            self._next_cursor = None
            self._consumed_browse_cursors.clear()
            rows = _newest_first(
                tuple(
                    ArchiveRow(
                        roast=cached.roast,
                        cached_revision=cached.revision.revision_number,
                        cached_sha256=cached.revision.sha256,
                        stale=True,
                        cached=cached,
                    )
                    for cached in value.items
                )
            )[:_MAX_ARCHIVE_ROWS]
            for row in rows:
                cached = row.cached
                if cached is not None:
                    self._current_archive_revisions[row.roast.roast_uuid] = (
                        row.roast.revision_count
                    )
                    self._remember_cached(cached, prune=False)
            self._bound_current_archive_revisions()
            self._prune_known_cached_revisions()
            view = ArchivePageView(
                rows=rows,
                next_cursor=None,
                online=False,
                retained_error=retained_error,
            )
        else:
            self.operationFailed.emit(
                request_id, _failure(FailureKind.INVALID_RESPONSE)
            )
            return
        self._prune_auxiliary_state()
        self.archivePageReady.emit(request_id, view)

    @pyqtSlot(str)
    def _on_browse_finished(self, request_id: str) -> None:
        if self._stop_requested or request_id != self._browse_active_id:
            return
        self._browse_requests.pop(request_id, None)
        self._browse_failures.pop(request_id, None)
        self._browse_active_id = None
        pending = self._pending_browse_id
        self._pending_browse_id = None
        if pending is not None and pending in self._browse_requests:
            self._launch_browse(pending)
        self._prune_auxiliary_state()

    def _online_archive_row(self, roast: RoastSummary) -> ArchiveRow:
        cached = self._known_cached_revisions.get(roast.roast_uuid)
        if cached is None or cached.revision.revision_number != roast.revision_count:
            return ArchiveRow(roast, None, None, False)
        self._known_cached_revisions.move_to_end(roast.roast_uuid)
        return ArchiveRow(
            roast,
            cached.revision.revision_number,
            cached.revision.sha256,
            False,
            cached,
        )

    @pyqtSlot(str, object)
    def _on_download_staged(self, request_id: str, value: object) -> None:
        if self._stop_requested:
            return
        tracked = self._online_requests.get(request_id)
        if not isinstance(value, PublishRequest):
            invalid = self._online_requests.pop(request_id, None)
            if invalid is not None:
                invalid.token.cancel()
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        if tracked is None:
            self._discard_stage(value.staged_path)
            return
        if (
            tracked.generation != self._open_generation
            or tracked.token.is_cancelled()
            or value.token is not tracked.token
            or self._active_namespace(require_enabled=True) != tracked.namespace
            or value.detail.roast_uuid != tracked.roast_uuid
        ):
            self._online_requests.pop(request_id, None)
            tracked.token.cancel()
            self._discard_stage(value.staged_path)
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        try:
            self._profile_validator(value.staged_path)
        except Exception:  # pylint: disable=broad-exception-caught
            self._online_requests.pop(request_id, None)
            tracked.token.cancel()
            self._discard_stage(value.staged_path)
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        if tracked.token.is_cancelled():
            self._online_requests.pop(request_id, None)
            self._discard_stage(value.staged_path)
            return
        self._online_requests.pop(request_id, None)
        try:
            publish_id = self._command_vault.put(value)
        except Exception:  # pylint: disable=broad-exception-caught
            tracked.token.cancel()
            self._discard_stage(value.staged_path)
            self._emit_failure(request_id, FailureKind.INVALID_RESPONSE)
            return
        if tracked.token.is_cancelled():
            self._take_queued_command(publish_id)
            self._discard_stage(value.staged_path)
            return
        self._publish_requests[publish_id] = _PublishTracking(
            request_id,
            tracked.namespace,
            tracked.roast_uuid,
            value,
            tracked.generation,
            tracked.token,
        )
        self._publishWorker.emit(publish_id)

    @pyqtSlot(str, object)
    def _on_cached_ready(self, request_id: str, value: object) -> None:
        if self._stop_requested:
            return
        tracked = self._cached_requests.pop(request_id, None)
        if tracked is None or not isinstance(value, CachedRevision):
            return
        if (
            tracked.generation != self._open_generation
            or tracked.token.is_cancelled()
            or value != tracked.cached
            or self._active_namespace(require_enabled=True) != value.namespace
        ):
            return
        self._current_archive_revisions[value.roast.roast_uuid] = (
            value.revision.revision_number
        )
        self._bound_current_archive_revisions()
        self._remember_cached(value)
        self._emit_profile_ready(value, stale=True)

    @pyqtSlot(str, object)
    def _on_cached_fallback(self, request_id: str, value: object) -> None:
        if self._stop_requested:
            return
        tracked = self._online_requests.pop(request_id, None)
        if tracked is None or not isinstance(value, CachedRevision):
            return
        if (
            tracked.generation != self._open_generation
            or tracked.token.is_cancelled()
            or value.namespace != tracked.namespace
            or value.roast.roast_uuid != tracked.roast_uuid
            or value.revision.revision_number != value.roast.revision_count
            or self._active_namespace(require_enabled=True) != tracked.namespace
        ):
            return
        self._remember_cached(value)
        self.cachedFallbackReady.emit(request_id, value)

    @pyqtSlot(str, object)
    def _on_cache_published(self, request_id: str, value: object) -> None:
        if self._stop_requested:
            return
        tracked = self._publish_requests.pop(request_id, None)
        if tracked is None or not isinstance(value, CachedRevision):
            return
        revision = tracked.request.detail.current_revision
        if (
            tracked.generation != self._open_generation
            or tracked.token.is_cancelled()
            or revision is None
            or value.namespace != tracked.namespace
            or value.roast.roast_uuid != tracked.roast_uuid
            or value.revision.sha256 != revision.sha256
            or value.revision.revision_number != revision.revision_number
            or self._active_namespace(require_enabled=True) != tracked.namespace
        ):
            return
        self._current_archive_revisions[tracked.roast_uuid] = revision.revision_number
        self._bound_current_archive_revisions()
        self._remember_cached(value)
        self._emit_profile_ready(value, stale=False)

    @pyqtSlot(bool)
    def _on_online_changed(self, value: bool) -> None:
        if not self._stop_requested and type(value) is bool:
            self.onlineChanged.emit(value)

    def _emit_profile_ready(self, cached: CachedRevision, *, stale: bool) -> None:
        source = replace(cached.source, stale=stale)
        path = _absolute_path(cached.path)
        self._ready_cache_paths[path] = source
        self._ready_cache_paths.move_to_end(path)
        self._prune_ready_cache_paths()
        self.profileReady.emit(str(path), source)

    def _launch_browse(self, request_id: str) -> None:
        self._browse_active_id = request_id
        self._browseWorker.emit(request_id)

    def _replace_pending_browse(self) -> None:
        pending = self._pending_browse_id
        self._pending_browse_id = None
        if pending is None:
            return
        self._browse_requests.pop(pending, None)
        self._browse_failures.pop(pending, None)
        self._take_queued_command(pending)

    def _begin_open(self) -> tuple[int, OpenCancellationToken]:
        self._open_generation += 1
        self._cancel_all_opens()
        token = OpenCancellationToken()
        return self._open_generation, token

    def _cancel_all_opens(self) -> None:
        for request_id, online in tuple(self._online_requests.items()):
            online.token.cancel()
            self._take_queued_command(request_id)
        for request_id, cached in tuple(self._cached_requests.items()):
            cached.token.cancel()
            self._take_queued_command(request_id)
        for published in tuple(self._publish_requests.values()):
            published.token.cancel()
        self._online_requests.clear()
        self._cached_requests.clear()
        self._publish_requests.clear()

    def _remember_cached(
        self, cached: CachedRevision, *, prune: bool = True
    ) -> None:
        roast_uuid = cached.roast.roast_uuid
        self._known_cached_revisions[roast_uuid] = cached
        self._known_cached_revisions.move_to_end(roast_uuid)
        if prune:
            self._prune_known_cached_revisions()

    def _prune_auxiliary_state(self) -> None:
        self._prune_known_cached_revisions()
        self._prune_ready_cache_paths()

    def _prune_known_cached_revisions(self) -> None:
        for roast_uuid in self._current_archive_revisions:
            if roast_uuid in self._known_cached_revisions:
                self._known_cached_revisions.move_to_end(roast_uuid)
        current_source = self._current_open_cache_source
        if (
            current_source is not None
            and current_source.roast_uuid in self._known_cached_revisions
        ):
            self._known_cached_revisions.move_to_end(current_source.roast_uuid)
        while len(self._known_cached_revisions) > _MAX_ARCHIVE_ROWS:
            self._known_cached_revisions.popitem(last=False)

    def _prune_ready_cache_paths(self) -> None:
        current = self._current_open_cache_path
        if current is not None and current in self._ready_cache_paths:
            self._ready_cache_paths.move_to_end(current)
        while len(self._ready_cache_paths) > _MAX_READY_CACHE_PATHS:
            oldest = next(iter(self._ready_cache_paths))
            if oldest == current:
                self._ready_cache_paths.move_to_end(oldest)
                continue
            self._ready_cache_paths.pop(oldest, None)

    def _bound_current_archive_revisions(self) -> None:
        while len(self._current_archive_revisions) > _MAX_ARCHIVE_ROWS:
            oldest = next(iter(self._current_archive_revisions))
            self._current_archive_revisions.pop(oldest, None)

    def _take_queued_command(self, request_id: str) -> bool:
        try:
            self._command_vault.take(request_id)
        except KeyError:
            return False
        return True

    def _queue_configuration(
        self,
        configuration: WorkerConfiguration,
        *,
        authorize_startup: bool = False,
        generation: int | None = None,
    ) -> None:
        selected_generation = (
            self._configuration_fence.advance()
            if generation is None
            else generation
        )
        queued = replace(configuration, generation=selected_generation)
        self._latest_configuration_validation = queued.validation_id
        self._authorized_startup_validation = (
            queued.validation_id if authorize_startup else None
        )
        self._configureWorker.emit(queued)
        self._queue_protected_paths(queued.namespace)

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

    def _settings_failure(
        self, operation: str, *, generation: int | None = None
    ) -> None:
        selected_generation = (
            self._revoke_configuration() if generation is None else generation
        )
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
        self._queue_configuration(
            self._configuration(enabled=False), generation=selected_generation
        )
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
        self._consumed_browse_cursors.clear()
        self._replace_pending_browse()
        self._browse_requests.clear()
        self._browse_failures.clear()
        self._open_generation += 1
        self._cancel_all_opens()
        current_cached = next(
            (
                cached
                for cached in self._known_cached_revisions.values()
                if self._current_open_cache_path is not None
                and _absolute_path(cached.path) == self._current_open_cache_path
            ),
            None,
        )
        self._known_cached_revisions.clear()
        if current_cached is not None:
            self._remember_cached(current_cached)
        self._current_archive_revisions.clear()
        current_ready = (
            None
            if self._current_open_cache_path is None
            else self._ready_cache_paths.get(self._current_open_cache_path)
        )
        self._ready_cache_paths.clear()
        if self._current_open_cache_path is not None and current_ready is not None:
            self._ready_cache_paths[self._current_open_cache_path] = current_ready
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
        self._rollback_settlements.clear()
        self._invalidate_archive_state()

    def _disallow_configuration_proof(self) -> None:
        self._latest_configuration_validation = None
        self._authorized_startup_validation = None

    def _revoke_configuration(self) -> int:
        self._disallow_configuration_proof()
        return self._configuration_fence.revoke()

    def _cancel_connection_transaction(self, transaction_id: str) -> None:
        if transaction_id != self._active_connection_test:
            return
        self._connection_tests.pop(transaction_id, None)
        self._active_connection_test = None
        if transaction_id in self._activation_previous:
            self._begin_rollback_settlement(transaction_id)
        else:
            self._cancelConnectionWorker.emit(transaction_id)

    def _begin_rollback_settlement(self, transaction_id: str) -> None:
        if transaction_id in self._rollback_settlements:
            return
        if transaction_id not in self._activation_previous:
            self._cancelConnectionWorker.emit(transaction_id)
            return
        self._rollback_settlements.add(transaction_id)
        self._cancelConnectionWorker.emit(transaction_id)
        QTimer.singleShot(
            _ROLLBACK_SETTLEMENT_TIMEOUT_MS,
            lambda: self._report_rollback_timeout(transaction_id),
        )

    def _report_rollback_timeout(self, transaction_id: str) -> None:
        if self._stop_requested or transaction_id not in self._rollback_settlements:
            return
        self._identity = None
        self._proof = None
        self._queue_configuration(self._configuration(enabled=False))
        self._emit_failure(transaction_id, FailureKind.KEYRING)

    def _cancel_connection_transactions(self) -> None:
        self._credential_vault.clear()
        active = self._active_connection_test
        if active is not None:
            self._cancel_connection_transaction(active)
        for transaction_id in tuple(self._activation_previous):
            self._begin_rollback_settlement(transaction_id)
        self._connection_tests.clear()

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


def _newest_first(rows: tuple[ArchiveRow, ...]) -> tuple[ArchiveRow, ...]:
    return tuple(
        sorted(
            rows,
            key=lambda row: (row.roast.roast_at, row.roast.roast_uuid.hex),
            reverse=True,
        )
    )


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
    'ArchivePageView',
    'ArchiveRow',
    'ControllerError',
    'RoastServerController',
    'SHUTDOWN_TIMEOUT_MESSAGE',
]
