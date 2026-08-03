from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from uuid import UUID

import pytest

from artisanlib.roastserver import FrozenJsonArray as ExportedFrozenJsonArray
from artisanlib.roastserver.contract import (
    ContractError,
    FrozenJsonArray,
    JS_SAFE_INTEGER_MAX,
    MAX_CURSOR_CHARS,
    MAX_JSON_BYTES,
    LabelColor,
    LabelSummary,
    RoastPage,
    ServerError,
    ServerIdentity,
    parse_aroast_ack,
    parse_error_envelope,
    parse_identity,
    parse_roast_detail,
    parse_roast_page,
    parse_revision_upload,
)

ROAST_UUID = UUID('11111111-1111-4111-8111-111111111111')
LABEL_UUID = UUID('33333333-3333-4333-8333-333333333333')
PROFILE_BYTES = b"{'roastUUID':'11111111111141118111111111111111','mode':'C'}"
SHA256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
PALETTE_VALUES: tuple[LabelColor, ...] = (
    'slate',
    'red',
    'orange',
    'amber',
    'green',
    'teal',
    'blue',
    'violet',
)
ROAST_AT_TEXT = '2026-08-01T12:34:56Z'
UPDATED_AT_TEXT = '2026-08-01T12:35:56+00:00'
UPLOADED_AT_TEXT = '2026-08-01T12:36:56.123456+00:00'
ACK_AT_TEXT = '2026-08-01T12:37:56.123456Z'


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


def revision_state_for_roast_state(state: str) -> str:
    return 'parsed' if state == 'parsed' else 'failed'


def metadata_payload() -> dict[str, object]:
    return {
        'roast': {
            'batch': 12,
            'measurements': {'end': 212.4, 'start': 198.5},
        },
        'score': 89,
    }


def reordered_metadata_payload() -> dict[str, object]:
    return {
        'score': 89,
        'roast': {
            'measurements': {'start': 198.5, 'end': 212.4},
            'batch': 12,
        },
    }


def nested_metadata(total_depth: int) -> dict[str, object]:
    root: dict[str, object] = {}
    current = root
    for _depth in range(2, total_depth + 1):
        child: dict[str, object] = {}
        current['next'] = child
        current = child
    current['leaf'] = 'done'
    return root


def cyclic_metadata() -> dict[str, object]:
    root: dict[str, object] = {}
    root['self'] = root
    return root


def ambiguous_container_metadata() -> dict[str, object]:
    return {
        'empty_array': [],
        'empty_object': {},
        'pair_array': [['key', 1]],
        'nested': [[], {}, [['nested-key', 2]]],
    }


def labels_payload(color: str = 'green') -> list[dict[str, object]]:
    return [
        {
            'label_uuid': LABEL_UUID.hex,
            'name': 'Production',
            'color': color,
            'archived': False,
        }
    ]


def valid_revision_payload(
    *,
    revision_number: int = 1,
    parse_state: str = 'parsed',
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        'revision_number': revision_number,
        'sha256': SHA256,
        'byte_size': len(PROFILE_BYTES),
        'parser_version': '2026.8.1',
        'parse_state': parse_state,
        'parse_diagnostic_code': None,
        'parse_diagnostic_message': None,
        'uploaded_at': UPLOADED_AT_TEXT,
        'metadata': metadata_payload() if metadata is None else metadata,
        'reparse_recommended': False,
    }


def valid_roast_item_payload(
    *,
    state: str = 'parsed',
    revision_count: int = 1,
    label_color: str = 'green',
    roast_at: str = ROAST_AT_TEXT,
    updated_at: str = UPDATED_AT_TEXT,
) -> dict[str, object]:
    return {
        'roast_uuid': ROAST_UUID.hex,
        'state': state,
        'roast_at': roast_at,
        'title': 'Sample Roast',
        'batch_prefix': 'B',
        'batch_number': 12,
        'batch_position': 1,
        'operator': 'Owner',
        'machine': 'Sample Roaster',
        'machine_setup': '12 kg drum',
        'temperature_unit': 'C',
        'duration_seconds': 600,
        'green_weight_kg': 1.0,
        'roasted_weight_kg': 0.85,
        'revision_count': revision_count,
        'updated_at': updated_at,
        'labels': labels_payload(label_color),
    }


