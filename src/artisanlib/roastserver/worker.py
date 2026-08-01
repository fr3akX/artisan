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

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import math
from pathlib import Path
import re
import threading
from typing import Final, TypeGuard, cast, override
from uuid import UUID, uuid4

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.api import ApiFailure, ClientFactory, DownloadReceipt
from artisanlib.roastserver.cache import (
    CACHE_FAILURE,
    CacheError,
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
    RevisionUpload,
    RoastDetail,
    RoastPage,
    ServerIdentity,
)
from artisanlib.roastserver.metadata import project_profile
from artisanlib.roastserver.origin import SettingsError, canonical_origin
from artisanlib.roastserver.outbox import Job, Outbox, OutboxError
from artisanlib.roastserver.settings import CredentialStore, CredentialStoreError, namespace_for

_REQUEST_ID_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_QUEUE_IDLE_MILLISECONDS: Final[int] = 1_000
_MAX_TIMER_MILLISECONDS: Final[int] = 2_147_483_647
_LEASE_SECONDS: Final[int] = 60


class OpaqueVault[T]:
    """Thread-safe one-shot transfer that never represents stored values."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, T] = {}

    @override
    def __repr__(self) -> str:
        return '<OpaqueVault values=<redacted>>'

    def put(self, value: T) -> str:
        request_id = uuid4().hex
        with self._lock:
            self._values[request_id] = value
        return request_id

    def take(self, request_id: str) -> T:
        with self._lock:
            return self._values.pop(request_id)

    def contains(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._values

    def size(self) -> int:
        with self._lock:
            return len(self._values)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


@dataclass(frozen=True, slots=True)
class WorkerConfiguration:
    origin: str
    namespace: Namespace | None
    enabled: bool
    automatic_upload: bool
    client_instance_uuid: UUID
    cache_limit_bytes: int


@dataclass(frozen=True, slots=True)
class ConnectionTestRequest:
    origin: str
    credential: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SavedProfileRequest:
    namespace: Namespace
    path: Path
    profile: ProfileData | None = field(repr=False)
    manual: bool


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


@dataclass(frozen=True, slots=True)
class CachedOpenRequest:
    cached: CachedRevision


@dataclass(frozen=True, slots=True)
class PublishRequest:
    detail: RoastDetail
    receipt: DownloadReceipt
    staged_path: Path


@dataclass(frozen=True, slots=True)
class ClearUnusedRequest:
    namespace: Namespace
    open_paths: frozenset[Path]


@dataclass(frozen=True, slots=True)
class _PendingStage:
    namespace: Namespace
    request: PublishRequest


class _DeliveryFailure(RuntimeError):
    def __init__(self, failure: PublicFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


type TimerFactory = Callable[[QObject], QTimer]
type ProfileLoader = Callable[[Path], ProfileData]


class RoastServerWorker(QObject):
    connectionTested = pyqtSignal(str, object)
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

    def __init__(
        self,
        *,
        outbox: Outbox,
        cache: CacheStore,
        credentials: CredentialStore,
        client_factory: ClientFactory,
        clock: Callable[[], datetime],
        credential_vault: OpaqueVault[ConnectionTestRequest],
        profile_vault: OpaqueVault[SavedProfileRequest],
        command_vault: OpaqueVault[object],
        timer_factory: TimerFactory | None = None,
        profile_loader: ProfileLoader | None = None,
    ) -> None:
        super().__init__()
        self._outbox = outbox
        self._cache = cache
        self._credentials = credentials
        self._client_factory = client_factory
        self._clock = clock
        self._credential_vault = credential_vault
        self._profile_vault = profile_vault
        self._command_vault = command_vault
        self._timer_factory = timer_factory or QTimer
        self._profile_loader = profile_loader or _load_saved_profile
        self._timer: QTimer | None = None
        self._configuration: WorkerConfiguration | None = None
        self._credential: str | None = None
        self._pending_stages: dict[Path, _PendingStage] = {}
        self._open_cache_paths: frozenset[Path] = frozenset()
        self._stop_event = threading.Event()
        self._started = False
        self._outbox_open = False
        self._stopped = False

    @override
    def __repr__(self) -> str:
        return '<RoastServerWorker credential=<redacted>>'

    @pyqtSlot()
    def start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        timer = self._timer_factory(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self.process_queue_once)
        self._timer = timer
        try:
            self._outbox.open()
            self._outbox_open = True
            self._outbox.recover_expired_leases(self._now())
        except (OutboxError, OSError, ValueError):
            self._emit_failure('start', _failure(FailureKind.LOCAL_PROFILE))
            return
        if self._configuration is not None:
            self._activate_configuration(None)

    @pyqtSlot(object)
    def configure(self, value: object) -> None:
        configuration = _valid_configuration(value)
        if configuration is None:
            self._emit_failure('configure', _failure(FailureKind.INVALID_RESPONSE))
            return
        previous = self._configuration
        self._configuration = configuration
        self._credential = None
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
        if old_namespace is not None and (
            old_namespace != configuration.namespace or not configuration.enabled
        ):
            self._pause_namespace(old_namespace, 'connector_disabled')

        namespace = configuration.namespace
        if not configuration.enabled or namespace is None:
            if namespace is not None:
                self._pause_namespace(namespace, 'connector_disabled')
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_aggregates(namespace)
            return

        try:
            credential = self._credentials.get(configuration.origin)
        except CredentialStoreError:
            self._pause_namespace(namespace, 'credential_removed')
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_failure('configure', _failure(FailureKind.KEYRING))
            self._emit_aggregates(namespace)
            return
        if credential is None or credential == '':
            self._pause_namespace(namespace, 'credential_removed')
            self.onlineChanged.emit(False)
            self._stop_timer()
            self._emit_aggregates(namespace)
            return

        self._credential = credential
        try:
            self._outbox.resume_namespace(namespace, self._now())
        except (OutboxError, ValueError):
            self._credential = None
            self._emit_failure('configure', _failure(FailureKind.LOCAL_PROFILE))
            self._stop_timer()
        self._emit_aggregates(namespace)
        if self._credential is not None:
            self._schedule_at(self._now())

    @pyqtSlot(str)
    def test_connection(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        request: ConnectionTestRequest | None = None
        credential = ''
        try:
            request = self._credential_vault.take(opaque_id)
            if not _valid_connection_request(request):
                raise ValueError
            credential = request.credential
            with self._client_factory(request.origin, credential) as client:
                identity = client.test_connection()
            if not isinstance(identity, ServerIdentity):
                raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
            self._credentials.set(request.origin, credential)
        except KeyError:
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        except CredentialStoreError:
            self._emit_failure(request_id, _failure(FailureKind.KEYRING))
            return
        except ApiFailure as error:
            self.onlineChanged.emit(False)
            self._emit_failure(request_id, error.failure)
            return
        except _DeliveryFailure as error:
            self._emit_failure(request_id, error.failure)
            return
        except (TypeError, ValueError):
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        except Exception:  # pylint: disable=broad-exception-caught
            self.onlineChanged.emit(False)
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        finally:
            request = None
            credential = ''
        self.connectionTested.emit(request_id, identity)

    @pyqtSlot(str)
    def enqueue_saved(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        try:
            request = self._profile_vault.take(opaque_id)
        except KeyError:
            self._emit_failure(request_id, _failure(FailureKind.LOCAL_PROFILE))
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
        try:
            snapshot = self._outbox.snapshot_saved_file(request.namespace, request.path)
            profile = (
                request.profile
                if request.profile is not None
                else self._profile_loader(snapshot.absolute_path)
            )
            if not isinstance(profile, dict):
                raise ValueError
            roast_uuid = _profile_roast_uuid(profile)
            metadata = project_profile(profile, snapshot.source_modified_at)
            self._outbox.enqueue(
                request.namespace,
                snapshot,
                roast_uuid,
                metadata,
                configuration.client_instance_uuid,
            )
            if self._credential is None:
                self._outbox.pause_namespace(
                    request.namespace, self._now(), 'credential_removed'
                )
        except (OutboxError, OSError, RecursionError, SyntaxError, TypeError, ValueError):
            self._emit_failure(request_id, _failure(FailureKind.LOCAL_PROFILE))
        self._emit_aggregates(request.namespace)
        if self._credential is not None:
            self._schedule_at(self._now())

    @pyqtSlot()
    def process_queue_once(self) -> None:
        configuration = self._configuration
        namespace = configuration.namespace if configuration is not None else None
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
        try:
            self._outbox.recover_expired_leases(now)
            job = self._outbox.lease_next(namespace, now, _LEASE_SECONDS)
        except (OutboxError, OSError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            self._emit_aggregates(namespace)
            self._schedule_idle()
            return
        if job is None:
            self._emit_aggregates(namespace)
            self._schedule_idle()
            return

        self._deliver_job(configuration, job)
        self._emit_aggregates(namespace)

    def _deliver_job(self, configuration: WorkerConfiguration, job: Job) -> None:
        token = job.lease_token
        if token is None:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            self._schedule_idle()
            return
        failure: PublicFailure | None = None
        retry_after: int | None = None
        status_code: int | None = None
        try:
            self._execute_delivery(configuration, job)
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
                self.onlineChanged.emit(True)
                self._schedule_at(now)
            else:
                self._schedule_idle()
            return

        if status_code == 401 or failure.kind is FailureKind.CREDENTIAL_REJECTED:
            if self._commit_retry(job, token, now, now, failure):
                self._pause_namespace(job.namespace, 'credential_rejected')
                self._credential = None
                self.onlineChanged.emit(False)
                self._stop_timer()
            else:
                self._schedule_idle()
            self._emit_failure('queue', failure)
            return

        if failure.retryable:
            delay = _retry_delay(job.attempts, retry_after)
            next_attempt_at = now + timedelta(seconds=delay)
            if self._commit_retry(job, token, now, next_attempt_at, failure):
                self._schedule_at(next_attempt_at)
            else:
                self._schedule_idle()
            self.onlineChanged.emit(False)
            self._emit_failure('queue', failure)
            return

        if self._commit_failed(job, token, now, failure):
            self._schedule_at(now)
        else:
            self._schedule_idle()
        self._emit_failure('queue', failure)

    def _execute_delivery(
        self, configuration: WorkerConfiguration, job: Job
    ) -> None:
        credential = self._credential
        if credential is None:
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
            client.post_aroast(job.roast_uuid, job.aroast_json.encode('utf-8'))
            upload = client.upload_revision(
                job.roast_uuid,
                job.content_sha256,
                job.idempotency_key,
                job.revision_json.encode('utf-8'),
                snapshot,
            )
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

    @pyqtSlot(str)
    def retry_job(self, job_id: str) -> None:
        namespace = self._current_namespace()
        if namespace is None or not self._failed_job_is_current(namespace, job_id):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return
        try:
            self._outbox.retry_now(job_id, self._now())
            if self._credential is None:
                self._outbox.pause_namespace(
                    namespace, self._now(), 'credential_removed'
                )
        except (OutboxError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        self._emit_aggregates(namespace)
        if self._credential is not None:
            self._schedule_at(self._now())

    @pyqtSlot(str)
    def remove_job(self, job_id: str) -> None:
        namespace = self._current_namespace()
        if namespace is None or not self._failed_job_is_current(namespace, job_id):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
            return
        try:
            self._outbox.remove(job_id)
        except (OutboxError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        self._emit_aggregates(namespace)

    def _failed_job_is_current(self, namespace: Namespace, job_id: str) -> bool:
        try:
            return any(job.id == job_id for job in self._outbox.failed_jobs(namespace))
        except OutboxError:
            return False

    @pyqtSlot(str)
    def browse(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        if not isinstance(value, BrowseRequest) or not self._namespace_is_current(
            value.namespace
        ):
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        request = value
        credential = self._credential
        configuration = self._configuration
        if credential is None or configuration is None or not configuration.enabled:
            self._browse_fallback(request_id, request, _failure(FailureKind.OFFLINE))
            return
        try:
            with self._client_factory(configuration.origin, credential) as client:
                page = client.list_roasts(request.filters, cursor=request.cursor, limit=50)
            if not isinstance(page, RoastPage):
                raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
        except ApiFailure as error:
            self._handle_nonqueue_api_failure(request_id, request.namespace, error)
            self._browse_fallback(request_id, request, error.failure, emit_failure=False)
            return
        except _DeliveryFailure as error:
            self._browse_fallback(request_id, request, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            self._browse_fallback(
                request_id, request, _failure(FailureKind.INVALID_RESPONSE)
            )
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
        if emit_failure:
            self._emit_failure(request_id, failure)
        self.onlineChanged.emit(False)
        try:
            page = self._cache.list_offline(request.namespace, request.filters)
        except CacheError as error:
            self._emit_failure(request_id, error.failure)
            return
        self.archivePageReady.emit(request_id, page)

    @pyqtSlot(str)
    def open_online(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        if not isinstance(value, OnlineOpenRequest) or not self._namespace_is_current(
            value.namespace
        ):
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        request = value
        configuration = self._configuration
        credential = self._credential
        if configuration is None or credential is None or not configuration.enabled:
            self._emit_failure(request_id, _failure(FailureKind.OFFLINE))
            self.onlineChanged.emit(False)
            return

        staged_path: Path | None = None
        try:
            with self._client_factory(configuration.origin, credential) as client:
                detail_value: object = client.get_roast(request.roast_uuid)
                if (
                    not isinstance(detail_value, RoastDetail)
                    or detail_value.roast_uuid != request.roast_uuid
                    or detail_value.current_revision is None
                ):
                    raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
                detail = detail_value
                staged_path, output = self._cache.new_staging_file(request.namespace)
                receipt = client.download_revision(detail, output)
            publish_request = PublishRequest(detail, receipt, staged_path)
            if not _valid_publish_request(publish_request):
                raise _DeliveryFailure(_failure(FailureKind.INVALID_RESPONSE))
            if self._interrupted():
                self._discard_stage(staged_path)
                self._stop_timer()
                return
            self._pending_stages[staged_path] = _PendingStage(
                request.namespace, publish_request
            )
        except ApiFailure as error:
            if staged_path is not None:
                self._discard_stage(staged_path)
            self._handle_nonqueue_api_failure(request_id, request.namespace, error)
            return
        except CacheError as error:
            if staged_path is not None:
                self._discard_stage(staged_path)
            self._emit_failure(request_id, error.failure)
            return
        except _DeliveryFailure as error:
            if staged_path is not None:
                self._discard_stage(staged_path)
            self._emit_failure(request_id, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if staged_path is not None:
                self._discard_stage(staged_path)
            self._emit_failure(request_id, _failure(FailureKind.INVALID_RESPONSE))
            return
        self.downloadStaged.emit(request_id, publish_request)
        self.onlineChanged.emit(True)

    @pyqtSlot(str)
    def open_cached(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        if not isinstance(value, CachedOpenRequest) or not self._namespace_is_current(
            value.cached.namespace
        ):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        try:
            cached = self._cache.validate(value.cached)
        except CacheError as error:
            self._emit_failure(request_id, error.failure)
            return
        self.cachedReady.emit(request_id, cached)

    @pyqtSlot(str)
    def publish_staged(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            self._discard_all_stages()
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        if not isinstance(value, PublishRequest):
            self._discard_all_stages()
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        request = value
        pending = self._pending_stages.get(request.staged_path)
        if pending is None or pending.request != request or not _valid_publish_request(request):
            if pending is None:
                self._discard_all_stages()
            else:
                self._discard_stage(request.staged_path)
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        if self._interrupted():
            self._discard_stage(request.staged_path)
            self._stop_timer()
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
            self._emit_failure(request_id, error.failure)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        self.cachePublished.emit(request_id, cached)
        self._prune_to_limit(request_id, pending.namespace)

    @pyqtSlot(str)
    def discard_staged(self, staged_path_text: str) -> None:
        matching = next(
            (
                path
                for path in self._pending_stages
                if str(path) == staged_path_text
            ),
            None,
        )
        if matching is None:
            self._discard_all_stages()
            self._emit_failure('discard', CACHE_FAILURE)
            return
        self._discard_stage(matching)
        self._emit_cache_stats(matching_operation='discard')

    @pyqtSlot(str)
    def clear_unused(self, opaque_id: str) -> None:
        request_id = _public_request_id(opaque_id)
        try:
            value = self._command_vault.take(opaque_id)
        except KeyError:
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        if not _valid_clear_request(value):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        request = value
        if not self._namespace_is_current(request.namespace):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        self._open_cache_paths = request.open_paths
        try:
            protected = request.open_paths | self._outbox.protected_paths(
                request.namespace
            )
            stats = self._cache.clear_unused(request.namespace, protected)
        except (CacheError, OutboxError, OSError, ValueError):
            self._emit_failure(request_id, CACHE_FAILURE)
            return
        self.cacheStatsChanged.emit(stats)

    def _prune_to_limit(self, operation: str, namespace: Namespace) -> None:
        configuration = self._configuration
        if configuration is None or configuration.namespace != namespace:
            return
        try:
            protected = self._open_cache_paths | self._outbox.protected_paths(namespace)
            stats = self._cache.prune(
                namespace, configuration.cache_limit_bytes, protected
            )
        except (CacheError, OutboxError, OSError, ValueError):
            self._emit_failure(operation, CACHE_FAILURE)
            return
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

    def _handle_nonqueue_api_failure(
        self, operation: str, namespace: Namespace, error: ApiFailure
    ) -> None:
        if error.status_code == 401 or error.failure.kind is FailureKind.CREDENTIAL_REJECTED:
            self._pause_namespace(namespace, 'credential_rejected')
            self._credential = None
            self._stop_timer()
        self.onlineChanged.emit(False)
        self._emit_failure(operation, error.failure)

    def _pause_namespace(self, namespace: Namespace, code: str) -> None:
        if not self._outbox_open:
            return
        try:
            self._outbox.pause_namespace(namespace, self._now(), code)
        except (OutboxError, ValueError):
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))

    def _emit_aggregates(self, namespace: Namespace | None) -> None:
        if namespace is None or not self._outbox_open:
            return
        try:
            counts = self._outbox.counts(namespace)
        except OutboxError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        else:
            self.queueChanged.emit(counts)
        try:
            failed = self._outbox.failed_jobs(namespace)
        except OutboxError:
            self._emit_failure('queue', _failure(FailureKind.LOCAL_PROFILE))
        else:
            self.failedJobsChanged.emit(failed)
        self._emit_cache_stats(namespace=namespace, matching_operation='cache')

    def _emit_cache_stats(
        self,
        *,
        namespace: Namespace | None = None,
        matching_operation: str,
    ) -> None:
        selected = namespace or self._current_namespace()
        if selected is None:
            return
        try:
            stats = self._cache.stats(selected)
        except CacheError as error:
            self._emit_failure(matching_operation, error.failure)
        else:
            self.cacheStatsChanged.emit(stats)

    @pyqtSlot()
    def stop(self) -> None:
        if self._stopped:
            return
        self._stop_event.set()
        self._stop_timer()
        self._discard_all_stages()
        self._credential_vault.clear()
        self._profile_vault.clear()
        self._command_vault.clear()
        self._credential = None
        try:
            self._cache.close()
        except CacheError as error:
            self._emit_failure('stop', error.failure)
        finally:
            self._pending_stages.clear()
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
        )

    def _current_namespace(self) -> Namespace | None:
        configuration = self._configuration
        return configuration.namespace if configuration is not None else None

    def _interrupted(self) -> bool:
        thread = self.thread()
        return self._stop_event.is_set() or (
            isinstance(thread, QThread) and thread.isInterruptionRequested()
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('worker clock must return an aware datetime')
        return now.astimezone(UTC)

    def _schedule_idle(self) -> None:
        timer = self._timer
        if timer is not None and not self._interrupted() and self._credential is not None:
            timer.start(_QUEUE_IDLE_MILLISECONDS)

    def _schedule_at(self, due: datetime) -> None:
        timer = self._timer
        if timer is None or self._interrupted() or self._credential is None:
            return
        seconds = max(0.0, (due.astimezone(UTC) - self._now()).total_seconds())
        milliseconds = min(_MAX_TIMER_MILLISECONDS, math.ceil(seconds * 1_000))
        timer.start(milliseconds)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()

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
    if (
        type(enabled) is not bool
        or type(automatic_upload) is not bool
        or not isinstance(client_instance_uuid, UUID)
        or type(cache_limit_bytes) is not int
        or cache_limit_bytes < 0
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
    if namespace_value is not None:
        if not isinstance(namespace_value, Namespace):
            return None
        expected = namespace_for(origin, namespace_value.organization_id)
        if namespace_value != expected:
            return None
    return value


def _valid_connection_request(value: object) -> bool:
    if not isinstance(value, ConnectionTestRequest):
        return False
    credential: object = value.credential
    if not isinstance(credential, str) or credential == '':
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
    path: object = value.path
    profile: object = value.profile
    manual: object = value.manual
    return (
        isinstance(namespace, Namespace)
        and isinstance(path, Path)
        and (profile is None or isinstance(profile, dict))
        and type(manual) is bool
    )


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


def _valid_clear_request(value: object) -> TypeGuard[ClearUnusedRequest]:
    if not isinstance(value, ClearUnusedRequest):
        return False
    namespace: object = value.namespace
    open_paths: object = value.open_paths
    return (
        isinstance(namespace, Namespace)
        and isinstance(open_paths, frozenset)
        and all(isinstance(path, Path) for path in open_paths)
    )


def _profile_roast_uuid(profile: ProfileData) -> UUID:
    value = profile.get('roastUUID')
    if not isinstance(value, str):
        raise ValueError('saved profile roast UUID is unavailable')
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError('saved profile roast UUID is invalid') from None


def _load_saved_profile(path: Path) -> ProfileData:
    with path.open('rb') as source:
        content = source.read(MAX_PROFILE_BYTES + 1)
    if not content or len(content) > MAX_PROFILE_BYTES:
        raise ValueError('saved profile size is invalid')
    try:
        value = ast.literal_eval(content.decode('utf-8'))
    except (RecursionError, SyntaxError, ValueError):
        raise ValueError('saved profile is invalid') from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError('saved profile is invalid')
    return cast(ProfileData, value)


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
    'ConnectionTestRequest',
    'OnlineOpenRequest',
    'OpaqueVault',
    'PublishRequest',
    'RoastServerWorker',
    'SavedProfileRequest',
    'WorkerConfiguration',
]
