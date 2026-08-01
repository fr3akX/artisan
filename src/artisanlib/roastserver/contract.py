#
# ABOUT
# Artisan Roast Server connector response contracts
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

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import math
import re
from typing import Final, Literal, NoReturn, cast
from uuid import UUID

MAX_PROFILE_BYTES: Final[int] = 16 * 1024 * 1024
MAX_METADATA_BYTES: Final[int] = 60 * 1024
MAX_JSON_BYTES: Final[int] = 2 * 1024 * 1024
MAX_CURSOR_CHARS: Final[int] = 512
JS_SAFE_INTEGER_MAX: Final[int] = 9_007_199_254_740_991
POSTGRESQL_INTEGER_MAX: Final[int] = 2_147_483_647

_ROLE_VALUES: Final[frozenset[str]] = frozenset({'admin', 'member'})
_STATE_VALUES: Final[frozenset[str]] = frozenset({'awaiting_profile', 'parsed', 'parse_failed'})
_TEMPERATURE_UNIT_VALUES: Final[frozenset[str]] = frozenset({'C', 'F'})
_HYPHENATED_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)
_HEX_UUID_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_LABEL_COLOR_RE: Final[re.Pattern[str]] = re.compile(r'^#[0-9a-fA-F]{6}$')


class ContractError(ValueError):
    def __init__(self) -> None:
        super().__init__('invalid server response')


class FailureKind(StrEnum):
    OFFLINE = 'offline'
    CREDENTIAL_REJECTED = 'credential_rejected'
    RATE_LIMITED = 'rate_limited'
    INVALID_RESPONSE = 'invalid_response'
    PROFILE_REJECTED = 'profile_rejected'
    LOCAL_PROFILE = 'local_profile'
    CHECKSUM_MISMATCH = 'checksum_mismatch'
    CACHE_CORRUPT = 'cache_corrupt'
    KEYRING = 'keyring'


FAILURE_MESSAGES: Final[dict[FailureKind, str]] = {
    FailureKind.OFFLINE: 'Offline / server unavailable.',
    FailureKind.CREDENTIAL_REJECTED: 'Credential rejected or revoked.',
    FailureKind.RATE_LIMITED: 'Request rate limited.',
    FailureKind.INVALID_RESPONSE: 'Invalid server response.',
    FailureKind.PROFILE_REJECTED: 'Profile rejected by server.',
    FailureKind.LOCAL_PROFILE: 'Local saved file changed or unavailable.',
    FailureKind.CHECKSUM_MISMATCH: 'Download checksum mismatch.',
    FailureKind.CACHE_CORRUPT: 'Cached copy corrupt or unavailable.',
    FailureKind.KEYRING: 'Operating-system keyring unavailable.',
}

type FrozenJsonScalar = None | bool | int | float | str
type FrozenJsonObject = tuple[tuple[str, 'JsonValue'], ...]
type JsonValue = FrozenJsonScalar | tuple['JsonValue', ...] | FrozenJsonObject


@dataclass(frozen=True, slots=True)
class PublicFailure:
    kind: FailureKind
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class Namespace:
    origin: str
    organization_id: UUID
    key: str


@dataclass(frozen=True, slots=True)
class ArchiveFilters:
    search: str | None = None
    state: Literal['awaiting_profile', 'parsed', 'parse_failed'] | None = None
    machine: str | None = None
    roast_at_from: datetime | None = None
    roast_at_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class ServerProfileSource:
    namespace: Namespace
    roast_uuid: UUID
    revision_number: int
    sha256: str
    stale: bool


@dataclass(frozen=True, slots=True)
class IdentityUser:
    id: UUID
    email: str
    nickname: str


@dataclass(frozen=True, slots=True)
class IdentityOrganization:
    id: UUID
    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    user: IdentityUser
    organization: IdentityOrganization
    role: Literal['admin', 'member']


@dataclass(frozen=True, slots=True)
class LabelSummary:
    label_uuid: UUID
    name: str
    color: str
    archived: bool


@dataclass(frozen=True, slots=True)
class Revision:
    revision_number: int
    sha256: str
    byte_size: int
    parser_version: str | None
    parse_state: Literal['awaiting_profile', 'parsed', 'parse_failed']
    parse_diagnostic_code: str | None
    parse_diagnostic_message: str | None
    uploaded_at: datetime
    metadata: FrozenJsonObject
    reparse_recommended: bool