def valid_roast_page_payload(
    *,
    state: str = 'parsed',
    revision_count: int = 1,
    label_color: str = 'green',
    next_cursor: str | None = None,
) -> dict[str, object]:
    return {
        'items': [
            valid_roast_item_payload(
                state=state,
                revision_count=revision_count,
                label_color=label_color,
            )
        ],
        'next_cursor': next_cursor,
    }


def valid_roast_detail_payload(
    *,
    state: str = 'parsed',
    revision_count: int = 1,
    current_metadata: dict[str, object] | None = None,
    revision_metadata: dict[str, object] | None = None,
    revision_parse_state: str | None = None,
    include_current_revision: bool | None = None,
) -> dict[str, object]:
    metadata = metadata_payload() if current_metadata is None else current_metadata
    payload = valid_roast_item_payload(state=state, revision_count=revision_count)
    payload['current_metadata'] = metadata
    if include_current_revision is None:
        include_current_revision = state != 'awaiting_profile'
    if include_current_revision:
        payload['current_revision'] = valid_revision_payload(
            revision_number=revision_count,
            parse_state=(
                revision_state_for_roast_state(state)
                if revision_parse_state is None
                else revision_parse_state
            ),
            metadata=metadata if revision_metadata is None else revision_metadata,
        )
    else:
        payload['current_revision'] = None
    payload['links'] = {
        'self': f'/api/v1/roasts/{ROAST_UUID.hex}',
        'chart': f'/api/v1/roasts/{ROAST_UUID.hex}/chart',
        'revisions': f'/api/v1/roasts/{ROAST_UUID.hex}/revisions',
    }
    return payload


def valid_upload_payload(
    *,
    state: str = 'parsed',
    revision_parse_state: str | None = None,
    revision_number: int = 1,
) -> dict[str, object]:
    revision = valid_revision_payload(
        revision_number=revision_number,
        parse_state=(revision_state_for_roast_state(state) if revision_parse_state is None else revision_parse_state),
    )
    return {
        'roast_uuid': ROAST_UUID.hex,
        'state': state,
        'revision': revision,
        'links': {
            'roast': f'/api/v1/roasts/{ROAST_UUID.hex}',
            'chart': f'/api/v1/roasts/{ROAST_UUID.hex}/chart',
            'revisions': f'/api/v1/roasts/{ROAST_UUID.hex}/revisions',
            'download': f'/api/v1/roasts/{ROAST_UUID.hex}/revisions/{revision_number}/download',
        },
    }


def valid_aroast_ack_payload(*, modified_at: str = ACK_AT_TEXT) -> dict[str, object]:
    return {
        'success': True,
        'result': {
            'roast_id': ROAST_UUID.hex,
            'modified_at': modified_at,
        },
        'rlimit': 1000,
        'rusage': 5,
        'rremaining': 995,
    }


def test_identity_requires_exact_hyphenated_uuids_and_role() -> None:
    identity = parse_identity(
        {
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
    )
    assert isinstance(identity, ServerIdentity)
    assert identity.organization.id.hex == '22222222222242228222222222222222'

    bad = valid_identity_payload()
    bad['role'] = 'owner'
    with pytest.raises(ContractError, match='invalid server response'):
        parse_identity(bad)

    bad = valid_identity_payload()
    bad['user'] = {
        'id': ROAST_UUID.hex,
        'email': 'owner@example.test',
        'nickname': 'Owner',
    }
    with pytest.raises(ContractError, match='invalid server response'):
        parse_identity(bad)


@pytest.mark.parametrize('label_color', PALETTE_VALUES)
def test_roast_page_accepts_exact_backend_palette_values(label_color: LabelColor) -> None:
    page = parse_roast_page(valid_roast_page_payload(label_color=label_color, next_cursor='x' * MAX_CURSOR_CHARS))

    assert isinstance(page, RoastPage)
    assert page.next_cursor == 'x' * MAX_CURSOR_CHARS
    assert page.items[0].labels == (
        LabelSummary(label_uuid=LABEL_UUID, name='Production', color=label_color, archived=False),
    )


@pytest.mark.parametrize(
    ('state', 'revision_count'),
    (
        ('awaiting_profile', 0),
        ('parsed', 1),
        ('parse_failed', 2),
    ),
)
def test_roast_page_enforces_state_revision_count_matrix(state: str, revision_count: int) -> None:
    page = parse_roast_page(valid_roast_page_payload(state=state, revision_count=revision_count))
    assert page.items[0].state == state
    assert page.items[0].revision_count == revision_count

    bad = valid_roast_page_payload(
        state=state,
        revision_count=(1 if state == 'awaiting_profile' else 0),
    )
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(bad)


def test_roast_page_returns_detached_immutable_tuples() -> None:
    payload = valid_roast_page_payload()
    page = parse_roast_page(payload)

    assert isinstance(page.items, tuple)
    assert page.items[0].labels == (
        LabelSummary(label_uuid=LABEL_UUID, name='Production', color='green', archived=False),
    )

    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    labels = item['labels']
    assert isinstance(labels, list)
    labels[0]['name'] = 'Mutated'
    items.append(valid_roast_item_payload(state='awaiting_profile', revision_count=0))

    assert len(page.items) == 1
    assert page.items[0].labels[0].name == 'Production'


def test_roast_page_rejects_extra_key_bad_cursor_and_css_label_color() -> None:
    payload = valid_roast_page_payload()
    item = deepcopy(valid_roast_item_payload())
    item['internal_id'] = 'private'
    payload['items'] = [item]
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload(next_cursor='x' * (MAX_CURSOR_CHARS + 1))
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload(label_color='#00aa00')
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)


