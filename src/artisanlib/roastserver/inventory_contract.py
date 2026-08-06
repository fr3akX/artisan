#
# ABOUT
# Artisan Roast Server inventory connector contracts
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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import math
import re
from typing import Final, Literal, NoReturn, cast
from uuid import UUID

from artisanlib.roastserver.contract import (
    POSTGRESQL_INTEGER_MAX,
    ContractError,
    FailureKind,
    Namespace,
    PublicFailure,
    _exact_object,
    _parse_bool,
    _parse_hex_uuid,
    _parse_required_string,
    _parse_safe_int,
    parse_error_envelope,
)
from artisanlib.roastserver.origin import SettingsError, canonical_origin

MAX_INVENTORY_CURSOR_CHARS: Final[int] = 4096
MAX_INVENTORY_PAGES: Final[int] = 100
MAX_CACHED_LOTS: Final[int] = 10_000

_MAX_LOTS_PER_PAGE: Final[int] = 100
_MAX_LOT_NAME_CODE_POINTS: Final[int] = 200
_MAX_LOT_NAME_BYTES: Final[int] = 800
_MAX_DESCRIPTOR_CODE_POINTS: Final[int] = 100
_MAX_DESCRIPTOR_BYTES: Final[int] = 400
_MAX_VARIETALS: Final[int] = 16
_MIN_SIGNED_GRAMS: Final[int] = -POSTGRESQL_INTEGER_MAX
_GRAMS_PER_WEIGHT_UNIT: Final[dict[str, float]] = {
    'g': 1.0,
    'Kg': 1000.0,
    'lb': 1 / (2.20462262185 / 1000),
    'oz': 1000 / (2.20462262185 * 16),
}
_CANONICAL_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$'
)
_PROCESSING_METHOD_VALUES: Final[frozenset[str]] = frozenset(
    {
        'washed',
        'natural',
        'honey',
        'pulped-natural',
        'wet-hulled',
        'anaerobic',
        'experimental',
        'other',
    }
)
_INVENTORY_OPERATION_VALUES: Final[frozenset[str]] = frozenset(
    {'reserve', 'finalize', 'release'}
)
_RESERVATION_STATE_VALUES: Final[frozenset[str]] = frozenset(
    {'reserved', 'finalized', 'released'}
)
_LEDGER_OPERATION_VALUES: Final[frozenset[str]] = frozenset(
    {
        'opening_balance',
        'manual_adjustment',
        'reservation',
        'reservation_release',
        'consumption',
    }
)
_CONFLICT_STATE_VALUES: Final[frozenset[str]] = frozenset({'open', 'resolved'})
_PROFILE_LINK_KEYS: Final[tuple[str, str, str, str]] = (
    'roastServerInventoryOrigin',
    'roastServerInventoryOrganizationUUID',
    'roastServerBeanLotUUID',
    'roastServerBeanLotName',
)

ProcessingMethod = Literal[
    'washed',
    'natural',
    'honey',
    'pulped-natural',
    'wet-hulled',
    'anaerobic',
    'experimental',
    'other',
]
InventoryOperation = Literal['reserve', 'finalize', 'release']
ReservationState = Literal['reserved', 'finalized', 'released']
LedgerOperation = Literal[
    'opening_balance',
    'manual_adjustment',
    'reservation',
    'reservation_release',
    'consumption',
]

@dataclass(frozen=True, slots=True)
class InventoryProfileLink:
    namespace: Namespace
    lot_id: UUID
    lot_name: str


@dataclass(frozen=True, slots=True)
class BeanLot:
    lot_id: UUID
    name: str
    origin: str | None
    varietals: tuple[str, ...]
    processing_method: ProcessingMethod | None
    crop_year: int | None
    on_hand_grams: int
    reserved_grams: int
    available_grams: int
    unresolved_conflict_count: int


@dataclass(frozen=True, slots=True)
class InventoryBalance:
    lot_id: UUID
    on_hand_grams: int
    reserved_grams: int
    available_grams: int
    unresolved_conflict_count: int


