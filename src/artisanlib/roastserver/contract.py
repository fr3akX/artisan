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
MAX_JSON_DEPTH: Final[int] = 64
MAX_ERROR_MESSAGE_CODE_POINTS: Final[int] = 500
MAX_ARCHIVE_SEARCH_CHARS: Final[int] = 200
MAX_ARCHIVE_MACHINE_CHARS: Final[int] = 100

_ROAST_STATE_VALUES: Final[frozenset[str]] = frozenset({'awaiting_profile', 'parsed', 'parse_failed'})
_REVISION_PARSE_STATE_VALUES: Final[frozenset[str]] = frozenset({'parsed', 'failed'})
_UPLOAD_STATE_VALUES: Final[frozenset[str]] = frozenset({'parsed', 'parse_failed'})
_ROLE_VALUES: Final[frozenset[str]] = frozenset({'admin', 'member'})
_TEMPERATURE_UNIT_VALUES: Final[frozenset[str]] = frozenset({'C', 'F'})
_LABEL_COLOR_VALUES: Final[frozenset[str]] = frozenset(
    {'slate', 'red', 'orange', 'amber', 'green', 'teal', 'blue', 'violet'}
)
_HYPHENATED_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)
_HEX_UUID_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$'
)


type FrozenJsonScalar = None | bool | int | float | str


@dataclass(frozen=True, slots=True)
class FrozenJsonArray:
    items: tuple[JsonValue, ...]


type FrozenJsonObject = tuple[tuple[str, 'JsonValue'], ...]
type JsonValue = FrozenJsonScalar | FrozenJsonArray | FrozenJsonObject
type RoastState = Literal['awaiting_profile', 'parsed', 'parse_failed']
type RevisionParseState = Literal['parsed', 'failed']
type UploadState = Literal['parsed', 'parse_failed']
type LabelColor = Literal['slate', 'red', 'orange', 'amber', 'green', 'teal', 'blue', 'violet']


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
    state: RoastState | None = None
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
    color: LabelColor
    archived: bool


@dataclass(frozen=True, slots=True)
class Revision:
    revision_number: int
    sha256: str
    byte_size: int
    parser_version: str | None
    parse_state: RevisionParseState
    parse_diagnostic_code: str | None
    parse_diagnostic_message: str | None
    uploaded_at: datetime
    metadata: FrozenJsonObject
    reparse_recommended: bool


@dataclass(frozen=True, slots=True)
class RoastSummary:
    roast_uuid: UUID
    state: RoastState
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
    state: RoastState
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
    state: UploadState
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
    mapping = cast(dict[object, object], value)
    if len(mapping) != len(keys):
        _fail()
    for key in mapping:
        if not isinstance(key, str) or key not in keys:
            _fail()
    return cast(dict[str, object], mapping)


def _has_prohibited_string_code_point(text: str, *, reject_controls: bool) -> bool:
    for char in text:
        code_point = ord(char)
        if code_point == 0 or 0xD800 <= code_point <= 0xDFFF:
            return True
        if reject_controls and (code_point < 0x20 or 0x7F <= code_point <= 0x9F):
            return True
    return False


def _parse_required_string(
    value: object,
    *,
    allow_empty: bool = False,
    max_length: int | None = None,
    reject_controls: bool = False,
) -> str:
    if not isinstance(value, str):
        _fail()
    if not allow_empty and value == '':
        _fail()
    if max_length is not None and len(value) > max_length:
        _fail()
    if _has_prohibited_string_code_point(value, reject_controls=reject_controls):
        _fail()
    return value


def _parse_optional_string(
    value: object,
    *,
    max_length: int | None = None,
    reject_controls: bool = False,
) -> str | None:
    if value is None:
        return None
    return _parse_required_string(value, max_length=max_length, reject_controls=reject_controls)


def _parse_bool(value: object) -> bool:
    if not isinstance(value, bool):
        _fail()
    return value


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
    if isinstance(value, int):
        if abs(value) > JS_SAFE_INTEGER_MAX:
            _fail()
        try:
            number = float(value)
        except OverflowError as exc:
            raise ContractError from exc
    else:
        number = value
    if not math.isfinite(number):
        _fail()
    return number


def _parse_aware_datetime(value: object) -> datetime:
    text = _parse_required_string(value)
    if _TIMESTAMP_RE.fullmatch(text) is None:
        _fail()
    normalized = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        parsed = datetime.fromisoformat(normalized)
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


def _parse_roast_state(value: object) -> RoastState:
    text = _parse_required_string(value)
    if text not in _ROAST_STATE_VALUES:
        _fail()
    return cast(RoastState, text)


def _parse_revision_parse_state(value: object) -> RevisionParseState:
    text = _parse_required_string(value)
    if text not in _REVISION_PARSE_STATE_VALUES:
        _fail()
    return cast(RevisionParseState, text)


