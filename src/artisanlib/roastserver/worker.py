#
# ABOUT
# Artisan Roast Server single-threaded QObject worker
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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
import math
from pathlib import Path
import re
import threading
from typing import Final, TypeGuard, override
from uuid import UUID, uuid4

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ApiFailure, ClientFactory, DownloadReceipt
from artisanlib.roastserver.cache import (
    CACHE_FAILURE,
    CacheError,
    CachedPage,
    CachedRevision,
    CacheStore,
)
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    MAX_CURSOR_CHARS,
    MAX_PROFILE_BYTES,
    ArchiveFilters,
    FailureKind,
    Namespace,
    PublicFailure,
    RevisionUpload,
    RoastDetail,
    RoastPage,
    ServerIdentity,
    validate_archive_filters,
)
from artisanlib.roastserver.inventory_contract import (
    MAX_CACHED_LOTS,
    MAX_INVENTORY_PAGES,
    BeanLot,
    BeanLotPage,
    InventoryCommandRequest,
    InventoryMutationResult,
)
from artisanlib.roastserver.inventory_store import (
    InventoryCommand,
    InventoryStore,
    InventoryStoreError,
)
from artisanlib.roastserver.metadata import project_profile
from artisanlib.roastserver.origin import SettingsError, canonical_origin
from artisanlib.roastserver.outbox import (
    Job,
    LeaseFailure,
    Outbox,
    OutboxError,
    Snapshot,
)
from artisanlib.roastserver.protection import ProtectionRegistry
from artisanlib.roastserver.settings import (
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    CredentialStore,
    CredentialStoreError,
    namespace_for,
)

_REQUEST_ID_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_MAX_TIMER_MILLISECONDS: Final[int] = 2_147_483_647
_LEASE_SECONDS: Final[int] = 60
_MAX_CREDENTIAL_TRANSACTIONS: Final[int] = 1
_MAX_OFFLINE_ARCHIVE_ROWS: Final[int] = 5_000


class ConfigurationPermit:
    """A bounded operation linearized before a later configuration revocation."""

    def __init__(self, release: Callable[[], None], generation: int) -> None:
        self._release_callback: Callable[[], None] | None = release
        self.generation = generation

    @override
    def __repr__(self) -> str:
        return '<ConfigurationPermit>'

    def __enter__(self) -> ConfigurationPermit:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        self.release()

    def release(self) -> None:
        release = self._release_callback
        if release is None:
            return
        self._release_callback = None
        release()


class ConfigurationFence:
    """Secret-free linearizable permits for one revocable configuration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._revoked = True
        self._active: dict[int, int] = {}

    @override
    def __repr__(self) -> str:
        return '<ConfigurationFence>'

    def advance(self) -> int:
        with self._lock:
            self._generation += 1
            self._revoked = False
            return self._generation

    def revoke(self) -> int:
        """Revoke synchronously without waiting for already-linearized work."""
        with self._lock:
            self._generation += 1
            self._revoked = True
            return self._generation

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def authorizes(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._revoked

    def acquire(self, generation: int) -> ConfigurationPermit | None:
        """Linearize one exact-generation operation or reject it after revoke."""
        with self._lock:
            if generation != self._generation or self._revoked:
                return None
            self._active[generation] = self._active.get(generation, 0) + 1
            return ConfigurationPermit(
                lambda: self._release(generation), generation
            )

    def active_permits(self, generation: int | None = None) -> int:
        with self._lock:
            if generation is None:
                return sum(self._active.values())
            return self._active.get(generation, 0)

    def _release(self, generation: int) -> None:
        with self._lock:
            active = self._active.get(generation, 0)
            if active <= 1:
                self._active.pop(generation, None)
            else:
                self._active[generation] = active - 1


class OpenCancellationToken:
    """Secret-free synchronous cancellation fence for one open request chain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False

    @override
    def __repr__(self) -> str:
        return '<OpenCancellationToken>'

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def begin_publication(self) -> bool:
        """Linearize publication before a concurrent cancellation."""
        with self._lock:
            return not self._cancelled


