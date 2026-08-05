from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import json
import math
from types import MappingProxyType
from uuid import UUID

import pytest

from artisanlib.roastserver import InventoryCommandRequest as ExportedInventoryCommandRequest
from artisanlib.roastserver.contract import (
    ContractError,
    FAILURE_MESSAGES,
    POSTGRESQL_INTEGER_MAX,
    FailureKind,
    Namespace,
)
from artisanlib.roastserver.inventory_contract import (
    MAX_CACHED_LOTS,
    MAX_INVENTORY_CURSOR_CHARS,
    MAX_INVENTORY_PAGES,
    BeanLot,
    InventoryCommandRequest,
    InventoryMutationResult,
    InventoryOperation,
    InventoryProfileLink,
    build_finalize_request,
    build_release_request,
    build_reserve_request,
    green_weight_grams,
    parse_bean_lot_page,
    parse_inventory_error,
    parse_inventory_mutation,
    parse_profile_link,
    profile_link_fields,
)
from artisanlib.roastserver.settings import namespace_for

ROAST_UUID = UUID('11111111-1111-4111-8111-111111111111')
LOT_UUID = UUID('22222222-2222-4222-8222-222222222222')
CLIENT_UUID = UUID('33333333-3333-4333-8333-333333333333')
RESERVATION_UUID = UUID('44444444-4444-4444-8444-444444444444')
SERVER_RESERVATION_UUID = UUID('55555555-5555-4555-8555-555555555555')
CONFLICT_UUID = UUID('66666666-6666-4666-8666-666666666666')
LEDGER_UUID = UUID('77777777-7777-4777-8777-777777777777')
ORGANIZATION_UUID = UUID('88888888-8888-4888-8888-888888888888')
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
PROCESSING_METHODS = (
    'washed',
    'natural',
    'honey',
    'pulped-natural',
    'wet-hulled',
    'anaerobic',
    'experimental',
    'other',
)


def valid_lot_payload() -> dict[str, object]:
    return {
        'lot_id': LOT_UUID.hex,
        'name': 'Ethiopia Guji',
        'origin': 'Ethiopia',
        'varietals': ['Heirloom', '74110'],
        'processing_method': 'washed',
        'crop_year': 2026,
        'on_hand_grams': 10_000,
        'reserved_grams': 1_250,
        'available_grams': 8_750,
        'unresolved_conflict_count': 0,
    }


def valid_page_payload() -> dict[str, object]:
    return {'items': [valid_lot_payload()], 'next_cursor': None}


def valid_reservation_payload(
    *,
    state: str = 'reserved',
    actual_grams: int | None = None,
    completed_at: str | None = None,
    open_conflict_id: str | None = None,
) -> dict[str, object]:
    return {
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
        'open_conflict_id': open_conflict_id,
    }


def valid_balance_payload() -> dict[str, object]:
    return {
        'lot_id': LOT_UUID.hex,
        'on_hand_grams': 8_750,
        'reserved_grams': 0,
        'available_grams': 8_750,
        'unresolved_conflict_count': 0,
    }


def valid_conflict_payload() -> dict[str, object]:
    return {
        'conflict_id': CONFLICT_UUID.hex,
        'lot_id': LOT_UUID.hex,
        'source_ledger_entry_id': LEDGER_UUID.hex,
        'roast_uuid': ROAST_UUID.hex,
        'reservation_id': SERVER_RESERVATION_UUID.hex,
        'trigger_operation': 'consumption',
        'available_grams_snapshot': -125,
        'state': 'open',
        'resolution_note': None,
        'resolved_by_user_id': None,
        'resolved_at': None,
        'created_at': '2026-08-05T12:01:00.000000Z',
    }


def valid_mutation_payload(
    *,
    state: str = 'reserved',
    actual_grams: int | None = None,
    completed_at: str | None = None,
    conflict: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        'reservation': valid_reservation_payload(
            state=state,
            actual_grams=actual_grams,
            completed_at=completed_at,
            open_conflict_id=(CONFLICT_UUID.hex if conflict is not None else None),
        ),
        'balance': valid_balance_payload(),
        'conflict': conflict,
        'idempotent_replay': False,
    }