def _parse_upload_state(value: object) -> UploadState:
    text = _parse_required_string(value)
    if text not in _UPLOAD_STATE_VALUES:
        _fail()
    return cast(UploadState, text)


def _parse_temperature_unit(value: object) -> Literal['C', 'F'] | None:
    if value is None:
        return None
    text = _parse_required_string(value)
    if text not in _TEMPERATURE_UNIT_VALUES:
        _fail()
    return cast(Literal['C', 'F'], text)


def _parse_label_color(value: object) -> LabelColor:
    text = _parse_required_string(value)
    if text not in _LABEL_COLOR_VALUES:
        _fail()
    return cast(LabelColor, text)


def _expected_revision_parse_state(state: UploadState) -> RevisionParseState:
    return 'parsed' if state == 'parsed' else 'failed'


def _validate_roast_state_count(state: RoastState, revision_count: int) -> None:
    if state == 'awaiting_profile':
        if revision_count != 0:
            _fail()
        return
    if revision_count < 1:
        _fail()


def _validate_json_scalar(value: object, *, reject_string_controls: bool) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > JS_SAFE_INTEGER_MAX:
            _fail()
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail()
        return
    if isinstance(value, str):
        if _has_prohibited_string_code_point(value, reject_controls=reject_string_controls):
            _fail()
        return
    _fail()


def _validate_json_graph(value: object, *, reject_string_controls: bool) -> None:
    stack: list[tuple[object, int, bool]] = [(value, 1, False)]
    active_container_ids: set[int] = set()
    while stack:
        current, depth, exiting = stack.pop()
        if isinstance(current, list | dict):
            container: list[object] | dict[object, object]
            if isinstance(current, list):
                container = cast(list[object], current)
            else:
                container = cast(dict[object, object], current)
            container_id = id(container)
            if exiting:
                active_container_ids.remove(container_id)
                continue
            if depth > MAX_JSON_DEPTH:
                _fail()
            if container_id in active_container_ids:
                _fail()
            active_container_ids.add(container_id)
            stack.append((container, depth, True))
            if isinstance(container, list):
                for item in reversed(container):
                    stack.append((item, depth + 1, False))
            else:
                items = list(container.items())
                for key, item in reversed(items):
                    if not isinstance(key, str):
                        _fail()
                    if _has_prohibited_string_code_point(key, reject_controls=reject_string_controls):
                        _fail()
                    stack.append((item, depth + 1, False))
            continue
        _validate_json_scalar(current, reject_string_controls=reject_string_controls)


def _canonicalize_json_value(value: object, *, reject_string_controls: bool) -> object:
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
        if _has_prohibited_string_code_point(value, reject_controls=reject_string_controls):
            _fail()
        return value
    if isinstance(value, list):
        sequence = cast(list[object], value)
        return [
            _canonicalize_json_value(item, reject_string_controls=reject_string_controls)
            for item in sequence
        ]
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        items: list[tuple[str, object]] = []
        for key, item in mapping.items():
            if not isinstance(key, str):
                _fail()
            if _has_prohibited_string_code_point(key, reject_controls=reject_string_controls):
                _fail()
            items.append((key, item))
        return {
            key: _canonicalize_json_value(
                item, reject_string_controls=reject_string_controls
            )
            for key, item in sorted(items)
        }
    _fail()