@dataclass(frozen=True, slots=True)
class RoastSummary:
    roast_uuid: UUID
    state: Literal['awaiting_profile', 'parsed', 'parse_failed']
    roast_at: datetime
    title: str | None
    batch_prefix: str | None
    batch_number: int | None
    batch_position: int | None
    operator: str | None
    machine: str | None
    machine_setup: str | None
    temperature_unit: Literal['C', 'F'] | None
    duration_seconds: int | None
    green_weight_kg: float | None
    roasted_weight_kg: float | None
    revision_count: int
    updated_at: datetime
    labels: tuple[LabelSummary, ...]


@dataclass(frozen=True, slots=True)
class RoastPage:
    items: tuple[RoastSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class RoastDetailLinks:
    self_path: str
    chart: str
    revisions: str


@dataclass(frozen=True, slots=True)
class RoastDetail:
    roast_uuid: UUID
    state: Literal['awaiting_profile', 'parsed', 'parse_failed']
    roast_at: datetime
    title: str | None
    batch_prefix: str | None
    batch_number: int | None
    batch_position: int | None
    operator: str | None
    machine: str | None
    machine_setup: str | None
    temperature_unit: Literal['C', 'F'] | None
    duration_seconds: int | None
    green_weight_kg: float | None
    roasted_weight_kg: float | None
    revision_count: int
    updated_at: datetime
    labels: tuple[LabelSummary, ...]
    current_metadata: FrozenJsonObject
    current_revision: Revision | None
    links: RoastDetailLinks


@dataclass(frozen=True, slots=True)
class RevisionUploadLinks:
    roast: str
    chart: str
    revisions: str
    download: str


@dataclass(frozen=True, slots=True)
class RevisionUpload:
    roast_uuid: UUID
    state: Literal['awaiting_profile', 'parsed', 'parse_failed']
    revision: Revision
    links: RevisionUploadLinks


@dataclass(frozen=True, slots=True)
class AroastResult:
    roast_id: UUID
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class AroastAck:
    success: Literal[True]
    result: AroastResult
    rlimit: int
    rusage: int
    rremaining: int


@dataclass(frozen=True, slots=True)
class ServerError:
    code: str
    message: str
    details: JsonValue


def _fail() -> NoReturn:
    raise ContractError


def _exact_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail()
    if set(value) != keys:
        _fail()
    return value


def _parse_bool(value: object) -> bool:
    if not isinstance(value, bool):
        _fail()
    return value


def _parse_required_string(value: object, *, allow_empty: bool = False, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        _fail()
    if '\x00' in value:
        _fail()
    if not allow_empty and value == '':
        _fail()
    if max_length is not None and len(value) > max_length:
        _fail()
    return value


def _parse_optional_string(value: object, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    return _parse_required_string(value, max_length=max_length)


def _parse_safe_int(value: object, *, minimum: int | None = None, maximum: int = JS_SAFE_INTEGER_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail()
    if minimum is not None and value < minimum:
        _fail()
    if value > maximum:
        _fail()
    return value


def _parse_optional_int(value: object, *, minimum: int | None = None, maximum: int = JS_SAFE_INTEGER_MAX) -> int | None:
    if value is None:
        return None
    return _parse_safe_int(value, minimum=minimum, maximum=maximum)


def _parse_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail()
    number = float(value)
    if not math.isfinite(number):
        _fail()
    return number


def _parse_aware_datetime(value: object) -> datetime:
    text = _parse_required_string(value)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail()
    return parsed


def _parse_hyphenated_uuid(value: object) -> UUID:
    text = _parse_required_string(value)
    if _HYPHENATED_UUID_RE.fullmatch(text) is None:
        _fail()
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ContractError from exc
    if str(parsed) != text:
        _fail()
    return parsed


def _parse_hex_uuid(value: object) -> UUID:
    text = _parse_required_string(value)
    if _HEX_UUID_RE.fullmatch(text) is None:
        _fail()
    try:
        parsed = UUID(hex=text)
    except ValueError as exc:
        raise ContractError from exc
    if parsed.hex != text:
        _fail()
    return parsed


def _parse_sha256(value: object) -> str:
    text = _parse_required_string(value)
    if _SHA256_RE.fullmatch(text) is None:
        _fail()
    return text


def _parse_role(value: object) -> Literal['admin', 'member']:
    text = _parse_required_string(value)
    if text not in _ROLE_VALUES:
        _fail()
    return cast(Literal['admin', 'member'], text)


def _parse_state(value: object) -> Literal['awaiting_profile', 'parsed', 'parse_failed']:
    text = _parse_required_string(value)
    if text not in _STATE_VALUES:
        _fail()
    return cast(Literal['awaiting_profile', 'parsed', 'parse_failed'], text)


def _parse_temperature_unit(value: object) -> Literal['C', 'F'] | None:
    if value is None:
        return None
    text = _parse_required_string(value)
    if text not in _TEMPERATURE_UNIT_VALUES:
        _fail()
    return cast(Literal['C', 'F'], text)


def _freeze_json(value: object, *, depth: int = 1) -> JsonValue:
    if depth > 64:
        _fail()
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > JS_SAFE_INTEGER_MAX:
            _fail()
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail()
        return value
    if isinstance(value, str):
        if '\x00' in value:
            _fail()
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        items: list[tuple[str, JsonValue]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                _fail()
            items.append((key, _freeze_json(item, depth=depth + 1)))
        return tuple(items)
    _fail()


def _metadata_object(value: object) -> FrozenJsonObject:
    if not isinstance(value, dict):
        _fail()
    try:
        encoded = json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ContractError from exc
    if len(encoded) > MAX_METADATA_BYTES:
        _fail()
    frozen = _freeze_json(value)
    if not isinstance(frozen, tuple):
        _fail()
    return cast(FrozenJsonObject, frozen)


def _safe_public_string(value: object, *, max_length: int) -> str:
    text = _parse_required_string(value, max_length=max_length)
    for char in text:
        if ord(char) < 32 or ord(char) == 127:
            _fail()
    return text


def _parse_label(value: object) -> LabelSummary:
    mapping = _exact_object(value, frozenset({'label_uuid', 'name', 'color', 'archived'}))
    color = _parse_required_string(mapping['color'])
    if _LABEL_COLOR_RE.fullmatch(color) is None:
        _fail()
    return LabelSummary(
        label_uuid=_parse_hex_uuid(mapping['label_uuid']),
        name=_parse_required_string(mapping['name']),
        color=color,
        archived=_parse_bool(mapping['archived']),
    )


def _parse_revision(value: object) -> Revision:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'revision_number',
                'sha256',
                'byte_size',
                'parser_version',
                'parse_state',
                'parse_diagnostic_code',
                'parse_diagnostic_message',
                'uploaded_at',
                'metadata',
                'reparse_recommended',
            }
        ),
    )
    return Revision(
        revision_number=_parse_safe_int(mapping['revision_number'], minimum=1, maximum=POSTGRESQL_INTEGER_MAX),
        sha256=_parse_sha256(mapping['sha256']),
        byte_size=_parse_safe_int(mapping['byte_size'], minimum=0, maximum=MAX_PROFILE_BYTES),
        parser_version=_parse_optional_string(mapping['parser_version']),
        parse_state=_parse_state(mapping['parse_state']),
        parse_diagnostic_code=_parse_optional_string(mapping['parse_diagnostic_code']),
        parse_diagnostic_message=_parse_optional_string(mapping['parse_diagnostic_message']),
        uploaded_at=_parse_aware_datetime(mapping['uploaded_at']),
        metadata=_metadata_object(mapping['metadata']),
        reparse_recommended=_parse_bool(mapping['reparse_recommended']),
    )


def _parse_roast_summary(value: object) -> RoastSummary:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'roast_uuid',
                'state',
                'roast_at',
                'title',
                'batch_prefix',
                'batch_number',
                'batch_position',
                'operator',
                'machine',
                'machine_setup',
                'temperature_unit',
                'duration_seconds',
                'green_weight_kg',
                'roasted_weight_kg',
                'revision_count',
                'updated_at',
                'labels',
            }
        ),
    )
    labels_raw = mapping['labels']
    if not isinstance(labels_raw, list):
        _fail()
    return RoastSummary(
        roast_uuid=_parse_hex_uuid(mapping['roast_uuid']),
        state=_parse_state(mapping['state']),
        roast_at=_parse_aware_datetime(mapping['roast_at']),
        title=_parse_optional_string(mapping['title']),
        batch_prefix=_parse_optional_string(mapping['batch_prefix']),
        batch_number=_parse_optional_int(mapping['batch_number'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
        batch_position=_parse_optional_int(mapping['batch_position'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
        operator=_parse_optional_string(mapping['operator']),
        machine=_parse_optional_string(mapping['machine']),
        machine_setup=_parse_optional_string(mapping['machine_setup']),
        temperature_unit=_parse_temperature_unit(mapping['temperature_unit']),
        duration_seconds=_parse_optional_int(mapping['duration_seconds'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
        green_weight_kg=_parse_optional_float(mapping['green_weight_kg']),
        roasted_weight_kg=_parse_optional_float(mapping['roasted_weight_kg']),
        revision_count=_parse_safe_int(mapping['revision_count'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
        updated_at=_parse_aware_datetime(mapping['updated_at']),
        labels=tuple(_parse_label(item) for item in labels_raw),
    )


def parse_identity(value: object) -> ServerIdentity:
    mapping = _exact_object(value, frozenset({'user', 'organization', 'role'}))
    user_mapping = _exact_object(mapping['user'], frozenset({'id', 'email', 'nickname'}))
    organization_mapping = _exact_object(mapping['organization'], frozenset({'id', 'name', 'slug'}))
    return ServerIdentity(
        user=IdentityUser(
            id=_parse_hyphenated_uuid(user_mapping['id']),
            email=_parse_required_string(user_mapping['email']),
            nickname=_parse_required_string(user_mapping['nickname']),
        ),
        organization=IdentityOrganization(
            id=_parse_hyphenated_uuid(organization_mapping['id']),
            name=_parse_required_string(organization_mapping['name']),
            slug=_parse_required_string(organization_mapping['slug']),
        ),
        role=_parse_role(mapping['role']),
    )


def parse_roast_page(value: object) -> RoastPage:
    mapping = _exact_object(value, frozenset({'items', 'next_cursor'}))
    items_raw = mapping['items']
    if not isinstance(items_raw, list):
        _fail()
    next_cursor_value = mapping['next_cursor']
    next_cursor: str | None
    if next_cursor_value is None:
        next_cursor = None
    else:
        next_cursor = _parse_required_string(next_cursor_value, max_length=MAX_CURSOR_CHARS)
    return RoastPage(
        items=tuple(_parse_roast_summary(item) for item in items_raw),
        next_cursor=next_cursor,
    )


def parse_roast_detail(value: object) -> RoastDetail:
    mapping = _exact_object(
        value,
        frozenset(
            {
                'roast_uuid',
                'state',
                'roast_at',
                'title',
                'batch_prefix',
                'batch_number',
                'batch_position',
                'operator',
                'machine',
                'machine_setup',
                'temperature_unit',
                'duration_seconds',
                'green_weight_kg',
                'roasted_weight_kg',
                'revision_count',
                'updated_at',
                'labels',
                'current_metadata',
                'current_revision',
                'links',
            }
        ),
    )
    summary = _parse_roast_summary(
        {
            'roast_uuid': mapping['roast_uuid'],
            'state': mapping['state'],
            'roast_at': mapping['roast_at'],
            'title': mapping['title'],
            'batch_prefix': mapping['batch_prefix'],
            'batch_number': mapping['batch_number'],
            'batch_position': mapping['batch_position'],
            'operator': mapping['operator'],
            'machine': mapping['machine'],
            'machine_setup': mapping['machine_setup'],
            'temperature_unit': mapping['temperature_unit'],
            'duration_seconds': mapping['duration_seconds'],
            'green_weight_kg': mapping['green_weight_kg'],
            'roasted_weight_kg': mapping['roasted_weight_kg'],
            'revision_count': mapping['revision_count'],
            'updated_at': mapping['updated_at'],
            'labels': mapping['labels'],
        }
    )
    current_metadata = _metadata_object(mapping['current_metadata'])
    current_revision_value = mapping['current_revision']
    current_revision = None if current_revision_value is None else _parse_revision(current_revision_value)
    links_mapping = _exact_object(mapping['links'], frozenset({'self', 'chart', 'revisions'}))
    expected_base = f'/api/v1/roasts/{summary.roast_uuid.hex}'
    self_path = _parse_required_string(links_mapping['self'])
    chart = _parse_required_string(links_mapping['chart'])
    revisions = _parse_required_string(links_mapping['revisions'])
    if self_path != expected_base or chart != f'{expected_base}/chart' or revisions != f'{expected_base}/revisions':
        _fail()
    if summary.state == 'awaiting_profile':
        if summary.revision_count != 0 or current_revision is not None or current_metadata != ():
            _fail()
    else:
        if current_revision is None:
            _fail()
        if summary.revision_count < 1 or current_revision.revision_number != summary.revision_count:
            _fail()
        if current_revision.parse_state != summary.state:
            _fail()
        if current_revision.metadata != current_metadata:
            _fail()
    return RoastDetail(
        roast_uuid=summary.roast_uuid,
        state=summary.state,
        roast_at=summary.roast_at,
        title=summary.title,
        batch_prefix=summary.batch_prefix,
        batch_number=summary.batch_number,
        batch_position=summary.batch_position,
        operator=summary.operator,
        machine=summary.machine,
        machine_setup=summary.machine_setup,
        temperature_unit=summary.temperature_unit,
        duration_seconds=summary.duration_seconds,
        green_weight_kg=summary.green_weight_kg,
        roasted_weight_kg=summary.roasted_weight_kg,
        revision_count=summary.revision_count,
        updated_at=summary.updated_at,
        labels=summary.labels,
        current_metadata=current_metadata,
        current_revision=current_revision,
        links=RoastDetailLinks(self_path=self_path, chart=chart, revisions=revisions),
    )


def parse_revision_upload(value: object) -> RevisionUpload:
    mapping = _exact_object(value, frozenset({'roast_uuid', 'state', 'revision', 'links'}))
    roast_uuid = _parse_hex_uuid(mapping['roast_uuid'])
    state = _parse_state(mapping['state'])
    revision = _parse_revision(mapping['revision'])
    if revision.parse_state != state:
        _fail()
    links_mapping = _exact_object(mapping['links'], frozenset({'roast', 'chart', 'revisions', 'download'}))
    expected_base = f'/api/v1/roasts/{roast_uuid.hex}'
    roast = _parse_required_string(links_mapping['roast'])
    chart = _parse_required_string(links_mapping['chart'])
    revisions = _parse_required_string(links_mapping['revisions'])
    download = _parse_required_string(links_mapping['download'])
    if roast != expected_base:
        _fail()
    if chart != f'{expected_base}/chart':
        _fail()
    if revisions != f'{expected_base}/revisions':
        _fail()
    if download != f'{expected_base}/revisions/{revision.revision_number}/download':
        _fail()
    return RevisionUpload(
        roast_uuid=roast_uuid,
        state=state,
        revision=revision,
        links=RevisionUploadLinks(roast=roast, chart=chart, revisions=revisions, download=download),
    )


def parse_aroast_ack(value: object) -> AroastAck:
    mapping = _exact_object(value, frozenset({'success', 'result', 'rlimit', 'rusage', 'rremaining'}))
    if mapping['success'] is not True:
        _fail()
    result_mapping = _exact_object(mapping['result'], frozenset({'roast_id', 'modified_at'}))
    return AroastAck(
        success=True,
        result=AroastResult(
            roast_id=_parse_hex_uuid(result_mapping['roast_id']),
            modified_at=_parse_aware_datetime(result_mapping['modified_at']),
        ),
        rlimit=_parse_safe_int(mapping['rlimit'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
        rusage=_parse_safe_int(mapping['rusage'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
        rremaining=_parse_safe_int(mapping['rremaining'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX),
    )


def parse_error_envelope(body: object) -> ServerError | None:
    if not isinstance(body, bytes):
        return None
    if len(body) > MAX_JSON_BYTES:
        return None
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        return None
    if '\x00' in text:
        return None
    try:
        value = json.loads(text)
        mapping = _exact_object(value, frozenset({'error'}))
        error_mapping = _exact_object(mapping['error'], frozenset({'code', 'message', 'details'}))
        code = _safe_public_string(error_mapping['code'], max_length=100)
        message = _safe_public_string(error_mapping['message'], max_length=500)
        details = _freeze_json(error_mapping['details'])
    except (ContractError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return ServerError(code=code, message=message, details=details)


__all__ = [
    'AroastAck',
    'AroastResult',
    'ArchiveFilters',
    'ContractError',
    'FAILURE_MESSAGES',
    'FailureKind',
    'FrozenJsonObject',
    'IdentityOrganization',
    'IdentityUser',
    'JS_SAFE_INTEGER_MAX',
    'JsonValue',
    'LabelSummary',
    'MAX_CURSOR_CHARS',
    'MAX_JSON_BYTES',
    'MAX_METADATA_BYTES',
    'MAX_PROFILE_BYTES',
    'Namespace',
    'POSTGRESQL_INTEGER_MAX',
    'PublicFailure',
    'Revision',
    'RevisionUpload',
    'RevisionUploadLinks',
    'RoastDetail',
    'RoastDetailLinks',
    'RoastPage',
    'RoastSummary',
    'ServerError',
    'ServerIdentity',
    'ServerProfileSource',
    'parse_aroast_ack',
    'parse_error_envelope',
    'parse_identity',
    'parse_revision_upload',
    'parse_roast_detail',
    'parse_roast_page',
]