def parse_mutation(
    payload: object,
    *,
    operation: InventoryOperation = 'reserve',
    requested_actual_grams: int | None = None,
) -> InventoryMutationResult:
    return parse_inventory_mutation(
        payload,
        operation=operation,
        expected_client_reservation_uuid=RESERVATION_UUID,
        expected_client_instance_uuid=CLIENT_UUID,
        expected_roast_uuid=ROAST_UUID,
        expected_lot_id=LOT_UUID,
        expected_planned_grams=1_250,
        requested_actual_grams=requested_actual_grams,
    )


def test_inventory_limits_and_failure_messages_are_fixed() -> None:
    assert MAX_INVENTORY_CURSOR_CHARS == 4096
    assert MAX_INVENTORY_PAGES == 100
    assert MAX_CACHED_LOTS == 10_000
    assert FAILURE_MESSAGES[FailureKind.INVENTORY_REJECTED] == 'Inventory operation rejected.'
    assert FAILURE_MESSAGES[FailureKind.INVENTORY_CONFLICT] == 'Inventory conflict requires review.'
    assert FAILURE_MESSAGES[FailureKind.INVENTORY_UNSUPPORTED] == 'Server does not support inventory.'
    assert FAILURE_MESSAGES[FailureKind.LOCAL_INVENTORY] == 'Inventory state could not be saved.'


@pytest.mark.parametrize('processing_method', PROCESSING_METHODS)
def test_bean_lot_page_accepts_every_processing_code_and_returns_immutable_values(
    processing_method: str,
) -> None:
    payload = valid_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    items[0]['processing_method'] = processing_method

    page = parse_bean_lot_page(payload)

    assert isinstance(page.items, tuple)
    assert isinstance(page.items[0], BeanLot)
    assert page.items[0].processing_method == processing_method
    assert page.items[0].varietals == ('Heirloom', '74110')
    items[0]['name'] = 'changed'
    assert page.items[0].name == 'Ethiopia Guji'


def test_bean_lot_page_accepts_nullable_descriptors_signed_balance_and_cursor_limit() -> None:
    payload = valid_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    lot = items[0]
    lot['origin'] = None
    lot['varietals'] = []
    lot['processing_method'] = None
    lot['crop_year'] = None
    lot['on_hand_grams'] = -POSTGRESQL_INTEGER_MAX
    lot['reserved_grams'] = 0
    lot['available_grams'] = -POSTGRESQL_INTEGER_MAX
    payload['next_cursor'] = 'x' * MAX_INVENTORY_CURSOR_CHARS

    page = parse_bean_lot_page(payload)

    assert page.items[0].available_grams == -POSTGRESQL_INTEGER_MAX
    assert page.next_cursor == 'x' * MAX_INVENTORY_CURSOR_CHARS


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('unexpected', 'value'),
        ('lot_id', str(LOT_UUID)),
        ('lot_id', f'a{LOT_UUID.hex[1:]}'.upper()),
        ('name', ''),
        ('name', '☕' * 201),
        ('origin', 'x' * 101),
        ('varietals', ['same', 'same']),
        ('varietals', [str(index) for index in range(17)]),
        ('varietals', ['x' * 101]),
        ('processing_method', 'semi-washed'),
        ('crop_year', 999),
        ('crop_year', True),
        ('on_hand_grams', True),
        ('on_hand_grams', POSTGRESQL_INTEGER_MAX + 1),
        ('reserved_grams', -1),
        ('reserved_grams', POSTGRESQL_INTEGER_MAX + 1),
        ('unresolved_conflict_count', -1),
    ),
)
def test_bean_lot_page_rejects_invalid_exact_fields(field: str, value: object) -> None:
    payload = valid_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    lot = items[0]
    if field == 'unexpected':
        lot[field] = value
    else:
        lot[field] = value

    with pytest.raises(ContractError, match='invalid server response'):
        parse_bean_lot_page(payload)


