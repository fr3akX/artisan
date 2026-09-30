#
# ABOUT
# Pure Santoker BLE diagnostic trace v1 wire contract.
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Bounded, synchronous helpers; callers must run these off UI/async event loops.

The server and desktop carry independent copies, with identical golden vectors.
See docs/diagnostic-traces-v1.md. No capture, persistence, or HTTP side effects.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, cast
from uuid import UUID

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type Record = dict[str, Json]

MAX_INTEGER = 2**63 - 1
CHUNK_SIZE = 65536
GZIP_HEADER = bytes.fromhex('1f8b08000000000000ff')
RX_UUID = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'
TX_UUID = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'
REASONS = frozenset({
    'crash_recovery', 'queue_overflow', 'storage_limit', 'event_limit',
    'io_error', 'cleanup_timeout', 'capture_error',
})
COMMON = frozenset({'kind', 'session_id', 'seq', 'mono_ns', 'dropped_before'})
FIELDS = {
    'connection': {'connection_id', 'state'},
    'rx': {'connection_id', 'characteristic', 'direction', 'payload'},
    'command_intent': {'connection_id', 'operation_id', 'payload'},
    'tx_attempt': {'connection_id', 'operation_id', 'chunk_index', 'characteristic',
                   'direction', 'response', 'payload'},
    'tx_result': {'connection_id', 'operation_id', 'chunk_index', 'outcome'},
    'device_ack': {'connection_id', 'operation_id', 'code'},
    'parser': {'connection_id', 'result'},
    'control': {'operation_id', 'action', 'value'},
    'milestone': {'roast_ordinal', 'name'},
    'status': {'severity', 'code'},
    'gap': {'reason'},
    'summary': {'ended_at', 'completion', 'reasons', 'accepted_events',
                'dropped_events', 'last_seq'},
}


class TraceValidationError(ValueError):
    """Invalid bundle; messages are fixed categories, never untrusted contents."""


@dataclass(frozen=True)
class TraceLimits:
    compressed_bytes: int = 65 * 1024 * 1024
    expanded_bytes: int = 64 * 1024 * 1024
    record_bytes: int = 64 * 1024
    events: int = 500000  # Includes the mandatory terminal summary, not the manifest.
    nesting: int = 8

    def __post_init__(self) -> None:
        if any(type(v) is not int or v <= 0 for v in (
            self.compressed_bytes, self.expanded_bytes, self.record_bytes,
            self.events, self.nesting,
        )) or self.expanded_bytes <= self.record_bytes:
            raise ValueError('invalid trace limits')


DEFAULT_LIMITS = TraceLimits()


@dataclass(frozen=True)
class ValidatedTrace:
    manifest: Record
    summary: Record
    sha256: str
    byte_size: int
    expanded_size: int


def _require(condition: bool, category: str) -> None:
    if not condition:
        raise TraceValidationError(category)


def _object(value: Json, fields: set[str] | frozenset[str]) -> Record:
    _require(isinstance(value, dict) and value.keys() == fields, 'invalid fields')
    return cast(Record, value)


def _integer(value: Json, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    _require(type(value) is int and minimum <= value <= maximum, 'invalid integer')
    return cast(int, value)


def _choice(value: Json, choices: set[str] | frozenset[str]) -> None:
    _require(isinstance(value, str) and value in choices, 'invalid enum')


def _uuid(value: Json) -> str:
    _require(isinstance(value, str), 'invalid UUID')
    text = cast(str, value)
    try:
        parsed = UUID(text)
    except ValueError:
        raise TraceValidationError('invalid UUID') from None
    _require(str(parsed) == text and parsed.int != 0, 'invalid UUID')
    return text


def _utc(value: Json) -> datetime:
    _require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z', value
    ) is not None, 'invalid UTC timestamp')
    try:
        return datetime.fromisoformat(cast(str, value).replace('Z', '+00:00'))
    except ValueError:
        raise TraceValidationError('invalid UTC timestamp') from None


def _version(value: Json) -> None:
    _require(isinstance(value, str) and re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}', value
    ) is not None, 'invalid version')