@dataclass(frozen=True, slots=True)
class InventoryReservation:
    reservation_id: UUID
    client_reservation_uuid: UUID
    lot_id: UUID
    roast_uuid: UUID
    client_instance_uuid: UUID
    state: ReservationState
    planned_grams: int
    actual_grams: int | None
    reserved_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    open_conflict_id: UUID | None


@dataclass(frozen=True, slots=True)
class InventoryConflict:
    conflict_id: UUID
    lot_id: UUID
    source_ledger_entry_id: UUID
    roast_uuid: UUID | None
    reservation_id: UUID | None
    trigger_operation: LedgerOperation
    available_grams_snapshot: int
    state: Literal['open', 'resolved']
    resolution_note: str | None
    resolved_by_user_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BeanLotPage:
    items: tuple[BeanLot, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class InventoryMutationResult:
    reservation: InventoryReservation
    balance: InventoryBalance
    conflict: InventoryConflict | None
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class InventoryCommandRequest:
    operation: InventoryOperation
    reservation_uuid: UUID
    roast_uuid: UUID
    lot_id: UUID
    request_json: bytes
    idempotency_key: str
    occurred_at: datetime
    client_instance_uuid: UUID
    planned_grams: int
    requested_actual_grams: int | None


def _fail() -> NoReturn:
    raise ContractError


def _parse_bounded_text(
    value: object,
    *,
    max_code_points: int,
    max_bytes: int,
) -> str:
    text = _parse_required_string(
        value,
        max_length=max_code_points,
        reject_controls=True,
    )
    try:
        encoded = text.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ContractError from exc
    if len(encoded) > max_bytes:
        _fail()
    return text


def _parse_optional_bounded_text(
    value: object,
    *,
    max_code_points: int,
    max_bytes: int,
) -> str | None:
    if value is None:
        return None
    return _parse_bounded_text(
        value,
        max_code_points=max_code_points,
        max_bytes=max_bytes,
    )


def _parse_canonical_timestamp(value: object) -> datetime:
    text = _parse_required_string(value)
    if _CANONICAL_TIMESTAMP_RE.fullmatch(text) is None:
        _fail()
    try:
        parsed = datetime.fromisoformat(f'{text[:-1]}+00:00')
    except ValueError as exc:
        raise ContractError from exc
    return parsed


def _parse_optional_canonical_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_canonical_timestamp(value)


def _parse_optional_hex_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return _parse_hex_uuid(value)


def _parse_signed_grams(value: object) -> int:
    return _parse_safe_int(
        value,
        minimum=_MIN_SIGNED_GRAMS,
        maximum=POSTGRESQL_INTEGER_MAX,
    )


def _parse_nonnegative_int(value: object) -> int:
    return _parse_safe_int(value, minimum=0, maximum=POSTGRESQL_INTEGER_MAX)


def _parse_positive_grams(value: object) -> int:
    return _parse_safe_int(value, minimum=1, maximum=POSTGRESQL_INTEGER_MAX)


def _parse_optional_positive_grams(value: object) -> int | None:
    if value is None:
        return None
    return _parse_positive_grams(value)


def _parse_processing_method(value: object) -> ProcessingMethod | None:
    if value is None:
        return None
    text = _parse_required_string(value)
    if text not in _PROCESSING_METHOD_VALUES:
        _fail()
    return cast(ProcessingMethod, text)


def _parse_reservation_state(value: object) -> ReservationState:
    text = _parse_required_string(value)
    if text not in _RESERVATION_STATE_VALUES:
        _fail()
    return cast(ReservationState, text)


def _parse_ledger_operation(value: object) -> LedgerOperation:
    text = _parse_required_string(value)
    if text not in _LEDGER_OPERATION_VALUES:
        _fail()
    return cast(LedgerOperation, text)


def _parse_conflict_state(value: object) -> Literal['open', 'resolved']:
    text = _parse_required_string(value)
    if text not in _CONFLICT_STATE_VALUES:
        _fail()
    return cast(Literal['open', 'resolved'], text)


def _parse_varietals(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail()
    raw_varietals = cast(list[object], value)
    if len(raw_varietals) > _MAX_VARIETALS:
        _fail()
    varietals = tuple(
        _parse_bounded_text(
            varietal,
            max_code_points=_MAX_DESCRIPTOR_CODE_POINTS,
            max_bytes=_MAX_DESCRIPTOR_BYTES,
        )
        for varietal in raw_varietals
    )
    if len(set(varietals)) != len(varietals):
        _fail()
    return varietals


def _validate_balance_arithmetic(
    on_hand_grams: int,
    reserved_grams: int,
    available_grams: int,
) -> None:
    if available_grams != on_hand_grams - reserved_grams:
        _fail()


def _parse_bean_lot(value: object) -> BeanLot:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'lot_id',
                'name',
                'origin',
                'varietals',
                'processing_method',
                'crop_year',
                'on_hand_grams',
                'reserved_grams',
                'available_grams',
                'unresolved_conflict_count',
            }
        ),
    )
    crop_year_value = mapping['crop_year']
    crop_year = (
        None
        if crop_year_value is None
        else _parse_safe_int(crop_year_value, minimum=1000, maximum=9999)
    )
    on_hand_grams = _parse_signed_grams(mapping['on_hand_grams'])
    reserved_grams = _parse_nonnegative_int(mapping['reserved_grams'])
    available_grams = _parse_signed_grams(mapping['available_grams'])
    _validate_balance_arithmetic(on_hand_grams, reserved_grams, available_grams)
    return BeanLot(
        lot_id=_parse_hex_uuid(mapping['lot_id']),
        name=_parse_bounded_text(
            mapping['name'],
            max_code_points=_MAX_LOT_NAME_CODE_POINTS,
            max_bytes=_MAX_LOT_NAME_BYTES,
        ),
        origin=_parse_optional_bounded_text(
            mapping['origin'],
            max_code_points=_MAX_DESCRIPTOR_CODE_POINTS,
            max_bytes=_MAX_DESCRIPTOR_BYTES,
        ),
        varietals=_parse_varietals(mapping['varietals']),
        processing_method=_parse_processing_method(mapping['processing_method']),
        crop_year=crop_year,
        on_hand_grams=on_hand_grams,
        reserved_grams=reserved_grams,
        available_grams=available_grams,
        unresolved_conflict_count=_parse_nonnegative_int(
            mapping['unresolved_conflict_count']
        ),
    )


