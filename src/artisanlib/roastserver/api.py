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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import hmac
from io import SEEK_END
import json
import math
import re
from typing import BinaryIO, Final, NoReturn, Protocol, TypeVar, cast, override, runtime_checkable
from uuid import UUID

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
    PublicFailure,
    RevisionUpload,
    RoastDetail,
    RoastPage,
    ServerError,
    ServerIdentity,
    parse_aroast_ack,
    parse_error_envelope,
    parse_identity,
    parse_revision_upload,
    parse_roast_detail,
    parse_roast_page,
)
from artisanlib.roastserver.origin import canonical_origin

CONNECT_TIMEOUT_SECONDS: Final[float] = 4.0
READ_TIMEOUT_SECONDS: Final[float] = 10.0
MAX_RETRY_AFTER_SECONDS: Final[int] = 3600

_RESPONSE_CHUNK_BYTES: Final[int] = 64 * 1024
_JSON_CONTENT_TYPE: Final[str] = 'application/json'
_PROFILE_CONTENT_TYPE: Final[str] = 'application/x-artisan-profile'
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_ALLOWED_ROAST_STATES: Final[frozenset[str]] = frozenset(
    {'awaiting_profile', 'parsed', 'parse_failed'}
)
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
        session: requests.Session | None = None,
    ) -> None:
        if credential == '':
            raise ValueError('credential must not be empty')
        self._origin = canonical_origin(origin)
        self._credential = credential
        if session is not None and type(session) is not requests.Session:
            raise TypeError('session must be an exact requests.Session')
        self._session = requests.Session() if session is None else session
        _sanitize_session(self._session, replace_adapters=True)

    @override
    def __repr__(self) -> str:
        return '<RoastServerClient credential=<redacted>>'

    def test_connection(self) -> ServerIdentity:
        response = self._request('GET', '/api/v1/auth/me', stream=True)
        try:
            self._require_status(response, frozenset({200}))
            return self._parse_json_response(response, parse_identity)
        finally:
            _close_response(response)

    def post_aroast(self, roast_uuid: UUID, aroast_json: bytes) -> None:
        _require_bounded_bytes(aroast_json, maximum=MAX_JSON_BYTES, name='aroast JSON')
        response = self._request(
            'POST',
            '/api/v1/aroast',
            json_bytes=aroast_json,
            stream=True,
        )
        try:
            self._require_status(response, frozenset({200}))
            acknowledgement = self._parse_json_response(response, parse_aroast_ack)
            if acknowledgement.result.roast_id != roast_uuid:
                raise _fixed_api_failure(FailureKind.INVALID_RESPONSE, status_code=200)
        finally:
            _close_response(response)

    def upload_revision(
        self,
        roast_uuid: UUID,
        sha256: str,
        idempotency_key: str,
        metadata_json: bytes,
        snapshot: BinaryIO,
    ) -> RevisionUpload:
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
        snapshot_bytes = _freeze_snapshot(snapshot)
        snapshot_size = len(snapshot_bytes)
        snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
        if not hmac.compare_digest(snapshot_sha256, sha256):
            raise _fixed_api_failure(FailureKind.LOCAL_PROFILE, status_code=None)

        data: dict[str, str | bytes] = {
            'sha256': sha256,
            'idempotency_key': idempotency_key,
            'metadata': metadata_json,
        }
        files: dict[str, tuple[str, bytes, str]] = {
            'profile': (
                f'{roast_uuid.hex}.alog',
                snapshot_bytes,
                _PROFILE_CONTENT_TYPE,
            )
        }
        response = self._request(
            'POST',
            f'/api/v1/roasts/{roast_uuid.hex}/revisions',
            data=data,
            files=files,
            stream=True,
        )
        try:
            self._require_status(response, frozenset({200, 201}))
            result = self._parse_json_response(response, parse_revision_upload)
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

    def list_roasts(
        self,
        filters: ArchiveFilters,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RoastPage:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('invalid archive page limit')
        params: dict[str, str | int] = {'limit': limit}
        if cursor is not None:
            params['cursor'] = _bounded_query_text(
                cursor,
                maximum=MAX_CURSOR_CHARS,
                name='cursor',
            )
        if filters.search is not None:
            params['search'] = _bounded_query_text(
                filters.search,
                maximum=200,
                name='search',
            )
        if filters.state is not None:
            if filters.state not in _ALLOWED_ROAST_STATES:
                raise ValueError('invalid roast state')
            params['state'] = filters.state
        if filters.machine is not None:
            params['machine'] = _bounded_query_text(
                filters.machine,
                maximum=100,
                name='machine',
            )
        roast_at_from = _utc_query_datetime(filters.roast_at_from, name='roast_at_from')
        roast_at_to = _utc_query_datetime(filters.roast_at_to, name='roast_at_to')
        if roast_at_from is not None:
            params['roast_at_from'] = roast_at_from
        if roast_at_to is not None:
            params['roast_at_to'] = roast_at_to
        if (
            filters.roast_at_from is not None
            and filters.roast_at_to is not None
            and filters.roast_at_from > filters.roast_at_to
        ):
            raise ValueError('invalid archive date range')

        response = self._request(
            'GET',
            '/api/v1/roasts',
            params=params,
            stream=True,
        )
        try:
            self._require_status(response, frozenset({200}))
            return self._parse_json_response(response, parse_roast_page)
        finally:
            _close_response(response)

    def get_roast(self, roast_uuid: UUID) -> RoastDetail:
        response = self._request(
            'GET',
            f'/api/v1/roasts/{roast_uuid.hex}',
            stream=True,
        )
        try:
            self._require_status(response, frozenset({200}))
            detail = self._parse_json_response(response, parse_roast_detail)
            if detail.roast_uuid != roast_uuid:
                raise _fixed_api_failure(
                    FailureKind.INVALID_RESPONSE,
                    status_code=response.status_code,
                )
            return detail
        finally:
            _close_response(response)

    def download_revision(
        self,
        detail: RoastDetail,
        destination: BinaryIO,
    ) -> DownloadReceipt:
        """Download into an empty connector-owned, seekable, writable destination.

        The destination must support truncation. The caller must discard it whenever
        the download fails. If a failed commit cannot be rolled back, it is closed.
        """
        revision = detail.current_revision
        if revision is None:
            raise _fixed_api_failure(FailureKind.INVALID_RESPONSE, status_code=None)
        _prepare_empty_destination(destination)
        filename = f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog'
        path = (
            f'/api/v1/roasts/{detail.roast_uuid.hex}/revisions/'
            f'{revision.revision_number}/download'
        )
        response = self._request('GET', path, stream=True)
        try:
            self._require_status(response, frozenset({200}))
            self._validate_download_headers(
                response,
                expected_sha256=revision.sha256,
                expected_byte_count=revision.byte_size,
                expected_revision_number=revision.revision_number,
                expected_filename=filename,
            )
            profile_bytes, downloaded_sha256 = _stage_profile(
                response,
                expected_byte_count=revision.byte_size,
            )
            if not hmac.compare_digest(downloaded_sha256, revision.sha256):
                raise _fixed_api_failure(
                    FailureKind.CHECKSUM_MISMATCH,
                    status_code=response.status_code,
                )
            _commit_profile(destination, profile_bytes)
            return DownloadReceipt(
                roast_uuid=detail.roast_uuid,
                revision_number=revision.revision_number,
                sha256=downloaded_sha256,
                byte_count=len(profile_bytes),
                filename=filename,
            )
        finally:
            _close_response(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str | bytes] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        json_bytes: bytes | None = None,
        stream: bool = False,
    ) -> requests.Response:
        url = self._same_origin_url(path)
        headers = dict(_FIXED_SESSION_HEADERS)
        headers['Authorization'] = f'Bearer {self._credential}'
        request_data: Mapping[str, str | bytes] | bytes | None = data
        if json_bytes is not None:
            if data is not None or files is not None:
                raise ValueError('JSON cannot be combined with multipart data')
            headers['Content-Type'] = _JSON_CONTENT_TYPE
            request_data = json_bytes

        _sanitize_session(self._session)
        request_failure: ApiFailure | None = None
        response: requests.Response | None = None
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                data=request_data,
                files=files,
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

        sanitization_failure: ApiFailure | None = None
        try:
            _sanitize_session(self._session)
        except ApiFailure as error:
            sanitization_failure = error
        if sanitization_failure is not None:
            _close_response(response)
            raise sanitization_failure

        security_failure = _response_security_failure(response)
        if security_failure is not None:
            _close_response(response)
            raise security_failure
        return response

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

        server_error = _safe_server_error(response)
        if (
            server_error is not None
            and self._credential not in server_error.code
            and self._credential not in server_error.message
        ):
            code = server_error.code
            message = server_error.message
        else:
            code = kind.value
            message = FAILURE_MESSAGES[kind]
        retry_after_seconds = (
            _parse_retry_after(response.headers.get('Retry-After'))
            if status_code == 429 or 500 <= status_code <= 599
            else None
        )
        return ApiFailure(
            PublicFailure(
                kind=kind,
                code=code,
                message=message,
                retryable=retryable,
            ),
            status_code,
            retry_after_seconds,
        )

    def _parse_json_response(
        self,
        response: requests.Response,
        parser: Callable[[object], _ResultT],
    ) -> _ResultT:
        if response.headers.get('Content-Type') != _JSON_CONTENT_TYPE:
            raise _fixed_api_failure(
                FailureKind.INVALID_RESPONSE,
                status_code=response.status_code,
            )
        body: bytes | None = None
        try:
            body = _bounded_body(response, MAX_JSON_BYTES)
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
            text = body.decode('utf-8')
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_object_pairs,
                parse_constant=_reject_json_constant,
            )
            parsed = parser(value)
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


