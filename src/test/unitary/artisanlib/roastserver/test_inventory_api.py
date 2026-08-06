from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
import json
import secrets
import socket
from typing import cast, override
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from artisanlib.roastserver.api import ApiFailure, RoastServerClient
from artisanlib.roastserver.contract import (
    FAILURE_MESSAGES,
    MAX_JSON_BYTES,
    POSTGRESQL_INTEGER_MAX,
    FailureKind,
)
from artisanlib.roastserver.inventory_contract import (
    MAX_INVENTORY_CURSOR_CHARS,
    InventoryCommandRequest,
    InventoryOperation,
    build_finalize_request,
    build_release_request,
    build_reserve_request,
)

ROAST_UUID = UUID('11111111-1111-4111-8111-111111111111')
LOT_UUID = UUID('22222222-2222-4222-8222-222222222222')
CLIENT_UUID = UUID('33333333-3333-4333-8333-333333333333')
RESERVATION_UUID = UUID('44444444-4444-4444-8444-444444444444')
SERVER_RESERVATION_UUID = UUID('55555555-5555-4555-8555-555555555555')
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
COMPLETED_AT = '2026-08-05T12:01:00.000000Z'

RESERVE_REQUEST = build_reserve_request(
    client_instance_uuid=CLIENT_UUID,
    reservation_uuid=RESERVATION_UUID,
    roast_uuid=ROAST_UUID,
    lot_id=LOT_UUID,
    planned_grams=1_250,
    occurred_at=NOW,
)
FINALIZE_REQUEST = build_finalize_request(
    client_instance_uuid=CLIENT_UUID,
    reservation_uuid=RESERVATION_UUID,
    roast_uuid=ROAST_UUID,
    lot_id=LOT_UUID,
    planned_grams=1_250,
    actual_grams=1_200,
    occurred_at=NOW,
)
RELEASE_REQUEST = build_release_request(
    client_instance_uuid=CLIENT_UUID,
    reservation_uuid=RESERVATION_UUID,
    roast_uuid=ROAST_UUID,
    lot_id=LOT_UUID,
    planned_grams=1_250,
    occurred_at=NOW,
)


class ForgedHex:
    hex = '../../auth/me'


class EqualToEverything:
    __hash__ = object.__hash__

    @override
    def __eq__(self, _other: object) -> bool:
        return True


class OperationString(str):
    pass


def lot_payload() -> dict[str, object]:
    return {
        'lot_id': LOT_UUID.hex,
        'name': 'Ethiopia Guji',
        'origin': 'Ethiopia',
        'varietals': ['Heirloom'],
        'processing_method': 'washed',
        'crop_year': 2026,
        'on_hand_grams': 10_000,
        'reserved_grams': 1_250,
        'available_grams': 8_750,
        'unresolved_conflict_count': 0,
    }


def mutation_payload(*, state: str = 'reserved') -> dict[str, object]:
    completed_at = None if state == 'reserved' else COMPLETED_AT
    actual_grams = 1_200 if state == 'finalized' else None
    return {
        'reservation': {
            'reservation_id': SERVER_RESERVATION_UUID.hex,
            'client_reservation_uuid': RESERVATION_UUID.hex,
            'lot_id': LOT_UUID.hex,
            'roast_uuid': ROAST_UUID.hex,
            'client_instance_uuid': CLIENT_UUID.hex,
            'state': state,
            'planned_grams': 1_250,
            'actual_grams': actual_grams,
            'reserved_at': '2026-08-05T12:00:01.000000Z',
            'completed_at': completed_at,
            'created_at': '2026-08-05T12:00:01.000000Z',
            'updated_at': completed_at or '2026-08-05T12:00:01.000000Z',
            'open_conflict_id': None,
        },
        'balance': {
            'lot_id': LOT_UUID.hex,
            'on_hand_grams': 8_750,
            'reserved_grams': 0,
            'available_grams': 8_750,
            'unresolved_conflict_count': 0,
        },
        'conflict': None,
        'idempotent_replay': False,
    }