def _payload(value: Json) -> None:
    _require(isinstance(value, dict), 'invalid payload')
    payload = cast(Record, value)
    if payload.get('encoding') == 'base64':
        _object(payload, {'encoding', 'byte_length', 'data'})
        length = _integer(payload['byte_length'], maximum=CHUNK_SIZE)
        data = payload['data']
        _require(isinstance(data, str), 'invalid base64')
        try:
            decoded = base64.b64decode(cast(str, data), validate=True)
        except (ValueError, binascii.Error):
            raise TraceValidationError('invalid base64') from None
        _require(len(decoded) == length and base64.b64encode(decoded).decode('ascii') == data,
                 'invalid base64 length or padding')
    else:
        _object(payload, {'encoding', 'byte_length', 'reason'})
        _choice(payload['encoding'], {'redacted'})
        _integer(payload['byte_length'], maximum=CHUNK_SIZE)
        _choice(payload['reason'], {'sensitive_protocol_field'})


def _manifest(record: Record) -> None:
    _object(record, {'kind', 'schema_version', 'session_id', 'device', 'transport',
                     'app', 'os', 'started_at', 'clock', 'sample_interval_ms', 'temperature_unit'})
    _choice(record['kind'], {'manifest'})
    _integer(record['schema_version'], 1, 1)
    _uuid(record['session_id'])
    _choice(record['device'], {'Santoker'})
    _choice(record['transport'], {'ble'})
    app = _object(record['app'], {'name', 'version'})
    _choice(app['name'], {'Artisan'})
    _version(app['version'])
    os_info = _object(record['os'], {'family', 'version', 'architecture'})
    _choice(os_info['family'], {'linux', 'macos', 'windows'})
    _version(os_info['version'])
    _choice(os_info['architecture'], {'x86', 'x86_64', 'arm', 'arm64', 'other'})
    _utc(record['started_at'])
    clock = _object(record['clock'], {'source', 'unit', 'origin_ns'})
    _choice(clock['source'], {'monotonic'})
    _choice(clock['unit'], {'ns'})
    _integer(clock['origin_ns'])
    _integer(record['sample_interval_ms'], 1, 60000)
    _choice(record['temperature_unit'], {'C', 'F'})


def _event_fields(record: Record, kind: str) -> None:
    for key in ('connection_id', 'operation_id'):
        if key in record:
            _uuid(record[key])
    if 'payload' in record:
        _payload(record['payload'])
    if 'chunk_index' in record:
        _integer(record['chunk_index'], maximum=500000)
    if kind in {'rx', 'tx_attempt'}:
        _choice(record['direction'], {'rx' if kind == 'rx' else 'tx'})
        _choice(record['characteristic'], {RX_UUID if kind == 'rx' else TX_UUID})
    if kind == 'tx_attempt':
        _require(type(record['response']) is bool, 'invalid response flag')
    elif kind == 'connection':
        _choice(record['state'], {
            'scan_started', 'scan_failed', 'connect_started', 'connected', 'connect_failed',
            'subscribe_started', 'subscribed', 'subscribe_failed', 'unsubscribe_started',
            'unsubscribed', 'unsubscribe_failed', 'disconnect_started', 'disconnected',
            'disconnect_failed',
        })
    elif kind == 'tx_result':
        _choice(record['outcome'], {'written', 'rejected', 'timeout', 'cancelled', 'error'})
    elif kind == 'device_ack':
        _integer(record['code'], maximum=65535)
    elif kind == 'parser':
        _choice(record['result'], {'accepted', 'noise', 'truncated', 'invalid_header',
                                   'invalid_length', 'crc_mismatch', 'invalid_tail'})
    elif kind == 'control':
        _choice(record['action'], {'power', 'fan', 'drum', 'machine_on', 'heating_on',
                                   'warmup', 'warmup_target'})
        _require(type(record['value']) in {bool, int, float}, 'invalid control value')
        _require(-1000 <= cast(float, record['value']) <= 10000, 'invalid control value')
    elif kind == 'milestone':
        _integer(record['roast_ordinal'], 1, 500000)
        _choice(record['name'], {'roast_start', 'roast_end', 'charge', 'dry_end',
                                'fc_start', 'fc_end', 'sc_start', 'sc_end', 'drop', 'cool_end'})
    elif kind == 'status':
        _choice(record['severity'], {'info', 'warning', 'error'})
        _choice(record['code'], REASONS | {'session_on', 'off_requested', 'cleanup_finished',
                                         'protocol_ready', 'protocol_not_ready'})
    elif kind == 'gap':
        _choice(record['reason'], REASONS)
        _integer(record['dropped_before'], 1)