def _parse_inventory_balance(value: object) -> InventoryBalance:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'lot_id',
                'on_hand_grams',
                'reserved_grams',
                'available_grams',
                'unresolved_conflict_count',
            }
        ),
    )
    on_hand_grams = _parse_signed_grams(mapping['on_hand_grams'])
    reserved_grams = _parse_nonnegative_int(mapping['reserved_grams'])
    available_grams = _parse_signed_grams(mapping['available_grams'])
    _validate_balance_arithmetic(on_hand_grams, reserved_grams, available_grams)
    return InventoryBalance(
        lot_id=_parse_hex_uuid(mapping['lot_id']),
        on_hand_grams=on_hand_grams,
        reserved_grams=reserved_grams,
        available_grams=available_grams,
        unresolved_conflict_count=_parse_nonnegative_int(
            mapping['unresolved_conflict_count']
        ),
    )


def _parse_inventory_reservation(value: object) -> InventoryReservation:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'reservation_id',
                'client_reservation_uuid',
                'lot_id',
                'roast_uuid',
                'client_instance_uuid',
                'state',
                'planned_grams',
                'actual_grams',
                'reserved_at',
                'completed_at',
                'created_at',
                'updated_at',
                'open_conflict_id',
            }
        ),
    )
    state = _parse_reservation_state(mapping['state'])
    reserved_at = _parse_canonical_timestamp(mapping['reserved_at'])
    completed_at = _parse_optional_canonical_timestamp(mapping['completed_at'])
    created_at = _parse_canonical_timestamp(mapping['created_at'])
    updated_at = _parse_canonical_timestamp(mapping['updated_at'])
    if created_at > updated_at:
        _fail()
    if completed_at is not None and completed_at < reserved_at:
        _fail()
    if (state == 'reserved') != (completed_at is None):
        _fail()
    return InventoryReservation(
        reservation_id=_parse_hex_uuid(mapping['reservation_id']),
        client_reservation_uuid=_parse_hex_uuid(mapping['client_reservation_uuid']),
        lot_id=_parse_hex_uuid(mapping['lot_id']),
        roast_uuid=_parse_hex_uuid(mapping['roast_uuid']),
        client_instance_uuid=_parse_hex_uuid(mapping['client_instance_uuid']),
        state=state,
        planned_grams=_parse_positive_grams(mapping['planned_grams']),
        actual_grams=_parse_optional_positive_grams(mapping['actual_grams']),
        reserved_at=reserved_at,
        completed_at=completed_at,
        created_at=created_at,
        updated_at=updated_at,
        open_conflict_id=_parse_optional_hex_uuid(mapping['open_conflict_id']),
    )


