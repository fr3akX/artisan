from __future__ import annotations

import base64
import gzip
import hashlib
import inspect
import io
import json
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Buffer, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import BinaryIO, cast, override
from uuid import UUID, uuid4

import pytest
import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3._collections import HTTPHeaderDict

import artisanlib.roastserver.api as roastserver_api
from artisanlib.roastserver.api import ApiFailure, DownloadReceipt, RoastServerClient
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    ArchiveFilters,
    FailureKind,
    MAX_JSON_BYTES,
    MAX_METADATA_BYTES,
    MAX_PROFILE_BYTES,
    RoastDetail,
    parse_roast_detail,
)

from artisanlib.santoker_trace import CaptureConfig
from artisanlib.santoker_trace_contract import Json, Record
from artisanlib.santoker_trace_store import Destination, StoreError, TraceStore, UploadTicket, summary_record

ROAST_UUID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_ROAST_UUID = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
PROFILE_BYTES = b"{'roastUUID':'11111111111141118111111111111111','mode':'C'}"
SHA256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
IDEMPOTENCY_KEY = (
    'archive-v1:22222222-2222-4222-8222-222222222222:'
    f'{ROAST_UUID}:{SHA256}'
)


def valid_identity_payload() -> dict[str, object]:
    return {
        'user': {
            'id': '11111111-1111-4111-8111-111111111111',
            'email': 'owner@example.test',
            'nickname': 'Owner',
        },
        'organization': {
            'id': '22222222-2222-4222-8222-222222222222',
            'name': 'Roastery',
            'slug': 'roastery',
        },
        'role': 'admin',
    }


def valid_revision_payload(
    *,
    revision_number: int = 1,
    sha256: str = SHA256,
    byte_size: int = len(PROFILE_BYTES),
) -> dict[str, object]:
    return {
        'revision_number': revision_number,
        'sha256': sha256,
        'byte_size': byte_size,
        'parser_version': '2026.8.1',
        'parse_state': 'parsed',
        'parse_diagnostic_code': None,
        'parse_diagnostic_message': None,
        'uploaded_at': '2026-08-01T12:36:56.123456+00:00',
        'metadata': {},
        'reparse_recommended': False,
    }


def valid_roast_item_payload(*, roast_uuid: UUID = ROAST_UUID) -> dict[str, object]:
    return {
        'roast_uuid': roast_uuid.hex,
        'state': 'parsed',
        'roast_at': '2026-08-01T12:34:56Z',
        'title': 'Sample Roast',
        'batch_prefix': 'B',
        'batch_number': 12,
        'batch_position': 1,
        'operator': 'Owner',
        'machine': 'Test Drum',
        'machine_setup': '12 kg drum',
        'temperature_unit': 'C',
        'duration_seconds': 600,
        'green_weight_kg': 1.0,
        'roasted_weight_kg': 0.85,
        'revision_count': 1,
        'updated_at': '2026-08-01T12:35:56+00:00',
        'labels': [],
    }


def valid_roast_page_payload() -> dict[str, object]:
    return {'items': [valid_roast_item_payload()], 'next_cursor': None}


def valid_roast_detail_payload(
    *,
    roast_uuid: UUID = ROAST_UUID,
    sha256: str = SHA256,
    byte_size: int = len(PROFILE_BYTES),
) -> dict[str, object]:
    payload = valid_roast_item_payload(roast_uuid=roast_uuid)
    payload['current_metadata'] = {}
    payload['current_revision'] = valid_revision_payload(
        sha256=sha256,
        byte_size=byte_size,
    )
    payload['links'] = {
        'self': f'/api/v1/roasts/{roast_uuid.hex}',
        'chart': f'/api/v1/roasts/{roast_uuid.hex}/chart',
        'revisions': f'/api/v1/roasts/{roast_uuid.hex}/revisions',
    }
    return payload


def valid_upload_payload(
    *,
    roast_uuid: UUID = ROAST_UUID,
    sha256: str = SHA256,
) -> dict[str, object]:
    return {
        'roast_uuid': roast_uuid.hex,
        'state': 'parsed',
        'revision': valid_revision_payload(sha256=sha256),
        'links': {
            'roast': f'/api/v1/roasts/{roast_uuid.hex}',
            'chart': f'/api/v1/roasts/{roast_uuid.hex}/chart',
            'revisions': f'/api/v1/roasts/{roast_uuid.hex}/revisions',
            'download': f'/api/v1/roasts/{roast_uuid.hex}/revisions/1/download',
        },
    }


def valid_aroast_ack_payload(*, roast_uuid: UUID = ROAST_UUID) -> dict[str, object]:
    return {
        'success': True,
        'result': {
            'roast_id': roast_uuid.hex,
            'modified_at': '2026-08-01T12:37:56.123456Z',
        },
        'rlimit': 1000,
        'rusage': 5,
        'rremaining': 995,
    }