class _Records:
    """Constant-state validation; gap ranges are implicit, never accumulated."""

    def __init__(self, limits: TraceLimits, session_id: str | None) -> None:
        self.limits = limits
        self.expected_session = None if session_id is None else _uuid(session_id)
        self.manifest: Record | None = None
        self.summary: Record | None = None
        self.seq = 0
        self.mono_ns = 0
        self.accepted = 0
        self.dropped = 0
        self.expanded = 0

    def add(self, record: Record, size: int) -> None:
        _require(self.summary is None, 'record after summary')
        self.expanded += size
        _require(self.expanded <= self.limits.expanded_bytes, 'expanded limit')
        if self.manifest is None:
            _manifest(record)
            _require(self.expected_session is None or record['session_id'] == self.expected_session,
                     'session mismatch')
            self.manifest = record
        else:
            kind = record.get('kind')
            _require(isinstance(kind, str) and kind in FIELDS, 'invalid kind')
            kind = cast(str, kind)
            _object(record, COMMON | FIELDS[kind])
            _require(record['session_id'] == self.manifest['session_id'], 'session mismatch')
            seq = _integer(record['seq'], 1)
            dropped = _integer(record['dropped_before'])
            _require(seq == self.seq + dropped + 1, 'unaccounted sequence gap')
            mono_ns = _integer(record['mono_ns'])
            _require(mono_ns >= self.mono_ns, 'clock reversed')
            self.seq, self.mono_ns = seq, mono_ns
            self.dropped += dropped
            _require(self.accepted + 1 <= self.limits.events, 'event limit')
            if kind == 'summary':
                self._summary(record)
                self.summary = record
                return
            _event_fields(record, kind)
            self.accepted += 1
            _require(self.accepted < self.limits.events, 'terminal event reserve')
        _require(self.expanded <= self.limits.expanded_bytes - self.limits.record_bytes,
                 'terminal byte reserve')

    def _summary(self, record: Record) -> None:
        # UTC may step backwards after clock correction; monotonic alone defines duration.
        _utc(record['ended_at'])
        _choice(record['completion'], {'complete', 'incomplete'})
        reasons = record['reasons']
        _require(isinstance(reasons, list) and len(reasons) <= len(REASONS), 'invalid reasons')
        reasons = cast(list[Json], reasons)
        for reason in reasons:
            _choice(reason, REASONS)
        _require(len(set(cast(list[str], reasons))) == len(reasons), 'duplicate reasons')
        _require(_integer(record['accepted_events']) == self.accepted, 'accepted count mismatch')
        _require(_integer(record['dropped_events']) == self.dropped, 'dropped count mismatch')
        _require(_integer(record['last_seq']) == self.seq, 'last sequence mismatch')
        _require((record['completion'] == 'complete' and not reasons and self.dropped == 0)
                 or (record['completion'] == 'incomplete' and bool(reasons)),
                 'invalid completion')

    def finish(self, digest: str, size: int) -> ValidatedTrace:
        _require(self.manifest is not None and self.summary is not None, 'missing terminal summary')
        return ValidatedTrace(cast(Record, self.manifest), cast(Record, self.summary),
                              digest, size, self.expanded)


def _pairs(pairs: list[tuple[str, Json]]) -> Record:
    result: Record = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _finite(value: str) -> float:
    result = float(value)
    _require(math.isfinite(result), 'nonfinite number')
    return result


def _parse(line: bytes, limits: TraceLimits) -> Record:
    _require(len(line) <= limits.record_bytes, 'record limit')
    _require(line.endswith(b'\n'), 'unterminated record')
    # Bound nesting before calling the recursive standard-library decoder.
    depth = 0
    quoted = escaped = False
    for byte in line:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            _require(depth <= limits.nesting, 'nesting limit')
        elif byte in (93, 125):
            depth -= 1
    try:
        value = json.loads(line.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_float=_finite, parse_constant=_finite)
    except (ValueError, RecursionError):
        raise TraceValidationError('invalid JSON record') from None
    _require(isinstance(value, dict), 'record must be object')
    return cast(Record, value)