def _parse_inventory_conflict(value: object) -> InventoryConflict:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'conflict_id',
                'lot_id',
                'source_ledger_entry_id',
                'roast_uuid',
                'reservation_id',
                'trigger_operation',
                'available_grams_snapshot',
                'state',
                'resolution_note',
                'resolved_by_user_id',
                'resolved_at',
                'created_at',
            }
        ),
    )
    state = _parse_conflict_state(mapping['state'])
    resolution_note = _parse_optional_bounded_text(
        mapping['resolution_note'],
        max_code_points=500,
        max_bytes=2000,
    )
    resolved_by_user_id = _parse_optional_hex_uuid(mapping['resolved_by_user_id'])
    resolved_at = _parse_optional_canonical_timestamp(mapping['resolved_at'])
    created_at = _parse_canonical_timestamp(mapping['created_at'])
    if state == 'open':
        if (
            resolution_note is not None
            or resolved_by_user_id is not None
            or resolved_at is not None
        ):
            _fail()
    elif resolved_by_user_id is None or resolved_at is None or resolved_at < created_at:
        _fail()
    return InventoryConflict(
        conflict_id=_parse_hex_uuid(mapping['conflict_id']),
        lot_id=_parse_hex_uuid(mapping['lot_id']),
        source_ledger_entry_id=_parse_hex_uuid(mapping['source_ledger_entry_id']),
        roast_uuid=_parse_optional_hex_uuid(mapping['roast_uuid']),
        reservation_id=_parse_optional_hex_uuid(mapping['reservation_id']),
        trigger_operation=_parse_ledger_operation(mapping['trigger_operation']),
        available_grams_snapshot=_parse_signed_grams(
            mapping['available_grams_snapshot']
        ),
        state=state,
        resolution_note=resolution_note,
        resolved_by_user_id=resolved_by_user_id,
        resolved_at=resolved_at,
        created_at=created_at,
    )


def parse_bean_lot_page(value: object) -> BeanLotPage:
    mapping = _exact_object(value, frozenset({'items', 'next_cursor'}))
    items_value = mapping['items']
    if not isinstance(items_value, list):
        _fail()
    raw_items = cast(list[object], items_value)
    if len(raw_items) > _MAX_LOTS_PER_PAGE:
        _fail()
    items = tuple(_parse_bean_lot(item) for item in raw_items)
    lot_ids = {item.lot_id for item in items}
    if len(lot_ids) != len(items):
        _fail()
    cursor_value = mapping['next_cursor']
    next_cursor = (
        None
        if cursor_value is None
        else _parse_required_string(
            cursor_value,
            max_length=MAX_INVENTORY_CURSOR_CHARS,
            reject_controls=True,
        )
    )
    return BeanLotPage(items=items, next_cursor=next_cursor)


