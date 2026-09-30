"""Shared v1 contract vectors; no Qt, database, network, or hardware fixtures."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import zlib
from collections.abc import Buffer
from dataclasses import replace
from pathlib import Path
from typing import cast, override

import pytest

from artisanlib.santoker_trace_contract import (
    GZIP_HEADER,
    Json,
    Record,
    TraceLimits,
    TraceValidationError,
    encode_trace,
    validate_trace,
)

DATA = Path(__file__).parent / 'data' / 'diagnostic-traces-v1'
DIGEST = 'a4129615d192ce4645a0fbc756147d20a02c701395ecf58e3e2342a4c2827201'
SESSION = '11111111-1111-4111-8111-111111111111'


def rows() -> list[Record]:
    lines = DATA.joinpath('incomplete.jsonl').read_bytes().splitlines()
    return [json.loads(line) for line in lines]


def raw_gzip(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=6, wbits=-15)
    return (GZIP_HEADER + compressor.compress(data) + compressor.flush()
            + struct.pack('<II', zlib.crc32(data), len(data)))


def bundle(records: list[Record]) -> bytes:
    return raw_gzip(b''.join((json.dumps(r, separators=(',', ':')) + '\n').encode()
                            for r in records))


def complete_rows() -> list[Record]:
    records = rows()
    return [records[0], dict(records[-1], seq=1, mono_ns=0, dropped_before=0,
                            accepted_events=0, dropped_events=0, last_seq=1,
                            completion='complete', reasons=[])]


class FragmentedReader(io.BytesIO):
    def __init__(self, data: bytes, fragment_size: int) -> None:
        super().__init__(data)
        self.fragment_size = fragment_size

    @override
    def read(self, size: int | None = -1) -> bytes:
        assert size is not None and 0 < size <= 65536
        return super().read(min(size, self.fragment_size))


@pytest.mark.parametrize('fragment_size', [1, 2, 7, 10, 31, 65536])
def test_golden_vector(fragment_size: int) -> None:
    data = DATA.joinpath('incomplete.jsonl.gz').read_bytes()
    assert DATA.joinpath('incomplete.sha256').read_text().strip() == DIGEST
    assert hashlib.sha256(data).hexdigest() == DIGEST
    result = validate_trace(FragmentedReader(data, fragment_size),
                            expected_session_id=SESSION, expected_sha256=DIGEST)
    assert result.byte_size == 784
    assert result.expanded_size == 2926
    assert result.summary['completion'] == 'incomplete'
    assert result.summary['dropped_events'] == 3
    assert result.manifest == rows()[0]
    for _ in range(2):
        target = io.BytesIO()
        encoded = encode_trace(iter(rows()), target)
        assert target.getvalue() == data
        assert encoded == result
    assert zlib.decompress(data, wbits=31) == DATA.joinpath('incomplete.jsonl').read_bytes()


def test_empty_complete_session_and_recovered_incomplete_session() -> None:
    records = complete_rows()
    assert validate_trace(io.BytesIO(bundle(records))).summary['completion'] == 'complete'
    records[-1].update(completion='incomplete', reasons=['crash_recovery'])
    assert validate_trace(io.BytesIO(bundle(records))).summary['completion'] == 'incomplete'


@pytest.mark.parametrize(('index', 'path', 'value'), [
    (0, 'schema_version', 2), (0, 'schema_version', True),
    (0, 'session_id', 'not-a-uuid'), (0, 'session_id', '0' * 32),
    (0, 'device', 'other'), (0, 'transport', 'tcp'),
    (0, 'app.password', 'canary'), (0, 'app.name', 'other'),
    (0, 'os.hostname', 'canary'), (0, 'os.family', 'other'),
    (0, 'os.architecture', 'hostname'), (0, 'os.version', '/home/user/private'),
    (0, 'app.version', 'x' * 65), (0, 'ble_mac', 'canary'),
    (0, 'settings', {'Authorization': 'canary'}),
    (0, 'started_at', '2026-01-01T00:00:00Z'),
    (0, 'started_at', '2026-02-30T00:00:00.000000Z'),
    (0, 'clock.origin_ns', -1), (0, 'clock.unit', 'ms'),
    (0, 'clock.source', 'wall'), (0, 'sample_interval_ms', 0),
    (0, 'temperature_unit', 'K'),
    (1, 'kind', 'log'), (1, 'seq', 2), (1, 'seq', True),
    (1, 'mono_ns', -1), (1, 'mono_ns', 2**63),
    (1, 'session_id', '22222222-2222-4222-8222-222222222222'),
    (1, 'message', 'canary'), (1, 'severity', 'debug'),
    (2, 'connection_id', 'ble-address'), (2, 'state', 'arbitrary'),
    (3, 'mono_ns', 0), (3, 'direction', 'tx'),
    (3, 'characteristic', '6e400002-b5a3-f393-e0a9-e50e24dcca9e'),
    (3, 'payload.byte_length', 3), (3, 'payload.byte_length', True),
    (3, 'payload.byte_length', -1), (3, 'payload.byte_length', 65537),
    (3, 'payload.data', '!!!!'), (3, 'payload.data', 'AP8NCg==='),
    (3, 'payload.data', 'AP8NCh=='), (3, 'payload.data', 'é'),
    (3, 'payload.encoding', 'hex'), (3, 'payload.token', 'canary'),
    (4, 'operation_id', 'mutable'), (5, 'chunk_index', -1),
    (5, 'response', 1), (6, 'outcome', 'acknowledged'),
    (7, 'result', 'exception message'), (8, 'value', 'canary'),
    (8, 'value', 10001), (8, 'value', float('inf')),
    (8, 'value', float('nan')), (8, 'action', 'auth'),
    (9, 'roast_ordinal', 0), (9, 'name', 'local.alog'),
    (10, 'dropped_before', 0), (10, 'reason', 'arbitrary'),
    (-1, 'accepted_events', 11), (-1, 'dropped_events', 2),
    (-1, 'last_seq', 13), (-1, 'completion', 'complete'),
    (-1, 'completion', 'active'), (-1, 'reasons', []),
    (-1, 'reasons', ['crash_recovery', 'crash_recovery']),
    (-1, 'reasons', [{}]), (-1, 'ended_at', 'invalid'),
])
def test_reject_invalid_fields(index: int, path: str, value: Json) -> None:
    records = rows()
    target = records[index]
    keys = path.split('.')
    for key in keys[:-1]:
        target = cast(Record, target[key])
    target[keys[-1]] = value
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(bundle(records)))
    with pytest.raises(TraceValidationError):
        encode_trace(records, io.BytesIO())


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'nonterminal', 'manifest_only'])
def test_summary_required_and_terminal(mutation: str) -> None:
    records = rows()
    if mutation == 'missing':
        records.pop()
    elif mutation == 'duplicate':
        records.append(records[-1])
    elif mutation == 'nonterminal':
        records[-1], records[-2] = records[-2], records[-1]
    else:
        records = records[:1]
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(bundle(records)))


@pytest.mark.parametrize('suffix', [b'x', b'\0', GZIP_HEADER, raw_gzip(b'')])
def test_reject_trailing_data_or_second_member(suffix: bytes) -> None:
    data = DATA.joinpath('incomplete.jsonl.gz').read_bytes()
    with pytest.raises(TraceValidationError):
        validate_trace(FragmentedReader(data + suffix, 1))


def test_every_truncated_prefix_rejected() -> None:
    data = DATA.joinpath('incomplete.jsonl.gz').read_bytes()
    for end in range(len(data)):
        with pytest.raises(TraceValidationError):
            validate_trace(io.BytesIO(data[:end]))


@pytest.mark.parametrize('offset', [0, 2, 3, 4, 8, 9, -1, -5, -8])
def test_bad_gzip_header_crc_and_length(offset: int) -> None:
    data = bytearray(DATA.joinpath('incomplete.jsonl.gz').read_bytes())
    data[offset] ^= 1
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(data))


@pytest.mark.parametrize('data', [
    b'', b'\n', b'{}', b'[]\n', b'null\n', b'\xff\n', b'\xef\xbb\xbf{}\n',
    b'{"kind":"manifest","kind":"manifest"}\n',
    b'{"x":{"password":1,"password":2}}\n',
    b'{"x":NaN}\n', b'{"x":Infinity}\n', b'{"x":1e999}\n',
    b'{"x":' + b'[' * 9 + b'0' + b']' * 9 + b'}\n',
    b'{"x":' + b'1' * 5000 + b'}\n',
    b' ' * 65536 + b'\n',
])
def test_malicious_json(data: bytes) -> None:
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(raw_gzip(data)))


@pytest.mark.parametrize('field', [b'"schema_version":1', b'"name":"Artisan"'])
def test_duplicate_recognized_key_in_otherwise_valid_bundle(field: bytes) -> None:
    data = b''.join((json.dumps(record, separators=(',', ':')) + '\n').encode()
                    for record in complete_rows())
    validate_trace(io.BytesIO(raw_gzip(data)))
    assert data.count(field) == 1
    duplicated = data.replace(field, field + b',' + field, 1)
    with pytest.raises(TraceValidationError, match='invalid JSON record'):
        validate_trace(io.BytesIO(raw_gzip(duplicated)))


@pytest.mark.parametrize('fragment_size', [997, 65536])
def test_valid_bundle_spans_multiple_decode_chunks(fragment_size: int) -> None:
    templates = rows()
    records = [templates[0]]
    for seq in range(1, 2501):
        payload = hashlib.sha256(str(seq).encode('ascii')).digest()
        records.append(dict(templates[3], seq=seq, mono_ns=seq, dropped_before=0,
                            payload={'encoding': 'base64', 'byte_length': len(payload),
                                     'data': base64.b64encode(payload).decode('ascii')}))
    records.append(dict(complete_rows()[-1], seq=2501, mono_ns=2501,
                        accepted_events=2500, last_seq=2501))
    destination = io.BytesIO()
    encoded = encode_trace(records, destination)
    assert encoded.expanded_size > 4 * 65536
    assert encoded.byte_size > 65536
    assert validate_trace(FragmentedReader(destination.getvalue(), fragment_size)) == encoded
    assert encoded.summary['accepted_events'] == 2500


def test_missing_field_and_extra_manifest() -> None:
    records = rows()
    del records[0]['app']
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(bundle(records)))
    records = rows()
    records.insert(1, records[0])
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(bundle(records)))


def test_payload_boundaries_verbatim_and_explicit_redaction() -> None:
    records = rows()
    arbitrary = b'password=CANARY\x00\xff\r\n'
    payload: Record = {'encoding': 'base64', 'byte_length': len(arbitrary),
                       'data': base64.b64encode(arbitrary).decode('ascii')}
    records[3]['payload'] = payload
    output = io.BytesIO()
    encode_trace(records, output)
    lines = zlib.decompress(output.getvalue(), 31).splitlines()
    decoded_rows = [json.loads(line) for line in lines]
    assert decoded_rows[3]['payload'] == payload  # No heuristic secret detection/byte alteration.
    assert len(decoded_rows) == len(records)  # RX notification/TX chunk boundaries unchanged.
    records[3]['payload'] = {'encoding': 'redacted', 'byte_length': len(arbitrary),
                             'reason': 'sensitive_protocol_field'}
    validate_trace(io.BytesIO(bundle(records)))
    cast(Record, records[3]['payload'])['data'] = 'must not survive redaction'
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(bundle(records)))


def test_multiple_notification_and_chunk_boundaries_across_reconnect() -> None:
    templates = rows()
    records = [templates[0]]
    for connection_id in ['22222222-2222-4222-8222-222222222222',
                          '44444444-4444-4444-8444-444444444444']:
        for index, data in enumerate([b'\x00\xff', b'\r', b'\n\x00\xff']):
            payload: Record = {'encoding': 'base64', 'byte_length': len(data),
                               'data': base64.b64encode(data).decode('ascii')}
            for template in [templates[3], templates[5]]:
                event = dict(template, connection_id=connection_id, payload=payload,
                             seq=len(records), mono_ns=len(records))
                if event['kind'] == 'tx_attempt':
                    event['operation_id'] = connection_id
                    event['chunk_index'] = index
                records.append(event)
    records.append(dict(complete_rows()[-1], seq=len(records), mono_ns=len(records),
                        accepted_events=len(records) - 1, last_seq=len(records)))
    output = io.BytesIO()
    encode_trace(records, output)
    validate_trace(FragmentedReader(output.getvalue(), 7))
    decoded = [json.loads(line) for line in zlib.decompress(output.getvalue(), 31).splitlines()]
    assert decoded == records


def test_clock_correction_and_same_admission_time_allowed() -> None:
    records = rows()
    records[3]['mono_ns'] = records[2]['mono_ns']
    records[8]['value'] = 25.5
    records[-1]['ended_at'] = '2025-12-31T23:59:59.000000Z'
    validate_trace(io.BytesIO(bundle(records)))


def test_device_ack_is_distinct_not_inferred_from_write() -> None:
    records = rows()
    records[7] = dict(records[6], kind='device_ack', seq=7, mono_ns=700, code=1)
    del records[7]['chunk_index']
    del records[7]['outcome']
    validate_trace(io.BytesIO(bundle(records)))
    assert rows()[6]['outcome'] == 'written'
    assert all(record['kind'] != 'device_ack' for record in rows())


def test_exact_resource_boundaries_and_terminal_reserve() -> None:
    data = DATA.joinpath('incomplete.jsonl.gz').read_bytes()
    raw = DATA.joinpath('incomplete.jsonl').read_bytes()
    lines = raw.splitlines(keepends=True)
    max_line = max(map(len, lines))
    body = len(raw) - len(lines[-1])
    limits = TraceLimits(compressed_bytes=len(data), expanded_bytes=body + max_line,
                         record_bytes=max_line, events=11, nesting=2)
    validate_trace(io.BytesIO(data), limits=limits)
    for invalid in [replace(limits, compressed_bytes=len(data) - 1),
                    replace(limits, expanded_bytes=body + max_line - 1),
                    replace(limits, record_bytes=max_line - 1),
                    replace(limits, events=10), replace(limits, nesting=1)]:
        with pytest.raises(TraceValidationError):
            validate_trace(io.BytesIO(data), limits=invalid)
        with pytest.raises(TraceValidationError):
            encode_trace(rows(), io.BytesIO(), limits=invalid)


def test_expansion_bomb_and_giant_unterminated_line() -> None:
    limits = TraceLimits(expanded_bytes=2048, record_bytes=1024)
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(raw_gzip(b' ' * 1000000)), limits=limits)


@pytest.mark.parametrize(('session', 'digest'), [
    ('invalid', DIGEST), ('22222222-2222-4222-8222-222222222222', DIGEST),
    (SESSION, DIGEST.upper()), (SESSION, '0' * 64), (SESSION, 'bad'),
])
def test_expected_artifact_identity(session: str, digest: str) -> None:
    with pytest.raises(TraceValidationError):
        validate_trace(io.BytesIO(DATA.joinpath('incomplete.jsonl.gz').read_bytes()),
                       expected_session_id=session, expected_sha256=digest)


class ShortWriter(io.BytesIO):
    @override
    def write(self, data: Buffer, /) -> int:
        return super().write(memoryview(data)[:-1])


def test_encoder_short_write_and_invalid_input_not_publishable() -> None:
    with pytest.raises(TraceValidationError, match='short write'):
        encode_trace(rows(), ShortWriter())
    with pytest.raises(TraceValidationError):
        encode_trace(rows()[:-1], io.BytesIO())


@pytest.mark.parametrize('field', ['compressed_bytes', 'expanded_bytes', 'record_bytes',
                                  'events', 'nesting'])
def test_invalid_limit_configuration(field: str) -> None:
    with pytest.raises(ValueError):
        replace(TraceLimits(), **{field: 0})
