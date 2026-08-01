from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest

from artisanlib.roastserver.contract import (
    ContractError,
    JS_SAFE_INTEGER_MAX,
    MAX_CURSOR_CHARS,
    LabelSummary,
    PublicFailure,
    RoastDetail,
    RoastPage,
    RevisionUpload,
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
PROFILE_BYTES = repr(
    {
        'roastUUID': ROAST_UUID.hex,
        'roastepoch': 1785587696,
        'mode': 'C',
        'weight': [1.0, 0.85],
        'computed': {},
    }
).encode('utf-8')
IDEMPOTENCY_KEY = 'archive-v1:44444444-4444-4444-8444-444444444444'
SHA256 = hashlib.sha256(PROFILE_BYTES).hexdigest()
MODIFIED = datetime(2026, 8, 1, 12, 34, 56, 123456, tzinfo=UTC)


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
    number: int = 1,
    sha256: str | None = None,
    byte_size: int | None = None,
) -> dict[str, object]:
    return {
        'revision_number': number,
        'sha256': SHA256 if sha256 is None else sha256,
        'byte_size': len(PROFILE_BYTES) if byte_size is None else byte_size,
        'parser_version': '2026.8.1',
        'parse_state': 'parsed',
        'parse_diagnostic_code': None,
        'parse_diagnostic_message': None,
        'uploaded_at': MODIFIED.isoformat(),
        'metadata': {},
        'reparse_recommended': False,
    }


def valid_roast_item_payload() -> dict[str, object]:
    return {
        'roast_uuid': ROAST_UUID.hex,
        'state': 'parsed',
        'roast_at': MODIFIED.isoformat(),
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
        'revision_count': 1,
        'updated_at': MODIFIED.isoformat(),
        'labels': [
            {
                'label_uuid': LABEL_UUID.hex,
                'name': 'Green',
                'color': '#00aa00',
                'archived': False,
            }
        ],
    }


def valid_roast_page_payload() -> dict[str, object]:
    return {'items': [valid_roast_item_payload()], 'next_cursor': None}


def valid_roast_detail_payload() -> dict[str, object]:
    payload = valid_roast_item_payload()
    payload['current_metadata'] = {}
    payload['current_revision'] = valid_revision_payload()
    payload['links'] = {
        'self': f'/api/v1/roasts/{ROAST_UUID.hex}',
        'chart': f'/api/v1/roasts/{ROAST_UUID.hex}/chart',
        'revisions': f'/api/v1/roasts/{ROAST_UUID.hex}/revisions',
    }
    return payload


def valid_upload_payload() -> dict[str, object]:
    revision = valid_revision_payload()
    return {
        'roast_uuid': ROAST_UUID.hex,
        'state': 'parsed',
        'revision': revision,
        'links': {
            'roast': f'/api/v1/roasts/{ROAST_UUID.hex}',
            'chart': f'/api/v1/roasts/{ROAST_UUID.hex}/chart',
            'revisions': f'/api/v1/roasts/{ROAST_UUID.hex}/revisions',
            'download': f'/api/v1/roasts/{ROAST_UUID.hex}/revisions/{revision["revision_number"]}/download',
        },
    }


def valid_aroast_ack_payload() -> dict[str, object]:
    return {
        'success': True,
        'result': {
            'roast_id': ROAST_UUID.hex,
            'modified_at': MODIFIED.isoformat(),
        },
        'rlimit': 1000,
        'rusage': 5,
        'rremaining': 995,
    }


def nested_metadata(depth: int) -> dict[str, object]:
    value: dict[str, object] = {}
    root = value
    for index in range(depth):
        child: dict[str, object] = {'level': index}
        value['next'] = child
        value = child
    return root


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
    user = deepcopy(bad['user'])
    user['id'] = ROAST_UUID.hex
    bad['user'] = user
    with pytest.raises(ContractError, match='invalid server response'):
        parse_identity(bad)


def test_roast_page_parses_exact_contract_into_detached_immutable_tuples() -> None:
    page = parse_roast_page(valid_roast_page_payload())
    assert isinstance(page, RoastPage)
    assert page.next_cursor is None
    assert isinstance(page.items, tuple)
    assert page.items[0].roast_uuid == ROAST_UUID
    assert page.items[0].labels == (
        LabelSummary(label_uuid=LABEL_UUID, name='Green', color='#00aa00', archived=False),
    )


