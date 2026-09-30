#
# ABOUT
# Private local Santoker trace journals and explicit upload job ledger.
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

"""Synchronous disk boundary: construct/use off the UI and transport threads.

One lifetime process lock and an in-process lock serialize capture and job changes.
No credentials, HTTP, automatic upload, eviction, or arbitrary caller paths.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from artisanlib.roastserver import _filesystem as fs
from artisanlib.roastserver.origin import canonical_origin
from artisanlib.santoker_trace_contract import (
    DEFAULT_LIMITS, REASONS, Json, Record, TraceLimits, ValidatedTrace,
    TraceValidationError, encode_trace, validate_trace,
)


def utc_now() -> str:
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def canonical_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('invalid identity')
    if str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError('invalid identity')
    return value


def line_bytes(record: Record) -> bytes:
    return (json.dumps(record, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('ascii')


def summary_record(session_id: str, seq: int, mono_ns: int, accepted: int,
                   dropped: int, dropped_before: int, reasons: set[str]) -> Record:
    return {'kind': 'summary', 'session_id': session_id, 'seq': seq,
            'mono_ns': mono_ns, 'dropped_before': dropped_before,
            'ended_at': utc_now(), 'completion': 'incomplete' if reasons else 'complete',
            'reasons': cast(list[Json], sorted(reasons)), 'accepted_events': accepted,
            'dropped_events': dropped, 'last_seq': seq}


class StoreError(RuntimeError):
    """Fixed safe failure categories, suitable for polling (not raw OS errors)."""


@dataclass(frozen=True, slots=True)
class Destination:
    origin: str
    organization_id: str
    uploader_user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'origin', canonical_origin(self.origin))
        canonical_id(self.organization_id)
        canonical_id(self.uploader_user_id)

    def record(self) -> Record:
        return {'origin': self.origin, 'organization_id': self.organization_id,
                'uploader_user_id': self.uploader_user_id}


@dataclass(frozen=True, slots=True)
class UploadTicket:
    session_id: str
    authorization_id: str
    attempt_id: str
    destination: Destination
    artifact: Path
    sha256: str
    byte_size: int


@dataclass(slots=True)
class _Journal:
    manifest: Record
    stream: BinaryIO
    size: int
    accepted: int = 0
    seq: int = 0
    mono_ns: int = 0
    dropped: int = 0


class TraceStore:
    """Private flat trace root; unrelated entries count toward quota, never deleted.

    The root's parent must already exist. Existing private filesystem primitives
    reject links/reparse points and enforce current-user Windows DACLs, not chmod
    emulation. Same-user hostile mutation is outside that primitive's trust model.
    """

    def __init__(self, root: Path, *, disk_bytes: int = 1024**3,
                 preparation_bytes: int = 65 * 1024**2,
                 failure_bytes: int = 1024**2,
                 limits: TraceLimits = DEFAULT_LIMITS) -> None:
        self.root = Path(os.path.abspath(root))
        self.limits = limits
        self.disk_bytes = disk_bytes
        self.preparation_bytes = preparation_bytes
        self.failure_bytes = failure_bytes
        if preparation_bytes < limits.compressed_bytes or failure_bytes < 16384:
            raise ValueError('insufficient reserves')
        if disk_bytes <= preparation_bytes + failure_bytes + limits.record_bytes:
            raise ValueError('insufficient disk budget')
        self._mutex = threading.RLock()
        self._active: dict[str, _Journal] = {}
        self._held_uploads: set[str] = set()
        self._lock_fd: int | None = None
        self._used = 0
        self.failure: str | None = None
        # Validate all ancestors before any mkdir/chmod, including POSIX links.
        for parent in reversed(self.root.parents):
            fs.require_directory_path(parent)
        fs.prepare_private_root(self.root)
        lock = self.root / '.trace.lock'
        if not os.path.lexists(lock):
            try:
                os.close(fs.create_generated_file(self.root, lock))
            except fs.FilesystemError:
                if not os.path.lexists(lock):
                    raise
        info = fs.generated_entry_stat(self.root, lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StoreError('unsafe_entry')
        descriptor = fs.open_generated_lock(self.root, lock)
        if not fs.try_acquire_file_lock(descriptor):
            os.close(descriptor)
            raise StoreError('store_busy')
        self._lock_fd = descriptor
        try:
            self._scan_usage()
            self._recover_metadata()
            self.recover()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._mutex:
            if self._held_uploads:
                raise StoreError('session_busy')
            for journal in self._active.values():
                journal.stream.close()
            self._active.clear()
            if self._lock_fd is not None:
                fs.release_file_lock(self._lock_fd)
                os.close(self._lock_fd)
                self._lock_fd = None

    def _ensure_open(self) -> None:
        if self._lock_fd is None:
            raise StoreError('store_closed')

    def _path(self, session_id: str, suffix: str) -> Path:
        return self.root / f'{canonical_id(session_id)}.{suffix}'

    def _scan_usage(self) -> None:
        used = 0
        for path in self.root.iterdir():
            info = fs.generated_entry_stat(self.root, path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StoreError('unsafe_entry')
            fs.verify_private_permissions(path, 0o600)
            used += info.st_size
        self._used = used
        if used > self.disk_bytes:
            self.failure = 'storage_limit'

    def _reserve(self, amount: int, *, capture: bool = False, metadata: bool = False,
                 summary: bool = False) -> None:
        ceiling = self.disk_bytes
        if not metadata:
            ceiling -= self.failure_bytes + len(self._active) * self.limits.record_bytes
            if summary:
                ceiling += self.limits.record_bytes
        if capture:
            ceiling -= self.preparation_bytes
        if self._used + amount > ceiling:
            self.failure = 'storage_limit'
            raise StoreError('storage_limit')
        # Charge before IO; a partial/uncertain write never undercounts quota.
        self._used += amount

    def _open(self, path: Path) -> BinaryIO:
        descriptor = fs.open_generated_file(self.root, path)
        try:
            info = os.fstat(descriptor)
            if info.st_nlink != 1:
                raise StoreError('unsafe_entry')
            fs.verify_private_permissions(path, 0o600)
            return os.fdopen(descriptor, 'rb')
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _sync(stream: BinaryIO) -> None:
        stream.flush()
        fs.fsync_descriptor(stream.fileno())

    @staticmethod
    def _write(stream: BinaryIO, data: bytes) -> None:
        if stream.write(data) != len(data):
            raise StoreError('io_error')

    def _save(self, session_id: str, metadata: Record) -> None:
        data = line_bytes(metadata)
        if len(data) > 16384:
            raise StoreError('invalid_metadata')
        self._reserve(len(data), metadata=True)
        temporary = self._path(session_id, 'meta-new')
        # A stale atomic-write candidate is evidence; never overwrite it.
        with os.fdopen(fs.create_generated_file(self.root, temporary), 'wb') as stream:
            self._write(stream, data)
            self._sync(stream)
        fs.replace_generated(self.root, temporary, self._path(session_id, 'json'))
        fs.fsync_directory(self.root)
        self._scan_usage()

    def _load(self, session_id: str, suffix: str = 'json') -> Record:
        with self._open(self._path(session_id, suffix)) as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise StoreError('invalid_metadata')
        value = cast(Json, json.loads(data))
        keys = {'session_id', 'state', 'destination', 'authorization_id', 'attempt_id', 'sha256',
                'byte_size', 'receipt', 'failure', 'delete_intent'}
        if not isinstance(value, dict) or value.keys() != keys or value['session_id'] != session_id:
            raise StoreError('invalid_metadata')
        if value['state'] not in {'active', 'finalizing', 'retained', 'authorized', 'preparing',
                                  'uploading', 'receipt_persisted', 'deletion_pending', 'removed', 'damaged'}:
            raise StoreError('invalid_metadata')
        if value['failure'] is not None and value['failure'] not in REASONS:
            raise StoreError('invalid_metadata')
        destination = value['destination']
        if destination is not None:
            if not isinstance(destination, dict) or destination.keys() != {
                    'origin', 'organization_id', 'uploader_user_id'}:
                raise StoreError('invalid_metadata')
            if any(not isinstance(item, str) for item in destination.values()):
                raise StoreError('invalid_metadata')
            Destination(**cast(dict[str, str], destination))
            canonical_id(cast(str, value['authorization_id']))
        if not isinstance(value['sha256'], str) or (value['sha256'] and re.fullmatch(
                '[0-9a-f]{64}', value['sha256']) is None):
            raise StoreError('invalid_metadata')
        if type(value['byte_size']) is not int or not 0 <= value['byte_size'] <= self.limits.compressed_bytes:
            raise StoreError('invalid_metadata')
        if destination is None and value['authorization_id'] != '':
            raise StoreError('invalid_metadata')
        if value['attempt_id'] != '':
            canonical_id(value['attempt_id'])
        state = value['state']
        artifact_present = bool(value['sha256']) and value['byte_size'] > 0
        if state in {'retained', 'authorized', 'preparing', 'uploading', 'receipt_persisted'} and not artifact_present:
            raise StoreError('invalid_metadata')
        if state in {'authorized', 'preparing', 'uploading', 'receipt_persisted'} and destination is None:
            raise StoreError('invalid_metadata')
        if state in {'preparing', 'uploading', 'receipt_persisted'} and not value['attempt_id']:
            raise StoreError('invalid_metadata')
        if value['receipt'] is not None:
            if not artifact_present or destination is None:
                raise StoreError('invalid_metadata')
            self._check_receipt(value, value['receipt'])
        if state == 'receipt_persisted' and value['receipt'] is None:
            raise StoreError('invalid_metadata')
        intent = value['delete_intent']
        if state in {'deletion_pending', 'removed'}:
            self._check_delete_intent(intent)
            if cast(Record, intent)['authority'] == 'receipt' and value['receipt'] is None:
                raise StoreError('invalid_metadata')
        elif intent is not None:
            raise StoreError('invalid_metadata')
        return value

    @staticmethod
    def _check_delete_intent(intent: Json) -> None:
        if not isinstance(intent, dict) or intent.keys() != {'authority', 'files'}:
            raise StoreError('invalid_metadata')
        if intent['authority'] not in {'user', 'receipt'} or not isinstance(intent['files'], list):
            raise StoreError('invalid_metadata')
        files = intent['files']
        if len(files) > 5:
            raise StoreError('invalid_metadata')
        suffixes: set[str] = set()
        for entry in files:
            if not isinstance(entry, dict) or entry.keys() != {'suffix', 'device', 'inode'}:
                raise StoreError('invalid_metadata')
            suffix = entry['suffix']
            if not isinstance(suffix, str) or suffix not in {
                    'journal', 'gz', 'part', 'part-damaged', 'meta-damaged'} or suffix in suffixes:
                raise StoreError('invalid_metadata')
            suffixes.add(suffix)
            if any(type(entry[key]) is not int or cast(int, entry[key]) < 0 for key in ('device', 'inode')):
                raise StoreError('invalid_metadata')

    @staticmethod
    def _metadata(session_id: str) -> Record:
        return {'session_id': session_id, 'state': 'active', 'destination': None,
                'authorization_id': '', 'attempt_id': '', 'sha256': '', 'byte_size': 0,
                'receipt': None, 'failure': None, 'delete_intent': None}

    def list_sessions(self) -> list[Record]:
        """Disk operation; later UI worker must cache/page this result, not poll on UI."""
        with self._mutex:
            self._ensure_open()
            result: list[Record] = []
            for path in sorted(self.root.glob('*.json')):
                try:
                    canonical_id(path.stem)
                except ValueError:
                    continue
                result.append(self._load(path.stem))
            return result

    def read_session(self, session_id: str) -> Record:
        with self._mutex:
            self._ensure_open()
            return self._load(session_id)

    def begin(self, manifest: Record) -> None:
        with self._mutex:
            self._ensure_open()
            session_id = cast(str, manifest['session_id'])
            # Public contract validation, without changing/copying its internals.
            terminal = summary_record(session_id, 1, 0, 0, 0, 0, set())
            encode_trace((manifest, terminal), io.BytesIO(), limits=self.limits)
            data = line_bytes(manifest)
            self._save(session_id, self._metadata(session_id))
            self._reserve(len(data) + self.limits.record_bytes, capture=True)
            self._used -= self.limits.record_bytes  # now represented by active reservation
            stream = os.fdopen(fs.create_generated_file(self.root, self._path(session_id, 'journal')), 'wb', buffering=0)
            journal = _Journal(manifest, stream, len(data))
            self._active[session_id] = journal
            self._write(stream, data)
            self._sync(stream)
            fs.fsync_directory(self.root)

    def append(self, session_id: str, record: Record) -> str | None:
        """Return a known cap reason without writing this event; IO errors raise."""
        with self._mutex:
            self._ensure_open()
            journal = self._active[session_id]
            data = line_bytes(record)
            if len(data) > self.limits.record_bytes:
                return 'capture_error'
            if journal.accepted >= self.limits.events - 1:
                return 'event_limit'
            if journal.size + len(data) > self.limits.expanded_bytes - self.limits.record_bytes:
                return 'storage_limit'
            # Validate this exact event independently; final encode validates the stream.
            seq = cast(int, record['seq'])
            probe = dict(record)
            probe['dropped_before'] = seq - 1
            terminal = summary_record(session_id, seq + 1, cast(int, record['mono_ns']),
                                      1, seq - 1, 0, {'capture_error'} if seq > 1 else set())
            try:
                encode_trace((journal.manifest, probe, terminal), io.BytesIO(), limits=self.limits)
            except TraceValidationError:
                return 'capture_error'
            try:
                self._reserve(len(data), capture=True)
            except StoreError:
                return 'storage_limit'
            self._write(journal.stream, data)
            journal.size += len(data)
            journal.accepted += 1
            journal.seq = seq
            journal.mono_ns = cast(int, record['mono_ns'])
            journal.dropped += cast(int, record['dropped_before'])
            return None

    def checkpoint(self) -> None:
        with self._mutex:
            self._ensure_open()
            for journal in self._active.values():
                self._sync(journal.stream)

    def abandon(self, session_id: str, reason: str = 'io_error') -> None:
        """Best-effort durable failure ledger; original journal remains recoverable."""
        with self._mutex:
            self._ensure_open()
            self.failure = reason
            journal = self._active.pop(session_id, None)
            if journal is not None:
                journal.stream.close()
            metadata = self._load(session_id)
            metadata['failure'] = reason
            self._save(session_id, metadata)

    def seal(self, session_id: str, summary: Record) -> ValidatedTrace:
        with self._mutex:
            self._ensure_open()
            journal = self._active[session_id]
            metadata = self._load(session_id)
            metadata['state'] = 'finalizing'
            self._save(session_id, metadata)
            data = line_bytes(summary)
            if len(data) > self.limits.record_bytes:
                raise StoreError('invalid_summary')
            self._reserve(len(data), summary=True)
            self._write(journal.stream, data)
            self._sync(journal.stream)
            journal.stream.close()
            del self._active[session_id]
            result = self._artifact(session_id, self._journal_records(session_id))
            self._retain(session_id, metadata, result)
            return result

    def _journal_records(self, session_id: str, *, stop_at_summary: bool = False) -> Iterator[Record]:
        with self._open(self._path(session_id, 'journal')) as stream:
            while data := stream.readline(self.limits.record_bytes + 1):
                if len(data) > self.limits.record_bytes or not data.endswith(b'\n'):
                    raise StoreError('damaged_journal')
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise StoreError('damaged_journal')
                record = cast(Record, value)
                yield record
                if stop_at_summary and record.get('kind') == 'summary':
                    return

    def _artifact(self, session_id: str, records: Iterator[Record]) -> ValidatedTrace:
        artifact = self._path(session_id, 'gz')
        if os.path.lexists(artifact):
            with self._open(artifact) as source:
                return validate_trace(source, expected_session_id=session_id, limits=self.limits)
        self._reserve(self.limits.compressed_bytes)
        with os.fdopen(fs.create_generated_file(self.root, self._path(session_id, 'part')), 'wb') as stream:
            result = encode_trace(records, stream, limits=self.limits)
            self._sync(stream)
        fs.replace_generated(self.root, self._path(session_id, 'part'), artifact)
        fs.fsync_directory(self.root)
        self._scan_usage()
        return result

    def _retain(self, session_id: str, metadata: Record, result: ValidatedTrace) -> None:
        metadata.update(state='retained', sha256=result.sha256, byte_size=result.byte_size)
        self._save(session_id, metadata)

    def _recovered_records(self, session_id: str) -> Iterator[Record]:
        """Salvage only a validated prefix; never modify damaged source evidence."""
        seq = mono = accepted = dropped = size = 0
        reasons = {'crash_recovery'}
        failure = self._load(session_id)['failure']
        if isinstance(failure, str):
            reasons.add(failure)
        with self._open(self._path(session_id, 'journal')) as stream:
            data = stream.readline(self.limits.record_bytes + 1)
            if not data.endswith(b'\n') or len(data) > self.limits.record_bytes:
                raise StoreError('damaged_manifest')
            manifest = cast(Record, json.loads(data))
            encode_trace((manifest, summary_record(session_id, 1, 0, 0, 0, 0, reasons)),
                         io.BytesIO(), limits=self.limits)
            yield manifest
            size = len(data)
            while data := stream.readline(self.limits.record_bytes + 1):
                try:
                    if len(data) > self.limits.record_bytes or not data.endswith(b'\n'):
                        raise ValueError('partial tail')
                    decoded = json.loads(data)
                    if not isinstance(decoded, dict):
                        raise ValueError('non-object record')
                    event = cast(Record, decoded)
                    if event.get('kind') == 'summary':
                        # Revalidate the terminated prefix without retaining its bytes.
                        # A durable terminal record knows more than the recovered
                        # event prefix: preserve its exact tail losses and UTC time.
                        with open(os.devnull, 'wb') as sink:
                            validated = encode_trace(self._journal_records(session_id, stop_at_summary=True),
                                                     sink, limits=self.limits)
                        if stream.read(1):
                            reasons.add('capture_error')
                        terminal = dict(validated.summary)
                        terminal.update(completion='incomplete', reasons=cast(list[Json], sorted(
                            reasons | set(cast(list[str], terminal['reasons'])))))
                        yield terminal
                        return
                    next_seq = cast(int, event['seq'])
                    next_mono = cast(int, event['mono_ns'])
                    gap = cast(int, event['dropped_before'])
                    if next_seq != seq + gap + 1 or next_mono < mono:
                        raise ValueError('ordering')
                    probe = dict(event)
                    probe['dropped_before'] = next_seq - 1
                    encode_trace((manifest, probe, summary_record(session_id, next_seq + 1,
                                 next_mono, 1, next_seq - 1, 0, reasons)), io.BytesIO(), limits=self.limits)
                    if size + len(data) > self.limits.expanded_bytes - self.limits.record_bytes:
                        raise ValueError('body limit')
                    if accepted >= self.limits.events - 1:
                        raise ValueError('event limit')
                except (ValueError, TypeError, KeyError):
                    reasons.add('capture_error')
                    break
                seq, mono = next_seq, next_mono
                accepted += 1
                dropped += gap
                size += len(data)
                yield event
        yield summary_record(session_id, seq + 1, mono, accepted, dropped, 0, reasons)

    def _recover_metadata(self) -> None:
        # Re-sync valid atomic candidates before publication. Invalid candidates
        # are retained under one bounded evidence name, never silently discarded.
        for path in self.root.glob('*.meta-new'):
            session_id = path.name.removesuffix('.meta-new')
            try:
                canonical_id(session_id)
            except ValueError:
                continue
            try:
                self._load(session_id, 'meta-new')
            except (ValueError, TypeError, KeyError, RuntimeError):
                damaged = self._path(session_id, 'meta-damaged')
                if os.path.lexists(damaged):
                    raise StoreError('damaged_metadata') from None
                fs.replace_generated(self.root, path, damaged)
                self.failure = 'io_error'
            else:
                # Windows FlushFileBuffers requires a writable native handle.
                descriptor = fs.open_generated_lock(self.root, path)
                try:
                    fs.fsync_descriptor(descriptor)
                finally:
                    os.close(descriptor)
                fs.replace_generated(self.root, path, self._path(session_id, 'json'))
            fs.fsync_directory(self.root)
        # Corrupt canonical ledgers cannot establish upload authorization or a
        # receipt. Preserve them and expose a delete-only damaged session.
        for path in self.root.glob('*.json'):
            session_id = path.stem
            try:
                canonical_id(session_id)
            except ValueError:
                continue
            try:
                self._load(session_id)
            except (ValueError, TypeError, KeyError, RuntimeError):
                damaged = self._path(session_id, 'meta-damaged')
                if os.path.lexists(damaged):
                    raise StoreError('damaged_metadata') from None
                fs.replace_generated(self.root, path, damaged)
                fs.fsync_directory(self.root)
                self.failure = 'io_error'
                metadata = self._metadata(session_id)
                metadata.update(state='damaged', failure='io_error')
                self._save(session_id, metadata)
        self._scan_usage()

    def recover(self) -> None:
        """Startup never uploads. Interrupted artifacts/journals retain their evidence."""
        with self._mutex:
            self._ensure_open()
            for metadata in self.list_sessions():
                session_id = cast(str, metadata['session_id'])
                try:
                    state = metadata['state']
                    if state in {'active', 'finalizing'}:
                        partial = self._path(session_id, 'part')
                        if os.path.lexists(partial) and not os.path.lexists(self._path(session_id, 'gz')):
                            evidence = self._path(session_id, 'part-damaged')
                            if os.path.lexists(evidence):
                                raise StoreError('damaged_artifact')
                            fs.replace_generated(self.root, partial, evidence)
                            fs.fsync_directory(self.root)
                        result = self._artifact(session_id, self._recovered_records(session_id))
                        self._retain(session_id, metadata, result)
                    elif state in {'preparing', 'uploading'}:
                        metadata['state'] = 'authorized'
                        self._save(session_id, metadata)
                    elif state in {'receipt_persisted', 'deletion_pending'}:
                        self._delete(session_id, metadata)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    self.failure = 'io_error'
                    # Do not erase a valid receipt or its deletion intent on failure.
                    if metadata['state'] in {'active', 'finalizing'}:
                        metadata.update(state='damaged', failure='io_error')
                        try:
                            self._save(session_id, metadata)
                        except (OSError, RuntimeError):
                            pass  # persistent source evidence + polled store failure

    def authorize(self, session_id: str, destination: Destination) -> None:
        """Explicit user action ONLY; new identity creates a new authorization epoch."""
        with self._mutex:
            self._ensure_open()
            metadata = self._load(session_id)
            if metadata['state'] not in {'retained', 'authorized'}:
                raise StoreError('session_busy')
            metadata.update(state='authorized', destination=destination.record(),
                            authorization_id=str(uuid4()), attempt_id='')
            self._save(session_id, metadata)

    def prepare(self, session_id: str) -> UploadTicket:
        """Explicit Retry/Upload worker action; artifact is already immutable/durable."""
        with self._mutex:
            self._ensure_open()
            metadata = self._load(session_id)
            if metadata['state'] != 'authorized':
                raise StoreError('not_authorized')
            metadata.update(state='preparing', attempt_id=str(uuid4()))
            self._save(session_id, metadata)
            try:
                with self._open(self._path(session_id, 'gz')) as source:
                    result = validate_trace(source, expected_session_id=session_id,
                                            expected_sha256=cast(str, metadata['sha256']), limits=self.limits)
                if result.byte_size != metadata['byte_size']:
                    raise StoreError('artifact_mismatch')
            except (OSError, RuntimeError, ValueError):
                metadata.update(state='authorized', failure='io_error')
                self.failure = 'io_error'
                self._save(session_id, metadata)
                raise StoreError('preparation_failed') from None
            return self._ticket(metadata)

    def _ticket(self, metadata: Record) -> UploadTicket:
        destination = cast(dict[str, str], metadata['destination'])
        session_id = cast(str, metadata['session_id'])
        return UploadTicket(session_id, cast(str, metadata['authorization_id']),
                            cast(str, metadata['attempt_id']), Destination(**destination),
                            self._path(session_id, 'gz'), cast(str, metadata['sha256']),
                            cast(int, metadata['byte_size']))

    def begin_upload(self, ticket: UploadTicket, authenticated: Destination) -> None:
        with self._mutex:
            self._ensure_open()
            metadata = self._load(ticket.session_id)
            if metadata['state'] != 'preparing' or self._ticket(metadata) != ticket or authenticated != ticket.destination:
                raise StoreError('identity_mismatch')
            metadata['state'] = 'uploading'
            self._save(ticket.session_id, metadata)

    @contextmanager
    def open_upload(self, ticket: UploadTicket) -> Iterator[BinaryIO]:
        """Hold the validated descriptor until the caller's actual HTTP call settles.

        No mutex is held across the yield. Completion/failure and store close must
        follow context exit; while held, no new attempt or deletion is permitted.
        """
        with self._mutex:
            self._ensure_open()
            metadata = self._load(ticket.session_id)
            if metadata['state'] != 'uploading' or self._ticket(metadata) != ticket:
                raise StoreError('stale_job')
            if ticket.session_id in self._held_uploads:
                raise StoreError('session_busy')
            source = self._open(ticket.artifact)
            try:
                if os.fstat(source.fileno()).st_size != ticket.byte_size:
                    raise StoreError('artifact_mismatch')
                result = validate_trace(source, expected_session_id=ticket.session_id,
                                        expected_sha256=ticket.sha256, limits=self.limits)
                if result.byte_size != ticket.byte_size:
                    raise StoreError('artifact_mismatch')
                source.seek(0)
            except BaseException:
                source.close()
                raise
            self._held_uploads.add(ticket.session_id)
        try:
            yield source
        finally:
            try:
                source.close()
            finally:
                with self._mutex:
                    self._held_uploads.remove(ticket.session_id)

    def upload_failed(self, ticket: UploadTicket) -> None:
        """Retain pinned identity and exact bytes; another explicit Retry is required."""
        with self._mutex:
            self._ensure_open()
            metadata = self._load(ticket.session_id)
            if ticket.session_id in self._held_uploads:
                raise StoreError('session_busy')
            if metadata['state'] not in {'preparing', 'uploading'} or self._ticket(metadata) != ticket:
                raise StoreError('stale_job')
            metadata['state'] = 'authorized'
            self._save(ticket.session_id, metadata)

    @staticmethod
    def _check_receipt(metadata: Record, receipt: Json) -> None:
        fields = {'id', 'session_id', 'organization_id', 'uploader_user_id', 'sha256',
                  'byte_size', 'stored_at', 'storage_status'}
        if not isinstance(receipt, dict) or receipt.keys() != fields:
            raise StoreError('invalid_receipt')
        for key in ('id', 'session_id', 'organization_id', 'uploader_user_id'):
            canonical_id(cast(str, receipt[key]))
        stored = receipt['stored_at']
        if not isinstance(stored, str) or re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z', stored) is None:
            raise StoreError('invalid_receipt')
        datetime.strptime(stored, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=UTC)
        destination = cast(Record, metadata['destination'])
        if (receipt['storage_status'] != 'stored' or receipt['session_id'] != metadata['session_id']
                or receipt['sha256'] != metadata['sha256'] or type(receipt['byte_size']) is not int
                or receipt['byte_size'] != metadata['byte_size'] or metadata['destination'] is None
                or receipt['organization_id'] != destination['organization_id']
                or receipt['uploader_user_id'] != destination['uploader_user_id']):
            raise StoreError('invalid_receipt')

    def accept_receipt(self, ticket: UploadTicket, receipt: Record, *, request_origin: str) -> None:
        with self._mutex:
            self._ensure_open()
            metadata = self._load(ticket.session_id)
            if ticket.session_id in self._held_uploads:
                raise StoreError('session_busy')
            if (metadata['state'] != 'uploading' or self._ticket(metadata) != ticket
                    or canonical_origin(request_origin) != ticket.destination.origin):
                raise StoreError('stale_job')
            self._check_receipt(metadata, receipt)
            metadata.update(state='receipt_persisted', receipt=dict(receipt))
            self._save(ticket.session_id, metadata)  # durable BEFORE any unlink
            self._delete(ticket.session_id, metadata)

    def delete_local(self, session_id: str) -> None:
        with self._mutex:
            self._ensure_open()
            metadata = self._load(session_id)
            if metadata['state'] not in {'retained', 'authorized', 'damaged', 'deletion_pending', 'receipt_persisted'}:
                raise StoreError('session_busy')
            self._delete(session_id, metadata, explicit=True)

    def _delete(self, session_id: str, metadata: Record, *, explicit: bool = False) -> None:
        if metadata['delete_intent'] is None:
            if not explicit and metadata['state'] != 'receipt_persisted':
                raise StoreError('invalid_metadata')
            files: list[Json] = []
            for suffix in ('journal', 'gz', 'part', 'part-damaged', 'meta-damaged'):
                path = self._path(session_id, suffix)
                if os.path.lexists(path):
                    info = fs.generated_entry_stat(self.root, path)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise StoreError('unsafe_entry')
                    files.append({'suffix': suffix, 'device': info.st_dev, 'inode': info.st_ino})
            metadata['delete_intent'] = {'authority': 'user' if explicit else 'receipt', 'files': files}
        # Persist all selected identities before the filesystem primitive can move
        # any bytes to a random quarantine name. Never infer ownership by UUID alone.
        metadata['state'] = 'deletion_pending'
        self._save(session_id, metadata)
        try:
            intent = cast(Record, metadata['delete_intent'])
            for entry in cast(list[Record], intent['files']):
                identity = (cast(int, entry['device']), cast(int, entry['inode']))
                path = self._path(session_id, cast(str, entry['suffix']))
                if os.path.lexists(path):
                    self._unlink_owned(path, identity)
                pattern = f'.artisan-quarantine-{identity[0]:x}-{identity[1]:x}-'
                for candidate in self.root.glob(f'{pattern}*'):
                    if re.fullmatch(re.escape(pattern) + '[0-9a-f]{32}', candidate.name):
                        self._unlink_owned(candidate, identity)
            if any(os.path.lexists(self._path(session_id, suffix)) for suffix in (
                    'journal', 'gz', 'part', 'part-damaged', 'meta-damaged')):
                raise StoreError('deletion_pending')
            fs.fsync_directory(self.root)
            metadata['state'] = 'removed'
            self._save(session_id, metadata)
        except (OSError, RuntimeError):
            self.failure = 'io_error'
            raise StoreError('deletion_pending') from None

    def _unlink_owned(self, path: Path, identity: tuple[int, int]) -> None:
        info = fs.generated_entry_stat(self.root, path)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != identity):
            raise StoreError('unsafe_entry')
        if not fs.unlink_generated_file(self.root, path, identity):
            raise StoreError('deletion_pending')