def test_detail_accepts_state_matrix_canonicalizes_metadata_and_detaches_source() -> None:
    awaiting = parse_roast_detail(
        valid_roast_detail_payload(
            state='awaiting_profile',
            revision_count=0,
            current_metadata={},
            include_current_revision=False,
        )
    )
    assert awaiting.state == 'awaiting_profile'
    assert awaiting.revision_count == 0
    assert awaiting.current_revision is None
    assert awaiting.current_metadata == ()

    parsed_payload = valid_roast_detail_payload(
        state='parsed',
        revision_count=2,
        current_metadata=reordered_metadata_payload(),
        revision_metadata=metadata_payload(),
    )
    parsed = parse_roast_detail(parsed_payload)
    assert parsed.current_revision is not None
    assert parsed.current_revision.parse_state == 'parsed'
    assert parsed.current_metadata == parsed.current_revision.metadata
    assert parsed.current_metadata == (
        ('roast', (('batch', 12), ('measurements', (('end', 212.4), ('start', 198.5))))),
        ('score', 89),
    )

    current_metadata = parsed_payload['current_metadata']
    assert isinstance(current_metadata, dict)
    current_metadata['score'] = 1
    roast_metadata = current_metadata['roast']
    assert isinstance(roast_metadata, dict)
    measurements = roast_metadata['measurements']
    assert isinstance(measurements, dict)
    measurements['end'] = 1.0
    revision = parsed_payload['current_revision']
    assert isinstance(revision, dict)
    revision_metadata = revision['metadata']
    assert isinstance(revision_metadata, dict)
    revision_metadata['score'] = 1
    labels = parsed_payload['labels']
    assert isinstance(labels, list)
    labels[0]['name'] = 'Mutated'

    assert parsed.current_metadata == (
        ('roast', (('batch', 12), ('measurements', (('end', 212.4), ('start', 198.5))))),
        ('score', 89),
    )
    assert parsed.labels[0].name == 'Production'

    failed = parse_roast_detail(valid_roast_detail_payload(state='parse_failed', revision_count=1))
    assert failed.current_revision is not None
    assert failed.current_revision.parse_state == 'failed'


def test_frozen_json_tags_arrays_without_changing_object_tuple_contract() -> None:
    payload = valid_roast_detail_payload(
        current_metadata=ambiguous_container_metadata(),
        revision_metadata=ambiguous_container_metadata(),
    )
    detail = parse_roast_detail(payload)
    metadata = dict(detail.current_metadata)

    assert metadata['empty_object'] == ()
    assert isinstance(metadata['empty_array'], FrozenJsonArray)
    assert isinstance(metadata['empty_array'], ExportedFrozenJsonArray)
    assert metadata['empty_array'] != metadata['empty_object']
    assert isinstance(metadata['pair_array'], FrozenJsonArray)
    assert isinstance(metadata['nested'], FrozenJsonArray)
    assert detail.current_revision is not None
    assert detail.current_revision.metadata == detail.current_metadata

    error_body = json.dumps(
        {
            'error': {
                'code': 'bad_request',
                'message': 'Invalid.',
                'details': [[], {}, [['key', 1]]],
            }
        },
        separators=(',', ':'),
    ).encode()
    error = parse_error_envelope(error_body)
    assert error is not None
    assert isinstance(error.details, FrozenJsonArray)