def test_roast_page_rejects_extra_key_bad_cursor_and_invalid_label() -> None:
    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = deepcopy(items[0])
    item['internal_id'] = 'private'
    payload['items'] = [item]
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload()
    payload['next_cursor'] = 'x' * (MAX_CURSOR_CHARS + 1)
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = deepcopy(items[0])
    labels = item['labels']
    assert isinstance(labels, list)
    label = deepcopy(labels[0])
    label['color'] = 'green'
    item['labels'] = [label]
    payload['items'] = [item]
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)


def test_roast_page_rejects_bool_as_int_and_nonfinite_numbers() -> None:
    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = deepcopy(items[0])
    item['batch_number'] = True
    payload['items'] = [item]
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)

    payload = valid_roast_page_payload()
    items = payload['items']
    assert isinstance(items, list)
    item = deepcopy(items[0])
    item['green_weight_kg'] = float('nan')
    payload['items'] = [item]
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_page(payload)


def test_detail_parses_current_revision_metadata_and_relative_links() -> None:
    detail = parse_roast_detail(valid_roast_detail_payload())
    assert isinstance(detail, RoastDetail)
    assert detail.current_revision is not None
    assert detail.current_revision.sha256 == SHA256
    assert detail.current_metadata == ()
    assert detail.links.self_path == f'/api/v1/roasts/{ROAST_UUID.hex}'


def test_detail_requires_state_revision_and_relative_link_consistency() -> None:
    payload = valid_roast_detail_payload()
    payload['links']['self'] = '/api/v1/roasts/' + 'f' * 32
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(payload)

    payload = valid_roast_detail_payload()
    payload['revision_count'] = 2
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(payload)

    payload = valid_roast_detail_payload()
    payload['state'] = 'awaiting_profile'
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(payload)


def test_detail_rejects_metadata_depth_past_limit() -> None:
    payload = valid_roast_detail_payload()
    payload['current_metadata'] = nested_metadata(65)
    with pytest.raises(ContractError, match='invalid server response'):
        parse_roast_detail(payload)


def test_revision_upload_requires_matching_links_and_safe_integer_sizes() -> None:
    upload = parse_revision_upload(valid_upload_payload())
    assert isinstance(upload, RevisionUpload)
    assert upload.links.download.endswith('/revisions/1/download')

    payload = valid_upload_payload()
    payload['links']['download'] = f'/api/v1/roasts/{ROAST_UUID.hex}/revisions/9/download'
    with pytest.raises(ContractError, match='invalid server response'):
        parse_revision_upload(payload)

    payload = valid_upload_payload()
    payload['revision']['byte_size'] = JS_SAFE_INTEGER_MAX + 1
    with pytest.raises(ContractError, match='invalid server response'):
        parse_revision_upload(payload)


def test_aroast_ack_requires_success_matching_uuid_and_safe_counters() -> None:
    ack = parse_aroast_ack(valid_aroast_ack_payload())
    assert ack.success is True
    assert ack.result.roast_id == ROAST_UUID

    payload = valid_aroast_ack_payload()
    payload['success'] = False
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(payload)

    payload = valid_aroast_ack_payload()
    payload['result'] = {'roast_id': str(ROAST_UUID), 'modified_at': MODIFIED.isoformat()}
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(payload)

    payload = valid_aroast_ack_payload()
    payload['rusage'] = JS_SAFE_INTEGER_MAX + 1
    with pytest.raises(ContractError, match='invalid server response'):
        parse_aroast_ack(payload)


def test_error_parser_never_returns_html_controls_or_oversized_text() -> None:
    for body in (
        b'<html>proxy secret</html>',
        b'{"error":{"code":"bad","message":"line\\nsecret","details":null}}',
    ):
        assert parse_error_envelope(body) is None

    body = (
        '{"error":{"code":"bad_request","message":"'
        + ('x' * 501)
        + '","details":null}}'
    ).encode('utf-8')
    assert parse_error_envelope(body) is None


def test_error_parser_returns_only_safe_public_text() -> None:
    error = parse_error_envelope(
        b'{"error":{"code":"bad_request","message":"Server said no.","details":{"status":400}}}'
    )
    assert isinstance(error, ServerError)
    assert error == ServerError(
        code='bad_request',
        message='Server said no.',
        details=(('status', 400),),
    )
    assert not isinstance(error, PublicFailure)