def test_bean_lot_page_rejects_bad_arithmetic_duplicate_ids_and_bad_cursor() -> None:
    payload = valid_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    items[0]['available_grams'] = 8_749
    with pytest.raises(ContractError):
        parse_bean_lot_page(payload)

    duplicate = valid_lot_payload()
    duplicate_payload: dict[str, object] = {
        'items': [valid_lot_payload(), duplicate],
        'next_cursor': None,
    }
    with pytest.raises(ContractError):
        parse_bean_lot_page(duplicate_payload)

    payload = valid_page_payload()
    payload['next_cursor'] = 'x' * (MAX_INVENTORY_CURSOR_CHARS + 1)
    with pytest.raises(ContractError):
        parse_bean_lot_page(payload)


def test_mutation_accepts_operation_specific_state_and_quantities() -> None:
    reserved = parse_mutation(valid_mutation_payload())
    assert reserved.reservation.state == 'reserved'

    completed = '2026-08-05T12:01:00.000000Z'
    finalized = parse_mutation(
        valid_mutation_payload(
            state='finalized', actual_grams=1_100, completed_at=completed
        ),
        operation='finalize',
        requested_actual_grams=1_100,
    )
    assert finalized.reservation.actual_grams == 1_100

    planned = parse_mutation(
        valid_mutation_payload(
            state='finalized', actual_grams=1_250, completed_at=completed
        ),
        operation='finalize',
    )
    assert planned.reservation.actual_grams == 1_250

    released = parse_mutation(
        valid_mutation_payload(state='released', completed_at=completed),
        operation='release',
    )
    assert released.reservation.state == 'released'


def test_mutation_requires_exact_fields_canonical_timestamp_bool_and_relationships() -> None:
    payload = valid_mutation_payload()
    payload['extra'] = None
    with pytest.raises(ContractError):
        parse_mutation(payload)

    payload = valid_mutation_payload()
    payload['idempotent_replay'] = 1
    with pytest.raises(ContractError):
        parse_mutation(payload)

    for invalid_timestamp in (
        '2026-08-05T12:00:01Z',
        '2026-08-05T12:00:01.000000+00:00',
        '2026-08-05T05:00:01.000000-07:00',
    ):
        payload = valid_mutation_payload()
        reservation = payload['reservation']
        assert isinstance(reservation, dict)
        reservation['reserved_at'] = invalid_timestamp
        with pytest.raises(ContractError):
            parse_mutation(payload)

    identity_fields = (
        ('client_reservation_uuid', UUID(int=1).hex),
        ('client_instance_uuid', UUID(int=2).hex),
        ('roast_uuid', UUID(int=3).hex),
        ('lot_id', UUID(int=4).hex),
        ('planned_grams', 1_249),
    )
    for field, value in identity_fields:
        payload = valid_mutation_payload()
        reservation = payload['reservation']
        assert isinstance(reservation, dict)
        reservation[field] = value
        with pytest.raises(ContractError):
            parse_mutation(payload)

    payload = valid_mutation_payload()
    balance = payload['balance']
    assert isinstance(balance, dict)
    balance['lot_id'] = UUID(int=5).hex
    with pytest.raises(ContractError):
        parse_mutation(payload)