def test_detail_requires_state_revision_count_and_null_matrix() -> None:
    invalid_payloads = [
        valid_roast_detail_payload(state='awaiting_profile', revision_count=1, current_metadata={}, include_current_revision=False),
        valid_roast_detail_payload(state='awaiting_profile', revision_count=0),
        valid_roast_detail_payload(state='parsed', revision_count=0),
        valid_roast_detail_payload(state='parsed', revision_count=1, include_current_revision=False),
        valid_roast_detail_payload(state='parse_failed', revision_count=1, revision_parse_state='parsed'),
    ]

    for payload in invalid_payloads:
        with pytest.raises(ContractError, match='invalid server response'):
            parse_roast_detail(payload)


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('self', '/api/v1/roasts/' + 'f' * 32),
        ('chart', f'/api/v1/roasts/{ROAST_UUID.hex}/plots'),
        ('revisions', f'/api/v1/roasts/{ROAST_UUID.hex}/revision-list'),
    ),
)
def test_detail_rejects_each_inconsistent_context_link(field: str, value: str) -> None:
    payload = valid_roast_detail_payload()
    links = payload['links']
    assert isinstance(links, dict)
    links[field] = value

    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(payload)


def test_detail_rejects_metadata_cycles_and_depth_over_limit() -> None:
    cycle_payload = valid_roast_detail_payload(current_metadata=cyclic_metadata(), revision_metadata=cyclic_metadata())
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(cycle_payload)

    deep_payload = valid_roast_detail_payload(
        current_metadata=nested_metadata(65),
        revision_metadata=nested_metadata(65),
    )
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(deep_payload)


@pytest.mark.parametrize(
    ('state', 'revision_parse_state'),
    (
        ('parsed', 'parsed'),
        ('parse_failed', 'failed'),
    ),
)
def test_revision_upload_parses_body_shape_used_for_200_201_and_idempotent_responses(
    state: str,
    revision_parse_state: str,
) -> None:
    upload = parse_revision_upload(
        valid_upload_payload(state=state, revision_parse_state=revision_parse_state, revision_number=2)
    )

    assert upload.state == state
    assert upload.revision.revision_number == 2
    assert upload.revision.parse_state == revision_parse_state
    assert upload.links.download == f'/api/v1/roasts/{ROAST_UUID.hex}/revisions/2/download'


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('roast', '/api/v1/roasts/' + 'f' * 32),
        ('chart', f'/api/v1/roasts/{ROAST_UUID.hex}/plots'),
        ('revisions', f'/api/v1/roasts/{ROAST_UUID.hex}/revision-list'),
        ('download', f'/api/v1/roasts/{ROAST_UUID.hex}/revisions/9/download'),
    ),
)
def test_revision_upload_rejects_awaiting_state_bad_revision_state_and_each_context_link(
    field: str,
    value: str,
) -> None:
    payload = valid_upload_payload(state='awaiting_profile', revision_parse_state='parsed')
    with pytest.raises(ContractError, match='invalid server response'):
        parse_revision_upload(payload)

    payload = valid_upload_payload(state='parse_failed', revision_parse_state='parsed')
    with pytest.raises(ContractError, match='invalid server response'):
        parse_revision_upload(payload)

    payload = valid_upload_payload()
    links = payload['links']
    assert isinstance(links, dict)
    links[field] = value
    with pytest.raises(ContractError, match='invalid server response'):
        parse_revision_upload(payload)


def test_numeric_fields_reject_bool_unsafe_integer_nonfinite_and_overflow() -> None:
    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item['batch_number'] = True
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item['green_weight_kg'] = JS_SAFE_INTEGER_MAX + 1
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item['green_weight_kg'] = 10**4000
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item['green_weight_kg'] = float('inf')
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)