class OpaqueVault[T]:
    """Thread-safe one-shot transfer that never represents stored values."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, T] = {}
        self._latest_id: str | None = None

    @override
    def __repr__(self) -> str:
        return '<OpaqueVault values=<redacted>>'

    def put(self, value: T) -> str:
        request_id = uuid4().hex
        with self._lock:
            self._values[request_id] = value
        return request_id

    def put_latest(self, value: T) -> str:
        """Replace the latest-only value and advance its generation."""
        request_id = uuid4().hex
        with self._lock:
            self._values.clear()
            self._values[request_id] = value
            self._latest_id = request_id
        return request_id

    def take(self, request_id: str) -> T:
        with self._lock:
            return self._values.pop(request_id)

    def take_if_current(self, request_id: str) -> T | None:
        """Take a latest-only value only while its generation is current."""
        with self._lock:
            if request_id != self._latest_id:
                return None
            return self._values.pop(request_id, None)

    def is_current(self, request_id: str) -> bool:
        with self._lock:
            return request_id == self._latest_id

    def run_if_current(self, request_id: str, action: Callable[[], None]) -> bool:
        """Linearize a non-blocking completion against latest replacement."""
        with self._lock:
            if request_id != self._latest_id:
                return False
            action()
            return True

    def acquire_permit_if_current(
        self,
        request_id: str,
        fence: ConfigurationFence,
        generation: int,
    ) -> ConfigurationPermit | None:
        """Acquire an exact-generation permit while the request is latest."""
        with self._lock:
            if request_id != self._latest_id:
                return None
            return fence.acquire(generation)

    def contains(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._values

    def size(self) -> int:
        with self._lock:
            return len(self._values)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._latest_id = None


@dataclass(frozen=True, slots=True)
class PendingConnectionRecovery:
    authenticated_identity: ServerIdentity | None
    credential_present: bool
    failure: PublicFailure


@dataclass(frozen=True, slots=True)
class WorkerConfiguration:
    origin: str
    namespace: Namespace | None
    enabled: bool
    automatic_upload: bool
    client_instance_uuid: UUID
    cache_limit_bytes: int
    generation: int = 0
    validation_id: str = field(default_factory=lambda: uuid4().hex)
    identity: ServerIdentity | None = None
    pending_connection: bool = False
    activation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionTestRequest:
    origin: str
    credential: str = field(repr=False)
    generation: int


@dataclass(frozen=True, slots=True)
class SavedProfileRequest:
    namespace: Namespace
    serialized_profile: bytes = field(repr=False)
    profile: ProfileData = field(repr=False)
    modified_at: datetime
    manual: bool


@dataclass(frozen=True, slots=True)
class RemoveCredentialRequest:
    origin: str


@dataclass(frozen=True, slots=True)
class BrowseRequest:
    namespace: Namespace
    filters: ArchiveFilters
    cursor: str | None
    refresh: bool


@dataclass(frozen=True, slots=True)
class OnlineOpenRequest:
    namespace: Namespace
    roast_uuid: UUID
    cached_fallback: CachedRevision | None = None
    token: OpenCancellationToken = field(default_factory=OpenCancellationToken)


@dataclass(frozen=True, slots=True)
class CachedOpenRequest:
    cached: CachedRevision
    token: OpenCancellationToken = field(default_factory=OpenCancellationToken)


@dataclass(frozen=True, slots=True)
class PublishRequest:
    detail: RoastDetail
    receipt: DownloadReceipt
    staged_path: Path
    token: OpenCancellationToken = field(default_factory=OpenCancellationToken)


@dataclass(frozen=True, slots=True)
class ProtectedPathsRequest:
    namespace: Namespace
    open_paths: frozenset[Path]


@dataclass(frozen=True, slots=True)
class InventoryRefreshRequest:
    namespace: Namespace
    generation: int


@dataclass(frozen=True, slots=True)
class InventoryWorkerEvent:
    generation: int
    namespace: Namespace | None
    value: object
    refresh_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClearUnusedRequest:
    namespace: Namespace


@dataclass(frozen=True, slots=True)
class _PendingStage:
    namespace: Namespace
    request: PublishRequest


@dataclass(slots=True, repr=False)
class _CredentialTransaction:
    origin: str
    candidate: str = field(repr=False)
    old_credential: str | None = field(repr=False)
    identity: ServerIdentity
    generation: int
    keyring_committed: bool
    recovered: bool = False
    activation_emitted: bool = False

    @override
    def __repr__(self) -> str:
        return '<_CredentialTransaction credentials=<redacted>>'


class _DeliveryFailure(RuntimeError):
    def __init__(self, failure: PublicFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class _StaleConfiguration(RuntimeError):
    pass


class _QueueClass(Enum):
    PROFILE = auto()
    INVENTORY = auto()


type TimerFactory = Callable[[QObject], QTimer]
type OperationHook = Callable[[str], None]


class RoastServerWorker(QObject):
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

    def __init__(
        self,
        *,
        outbox: Outbox,
        cache: CacheStore,
        inventory_store: InventoryStore | None = None,
        credentials: CredentialStore,
        client_factory: ClientFactory,
        clock: Callable[[], datetime],
        credential_vault: OpaqueVault[ConnectionTestRequest],
        profile_vault: OpaqueVault[SavedProfileRequest],
        command_vault: OpaqueVault[object],
        configuration_fence: ConfigurationFence | None = None,
        protection_registry: ProtectionRegistry | None = None,
        timer_factory: TimerFactory | None = None,
        operation_hook: OperationHook | None = None,
    ) -> None:
        super().__init__()
        self._outbox = outbox
        self._cache = cache
        self._inventory_store = inventory_store
        self._credentials = credentials
        self._client_factory = client_factory
        self._clock = clock
        self._credential_vault = credential_vault
        self._profile_vault = profile_vault
        self._command_vault = command_vault
        self._configuration_fence = configuration_fence or ConfigurationFence()
        self._protection_registry = protection_registry or ProtectionRegistry()
        self._timer_factory = timer_factory or QTimer
        self._operation_hook = operation_hook
        self._timer: QTimer | None = None
        self._configuration: WorkerConfiguration | None = None
        self._credential: str | None = None
        self._authorized_target: tuple[str, ServerIdentity] | None = None
        self._authorized_transaction_id: str | None = None
        self._credential_transactions: dict[str, _CredentialTransaction] = {}
        self._pending_stages: dict[Path, _PendingStage] = {}
        self._open_cache_paths: frozenset[Path] = frozenset()
        self._stop_event = threading.Event()
        self._started = False
        self._outbox_open = False
        self._inventory_store_open = False
        self._cache_open = False
        self._last_queue_class: _QueueClass | None = None
        self._stopped = False

    @override
    def __repr__(self) -> str:
        return '<RoastServerWorker credential=<redacted>>'

    @pyqtSlot()
    def start(self) -> None:
        if self._reject_wrong_thread('start', FailureKind.INVALID_RESPONSE):
            return
        if self._started or self._stopped or self._interrupted():
            return
        self._started = True
        timer = self._timer_factory(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self.process_queue_once)
        self._timer = timer
        if self._cancelled():
            return
        try:
            self._outbox.open()
            self._outbox_open = True
            if self._cancelled():
                return
            if self._inventory_store is None:
                self._cache.open()
                self._cache_open = True
                if self._cancelled():
                    return
                self._outbox.recover_expired_leases(self._now())
            else:
                self._outbox.recover_expired_leases(self._now())
                if self._cancelled():
                    return
                self._inventory_store.open()
                self._inventory_store_open = True
                if self._cancelled():
                    return
                self._inventory_store.recover_expired_leases(self._now())
                if self._cancelled():
                    return
                self._cache.open()
                self._cache_open = True
            if self._cancelled():
                return
        except CacheError as error:
            self._emit_failure('start', error.failure)
            return
        except InventoryStoreError:
            self._emit_failure('start', _failure(FailureKind.LOCAL_INVENTORY))
            return
        except (OutboxError, OSError, ValueError):
            self._emit_failure('start', _failure(FailureKind.LOCAL_PROFILE))
            return
        if self._configuration is not None:
            self._activate_configuration(None)

    @pyqtSlot(object)
    def configure(self, value: object) -> None:
        if self._reject_wrong_thread('configure', FailureKind.INVALID_RESPONSE):
            return
        if self._cancelled():
            return
        configuration = _valid_configuration(value)
        if configuration is None:
            self._emit_failure('configure', _failure(FailureKind.INVALID_RESPONSE))
            return
        if not self._configuration_fence.is_current(configuration.generation):
            return
        previous = self._configuration
        old_namespace = previous.namespace if previous is not None else None
        if old_namespace != configuration.namespace:
            self._open_cache_paths = frozenset()
        if previous is not None and old_namespace is not None and (
            old_namespace != configuration.namespace
            or not configuration.enabled
            or previous.identity != configuration.identity
        ):
            self._discard_namespace_stages(old_namespace)
        if self._cancelled():
            return
        transaction = (
            self._credential_transactions.get(configuration.activation_id)
            if configuration.activation_id is not None
            else None
        )
        if (
            transaction is not None
            and transaction.origin == configuration.origin
            and transaction.identity == configuration.identity
            and configuration.namespace
            == namespace_for(
                transaction.origin, transaction.identity.organization.id
            )
        ):
            transaction.generation = configuration.generation
        self._configuration = configuration
        self._credential = None
        self._authorized_target = None
        if self._authorized_transaction_id not in self._credential_transactions:
            self._authorized_transaction_id = None
        if not self._started or not self._outbox_open or self._stopped:
            return
        self._activate_configuration(previous)

    def _activate_configuration(
        self, previous: WorkerConfiguration | None
    ) -> None:
        configuration = self._configuration
        if configuration is None:
            return
        old_namespace = previous.namespace if previous is not None else None
        if previous is not None and old_namespace is not None and (
            old_namespace != configuration.namespace
            or not configuration.enabled
            or previous.identity != configuration.identity
        ):
            self._pause_namespace(old_namespace, 'connector_disabled')
        if self._cancelled() or self._reject_stale_configuration(configuration):
            return

        namespace = configuration.namespace
        identity = configuration.identity
        if namespace is None or identity is None:
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_aggregates(namespace)
            return
        if configuration.activation_id is not None:
            self._pause_namespace(namespace, 'connector_disabled')
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_aggregates(namespace)
            return
        self._pause_namespace(namespace, 'credential_removed')
        self._credential = None
        self._authorized_target = None
        try:
            credential = self._credentials.get(configuration.origin)
        except CredentialStoreError:
            if self._cancelled() or self._reject_stale_configuration(configuration):
                return
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_failure('configure', _failure(FailureKind.KEYRING))
            self._emit_aggregates(namespace)
            return
        if self._cancelled() or self._reject_stale_configuration(configuration):
            return
        if credential is None or credential == '':
            self.onlineChanged.emit(False)
            self._stop_timer()
            if configuration.pending_connection:
                transaction_id = uuid4().hex
                failure = _failure(FailureKind.CREDENTIAL_REJECTED)
                self.pendingConnectionRecoveryRequired.emit(
                    transaction_id,
                    PendingConnectionRecovery(None, False, failure),
                )
            self._emit_aggregates(namespace)
            return

        try:
            permit = self._operation_permit(
                configuration, 'authenticate_startup'
            )
            if permit is None:
                self._reject_stale_configuration(configuration)
                return
            with permit, self._client_factory(
                configuration.origin, credential
            ) as client:
                authenticated: object = client.test_connection()
            if self._cancelled():
                return
        except ApiFailure as error:
            if self._cancelled() or self._reject_stale_configuration(configuration):
                return
            self.onlineChanged.emit(False)
            if configuration.pending_connection and not error.failure.retryable:
                self.pendingConnectionRecoveryRequired.emit(
                    uuid4().hex,
                    PendingConnectionRecovery(None, True, error.failure),
                )
            else:
                self._emit_failure('configure', error.failure)
            if error.failure.retryable:
                self._schedule_authentication_retry()
            else:
                self._stop_timer()
            self._emit_aggregates(namespace)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if not self._cancelled() and not self._reject_stale_configuration(
                configuration
            ):
                self.onlineChanged.emit(False)
                self._stop_timer()
                self._emit_failure(
                    'configure', _failure(FailureKind.INVALID_RESPONSE)
                )
                self._emit_aggregates(namespace)
            return
        if self._reject_stale_configuration(configuration):
            return
        if not isinstance(authenticated, ServerIdentity):
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_failure('configure', _failure(FailureKind.INVALID_RESPONSE))
            self._emit_aggregates(namespace)
            return
        if authenticated != identity:
            self.onlineChanged.emit(False)
            self._stop_timer()
            failure = _failure(FailureKind.CREDENTIAL_REJECTED)
            if configuration.pending_connection:
                self.pendingConnectionRecoveryRequired.emit(
                    uuid4().hex,
                    PendingConnectionRecovery(authenticated, True, failure),
                )
            else:
                self._emit_failure('configure', failure)
            self._emit_aggregates(namespace)
            return
        if configuration.pending_connection:
            transaction_id = uuid4().hex
            self._make_credential_transaction_room()
            if self._reject_stale_configuration(configuration):
                return
            self._credential_transactions[transaction_id] = _CredentialTransaction(
                origin=configuration.origin,
                candidate=credential,
                old_credential=None,
                identity=authenticated,
                generation=configuration.generation,
                keyring_committed=True,
                recovered=True,
            )
            self._stop_timer()
            self.credentialCommitted.emit(transaction_id, authenticated)
            self._emit_aggregates(namespace)
            return

        permit = self._operation_permit(configuration, 'install_authorization')
        if permit is None:
            self._reject_stale_configuration(configuration)
            return
        with permit:
            self._credential = credential
            self._authorized_target = (configuration.origin, authenticated)
        self.configurationValidated.emit(configuration)
        self._apply_authorized_configuration(configuration)

    def _apply_authorized_configuration(
        self, configuration: WorkerConfiguration
    ) -> None:
        namespace = configuration.namespace
        if self._reject_stale_configuration(configuration):
            return
        if namespace is None or not self._configuration_is_authorized(configuration):
            self._credential = None
            self._authorized_target = None
            self._stop_timer()
            return
        if not configuration.enabled:
            self._pause_namespace(namespace, 'connector_disabled')
            self._stop_timer()
            self._emit_aggregates(namespace)
            return
        if self._reject_stale_configuration(configuration):
            return
        now = self._now()
        if self._reject_stale_configuration(configuration):
            return
        permit = self._operation_permit(configuration, 'resume_namespace')
        if permit is None:
            self._reject_stale_configuration(configuration)
            return
        try:
            with permit:
                self._outbox.resume_namespace(namespace, now)
                if self._inventory_store_open and self._inventory_store is not None:
                    self._inventory_store.resume_namespace(namespace, now)
        except (OutboxError, ValueError):
            self._credential = None
            self._authorized_target = None
            self._emit_failure('configure', _failure(FailureKind.LOCAL_PROFILE))
            self._stop_timer()
        except InventoryStoreError:
            self._credential = None
            self._authorized_target = None
            self._emit_failure('configure', _failure(FailureKind.LOCAL_INVENTORY))
            self._stop_timer()
        if self._cancelled() or self._reject_stale_configuration(configuration):
            return
        self._emit_aggregates(namespace)
        if self._credential is not None:
            self.onlineChanged.emit(True)
            self._schedule_next(namespace)

    def _schedule_authentication_retry(self) -> None:
        timer = self._timer
        configuration = self._configuration
        if (
            timer is not None
            and not self._interrupted()
            and configuration is not None
        ):
            self._arm_timer(configuration, timer, 5_000)

    @pyqtSlot(str)
    def test_connection(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.INVALID_RESPONSE,
            erase=lambda: self._credential_vault.take(opaque_id),
        ):
            return
        request: ConnectionTestRequest | None = None
        candidate = ''
        old_credential: str | None = None
        transaction: _CredentialTransaction | None = None
        try:
            request = self._credential_vault.take_if_current(opaque_id)
            if request is None or self._cancelled():
                return
            if not _valid_connection_request(request):
                raise ValueError
            configuration = self._configuration
            if (
                configuration is None
                or configuration.generation != request.generation
                or configuration.origin != request.origin
                or not self._credential_vault.is_current(opaque_id)
            ):
                return
            candidate = request.credential
            hook = self._operation_hook
            if hook is not None:
                hook('authenticate_candidate')
            permit = self._credential_vault.acquire_permit_if_current(
                opaque_id,
                self._configuration_fence,
                request.generation,
            )
            if permit is None:
                self._reject_stale_configuration(configuration)
                return
            with permit:
                with self._client_factory(request.origin, candidate) as client:
                    identity = client.test_connection()
                if (
                    self._cancelled()
                    or self._configuration is not configuration
                    or not self._configuration_fence.authorizes(
                        request.generation
                    )
                    or not self._credential_vault.is_current(opaque_id)
                ):
                    return
                if not isinstance(identity, ServerIdentity):
                    raise _DeliveryFailure(
                        _failure(FailureKind.INVALID_RESPONSE)
                    )
                old_credential = self._credentials.get(request.origin)
            if (
                self._cancelled()
                or self._configuration is not configuration
                or not self._configuration_fence.authorizes(request.generation)
                or not self._credential_vault.is_current(opaque_id)
            ):
                return
            self._make_credential_transaction_room()
            transaction = _CredentialTransaction(
                origin=request.origin,
                candidate=candidate,
                old_credential=old_credential,
                identity=identity,
                generation=request.generation,
                keyring_committed=False,
            )
            completed_transaction = transaction
            retained = False

            def retain_and_emit() -> None:
                nonlocal retained
                if (
                    self._configuration is not configuration
                    or not self._configuration_fence.authorizes(
                        completed_transaction.generation
                    )
                ):
                    return
                self._credential_transactions[request_id] = completed_transaction
                retained = True
                self.connectionTested.emit(request_id, identity)

            current = self._credential_vault.run_if_current(
                opaque_id, retain_and_emit
            )
            if not current or not retained:
                completed_transaction.candidate = ''
                completed_transaction.old_credential = None
            transaction = None
        except CredentialStoreError:
            if not self._cancelled() and self._credential_vault.is_current(opaque_id):
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        except ApiFailure as error:
            if not self._cancelled() and self._credential_vault.is_current(opaque_id):
                self.onlineChanged.emit(False)
                self._emit_failure(request_id, error.failure)
            return
        except _DeliveryFailure as error:
            if not self._cancelled() and self._credential_vault.is_current(opaque_id):
                self._emit_failure(request_id, error.failure)
            return
        except (TypeError, ValueError):
            if not self._cancelled() and self._credential_vault.is_current(opaque_id):
                self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if not self._cancelled() and self._credential_vault.is_current(opaque_id):
                self.onlineChanged.emit(False)
                self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        finally:
            request = None
            candidate = ''
            old_credential = None
            if transaction is not None:
                transaction.candidate = ''
                transaction.old_credential = None

    @pyqtSlot(str)
    def commit_connection(self, transaction_id: str) -> None:
        request_id = _public_request_id(transaction_id)
        if self._reject_wrong_thread(request_id, FailureKind.KEYRING):
            return
        transaction = self._credential_transactions.get(transaction_id)
        if self._cancelled() or transaction is None or transaction.keyring_committed:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        if not self._credential_vault.is_current(transaction_id):
            self._rollback_and_signal(transaction_id)
            return
        try:
            transaction.keyring_committed = True
            self._credentials.set(transaction.origin, transaction.candidate)
            if self._interrupted():
                return
            readback = self._credentials.get(transaction.origin)
            if readback != transaction.candidate:
                raise CredentialStoreError
            if self._interrupted():
                return
            if not self._credential_vault.is_current(transaction_id):
                self._rollback_and_signal(transaction_id)
                return
        except CredentialStoreError:
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        if not self._cancelled():
            self.credentialCommitted.emit(request_id, transaction.identity)

    @pyqtSlot(str)
    def finalize_connection(self, transaction_id: str) -> None:
        request_id = _public_request_id(transaction_id)
        if self._reject_wrong_thread(request_id, FailureKind.KEYRING):
            return
        transaction = self._credential_transactions.get(transaction_id)
        configuration = self._configuration
        if (
            self._cancelled()
            or transaction is None
            or not transaction.keyring_committed
            or transaction.activation_emitted
            or configuration is None
            or configuration.activation_id != transaction_id
            or configuration.origin != transaction.origin
            or configuration.identity != transaction.identity
            or configuration.generation != transaction.generation
            or configuration.namespace
            != namespace_for(transaction.origin, transaction.identity.organization.id)
        ):
            if not self._cancelled():
                self._rollback_and_signal(transaction_id)
                self._emit_failure(
                    request_id, _failure(FailureKind.CREDENTIAL_REJECTED)
                )
            return
        if self._reject_stale_configuration(configuration):
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            return
        try:
            permit = self._operation_permit(
                configuration, 'authenticate_activation'
            )
            if permit is None:
                self._reject_stale_configuration(configuration)
                if not self._interrupted():
                    self._rollback_and_signal(transaction_id)
                return
            with permit:
                readback = self._credentials.get(transaction.origin)
                if readback != transaction.candidate:
                    raise CredentialStoreError
                with self._client_factory(
                    transaction.origin, transaction.candidate
                ) as client:
                    authenticated = client.test_connection()
            if authenticated != transaction.identity:
                raise _DeliveryFailure(
                    _failure(FailureKind.CREDENTIAL_REJECTED)
                )
        except CredentialStoreError:
            stale = self._reject_stale_configuration(configuration)
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            if not self._cancelled() and not stale:
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        except ApiFailure as error:
            stale = self._reject_stale_configuration(configuration)
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            if not self._cancelled() and not stale:
                self.onlineChanged.emit(False)
                self._emit_failure(request_id, error.failure)
            return
        except _DeliveryFailure as error:
            stale = self._reject_stale_configuration(configuration)
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            if not self._cancelled() and not stale:
                self._emit_failure(request_id, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            stale = self._reject_stale_configuration(configuration)
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            if not self._cancelled() and not stale:
                self._emit_failure(
                    request_id, _failure(FailureKind.INVALID_RESPONSE)
                )
            return
        if (
            (not transaction.recovered
            and not self._credential_vault.is_current(transaction_id))
            or transaction.generation != configuration.generation
            or self._reject_stale_configuration(configuration)
        ):
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            return
        permit = self._operation_permit(configuration, 'install_activation')
        if permit is None:
            self._reject_stale_configuration(configuration)
            if not self._interrupted():
                self._rollback_and_signal(transaction_id)
            return
        with permit:
            self._credential = transaction.candidate
            self._authorized_target = (transaction.origin, transaction.identity)
            self._authorized_transaction_id = transaction_id
            transaction.activation_emitted = True
        if not self._cancelled():
            self.connectionActivated.emit(request_id, transaction.identity)

    @pyqtSlot(str)
    def acknowledge_connection_activation(self, transaction_id: str) -> None:
        request_id = _public_request_id(transaction_id)
        if self._reject_wrong_thread(request_id, FailureKind.KEYRING):
            return
        transaction = self._credential_transactions.get(transaction_id)
        if (
            self._stopped
            or transaction is None
            or not transaction.activation_emitted
            or transaction_id != self._authorized_transaction_id
        ):
            if not self._stopped:
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        self._credential_transactions.pop(transaction_id, None)
        transaction.candidate = ''
        transaction.old_credential = None
        self._authorized_transaction_id = None

    @pyqtSlot(str)
    def rollback_connection(self, transaction_id: str) -> None:
        request_id = _public_request_id(transaction_id)
        if self._reject_wrong_thread(request_id, FailureKind.KEYRING):
            return
        if transaction_id not in self._credential_transactions:
            self.connectionRollbackFinished.emit(request_id, False)
            return
        self._rollback_and_signal(transaction_id)

    @pyqtSlot(str)
    def cancel_connection_transaction(self, transaction_id: str) -> None:
        request_id = _public_request_id(transaction_id)
        if self._reject_wrong_thread(request_id, FailureKind.KEYRING):
            return
        if request_id != transaction_id:
            self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        if transaction_id in self._credential_transactions:
            self._rollback_and_signal(transaction_id)
            return
        if transaction_id == self._authorized_transaction_id:
            self._authorized_transaction_id = None
            self._credential = None
            self._authorized_target = None
            configuration = self._configuration
            if configuration is not None and configuration.namespace is not None:
                self._pause_namespace(
                    configuration.namespace, 'credential_removed'
                )
            self._stop_timer()
            self.connectionRollbackFinished.emit(request_id, False)
            return
        self.connectionRollbackFinished.emit(request_id, False)

    def _make_credential_transaction_room(self) -> None:
        while len(self._credential_transactions) >= _MAX_CREDENTIAL_TRANSACTIONS:
            oldest = next(iter(self._credential_transactions))
            self._rollback_and_signal(oldest)

    def _rollback_and_signal(self, transaction_id: str) -> bool:
        if self._interrupted():
            return False
        succeeded = self._rollback_transaction(transaction_id)
        self.connectionRollbackFinished.emit(transaction_id, succeeded)
        return succeeded

    def _rollback_transaction(self, transaction_id: str) -> bool:
        transaction = self._credential_transactions.pop(transaction_id, None)
        if transaction is None:
            return False
        succeeded = not (transaction.keyring_committed and transaction.recovered)
        try:
            if transaction.keyring_committed and not transaction.recovered:
                if transaction.old_credential is None:
                    self._credentials.delete(transaction.origin)
                    if self._credentials.get(transaction.origin) is not None:
                        raise CredentialStoreError
                else:
                    self._credentials.set(
                        transaction.origin, transaction.old_credential
                    )
                    if (
                        self._credentials.get(transaction.origin)
                        != transaction.old_credential
                    ):
                        raise CredentialStoreError
        except CredentialStoreError:
            succeeded = False
        transaction.candidate = ''
        transaction.old_credential = None
        self._credential = None
        self._authorized_target = None
        self._authorized_transaction_id = None
        configuration = self._configuration
        if configuration is not None and configuration.namespace is not None:
            self._pause_namespace(configuration.namespace, 'credential_removed')
        self._stop_timer()
        return succeeded

    @pyqtSlot(str)
    def remove_credential(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.KEYRING,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        if self._cancelled():
            return
        if not isinstance(value, RemoveCredentialRequest):
            self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        configuration = self._configuration
        try:
            origin = canonical_origin(value.origin)
        except SettingsError:
            self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        if (
            configuration is None
            or origin != value.origin
            or configuration.origin != origin
        ):
            self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        try:
            self._credentials.delete(origin)
        except CredentialStoreError:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        if self._cancelled():
            return
        namespace = configuration.namespace
        self._credential = None
        self._authorized_target = None
        self._authorized_transaction_id = None
        if namespace is not None:
            self._discard_namespace_stages(namespace)
            self._pause_namespace(namespace, 'credential_removed')
        self._stop_timer()
        self.onlineChanged.emit(False)
        self._emit_aggregates(namespace)
        self.credentialRemoved.emit(request_id)

    @pyqtSlot(str)
    def enqueue_saved(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.LOCAL_PROFILE,
            erase=lambda: self._profile_vault.take(opaque_id),
        ):
            return
        request: SavedProfileRequest | None = None
        serialized_profile = b''
        profile: ProfileData | None = None
        namespace: Namespace | None = None
        snapshot: Snapshot | None = None
        try:
            request = self._profile_vault.take(opaque_id)
            if self._cancelled():
                return
            configuration = self._configuration
            if (
                not _valid_saved_request(request)
                or configuration is None
                or not configuration.enabled
                or request.namespace != configuration.namespace
                or (not request.manual and not configuration.automatic_upload)
                or not self._outbox_open
            ):
                self._emit_failure(request_id, _failure(FailureKind.LOCAL_PROFILE))
                return
            namespace = request.namespace
            serialized_profile = request.serialized_profile
            profile = request.profile
            snapshot = self._outbox.snapshot_bytes(
                namespace, serialized_profile, request.modified_at
            )
            serialized_profile = b''
            request = None
            if self._cancelled():
                return
            roast_uuid = _profile_roast_uuid(profile)
            metadata = project_profile(profile, snapshot.source_modified_at)
            profile = None
            if self._cancelled():
                return
            self._outbox.enqueue(
                namespace,
                snapshot,
                roast_uuid,
                metadata,
                configuration.client_instance_uuid,
            )
            snapshot = None
            if self._cancelled():
                return
            if self._credential is None:
                self._outbox.pause_namespace(
                    namespace, self._now(), 'credential_removed'
                )
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.LOCAL_PROFILE))
        except (OutboxError, OSError, RecursionError, TypeError, ValueError):
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.LOCAL_PROFILE))
        finally:
            request = None
            serialized_profile = b''
            profile = None
            if snapshot is not None:
                try:
                    self._outbox.discard_staged_snapshot(snapshot)
                except OutboxError:
                    if not self._cancelled():
                        self._emit_failure(request_id, _failure(FailureKind.LOCAL_PROFILE))
        if self._cancelled() or namespace is None:
            return
        self._emit_aggregates(namespace)
        if self._credential is not None:
            self._schedule_next(namespace)

    @pyqtSlot()
    def process_queue_once(self) -> None:
        if self._reject_wrong_thread('queue', FailureKind.INVALID_RESPONSE):
            return
        configuration = self._configuration
        namespace = configuration.namespace if configuration is not None else None
        if configuration is not None and self._reject_stale_configuration(configuration):
            return
        if (
            not self._stopped
            and self._outbox_open
            and configuration is not None
            and configuration.identity is not None
            and namespace is not None
            and not self._configuration_is_authorized(configuration)
        ):
            self._credential = None
            self._authorized_target = None
            self._activate_configuration(configuration)
            return
        if (
            self._stopped
            or not self._outbox_open
            or configuration is None
            or not configuration.enabled
            or namespace is None
            or self._credential is None
        ):
            self._stop_timer()
            self._emit_aggregates(namespace)
            return
        if self._interrupted():
            self._stop_timer()
            self._emit_aggregates(namespace)
            return

        now = self._now()
        selected: _QueueClass | None
        try:
            self._outbox.recover_expired_leases(now)
            if self._cancelled():
                return
            inventory_store = self._inventory_store
            if self._inventory_store_open and inventory_store is not None:
                inventory_store.recover_expired_leases(now)
                if self._cancelled():
                    return
            selected = self._select_queue_class(namespace)
        except (OutboxError, OSError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            self._emit_aggregates(namespace)
            self._stop_timer()
            return
        except InventoryStoreError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
            self._emit_aggregates(namespace)
            self._stop_timer()
            return
        if self._cancelled():
            return
        if selected is None:
            self._emit_aggregates(namespace)
            self._schedule_next(namespace)
            return
        permit = self._operation_permit(configuration, 'lease_next')
        if permit is None:
            self._reject_stale_configuration(configuration)
            return
        try:
            with permit:
                if selected is _QueueClass.PROFILE:
                    outcome: Job | InventoryCommand | None = self._outbox.lease_next(
                        namespace, now, _LEASE_SECONDS
                    )
                else:
                    inventory_store = self._inventory_store
                    if inventory_store is None:
                        return
                    outcome = inventory_store.lease_next(
                        namespace, now, _LEASE_SECONDS
                    )
        except (OutboxError, OSError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            self._emit_aggregates(namespace)
            self._stop_timer()
            return
        except InventoryStoreError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
            self._emit_aggregates(namespace)
            self._stop_timer()
            return
        if self._cancelled():
            return
        if outcome is None:
            self._emit_aggregates(namespace)
            self._schedule_next(namespace)
            return
        self._last_queue_class = selected
        if selected is _QueueClass.PROFILE:
            if isinstance(outcome, LeaseFailure):
                self._emit_failure('queue', outcome.failure)
                self._emit_aggregates(namespace)
                self._schedule_next(namespace)
                return
            if not isinstance(outcome, Job):
                self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
                self._schedule_next(namespace)
                return
            self._deliver_job(configuration, outcome)
        else:
            if not isinstance(outcome, InventoryCommand):
                self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
                self._schedule_next(namespace)
                return
            self._deliver_inventory_command(configuration, outcome)
            if (
                not self._cancelled()
                and self._configuration is configuration
                and self._configuration_fence.authorizes(configuration.generation)
            ):
                self._schedule_next(namespace)
            return
        if not self._cancelled():
            self._emit_aggregates(namespace)

    def _deliver_job(self, configuration: WorkerConfiguration, job: Job) -> None:
        token = job.lease_token
        if token is None:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            self._schedule_next(job.namespace)
            return
        failure: PublicFailure | None = None
        retry_after: int | None = None
        status_code: int | None = None
        try:
            self._execute_delivery(configuration, job)
        except _StaleConfiguration:
            self._stop_timer()
            return
        except ApiFailure as error:
            failure = _persistence_failure(error.failure)
            retry_after = error.retry_after_seconds
            status_code = error.status_code
        except _DeliveryFailure as error:
            failure = error.failure
        except OSError:
            failure = _failure(FailureKind.LOCAL_PROFILE)
        except Exception:  # pylint: disable=broad-exception-caught
            failure = _failure(FailureKind.INVALID_RESPONSE)

        if self._interrupted():
            self._stop_timer()
            return

        now = self._now()
        if failure is None:
            if self._commit_complete(job, token, now):
                if self._cancelled():
                    return
                self.onlineChanged.emit(True)
            self._schedule_next(job.namespace)
            return

        if status_code == 401 or failure.kind is FailureKind.CREDENTIAL_REJECTED:
            if self._commit_retry(job, token, now, now, failure):
                if self._cancelled():
                    return
                self._pause_namespace(job.namespace, 'credential_rejected')
                if self._cancelled():
                    return
                self._credential = None
                self._authorized_target = None
                self.onlineChanged.emit(False)
                self._stop_timer()
            else:
                self._schedule_next(job.namespace)
            self._emit_failure('queue', failure)
            return

        if failure.retryable:
            delay = _retry_delay(job.attempts, retry_after)
            next_attempt_at = now + timedelta(seconds=delay)
            self._commit_retry(job, token, now, next_attempt_at, failure)
            if self._cancelled():
                return
            self._schedule_next(job.namespace)
            self.onlineChanged.emit(False)
            self._emit_failure('queue', failure)
            return

        self._commit_failed(job, token, now, failure)
        if self._cancelled():
            return
        self._schedule_next(job.namespace)
        self._emit_failure('queue', failure)

    def _execute_delivery(
        self, configuration: WorkerConfiguration, job: Job
    ) -> None:
        credential = self._credential
        if not self._configuration_fence.authorizes(configuration.generation):
            raise _StaleConfiguration
        if credential is None or not self._configuration_is_authorized(configuration):
            raise _DeliveryFailure(_failure(FailureKind.CREDENTIAL_REJECTED))
        if (
            job.namespace != configuration.namespace
            or job.snapshot_path is None
            or job.snapshot_sha256 is None
            or job.snapshot_byte_count is None
            or job.content_sha256 != job.snapshot_sha256
        ):
            raise _DeliveryFailure(_failure(FailureKind.LOCAL_PROFILE))
        try:
            snapshot = job.snapshot_path.open('rb')
        except OSError:
            raise _DeliveryFailure(_failure(FailureKind.LOCAL_PROFILE)) from None
        with snapshot, self._client_factory(configuration.origin, credential) as client:
            if self._cancelled():
                return
            permit = self._operation_permit(configuration, 'post_aroast')
            if permit is None:
                raise _StaleConfiguration
            with permit:
                client.post_aroast(job.roast_uuid, job.aroast_json.encode('utf-8'))
            if self._cancelled():
                return
            permit = self._operation_permit(configuration, 'upload_revision')
            if permit is None:
                raise _StaleConfiguration
            with permit:
                upload = client.upload_revision(
                    job.roast_uuid,
                    job.content_sha256,
                    job.idempotency_key,
                    job.revision_json.encode('utf-8'),
                    snapshot,
                )
            if self._cancelled():
                return
            if not self._configuration_fence.authorizes(configuration.generation):
                raise _StaleConfiguration
        if not _upload_matches(upload, job):
            raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))

    def _commit_complete(
        self, job: Job, lease_token: str, now: datetime
    ) -> bool:
        try:
            self._outbox.mark_complete(job.id, lease_token, now)
            return True
        except OutboxError as error:
            return self._transition_error(error)
        except ValueError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return False

    def _commit_retry(
        self,
        job: Job,
        lease_token: str,
        now: datetime,
        next_attempt_at: datetime,
        failure: PublicFailure,
    ) -> bool:
        try:
            self._outbox.mark_retry(
                job.id, lease_token, now, next_attempt_at, failure
            )
            return True
        except OutboxError as error:
            return self._transition_error(error)
        except ValueError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return False

    def _commit_failed(
        self,
        job: Job,
        lease_token: str,
        now: datetime,
        failure: PublicFailure,
    ) -> bool:
        try:
            self._outbox.mark_failed(job.id, lease_token, now, failure)
            return True
        except OutboxError as error:
            return self._transition_error(error)
        except ValueError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return False

    def _transition_error(self, error: OutboxError) -> bool:
        if str(error) != 'lease_lost':
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        return False

    def _deliver_inventory_command(
        self,
        configuration: WorkerConfiguration,
        command: InventoryCommand,
    ) -> None:
        store = self._inventory_store
        token = command.lease_token
        if store is None or token is None:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
            return
        failure: PublicFailure | None = None
        retry_after: int | None = None
        status_code: int | None = None
        result: InventoryMutationResult | None = None
        try:
            request = self._inventory_command_request(configuration, command)
            credential = self._credential
            if credential is None or not self._configuration_is_authorized(configuration):
                raise _DeliveryFailure(_failure(FailureKind.CREDENTIAL_REJECTED))
            with self._client_factory(configuration.origin, credential) as client:
                permit = self._operation_permit(
                    configuration, f'inventory_{command.operation}'
                )
                if permit is None:
                    raise _StaleConfiguration
                with permit:
                    response: object = client.execute_inventory_command(request)
            if (
                self._cancelled()
                or not self._configuration_fence.authorizes(configuration.generation)
                or self._configuration is not configuration
            ):
                raise _StaleConfiguration
            if not isinstance(response, InventoryMutationResult):
                raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
            result = response
        except _StaleConfiguration:
            self._stop_timer()
            return
        except ApiFailure as error:
            failure = _inventory_persistence_failure(error.failure)
            retry_after = error.retry_after_seconds
            status_code = error.status_code
        except _DeliveryFailure as error:
            failure = _inventory_persistence_failure(error.failure)
        except (InventoryStoreError, TypeError, ValueError):
            failure = _failure(FailureKind.LOCAL_INVENTORY)
        except Exception:  # pylint: disable=broad-exception-caught
            failure = _failure(FailureKind.INVALID_RESPONSE)

        if (
            self._interrupted()
            or self._configuration is not configuration
            or not self._configuration_fence.authorizes(configuration.generation)
        ):
            self._stop_timer()
            return
        permit = self._operation_permit(configuration, 'inventory_transition')
        if permit is None:
            self._stop_timer()
            return
        with permit:
            if (
                self._interrupted()
                or self._configuration is not configuration
                or not self._configuration_fence.authorizes(configuration.generation)
            ):
                self._stop_timer()
                return
            now = self._now()
            try:
                if failure is None and result is not None:
                    reservation = store.mark_complete(command.id, token, result, now)
                    self.inventoryReservationChanged.emit(
                        InventoryWorkerEvent(
                            configuration.generation, command.namespace, reservation
                        )
                    )
                    self.inventoryLotsChanged.emit(
                        InventoryWorkerEvent(
                            configuration.generation,
                            command.namespace,
                            store.cache_snapshot(command.namespace),
                        )
                    )
                    self.onlineChanged.emit(True)
                    if result.conflict is not None:
                        self._emit_failure(
                            'queue', _failure(FailureKind.INVENTORY_CONFLICT)
                        )
                    self._emit_aggregates(command.namespace)
                    return

                assert failure is not None
                if (
                    status_code in {401, 403}
                    or failure.kind is FailureKind.CREDENTIAL_REJECTED
                ):
                    paused = _failure(FailureKind.CREDENTIAL_REJECTED)
                    store.mark_paused(command.id, token, now, paused)
                    self._pause_namespace(command.namespace, 'credential_rejected')
                    self._credential = None
                    self._authorized_target = None
                    self.onlineChanged.emit(False)
                    self._stop_timer()
                elif failure.kind is FailureKind.INVENTORY_UNSUPPORTED:
                    unsupported = _failure(FailureKind.INVENTORY_UNSUPPORTED)
                    store.mark_paused(command.id, token, now, unsupported)
                    store.pause_namespace(
                        command.namespace, now, 'inventory_unsupported'
                    )
                    self._stop_timer()
                elif failure.retryable:
                    next_attempt_at = now + timedelta(
                        seconds=_retry_delay(command.attempts, retry_after)
                    )
                    store.mark_retry(
                        command.id, token, now, next_attempt_at, failure
                    )
                    self.onlineChanged.emit(False)
                else:
                    store.mark_failed(command.id, token, now, failure)
                self._emit_inventory_reservation(
                    command.namespace, command.roast_uuid
                )
                self._emit_failure('queue', failure)
                self._emit_aggregates(command.namespace)
            except (InventoryStoreError, TypeError, ValueError):
                self._emit_failure(
                    'queue', _failure(FailureKind.LOCAL_INVENTORY)
                )
                self._emit_aggregates(command.namespace)

    def _inventory_command_request(
        self,
        configuration: WorkerConfiguration,
        command: InventoryCommand,
    ) -> InventoryCommandRequest:
        store = self._inventory_store
        if store is None or command.namespace != configuration.namespace:
            raise InventoryStoreError('inventory command namespace is invalid')
        state = store.roast_state(command.namespace, command.roast_uuid)
        if (
            state is None
            or state.lot_id != command.lot_id
            or state.reservation_uuid != command.reservation_uuid
        ):
            raise InventoryStoreError('inventory command identity is invalid')
        occurred_at: datetime | None
        if command.operation == 'reserve':
            occurred_at = state.reserve_occurred_at
        elif command.operation == 'finalize':
            occurred_at = state.finalize_occurred_at
        else:
            occurred_at = state.release_occurred_at
        if occurred_at is None:
            raise InventoryStoreError('inventory command time is invalid')
        client_instance_uuid = _inventory_command_client_uuid(command)
        return InventoryCommandRequest(
            operation=command.operation,
            reservation_uuid=command.reservation_uuid,
            roast_uuid=command.roast_uuid,
            lot_id=command.lot_id,
            request_json=command.request_json,
            idempotency_key=command.idempotency_key,
            occurred_at=occurred_at,
            client_instance_uuid=client_instance_uuid,
            planned_grams=state.planned_grams,
            requested_actual_grams=(
                state.actual_grams if command.operation == 'finalize' else None
            ),
        )

    @pyqtSlot(str)
    def refresh_inventory(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.LOCAL_INVENTORY,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.LOCAL_INVENTORY))
            return
        if self._cancelled():
            return
        configuration = self._configuration
        store = self._inventory_store
        if (
            not _valid_inventory_refresh_request(value)
            or configuration is None
            or store is None
            or not self._inventory_store_open
            or value.namespace != configuration.namespace
            or value.generation != configuration.generation
            or not self._configuration_is_authorized(configuration)
        ):
            self._emit_failure(request_id, _failure(FailureKind.LOCAL_INVENTORY))
            return
        failure: PublicFailure | None = None
        status_code: int | None = None
        lots: list[BeanLot] = []
        try:
            credential = self._credential
            if credential is None:
                raise _DeliveryFailure(_failure(FailureKind.CREDENTIAL_REJECTED))
            cursor: str | None = None
            seen_cursors: set[str] = set()
            seen_lots: set[UUID] = set()
            with self._client_factory(configuration.origin, credential) as client:
                for page_number in range(1, MAX_INVENTORY_PAGES + 1):
                    permit = self._operation_permit(configuration, 'inventory_refresh')
                    if permit is None:
                        raise _StaleConfiguration
                    with permit:
                        page_value: object = client.list_inventory_lots(
                            cursor=cursor, limit=100
                        )
                    if (
                        self._cancelled()
                        or self._configuration is not configuration
                        or not self._configuration_fence.authorizes(
                            configuration.generation
                        )
                    ):
                        raise _StaleConfiguration
                    if not isinstance(page_value, BeanLotPage):
                        raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
                    for lot in page_value.items:
                        if lot.lot_id in seen_lots:
                            raise _DeliveryFailure(
                                _failure(FailureKind.INVALID_RESPONSE)
                            )
                        seen_lots.add(lot.lot_id)
                        lots.append(lot)
                        if len(lots) > MAX_CACHED_LOTS:
                            raise _DeliveryFailure(
                                _failure(FailureKind.INVALID_RESPONSE)
                            )
                    next_cursor = page_value.next_cursor
                    if next_cursor is None:
                        break
                    if next_cursor in seen_cursors or next_cursor == cursor:
                        raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
                    if page_number == MAX_INVENTORY_PAGES:
                        raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
            if (
                self._cancelled()
                or self._configuration is not configuration
                or not self._configuration_fence.authorizes(
                    configuration.generation
                )
            ):
                raise _StaleConfiguration
            permit = self._operation_permit(configuration, 'inventory_refresh_commit')
            if permit is None:
                raise _StaleConfiguration
            with permit:
                if (
                    self._cancelled()
                    or self._configuration is not configuration
                    or not self._configuration_fence.authorizes(
                        configuration.generation
                    )
                ):
                    raise _StaleConfiguration
                store.replace_lots(value.namespace, tuple(lots), self._now())
                snapshot = store.cache_snapshot(value.namespace)
                self.inventoryLotsChanged.emit(
                    InventoryWorkerEvent(
                        value.generation, value.namespace, snapshot, opaque_id
                    )
                )
                self.onlineChanged.emit(True)
                self._emit_aggregates(value.namespace)
            return
        except _StaleConfiguration:
            self._stop_timer()
            return
        except ApiFailure as error:
            failure = _inventory_persistence_failure(error.failure)
            status_code = error.status_code
        except _DeliveryFailure as error:
            failure = _inventory_persistence_failure(error.failure)
        except (InventoryStoreError, TypeError, ValueError):
            failure = _failure(FailureKind.LOCAL_INVENTORY)
        except Exception:  # pylint: disable=broad-exception-caught
            failure = _failure(FailureKind.INVALID_RESPONSE)

        if (
            self._interrupted()
            or self._configuration is not configuration
            or not self._configuration_fence.authorizes(configuration.generation)
        ):
            self._stop_timer()
            return
        permit = self._operation_permit(
            configuration, 'inventory_refresh_transition'
        )
        if permit is None:
            self._stop_timer()
            return
        with permit:
            if (
                self._interrupted()
                or self._configuration is not configuration
                or not self._configuration_fence.authorizes(
                    configuration.generation
                )
            ):
                self._stop_timer()
                return
            if (
                status_code in {401, 403}
                or failure.kind is FailureKind.CREDENTIAL_REJECTED
            ):
                self._pause_namespace(value.namespace, 'credential_rejected')
                self._credential = None
                self._authorized_target = None
                self.onlineChanged.emit(False)
                self._stop_timer()
            elif failure.kind is FailureKind.INVENTORY_UNSUPPORTED:
                try:
                    store.pause_namespace(
                        value.namespace, self._now(), 'inventory_unsupported'
                    )
                except (InventoryStoreError, ValueError):
                    failure = _failure(FailureKind.LOCAL_INVENTORY)
                self._schedule_next(value.namespace)
            elif failure.retryable:
                self.onlineChanged.emit(False)
            self._emit_failure(request_id, failure)
            self._emit_aggregates(value.namespace)

    @pyqtSlot()
    def refresh(self) -> None:
        if self._reject_wrong_thread('queue', FailureKind.INVALID_RESPONSE):
            return
        if not self._cancelled():
            self._emit_aggregates(self._current_namespace())

    @pyqtSlot()
    def wake_inventory(self) -> None:
        if self._reject_wrong_thread('queue', FailureKind.INVALID_RESPONSE):
            return
        if self._cancelled() or not self._inventory_store_open:
            return
        namespace = self._current_namespace()
        if namespace is not None:
            self._emit_aggregates(namespace)
            self._schedule_next(namespace)

    @pyqtSlot(str)
    def retry_inventory_command(self, command_id: str) -> None:
        if self._reject_wrong_thread('queue', FailureKind.LOCAL_INVENTORY):
            return
        if self._cancelled():
            return
        namespace = self._current_namespace()
        store = self._inventory_store
        if (
            namespace is None
            or store is None
            or _REQUEST_ID_RE.fullmatch(command_id) is None
        ):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
            return
        try:
            if not any(
                command.id == command_id
                for command in store.failed_commands(namespace)
            ):
                raise InventoryStoreError('inventory command is not failed')
            failed = next(
                command
                for command in store.failed_commands(namespace)
                if command.id == command_id
            )
            store.retry_same(command_id, self._now())
            self._emit_inventory_reservation(namespace, failed.roast_uuid)
            if self._credential is None:
                store.pause_namespace(namespace, self._now(), 'credential_removed')
        except (InventoryStoreError, StopIteration, TypeError, ValueError):
            if not self._cancelled():
                self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
        if self._cancelled():
            return
        self._emit_aggregates(namespace)
        if self._credential is not None:
            self._schedule_next(namespace)

    @pyqtSlot(str)
    def retry_job(self, job_id: str) -> None:
        if self._reject_wrong_thread('queue', FailureKind.INVALID_RESPONSE):
            return
        if self._cancelled():
            return
        namespace = self._current_namespace()
        if namespace is None or not self._failed_job_is_current(namespace, job_id):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return
        if self._cancelled():
            return
        try:
            self._outbox.retry_now(job_id, self._now())
            if self._credential is None:
                self._outbox.pause_namespace(
                    namespace, self._now(), 'credential_removed'
                )
        except (OutboxError, ValueError):
            if not self._cancelled():
                self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        if self._cancelled():
            return
        self._emit_aggregates(namespace)
        if self._credential is not None:
            self._schedule_next(namespace)

    @pyqtSlot(str)
    def remove_job(self, job_id: str) -> None:
        if self._reject_wrong_thread('queue', FailureKind.INVALID_RESPONSE):
            return
        if self._cancelled():
            return
        namespace = self._current_namespace()
        if namespace is None or not self._failed_job_is_current(namespace, job_id):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return
        if self._cancelled():
            return
        try:
            self._outbox.remove(job_id)
        except (OutboxError, ValueError):
            if not self._cancelled():
                self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        if not self._cancelled():
            self._emit_aggregates(namespace)

    def _failed_job_is_current(self, namespace: Namespace, job_id: str) -> bool:
        try:
            return any(job.id == job_id for job in self._outbox.failed_jobs(namespace))
        except OutboxError:
            return False

    @pyqtSlot(str)
    def browse(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.INVALID_RESPONSE,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            self._browse(opaque_id, request_id)
        finally:
            self.browseFinished.emit(request_id)

    def _browse(self, opaque_id: str, request_id: str) -> None:
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        if self._cancelled():
            return
        if not _valid_browse_request(value) or not self._namespace_is_current(
            value.namespace
        ):
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        request = value
        credential = self._credential
        configuration = self._configuration
        if (
            credential is None
            or configuration is None
            or not configuration.enabled
            or not self._configuration_is_authorized(configuration)
        ):
            self._browse_fallback(request_id, request, _failure(FailureKind.OFFLINE))
            return
        try:
            with self._client_factory(configuration.origin, credential) as client:
                if self._cancelled():
                    return
                permit = self._operation_permit(configuration, 'list_roasts')
                if permit is None:
                    raise _StaleConfiguration
                with permit:
                    page = client.list_roasts(
                        request.filters, cursor=request.cursor, limit=50
                    )
                if self._cancelled():
                    return
            if not isinstance(page, RoastPage):
                raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
        except _StaleConfiguration:
            self._browse_fallback(
                request_id, request, _failure(FailureKind.OFFLINE)
            )
            return
        except ApiFailure as error:
            if self._cancelled():
                return
            self._handle_nonqueue_api_failure(request_id, request.namespace, error)
            self._browse_fallback(request_id, request, error.failure, emit_failure=False)
            return
        except _DeliveryFailure as error:
            if self._cancelled():
                return
            self._browse_fallback(request_id, request, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if self._cancelled():
                return
            self._browse_fallback(
                request_id, request, _failure(FailureKind.INVALID_RESPONSE)
            )
            return
        if self._cancelled():
            return
        self.archivePageReady.emit(request_id, page)
        self.onlineChanged.emit(True)

    def _browse_fallback(
        self,
        request_id: str,
        request: BrowseRequest,
        failure: PublicFailure,
        *,
        emit_failure: bool = True,
    ) -> None:
        if self._cancelled():
            return
        if emit_failure:
            self._emit_failure(request_id, failure)
        self.onlineChanged.emit(False)
        try:
            cached_page = self._cache.list_offline(request.namespace, request.filters)
            page = CachedPage(cached_page.items[:_MAX_OFFLINE_ARCHIVE_ROWS])
        except CacheError as error:
            if not self._cancelled():
                self._emit_failure(request_id, error.failure)
            return
        if not self._cancelled():
            self.archivePageReady.emit(request_id, page)

    @pyqtSlot(str)
    def open_online(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.INVALID_RESPONSE,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        if self._cancelled():
            return
        token_value: object = (
            value.token if isinstance(value, OnlineOpenRequest) else None
        )
        if (
            not isinstance(value, OnlineOpenRequest)
            or not isinstance(token_value, OpenCancellationToken)
            or not self._namespace_is_current(value.namespace)
        ):
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        request = value
        if request.token.is_cancelled():
            return
        configuration = self._configuration
        credential = self._credential
        if (
            configuration is None
            or credential is None
            or not configuration.enabled
            or not self._configuration_is_authorized(configuration)
        ):
            if self._open_cancelled(request.token):
                return
            self._emit_failure(request_id, _failure(FailureKind.OFFLINE))
            self.onlineChanged.emit(False)
            self._offer_request_fallback(request_id, request)
            return

        staged_path: Path | None = None
        detail: RoastDetail | None = None
        try:
            with self._client_factory(configuration.origin, credential) as client:
                if self._open_cancelled(request.token):
                    return
                permit = self._operation_permit(configuration, 'get_roast')
                if permit is None:
                    raise _StaleConfiguration
                with permit:
                    detail_value: object = client.get_roast(request.roast_uuid)
                if self._open_cancelled(request.token):
                    return
                if (
                    not isinstance(detail_value, RoastDetail)
                    or detail_value.roast_uuid != request.roast_uuid
                    or detail_value.current_revision is None
                ):
                    raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
                detail = detail_value
                staged_path, output = self._cache.new_staging_file(request.namespace)
                if self._open_cancelled(request.token):
                    self._discard_stage(staged_path)
                    return
                permit = self._operation_permit(configuration, 'download_revision')
                if permit is None:
                    raise _StaleConfiguration
                with permit:
                    receipt = client.download_revision(detail, output)
                if self._open_cancelled(request.token):
                    self._discard_stage(staged_path)
                    return
            publish_request = PublishRequest(detail, receipt, staged_path, request.token)
            if not _valid_publish_request(publish_request):
                raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
            if self._open_cancelled(request.token):
                self._discard_stage(staged_path)
                return
            self._pending_stages[staged_path] = _PendingStage(
                request.namespace, publish_request
            )
        except _StaleConfiguration:
            if staged_path is not None:
                self._discard_stage(staged_path)
            if not self._open_cancelled(request.token):
                self._emit_failure(request_id, _failure(FailureKind.OFFLINE))
                self.onlineChanged.emit(False)
            return
        except ApiFailure as error:
            if staged_path is not None:
                self._discard_stage(staged_path)
            if not self._open_cancelled(request.token):
                self._handle_nonqueue_api_failure(request_id, request.namespace, error)
                if error.failure.retryable:
                    if detail is None:
                        self._offer_request_fallback(request_id, request)
                    else:
                        self._offer_cached_fallback(
                            request_id, request.namespace, detail, request.token
                        )
            return
        except CacheError as error:
            if staged_path is not None:
                self._discard_stage(staged_path)
            if not self._open_cancelled(request.token):
                self._emit_failure(request_id, error.failure)
            return
        except _DeliveryFailure as error:
            if staged_path is not None:
                self._discard_stage(staged_path)
            if not self._open_cancelled(request.token):
                self._emit_failure(request_id, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if staged_path is not None:
                self._discard_stage(staged_path)
            if not self._open_cancelled(request.token):
                self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        if self._open_cancelled(request.token):
            self._discard_stage(staged_path)
            return
        self.downloadStaged.emit(request_id, publish_request)
        self.onlineChanged.emit(True)

    def _offer_request_fallback(
        self,
        request_id: str,
        request: OnlineOpenRequest,
    ) -> None:
        expected = request.cached_fallback
        if expected is None or self._open_cancelled(request.token):
            return
        try:
            cached = self._cache.validate(expected)
        except CacheError:
            return
        if (
            cached.namespace == request.namespace
            and cached.roast.roast_uuid == request.roast_uuid
            and cached.revision.revision_number == cached.roast.revision_count
            and not self._cancelled()
            and not request.token.is_cancelled()
        ):
            self.cachedFallbackReady.emit(request_id, cached)

    def _offer_cached_fallback(
        self,
        request_id: str,
        namespace: Namespace,
        detail: RoastDetail,
        token: OpenCancellationToken,
    ) -> None:
        revision = detail.current_revision
        if revision is None or self._open_cancelled(token):
            return
        try:
            cached = self._cache.find_current(
                namespace,
                detail.roast_uuid,
                revision.revision_number,
                revision.sha256,
            )
        except CacheError:
            return
        if cached is not None and not self._open_cancelled(token):
            self.cachedFallbackReady.emit(request_id, cached)

    def _open_cancelled(self, token: OpenCancellationToken) -> bool:
        return self._cancelled() or token.is_cancelled()

    @pyqtSlot(str)
    def open_cached(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.CACHE_CORRUPT,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._cancelled():
            return
        token_value: object = (
            value.token if isinstance(value, CachedOpenRequest) else None
        )
        if (
            not isinstance(value, CachedOpenRequest)
            or not isinstance(token_value, OpenCancellationToken)
            or not self._namespace_is_current(value.cached.namespace)
        ):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        if value.token.is_cancelled():
            return
        try:
            cached = self._cache.validate(value.cached)
        except CacheError as error:
            if not self._cancelled():
                self._emit_failure(request_id, error.failure)
            return
        if not self._cancelled() and not value.token.is_cancelled():
            self.cachedReady.emit(request_id, cached)

    @pyqtSlot(str)
    def publish_staged(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.CACHE_CORRUPT,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._cancelled():
            if isinstance(value, PublishRequest):
                pending = self._pending_stages.get(value.staged_path)
                if pending is not None and pending.request == value:
                    self._discard_stage(value.staged_path)
            return
        if not isinstance(value, PublishRequest):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        request = value
        pending = self._pending_stages.get(request.staged_path)
        if pending is None or pending.request != request or not _valid_publish_request(request):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._interrupted():
            self._discard_stage(request.staged_path)
            self._stop_timer()
            return
        if not self._namespace_is_current(pending.namespace):
            self._discard_stage(request.staged_path)
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        hook = self._operation_hook
        if hook is not None:
            hook('publish_staged')
        if not request.token.begin_publication():
            self._discard_stage(request.staged_path)
            return
        self._pending_stages.pop(request.staged_path, None)
        try:
            cached = self._cache.publish(
                pending.namespace,
                request.detail,
                request.receipt,
                request.staged_path,
                self._now(),
            )
        except CacheError as error:
            if not self._cancelled():
                self._emit_failure(request_id, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._cancelled():
            return
        self.cachePublished.emit(request_id, cached)
        if not request.token.is_cancelled():
            self._prune_to_limit(request_id, pending.namespace)

    @pyqtSlot(str)
    def discard_staged(self, staged_path_text: str) -> None:
        if self._reject_wrong_thread('discard', FailureKind.CACHE_CORRUPT):
            return
        if self._cancelled():
            return
        matching = next(
            (
                path
                for path in self._pending_stages
                if str(path) == staged_path_text
            ),
            None,
        )
        if matching is None:
            self._emit_failure('discard', CACHE_FAILURE)
            return
        self._discard_stage(matching)
        self._emit_cache_stats(matching_operation='discard')

    @pyqtSlot(str)
    def update_protected_paths(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.CACHE_CORRUPT,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._cancelled() or not _valid_protected_paths_request(value):
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        configuration = self._configuration
        if configuration is None or configuration.namespace != value.namespace:
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        self._open_cache_paths = value.open_paths

    @pyqtSlot(str)
    def clear_unused(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        if self._reject_wrong_thread(
            request_id,
            FailureKind.CACHE_CORRUPT,
            erase=lambda: self._command_vault.take(opaque_id),
        ):
            return
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._cancelled():
            return
        if not _valid_clear_request(value):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        request = value
        if not self._namespace_is_current(request.namespace):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        try:
            outbox_paths = self._outbox.protected_paths(request.namespace)
            with self._protection_registry.read_guard(
                request.namespace
            ) as registry_paths:
                protected = (
                    self._open_cache_paths | registry_paths | outbox_paths)
                if self._cancelled():
                    return
                stats = self._cache.clear_unused(request.namespace, protected)
        except (CacheError, OutboxError, OSError, ValueError):
            if not self._cancelled():
                self._emit_failure(request_id, CACHE_FAILURE)
            return
        if not self._cancelled():
            self.cacheStatsChanged.emit(stats)

    def _prune_to_limit(self, operation: str, namespace: Namespace) -> None:
        if self._cancelled():
            return
        configuration = self._configuration
        if configuration is None or configuration.namespace != namespace:
            return
        try:
            outbox_paths = self._outbox.protected_paths(namespace)
            with self._protection_registry.read_guard(namespace) as registry_paths:
                protected = (
                    self._open_cache_paths | registry_paths | outbox_paths)
                if self._cancelled():
                    return
                stats = self._cache.prune(
                    namespace, configuration.cache_limit_bytes, protected
                )
        except (CacheError, OutboxError, OSError, ValueError):
            if not self._cancelled():
                self._emit_failure(operation, CACHE_FAILURE)
            return
        if not self._cancelled():
            self.cacheStatsChanged.emit(stats)

    def _discard_stage(self, path: Path) -> bool:
        try:
            self._cache.discard_staging(path)
        except CacheError:
            self._emit_failure('discard', CACHE_FAILURE)
            return False
        self._pending_stages.pop(path, None)
        return True

    def _discard_all_stages(self) -> None:
        for path in tuple(self._pending_stages):
            self._discard_stage(path)

    def _discard_namespace_stages(self, namespace: Namespace) -> None:
        for path, pending in tuple(self._pending_stages.items()):
            if pending.namespace != namespace:
                continue
            self._pending_stages.pop(path, None)
            try:
                self._cache.discard_staging(path)
            except CacheError:
                self._emit_failure('discard', CACHE_FAILURE)

    def _handle_nonqueue_api_failure(
        self, operation: str, namespace: Namespace, error: ApiFailure
    ) -> None:
        if self._cancelled():
            return
        if error.status_code == 401 or error.failure.kind is FailureKind.CREDENTIAL_REJECTED:
            self._pause_namespace(namespace, 'credential_rejected')
            self._credential = None
            self._authorized_target = None
            self._stop_timer()
        self.onlineChanged.emit(False)
        self._emit_failure(operation, error.failure)

    def _pause_namespace(self, namespace: Namespace, code: str) -> None:
        if self._cancelled() or not self._outbox_open:
            return
        now = self._now()
        try:
            self._outbox.pause_namespace(namespace, now, code)
        except (OutboxError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        inventory_store = self._inventory_store
        if not self._inventory_store_open or inventory_store is None:
            return
        try:
            inventory_store.pause_namespace(namespace, now, code)
        except (InventoryStoreError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))

    def _emit_aggregates(self, namespace: Namespace | None) -> None:
        if self._cancelled() or namespace is None or not self._outbox_open:
            return
        try:
            counts = self._outbox.counts(namespace)
        except OutboxError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        else:
            if self._cancelled():
                return
            self.queueChanged.emit(counts)
        if self._cancelled():
            return
        try:
            failed = self._outbox.failed_jobs(namespace)
        except OutboxError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        else:
            if self._cancelled():
                return
            self.failedJobsChanged.emit(failed)
        if self._cancelled():
            return
        inventory_store = self._inventory_store
        if not self._inventory_store_open or inventory_store is None:
            self._emit_cache_stats(namespace=namespace, matching_operation='cache')
            return
        try:
            inventory_counts = inventory_store.counts(namespace)
            inventory_failed = inventory_store.failed_commands(namespace)
            inventory_recovery = inventory_store.interrupted_reservations()
            inventory_lots = inventory_store.cache_snapshot(namespace)
        except InventoryStoreError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
        else:
            if self._cancelled():
                return
            configuration = self._configuration
            if configuration is None:
                return
            self.inventoryQueueChanged.emit(
                InventoryWorkerEvent(
                    configuration.generation, namespace, inventory_counts
                )
            )
            self.inventoryFailedChanged.emit(
                InventoryWorkerEvent(
                    configuration.generation, namespace, inventory_failed
                )
            )
            self.inventoryRecoveryChanged.emit(
                InventoryWorkerEvent(
                    configuration.generation, namespace, inventory_recovery
                )
            )
            self.inventoryLotsChanged.emit(
                InventoryWorkerEvent(
                    configuration.generation, namespace, inventory_lots
                )
            )
        if not self._cancelled():
            self._emit_cache_stats(namespace=namespace, matching_operation='cache')

    def _emit_inventory_reservation(
        self, namespace: Namespace, roast_uuid: UUID
    ) -> None:
        inventory_store = self._inventory_store
        if self._cancelled() or inventory_store is None:
            return
        try:
            reservation = inventory_store.roast_state(namespace, roast_uuid)
        except (InventoryStoreError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
            return
        configuration = self._configuration
        if (
            reservation is not None
            and not self._cancelled()
            and configuration is not None
        ):
            self.inventoryReservationChanged.emit(
                InventoryWorkerEvent(
                    configuration.generation, namespace, reservation
                )
            )

    def _emit_cache_stats(
        self,
        *,
        namespace: Namespace | None = None,
        matching_operation: str,
    ) -> None:
        selected = namespace or self._current_namespace()
        if self._cancelled() or selected is None or not self._cache_open:
            return
        try:
            stats = self._cache.stats(selected)
        except CacheError as error:
            if not self._cancelled():
                self._emit_failure(matching_operation, error.failure)
        else:
            if not self._cancelled():
                self.cacheStatsChanged.emit(stats)

    @pyqtSlot()
    def stop(self) -> None:
        if self._reject_wrong_thread('stop', FailureKind.INVALID_RESPONSE):
            return
        if self._stopped:
            return
        self._stop_event.set()
        self._stop_timer()
        self._discard_all_stages()
        self._credential_vault.clear()
        self._profile_vault.clear()
        self._command_vault.clear()
        for transaction in self._credential_transactions.values():
            transaction.candidate = ''
            transaction.old_credential = None
        self._credential_transactions.clear()
        self._credential = None
        self._authorized_target = None
        self._authorized_transaction_id = None
        try:
            self._cache.close()
        except CacheError as error:
            self._emit_failure('stop', error.failure)
        finally:
            self._cache_open = False
            self._pending_stages.clear()
        if self._inventory_store_open and self._inventory_store is not None:
            try:
                self._inventory_store.close()
            except InventoryStoreError:
                self._emit_failure('stop', _failure(FailureKind.LOCAL_INVENTORY))
            finally:
                self._inventory_store_open = False
        if self._outbox_open:
            try:
                self._outbox.close()
            except OutboxError:
                self._emit_failure('stop', _failure(FailureKind.LOCAL_PROFILE))
            finally:
                self._outbox_open = False
        self._stopped = True
        self.stopped.emit()

    def _namespace_is_current(self, namespace: Namespace) -> bool:
        configuration = self._configuration
        return (
            configuration is not None
            and configuration.enabled
            and configuration.namespace == namespace
            and self._configuration_fence.authorizes(configuration.generation)
        )

    def _current_namespace(self) -> Namespace | None:
        configuration = self._configuration
        return configuration.namespace if configuration is not None else None

    def _reject_stale_configuration(
        self, configuration: WorkerConfiguration
    ) -> bool:
        """Return one fixed stale result after revoking all delivery authority."""
        if self._configuration_fence.authorizes(configuration.generation):
            return False
        if self._configuration is configuration:
            namespace = configuration.namespace
            self._credential = None
            self._authorized_target = None
            self._stop_timer()
            if namespace is not None:
                self._pause_namespace(namespace, 'connector_disabled')
                self._emit_aggregates(namespace)
            self.onlineChanged.emit(False)
        return True

    def _configuration_is_authorized(
        self, configuration: WorkerConfiguration
    ) -> bool:
        identity = configuration.identity
        namespace = configuration.namespace
        return (
            self._configuration_fence.authorizes(configuration.generation)
            and self._credential is not None
            and identity is not None
            and namespace is not None
            and not configuration.pending_connection
            and configuration.activation_id is None
            and namespace
            == namespace_for(configuration.origin, identity.organization.id)
            and self._authorized_target == (configuration.origin, identity)
        )

    def _interrupted(self) -> bool:
        thread = self.thread()
        return self._stop_event.is_set() or (
            isinstance(thread, QThread) and thread.isInterruptionRequested()
        )

    def _cancelled(self) -> bool:
        cancelled = self._stopped or self._interrupted()
        if cancelled:
            self._stop_timer()
        return cancelled

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('worker clock must return an aware datetime')
        return now.astimezone(UTC)

    def _select_queue_class(self, namespace: Namespace) -> _QueueClass | None:
        profile_due = self._outbox.next_due_at(namespace)
        inventory_due: datetime | None = None
        inventory_store = self._inventory_store
        if self._inventory_store_open and inventory_store is not None:
            inventory_due = inventory_store.next_due_at(namespace)
        if profile_due is None:
            return None if inventory_due is None else _QueueClass.INVENTORY
        if inventory_due is None or profile_due < inventory_due:
            return _QueueClass.PROFILE
        if inventory_due < profile_due:
            return _QueueClass.INVENTORY
        if self._last_queue_class is _QueueClass.PROFILE:
            return _QueueClass.INVENTORY
        return _QueueClass.PROFILE

    def _next_due_at(self, namespace: Namespace) -> datetime | None:
        profile_due = self._outbox.next_due_at(namespace)
        inventory_due: datetime | None = None
        inventory_store = self._inventory_store
        if self._inventory_store_open and inventory_store is not None:
            inventory_due = inventory_store.next_due_at(namespace)
        if profile_due is None:
            return inventory_due
        if inventory_due is None:
            return profile_due
        return min(profile_due, inventory_due)

    def _schedule_next(self, namespace: Namespace) -> None:
        configuration = self._configuration
        if (
            self._interrupted()
            or configuration is None
            or configuration.namespace != namespace
            or not self._configuration_is_authorized(configuration)
        ):
            self._stop_timer()
            return
        try:
            due = self._next_due_at(namespace)
        except (OutboxError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            self._stop_timer()
            return
        except InventoryStoreError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_INVENTORY))
            self._stop_timer()
            return
        if self._cancelled():
            return
        if due is None:
            self._stop_timer()
            return
        self._schedule_at(due)

    def _schedule_at(self, due: datetime) -> None:
        timer = self._timer
        configuration = self._configuration
        if (
            timer is None
            or self._interrupted()
            or configuration is None
            or not self._configuration_is_authorized(configuration)
        ):
            self._stop_timer()
            return
        seconds = max(0.0, (due.astimezone(UTC) - self._now()).total_seconds())
        milliseconds = min(_MAX_TIMER_MILLISECONDS, math.ceil(seconds * 1_000))
        self._arm_timer(configuration, timer, milliseconds)

    def _arm_timer(
        self,
        configuration: WorkerConfiguration,
        timer: QTimer,
        milliseconds: int,
    ) -> None:
        permit = self._operation_permit(configuration, 'timer_start')
        if permit is None:
            self._stop_timer()
            return
        with permit:
            timer.start(milliseconds)

    def _operation_permit(
        self,
        configuration: WorkerConfiguration,
        operation: str,
    ) -> ConfigurationPermit | None:
        hook = self._operation_hook
        if hook is not None:
            hook(operation)
        return self._configuration_fence.acquire(configuration.generation)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _reject_wrong_thread(
        self,
        operation: str,
        kind: FailureKind,
        *,
        erase: Callable[[], object] | None = None,
    ) -> bool:
        if QThread.currentThread() is self.thread():
            return False
        if erase is not None:
            try:
                erase()
            except KeyError:
                pass
        self._emit_failure(operation, _failure(kind))
        return True

    def _emit_failure(self, operation: str, failure: PublicFailure) -> None:
        safe_operation = (
            operation
            if operation in {'start', 'configure', 'queue', 'cache', 'discard', 'stop'}
            or _REQUEST_ID_RE.fullmatch(operation) is not None
            else 'request'
        )
        self.operationFailed.emit(safe_operation, failure)


def _failure(kind: FailureKind) -> PublicFailure:
    retryable = kind in {
        FailureKind.OFFLINE,
        FailureKind.RATE_LIMITED,
    }
    return PublicFailure(
        kind=kind,
        code=kind.value,
        message=FAILURE_MESSAGES[kind],
        retryable=retryable,
    )


def _persistence_failure(failure: object) -> PublicFailure:
    if not isinstance(failure, PublicFailure) or not isinstance(failure.kind, FailureKind):
        return _failure(FailureKind.INVALID_RESPONSE)
    return _failure(failure.kind)


_INVENTORY_FIXED_FAILURES: Final[dict[str, tuple[FailureKind, str, bool]]] = {
    'bean_lot_not_found': (
        FailureKind.INVENTORY_REJECTED, 'Bean lot not found', False
    ),
    'bean_lot_archived': (
        FailureKind.INVENTORY_REJECTED, 'Bean lot archived', False
    ),
    'invalid_inventory_transition': (
        FailureKind.INVENTORY_REJECTED, 'Invalid inventory transition', False
    ),
    'inventory_idempotency_conflict': (
        FailureKind.INVENTORY_CONFLICT,
        'Idempotency key conflicts with an earlier request',
        False,
    ),
    'inventory_reservation_not_found': (
        FailureKind.INVENTORY_REJECTED, 'Inventory reservation not found', False
    ),
    'inventory_unavailable': (
        FailureKind.OFFLINE, 'Inventory unavailable', True
    ),
    'invalid_request': (
        FailureKind.INVENTORY_REJECTED, 'Invalid request', False
    ),
}


def _inventory_command_client_uuid(command: InventoryCommand) -> UUID:
    parts = command.idempotency_key.split(':')
    if len(parts) != 4:
        raise InventoryStoreError('inventory command idempotency key is invalid')
    prefix, client_hex, reservation_hex, operation = parts
    try:
        client_instance_uuid = UUID(hex=client_hex)
        reservation_uuid = UUID(hex=reservation_hex)
    except (AttributeError, TypeError, ValueError) as error:
        raise InventoryStoreError(
            'inventory command idempotency key is invalid'
        ) from error
    expected = (
        f'inventory-v1:{client_instance_uuid.hex}:'
        f'{reservation_uuid.hex}:{command.operation}'
    )
    if (
        prefix != 'inventory-v1'
        or client_hex != client_instance_uuid.hex
        or reservation_hex != reservation_uuid.hex
        or reservation_uuid != command.reservation_uuid
        or operation not in {'reserve', 'finalize', 'release'}
        or operation != command.operation
        or command.idempotency_key != expected
    ):
        raise InventoryStoreError('inventory command idempotency key is invalid')
    return client_instance_uuid


def _inventory_persistence_failure(failure: object) -> PublicFailure:
    if not isinstance(failure, PublicFailure) or not isinstance(failure.kind, FailureKind):
        return _failure(FailureKind.INVALID_RESPONSE)
    fixed = _INVENTORY_FIXED_FAILURES.get(failure.code)
    if fixed is not None and fixed == (
        failure.kind,
        failure.message,
        failure.retryable,
    ):
        return failure
    if failure.kind is FailureKind.OFFLINE:
        code = failure.code
        if code == 'timeout':
            code = 'deadline_exceeded'
        elif code == 'client_closed':
            code = 'request_error'
        if code in {
            'connection_error',
            'deadline_exceeded',
            'offline',
            'request_error',
            'server_unavailable',
            'tls_error',
        }:
            return PublicFailure(
                FailureKind.OFFLINE,
                code,
                FAILURE_MESSAGES[FailureKind.OFFLINE],
                True,
            )
    if failure.kind in {
        FailureKind.CREDENTIAL_REJECTED,
        FailureKind.RATE_LIMITED,
        FailureKind.INVALID_RESPONSE,
        FailureKind.INVENTORY_UNSUPPORTED,
        FailureKind.LOCAL_INVENTORY,
    }:
        return _failure(failure.kind)
    return _failure(FailureKind.INVALID_RESPONSE)


def _public_request_id(value: object) -> str:
    if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None:
        return value
    return 'request'


def _valid_configuration(value: object) -> WorkerConfiguration | None:
    if not isinstance(value, WorkerConfiguration):
        return None
    enabled: object = value.enabled
    automatic_upload: object = value.automatic_upload
    client_instance_uuid: object = value.client_instance_uuid
    cache_limit_bytes: object = value.cache_limit_bytes
    generation: object = value.generation
    if (
        type(enabled) is not bool
        or type(automatic_upload) is not bool
        or not isinstance(client_instance_uuid, UUID)
        or type(cache_limit_bytes) is not int
        or type(generation) is not int
        or generation <= 0
        or not MIN_CACHE_LIMIT_BYTES <= cache_limit_bytes <= MAX_CACHE_LIMIT_BYTES
    ):
        return None
    origin_value: object = value.origin
    if not isinstance(origin_value, str):
        return None
    try:
        origin = canonical_origin(origin_value)
    except SettingsError:
        return None
    if origin != origin_value:
        return None
    namespace_value: object = value.namespace
    identity_value: object = value.identity
    pending_connection: object = value.pending_connection
    activation_id: object = value.activation_id
    validation_id: object = value.validation_id
    if (
        type(pending_connection) is not bool
        or not isinstance(validation_id, str)
        or _REQUEST_ID_RE.fullmatch(validation_id) is None
    ):
        return None
    if activation_id is not None and (
        not isinstance(activation_id, str)
        or _REQUEST_ID_RE.fullmatch(activation_id) is None
    ):
        return None
    if namespace_value is None or identity_value is None:
        if namespace_value is not None or identity_value is not None:
            return None
        if pending_connection or activation_id is not None:
            return None
        return value
    if not isinstance(namespace_value, Namespace) or not isinstance(
        identity_value, ServerIdentity
    ):
        return None
    expected = namespace_for(origin, identity_value.organization.id)
    if namespace_value != expected:
        return None
    return value


def _valid_inventory_refresh_request(value: object) -> TypeGuard[InventoryRefreshRequest]:
    if not isinstance(value, InventoryRefreshRequest):
        return False
    namespace: object = value.namespace
    generation: object = value.generation
    return (
        isinstance(namespace, Namespace)
        and type(generation) is int
        and generation > 0
    )


def _valid_connection_request(value: object) -> bool:
    if not isinstance(value, ConnectionTestRequest):
        return False
    credential: object = value.credential
    generation: object = value.generation
    if (
        not isinstance(credential, str)
        or credential == ''
        or type(generation) is not int
        or generation <= 0
    ):
        return False
    origin: object = value.origin
    if not isinstance(origin, str):
        return False
    try:
        return canonical_origin(origin) == origin
    except SettingsError:
        return False


def _valid_saved_request(value: object) -> TypeGuard[SavedProfileRequest]:
    if not isinstance(value, SavedProfileRequest):
        return False
    namespace: object = value.namespace
    serialized_profile: object = value.serialized_profile
    profile: object = value.profile
    modified_at: object = value.modified_at
    manual: object = value.manual
    return (
        isinstance(namespace, Namespace)
        and isinstance(serialized_profile, bytes)
        and 1 <= len(serialized_profile) <= MAX_PROFILE_BYTES
        and isinstance(profile, dict)
        and isinstance(modified_at, datetime)
        and modified_at.tzinfo is not None
        and modified_at.utcoffset() is not None
        and type(manual) is bool
    )


def _valid_browse_request(value: object) -> TypeGuard[BrowseRequest]:
    if not isinstance(value, BrowseRequest):
        return False
    namespace: object = value.namespace
    cursor: object = value.cursor
    refresh: object = value.refresh
    if (
        not isinstance(namespace, Namespace)
        or type(refresh) is not bool
        or (
            cursor is not None
            and (
                not isinstance(cursor, str)
                or cursor == ''
                or len(cursor) > MAX_CURSOR_CHARS
            )
        )
    ):
        return False
    try:
        validate_archive_filters(value.filters)
    except ValueError:
        return False
    return True


def _valid_publish_request(value: object) -> TypeGuard[PublishRequest]:
    if not isinstance(value, PublishRequest):
        return False
    detail: object = value.detail
    receipt: object = value.receipt
    staged_path: object = value.staged_path
    if (
        not isinstance(detail, RoastDetail)
        or not isinstance(receipt, DownloadReceipt)
        or not isinstance(staged_path, Path)
        or not isinstance(value.token, OpenCancellationToken)
    ):
        return False
    revision = detail.current_revision
    return (
        revision is not None
        and receipt.roast_uuid == detail.roast_uuid
        and receipt.revision_number == revision.revision_number
        and receipt.sha256 == revision.sha256
        and receipt.byte_count == revision.byte_size
        and receipt.filename
        == f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog'
    )


def _valid_protected_paths_request(
    value: object,
) -> TypeGuard[ProtectedPathsRequest]:
    if not isinstance(value, ProtectedPathsRequest):
        return False
    namespace: object = value.namespace
    open_paths: object = value.open_paths
    return (
        isinstance(namespace, Namespace)
        and isinstance(open_paths, frozenset)
        and all(isinstance(path, Path) for path in open_paths)
    )


def _valid_clear_request(value: object) -> TypeGuard[ClearUnusedRequest]:
    return isinstance(value, ClearUnusedRequest) and isinstance(
        value.namespace, Namespace
    )


def _profile_roast_uuid(profile: ProfileData) -> UUID:
    value = profile.get('roastUUID')
    if not isinstance(value, str):
        raise ValueError('saved profile roast UUID is unavailable')
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError('saved profile roast UUID is invalid') from None


def _upload_matches(upload: object, job: Job) -> bool:
    return (
        isinstance(upload, RevisionUpload)
        and upload.roast_uuid == job.roast_uuid
        and upload.revision.sha256 == job.content_sha256
        and upload.revision.byte_size == job.snapshot_byte_count
    )


def _retry_delay(attempts: int, retry_after: int | None) -> int:
    backoff = 300 if attempts >= 7 else 5 * 2 ** max(0, attempts - 1)
    bounded_retry_after = retry_after if retry_after is not None and retry_after >= 0 else 0
    return max(backoff, bounded_retry_after)


__all__ = [
    'BrowseRequest',
    'CachedOpenRequest',
    'ClearUnusedRequest',
    'ConfigurationFence',
    'ConfigurationPermit',
    'ConnectionTestRequest',
    'InventoryRefreshRequest',
    'OnlineOpenRequest',
    'OpenCancellationToken',
    'OpaqueVault',
    'PendingConnectionRecovery',
    'ProtectedPathsRequest',
    'PublishRequest',
    'RemoveCredentialRequest',
    'RoastServerWorker',
    'SavedProfileRequest',
    'WorkerConfiguration',
]
