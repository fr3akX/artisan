from __future__ import annotations

import gzip
import hashlib
import inspect
import io
import json
import secrets
import subprocess
import sys
from collections.abc import Buffer, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from typing import cast, override
from uuid import UUID

import pytest
import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3._collections import HTTPHeaderDict

from artisanlib.roastserver.api import ApiFailure, DownloadReceipt, RoastServerClient
from artisanlib.roastserver.contract import (
    ArchiveFilters,
    FailureKind,
    MAX_JSON_BYTES,
    MAX_METADATA_BYTES,
    MAX_PROFILE_BYTES,
    RoastDetail,
    parse_roast_detail,
)

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


def multipart_profile(call: AdapterCall) -> bytes:
    body = call.request.body
    content_type = call.request.headers.get('Content-Type', '')
    assert isinstance(body, bytes)
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
    assert isinstance(call.data, bytes)
    assert call.data.count(b'Content-Disposition: form-data; name=') == 4
    assert b'name="sha256"' in call.data
    assert SHA256.encode() in call.data
    assert b'name="idempotency_key"' in call.data
    assert IDEMPOTENCY_KEY.encode() in call.data
    assert b'name="metadata"' in call.data
    assert b'{"machine":"Test Drum"}' in call.data
    assert b'name="profile"' in call.data
    assert f'filename="{ROAST_UUID.hex}.alog"'.encode() in call.data
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
            'code': 'safe_server_code',
            'message': 'Safe server message.',
            'details': None,
        }
    }
    client, _session = client_factory(json_response(status_code, body))

    with pytest.raises(ApiFailure) as raised:
        client.test_connection()

    assert raised.value.failure.kind is kind
    assert raised.value.failure.retryable is retryable
    assert raised.value.failure.code == 'safe_server_code'
    assert raised.value.failure.message == 'Safe server message.'
    assert raised.value.status_code == status_code


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
    assert isinstance(call.request.body, bytes)
    assert len(call.request.body) <= MAX_PROFILE_BYTES + MAX_METADATA_BYTES + 4096
    assert call.request.body.count(b'Content-Disposition: form-data; name=') == 4
    assert b'name="sha256"' in call.request.body
    assert b'name="idempotency_key"' in call.request.body
    assert b'name="metadata"' in call.request.body
    assert b'name="profile"' in call.request.body
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