class FakeResponse(requests.Response):
    def __init__(
        self,
        status_code: int,
        body: bytes,
        headers: Mapping[str, str],
        *,
        chunks: list[bytes] | None = None,
    ) -> None:
        super().__init__()
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers)
        self._chunks = [body] if chunks is None else chunks
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
        yield from self._chunks

    @override
    def close(self) -> None:
        self.closed_by_client = True


def response(
    status_code: int,
    payload: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> FakeResponse:
    body = json.dumps(payload, separators=(',', ':')).encode()
    response_headers = {
        'Content-Type': 'application/json',
        'Content-Length': str(len(body)),
    }
    if headers is not None:
        response_headers.update(headers)
    return FakeResponse(status_code, body, response_headers)


@dataclass(frozen=True, slots=True)
class RecordedCall:
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
    def path(self) -> str:
        return urlsplit(self.request.url or '').path

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.request.url or '').query, keep_blank_values=True)

    @property
    def headers(self) -> Mapping[str, str]:
        return self.request.headers

    @property
    def data(self) -> object:
        return self.request.body


class RecordingSession(HTTPAdapter):
    def __init__(self) -> None:
        super().__init__(max_retries=0)
        self._responses: list[requests.Response | requests.RequestException] = []
        self.calls: list[RecordedCall] = []

    def respond(
        self,
        status_code: int,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        configured = response(status_code, payload, headers=headers)
        self._responses.append(configured)
        return configured

    def fail_with(self, failure: requests.RequestException) -> None:
        self._responses.append(failure)

    def respond_raw(
        self,
        status_code: int,
        body: bytes,
        headers: Mapping[str, str],
        *,
        chunks: list[bytes] | None = None,
    ) -> FakeResponse:
        configured = FakeResponse(status_code, body, headers, chunks=chunks)
        self._responses.append(configured)
        return configured

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
        self.calls.append(RecordedCall(request, stream, timeout, verify, cert, proxies))
        if not self._responses:
            raise AssertionError('unconfigured recording session request')
        configured = self._responses.pop(0)
        if isinstance(configured, requests.RequestException):
            raise configured
        configured.request = request
        configured.url = request.url or ''
        return configured


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('network access is forbidden')

    monkeypatch.setattr(socket.socket, 'connect', reject_network)


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


def client(session: RecordingSession, *, credential: str | None = None) -> RoastServerClient:
    result = RoastServerClient(
        'https://example.test',
        secrets.token_urlsafe(32) if credential is None else credential,
    )
    requests_session = cast(requests.Session, vars(result)['_session'])
    replaced = requests_session.get_adapter('https://')
    requests_session.mount('https://', session)
    replaced.close()
    return result


def error_payload(code: str, message: str) -> dict[str, object]:
    return {'error': {'code': code, 'message': message, 'details': None}}


def test_list_inventory_lots_uses_exact_bounded_request(session: RecordingSession) -> None:
    configured = session.respond(
        200,
        {'items': [lot_payload()], 'next_cursor': 'next-page'},
    )

    page = client(session).list_inventory_lots(cursor='opaque-cursor', limit=100)

    assert page.items[0].lot_id == LOT_UUID
    assert page.next_cursor == 'next-page'
    call = session.calls[0]
    assert call.method == 'GET'
    assert call.path == '/api/v1/inventory/bean-lots'
    assert call.query == {'limit': ['100'], 'cursor': ['opaque-cursor']}
    assert call.data is None
    assert call.stream is True
    assert call.timeout == (4.0, 10.0)
    assert call.verify is True
    assert call.cert is None
    assert call.proxies == {}
    assert 'Cookie' not in call.headers
    assert configured.closed_by_client is True
    assert configured.requested_chunk_size == 64 * 1024


@pytest.mark.parametrize(
    ('cursor', 'limit'),
    (
        ('', 100),
        ('x' * (MAX_INVENTORY_CURSOR_CHARS + 1), 100),
        ('control\x00cursor', 100),
        (None, 0),
        (None, 101),
        (None, True),
    ),
)
def test_list_inventory_lots_rejects_invalid_bounds_without_request(
    session: RecordingSession,
    cursor: str | None,
    limit: int,
) -> None:
    with pytest.raises(ValueError):
        client(session).list_inventory_lots(cursor=cursor, limit=limit)
    assert session.calls == []


@pytest.mark.parametrize(
    ('command_request', 'status', 'path', 'state'),
    (
        (RESERVE_REQUEST, 201, '/api/v1/inventory/reservations', 'reserved'),
        (
            FINALIZE_REQUEST,
            200,
            f'/api/v1/inventory/reservations/{RESERVATION_UUID.hex}/finalize',
            'finalized',
        ),
        (
            RELEASE_REQUEST,
            200,
            f'/api/v1/inventory/reservations/{RESERVATION_UUID.hex}/release',
            'released',
        ),
    ),
)
def test_inventory_commands_use_immutable_body_exact_path_and_idempotency_header(
    session: RecordingSession,
    command_request: InventoryCommandRequest,
    status: int,
    path: str,
    state: str,
) -> None:
    configured = session.respond(status, mutation_payload(state=state))

    result = client(session).execute_inventory_command(command_request)

    call = session.calls[0]
    assert call.method == 'POST'
    assert call.path == path
    assert call.query == {}
    assert call.headers['Idempotency-Key'] == command_request.idempotency_key
    assert call.headers['Content-Type'] == 'application/json'
    assert call.headers['Content-Length'] == str(len(command_request.request_json))
    assert call.data is command_request.request_json
    assert result.reservation.state == state
    assert configured.closed_by_client is True


@pytest.mark.parametrize(
    'malicious_request',
    (
        pytest.param(object(), id='not-exact-command-dataclass'),
        pytest.param(
            replace(
                RESERVE_REQUEST,
                operation=cast(InventoryOperation, OperationString('reserve')),
            ),
            id='operation-string-subclass',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, operation=cast(InventoryOperation, 'delete')),
            id='invalid-operation',
        ),
        pytest.param(
            replace(
                RELEASE_REQUEST,
                reservation_uuid=cast(UUID, ForgedHex()),
                idempotency_key=(
                    f'inventory-v1:{CLIENT_UUID.hex}:../../auth/me:release'
                ),
                operation='release',
            ),
            id='forged-reservation-path-traversal',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, client_instance_uuid=cast(UUID, ForgedHex())),
            id='forged-client-uuid',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, roast_uuid=cast(UUID, ForgedHex())),
            id='forged-roast-uuid',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, lot_id=cast(UUID, ForgedHex())),
            id='forged-lot-uuid',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, planned_grams=cast(int, True)),
            id='boolean-planned-grams',
        ),
        pytest.param(replace(RESERVE_REQUEST, planned_grams=0), id='zero-planned-grams'),
        pytest.param(
            replace(RESERVE_REQUEST, planned_grams=POSTGRESQL_INTEGER_MAX + 1),
            id='oversized-planned-grams',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, requested_actual_grams=1),
            id='reserve-with-actual-grams',
        ),
        pytest.param(
            replace(RELEASE_REQUEST, requested_actual_grams=1),
            id='release-with-actual-grams',
        ),
        pytest.param(
            replace(FINALIZE_REQUEST, requested_actual_grams=cast(int, True)),
            id='boolean-actual-grams',
        ),
        pytest.param(
            replace(FINALIZE_REQUEST, requested_actual_grams=0),
            id='zero-actual-grams',
        ),
        pytest.param(
            replace(
                FINALIZE_REQUEST,
                requested_actual_grams=POSTGRESQL_INTEGER_MAX + 1,
            ),
            id='oversized-actual-grams',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, occurred_at=NOW.replace(tzinfo=None)),
            id='naive-timestamp',
        ),
        pytest.param(
            replace(
                RESERVE_REQUEST,
                occurred_at=NOW.astimezone(timezone(timedelta(hours=1))),
            ),
            id='non-utc-timestamp',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, occurred_at=cast(datetime, object())),
            id='non-datetime-timestamp',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, request_json=cast(bytes, bytearray(b'{}'))),
            id='mutable-request-body',
        ),
        pytest.param(replace(RESERVE_REQUEST, request_json=b''), id='empty-request-body'),
        pytest.param(
            replace(RESERVE_REQUEST, request_json=b'x' * (MAX_JSON_BYTES + 1)),
            id='oversized-request-body',
        ),
        pytest.param(
            replace(
                RESERVE_REQUEST,
                idempotency_key=cast(str, EqualToEverything()),
            ),
            id='non-string-idempotency-key',
        ),
        pytest.param(
            replace(RESERVE_REQUEST, idempotency_key='inventory-v1:forged'),
            id='inconsistent-idempotency-key',
        ),
    ),
)
def test_inventory_command_rejects_malicious_metadata_without_transport(
    session: RecordingSession,
    malicious_request: object,
) -> None:
    with pytest.raises(ValueError, match='invalid inventory command request'):
        client(session).execute_inventory_command(
            cast(InventoryCommandRequest, malicious_request)
        )

    assert session.calls == []