def parse_inventory_mutation(
    value: object,
    *,
    operation: InventoryOperation,
    expected_client_reservation_uuid: UUID,
    expected_client_instance_uuid: UUID,
    expected_roast_uuid: UUID,
    expected_lot_id: UUID,
    expected_planned_grams: int,
    requested_actual_grams: int | None,
) -> InventoryMutationResult:
    if operation not in _INVENTORY_OPERATION_VALUES:
        _fail()
    mapping = _exact_object(
        value,
        frozenset({'reservation', 'balance', 'conflict', 'idempotent_replay'}),
    )
    reservation = _parse_inventory_reservation(mapping['reservation'])
    balance = _parse_inventory_balance(mapping['balance'])
    conflict_value = mapping['conflict']
    conflict = (
        None if conflict_value is None else _parse_inventory_conflict(conflict_value)
    )
    expected_state: ReservationState
    expected_actual_grams: int | None
    expects_completion: bool
    if operation == 'reserve':
        expected_state = 'reserved'
        expected_actual_grams = None
        expects_completion = False
    elif operation == 'finalize':
        expected_state = 'finalized'
        expected_actual_grams = (
            expected_planned_grams
            if requested_actual_grams is None
            else requested_actual_grams
        )
        expects_completion = True
    else:
        expected_state = 'released'
        expected_actual_grams = None
        expects_completion = True
    if (
        reservation.client_reservation_uuid != expected_client_reservation_uuid
        or reservation.client_instance_uuid != expected_client_instance_uuid
        or reservation.roast_uuid != expected_roast_uuid
        or reservation.lot_id != expected_lot_id
        or reservation.planned_grams != expected_planned_grams
        or reservation.state != expected_state
        or reservation.actual_grams != expected_actual_grams
        or (reservation.completed_at is not None) != expects_completion
        or balance.lot_id != expected_lot_id
    ):
        _fail()
    if conflict is None:
        if reservation.open_conflict_id is not None:
            _fail()
    elif (
        conflict.state != 'open'
        or reservation.open_conflict_id != conflict.conflict_id
        or conflict.lot_id != expected_lot_id
        or conflict.roast_uuid != expected_roast_uuid
        or conflict.reservation_id != reservation.reservation_id
    ):
        _fail()
    return InventoryMutationResult(
        reservation=reservation,
        balance=balance,
        conflict=conflict,
        idempotent_replay=_parse_bool(mapping['idempotent_replay']),
    )


def parse_inventory_error(status: int, value: object) -> PublicFailure | None:
    error = parse_error_envelope(value)
    if error is None or error.details is not None:
        return None
    triple = (status, error.code, error.message)
    classification = _INVENTORY_ERROR_CLASSIFICATIONS.get(triple)
    if classification is None:
        return None
    kind, retryable = classification
    return PublicFailure(
        kind=kind,
        code=error.code,
        message=error.message,
        retryable=retryable,
    )


def parse_profile_link(value: Mapping[str, object]) -> InventoryProfileLink | None:
    from artisanlib.roastserver.settings import namespace_for

    raw_values = tuple(value.get(key) for key in _PROFILE_LINK_KEYS)
    if all(item is None for item in raw_values):
        return None
    if any(item is None for item in raw_values):
        _fail()
    origin_value, organization_value, lot_value, lot_name_value = raw_values
    origin = _parse_required_string(origin_value, reject_controls=True)
    try:
        canonical = canonical_origin(origin)
    except SettingsError as exc:
        raise ContractError from exc
    if origin != canonical:
        _fail()
    organization_id = _parse_hex_uuid(organization_value)
    lot_id = _parse_hex_uuid(lot_value)
    lot_name = _parse_bounded_text(
        lot_name_value,
        max_code_points=_MAX_LOT_NAME_CODE_POINTS,
        max_bytes=_MAX_LOT_NAME_BYTES,
    )
    return InventoryProfileLink(
        namespace=namespace_for(canonical, organization_id),
        lot_id=lot_id,
        lot_name=lot_name,
    )