class FakeResponse(requests.Response):
    def __init__(
        self,
        status_code: int,
        chunks: list[bytes | requests.RequestException] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self._chunks = [] if chunks is None else list(chunks)
        self.closed_by_client = False
        self.requested_chunk_size: int | None = None

    @override
    def iter_content(
        self,
        chunk_size: int | None = 1,
        decode_unicode: bool = False,
    ) -> Iterator[bytes]:
        assert decode_unicode is False
        self.requested_chunk_size = chunk_size
        for chunk in self._chunks:
            if isinstance(chunk, requests.RequestException):
                raise chunk
            yield chunk

    @override
    def close(self) -> None:
        self.closed_by_client = True


def raw_response(
    status_code: int,
    body: bytes,
    headers: dict[str, str] | None = None,
    *,
    chunks: list[bytes | requests.RequestException] | None = None,
) -> FakeResponse:
    return FakeResponse(status_code, [body] if chunks is None else chunks, headers)


def json_response(
    status_code: int,
    payload: object,
    headers: dict[str, str] | None = None,
    *,
    chunks: list[bytes | requests.RequestException] | None = None,
) -> FakeResponse:
    body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    response_headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
    if headers is not None:
        response_headers.update(headers)
    return raw_response(status_code, body, response_headers, chunks=chunks)


@dataclass(frozen=True, slots=True)
class AdapterCall:
    request: requests.PreparedRequest = field(repr=False)
    stream: bool
    timeout: object
    verify: object
    cert: object
    proxies: Mapping[str, str] | None

    @property
    def method(self) -> str:
        return self.request.method or ''

    @property
    def url(self) -> str:
        return self.request.url or ''

    @property
    def headers(self) -> dict[str, str]:
        headers = dict(self.request.headers)
        if 'Authorization' in headers:
            headers['Authorization'] = 'Bearer <redacted>'
        return headers

    @property
    def data(self) -> object:
        return self.request.body


class RecordingAdapter(HTTPAdapter):
    def __init__(
        self,
        credential: str,
        outcomes: tuple[requests.Response | requests.RequestException, ...],
    ) -> None:
        super().__init__(max_retries=0)
        self._expected_authorization = f'Bearer {credential}'
        self._outcomes = iter(outcomes)
        self.calls: list[AdapterCall] = []

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: object = None,
        verify: object = True,
        cert: object = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        if not secrets.compare_digest(
            request.headers.get('Authorization', ''), self._expected_authorization
        ):
            raise AssertionError('missing or invalid authorization')
        self.calls.append(
            AdapterCall(
                request=request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        )
        try:
            outcome = next(self._outcomes)
        except StopIteration:
            raise AssertionError('unconfigured recording adapter request') from None
        if isinstance(outcome, requests.RequestException):
            raise outcome
        outcome.request = request
        outcome.url = request.url or ''
        return outcome


@dataclass(frozen=True, slots=True)
class RawOutcome:
    status_code: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


class HostileSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.request_calls = 0
        self.send_calls = 0

    @override
    def request(self, *args: object, **kwargs: object) -> requests.Response:
        self.request_calls += 1
        raise AssertionError('hostile Session.request called')

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        self.send_calls += 1
        raise AssertionError('hostile Session.send called')


class NoNetworkAdapter(HTTPAdapter):
    def __init__(self, outcomes: tuple[RawOutcome | Exception, ...]) -> None:
        super().__init__()
        self._outcomes = iter(outcomes)
        self.calls: list[AdapterCall] = []
        self.responses: list[requests.Response] = []
        self.close_calls = 0

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: object = None,
        verify: object = True,
        cert: object = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        self.calls.append(
            AdapterCall(
                request=request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        )
        try:
            outcome = next(self._outcomes)
        except StopIteration:
            raise AssertionError('unconfigured no-network adapter request') from None
        if isinstance(outcome, Exception):
            raise outcome
        headers = HTTPHeaderDict(outcome.headers)
        raw = urllib3.response.HTTPResponse(
            body=io.BytesIO(outcome.body),
            headers=headers,
            status=outcome.status_code,
            preload_content=False,
            decode_content=False,
            request_method=request.method,
            request_url=request.url,
        )
        response = self.build_response(request, raw)
        self.responses.append(response)
        return response

    @override
    def close(self) -> None:
        self.close_calls += 1
        super().close()


def _install_test_adapter(
    client: RoastServerClient,
    adapter: HTTPAdapter,
    *,
    prefix: str = 'https://',
) -> requests.Session:
    """Install a trusted no-socket adapter through a test-only private seam."""
    session = cast(requests.Session, vars(client)['_session'])
    replaced = session.get_adapter(prefix)
    session.mount(prefix, adapter)
    if replaced is not adapter:
        replaced.close()
    return session


def real_raw_outcome(
    status_code: int,
    body: bytes,
    headers: tuple[tuple[str, str], ...] = (),
) -> RawOutcome:
    return RawOutcome(status_code=status_code, body=body, headers=headers)


def real_json_outcome(
    status_code: int,
    payload: object,
    headers: tuple[tuple[str, str], ...] = (),
) -> RawOutcome:
    body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return real_raw_outcome(
        status_code,
        body,
        (
            ('Content-Type', 'application/json'),
            ('Content-Length', str(len(body))),
            *headers,
        ),
    )


def real_client(
    *outcomes: RawOutcome | Exception,
) -> tuple[RoastServerClient, requests.Session, NoNetworkAdapter, str]:
    credential = secrets.token_urlsafe(32)
    client = RoastServerClient('https://example.test', credential)
    adapter = NoNetworkAdapter(tuple(outcomes))
    session = _install_test_adapter(client, adapter)
    return client, session, adapter, credential


def multipart_body(call: AdapterCall) -> bytes:
    body = call.request.body
    if isinstance(body, bytes):
        return body
    assert hasattr(body, 'read') and hasattr(body, 'seek') and hasattr(body, 'tell')
    position = body.tell()
    body.seek(0)
    value = body.read()
    body.seek(position)
    assert isinstance(value, bytes)
    return value


def multipart_profile(call: AdapterCall) -> bytes:
    body = multipart_body(call)
    content_type = call.request.headers.get('Content-Type', '')
    assert content_type.startswith('multipart/form-data; boundary=')
    boundary = content_type.removeprefix('multipart/form-data; boundary=').encode('ascii')
    for part in body.split(b'--' + boundary):
        if b'name="profile"' not in part:
            continue
        _part_headers, separator, content = part.partition(b'\r\n\r\n')
        assert separator == b'\r\n\r\n'
        assert content.endswith(b'\r\n')
        return content[:-2]
    raise AssertionError('profile multipart field is absent')


class MutatingSnapshot(io.BytesIO):
    def __init__(self) -> None:
        super().__init__(PROFILE_BYTES)
        self.read_calls = 0

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        self.read_calls += 1
        if self.read_calls == 1:
            return PROFILE_BYTES
        if self.read_calls == 2:
            return b''
        return b'changed-after-validation'


class CountingSnapshot(io.BytesIO):
    def __init__(self) -> None:
        super().__init__(PROFILE_BYTES)
        self.read_calls = 0

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        self.read_calls += 1
        return super().read(size)


class OversizedSnapshot(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.requested_sizes: list[int | None] = []

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        self.requested_sizes.append(size)
        assert isinstance(size, int)
        return b'x' * size


class SnapshotReadFailure(io.BytesIO):
    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self._failure = failure

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        raise self._failure


class NonSeekableDestination(io.BytesIO):
    @override
    def seekable(self) -> bool:
        return False


class NonWritableDestination(io.BytesIO):
    @override
    def writable(self) -> bool:
        return False


class PartialWriteFailure(io.BytesIO):
    @override
    def write(self, data: Buffer, /) -> int:
        super().write(memoryview(data)[:7])
        raise OSError('/private/cache/archive.alog')


class RollbackFailureDestination(PartialWriteFailure):
    def __init__(self) -> None:
        super().__init__()
        self.truncate_calls = 0
        self.close_calls = 0

    @override
    def truncate(self, size: int | None = None, /) -> int:
        self.truncate_calls += 1
        if self.truncate_calls > 1:
            raise OSError('/private/cache/rollback-failed.alog')
        return super().truncate(size)

    @override
    def close(self) -> None:
        self.close_calls += 1
        super().close()


class NonTruncatableDestination(io.BytesIO):
    @override
    def truncate(self, size: int | None = None, /) -> int:
        raise OSError('truncate unsupported')


class RecordingWriteDestination(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.write_sizes: list[int] = []
        self.flush_calls = 0

    @override
    def write(self, data: Buffer, /) -> int:
        self.write_sizes.append(len(data))
        return super().write(data)

    @override
    def flush(self) -> None:
        self.flush_calls += 1
        super().flush()


class SlowDripResponse(FakeResponse):
    def __init__(
        self,
        body: bytes,
        delay_seconds: float,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            200,
            [bytes((value,)) for value in body],
            headers
            if headers is not None
            else {
                'Content-Type': 'application/json',
                'Content-Length': str(len(body)),
            },
        )
        self._delay_seconds = delay_seconds
        self._closed_event = threading.Event()
        self.close_calls = 0

    @override
    def iter_content(
        self,
        chunk_size: int | None = 1,
        decode_unicode: bool = False,
    ) -> Iterator[bytes]:
        assert decode_unicode is False
        self.requested_chunk_size = chunk_size
        for chunk in self._chunks:
            assert isinstance(chunk, bytes)
            if self._closed_event.wait(self._delay_seconds):
                return
            yield chunk

    @override
    def close(self) -> None:
        self.close_calls += 1
        self._closed_event.set()
        super().close()


class DeadlineBlockingAdapter(HTTPAdapter):
    def __init__(self, *, consume_upload: bool) -> None:
        super().__init__(max_retries=0)
        self.consume_upload = consume_upload
        self.calls: list[requests.PreparedRequest] = []
        self.close_calls = 0
        self.body_started = threading.Event()
        self._closed_event = threading.Event()

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: object = None,
        verify: object = True,
        cert: object = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        del stream, timeout, verify, cert, proxies
        self.calls.append(request)
        if self.consume_upload:
            body = request.body
            if not hasattr(body, 'read'):
                raise AssertionError('upload body is not deadline-guarded')
            chunk = body.read(7)
            assert isinstance(chunk, bytes) and chunk
            self.body_started.set()
        if not self._closed_event.wait(0.75):
            raise AssertionError('deadline did not close the adapter')
        raise requests.ConnectionError('released blocked transport diagnostic')

    @override
    def close(self) -> None:
        self.close_calls += 1
        self._closed_event.set()
        super().close()


type ClientFactory = Callable[
    [requests.Response | requests.RequestException],
    tuple[RoastServerClient, RecordingAdapter],
]


@pytest.fixture
def client_factory() -> ClientFactory:
    def make_client(
        outcome: requests.Response | requests.RequestException,
    ) -> tuple[RoastServerClient, RecordingAdapter]:
        credential = secrets.token_urlsafe(32)
        client = RoastServerClient('https://example.test', credential)
        adapter = RecordingAdapter(credential, (outcome,))
        _install_test_adapter(client, adapter)
        return client, adapter

    return make_client


def assert_invalid_response(raised: pytest.ExceptionInfo[ApiFailure]) -> None:
    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert raised.value.failure.retryable is False
    assert raised.value.status_code == 200


def detail_for_download(
    *,
    sha256: str = SHA256,
    byte_size: int = len(PROFILE_BYTES),
) -> RoastDetail:
    return parse_roast_detail(valid_roast_detail_payload(sha256=sha256, byte_size=byte_size))


def download_headers(
    *,
    sha256: str = SHA256,
    byte_size: int = len(PROFILE_BYTES),
) -> dict[str, str]:
    return {
        'Content-Type': 'application/x-artisan-profile',
        'Content-Length': str(byte_size),
        'Content-Disposition': f'attachment; filename="{ROAST_UUID.hex}-r1.alog"',
        'X-Content-SHA256': sha256,
        'X-Checksum-SHA256': sha256,
        'ETag': f'"{sha256}"',
        'X-Revision-Number': '1',
    }


def invoke_json_endpoint(client: RoastServerClient, endpoint: str) -> object:
    if endpoint == 'identity':
        return client.test_connection()
    if endpoint == 'aroast':
        client.post_aroast(ROAST_UUID, b'{}')
        return None
    if endpoint == 'list':
        return client.list_roasts(ArchiveFilters())
    if endpoint == 'detail':
        return client.get_roast(ROAST_UUID)
    if endpoint == 'upload':
        return client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'{}',
            io.BytesIO(PROFILE_BYTES),
        )
    raise AssertionError('unknown endpoint')


def invoke_closed_endpoint(client: RoastServerClient, endpoint: str) -> object:
    if endpoint == 'identity':
        return client.test_connection()
    if endpoint == 'aroast':
        client.post_aroast(ROAST_UUID, b'')
        return None
    if endpoint == 'list':
        return client.list_roasts(ArchiveFilters(), limit=0)
    if endpoint == 'detail':
        return client.get_roast(ROAST_UUID)
    if endpoint == 'upload':
        return client.upload_revision(
            ROAST_UUID,
            'invalid',
            '',
            b'',
            SnapshotReadFailure(AssertionError('closed client read snapshot')),
        )
    if endpoint == 'download':
        return client.download_revision(detail_for_download(), NonSeekableDestination())
    raise AssertionError('unknown endpoint')


def test_session_disables_proxy_inheritance_tls_bypass_and_redirects(
    client_factory: ClientFactory,
) -> None:
    client, adapter = client_factory(json_response(200, valid_identity_payload()))
    session = cast(requests.Session, vars(client)['_session'])

    client.test_connection()

    assert session.trust_env is False
    request = adapter.calls[0]
    assert request.verify is True
    assert request.timeout == (4.0, 10.0)
    assert request.method == 'GET'
    assert request.url == 'https://example.test/api/v1/auth/me'
    assert request.headers['Cache-Control'] == 'no-store'
    assert request.headers['User-Agent'].startswith('Artisan/')
    assert request.headers['Authorization'] == 'Bearer <redacted>'
    assert 'Authorization' not in session.headers


def test_redirect_is_rejected_without_followup(client_factory: ClientFactory) -> None:
    client, session = client_factory(
        raw_response(307, b'', {'Location': 'https://other.test'})
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert raised.value.failure.retryable is False
    assert raised.value.status_code == 307
    assert len(session.calls) == 1


def test_exact_json_endpoints_and_same_origin_paths(client_factory: ClientFactory) -> None:
    client, session = client_factory(json_response(200, valid_aroast_ack_payload()))

    client.post_aroast(ROAST_UUID, b'{"roast_id":"value"}')

    call = session.calls[0]
    assert call.method == 'POST'
    assert call.url == 'https://example.test/api/v1/aroast'
    assert call.data == b'{"roast_id":"value"}'
    assert call.headers['Content-Type'] == 'application/json'


def test_list_serializes_only_bounded_filters(client_factory: ClientFactory) -> None:
    client, session = client_factory(json_response(200, valid_roast_page_payload()))
    filters = ArchiveFilters(
        search='sample',
        state='parsed',
        machine='Test Drum',
        roast_at_from=datetime(2026, 8, 1, 14, 30, tzinfo=timezone(timedelta(hours=2))),
        roast_at_to=datetime(2026, 8, 2, 12, 30, tzinfo=UTC),
    )

    page = client.list_roasts(filters, cursor='opaque-cursor', limit=25)

    assert page.items[0].roast_uuid == ROAST_UUID
    assert session.calls[0].url == (
        'https://example.test/api/v1/roasts?limit=25&cursor=opaque-cursor&search=sample'
        '&state=parsed&machine=Test+Drum&roast_at_from=2026-08-01T12%3A30%3A00%2B00%3A00'
        '&roast_at_to=2026-08-02T12%3A30%3A00%2B00%3A00'
    )


@pytest.mark.parametrize(
    ('cursor', 'limit'),
    (
        ('', 50),
        ('x' * 513, 50),
        (None, 0),
        (None, 101),
    ),
)
def test_list_rejects_invalid_cursor_and_limit_without_request(
    client_factory: ClientFactory,
    cursor: str | None,
    limit: int,
) -> None:
    client, session = client_factory(json_response(200, valid_roast_page_payload()))

    with pytest.raises(ValueError):
        client.list_roasts(ArchiveFilters(), cursor=cursor, limit=limit)

    assert session.calls == []


def test_list_preserves_filter_whitespace_and_rejects_exact_filter_bounds_without_request(
    client_factory: ClientFactory,
) -> None:
    client, session = client_factory(json_response(200, valid_roast_page_payload()))
    client.list_roasts(ArchiveFilters(search=' sample ', machine=' Test Drum '))
    assert 'search=+sample+' in session.calls[0].url
    assert 'machine=+Test+Drum+' in session.calls[0].url

    invalid = (
        ArchiveFilters(search=''),
        ArchiveFilters(search='x' * 201),
        ArchiveFilters(state='unknown'),  # type: ignore[arg-type]
        ArchiveFilters(machine=''),
        ArchiveFilters(machine='x' * 101),
    )
    for filters in invalid:
        call_count = len(session.calls)
        with pytest.raises(ValueError):
            client.list_roasts(filters)
        assert len(session.calls) == call_count


def test_detail_requires_response_uuid_to_match_request(client_factory: ClientFactory) -> None:
    client, _session = client_factory(
        json_response(200, valid_roast_detail_payload(roast_uuid=OTHER_ROAST_UUID))
    )

    with pytest.raises(ApiFailure) as raised:
        client.get_roast(ROAST_UUID)

    assert_invalid_response(raised)


def test_aroast_requires_ack_uuid_to_match_request(client_factory: ClientFactory) -> None:
    client, _session = client_factory(
        json_response(200, valid_aroast_ack_payload(roast_uuid=OTHER_ROAST_UUID))
    )

    with pytest.raises(ApiFailure) as raised:
        client.post_aroast(ROAST_UUID, b'{}')

    assert_invalid_response(raised)


def test_upload_multipart_has_exact_fields_and_validates_current_hash_success(
    client_factory: ClientFactory,
) -> None:
    client, session = client_factory(json_response(200, valid_upload_payload()))
    snapshot = io.BytesIO(PROFILE_BYTES)

    result = client.upload_revision(
        ROAST_UUID,
        SHA256,
        IDEMPOTENCY_KEY,
        b'{"machine":"Test Drum"}',
        snapshot,
    )

    assert result.revision.sha256 == SHA256
    call = session.calls[0]
    assert call.method == 'POST'
    assert call.url == f'https://example.test/api/v1/roasts/{ROAST_UUID.hex}/revisions'
    body = multipart_body(call)
    assert call.headers['Content-Length'] == str(len(body))
    assert body.count(b'Content-Disposition: form-data; name=') == 4
    assert b'name="sha256"' in body
    assert SHA256.encode() in body
    assert b'name="idempotency_key"' in body
    assert IDEMPOTENCY_KEY.encode() in body
    assert b'name="metadata"' in body
    assert b'{"machine":"Test Drum"}' in body
    assert b'name="profile"' in body
    assert f'filename="{ROAST_UUID.hex}.alog"'.encode() in body
    assert multipart_profile(call) == PROFILE_BYTES
    assert call.headers['Content-Type'].startswith('multipart/form-data; boundary=')
    assert snapshot.closed is False


@pytest.mark.parametrize(
    'payload',
    (
        valid_upload_payload(roast_uuid=OTHER_ROAST_UUID),
        valid_upload_payload(sha256='a' * 64),
    ),
)
def test_upload_rejects_response_uuid_or_hash_mismatch(
    client_factory: ClientFactory,
    payload: dict[str, object],
) -> None:
    client, _session = client_factory(json_response(200, payload))

    with pytest.raises(ApiFailure) as raised:
        client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'{}',
            io.BytesIO(PROFILE_BYTES),
        )

    assert_invalid_response(raised)


def test_upload_rejects_response_link_mismatch(client_factory: ClientFactory) -> None:
    payload = valid_upload_payload()
    links = payload['links']
    assert isinstance(links, dict)
    links['download'] = f'/api/v1/roasts/{ROAST_UUID.hex}/revisions/9/download'
    client, _session = client_factory(json_response(200, payload))

    with pytest.raises(ApiFailure) as raised:
        client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'{}',
            io.BytesIO(PROFILE_BYTES),
        )

    assert_invalid_response(raised)


def test_upload_rejects_changed_snapshot_and_oversized_metadata_before_request(
    client_factory: ClientFactory,
) -> None:
    client, session = client_factory(json_response(200, valid_upload_payload()))

    with pytest.raises(ApiFailure) as changed:
        client.upload_revision(
            ROAST_UUID,
            'a' * 64,
            IDEMPOTENCY_KEY,
            b'{}',
            io.BytesIO(PROFILE_BYTES),
        )
    assert changed.value.failure.kind is FailureKind.LOCAL_PROFILE
    assert changed.value.status_code is None

    with pytest.raises(ValueError):
        client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'x' * (MAX_METADATA_BYTES + 1),
            io.BytesIO(PROFILE_BYTES),
        )

    assert session.calls == []


@pytest.mark.parametrize('endpoint', ('identity', 'aroast', 'list', 'detail', 'upload'))
def test_all_json_success_responses_are_bounded_before_parsing(
    client_factory: ClientFactory,
    endpoint: str,
) -> None:
    response = raw_response(
        200,
        b'',
        {'Content-Type': 'application/json'},
        chunks=[b'{}', b'x' * MAX_JSON_BYTES],
    )
    client, _session = client_factory(response)

    with pytest.raises(ApiFailure) as raised:
        invoke_json_endpoint(client, endpoint)

    assert_invalid_response(raised)
    assert response.closed_by_client is True


@pytest.mark.parametrize('declared_delta', (-1, 1))
def test_json_rejects_content_length_lies(
    client_factory: ClientFactory,
    declared_delta: int,
) -> None:
    response = json_response(200, valid_identity_payload())
    actual_length = int(response.headers['Content-Length'])
    response.headers['Content-Length'] = str(actual_length + declared_delta)
    client, _session = client_factory(response)

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert_invalid_response(raised)


@pytest.mark.parametrize('content_type', ('text/html', 'application/json; charset=utf-8', ''))
def test_json_requires_exact_content_type(
    client_factory: ClientFactory,
    content_type: str,
) -> None:
    response = json_response(200, valid_identity_payload(), {'Content-Type': content_type})
    client, _session = client_factory(response)

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert_invalid_response(raised)


def test_successful_json_rejects_arbitrary_html_without_exposing_it(
    client_factory: ClientFactory,
) -> None:
    body = b'<html>proxy diagnostic and infrastructure details</html>'
    client, _session = client_factory(
        raw_response(
            200,
            body,
            {'Content-Type': 'application/json', 'Content-Length': str(len(body))},
        )
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert_invalid_response(raised)
    assert 'proxy diagnostic' not in str(raised.value)
    assert 'proxy diagnostic' not in repr(raised.value)


def test_download_validates_headers_then_streams_and_hashes_to_caller_destination(
    client_factory: ClientFactory,
) -> None:
    headers = download_headers()
    response = raw_response(
        200,
        b'',
        headers,
        chunks=[PROFILE_BYTES[:11], b'', PROFILE_BYTES[11:]],
    )
    client, session = client_factory(response)
    destination = io.BytesIO()

    receipt = client.download_revision(detail_for_download(), destination)

    assert receipt == DownloadReceipt(
        roast_uuid=ROAST_UUID,
        revision_number=1,
        sha256=SHA256,
        byte_count=len(PROFILE_BYTES),
        filename=f'{ROAST_UUID.hex}-r1.alog',
    )
    assert destination.getvalue() == PROFILE_BYTES
    assert destination.tell() == 0
    assert destination.closed is False
    assert response.closed_by_client is True
    assert response.requested_chunk_size == 64 * 1024
    call = session.calls[0]
    assert call.method == 'GET'
    assert call.url == (
        f'https://example.test/api/v1/roasts/{ROAST_UUID.hex}/revisions/1/download'
    )
    assert call.stream is True


@pytest.mark.parametrize(
    ('header', 'value', 'kind'),
    (
        ('Content-Type', 'application/octet-stream', FailureKind.INVALID_RESPONSE),
        ('Content-Type', 'application/x-artisan-profile; charset=utf-8', FailureKind.INVALID_RESPONSE),
        ('Content-Length', '01', FailureKind.INVALID_RESPONSE),
        ('Content-Length', str(len(PROFILE_BYTES) + 1), FailureKind.INVALID_RESPONSE),
        ('Content-Disposition', 'attachment; filename="other.alog"', FailureKind.INVALID_RESPONSE),
        ('X-Revision-Number', '01', FailureKind.INVALID_RESPONSE),
        ('X-Revision-Number', '2', FailureKind.INVALID_RESPONSE),
        ('X-Content-SHA256', 'a' * 64, FailureKind.CHECKSUM_MISMATCH),
        ('X-Checksum-SHA256', 'a' * 64, FailureKind.CHECKSUM_MISMATCH),
        ('ETag', f'W/"{SHA256}"', FailureKind.CHECKSUM_MISMATCH),
        ('ETag', '"' + 'a' * 64 + '"', FailureKind.CHECKSUM_MISMATCH),
    ),
)
def test_download_rejects_each_inexact_required_header_before_writing(
    client_factory: ClientFactory,
    header: str,
    value: str,
    kind: FailureKind,
) -> None:
    headers = download_headers()
    headers[header] = value
    client, _session = client_factory(raw_response(200, PROFILE_BYTES, headers))
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is kind
    assert raised.value.failure.retryable is False
    assert destination.getvalue() == b''


@pytest.mark.parametrize(
    'missing_header',
    (
        'Content-Type',
        'Content-Length',
        'Content-Disposition',
        'X-Content-SHA256',
        'X-Checksum-SHA256',
        'ETag',
        'X-Revision-Number',
    ),
)
def test_download_requires_every_pinned_header(
    client_factory: ClientFactory,
    missing_header: str,
) -> None:
    headers = download_headers()
    del headers[missing_header]
    client, _session = client_factory(raw_response(200, PROFILE_BYTES, headers))

    with pytest.raises(ApiFailure):
        client.download_revision(detail_for_download(), io.BytesIO())


@pytest.mark.parametrize('body', (PROFILE_BYTES[:-1], PROFILE_BYTES + b'x'))
def test_download_rejects_short_and_long_streams_without_touching_destination(
    client_factory: ClientFactory,
    body: bytes,
) -> None:
    client, _session = client_factory(raw_response(200, body, download_headers()))
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert destination.getvalue() == b''
    assert destination.tell() == 0


def test_download_rejects_streamed_sha256_mismatch_without_committing(
    client_factory: ClientFactory,
) -> None:
    changed = bytes([PROFILE_BYTES[0] ^ 1]) + PROFILE_BYTES[1:]
    client, _session = client_factory(raw_response(200, changed, download_headers()))
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.CHECKSUM_MISMATCH
    assert destination.getvalue() == b''
    assert destination.tell() == 0


def test_download_stops_before_a_chunk_can_exceed_profile_ceiling(
    client_factory: ClientFactory,
) -> None:
    expected_sha256 = 'a' * 64
    headers = download_headers(sha256=expected_sha256, byte_size=MAX_PROFILE_BYTES)
    response = raw_response(
        200,
        b'',
        headers,
        chunks=[b'x' * (MAX_PROFILE_BYTES + 1)],
    )
    client, _session = client_factory(response)
    detail = detail_for_download(sha256=expected_sha256, byte_size=MAX_PROFILE_BYTES)
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail, destination)

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert destination.getvalue() == b''


@pytest.mark.parametrize(
    ('exception_type', 'expected_code'),
    (
        (requests.ConnectionError, 'connection_error'),
        (requests.Timeout, 'timeout'),
        (requests.exceptions.SSLError, 'tls_error'),
    ),
)
def test_transport_failures_are_safe_and_retryable(
    client_factory: ClientFactory,
    exception_type: type[requests.RequestException],
    expected_code: str,
) -> None:
    client, _session = client_factory(exception_type('arbitrary upstream diagnostics'))

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == expected_code
    assert raised.value.failure.retryable is True
    assert raised.value.status_code is None
    assert raised.value.retry_after_seconds is None
    assert 'upstream' not in str(raised.value)
    assert 'upstream' not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ('status_code', 'kind', 'retryable'),
    (
        (401, FailureKind.CREDENTIAL_REJECTED, False),
        (429, FailureKind.RATE_LIMITED, True),
        (500, FailureKind.OFFLINE, True),
        (599, FailureKind.OFFLINE, True),
        (400, FailureKind.PROFILE_REJECTED, False),
        (404, FailureKind.PROFILE_REJECTED, False),
        (499, FailureKind.PROFILE_REJECTED, False),
    ),
)
def test_http_status_retry_and_pause_classification(
    client_factory: ClientFactory,
    status_code: int,
    kind: FailureKind,
    retryable: bool,
) -> None:
    body: dict[str, object] = {
        'error': {
            'code': 'server_controlled_code',
            'message': '<b>Server-controlled diagnostic.</b>',
            'details': {'private': 'infrastructure detail'},
        }
    }
    client, _session = client_factory(json_response(status_code, body))

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is kind
    assert raised.value.failure.retryable is retryable
    assert raised.value.failure.code == kind.value
    assert raised.value.failure.message == FAILURE_MESSAGES[kind]
    assert raised.value.status_code == status_code
    rendered = f'{raised.value!s}\n{raised.value!r}\n{raised.value.failure!r}'
    assert 'server_controlled' not in rendered
    assert '<b>' not in rendered
    assert 'infrastructure' not in rendered


def test_server_error_envelope_never_retains_transformed_credentials_or_details() -> None:
    credential = secrets.token_urlsafe(48)
    transformed = (
        base64.urlsafe_b64encode(credential.encode()).decode(),
        hashlib.sha256(credential.encode()).hexdigest(),
        ''.join(f'&#{ord(char)};' for char in credential[:8]),
    )
    body = {
        'error': {
            'code': transformed[0],
            'message': f'{transformed[1]} <img src=x> {transformed[2]}',
            'details': {'diagnostic': transformed},
        }
    }
    client = RoastServerClient('https://example.test', credential)
    adapter = RecordingAdapter(credential, (json_response(400, body),))
    _install_test_adapter(client, adapter)

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.code == FailureKind.PROFILE_REJECTED.value
    assert raised.value.failure.message == FAILURE_MESSAGES[FailureKind.PROFILE_REJECTED]
    rendered = f'{raised.value!s}\n{raised.value!r}\n{raised.value.failure!r}'
    assert all(value not in rendered for value in transformed)
    assert '<img' not in rendered
    assert 'diagnostic' not in rendered


@pytest.mark.parametrize(
    ('value', 'expected'),
    (
        ('0', 0),
        ('120', 120),
        ('999999999999999999999999', 3600),
        ('-1', None),
        ('1.5', None),
        ('soon', None),
        ('', None),
    ),
)
def test_retry_after_delta_seconds_are_bounded_or_ignored(
    client_factory: ClientFactory,
    value: str,
    expected: int | None,
) -> None:
    client, _session = client_factory(
        raw_response(429, b'', {'Retry-After': value})
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.retry_after_seconds == expected


@pytest.mark.parametrize(
    ('when', 'expected'),
    (
        (datetime.now(UTC) - timedelta(days=1), 0),
        (datetime.now(UTC) + timedelta(days=1), 3600),
    ),
)
def test_retry_after_rfc_dates_are_parsed_and_clamped(
    client_factory: ClientFactory,
    when: datetime,
    expected: int,
) -> None:
    client, _session = client_factory(
        raw_response(503, b'', {'Retry-After': format_datetime(when, usegmt=True)})
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.retry_after_seconds == expected


def test_arbitrary_error_body_and_authorization_never_reach_logs_or_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = secrets.token_urlsafe(48)
    body = f'<html>{credential} infrastructure diagnostic</html>'.encode()
    client = RoastServerClient('https://example.test', credential)
    adapter = RecordingAdapter(
        credential,
        (raw_response(502, body, {'Content-Length': str(len(body))}),),
    )
    _install_test_adapter(client, adapter)

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    rendered = '\n'.join(
        (
            str(raised.value),
            repr(raised.value),
            repr(raised.value.failure),
            repr(client),
            repr(adapter),
            caplog.text,
        )
    )
    assert credential not in rendered
    assert 'infrastructure diagnostic' not in rendered
    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == FailureKind.OFFLINE.value


def test_public_constructor_has_no_transport_injection_parameter() -> None:
    assert tuple(inspect.signature(RoastServerClient).parameters) == ('origin', 'credential')


def test_session_factory_subclasses_overriding_request_and_send_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = HostileSession()
    monkeypatch.setattr(requests, 'Session', lambda: session)

    with pytest.raises(TypeError, match='exact requests.Session'):
        RoastServerClient('https://example.test', secrets.token_urlsafe(32))

    assert session.request_calls == 0
    assert session.send_calls == 0


def test_hostile_factory_adapter_is_closed_removed_and_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.sessions.Session()
    inherited_adapters = tuple(session.adapters.values())
    hostile_adapter = NoNetworkAdapter((real_json_outcome(200, valid_identity_payload()),))
    session.mount('https://example.test/', hostile_adapter)
    monkeypatch.setattr(requests, 'Session', lambda: session)
    credential = secrets.token_urlsafe(32)

    client = RoastServerClient('https://example.test', credential)

    assert hostile_adapter.close_calls == 1
    assert hostile_adapter.calls == []
    assert set(session.adapters) == {'https://', 'http://'}
    sanitized_adapters = tuple(session.adapters.values())
    assert all(type(adapter) is HTTPAdapter for adapter in sanitized_adapters)
    http_adapters = tuple(cast(HTTPAdapter, adapter) for adapter in sanitized_adapters)
    assert all(adapter.max_retries.total == 0 for adapter in http_adapters)
    assert all(adapter not in inherited_adapters for adapter in http_adapters)
    assert hostile_adapter not in http_adapters

    trusted_adapter = NoNetworkAdapter((real_json_outcome(200, valid_identity_payload()),))
    _install_test_adapter(client, trusted_adapter)
    client.test_connection()

    assert hostile_adapter.calls == []
    assert len(trusted_adapter.calls) == 1


def test_loopback_http_uses_no_retry_default_without_environment_proxies() -> None:
    client = RoastServerClient('http://127.0.0.1:8000', secrets.token_urlsafe(32))
    session = cast(requests.Session, vars(client)['_session'])

    default_http_adapter = session.adapters['http://']
    assert type(default_http_adapter) is HTTPAdapter
    assert default_http_adapter.max_retries.total == 0

    trusted_adapter = NoNetworkAdapter((real_json_outcome(200, valid_identity_payload()),))
    _install_test_adapter(client, trusted_adapter, prefix='http://')
    identity = client.test_connection()

    assert identity.user.id == ROAST_UUID
    assert trusted_adapter.calls[0].request.url == 'http://127.0.0.1:8000/api/v1/auth/me'
    assert trusted_adapter.calls[0].proxies == {}


def test_factory_session_is_fully_sanitized_and_prepared_request_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_calls: list[str] = []

    def hostile_hook(response: requests.Response, **_kwargs: object) -> requests.Response:
        hook_calls.append('called')
        return response

    session = requests.sessions.Session()
    session.trust_env = True
    session.proxies = {'https': 'https://proxy.invalid'}
    vars(session)['auth'] = ('hostile-user', 'hostile-password')
    session.cookies.set('session', 'hostile-cookie')
    session.params = {'injected': 'query'}
    session.hooks = {'response': [hostile_hook]}
    session.headers.update(
        {
            'Authorization': 'Basic hostile-default',
            'Cookie': 'literal-hostile-cookie',
            'X-Hostile-Default': 'present',
        }
    )
    vars(session)['cert'] = '/private/client-certificate.pem'
    monkeypatch.setattr(requests, 'Session', lambda: session)
    client, sanitized, adapter, credential = real_client(
        real_json_outcome(200, valid_identity_payload()),
    )

    client.test_connection()

    assert sanitized is session
    assert session.trust_env is False
    assert session.proxies == {}
    assert session.auth is None
    assert len(session.cookies) == 0
    assert session.params == {}
    assert session.hooks == {'response': []}
    assert session.cert is None
    assert session.verify is True
    session_user_agent = session.headers.get('User-Agent')
    assert isinstance(session_user_agent, str)
    assert session.headers == {
        'Accept-Encoding': 'identity',
        'Cache-Control': 'no-store',
        'User-Agent': session_user_agent,
    }
    assert session_user_agent.startswith('Artisan/')
    assert hook_calls == []
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call.request.url == 'https://example.test/api/v1/auth/me'
    prepared_user_agent = call.request.headers.get('User-Agent')
    assert isinstance(prepared_user_agent, str)
    assert call.request.headers == {
        'Accept-Encoding': 'identity',
        'Authorization': f'Bearer {credential}',
        'Cache-Control': 'no-store',
        'User-Agent': prepared_user_agent,
    }
    assert call.timeout == (4.0, 10.0)
    assert call.verify is True
    assert call.cert is None
    assert call.proxies == {}
    assert call.stream is True
    assert adapter.responses[0].raw.closed


def test_context_manager_closes_owned_session_and_adapters_once_and_wipes_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_close_calls: list[requests.Session] = []
    original_session_close = requests.sessions.Session.close

    def record_session_close(session: requests.Session) -> None:
        session_close_calls.append(session)
        original_session_close(session)

    monkeypatch.setattr(requests.sessions.Session, 'close', record_session_close)
    credential = secrets.token_urlsafe(48)
    client = RoastServerClient('https://example.test', credential)
    adapter = NoNetworkAdapter((real_json_outcome(200, valid_identity_payload()),))
    session = _install_test_adapter(client, adapter)

    with client as entered:
        assert entered is client
        assert entered.test_connection().user.id == ROAST_UUID

    assert session_close_calls == [session]
    assert adapter.close_calls == 1
    assert vars(client)['_closed'] is True
    assert vars(client)['_credential'] == ''
    assert vars(client)['_session'] is None
    assert session.headers == {}
    assert len(session.cookies) == 0
    assert session.adapters == {}

    client.close()

    assert session_close_calls == [session]
    assert adapter.close_calls == 1
    assert credential not in repr(client)


def test_context_manager_closes_without_suppressing_exceptions() -> None:
    client = RoastServerClient('https://example.test', secrets.token_urlsafe(32))

    with pytest.raises(RuntimeError, match='context marker'), client:
        raise RuntimeError('context marker')

    assert vars(client)['_closed'] is True


@pytest.mark.parametrize(
    'endpoint',
    ('identity', 'aroast', 'list', 'detail', 'upload', 'download'),
)
def test_every_endpoint_after_close_raises_same_fixed_unchained_failure(
    endpoint: str,
) -> None:
    credential = secrets.token_urlsafe(48)
    client = RoastServerClient('https://example.test', credential)
    client.close()

    with pytest.raises(ApiFailure) as raised:
        invoke_closed_endpoint(client, endpoint)

    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == 'client_closed'
    assert raised.value.failure.retryable is False
    assert raised.value.status_code is None
    assert raised.value.retry_after_seconds is None
    assert credential not in str(raised.value)
    assert credential not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_enter_after_close_raises_fixed_unchained_failure() -> None:
    client = RoastServerClient('https://example.test', secrets.token_urlsafe(32))
    client.close()

    with pytest.raises(ApiFailure) as raised:
        client.__enter__()

    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == 'client_closed'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_real_session_redirect_is_one_adapter_call_and_response_is_closed() -> None:
    client, _session, adapter, _credential = real_client(
        real_raw_outcome(
            307,
            b'',
            (
                ('Content-Length', '0'),
                ('Location', 'https://other.test/stolen'),
            ),
        )
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert len(adapter.calls) == 1
    assert adapter.responses[0].raw.closed


def test_operation_deadline_aborts_slow_drip_and_permanently_closes_client(
    monkeypatch: pytest.MonkeyPatch,
    client_factory: ClientFactory,
) -> None:
    monkeypatch.setattr(roastserver_api, 'OPERATION_DEADLINE_SECONDS', 0.08)
    body = json.dumps(valid_identity_payload(), separators=(',', ':')).encode()
    response = SlowDripResponse(body, 0.03)
    client, recording_adapter = client_factory(response)

    started = time.monotonic()
    with pytest.raises(ApiFailure) as raised:
        client.test_connection()
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == 'timeout'
    assert raised.value.failure.message == FAILURE_MESSAGES[FailureKind.OFFLINE]
    assert raised.value.failure.retryable is True
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert response.closed_by_client is True
    assert response.close_calls >= 1
    assert len(recording_adapter.calls) == 1
    assert vars(client)['_closed'] is True
    assert vars(client)['_credential'] == ''
    assert vars(client)['_session'] is None

    with pytest.raises(ApiFailure) as closed:
        client.test_connection()
    assert closed.value.failure.code == 'client_closed'
    assert not any(
        thread.name == 'RoastServerDeadlineWatchdog' and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_download_deadline_rolls_back_streamed_prefix_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
    client_factory: ClientFactory,
) -> None:
    monkeypatch.setattr(roastserver_api, 'OPERATION_DEADLINE_SECONDS', 0.08)
    response = SlowDripResponse(PROFILE_BYTES, 0.03, download_headers())
    client, adapter = client_factory(response)
    destination = io.BytesIO()

    started = time.monotonic()
    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert raised.value.failure.code == 'timeout'
    assert raised.value.failure.message == FAILURE_MESSAGES[FailureKind.OFFLINE]
    assert destination.getvalue() == b''
    assert destination.tell() == 0
    assert response.closed_by_client is True
    assert response.close_calls >= 1
    assert len(adapter.calls) == 1
    assert vars(client)['_closed'] is True


@pytest.mark.parametrize('operation', ('headers', 'upload'))
def test_operation_deadline_closes_adapter_to_release_blocked_transport(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setattr(roastserver_api, 'OPERATION_DEADLINE_SECONDS', 0.08)
    client = RoastServerClient('https://example.test', secrets.token_urlsafe(32))
    adapter = DeadlineBlockingAdapter(consume_upload=operation == 'upload')
    _install_test_adapter(client, adapter)

    started = time.monotonic()
    with pytest.raises(ApiFailure) as raised:
        if operation == 'upload':
            client.upload_revision(
                ROAST_UUID,
                SHA256,
                IDEMPOTENCY_KEY,
                b'{}',
                io.BytesIO(PROFILE_BYTES),
            )
        else:
            client.test_connection()
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == 'timeout'
    assert raised.value.failure.message == FAILURE_MESSAGES[FailureKind.OFFLINE]
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert len(adapter.calls) == 1
    assert adapter.close_calls == 1
    assert adapter.body_started.is_set() is (operation == 'upload')
    assert vars(client)['_closed'] is True
    assert vars(client)['_credential'] == ''
    assert vars(client)['_session'] is None


def test_upload_freezes_one_bounded_caller_read_before_real_multipart_preparation() -> None:
    snapshot = MutatingSnapshot()
    client, _session, adapter, _credential = real_client(
        real_json_outcome(200, valid_upload_payload())
    )

    result = client.upload_revision(
        ROAST_UUID,
        SHA256,
        IDEMPOTENCY_KEY,
        b'{"machine":"Test Drum"}',
        snapshot,
    )

    assert result.revision.sha256 == SHA256
    assert snapshot.read_calls == 1
    call = adapter.calls[0]
    assert multipart_profile(call) == PROFILE_BYTES
    body = multipart_body(call)
    assert len(body) <= MAX_PROFILE_BYTES + MAX_METADATA_BYTES + 4096
    assert body.count(b'Content-Disposition: form-data; name=') == 4
    assert b'name="sha256"' in body
    assert b'name="idempotency_key"' in body
    assert b'name="metadata"' in body
    assert b'name="profile"' in body
    assert call.timeout == (4.0, 10.0)
    assert call.verify is True
    assert call.stream is True
    assert adapter.responses[0].raw.closed


def test_upload_retry_after_prepared_failure_reads_once_per_call_and_sends_same_snapshot() -> None:
    snapshot = CountingSnapshot()
    client, _session, adapter, _credential = real_client(
        requests.ConnectionError('failure after preparation'),
        real_json_outcome(200, valid_upload_payload()),
    )

    with pytest.raises(ApiFailure):
        client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'{}',
            snapshot,
        )
    result = client.upload_revision(
        ROAST_UUID,
        SHA256,
        IDEMPOTENCY_KEY,
        b'{}',
        snapshot,
    )

    assert result.revision.sha256 == SHA256
    assert snapshot.read_calls == 2
    assert len(adapter.calls) == 2
    assert multipart_profile(adapter.calls[0]) == PROFILE_BYTES
    assert multipart_profile(adapter.calls[1]) == PROFILE_BYTES


def test_upload_oversize_probe_is_one_bounded_read_and_sends_nothing() -> None:
    snapshot = OversizedSnapshot()
    client, _session, adapter, _credential = real_client(
        real_json_outcome(200, valid_upload_payload())
    )

    with pytest.raises(ApiFailure) as raised:
        client.upload_revision(
            ROAST_UUID,
            'a' * 64,
            IDEMPOTENCY_KEY,
            b'{}',
            snapshot,
        )

    assert raised.value.failure.kind is FailureKind.LOCAL_PROFILE
    assert snapshot.requested_sizes == [MAX_PROFILE_BYTES + 1]
    assert adapter.calls == []


@pytest.mark.parametrize(
    'failure',
    (
        OSError('/private/customer/profile.alog'),
        UnicodeEncodeError('utf-8', '/private/customer/profile.alog', 0, 1, 'invalid'),
        RuntimeError('/private/customer/profile.alog'),
    ),
)
def test_upload_read_failures_are_fixed_unchained_and_redacted(failure: Exception) -> None:
    client, _session, adapter, credential = real_client(
        real_json_outcome(200, valid_upload_payload())
    )

    with pytest.raises(ApiFailure) as raised:
        client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'{}',
            SnapshotReadFailure(failure),
        )

    rendered = f'{raised.value!s}\n{raised.value!r}'
    assert '/private/' not in rendered
    assert credential not in rendered
    assert raised.value.failure.kind is FailureKind.LOCAL_PROFILE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert adapter.calls == []


def test_upload_preparation_oserror_is_fixed_unchained_and_redacted() -> None:
    local_path = '/private/cache/customer-name.alog'
    client, _session, adapter, credential = real_client(OSError(local_path))

    with pytest.raises(ApiFailure) as raised:
        client.upload_revision(
            ROAST_UUID,
            SHA256,
            IDEMPOTENCY_KEY,
            b'{}',
            io.BytesIO(PROFILE_BYTES),
        )

    rendered = f'{raised.value!s}\n{raised.value!r}'
    assert local_path not in rendered
    assert credential not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert len(adapter.calls) == 1


def test_real_prepared_query_preserves_reserved_plus_unicode_and_utc_offsets() -> None:
    filters = ArchiveFilters(
        search='a b+café/?&=',
        state='parsed',
        machine='Drum +/雪',
        roast_at_from=datetime(
            2026,
            8,
            1,
            12,
            30,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        ),
        roast_at_to=datetime(
            2026,
            8,
            2,
            12,
            30,
            tzinfo=timezone(-timedelta(hours=4)),
        ),
    )
    client, _session, adapter, _credential = real_client(
        real_json_outcome(200, valid_roast_page_payload())
    )

    client.list_roasts(filters, cursor='c +/?&=✓', limit=25)

    assert adapter.calls[0].request.url == (
        'https://example.test/api/v1/roasts?limit=25&'
        'cursor=c+%2B%2F%3F%26%3D%E2%9C%93&'
        'search=a+b%2Bcaf%C3%A9%2F%3F%26%3D&state=parsed&'
        'machine=Drum+%2B%2F%E9%9B%AA&'
        'roast_at_from=2026-08-01T07%3A00%3A00%2B00%3A00&'
        'roast_at_to=2026-08-02T16%3A30%3A00%2B00%3A00'
    )


@pytest.mark.parametrize(('field', 'expected_message'), (
    ('cursor', 'invalid archive cursor'),
    ('search', 'invalid archive search'),
    ('machine', 'invalid archive machine'),
    ('idempotency', 'invalid idempotency key'),
))
def test_query_and_idempotency_reject_surrogates_before_preparation(
    field: str,
    expected_message: str,
) -> None:
    client, _session, adapter, _credential = real_client(
        real_json_outcome(200, valid_roast_page_payload())
    )
    surrogate = '\ud800'

    with pytest.raises(ValueError) as raised:
        if field == 'idempotency':
            client.upload_revision(
                ROAST_UUID,
                SHA256,
                surrogate,
                b'{}',
                io.BytesIO(PROFILE_BYTES),
            )
        else:
            client.list_roasts(
                ArchiveFilters(
                    search=surrogate if field == 'search' else None,
                    machine=surrogate if field == 'machine' else None,
                ),
                cursor=surrogate if field == 'cursor' else None,
            )

    assert raised.value.args == (expected_message,)
    assert raised.value.__cause__ is None
    assert adapter.calls == []


@pytest.mark.parametrize(
    ('encoding', 'body'),
    (
        (
            'gzip',
            gzip.compress(
                json.dumps(valid_identity_payload(), separators=(',', ':')).encode('utf-8')
            ),
        ),
        ('br', b'unsolicited brotli representation'),
    ),
)
def test_real_response_rejects_unsolicited_content_encoding_before_reading(
    encoding: str,
    body: bytes,
) -> None:
    client, _session, adapter, _credential = real_client(
        real_raw_outcome(
            200,
            body,
            (
                ('Content-Type', 'application/json'),
                ('Content-Encoding', encoding),
            ),
        )
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert len(adapter.calls) == 1
    assert adapter.responses[0].raw.closed


def test_real_response_accepts_exact_identity_content_encoding() -> None:
    payload = json.dumps(valid_identity_payload(), separators=(',', ':')).encode('utf-8')
    client, _session, adapter, _credential = real_client(
        real_raw_outcome(
            200,
            payload,
            (
                ('Content-Type', 'application/json'),
                ('Content-Length', str(len(payload))),
                ('Content-Encoding', 'identity'),
            ),
        )
    )

    identity = client.test_connection()

    assert identity.user.id == ROAST_UUID
    assert adapter.responses[0].raw.closed


@pytest.mark.parametrize(
    'encodings',
    (
        ('identity', 'identity'),
        ('identity', 'gzip'),
    ),
)
def test_real_response_rejects_repeated_or_conflicting_security_headers(
    encodings: tuple[str, str],
) -> None:
    payload = json.dumps(valid_identity_payload(), separators=(',', ':')).encode('utf-8')
    client, _session, adapter, _credential = real_client(
        real_raw_outcome(
            200,
            payload,
            (
                ('Content-Type', 'application/json'),
                ('Content-Length', str(len(payload))),
                ('Content-Encoding', encodings[0]),
                ('Content-Encoding', encodings[1]),
            ),
        )
    )

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert adapter.responses[0].raw.headers.getlist('Content-Encoding') == list(encodings)
    assert adapter.responses[0].raw.closed


@pytest.mark.parametrize('transfer_encoding', ('chunked', 'identity'))
def test_real_download_rejects_transfer_encoding_with_content_length_before_commit(
    transfer_encoding: str,
) -> None:
    headers = tuple(download_headers().items()) + (('Transfer-Encoding', transfer_encoding),)
    client, _session, adapter, _credential = real_client(
        real_raw_outcome(200, PROFILE_BYTES, headers)
    )
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert destination.getvalue() == b''
    assert adapter.responses[0].raw.closed


def test_real_download_rejects_duplicate_checksum_header_as_invalid_framing() -> None:
    headers = tuple(download_headers().items()) + (('X-Content-SHA256', SHA256),)
    client, _session, adapter, _credential = real_client(
        real_raw_outcome(200, PROFILE_BYTES, headers)
    )
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert adapter.responses[0].raw.headers.getlist('X-Content-SHA256') == [SHA256, SHA256]
    assert destination.getvalue() == b''
    assert adapter.responses[0].raw.closed


def test_download_writes_each_response_chunk_directly_then_flushes_and_rewinds(
    client_factory: ClientFactory,
) -> None:
    chunks = [PROFILE_BYTES[:5], PROFILE_BYTES[5:17], PROFILE_BYTES[17:]]
    response = raw_response(200, b'', download_headers(), chunks=chunks)
    client, _session = client_factory(response)
    destination = RecordingWriteDestination()

    receipt = client.download_revision(detail_for_download(), destination)

    assert receipt.byte_count == len(PROFILE_BYTES)
    assert destination.getvalue() == PROFILE_BYTES
    assert destination.write_sizes == [len(chunk) for chunk in chunks]
    assert destination.flush_calls == 1
    assert destination.tell() == 0


def test_download_midstream_transport_failure_leaves_empty_rewound_destination(
    client_factory: ClientFactory,
) -> None:
    response = raw_response(
        200,
        b'',
        download_headers(),
        chunks=[PROFILE_BYTES[:9], requests.ConnectionError('midstream diagnostic')],
    )
    client, _session = client_factory(response)
    destination = io.BytesIO()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert destination.getvalue() == b''
    assert destination.tell() == 0
    assert response.closed_by_client is True


def test_download_commit_failure_rolls_back_partial_bytes_and_rewinds(
    client_factory: ClientFactory,
) -> None:
    client, _session = client_factory(raw_response(200, PROFILE_BYTES, download_headers()))
    destination = PartialWriteFailure()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT
    assert destination.getvalue() == b''
    assert destination.tell() == 0
    assert '/private/' not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_download_rollback_failure_closes_destination_and_raises_fixed_failure(
    client_factory: ClientFactory,
) -> None:
    client, _session = client_factory(raw_response(200, PROFILE_BYTES, download_headers()))
    destination = RollbackFailureDestination()

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert destination.closed is True
    assert destination.close_calls == 1
    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT
    assert raised.value.failure.code == 'cache_corrupt'
    assert raised.value.failure.message == 'Cached copy corrupt or unavailable.'
    assert raised.value.status_code is None
    assert raised.value.retry_after_seconds is None
    assert '/private/' not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    'destination',
    (NonSeekableDestination(), NonWritableDestination(), NonTruncatableDestination()),
)
def test_download_rejects_unusable_destination_without_request(
    client_factory: ClientFactory,
    destination: io.BytesIO,
) -> None:
    client, session = client_factory(raw_response(200, PROFILE_BYTES, download_headers()))

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT
    assert destination.getvalue() == b''
    assert session.calls == []


def test_download_rejects_nonempty_destination_without_request(
    client_factory: ClientFactory,
) -> None:
    client, session = client_factory(raw_response(200, PROFILE_BYTES, download_headers()))
    destination = io.BytesIO(b'existing cache data')

    with pytest.raises(ApiFailure) as raised:
        client.download_revision(detail_for_download(), destination)

    assert raised.value.failure.kind is FailureKind.CACHE_CORRUPT
    assert destination.getvalue() == b'existing cache data'
    assert session.calls == []


def test_importing_api_has_no_settings_or_qt_transitive_import() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'import sys; import artisanlib.roastserver.api; '
                "assert 'artisanlib.roastserver.settings' not in sys.modules; "
                "assert not any(name == 'PyQt6' or name.startswith('PyQt6.') "
                'for name in sys.modules)'
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


# Diagnostic trace PUT and caller-owned immutable artifact streams.
TRACE_DESTINATION = Destination('https://example.test',
                          '22222222-2222-4222-8222-222222222222',
                          '11111111-1111-4111-8111-111111111111')


@pytest.fixture
def prepared_trace(tmp_path: Path) -> Iterator[tuple[TraceStore, UploadTicket]]:
    store = TraceStore(tmp_path / 'traces')
    session = str(uuid4())
    store.begin(CaptureConfig('4.0.0', 'linux', '6.1', 'x86_64').manifest(session, 0))
    store.seal(session, summary_record(session, 1, 1, 0, 0, 0, set()))
    store.authorize(session, TRACE_DESTINATION)
    ticket = store.prepare(session)
    try:
        yield store, ticket
    finally:
        store.close()


def trace_receipt(ticket: UploadTicket) -> Record:
    return {'id': '33333333-3333-4333-8333-333333333333', 'session_id': ticket.session_id,
            'organization_id': ticket.destination.organization_id,
            'uploader_user_id': ticket.destination.uploader_user_id,
            'sha256': ticket.sha256, 'byte_size': ticket.byte_size,
            'stored_at': '2026-08-01T12:00:00.123456Z', 'storage_status': 'stored'}


class TraceAdapter(RecordingAdapter):
    def __init__(self, outcomes: tuple[requests.Response | requests.RequestException, ...]) -> None:
        super().__init__('test-only-not-a-real-credential', outcomes)
        self.bodies: list[bytes] = []
        self.read_sizes: list[int] = []

    @override
    def send(self, request: requests.PreparedRequest, stream: bool = False,
             timeout: object = None, verify: object = True, cert: object = None,
             proxies: Mapping[str, str] | None = None) -> requests.Response:
        if request.method == 'PUT':
            body = cast(BinaryIO, request.body)
            chunks: list[bytes] = []
            while chunk := body.read(1024 * 1024):
                self.read_sizes.append(len(chunk))
                chunks.append(chunk)
            self.bodies.append(b''.join(chunks))
        return super().send(request, stream, timeout, verify, cert, proxies)


def trace_client_for(*outcomes: requests.Response | requests.RequestException) -> tuple[RoastServerClient, TraceAdapter]:
    client = RoastServerClient(TRACE_DESTINATION.origin, 'test-only-not-a-real-credential')
    adapter = TraceAdapter(outcomes)
    _install_test_adapter(client, adapter)
    return client, adapter


def test_held_upload_receipt_before_unlink_and_identical_explicit_retry(
    prepared_trace: tuple[TraceStore, UploadTicket],
) -> None:
    store, first = prepared_trace
    original = first.artifact.read_bytes()
    client, adapter = trace_client_for(json_response(200, valid_identity_payload()),
                                 json_response(200, valid_identity_payload()), raw_response(503, b''),
                                 json_response(200, valid_identity_payload()), json_response(200, trace_receipt(first)))
    with client:
        identity = client.test_connection()
        store.begin_upload(first, Destination(TRACE_DESTINATION.origin, str(identity.organization.id), str(identity.user.id)))
        with store.open_upload(first) as source:
            with pytest.raises(ApiFailure):
                client.put_diagnostic_trace(first, source)
            assert not bool(source.closed)
            assert first.artifact.exists()
        assert source.closed
        store.upload_failed(first)
        second = store.prepare(first.session_id)
        assert second.attempt_id != first.attempt_id
        assert second.sha256 == first.sha256
        store.begin_upload(second, TRACE_DESTINATION)
        with store.open_upload(second) as source:
            result = client.put_diagnostic_trace(second, source)
            assert not bool(source.closed)
        store.accept_receipt(second, result, request_origin=TRACE_DESTINATION.origin)
    assert adapter.bodies == [original, original]
    assert not first.artifact.exists()
    metadata = store.read_session(first.session_id)
    assert metadata['state'] == 'removed'
    assert metadata['receipt'] == result
    put = adapter.calls[-1]
    assert put.method == 'PUT'
    assert put.url == f'{TRACE_DESTINATION.origin}/api/v1/diagnostic-traces/{first.session_id}'
    assert put.headers['Content-Type'] == 'application/gzip'
    assert put.headers['Content-Length'] == str(first.byte_size)
    assert put.headers['X-Trace-SHA256'] == first.sha256
    assert put.headers['X-Trace-Organization-ID'] == TRACE_DESTINATION.organization_id
    assert put.headers['X-Trace-Uploader-ID'] == TRACE_DESTINATION.uploader_user_id
    assert 'Transfer-Encoding' not in put.headers
    assert all(call.timeout == (4.0, 10.0) and call.verify is True and not call.proxies for call in adapter.calls)
    assert all(not any(key.startswith('X-Trace-') for key in call.headers)
               for call in adapter.calls if call.method == 'GET')


@pytest.mark.parametrize('mismatch', ['origin', 'organization', 'user'])
def test_identity_mismatch_never_reads_or_sends_artifact(
    prepared_trace: tuple[TraceStore, UploadTicket], mismatch: str,
) -> None:
    _, ticket = prepared_trace
    identity = valid_identity_payload()
    if mismatch == 'origin':
        ticket = replace(ticket, destination=replace(TRACE_DESTINATION, origin='https://other.test'))
    else:
        cast(dict[str, str], identity[mismatch])['id'] = str(uuid4())
    client, adapter = trace_client_for(json_response(200, identity))
    source = io.BytesIO(b'must not read')
    with client, pytest.raises(ApiFailure) as raised:
        client.put_diagnostic_trace(ticket, source)
    assert raised.value.failure.kind is FailureKind.CREDENTIAL_REJECTED
    assert source.tell() == 0
    assert not bool(source.closed)
    assert len(adapter.calls) == (0 if mismatch == 'origin' else 1)
    assert not adapter.bodies


@pytest.mark.parametrize(('key', 'value'), [
    ('id', '00000000-0000-0000-0000-000000000000'), ('id', 'A' * 32),
    ('id', 1), ('session_id', str(uuid4())), ('organization_id', str(uuid4())),
    ('uploader_user_id', str(uuid4())), ('sha256', 'A' * 64), ('byte_size', True),
    ('byte_size', 1), ('byte_size', 1.0), ('byte_size', '368'), ('stored_at', '2026-02-30T12:00:00.000000Z'),
    ('stored_at', '2026-08-01T12:00:00Z'), ('storage_status', 'pending'),
    ('extra', 'forbidden'),
])
def test_malformed_receipt_retains_data(prepared_trace: tuple[TraceStore, UploadTicket], key: str, value: Json) -> None:
    store, ticket = prepared_trace
    malformed = trace_receipt(ticket)
    malformed[key] = value
    client, _ = trace_client_for(json_response(200, valid_identity_payload()), json_response(201, malformed))
    store.begin_upload(ticket, TRACE_DESTINATION)
    with client, store.open_upload(ticket) as source, pytest.raises(ApiFailure) as raised:
        client.put_diagnostic_trace(ticket, source)
    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    store.upload_failed(ticket)
    assert ticket.artifact.exists()
    assert store.read_session(ticket.session_id)['receipt'] is None


@pytest.mark.parametrize('malformation', ['duplicate', 'oversized', 'missing', 'constant', 'content-type', 'encoding'])
def test_receipt_decoding_is_strict_and_bounded(
    prepared_trace: tuple[TraceStore, UploadTicket], malformation: str,
) -> None:
    _, ticket = prepared_trace
    body = json.dumps(trace_receipt(ticket)).encode()
    headers = {'Content-Type': 'application/json'}
    if malformation == 'duplicate':
        body = body[:-1] + b', "storage_status": "stored"}'
    elif malformation == 'oversized':
        body = b' ' * 8193 + body
    elif malformation == 'missing':
        body = b'{}'
    elif malformation == 'constant':
        body = body.replace(str(ticket.byte_size).encode(), b'NaN')
    elif malformation == 'content-type':
        headers['Content-Type'] = 'text/html'
    else:
        headers['Content-Encoding'] = 'gzip'
    response = raw_response(200, body, headers)
    client, _ = trace_client_for(json_response(200, valid_identity_payload()), response)
    with client, ticket.artifact.open('rb') as source, pytest.raises(ApiFailure):
        client.put_diagnostic_trace(ticket, source)
    assert ticket.artifact.exists()
    assert response.closed_by_client
    if response.requested_chunk_size is not None:
        assert response.requested_chunk_size <= 8193


@pytest.mark.parametrize('status', [200, 201, 202, 204, 301, 302, 307, 308, 401, 403, 409, 410, 413, 422, 429, 500, 503])
def test_only_stored_success_statuses_no_redirect_or_retry(
    prepared_trace: tuple[TraceStore, UploadTicket], status: int,
) -> None:
    _, ticket = prepared_trace
    response = json_response(status, trace_receipt(ticket), {'Location': 'https://other.test/private'})
    client, adapter = trace_client_for(json_response(200, valid_identity_payload()), response)
    with client, ticket.artifact.open('rb') as source:
        if status in {200, 201}:
            assert client.put_diagnostic_trace(ticket, source) == trace_receipt(ticket)
        else:
            with pytest.raises(ApiFailure):
                client.put_diagnostic_trace(ticket, source)
        assert not bool(source.closed)
    assert len(adapter.calls) == 2
    assert ticket.artifact.exists()
    assert response.closed_by_client


@pytest.mark.parametrize('failure', [requests.Timeout('secret'), requests.ConnectionError('secret'), requests.exceptions.SSLError('secret')])
def test_transport_failure_has_no_retry_or_deletion(
    prepared_trace: tuple[TraceStore, UploadTicket], failure: requests.RequestException,
) -> None:
    _, ticket = prepared_trace
    client, adapter = trace_client_for(json_response(200, valid_identity_payload()), failure)
    with client, ticket.artifact.open('rb') as source, pytest.raises(ApiFailure) as raised:
        client.put_diagnostic_trace(ticket, source)
    assert 'secret' not in str(raised.value)
    assert len(adapter.calls) == 2
    assert ticket.artifact.exists()


@pytest.mark.parametrize('corruption', ['hash', 'size', 'gzip', 'closed', 'truncated', 'trailing'])
def test_invalid_held_bytes_fail_before_put(prepared_trace: tuple[TraceStore, UploadTicket], corruption: str) -> None:
    _, ticket = prepared_trace
    data = ticket.artifact.read_bytes()
    if corruption == 'hash':
        ticket = replace(ticket, sha256='0' * 64)
    elif corruption == 'size':
        ticket = replace(ticket, byte_size=ticket.byte_size + 1)
    elif corruption == 'gzip':
        data = b'x' * len(data)
        ticket = replace(ticket, sha256=hashlib.sha256(data).hexdigest())
    elif corruption == 'truncated':
        data = data[:-1]
    elif corruption == 'trailing':
        data += b'x'
    source = io.BytesIO(data)
    if corruption == 'closed':
        source.close()
    client, adapter = trace_client_for(json_response(200, valid_identity_payload()))
    with client, pytest.raises(ApiFailure):
        client.put_diagnostic_trace(ticket, source)
    assert len(adapter.calls) == 1
    assert not adapter.bodies


def test_pre_disclosure_generation_veto_and_client_close(prepared_trace: tuple[TraceStore, UploadTicket]) -> None:
    _, ticket = prepared_trace
    client, adapter = trace_client_for(json_response(200, valid_identity_payload()))
    with client, ticket.artifact.open('rb') as source, pytest.raises(ApiFailure):
        client.put_diagnostic_trace(ticket, source, before_disclosure=client.close)
    assert len(adapter.calls) == 1
    client, adapter = trace_client_for(json_response(200, valid_identity_payload()))

    def veto() -> None:
        raise RuntimeError('generation changed')

    with client, ticket.artifact.open('rb') as source, pytest.raises(RuntimeError, match='generation changed'):
        client.put_diagnostic_trace(ticket, source, before_disclosure=veto)
    assert len(adapter.calls) == 1


def test_trace_deadline_includes_fresh_identity_and_does_not_change_generic_budget(
    prepared_trace: tuple[TraceStore, UploadTicket], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, ticket = prepared_trace
    assert roastserver_api.TRACE_OPERATION_DEADLINE_SECONDS == 120
    assert roastserver_api.OPERATION_DEADLINE_SECONDS == 12
    monkeypatch.setattr(roastserver_api, 'TRACE_OPERATION_DEADLINE_SECONDS', 0.05)
    response = SlowDripResponse(json.dumps(valid_identity_payload()).encode(), 0.02)
    client, adapter = trace_client_for(response)
    with client, ticket.artifact.open('rb') as source, pytest.raises(ApiFailure) as raised:
        client.put_diagnostic_trace(ticket, source)
    assert raised.value.failure.code == 'timeout'
    assert len(adapter.calls) == 1
    assert response.closed_by_client
    client, _ = trace_client_for(json_response(200, valid_identity_payload()), json_response(200, trace_receipt(ticket)))
    monkeypatch.setattr(roastserver_api, 'OPERATION_DEADLINE_SECONDS', 0.01)
    monkeypatch.setattr(roastserver_api, 'TRACE_OPERATION_DEADLINE_SECONDS', 1.0)
    with client, ticket.artifact.open('rb') as source:
        assert client.put_diagnostic_trace(ticket, source, before_disclosure=lambda: time.sleep(0.03)) == trace_receipt(ticket)


def test_store_held_descriptor_is_busy_but_mutex_is_free(prepared_trace: tuple[TraceStore, UploadTicket]) -> None:
    store, ticket = prepared_trace
    store.begin_upload(ticket, TRACE_DESTINATION)
    with store.open_upload(ticket) as source:
        for action in (store.close, lambda: store.upload_failed(ticket),
                       lambda: store.delete_local(ticket.session_id),
                       lambda: store.accept_receipt(ticket, trace_receipt(ticket), request_origin=TRACE_DESTINATION.origin)):
            with pytest.raises(StoreError, match='session_busy'):
                action()
        with pytest.raises(StoreError, match='session_busy'), store.open_upload(ticket):
            pass
        completed = threading.Event()

        def other_store_work() -> None:
            store.read_session(ticket.session_id)
            completed.set()

        thread = threading.Thread(target=other_store_work)
        thread.start()
        try:
            assert completed.wait(1)
        finally:
            thread.join(1)
        assert hashlib.sha256(source.read()).hexdigest() == ticket.sha256
    assert source.closed
    store.upload_failed(ticket)


def test_store_rejects_unbegun_stale_and_closed_tickets(prepared_trace: tuple[TraceStore, UploadTicket]) -> None:
    store, ticket = prepared_trace
    with pytest.raises(StoreError, match='stale_job'), store.open_upload(ticket):
        pass
    store.begin_upload(ticket, TRACE_DESTINATION)
    store.upload_failed(ticket)
    new = store.prepare(ticket.session_id)
    store.begin_upload(new, TRACE_DESTINATION)
    with pytest.raises(StoreError, match='stale_job'), store.open_upload(ticket):
        pass
    store.close()
    with pytest.raises(StoreError, match='store_closed'), store.open_upload(new):
        pass


@pytest.mark.parametrize('substitution', ['symlink', 'hardlink', 'bytes', 'same-size', 'path'])
def test_store_rejects_substitution_before_disclosure(
    prepared_trace: tuple[TraceStore, UploadTicket], substitution: str, tmp_path: Path,
) -> None:
    store, ticket = prepared_trace
    store.begin_upload(ticket, TRACE_DESTINATION)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'not the immutable artifact')
    if substitution == 'path':
        ticket = replace(ticket, artifact=outside)
    elif substitution == 'symlink':
        ticket.artifact.unlink()
        ticket.artifact.symlink_to(outside)
    elif substitution == 'hardlink':
        outside.unlink()
        outside.hardlink_to(ticket.artifact)
    elif substitution == 'same-size':
        ticket.artifact.write_bytes(b'x' * ticket.byte_size)
    else:
        ticket.artifact.write_bytes(b'changed')
    with pytest.raises((OSError, RuntimeError, ValueError)), store.open_upload(ticket):
        pass


def test_stream_never_reopens_path_after_validation(
    prepared_trace: tuple[TraceStore, UploadTicket], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ticket = prepared_trace
    store.begin_upload(ticket, TRACE_DESTINATION)
    original = ticket.artifact.read_bytes()
    with store.open_upload(ticket) as source:
        def forbidden_open(_path: Path) -> BinaryIO:
            raise AssertionError('reopened artifact')
        monkeypatch.setattr(store, '_open', forbidden_open)
        client, adapter = trace_client_for(json_response(200, valid_identity_payload()), json_response(201, trace_receipt(ticket)))
        with client:
            assert client.put_diagnostic_trace(ticket, source) == trace_receipt(ticket)
        assert adapter.bodies == [original]


@pytest.mark.parametrize(('field', 'value'), [
    ('sha256', 'A' * 64), ('sha256', '0' * 64 + '\n'),
    ('session_id', 'A' * 32), ('session_id', '00000000-0000-0000-0000-000000000000'),
    ('byte_size', True), ('byte_size', 0), ('byte_size', 65 * 1024 * 1024 + 1),
])
def test_trace_ticket_headers_are_canonical_before_any_request(
    prepared_trace: tuple[TraceStore, UploadTicket], field: str, value: object,
) -> None:
    _, ticket = prepared_trace
    # Runtime boundary deliberately receives malformed typed input.
    malformed = replace(ticket, **{field: value})  # type: ignore[arg-type]
    client, adapter = trace_client_for()
    with client, pytest.raises(ValueError):
        client.put_diagnostic_trace(malformed, io.BytesIO())
    assert not adapter.calls


def test_trace_headers_cannot_escape_diagnostic_put(prepared_trace: tuple[TraceStore, UploadTicket]) -> None:
    _, ticket = prepared_trace
    client, adapter = trace_client_for()
    with client:
        for method, path, content_type in (
            ('POST', '/api/v1/diagnostic-traces/' + ticket.session_id, 'application/gzip'),
            ('PUT', '/api/v1/aroast', 'application/gzip'),
            ('PUT', '/api/v1/diagnostic-traces/' + ticket.session_id, 'text/plain'),
        ):
            def rejected_request(deadline: roastserver_api._DeadlineGuard, method: str = method,
                                 path: str = path, content_type: str = content_type) -> requests.Response:
                return client._request(
                    method, path, deadline=deadline, data=io.BytesIO(),
                    trace_ticket=ticket, body_content_type=content_type,
                    body_content_length=ticket.byte_size,
                )

            with pytest.raises(ValueError):
                client._run_operation(rejected_request)
        with pytest.raises(ValueError):
            client._run_operation(lambda deadline: client._request(
                'GET', '/api/v1/auth/me', deadline=deadline,
                additional_headers={'X-Trace-SHA256': ticket.sha256},
            ))
    assert not adapter.calls


def test_large_trace_transport_reads_are_bounded_and_exact(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / 'large')
    session = str(uuid4())
    try:
        store.begin(CaptureConfig('4.0.0', 'linux', '6.1', 'x86_64').manifest(session, 0))
        for seq in range(1, 5):
            data = secrets.token_bytes(32768)
            event: Record = {'kind': 'rx', 'session_id': session, 'seq': seq,
                             'mono_ns': seq, 'dropped_before': 0,
                             'connection_id': str(uuid4()), 'direction': 'rx',
                             'characteristic': '6e400003-b5a3-f393-e0a9-e50e24dcca9e',
                             'payload': {'encoding': 'base64', 'byte_length': len(data),
                                         'data': base64.b64encode(data).decode('ascii')}}
            assert store.append(session, event) is None
        store.seal(session, summary_record(session, 5, 5, 4, 0, 0, set()))
        store.authorize(session, TRACE_DESTINATION)
        ticket = store.prepare(session)
        assert ticket.byte_size > 65536
        store.begin_upload(ticket, TRACE_DESTINATION)
        client, adapter = trace_client_for(json_response(200, valid_identity_payload()), json_response(201, trace_receipt(ticket)))
        with client, store.open_upload(ticket) as source:
            assert client.put_diagnostic_trace(ticket, source) == trace_receipt(ticket)
        assert max(adapter.read_sizes) == 65536
        assert sum(adapter.read_sizes) == ticket.byte_size
        assert hashlib.sha256(adapter.bodies[0]).hexdigest() == ticket.sha256
    finally:
        store.close()


def test_trace_success_without_consuming_exact_body_is_rejected(prepared_trace: tuple[TraceStore, UploadTicket]) -> None:
    _, ticket = prepared_trace
    client = RoastServerClient(TRACE_DESTINATION.origin, 'test-only')
    adapter = RecordingAdapter('test-only', (json_response(200, valid_identity_payload()),
                                            json_response(201, trace_receipt(ticket))))
    _install_test_adapter(client, adapter)
    with client, ticket.artifact.open('rb') as source, pytest.raises(ApiFailure):
        client.put_diagnostic_trace(ticket, source)
    assert ticket.artifact.exists()


def test_held_stream_survives_cancellation_until_actual_request_settles(
    prepared_trace: tuple[TraceStore, UploadTicket], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ticket = prepared_trace
    store.begin_upload(ticket, TRACE_DESTINATION)
    started, release = threading.Event(), threading.Event()
    failure: list[Exception] = []
    disclosed: list[bytes] = []

    class BlockedTraceAdapter(TraceAdapter):
        @override
        def send(self, request: requests.PreparedRequest, stream: bool = False,
                 timeout: object = None, verify: object = True, cert: object = None,
                 proxies: Mapping[str, str] | None = None) -> requests.Response:
            if request.method == 'PUT':
                disclosed.append(cast(BinaryIO, request.body).read(7))
                started.set()
                assert release.wait(2)
            return super().send(request, stream, timeout, verify, cert, proxies)

    client = RoastServerClient(TRACE_DESTINATION.origin, 'test-only-not-a-real-credential')
    adapter = BlockedTraceAdapter((json_response(200, valid_identity_payload()), json_response(201, trace_receipt(ticket))))
    _install_test_adapter(client, adapter)
    monkeypatch.setattr(roastserver_api, 'TRACE_OPERATION_DEADLINE_SECONDS', 0.05)
    with store.open_upload(ticket) as source:
        def upload() -> None:
            try:
                client.put_diagnostic_trace(ticket, source)
            except Exception as error:  # thread returns failures to the test owner
                failure.append(error)

        thread = threading.Thread(target=upload)
        thread.start()
        try:
            assert started.wait(1)
            # A deadline closes the client but cannot pretend a blocked adapter has settled.
            time.sleep(0.1)
            assert thread.is_alive()
            assert not bool(source.closed)
            with pytest.raises(StoreError, match='session_busy'):
                store.upload_failed(ticket)
            release.set()
            thread.join(1)
            assert not thread.is_alive()
            assert not bool(source.closed)
        finally:
            release.set()
            thread.join(2)
            client.close()
    assert source.closed
    assert len(failure) == 1 and isinstance(failure[0], ApiFailure)
    assert failure[0].failure.code == 'timeout'
    store.upload_failed(ticket)
    assert ticket.artifact.exists()
    assert disclosed == [ticket.artifact.read_bytes()[:7]]
    assert not adapter.bodies  # Deadline/cancellation guard rejects further body reads.


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX rename of a held descriptor')
def test_trace_upload_uses_held_inode_not_substituted_path(prepared_trace: tuple[TraceStore, UploadTicket]) -> None:
    store, ticket = prepared_trace
    store.begin_upload(ticket, TRACE_DESTINATION)
    original = ticket.artifact.read_bytes()
    with store.open_upload(ticket) as source:
        ticket.artifact.rename(ticket.artifact.with_suffix('.held-test'))
        ticket.artifact.write_bytes(b'x' * ticket.byte_size)
        client, adapter = trace_client_for(json_response(200, valid_identity_payload()),
                                           json_response(201, trace_receipt(ticket)))
        with client:
            assert client.put_diagnostic_trace(ticket, source) == trace_receipt(ticket)
        assert adapter.bodies == [original]
    # Hostile same-user substitution is outside the filesystem trust boundary;
    # do not authorize deletion of the replacement in this synthetic test.
    assert ticket.artifact.read_bytes() != original
