#
# ABOUT
# Artisan Roast Server bounded synchronous HTTP client
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

# HTTP boundary code intentionally normalizes unexpected client and transport failures.
# pylint: disable=broad-exception-caught

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import hmac
from io import SEEK_END, BytesIO
import json
import math
import re
import secrets
import threading
import time
from types import TracebackType
from typing import TYPE_CHECKING, BinaryIO, Final, NoReturn, Protocol, Self, TypeVar, cast, override, runtime_checkable
from uuid import UUID
import weakref

import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from artisanlib import __version__
from artisanlib.roastserver.contract import (
    ArchiveFilters,
    ContractError,
    FAILURE_MESSAGES,
    FailureKind,
    MAX_CURSOR_CHARS,
    MAX_JSON_BYTES,
    MAX_METADATA_BYTES,
    MAX_PROFILE_BYTES,
    POSTGRESQL_INTEGER_MAX,
    PublicFailure,
    RevisionUpload,
    RoastDetail,
    RoastPage,
    ServerIdentity,
    parse_aroast_ack,
    parse_identity,
    parse_revision_upload,
    parse_roast_detail,
    parse_roast_page,
    validate_archive_filters,
)
from artisanlib.roastserver.origin import canonical_origin

if TYPE_CHECKING:
    from artisanlib.roastserver.inventory_contract import (
        BeanLotPage,
        InventoryCommandRequest,
        InventoryMutationResult,
    )

CONNECT_TIMEOUT_SECONDS: Final[float] = 4.0
READ_TIMEOUT_SECONDS: Final[float] = 10.0
# This hard whole-operation budget remains below the worker's 15-second shutdown wait.
OPERATION_DEADLINE_SECONDS: Final[float] = 12.0
MAX_RETRY_AFTER_SECONDS: Final[int] = 3600

_RESPONSE_CHUNK_BYTES: Final[int] = 64 * 1024
_MULTIPART_OVERHEAD_BYTES: Final[int] = 4096
_JSON_CONTENT_TYPE: Final[str] = 'application/json'
_PROFILE_CONTENT_TYPE: Final[str] = 'application/x-artisan-profile'
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_SESSION_TYPE: Final[type[requests.Session]] = requests.sessions.Session
_FIXED_SESSION_HEADERS: Final[dict[str, str]] = {
    'Accept-Encoding': 'identity',
    'Cache-Control': 'no-store',
    'User-Agent': f'Artisan/{__version__}',
}
_SECURITY_RESPONSE_HEADERS: Final[tuple[str, ...]] = (
    'Content-Encoding',
    'Transfer-Encoding',
    'Content-Length',
    'Content-Type',
    'Content-Disposition',
    'X-Revision-Number',
    'X-Content-SHA256',
    'X-Checksum-SHA256',
    'ETag',
    'Retry-After',
    'Location',
)

_ResultT = TypeVar('_ResultT')


@runtime_checkable
class _GetListHeaders(Protocol):
    def getlist(self, name: str) -> object: ...


@runtime_checkable
class _GetAllHeaders(Protocol):
    def get_all(self, name: str) -> object: ...


class ApiFailure(RuntimeError):
    def __init__(
        self,
        failure: PublicFailure,
        status_code: int | None,
        retry_after_seconds: int | None,
    ) -> None:
        self.failure = failure
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(failure.message)


class _ResponseBodyError(ValueError):
    def __init__(self) -> None:
        super().__init__('invalid response body')