def test_mutation_rejects_invalid_state_quantity_and_timestamp_relationships() -> None:
    completed = '2026-08-05T12:01:00.000000Z'
    invalid_cases = (
        (valid_mutation_payload(state='finalized', actual_grams=1_250, completed_at=completed), 'reserve', None),
        (valid_mutation_payload(actual_grams=1_250), 'reserve', None),
        (valid_mutation_payload(completed_at=completed), 'reserve', None),
        (valid_mutation_payload(state='finalized', actual_grams=1_099, completed_at=completed), 'finalize', 1_100),
        (valid_mutation_payload(state='finalized', actual_grams=1_249, completed_at=completed), 'finalize', None),
        (valid_mutation_payload(state='released', actual_grams=1, completed_at=completed), 'release', None),
        (valid_mutation_payload(state='released'), 'release', None),
    )
    for payload, operation, requested_actual in invalid_cases:
        with pytest.raises(ContractError):
            parse_mutation(
                payload,
                operation=operation,  # type: ignore[arg-type]
                requested_actual_grams=requested_actual,
            )

    payload = valid_mutation_payload(
        state='released', completed_at='2026-08-05T11:59:59.000000Z'
    )
    with pytest.raises(ContractError):
        parse_mutation(payload, operation='release')


def test_mutation_validates_balance_arithmetic_and_numeric_bounds() -> None:
    for field, value in (
        ('on_hand_grams', True),
        ('on_hand_grams', POSTGRESQL_INTEGER_MAX + 1),
        ('reserved_grams', -1),
        ('available_grams', 8_749),
        ('unresolved_conflict_count', -1),
    ):
        payload = valid_mutation_payload()
        balance = payload['balance']
        assert isinstance(balance, dict)
        balance[field] = value
        with pytest.raises(ContractError):
            parse_mutation(payload)


def test_mutation_validates_open_conflict_relationships() -> None:
    completed = '2026-08-05T12:01:00.000000Z'
    conflict = valid_conflict_payload()
    result = parse_mutation(
        valid_mutation_payload(
            state='finalized',
            actual_grams=1_100,
            completed_at=completed,
            conflict=conflict,
        ),
        operation='finalize',
        requested_actual_grams=1_100,
    )
    assert result.conflict is not None
    assert result.conflict.conflict_id == CONFLICT_UUID

    invalid_fields: tuple[tuple[str, object], ...] = (
        ('state', 'resolved'),
        ('lot_id', UUID(int=1).hex),
        ('roast_uuid', UUID(int=2).hex),
        ('reservation_id', UUID(int=3).hex),
        ('trigger_operation', 'reservation'),
        ('resolution_note', 'secret'),
    )
    for field, value in invalid_fields:
        bad_conflict = valid_conflict_payload()
        bad_conflict[field] = value
        payload = valid_mutation_payload(
            state='finalized',
            actual_grams=1_100,
            completed_at=completed,
            conflict=bad_conflict,
        )
        with pytest.raises(ContractError):
            parse_mutation(
                payload,
                operation='finalize',
                requested_actual_grams=1_100,
            )

    payload = valid_mutation_payload()
    reservation = payload['reservation']
    assert isinstance(reservation, dict)
    reservation['open_conflict_id'] = CONFLICT_UUID.hex
    with pytest.raises(ContractError):
        parse_mutation(payload)


def test_profile_link_is_all_or_none_canonical_and_namespace_bound() -> None:
    fields = {
        'roastServerInventoryOrigin': 'https://inventory.example.test',
        'roastServerInventoryOrganizationUUID': ORGANIZATION_UUID.hex,
        'roastServerBeanLotUUID': LOT_UUID.hex,
        'roastServerBeanLotName': 'Ethiopia Guji',
        'unrelated': 'preserved',
    }
    link = parse_profile_link(MappingProxyType(fields))
    assert isinstance(link, InventoryProfileLink)
    assert link == InventoryProfileLink(
        namespace=namespace_for('https://inventory.example.test', ORGANIZATION_UUID),
        lot_id=LOT_UUID,
        lot_name='Ethiopia Guji',
    )
    assert profile_link_fields(link) == {
        key: value for key, value in fields.items() if key != 'unrelated'
    }
    assert parse_profile_link({'unrelated': True}) is None
    assert parse_profile_link(
        {
            'roastServerInventoryOrigin': None,
            'roastServerInventoryOrganizationUUID': None,
            'roastServerBeanLotUUID': None,
            'roastServerBeanLotName': None,
        }
    ) is None

    mismatched = InventoryProfileLink(
        namespace=Namespace(
            origin=link.namespace.origin,
            organization_id=link.namespace.organization_id,
            key='namespace-sha256:not-the-matching-key',
        ),
        lot_id=link.lot_id,
        lot_name=link.lot_name,
    )
    with pytest.raises(ValueError, match='invalid inventory profile link'):
        profile_link_fields(mismatched)