def _bounded_body(response: requests.Response, maximum: int) -> bytes:
    declared_length = _strict_content_length(
        response.headers.get('Content-Length'),
        maximum=maximum,
        required=False,
    )
    body = bytearray()
    stream_failure: ApiFailure | None = None
    body_error = False
    try:
        for chunk in response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not isinstance(chunk, bytes):
                raise _ResponseBodyError
            if not chunk:
                continue
            if len(body) + len(chunk) > maximum:
                raise _ResponseBodyError
            body.extend(chunk)
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
    return bytes(body)


def _safe_server_error(response: requests.Response) -> ServerError | None:
    try:
        body = _bounded_body(response, MAX_JSON_BYTES)
    except (ApiFailure, _ResponseBodyError):
        return None
    return parse_error_envelope(body)


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


def _freeze_snapshot(snapshot: BinaryIO) -> bytes:
    snapshot_bytes: bytes | None = None
    try:
        snapshot.seek(0)
        value = snapshot.read(MAX_PROFILE_BYTES + 1)
        if len(value) <= MAX_PROFILE_BYTES:
            snapshot_bytes = bytes(value)
    except Exception:
        pass
    if snapshot_bytes is None:
        raise _fixed_api_failure(FailureKind.LOCAL_PROFILE, status_code=None)
    return snapshot_bytes


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