class _DeadlineGuard:
    """Secret-free watchdog state for one absolute monotonic deadline."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + OPERATION_DEADLINE_SECONDS
        self._done = threading.Event()
        self._expired = threading.Event()
        self._response_lock = threading.Lock()
        self._response_ref: weakref.ReferenceType[requests.Response] | None = None
        self._client_ref: weakref.ReferenceType[RoastServerClient] | None = None
        self._thread: threading.Thread | None = None

    @override
    def __repr__(self) -> str:
        return '<RoastServerOperationDeadline>'

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    def start(self, client: RoastServerClient) -> None:
        self._client_ref = weakref.ref(client)
        thread = threading.Thread(
            target=_deadline_watchdog,
            args=(self,),
            name='RoastServerDeadlineWatchdog',
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def check(self) -> None:
        if self._done.is_set():
            return
        if not self._expired.is_set() and time.monotonic() < self.deadline:
            return
        self.expire()
        raise _operation_timeout_failure()

    def expire(self) -> None:
        client_ref = self._client_ref
        client = None if client_ref is None else client_ref()
        if client is None:
            self._expired.set()
            return
        client._abort_for_deadline(self)  # pylint: disable=protected-access

    def mark_expired(self) -> None:
        self._expired.set()

    def register_response(self, response: requests.Response) -> None:
        self.check()
        with self._response_lock:
            self._response_ref = weakref.ref(response)
        self.check()

    def response(self) -> requests.Response | None:
        with self._response_lock:
            response_ref = self._response_ref
        return None if response_ref is None else response_ref()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def disarm(self) -> None:
        self._done.set()

    def join(self) -> None:
        thread = self._thread
        self._thread = None
        self._client_ref = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.1)


class _DeadlineUploadBody(BytesIO):
    """Seekable prepared upload whose transport reads check the operation deadline."""

    def __init__(self, content: bytes, deadline: _DeadlineGuard) -> None:
        super().__init__(content)
        self._byte_count = len(content)
        self._deadline = deadline

    @override
    def __repr__(self) -> str:
        return '<RoastServerUploadBody content=<redacted>>'

    def __len__(self) -> int:
        self._deadline.check()
        return self._byte_count

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        self._deadline.check()
        value = super().read(size)
        self._deadline.check()
        return value

    @override
    def seek(self, offset: int, whence: int = 0, /) -> int:
        self._deadline.check()
        position = super().seek(offset, whence)
        self._deadline.check()
        return position

    @override
    def tell(self) -> int:
        self._deadline.check()
        return super().tell()


@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    roast_uuid: UUID
    revision_number: int
    sha256: str
    byte_count: int
    filename: str


class RoastServerClient:
    def __init__(
        self,
        origin: str,
        credential: str,
    ) -> None:
        if credential == '':
            raise ValueError('credential must not be empty')
        origin = canonical_origin(origin)
        session = requests.Session()
        if type(session) is not _SESSION_TYPE:
            _close_owned_session(session)
            raise TypeError('session factory must return an exact requests.Session')
        try:
            _sanitize_session(session, replace_adapters=True)
        except ApiFailure:
            _close_owned_session(session)
            raise
        self._origin = origin
        self._credential = credential
        self._session: requests.Session | None = session
        self._closed = False
        self._state_lock = threading.RLock()
        self._active_deadline: _DeadlineGuard | None = None

    @override
    def __repr__(self) -> str:
        return '<RoastServerClient credential=<redacted>>'

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._state_lock:
            if self._closed and self._session is None:
                return
            self._closed = True
            self._credential = ''
            session = self._session
            self._session = None
            deadline = self._active_deadline
            self._active_deadline = None
            if deadline is not None:
                deadline.disarm()
                response = deadline.response()
            else:
                response = None
        if response is not None:
            _close_response_transport(response)
        if session is not None:
            _close_owned_session(session)
        if deadline is not None:
            deadline.join()

    def test_connection(self) -> ServerIdentity:
        return self._run_operation(self._test_connection)

    def _test_connection(self, deadline: _DeadlineGuard) -> ServerIdentity:
        response = self._request(
            'GET', '/api/v1/auth/me', deadline=deadline, stream=True)
        try:
            self._require_status(response, frozenset({200}))
            return self._parse_json_response(response, parse_identity, deadline)
        finally:
            _close_response(response)

    def post_aroast(self, roast_uuid: UUID, aroast_json: bytes) -> None:
        def operation(deadline: _DeadlineGuard) -> None:
            _require_bounded_bytes(
                aroast_json, maximum=MAX_JSON_BYTES, name='aroast JSON')
            deadline.check()
            response = self._request(
                'POST',
                '/api/v1/aroast',
                deadline=deadline,
                json_bytes=aroast_json,
                stream=True,
            )
            try:
                self._require_status(response, frozenset({200}))
                acknowledgement = self._parse_json_response(
                    response, parse_aroast_ack, deadline)
                if acknowledgement.result.roast_id != roast_uuid:
                    raise _fixed_api_failure(
                        FailureKind.INVALID_RESPONSE, status_code=200)
            finally:
                _close_response(response)

        self._run_operation(operation)

    def upload_revision(
        self,
        roast_uuid: UUID,
        sha256: str,
        idempotency_key: str,
        metadata_json: bytes,
        snapshot: BinaryIO,
    ) -> RevisionUpload:
        def operation(deadline: _DeadlineGuard) -> RevisionUpload:
            if _SHA256_RE.fullmatch(sha256) is None:
                raise ValueError('invalid SHA-256')
            if (
                not 1 <= len(idempotency_key) <= 255
                or idempotency_key.strip() == ''
                or _has_prohibited_text_code_point(idempotency_key)
            ):
                raise ValueError('invalid idempotency key')
            _require_bounded_bytes(
                metadata_json,
                maximum=MAX_METADATA_BYTES,
                name='revision metadata',
            )
            deadline.check()
            snapshot_bytes = _freeze_snapshot(snapshot, deadline)
            snapshot_size = len(snapshot_bytes)
            snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
            deadline.check()
            if not hmac.compare_digest(snapshot_sha256, sha256):
                raise _fixed_api_failure(FailureKind.LOCAL_PROFILE, status_code=None)

            content_type, upload_body = _prepare_multipart_upload(
                roast_uuid,
                sha256,
                idempotency_key,
                metadata_json,
                snapshot_bytes,
                deadline,
            )
            response = self._request(
                'POST',
                f'/api/v1/roasts/{roast_uuid.hex}/revisions',
                deadline=deadline,
                data=upload_body,
                body_content_type=content_type,
                body_content_length=len(upload_body),
                stream=True,
            )
            try:
                self._require_status(response, frozenset({200, 201}))
                result = self._parse_json_response(
                    response, parse_revision_upload, deadline)
                if (
                    result.roast_uuid != roast_uuid
                    or not hmac.compare_digest(result.revision.sha256, sha256)
                    or result.revision.byte_size != snapshot_size
                ):
                    raise _fixed_api_failure(
                        FailureKind.INVALID_RESPONSE,
                        status_code=response.status_code,
                    )
                return result
            finally:
                _close_response(response)

        return self._run_operation(operation)

    def list_roasts(
        self,
        filters: ArchiveFilters,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RoastPage:
        def operation(deadline: _DeadlineGuard) -> RoastPage:
            if isinstance(limit, bool) or not 1 <= limit <= 100:
                raise ValueError('invalid archive page limit')
            normalized_filters = validate_archive_filters(filters)
            params: dict[str, str | int] = {'limit': limit}
            if cursor is not None:
                params['cursor'] = _bounded_query_text(
                    cursor,
                    maximum=MAX_CURSOR_CHARS,
                    name='cursor',
                )
            if normalized_filters.search is not None:
                params['search'] = normalized_filters.search
            if normalized_filters.state is not None:
                params['state'] = normalized_filters.state
            if normalized_filters.machine is not None:
                params['machine'] = normalized_filters.machine
            roast_at_from = _utc_query_datetime(
                normalized_filters.roast_at_from, name='roast_at_from'
            )
            roast_at_to = _utc_query_datetime(
                normalized_filters.roast_at_to, name='roast_at_to'
            )
            if roast_at_from is not None:
                params['roast_at_from'] = roast_at_from
            if roast_at_to is not None:
                params['roast_at_to'] = roast_at_to
            deadline.check()
            response = self._request(
                'GET',
                '/api/v1/roasts',
                deadline=deadline,
                params=params,
                stream=True,
            )
            try:
                self._require_status(response, frozenset({200}))
                return self._parse_json_response(
                    response, parse_roast_page, deadline)
            finally:
                _close_response(response)

        return self._run_operation(operation)

    def list_inventory_lots(
        self,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BeanLotPage:
        def operation(deadline: _DeadlineGuard) -> BeanLotPage:
            from artisanlib.roastserver.inventory_contract import (
                MAX_INVENTORY_CURSOR_CHARS,
                MAX_INVENTORY_PAGES,
                parse_bean_lot_page,
            )

            if isinstance(limit, bool) or not 1 <= limit <= MAX_INVENTORY_PAGES:
                raise ValueError('invalid inventory page limit')
            params: dict[str, str | int] = {'limit': limit}
            if cursor is not None:
                params['cursor'] = _bounded_query_text(
                    cursor,
                    maximum=MAX_INVENTORY_CURSOR_CHARS,
                    name='inventory cursor',
                )
            deadline.check()
            response = self._request(
                'GET',
                '/api/v1/inventory/bean-lots',
                deadline=deadline,
                params=params,
                stream=True,
            )
            try:
                self._require_inventory_status(
                    response,
                    frozenset({200}),
                    deadline,
                )
                return self._parse_json_response(
                    response,
                    parse_bean_lot_page,
                    deadline,
                )
            finally:
                _close_response(response)

        return self._run_operation(operation)

    def execute_inventory_command(
        self,
        request: InventoryCommandRequest,
    ) -> InventoryMutationResult:
        def operation(deadline: _DeadlineGuard) -> InventoryMutationResult:
            from artisanlib.roastserver.inventory_contract import parse_inventory_mutation

            _validate_inventory_command_request(request)
            if request.operation == 'reserve':
                path = '/api/v1/inventory/reservations'
                success_statuses = frozenset({201})
            else:
                path = (
                    f'/api/v1/inventory/reservations/{request.reservation_uuid.hex}/'
                    f'{request.operation}'
                )
                success_statuses = frozenset({200})
            deadline.check()
            response = self._request(
                'POST',
                path,
                deadline=deadline,
                json_bytes=request.request_json,
                additional_headers={'Idempotency-Key': request.idempotency_key},
                stream=True,
            )
            try:
                self._require_inventory_status(
                    response,
                    success_statuses,
                    deadline,
                )
                return self._parse_json_response(
                    response,
                    lambda value: parse_inventory_mutation(
                        value,
                        operation=request.operation,
                        expected_client_reservation_uuid=request.reservation_uuid,
                        expected_client_instance_uuid=request.client_instance_uuid,
                        expected_roast_uuid=request.roast_uuid,
                        expected_lot_id=request.lot_id,
                        expected_planned_grams=request.planned_grams,
                        requested_actual_grams=request.requested_actual_grams,
                    ),
                    deadline,
                )
            finally:
                _close_response(response)

        return self._run_operation(operation)

    def get_roast(self, roast_uuid: UUID) -> RoastDetail:
        def operation(deadline: _DeadlineGuard) -> RoastDetail:
            response = self._request(
                'GET',
                f'/api/v1/roasts/{roast_uuid.hex}',
                deadline=deadline,
                stream=True,
            )
            try:
                self._require_status(response, frozenset({200}))
                detail = self._parse_json_response(
                    response, parse_roast_detail, deadline)
                if detail.roast_uuid != roast_uuid:
                    raise _fixed_api_failure(
                        FailureKind.INVALID_RESPONSE,
                        status_code=response.status_code,
                    )
                return detail
            finally:
                _close_response(response)

        return self._run_operation(operation)

    def download_revision(
        self,
        detail: RoastDetail,
        destination: BinaryIO,
    ) -> DownloadReceipt:
        """Stream into an empty connector-owned destination and rewind on success.

        The destination must support truncation. The caller must discard it whenever
        the download fails. If rollback cannot be completed, the destination is closed.
        """
        def operation(deadline: _DeadlineGuard) -> DownloadReceipt:
            revision = detail.current_revision
            if revision is None:
                raise _fixed_api_failure(
                    FailureKind.INVALID_RESPONSE, status_code=None)
            _prepare_empty_destination(destination)
            deadline.check()
            filename = f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog'
            path = (
                f'/api/v1/roasts/{detail.roast_uuid.hex}/revisions/'
                f'{revision.revision_number}/download'
            )
            response = self._request('GET', path, deadline=deadline, stream=True)
            try:
                try:
                    self._require_status(response, frozenset({200}))
                    self._validate_download_headers(
                        response,
                        expected_sha256=revision.sha256,
                        expected_byte_count=revision.byte_size,
                        expected_revision_number=revision.revision_number,
                        expected_filename=filename,
                    )
                    byte_count, downloaded_sha256 = _stream_profile(
                        response,
                        destination,
                        expected_byte_count=revision.byte_size,
                        deadline=deadline,
                    )
                    if not hmac.compare_digest(downloaded_sha256, revision.sha256):
                        raise _fixed_api_failure(
                            FailureKind.CHECKSUM_MISMATCH,
                            status_code=response.status_code,
                        )
                    _finish_profile_destination(destination, deadline)
                    return DownloadReceipt(
                        roast_uuid=detail.roast_uuid,
                        revision_number=revision.revision_number,
                        sha256=downloaded_sha256,
                        byte_count=byte_count,
                        filename=filename,
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    _rollback_destination(destination)
                    raise
            finally:
                _close_response(response)

        return self._run_operation(operation)

    def _request(
        self,
        method: str,
        path: str,
        *,
        deadline: _DeadlineGuard,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str | bytes] | BinaryIO | None = None,
        json_bytes: bytes | None = None,
        body_content_type: str | None = None,
        body_content_length: int | None = None,
        additional_headers: Mapping[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        deadline.check()
        self._require_open()
        session = self._session
        if session is None or type(session) is not _SESSION_TYPE:
            raise _fixed_api_failure(
                FailureKind.INVALID_RESPONSE,
                status_code=None,
                code='request_error',
            )
        url = self._same_origin_url(path)
        headers = dict(_FIXED_SESSION_HEADERS)
        headers['Authorization'] = f'Bearer {self._credential}'
        if additional_headers is not None:
            if (
                set(additional_headers) != {'Idempotency-Key'}
                or not isinstance(additional_headers.get('Idempotency-Key'), str)
            ):
                raise ValueError('invalid additional request header')
            idempotency_key = additional_headers['Idempotency-Key']
            if (
                not 1 <= len(idempotency_key) <= 255
                or idempotency_key.strip() == ''
                or _has_prohibited_text_code_point(idempotency_key)
            ):
                raise ValueError('invalid additional request header')
            headers['Idempotency-Key'] = idempotency_key
        request_data: Mapping[str, str | bytes] | BinaryIO | bytes | None = data
        if json_bytes is not None:
            if data is not None or body_content_type is not None:
                raise ValueError('JSON cannot be combined with another request body')
            headers['Content-Type'] = _JSON_CONTENT_TYPE
            request_data = json_bytes
        elif body_content_type is not None:
            if data is None or body_content_length is None:
                raise ValueError('request body headers require a body')
            headers['Content-Type'] = body_content_type
            headers['Content-Length'] = str(body_content_length)
        deadline.check()
        _sanitize_session(session)
        deadline.check()
        request_failure: ApiFailure | None = None
        response: requests.Response | None = None
        try:
            response = session.request(
                method,
                url,
                params=params,
                data=request_data,
                headers=headers,
                stream=stream,
                verify=True,
                allow_redirects=False,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except Exception as error:
            request_failure = _request_api_failure(error)
        if request_failure is not None:
            raise request_failure
        if response is None:
            raise _fixed_api_failure(FailureKind.INVALID_RESPONSE, status_code=None)
        deadline.register_response(response)

        sanitization_failure: ApiFailure | None = None
        try:
            _sanitize_session(session)
        except ApiFailure as error:
            sanitization_failure = error
        if sanitization_failure is not None:
            _close_response(response)
            raise sanitization_failure

        security_failure = _response_security_failure(response)
        if security_failure is not None:
            _close_response(response)
            raise security_failure
        deadline.check()
        return response

    def _run_operation(
        self,
        operation: Callable[[_DeadlineGuard], _ResultT],
    ) -> _ResultT:
        deadline = self._start_operation()
        result: _ResultT | None = None
        operation_error: Exception | None = None
        try:
            result = operation(deadline)
        except Exception as error:  # pylint: disable=broad-exception-caught
            operation_error = error
        except (KeyboardInterrupt, SystemExit):
            self._finish_operation(deadline)
            raise
        expired = self._finish_operation(deadline)
        if expired:
            raise _operation_timeout_failure()
        if operation_error is not None:
            raise operation_error
        return cast(_ResultT, result)

    def _start_operation(self) -> _DeadlineGuard:
        with self._state_lock:
            if self._closed:
                raise _fixed_api_failure(
                    FailureKind.OFFLINE,
                    status_code=None,
                    code='client_closed',
                )
            if self._active_deadline is not None:
                raise _fixed_api_failure(
                    FailureKind.INVALID_RESPONSE,
                    status_code=None,
                    code='request_error',
                )
            deadline = _DeadlineGuard()
            self._active_deadline = deadline
        deadline.start(self)
        return deadline

    def _finish_operation(self, deadline: _DeadlineGuard) -> bool:
        session: requests.Session | None = None
        response: requests.Response | None = None
        with self._state_lock:
            if (
                self._active_deadline is deadline
                and not deadline.expired
                and time.monotonic() >= deadline.deadline
            ):
                deadline.mark_expired()
                self._closed = True
                self._credential = ''
                session = self._session
                self._session = None
                response = deadline.response()
            if self._active_deadline is deadline:
                self._active_deadline = None
            deadline.disarm()
            expired = deadline.expired
        if response is not None:
            _close_response_transport(response)
        if session is not None:
            _close_owned_session(session)
        deadline.join()
        return expired

    def _abort_for_deadline(self, deadline: _DeadlineGuard) -> None:
        with self._state_lock:
            if self._active_deadline is not deadline or deadline.expired:
                return
            deadline.mark_expired()
            self._closed = True
            self._credential = ''
            session = self._session
            self._session = None
            response = deadline.response()
        if response is not None:
            _close_response_transport(response)
        if session is not None:
            _close_owned_session(session)

    def _require_open(self) -> None:
        with self._state_lock:
            closed = self._closed
        if closed:
            raise _fixed_api_failure(
                FailureKind.OFFLINE,
                status_code=None,
                code='client_closed',
            )

    def _same_origin_url(self, path: str) -> str:
        if (
            not path.startswith('/')
            or path.startswith('//')
            or '?' in path
            or '#' in path
            or '\\' in path
            or _has_prohibited_text_code_point(path)
        ):
            raise ValueError('invalid relative API path')
        return f'{self._origin}{path}'

    def _require_status(
        self,
        response: requests.Response,
        expected_statuses: frozenset[int],
    ) -> None:
        if response.status_code in expected_statuses:
            return
        raise self._response_api_failure(response)

    def _response_api_failure(self, response: requests.Response) -> ApiFailure:
        status_code = response.status_code
        if 300 <= status_code <= 399:
            kind = FailureKind.INVALID_RESPONSE
            retryable = False
        elif status_code == 401:
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

        retry_after_seconds = (
            _parse_retry_after(response.headers.get('Retry-After'))
            if status_code == 429 or 500 <= status_code <= 599
            else None
        )
        return ApiFailure(
            PublicFailure(
                kind=kind,
                code=kind.value,
                message=FAILURE_MESSAGES[kind],
                retryable=retryable,
            ),
            status_code,
            retry_after_seconds,
        )

    def _require_inventory_status(
        self,
        response: requests.Response,
        expected_statuses: frozenset[int],
        deadline: _DeadlineGuard,
    ) -> None:
        if response.status_code in expected_statuses:
            return
        raise self._inventory_response_api_failure(response, deadline)

    def _inventory_response_api_failure(
        self,
        response: requests.Response,
        deadline: _DeadlineGuard,
    ) -> ApiFailure:
        from artisanlib.roastserver.inventory_contract import parse_inventory_error

        status_code = response.status_code
        parsed_failure: PublicFailure | None = None
        try:
            body = _bounded_body(response, MAX_JSON_BYTES, deadline)
            parsed_failure = parse_inventory_error(status_code, body)
        except _ResponseBodyError:
            pass
        if parsed_failure is not None:
            retry_after_seconds = (
                _parse_retry_after(response.headers.get('Retry-After'))
                if status_code == 429 or 500 <= status_code <= 599
                else None
            )
            return ApiFailure(parsed_failure, status_code, retry_after_seconds)
        if 300 <= status_code <= 399:
            kind = FailureKind.INVALID_RESPONSE
            retryable = False
        elif status_code in {401, 403}:
            kind = FailureKind.CREDENTIAL_REJECTED
            retryable = False
        elif status_code == 404:
            kind = FailureKind.INVENTORY_UNSUPPORTED
            retryable = False
        elif status_code == 429:
            kind = FailureKind.RATE_LIMITED
            retryable = True
        elif 500 <= status_code <= 599:
            kind = FailureKind.OFFLINE
            retryable = True
        elif 400 <= status_code <= 499:
            kind = FailureKind.INVENTORY_REJECTED
            retryable = False
        else:
            kind = FailureKind.INVALID_RESPONSE
            retryable = False
        retry_after_seconds = (
            _parse_retry_after(response.headers.get('Retry-After'))
            if status_code == 429 or 500 <= status_code <= 599
            else None
        )
        return _fixed_api_failure(
            kind,
            status_code=status_code,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )

    def _parse_json_response(
        self,
        response: requests.Response,
        parser: Callable[[object], _ResultT],
        deadline: _DeadlineGuard,
    ) -> _ResultT:
        if response.headers.get('Content-Type') != _JSON_CONTENT_TYPE:
            raise _fixed_api_failure(
                FailureKind.INVALID_RESPONSE,
                status_code=response.status_code,
            )
        body: bytes | None = None
        try:
            body = _bounded_body(response, MAX_JSON_BYTES, deadline)
        except _ResponseBodyError:
            pass
        if body is None:
            raise _fixed_api_failure(
                FailureKind.INVALID_RESPONSE,
                status_code=response.status_code,
            )

        parsed: _ResultT | None = None
        parse_failed = False
        try:
            deadline.check()
            text = body.decode('utf-8')
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_object_pairs,
                parse_constant=_reject_json_constant,
            )
            parsed = parser(value)
            deadline.check()
        except (ContractError, RecursionError, UnicodeDecodeError, ValueError):
            parse_failed = True
        if parse_failed:
            raise _fixed_api_failure(
                FailureKind.INVALID_RESPONSE,
                status_code=response.status_code,
            )
        return cast(_ResultT, parsed)

    @staticmethod
    def _validate_download_headers(
        response: requests.Response,
        *,
        expected_sha256: str,
        expected_byte_count: int,
        expected_revision_number: int,
        expected_filename: str,
    ) -> None:
        headers = response.headers
        invalid_response = (
            headers.get('Content-Type') != _PROFILE_CONTENT_TYPE
            or headers.get('Content-Disposition')
            != f'attachment; filename="{expected_filename}"'
            or headers.get('X-Revision-Number') != str(expected_revision_number)
            or headers.get('Transfer-Encoding') is not None
        )
        try:
            content_length = _strict_content_length(
                headers.get('Content-Length'),
                maximum=MAX_PROFILE_BYTES,
                required=True,
            )
        except _ResponseBodyError:
            invalid_response = True
            content_length = None
        if content_length != expected_byte_count:
            invalid_response = True
        if invalid_response:
            raise _fixed_api_failure(
                FailureKind.INVALID_RESPONSE,
                status_code=response.status_code,
            )

        if (
            headers.get('X-Content-SHA256') != expected_sha256
            or headers.get('X-Checksum-SHA256') != expected_sha256
            or headers.get('ETag') != f'"{expected_sha256}"'
        ):
            raise _fixed_api_failure(
                FailureKind.CHECKSUM_MISMATCH,
                status_code=response.status_code,
            )


type ClientFactory = Callable[[str, str], RoastServerClient]


def _deadline_watchdog(deadline: _DeadlineGuard) -> None:
    remaining = max(0.0, deadline.deadline - time.monotonic())
    if not deadline.wait(remaining):
        deadline.expire()


def _close_owned_session(session: requests.Session) -> None:
    adapters: tuple[BaseAdapter, ...] = ()
    try:
        adapters = tuple(session.adapters.values())
        session.adapters = {}
    except Exception:
        pass
    try:
        session.headers.clear()
    except Exception:
        pass
    try:
        session.proxies.clear()
    except Exception:
        pass
    try:
        params = session.params
        if isinstance(params, dict):
            params.clear()
        session.params = {}
    except Exception:
        pass
    try:
        session.hooks.clear()
    except Exception:
        pass
    try:
        session.cookies.clear()
    except Exception:
        pass
    try:
        session.auth = None
        session.cert = None
    except Exception:
        pass
    try:
        _SESSION_TYPE.close(session)
    except Exception:
        pass
    closed_adapter_ids: set[int] = set()
    for adapter in adapters:
        if id(adapter) in closed_adapter_ids:
            continue
        closed_adapter_ids.add(id(adapter))
        try:
            adapter.close()
        except Exception:
            pass


def _sanitize_session(
    session: requests.Session,
    *,
    replace_adapters: bool = False,
) -> None:
    failed = False
    if replace_adapters:
        inherited_adapters: tuple[BaseAdapter, ...] = ()
        try:
            inherited_adapters = tuple(session.adapters.values())
        except Exception:
            failed = True
        try:
            session.adapters = {}
        except Exception:
            failed = True
        closed_adapter_ids: set[int] = set()
        for adapter in inherited_adapters:
            if id(adapter) in closed_adapter_ids:
                continue
            closed_adapter_ids.add(id(adapter))
            try:
                adapter.close()
            except Exception:
                failed = True
        try:
            session.adapters = {
                'https://': HTTPAdapter(max_retries=0),
                'http://': HTTPAdapter(max_retries=0),
            }
        except Exception:
            failed = True
    try:
        vars(session).pop('request', None)
        vars(session).pop('send', None)
        vars(session).pop('close', None)
        session.trust_env = False
        session.proxies = {}
        session.auth = None
        session.cookies = RequestsCookieJar()
        session.params = {}
        session.hooks = {'response': []}
        session.headers = CaseInsensitiveDict(_FIXED_SESSION_HEADERS)
        session.cert = None
        session.verify = True
        session.stream = False
    except Exception:
        failed = True
    if failed:
        raise _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=None,
            code='request_error',
        )


def _request_api_failure(error: Exception) -> ApiFailure:
    if isinstance(error, requests.RequestException):
        return _transport_api_failure(error)
    return _fixed_api_failure(
        FailureKind.INVALID_RESPONSE,
        status_code=None,
        code='request_error',
    )


def _raw_header_values(response: requests.Response, name: str) -> tuple[str, ...] | None:
    raw_headers = getattr(response.raw, 'headers', None)
    if isinstance(raw_headers, _GetListHeaders):
        values = raw_headers.getlist(name)
    elif isinstance(raw_headers, _GetAllHeaders):
        values = raw_headers.get_all(name)
    else:
        return None
    if values is None:
        return ()
    if not isinstance(values, list | tuple) or not all(
        isinstance(value, str) for value in values
    ):
        raise _ResponseBodyError
    return tuple(cast(str, value) for value in values)


def _response_security_failure(response: requests.Response) -> ApiFailure | None:
    invalid = False
    try:
        for name in _SECURITY_RESPONSE_HEADERS:
            values = _raw_header_values(response, name)
            if values is not None and len(values) > 1:
                invalid = True
                break
        content_encoding = response.headers.get('Content-Encoding')
        if content_encoding is not None and content_encoding != 'identity':
            invalid = True
    except Exception:
        invalid = True
    if invalid:
        return _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=response.status_code,
        )
    return None


def _close_response(response: requests.Response) -> None:
    try:
        response.close()
    except Exception:
        pass


def _close_response_transport(response: requests.Response) -> None:
    raw = getattr(response, 'raw', None)
    if raw is not None:
        try:
            raw.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    _close_response(response)


def _fixed_api_failure(
    kind: FailureKind,
    *,
    status_code: int | None,
    code: str | None = None,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
) -> ApiFailure:
    return ApiFailure(
        PublicFailure(
            kind=kind,
            code=kind.value if code is None else code,
            message=FAILURE_MESSAGES[kind],
            retryable=retryable,
        ),
        status_code,
        retry_after_seconds,
    )


def _operation_timeout_failure() -> ApiFailure:
    return _fixed_api_failure(
        FailureKind.OFFLINE,
        status_code=None,
        code='timeout',
        retryable=True,
    )


def _transport_api_failure(error: requests.RequestException) -> ApiFailure:
    if isinstance(error, requests.exceptions.SSLError):
        code = 'tls_error'
    elif isinstance(error, requests.Timeout):
        code = 'timeout'
    elif isinstance(error, requests.ConnectionError):
        code = 'connection_error'
    else:
        return _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=None,
            code='request_error',
        )
    return _fixed_api_failure(
        FailureKind.OFFLINE,
        status_code=None,
        code=code,
        retryable=True,
    )


def _strict_content_length(
    value: str | None,
    *,
    maximum: int,
    required: bool,
) -> int | None:
    if value is None:
        if required:
            raise _ResponseBodyError
        return None
    if value == '' or not value.isascii() or not value.isdigit():
        raise _ResponseBodyError
    normalized = value.lstrip('0') or '0'
    if normalized != value:
        raise _ResponseBodyError
    if len(value) > len(str(maximum)):
        raise _ResponseBodyError
    parsed = int(value)
    if parsed > maximum:
        raise _ResponseBodyError
    return parsed


def _bounded_body(
    response: requests.Response,
    maximum: int,
    deadline: _DeadlineGuard,
) -> bytes:
    declared_length = _strict_content_length(
        response.headers.get('Content-Length'),
        maximum=maximum,
        required=False,
    )
    body = bytearray()
    stream_failure: ApiFailure | None = None
    body_error = False
    try:
        iterator = iter(response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES))
        while True:
            deadline.check()
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            deadline.check()
            if not isinstance(chunk, bytes):
                raise _ResponseBodyError
            if not chunk:
                continue
            if len(body) + len(chunk) > maximum:
                raise _ResponseBodyError
            body.extend(chunk)
    except ApiFailure as error:
        stream_failure = error
    except _ResponseBodyError:
        body_error = True
    except requests.RequestException as error:
        stream_failure = _transport_api_failure(error)
    except Exception:
        stream_failure = _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=response.status_code,
        )
    if body_error:
        raise _ResponseBodyError
    if stream_failure is not None:
        raise stream_failure
    if declared_length is not None and len(body) != declared_length:
        raise _ResponseBodyError
    deadline.check()
    return bytes(body)


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if text == '':
        return None
    if text.isascii() and text.isdigit():
        normalized = text.lstrip('0') or '0'
        if len(normalized) > len(str(MAX_RETRY_AFTER_SECONDS)):
            return MAX_RETRY_AFTER_SECONDS
        return min(int(normalized), MAX_RETRY_AFTER_SECONDS)

    try:
        retry_at = parsedate_to_datetime(text)
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            return None
        seconds = math.ceil((retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    except (OverflowError, TypeError, ValueError):
        return None
    return min(max(seconds, 0), MAX_RETRY_AFTER_SECONDS)


def _freeze_snapshot(snapshot: BinaryIO, deadline: _DeadlineGuard) -> bytes:
    snapshot_bytes: bytes | None = None
    try:
        deadline.check()
        snapshot.seek(0)
        deadline.check()
        value = snapshot.read(MAX_PROFILE_BYTES + 1)
        deadline.check()
        if len(value) <= MAX_PROFILE_BYTES:
            snapshot_bytes = bytes(value)
    except ApiFailure:
        raise
    except Exception:
        pass
    if snapshot_bytes is None:
        raise _fixed_api_failure(FailureKind.LOCAL_PROFILE, status_code=None)
    return snapshot_bytes


def _prepare_multipart_upload(
    roast_uuid: UUID,
    sha256: str,
    idempotency_key: str,
    metadata_json: bytes,
    snapshot_bytes: bytes,
    deadline: _DeadlineGuard,
) -> tuple[str, _DeadlineUploadBody]:
    deadline.check()
    boundary = f'artisan-{secrets.token_hex(16)}'
    boundary_bytes = boundary.encode('ascii')
    fields = (
        ('sha256', sha256.encode('ascii')),
        ('idempotency_key', idempotency_key.encode('utf-8')),
        ('metadata', metadata_json),
    )
    parts: list[bytes] = []
    for name, value in fields:
        deadline.check()
        parts.extend(
            (
                b'--' + boundary_bytes + b'\r\n',
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    'ascii'),
                value,
                b'\r\n',
            )
        )
    deadline.check()
    parts.extend(
        (
            b'--' + boundary_bytes + b'\r\n',
            (
                'Content-Disposition: form-data; name="profile"; '
                f'filename="{roast_uuid.hex}.alog"\r\n'
            ).encode('ascii'),
            f'Content-Type: {_PROFILE_CONTENT_TYPE}\r\n\r\n'.encode('ascii'),
            snapshot_bytes,
            b'\r\n',
            b'--' + boundary_bytes + b'--\r\n',
        )
    )
    content = b''.join(parts)
    deadline.check()
    if len(content) > MAX_PROFILE_BYTES + MAX_METADATA_BYTES + _MULTIPART_OVERHEAD_BYTES:
        raise _fixed_api_failure(FailureKind.LOCAL_PROFILE, status_code=None)
    return f'multipart/form-data; boundary={boundary}', _DeadlineUploadBody(
        content, deadline)


def _prepare_empty_destination(destination: BinaryIO) -> None:
    failed = False
    nonempty = False
    try:
        if not destination.seekable() or not destination.writable():
            raise _ResponseBodyError
        original_position = destination.tell()
        destination.seek(0, SEEK_END)
        nonempty = destination.tell() != 0
        if nonempty:
            destination.seek(original_position)
        else:
            destination.truncate(0)
            destination.seek(0)
    except Exception:
        failed = True
    if failed or nonempty:
        raise _fixed_api_failure(FailureKind.CACHE_CORRUPT, status_code=None)


def _stream_profile(
    response: requests.Response,
    destination: BinaryIO,
    *,
    expected_byte_count: int,
    deadline: _DeadlineGuard,
) -> tuple[int, str]:
    byte_count = 0
    digest = hashlib.sha256()
    stream_failure: ApiFailure | None = None
    try:
        iterator = iter(response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES))
        while True:
            deadline.check()
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            deadline.check()
            if not isinstance(chunk, bytes):
                raise _ResponseBodyError
            if not chunk:
                continue
            next_byte_count = byte_count + len(chunk)
            if (
                next_byte_count > MAX_PROFILE_BYTES
                or next_byte_count > expected_byte_count
            ):
                raise _ResponseBodyError
            written: int | None = None
            try:
                written = destination.write(chunk)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            if written != len(chunk):
                raise _fixed_api_failure(
                    FailureKind.CACHE_CORRUPT, status_code=None)
            byte_count = next_byte_count
            digest.update(chunk)
            deadline.check()
    except ApiFailure as error:
        stream_failure = error
    except requests.RequestException as error:
        stream_failure = _transport_api_failure(error)
    except _ResponseBodyError:
        stream_failure = _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=response.status_code,
        )
    except Exception:
        stream_failure = _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=response.status_code,
        )
    if stream_failure is not None:
        raise stream_failure
    if byte_count != expected_byte_count:
        raise _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=response.status_code,
        )
    deadline.check()
    return byte_count, digest.hexdigest()


def _rollback_destination(destination: BinaryIO) -> None:
    rollback_failed = False
    try:
        destination.seek(0)
        destination.truncate(0)
        destination.seek(0)
        destination.flush()
    except Exception:
        rollback_failed = True
    if rollback_failed:
        try:
            destination.close()
        except Exception:
            pass


def _finish_profile_destination(
    destination: BinaryIO,
    deadline: _DeadlineGuard,
) -> None:
    failed = False
    try:
        deadline.check()
        destination.flush()
        deadline.check()
        destination.seek(0)
        deadline.check()
    except ApiFailure:
        raise
    except Exception:
        failed = True
    if failed:
        raise _fixed_api_failure(FailureKind.CACHE_CORRUPT, status_code=None)


def _validate_inventory_command_request(request: object) -> None:
    from artisanlib.roastserver.inventory_contract import InventoryCommandRequest

    if type(request) is not InventoryCommandRequest:
        raise ValueError('invalid inventory command request')
    operation: object = request.operation
    reservation_uuid: object = request.reservation_uuid
    roast_uuid: object = request.roast_uuid
    lot_id: object = request.lot_id
    request_json: object = request.request_json
    idempotency_key: object = request.idempotency_key
    occurred_at: object = request.occurred_at
    client_instance_uuid: object = request.client_instance_uuid
    planned_grams: object = request.planned_grams
    requested_actual_grams: object = request.requested_actual_grams
    if (
        type(operation) is not str
        or operation not in {'reserve', 'finalize', 'release'}
        or type(reservation_uuid) is not UUID
        or type(client_instance_uuid) is not UUID
        or type(roast_uuid) is not UUID
        or type(lot_id) is not UUID
        or type(planned_grams) is not int
        or not 1 <= planned_grams <= POSTGRESQL_INTEGER_MAX
        or type(request_json) is not bytes
        or not 1 <= len(request_json) <= MAX_JSON_BYTES
        or type(idempotency_key) is not str
        or not isinstance(occurred_at, datetime)
        or occurred_at.tzinfo is None
    ):
        raise ValueError('invalid inventory command request')
    try:
        utc_offset = occurred_at.utcoffset()
    except (OverflowError, ValueError):
        raise ValueError('invalid inventory command request') from None
    if utc_offset != timedelta(0):
        raise ValueError('invalid inventory command request')
    if operation == 'finalize':
        if requested_actual_grams is not None and (
            type(requested_actual_grams) is not int
            or not 1 <= requested_actual_grams <= POSTGRESQL_INTEGER_MAX
        ):
            raise ValueError('invalid inventory command request')
    elif requested_actual_grams is not None:
        raise ValueError('invalid inventory command request')
    expected_idempotency_key = (
        f'inventory-v1:{client_instance_uuid.hex}:'
        f'{reservation_uuid.hex}:{operation}'
    )
    if idempotency_key != expected_idempotency_key:
        raise ValueError('invalid inventory command request')


def _require_bounded_bytes(value: object, *, maximum: int, name: str) -> None:
    if not isinstance(value, bytes) or not 1 <= len(value) <= maximum:
        raise ValueError(f'invalid {name}')


def _bounded_query_text(value: object, *, maximum: int, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or _has_prohibited_text_code_point(value)
    ):
        raise ValueError(f'invalid archive {name}')
    return value


def _utc_query_datetime(value: datetime | None, *, name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'invalid archive {name}')
    return value.astimezone(UTC).isoformat()


def _has_prohibited_text_code_point(value: str) -> bool:
    for char in value:
        code_point = ord(char)
        if (
            code_point == 0
            or code_point < 0x20
            or 0x7F <= code_point <= 0x9F
            or 0xD800 <= code_point <= 0xDFFF
        ):
            return True
    return False


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError('invalid JSON constant')


__all__ = [
    'ApiFailure',
    'CONNECT_TIMEOUT_SECONDS',
    'ClientFactory',
    'DownloadReceipt',
    'MAX_RETRY_AFTER_SECONDS',
    'OPERATION_DEADLINE_SECONDS',
    'READ_TIMEOUT_SECONDS',
    'RoastServerClient',
]