@pytest.mark.parametrize(
    'updates',
    (
        {'roastServerBeanLotName': None},
        {'roastServerBeanLotName': ''},
        {'roastServerInventoryOrigin': 'https://inventory.example.test/'},
        {'roastServerInventoryOrganizationUUID': str(ORGANIZATION_UUID)},
        {'roastServerBeanLotUUID': f'a{LOT_UUID.hex[1:]}'.upper()},
        {'roastServerBeanLotName': '☕' * 201},
    ),
)
def test_profile_link_rejects_incomplete_malformed_or_noncanonical_values(
    updates: dict[str, object],
) -> None:
    fields: dict[str, object] = {
        'roastServerInventoryOrigin': 'https://inventory.example.test',
        'roastServerInventoryOrganizationUUID': ORGANIZATION_UUID.hex,
        'roastServerBeanLotUUID': LOT_UUID.hex,
        'roastServerBeanLotName': 'Ethiopia Guji',
    }
    fields.update(updates)
    with pytest.raises(ContractError):
        parse_profile_link(fields)


def test_green_weight_supports_every_unit_and_rounds_half_up() -> None:
    assert green_weight_grams(1, 'g') == 1
    assert green_weight_grams(1.0005, 'Kg') == 1001
    assert green_weight_grams(1, 'lb') == 454
    assert green_weight_grams(1, 'oz') == 28
    assert green_weight_grams(0.0004, 'Kg') is None


@pytest.mark.parametrize(
    ('value', 'unit'),
    (
        (True, 'g'),
        ('1', 'g'),
        (0, 'g'),
        (-1, 'g'),
        (math.inf, 'g'),
        (math.nan, 'g'),
        (1, 'kg'),
        (POSTGRESQL_INTEGER_MAX + 1, 'g'),
        (10**4000, 'g'),
    ),
)
def test_green_weight_rejects_wrong_type_zero_nonfinite_unit_and_overflow(
    value: object, unit: object
) -> None:
    assert green_weight_grams(value, unit) is None


def test_build_reserve_request_is_canonical_and_stable() -> None:
    request = build_reserve_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1250,
        occurred_at=NOW,
    )
    assert isinstance(request, InventoryCommandRequest)
    assert isinstance(request, ExportedInventoryCommandRequest)
    assert request.idempotency_key == (
        f'inventory-v1:{CLIENT_UUID.hex}:{RESERVATION_UUID.hex}:reserve'
    )
    assert request.request_json == (
        b'{"client_instance_uuid":"33333333333343338333333333333333",'
        b'"client_reservation_uuid":"44444444444444448444444444444444",'
        b'"lot_id":"22222222222242228222222222222222",'
        b'"occurred_at":"2026-08-05T12:00:00.000000Z",'
        b'"planned_grams":1250,'
        b'"roast_uuid":"11111111111141118111111111111111"}'
    )
    assert request.client_instance_uuid == CLIENT_UUID
    assert request.planned_grams == 1250
    assert request.requested_actual_grams is None
    with pytest.raises((AttributeError, TypeError)):
        request.request_json = b'changed'  # type: ignore[misc]