def validate_trace(source: BinaryIO, *, expected_session_id: str | None = None,
                   expected_sha256: str | None = None,
                   limits: TraceLimits = DEFAULT_LIMITS) -> ValidatedTrace:
    """Consume exactly one gzip member using bounded reads and decompression.

    Does not close source. No result is returned before the trailer, EOF, terminal
    summary and optional compressed digest/session assertions have been checked.
    """
    if expected_sha256 is not None:
        _require(re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is not None, 'invalid digest')
    records = _Records(limits, expected_session_id)
    decoder = zlib.decompressobj(wbits=31)
    digest = hashlib.sha256()
    size = expanded = 0
    pending = bytearray()
    header = bytearray()
    while True:
        block = source.read(min(CHUNK_SIZE, limits.compressed_bytes - size + 1))
        if not block:
            break
        size += len(block)
        _require(size <= limits.compressed_bytes, 'compressed limit')
        digest.update(block)
        if len(header) < len(GZIP_HEADER):
            header.extend(block[:len(GZIP_HEADER) - len(header)])
            _require(GZIP_HEADER.startswith(header), 'invalid gzip header')
        _require(not decoder.eof, 'trailing compressed data')
        while block:
            try:
                output = decoder.decompress(block, min(CHUNK_SIZE,
                                            limits.expanded_bytes - expanded + 1))
            except zlib.error:
                raise TraceValidationError('invalid gzip') from None
            expanded += len(output)
            _require(expanded <= limits.expanded_bytes, 'expanded limit')
            pending.extend(output)
            while (end := pending.find(b'\n')) >= 0:
                line = bytes(pending[:end + 1])
                del pending[:end + 1]
                records.add(_parse(line, limits), len(line))
            _require(len(pending) < limits.record_bytes, 'record limit')
            _require(not decoder.unused_data, 'trailing compressed data')
            block = decoder.unconsumed_tail
    _require(decoder.eof, 'truncated gzip')
    _require(not pending, 'unterminated record')
    result = records.finish(digest.hexdigest(), size)
    _require(expected_sha256 is None or result.sha256 == expected_sha256, 'digest mismatch')
    return result


def encode_trace(records: Iterable[Record], destination: BinaryIO, *,
                 limits: TraceLimits = DEFAULT_LIMITS) -> ValidatedTrace:
    """Stream canonical JSONL into deterministic single-member gzip (mtime=0).

    On any exception, destination is partial/unpublishable: caller must discard it.
    Input is record-at-a-time; caller must not construct unbounded input records.
    No local durability or storage acknowledgement is implied by this return value.
    """
    state = _Records(limits, None)
    # Explicit header avoids platform-dependent gzip OS bytes. Raw DEFLATE
    # version differences remain possible: retries must reuse immutable bytes.
    encoder = zlib.compressobj(level=6, wbits=-15)
    digest = hashlib.sha256()
    size = crc = 0

    def write(block: bytes) -> None:
        nonlocal size
        size += len(block)
        _require(size <= limits.compressed_bytes, 'compressed limit')
        digest.update(block)
        if block:
            _require(destination.write(block) == len(block), 'short write')

    write(GZIP_HEADER)
    for record in records:
        line = bytearray()
        try:
            for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False, sort_keys=True,
                                         separators=(',', ':')).iterencode(record):
                # Check before encoding a possibly large string fragment.
                _require(len(line) + len(part) + 1 <= limits.record_bytes, 'record limit')
                line.extend(part.encode('ascii'))
        except (ValueError, TypeError, RecursionError):
            raise TraceValidationError('invalid JSON record') from None
        line.append(10)
        data = bytes(line)
        state.add(_parse(data, limits), len(data))
        crc = zlib.crc32(data, crc)
        write(encoder.compress(data))
    state.finish('', 0)
    write(encoder.flush())
    write(struct.pack('<II', crc, state.expanded & 0xffffffff))
    return state.finish(digest.hexdigest(), size)