@pytest.mark.parametrize(
    ('status', 'code', 'message', 'kind', 'retryable'),
    (
        (404, 'bean_lot_not_found', 'Bean lot not found', FailureKind.INVENTORY_REJECTED, False),
        (409, 'bean_lot_archived', 'Bean lot archived', FailureKind.INVENTORY_REJECTED, False),
        (
            409,
            'invalid_inventory_transition',
            'Invalid inventory transition',
            FailureKind.INVENTORY_REJECTED,
            False,
        ),
        (
            409,
            'inventory_idempotency_conflict',
            'Idempotency key conflicts with an earlier request',
            FailureKind.INVENTORY_CONFLICT,
            False,
        ),
        (
            404,
            'inventory_reservation_not_found',
            'Inventory reservation not found',
            FailureKind.INVENTORY_REJECTED,
            False,
        ),
        (503, 'inventory_unavailable', 'Inventory unavailable', FailureKind.OFFLINE, True),
        (422, 'invalid_request', 'Invalid request', FailureKind.INVENTORY_REJECTED, False),
    ),
)
def test_inventory_exact_error_envelopes_keep_fixed_classification(
    session: RecordingSession,
    status: int,
    code: str,
    message: str,
    kind: FailureKind,
    retryable: bool,
) -> None:
    configured = session.respond(status, error_payload(code, message))

    with pytest.raises(ApiFailure) as raised:
        client(session).execute_inventory_command(RESERVE_REQUEST)

    assert raised.value.failure.kind is kind
    assert raised.value.failure.code == code
    assert raised.value.failure.message == message
    assert raised.value.failure.retryable is retryable
    assert raised.value.status_code == status
    assert configured.closed_by_client is True