def test_build_finalize_and_release_requests_have_exact_payloads() -> None:
    finalized = build_finalize_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1_250,
        actual_grams=1_100,
        occurred_at=NOW,
    )
    assert finalized.request_json == (
        b'{"actual_grams":1100,"occurred_at":"2026-08-05T12:00:00.000000Z"}'
    )
    assert finalized.requested_actual_grams == 1_100
    assert finalized.idempotency_key.endswith(':finalize')

    planned = build_finalize_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1_250,
        actual_grams=None,
        occurred_at=NOW,
    )
    assert planned.request_json == b'{"occurred_at":"2026-08-05T12:00:00.000000Z"}'

    released = build_release_request(
        client_instance_uuid=CLIENT_UUID,
        reservation_uuid=RESERVATION_UUID,
        roast_uuid=ROAST_UUID,
        lot_id=LOT_UUID,
        planned_grams=1_250,
        occurred_at=NOW,
    )
    assert released.request_json == b'{"occurred_at":"2026-08-05T12:00:00.000000Z"}'
    assert released.idempotency_key.endswith(':release')


@pytest.mark.parametrize(
    'kwargs',
    (
        {'client_instance_uuid': 'not-uuid'},
        {'planned_grams': True},
        {'planned_grams': 0},
        {'planned_grams': POSTGRESQL_INTEGER_MAX + 1},
        {'occurred_at': NOW.replace(tzinfo=None)},
        {'occurred_at': NOW.astimezone(timezone(timedelta(hours=1)))},
    ),
)
def test_builders_reject_invalid_arguments(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        'client_instance_uuid': CLIENT_UUID,
        'reservation_uuid': RESERVATION_UUID,
        'roast_uuid': ROAST_UUID,
        'lot_id': LOT_UUID,
        'planned_grams': 1_250,
        'occurred_at': NOW,
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        build_reserve_request(**arguments)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        build_finalize_request(
            client_instance_uuid=CLIENT_UUID,
            reservation_uuid=RESERVATION_UUID,
            roast_uuid=ROAST_UUID,
            lot_id=LOT_UUID,
            planned_grams=1_250,
            actual_grams=0,
            occurred_at=NOW,
        )


def error_body(code: str, message: str, *, details: object = None) -> bytes:
    return json.dumps(
        {'error': {'code': code, 'message': message, 'details': details}},
        separators=(',', ':'),
    ).encode()


@pytest.mark.parametrize(
    ('status', 'code', 'message', 'kind', 'retryable'),
    (
        (404, 'bean_lot_not_found', 'Bean lot not found', FailureKind.INVENTORY_REJECTED, False),
        (409, 'bean_lot_archived', 'Bean lot archived', FailureKind.INVENTORY_REJECTED, False),
        (409, 'invalid_inventory_transition', 'Invalid inventory transition', FailureKind.INVENTORY_REJECTED, False),
        (409, 'inventory_idempotency_conflict', 'Idempotency key conflicts with an earlier request', FailureKind.INVENTORY_CONFLICT, False),
        (404, 'inventory_reservation_not_found', 'Inventory reservation not found', FailureKind.INVENTORY_REJECTED, False),
        (503, 'inventory_unavailable', 'Inventory unavailable', FailureKind.OFFLINE, True),
        (422, 'invalid_request', 'Invalid request', FailureKind.INVENTORY_REJECTED, False),
    ),
)
def test_inventory_error_accepts_only_pinned_triples(
    status: int,
    code: str,
    message: str,
    kind: FailureKind,
    retryable: bool,
) -> None:
    failure = parse_inventory_error(status, error_body(code, message))
    assert failure is not None
    assert failure.kind is kind
    assert failure.code == code
    assert failure.message == message
    assert failure.retryable is retryable

    assert parse_inventory_error(status + 1, error_body(code, message)) is None
    assert parse_inventory_error(status, error_body(code, message + '.')) is None
    assert parse_inventory_error(status, error_body(code, message, details={})) is None


@pytest.mark.parametrize(
    'body',
    (
        b'<html>no</html>',
        b'{"error":{"code":"invalid_request","message":"Invalid request"}}',
        b'{"error":{"code":"invalid_request","message":"Invalid request","details":null},"trace":"secret"}',
        b'{"error":{"code":"invalid_request","code":"other","message":"Invalid request","details":null}}',
    ),
)
def test_inventory_error_discards_arbitrary_or_nonexact_bodies(body: bytes) -> None:
    assert parse_inventory_error(422, body) is None