def profile_link_fields(link: InventoryProfileLink) -> dict[str, str]:
    from artisanlib.roastserver.settings import namespace_for

    if not isinstance(link, InventoryProfileLink):
        raise ValueError('invalid inventory profile link')
    expected_namespace = namespace_for(
        link.namespace.origin,
        link.namespace.organization_id,
    )
    if link.namespace != expected_namespace:
        raise ValueError('invalid inventory profile link')
    try:
        lot_name = _parse_bounded_text(
            link.lot_name,
            max_code_points=_MAX_LOT_NAME_CODE_POINTS,
            max_bytes=_MAX_LOT_NAME_BYTES,
        )
    except ContractError:
        raise ValueError('invalid inventory profile link') from None
    return {
        'roastServerInventoryOrigin': link.namespace.origin,
        'roastServerInventoryOrganizationUUID': link.namespace.organization_id.hex,
        'roastServerBeanLotUUID': link.lot_id.hex,
        'roastServerBeanLotName': lot_name,
    }


def green_weight_grams(value: object, unit: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if (
        not math.isfinite(number)
        or number <= 0
        or not isinstance(unit, str)
        or unit not in _GRAMS_PER_WEIGHT_UNIT
    ):
        return None
    grams = number * _GRAMS_PER_WEIGHT_UNIT[unit]
    try:
        rounded = int(
            Decimal(str(grams)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        )
    except InvalidOperation:
        return None
    return rounded if 1 <= rounded <= POSTGRESQL_INTEGER_MAX else None


def _validate_builder_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError('invalid inventory command argument')
    return value


def _validate_builder_grams(value: object, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= POSTGRESQL_INTEGER_MAX
    ):
        raise ValueError('invalid inventory command argument')
    return value


def _validate_builder_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('invalid inventory command argument')
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        raise ValueError('invalid inventory command argument') from None
    if offset != timedelta(0):
        raise ValueError('invalid inventory command argument')
    return value


def _timestamp_text(value: datetime) -> str:
    return (
        f'{value.year:04d}-{value.month:02d}-{value.day:02d}'
        f'T{value.hour:02d}:{value.minute:02d}:{value.second:02d}.{value.microsecond:06d}Z'
    )


def _request_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')


def _build_request(
    *,
    operation: InventoryOperation,
    client_instance_uuid: object,
    reservation_uuid: object,
    roast_uuid: object,
    lot_id: object,
    planned_grams: object,
    actual_grams: object,
    occurred_at: object,
) -> InventoryCommandRequest:
    client_uuid = _validate_builder_uuid(client_instance_uuid)
    reservation = _validate_builder_uuid(reservation_uuid)
    roast = _validate_builder_uuid(roast_uuid)
    lot = _validate_builder_uuid(lot_id)
    planned = _validate_builder_grams(planned_grams)
    actual = _validate_builder_grams(actual_grams, optional=True)
    occurred = _validate_builder_timestamp(occurred_at)
    assert isinstance(planned, int)
    payload: dict[str, object]
    if operation == 'reserve':
        if actual is not None:
            raise ValueError('invalid inventory command argument')
        payload = {
            'client_reservation_uuid': reservation.hex,
            'client_instance_uuid': client_uuid.hex,
            'roast_uuid': roast.hex,
            'lot_id': lot.hex,
            'planned_grams': planned,
            'occurred_at': _timestamp_text(occurred),
        }
    else:
        payload = {'occurred_at': _timestamp_text(occurred)}
        if operation == 'finalize' and actual is not None:
            payload['actual_grams'] = actual
    return InventoryCommandRequest(
        operation=operation,
        reservation_uuid=reservation,
        roast_uuid=roast,
        lot_id=lot,
        request_json=_request_json(payload),
        idempotency_key=(
            f'inventory-v1:{client_uuid.hex}:{reservation.hex}:{operation}'
        ),
        occurred_at=occurred,
        client_instance_uuid=client_uuid,
        planned_grams=planned,
        requested_actual_grams=actual,
    )


def build_reserve_request(
    *,
    client_instance_uuid: UUID,
    reservation_uuid: UUID,
    roast_uuid: UUID,
    lot_id: UUID,
    planned_grams: int,
    occurred_at: datetime,
) -> InventoryCommandRequest:
    return _build_request(
        operation='reserve',
        client_instance_uuid=client_instance_uuid,
        reservation_uuid=reservation_uuid,
        roast_uuid=roast_uuid,
        lot_id=lot_id,
        planned_grams=planned_grams,
        actual_grams=None,
        occurred_at=occurred_at,
    )


def build_finalize_request(
    *,
    client_instance_uuid: UUID,
    reservation_uuid: UUID,
    roast_uuid: UUID,
    lot_id: UUID,
    planned_grams: int,
    actual_grams: int | None,
    occurred_at: datetime,
) -> InventoryCommandRequest:
    return _build_request(
        operation='finalize',
        client_instance_uuid=client_instance_uuid,
        reservation_uuid=reservation_uuid,
        roast_uuid=roast_uuid,
        lot_id=lot_id,
        planned_grams=planned_grams,
        actual_grams=actual_grams,
        occurred_at=occurred_at,
    )


def build_release_request(
    *,
    client_instance_uuid: UUID,
    reservation_uuid: UUID,
    roast_uuid: UUID,
    lot_id: UUID,
    planned_grams: int,
    occurred_at: datetime,
) -> InventoryCommandRequest:
    return _build_request(
        operation='release',
        client_instance_uuid=client_instance_uuid,
        reservation_uuid=reservation_uuid,
        roast_uuid=roast_uuid,
        lot_id=lot_id,
        planned_grams=planned_grams,
        actual_grams=None,
        occurred_at=occurred_at,
    )


_INVENTORY_ERROR_CLASSIFICATIONS: Final[
    dict[tuple[int, str, str], tuple[FailureKind, bool]]
] = {
    (404, 'bean_lot_not_found', 'Bean lot not found'): (
        FailureKind.INVENTORY_REJECTED,
        False,
    ),
    (409, 'bean_lot_archived', 'Bean lot archived'): (
        FailureKind.INVENTORY_REJECTED,
        False,
    ),
    (409, 'invalid_inventory_transition', 'Invalid inventory transition'): (
        FailureKind.INVENTORY_REJECTED,
        False,
    ),
    (
        409,
        'inventory_idempotency_conflict',
        'Idempotency key conflicts with an earlier request',
    ): (FailureKind.INVENTORY_CONFLICT, False),
    (404, 'inventory_reservation_not_found', 'Inventory reservation not found'): (
        FailureKind.INVENTORY_REJECTED,
        False,
    ),
    (503, 'inventory_unavailable', 'Inventory unavailable'): (
        FailureKind.OFFLINE,
        True,
    ),
    (422, 'invalid_request', 'Invalid request'): (
        FailureKind.INVENTORY_REJECTED,
        False,
    ),
}


__all__ = [
    'BeanLot',
    'BeanLotPage',
    'InventoryBalance',
    'InventoryCommandRequest',
    'InventoryConflict',
    'InventoryMutationResult',
    'InventoryOperation',
    'InventoryProfileLink',
    'InventoryReservation',
    'LedgerOperation',
    'MAX_CACHED_LOTS',
    'MAX_INVENTORY_CURSOR_CHARS',
    'MAX_INVENTORY_PAGES',
    'ProcessingMethod',
    'ReservationState',
    'build_finalize_request',
    'build_release_request',
    'build_reserve_request',
    'green_weight_grams',
    'parse_bean_lot_page',
    'parse_inventory_error',
    'parse_inventory_mutation',
    'parse_profile_link',
    'profile_link_fields',
]