def _freeze_canonical_json(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        sequence = cast(list[object], value)
        return FrozenJsonArray(tuple(_freeze_canonical_json(item) for item in sequence))
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        return tuple(
            (key, _freeze_canonical_json(item)) for key, item in mapping.items()
        )
    _fail()


def _canonical_json(value: object, *, reject_string_controls: bool) -> object:
    _validate_json_graph(value, reject_string_controls=reject_string_controls)
    return _canonicalize_json_value(value, reject_string_controls=reject_string_controls)


def _metadata_object(value: object) -> FrozenJsonObject:
    if not isinstance(value, dict):
        _fail()
    mapping = cast(dict[object, object], value)
    canonical = _canonical_json(mapping, reject_string_controls=False)
    try:
        encoded = json.dumps(
            canonical,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        ).encode('utf-8')
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ContractError from exc
    if len(encoded) > MAX_METADATA_BYTES:
        _fail()
    frozen = _freeze_canonical_json(canonical)
    if not isinstance(frozen, tuple):
        _fail()
    return frozen


def _safe_public_string(value: object, *, max_length: int) -> str:
    return _parse_required_string(value, max_length=max_length, reject_controls=True)


def validate_archive_filters(value: object) -> ArchiveFilters:
    if not isinstance(value, ArchiveFilters):
        raise ValueError('invalid archive filters')
    if value.search is not None:
        try:
            _parse_required_string(
                value.search,
                max_length=MAX_ARCHIVE_SEARCH_CHARS,
                reject_controls=True,
            )
        except ContractError:
            raise ValueError('invalid archive search') from None
    if value.state is not None and value.state not in _ROAST_STATE_VALUES:
        raise ValueError('invalid roast state')
    if value.machine is not None:
        try:
            _parse_required_string(
                value.machine,
                max_length=MAX_ARCHIVE_MACHINE_CHARS,
                reject_controls=True,
            )
        except ContractError:
            raise ValueError('invalid archive machine') from None
    for name, candidate in (
        ('roast_at_from', value.roast_at_from),
        ('roast_at_to', value.roast_at_to),
    ):
        candidate_object: object = candidate
        if candidate_object is None:
            continue
        try:
            aware = (
                isinstance(candidate_object, datetime)
                and candidate_object.tzinfo is not None
                and candidate_object.utcoffset() is not None
            )
        except (OverflowError, ValueError):
            aware = False
        if not aware:
            raise ValueError(f'invalid archive {name}')
    try:
        invalid_range = (
            value.roast_at_from is not None
            and value.roast_at_to is not None
            and value.roast_at_from > value.roast_at_to
        )
    except (OverflowError, TypeError, ValueError):
        raise ValueError('invalid archive date range') from None
    if invalid_range:
        raise ValueError('invalid archive date range')
    return value


def _parse_label(value: object) -> LabelSummary:
    mapping = _exact_object(value, frozenset({'label_uuid', 'name', 'color', 'archived'}))
    return LabelSummary(
        label_uuid=_parse_hex_uuid(mapping['label_uuid']),
        name=_parse_required_string(mapping['name']),
        color=_parse_label_color(mapping['color']),
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
        parse_state=_parse_revision_parse_state(mapping['parse_state']),
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
    labels_value = mapping['labels']
    if not isinstance(labels_value, list):
        _fail()
    labels_raw = cast(list[object], labels_value)
    state = _parse_roast_state(mapping['state'])
    revision_count = _parse_safe_int(mapping['revision_count'], minimum=0, maximum=POSTGRESQL_INTEGER_MAX)
    _validate_roast_state_count(state, revision_count)
    return RoastSummary(
        roast_uuid=_parse_hex_uuid(mapping['roast_uuid']),
        state=state,
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
        revision_count=revision_count,
        updated_at=_parse_aware_datetime(mapping['updated_at']),
        labels=tuple(_parse_label(item) for item in labels_raw),
    )


def _parse_error_details(value: object) -> JsonValue:
    canonical = _canonical_json(value, reject_string_controls=True)
    return _freeze_canonical_json(canonical)


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


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
    items_value = mapping['items']
    if not isinstance(items_value, list):
        _fail()
    items_raw = cast(list[object], items_value)
    next_cursor_value = mapping['next_cursor']
    next_cursor: str | None
    if next_cursor_value is None:
        next_cursor = None
    else:
        next_cursor = _parse_required_string(next_cursor_value, max_length=MAX_CURSOR_CHARS)
    return RoastPage(items=tuple(_parse_roast_summary(item) for item in items_raw), next_cursor=next_cursor)


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
        if current_revision is not None or current_metadata != ():
            _fail()
    else:
        expected_revision_state: RevisionParseState = (
            'parsed' if summary.state == 'parsed' else 'failed'
        )
        if current_revision is None:
            _fail()
        if current_revision.revision_number != summary.revision_count:
            _fail()
        if current_revision.parse_state != expected_revision_state:
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
    state = _parse_upload_state(mapping['state'])
    revision = _parse_revision(mapping['revision'])
    if revision.parse_state != _expected_revision_parse_state(state):
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
        value = json.loads(text, object_pairs_hook=_reject_duplicate_object_pairs)
        mapping = _exact_object(value, frozenset({'error'}))
        error_mapping = _exact_object(mapping['error'], frozenset({'code', 'message', 'details'}))
        code = _safe_public_string(error_mapping['code'], max_length=100)
        message = _safe_public_string(
            error_mapping['message'], max_length=MAX_ERROR_MESSAGE_CODE_POINTS
        )
        details = _parse_error_details(error_mapping['details'])
    except (ContractError, RecursionError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return ServerError(code=code, message=message, details=details)


__all__ = [
    'AroastAck',
    'AroastResult',
    'ArchiveFilters',
    'ContractError',
    'FAILURE_MESSAGES',
    'FailureKind',
    'FrozenJsonArray',
    'FrozenJsonObject',
    'IdentityOrganization',
    'IdentityUser',
    'JS_SAFE_INTEGER_MAX',
    'JsonValue',
    'LabelSummary',
    'MAX_ARCHIVE_MACHINE_CHARS',
    'MAX_ARCHIVE_SEARCH_CHARS',
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
    'validate_archive_filters',
]