@pytest.mark.parametrize(
    'timestamp',
    (
        '2026-08-01T12:34:56Z',
        '2026-08-01T12:34:56.123456Z',
        '2026-08-01T12:34:56+00:00',
        '2026-08-01T12:34:56.123-07:30',
    ),
)
def test_timestamps_accept_exact_backend_format(timestamp: str) -> None:
    page = parse_roast_page(valid_roast_page_payload())
    assert page.items[0].roast_at.tzinfo is not None

    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item['roast_at'] = timestamp
    item['updated_at'] = timestamp
    parsed = parse_roast_page(payload)
    assert parsed.items[0].roast_at.tzinfo is not None
    assert parsed.items[0].updated_at.tzinfo is not None


@pytest.mark.parametrize(
    'timestamp',
    (
        '2026-08-01 12:34:56Z',
        '2026-08-01T12:34:56',
        '2026-08-01T12:34:56+0000',
        '2026-08-01T12:34:56.1234567Z',
        '2026-02-30T12:34:56Z',
        '2026-08-01t12:34:56Z',
    ),
)
def test_timestamps_reject_non_backend_variants(timestamp: str) -> None:
    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item['roast_at'] = timestamp
    item['updated_at'] = timestamp
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    ack_payload = valid_aroast_ack_payload(modified_at=timestamp)
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(ack_payload)


@pytest.mark.parametrize(
    'body',
    (
        b'<html>proxy secret</html>',
        b'{"error":{"code":"bad_request","code":"duplicate","message":"Server said no.","details":null}}',
        b'{"error":{"code":"bad_request","message":"line\\nsecret","details":null}}',
        '{"error":{"code":"bad_request","message":"bad\u0085news","details":null}}'.encode(),
        b'{"error":{"code":"bad_request","message":"\\ud800","details":null}}',
        b'\x80',
    ),
)
def test_error_parser_never_returns_html_duplicate_keys_controls_surrogates_or_invalid_utf8(
    body: bytes,
) -> None:
    assert parse_error_envelope(body) is None


def test_error_parser_enforces_depth_and_byte_limits() -> None:
    deep_body = json.dumps(
        {'error': {'code': 'bad_request', 'message': 'Too deep.', 'details': nested_metadata(65)}},
        separators=(',', ':'),
    ).encode('utf-8')
    assert parse_error_envelope(deep_body) is None

    assert parse_error_envelope(b'x' * (MAX_JSON_BYTES + 1)) is None


def test_error_parser_accepts_exact_multibyte_limit_and_rejects_501_code_points() -> None:
    exact_message = '☕' * 500
    body = json.dumps(
        {
            'error': {
                'code': 'bad_request',
                'message': exact_message,
                'details': {'status': 400, 'meta': {'b': 2, 'a': 1}},
            }
        },
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')

    error = parse_error_envelope(body)
    assert isinstance(error, ServerError)
    assert error == ServerError(
        code='bad_request',
        message=exact_message,
        details=(('meta', (('a', 1), ('b', 2))), ('status', 400)),
    )

    too_long = json.dumps(
        {'error': {'code': 'bad_request', 'message': '☕' * 501, 'details': None}},
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')
    assert parse_error_envelope(too_long) is None


def test_aroast_ack_accepts_bounded_compatibility_result_fields() -> None:
    payload = valid_aroast_ack_payload()
    result = payload['result']
    assert isinstance(result, dict)
    result.update(
        {
            'label': 'Archive Fixture Batch',
            'amount': 1.23,
            'end_weight_est': False,
            'coffee': {'hr_id': 'coffee-1', 'label': 'Ethiopia Worka'},
        }
    )

    ack = parse_aroast_ack(payload)

    assert ack.success is True
    assert ack.result.roast_id == ROAST_UUID


def test_aroast_ack_requires_success_matching_uuid_and_safe_counters() -> None:
    ack = parse_aroast_ack(valid_aroast_ack_payload())
    assert ack.success is True
    assert ack.result.roast_id == ROAST_UUID

    payload = valid_aroast_ack_payload()
    payload['success'] = False
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(payload)

    payload = valid_aroast_ack_payload()
    payload['result'] = {'roast_id': str(ROAST_UUID), 'modified_at': ACK_AT_TEXT}
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(payload)

    payload = valid_aroast_ack_payload()
    payload['rusage'] = JS_SAFE_INTEGER_MAX + 1
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(payload)