@pytest.mark.parametrize(
    ('status', 'kind', 'retryable'),
    (
        (401, FailureKind.CREDENTIAL_REJECTED, False),
        (403, FailureKind.CREDENTIAL_REJECTED, False),
        (404, FailureKind.INVENTORY_UNSUPPORTED, False),
        (429, FailureKind.RATE_LIMITED, True),
        (500, FailureKind.OFFLINE, True),
        (599, FailureKind.OFFLINE, True),
        (400, FailureKind.INVENTORY_REJECTED, False),
    ),
)
def test_inventory_generic_status_classification(
    session: RecordingSession,
    status: int,
    kind: FailureKind,
    retryable: bool,
) -> None:
    configured = session.respond(
        status,
        error_payload('arbitrary_private_code', 'Arbitrary private message'),
        headers={'Retry-After': '120'},
    )

    with pytest.raises(ApiFailure) as raised:
        client(session).list_inventory_lots()

    assert raised.value.failure.kind is kind
    assert raised.value.failure.code == kind.value
    assert raised.value.failure.message == FAILURE_MESSAGES[kind]
    assert raised.value.failure.retryable is retryable
    assert raised.value.retry_after_seconds == (120 if status == 429 or status >= 500 else None)
    rendered = f'{raised.value!s}\n{raised.value!r}\n{raised.value.failure!r}'
    assert 'arbitrary_private' not in rendered
    assert 'Arbitrary private' not in rendered
    assert configured.closed_by_client is True