def _stage_profile(
    response: requests.Response,
    *,
    expected_byte_count: int,
) -> tuple[bytes, str]:
    body = bytearray()
    digest = hashlib.sha256()
    stream_failure: ApiFailure | None = None
    try:
        for chunk in response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not isinstance(chunk, bytes):
                raise _ResponseBodyError
            if not chunk:
                continue
            next_byte_count = len(body) + len(chunk)
            if next_byte_count > MAX_PROFILE_BYTES or next_byte_count > expected_byte_count:
                raise _ResponseBodyError
            body.extend(chunk)
            digest.update(chunk)
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
    if len(body) != expected_byte_count:
        raise _fixed_api_failure(
            FailureKind.INVALID_RESPONSE,
            status_code=response.status_code,
        )
    return bytes(body), digest.hexdigest()


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


def _commit_profile(destination: BinaryIO, profile_bytes: bytes) -> None:
    failed = False
    try:
        written = destination.write(profile_bytes)
        if written != len(profile_bytes):
            failed = True
        else:
            destination.flush()
    except Exception:
        failed = True
    if failed:
        _rollback_destination(destination)
        raise _fixed_api_failure(FailureKind.CACHE_CORRUPT, status_code=None)


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
    'READ_TIMEOUT_SECONDS',
    'RoastServerClient',
]