@pytest.mark.parametrize(
    ('failure', 'code'),
    (
        (requests.ConnectionError('private transport diagnostic'), 'connection_error'),
        (requests.Timeout('private transport diagnostic'), 'timeout'),
        (requests.exceptions.SSLError('private transport diagnostic'), 'tls_error'),
    ),
)
def test_inventory_transport_failures_are_fixed_and_retryable(
    session: RecordingSession,
    failure: requests.RequestException,
    code: str,
) -> None:
    session.fail_with(failure)

    with pytest.raises(ApiFailure) as raised:
        client(session).list_inventory_lots()

    assert raised.value.failure.kind is FailureKind.OFFLINE
    assert raised.value.failure.code == code
    assert raised.value.failure.retryable is True
    assert raised.value.status_code is None
    rendered = f'{raised.value!s}\n{raised.value!r}\n{raised.value.failure!r}'
    assert 'private transport' not in rendered


def test_inventory_redirect_is_rejected_without_followup(session: RecordingSession) -> None:
    configured = session.respond_raw(307, b'', {'Location': 'https://other.test'})

    with pytest.raises(ApiFailure) as raised:
        client(session).list_inventory_lots()

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert raised.value.status_code == 307
    assert len(session.calls) == 1
    assert configured.closed_by_client is True


@pytest.mark.parametrize(
    ('method_name', 'status', 'payload'),
    (
        ('list', 200, {'items': [], 'next_cursor': '', 'extra': True}),
        ('reserve', 201, mutation_payload(state='finalized')),
    ),
)
def test_inventory_malformed_success_is_invalid_and_closed(
    session: RecordingSession,
    method_name: str,
    status: int,
    payload: object,
) -> None:
    configured = session.respond(status, payload)

    with pytest.raises(ApiFailure) as raised:
        if method_name == 'list':
            client(session).list_inventory_lots()
        else:
            client(session).execute_inventory_command(RESERVE_REQUEST)

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert raised.value.status_code == status
    assert configured.closed_by_client is True


def test_inventory_success_body_is_bounded_before_parsing(session: RecordingSession) -> None:
    configured = session.respond_raw(
        200,
        b'',
        {'Content-Type': 'application/json'},
        chunks=[b'{}', b'x' * MAX_JSON_BYTES],
    )

    with pytest.raises(ApiFailure) as raised:
        client(session).list_inventory_lots()

    assert raised.value.failure.kind is FailureKind.INVALID_RESPONSE
    assert configured.closed_by_client is True
    assert configured.requested_chunk_size == 64 * 1024


def test_inventory_error_body_is_bounded_and_never_exposed(
    session: RecordingSession,
) -> None:
    credential = secrets.token_urlsafe(48)
    secret_body = f'{credential} private diagnostic'.encode()
    configured = session.respond_raw(
        404,
        b'',
        {'Content-Type': 'application/json'},
        chunks=[secret_body, b'x' * MAX_JSON_BYTES],
    )
    roast_client = client(session, credential=credential)

    with pytest.raises(ApiFailure) as raised:
        roast_client.list_inventory_lots()

    assert raised.value.failure.kind is FailureKind.INVENTORY_UNSUPPORTED
    rendered = f'{raised.value!s}\n{raised.value!r}\n{raised.value.failure!r}\n{roast_client!r}'
    assert credential not in rendered
    assert 'private diagnostic' not in rendered
    assert configured.closed_by_client is True


def test_request_rejects_unapproved_extra_headers_without_transport(
    session: RecordingSession,
) -> None:
    roast_client = client(session)
    deadline = roast_client._start_operation()  # pylint: disable=protected-access
    try:
        with pytest.raises(ValueError, match='invalid additional request header'):
            roast_client._request(  # pylint: disable=protected-access
                'POST',
                '/api/v1/inventory/reservations',
                deadline=deadline,
                json_bytes=b'{}',
                additional_headers={'Authorization': 'Bearer attacker'},
            )
        with pytest.raises(ValueError, match='invalid additional request header'):
            roast_client._request(  # pylint: disable=protected-access
                'POST',
                '/api/v1/inventory/reservations',
                deadline=deadline,
                json_bytes=b'{}',
                additional_headers={'Content-Type': 'text/plain'},
            )
    finally:
        roast_client._finish_operation(deadline)  # pylint: disable=protected-access

    assert session.calls == []
