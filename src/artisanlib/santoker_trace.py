#
# ABOUT
# Bounded Qt-independent Santoker trace recorder.
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

"""A session-bound sink with bounded admission and one background disk writer.

Construction/start/emit/close/status do not touch disk. Mutable payloads must be
copied at admission; base64, validation, JSON, compression and fsync run on writer.
A handle's lifetime is independent of subsequent ON cycles. No per-packet signals,
futures, or per-session threads. See docs/santoker-local-recording.md.
"""
from __future__ import annotations

import base64
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
import re
from pathlib import Path
import threading
import time
from typing import cast
from uuid import uuid4

from artisanlib.santoker_trace_contract import DEFAULT_LIMITS, FIELDS, REASONS, Record, TraceLimits
from artisanlib.santoker_trace_store import TraceStore, summary_record, utc_now


type Scalar = str | int | float | bool


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    app_version: str
    os_family: str
    os_version: str
    architecture: str
    sample_interval_ms: int = 1000
    temperature_unit: str = 'C'

    def __post_init__(self) -> None:
        if (any(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}', value) is None
                for value in (self.app_version, self.os_version))
                or self.os_family not in {'linux', 'macos', 'windows'}
                or self.architecture not in {'x86', 'x86_64', 'arm', 'arm64', 'other'}
                or self.temperature_unit not in {'C', 'F'}
                or type(self.sample_interval_ms) is not int or not 1 <= self.sample_interval_ms <= 60000):
            raise ValueError('invalid capture configuration')

    def manifest(self, session_id: str, origin_ns: int) -> Record:
        return {'kind': 'manifest', 'schema_version': 1, 'session_id': session_id,
                'device': 'Santoker', 'transport': 'ble',
                'app': {'name': 'Artisan', 'version': self.app_version},
                'os': {'family': self.os_family, 'version': self.os_version,
                       'architecture': self.architecture},
                'started_at': utc_now(),
                'clock': {'source': 'monotonic', 'unit': 'ns', 'origin_ns': origin_ns},
                'sample_interval_ms': self.sample_interval_ms, 'temperature_unit': self.temperature_unit}


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    session_id: str
    state: str
    attempted_events: int
    stored_events: int
    dropped_events: int
    queued_events: int
    queued_bytes: int
    reasons: tuple[str, ...]
    failure: str | None


@dataclass(slots=True)
class _Session:
    session_id: str
    origin_ns: int
    manifest: Record
    state: str = 'active'
    seq: int = 0
    stored_seq: int = 0
    stored: int = 0
    dropped: int = 0
    mono_ns: int = 0
    queued: int = 0
    queued_bytes: int = 0
    reasons: set[str] = field(default_factory=set)
    failure: str | None = None
    deadline: float | None = None
    begun: bool = False
    stopped: bool = False


@dataclass(frozen=True, slots=True)
class _Event:
    session: _Session
    seq: int
    mono_ns: int
    kind: str
    fields: Record
    payload: bytes | None
    charge: int


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """Immutable identity; old transport closures retain THIS sink, never current ON."""
    session_id: str
    _recorder: TraceRecorder = field(repr=False, compare=False)
    _session: _Session = field(repr=False, compare=False)

    def emit(self, kind: str, fields: Mapping[str, Scalar], *,
             payload: bytes | bytearray | memoryview | None = None) -> bool:
        """True means bounded queue admission, NOT durable storage/transport success."""
        return self._recorder._emit(self._session, kind, fields, payload)

    def request_close(self, *, cleanup_timeout: float = 5.0) -> None:
        """OFF intent only; keep accepting cleanup traffic until barrier/deadline."""
        self._recorder._request_close(self._session, cleanup_timeout)

    def mark_incomplete(self, reason: str) -> None:
        """Record known capture loss without inventing rejected raw admissions."""
        if reason not in REASONS:
            raise ValueError('invalid incomplete reason')
        with self._recorder._condition:
            if not self._session.stopped:
                self._session.reasons.add(reason)

    def cleanup_finished(self) -> None:
        """Caller asserts the actual transport cleanup barrier has completed."""
        self._recorder._seal(self._session, complete=True)

    def cleanup_timed_out(self) -> None:
        self._recorder._seal(self._session, complete=False)

    def status(self) -> CaptureStatus:
        return self._recorder._status(self._session)


class TraceRecorder:
    def __init__(self, root: Path, *, queue_events: int = 4096,
                 queue_bytes: int = 2 * 1024**2, max_sessions: int = 16,
                 checkpoint_seconds: float = 1.0, limits: TraceLimits = DEFAULT_LIMITS,
                 store_factory: Callable[[], TraceStore] | None = None) -> None:
        if min(queue_events, queue_bytes, max_sessions) <= 0 or not math.isfinite(checkpoint_seconds) or checkpoint_seconds <= 0:
            raise ValueError('invalid recorder bounds')
        self._condition = threading.Condition()
        self._events: deque[_Event] = deque()
        self._sessions: dict[str, _Session] = {}
        self._queue_limit = queue_events
        self._byte_limit = queue_bytes
        self._max_sessions = max_sessions
        self._bytes = 0
        self._inflight = 0
        self._checkpoint_seconds = checkpoint_seconds
        self._factory = store_factory or (lambda: TraceStore(root, limits=limits))
        self._shutdown = False
        self._fatal = False
        self._failure: str | None = None
        self._thread = threading.Thread(target=self._run, name='SantokerTraceWriter', daemon=True)
        self._thread.start()

    @property
    def failure(self) -> str | None:
        """Sticky recorder-level failure, including startup; no raw exceptions/secrets."""
        with self._condition:
            return self._failure

    def start(self, config: CaptureConfig) -> SessionHandle:
        with self._condition:
            session_id = str(uuid4())
            origin = time.monotonic_ns()
            session = _Session(session_id, origin, config.manifest(session_id, origin))
            handle = SessionHandle(session_id, self, session)
            if self._shutdown or self._fatal or len(self._sessions) >= self._max_sessions:
                session.state = 'failed'
                session.stopped = True
                session.failure = self._failure or 'queue_overflow'
                session.reasons.add(session.failure)
                self._failure = session.failure
            else:
                self._sessions[session_id] = session
                self._condition.notify()
            return handle

    def _emit(self, session: _Session, kind: str, fields: Mapping[str, Scalar],
              payload: bytes | bytearray | memoryview | None) -> bool:
        with self._condition:
            if session.deadline is not None and time.monotonic() >= session.deadline:
                self._seal(session, complete=False)
            if session.stopped:
                return False  # explicitly stopped callbacks do not consume sequence
            session.seq += 1
            session.mono_ns = max(session.mono_ns, time.monotonic_ns() - session.origin_ns)
            # Bound object traversal/copy, prohibit arbitrary nested metadata and payload aliases.
            expected = FIELDS.get(kind)
            valid = (kind not in {'summary', 'gap'} and expected is not None
                     and len(fields) <= 8 and set(fields) == expected - ({'payload'} if payload is not None else set()))
            charge = 1024
            if valid:
                for value in fields.values():
                    if type(value) not in {str, int, float, bool}:
                        valid = False
                        break
                    if isinstance(value, str):
                        if len(value) > 128:
                            valid = False
                            break
                        charge += len(value) * 6
                    elif not -2**63 < value < 2**63 or not math.isfinite(value):
                        valid = False
                        break
            if payload is not None:
                length = payload.nbytes if isinstance(payload, memoryview) else len(payload)
                valid = valid and length <= 65536
                charge += length
            if not valid:
                session.dropped += 1
                session.reasons.add('capture_error')
                session.failure = self._failure = 'capture_error'
                return False
            if len(self._events) + self._inflight >= self._queue_limit or self._bytes + charge > self._byte_limit:
                session.dropped += 1
                session.reasons.add('queue_overflow')
                session.failure = self._failure = 'queue_overflow'
                return False
            try:
                copied = None if payload is None else bytes(payload)
                event = _Event(session, session.seq, session.mono_ns, kind,
                               cast(Record, dict(fields)), copied, charge)
            except (ValueError, TypeError, BufferError):
                session.dropped += 1
                session.reasons.add('capture_error')
                session.failure = self._failure = 'capture_error'
                return False
            self._events.append(event)
            session.queued += 1
            session.queued_bytes += charge
            self._bytes += charge
            self._condition.notify()
            return True

    def _request_close(self, session: _Session, timeout: float) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError('invalid cleanup timeout')
        with self._condition:
            if not session.stopped and session.deadline is None:
                session.state = 'closing'
                session.deadline = time.monotonic() + timeout
                self._condition.notify()

    def _seal(self, session: _Session, *, complete: bool) -> None:
        with self._condition:
            if not session.stopped:
                session.stopped = True
                session.state = 'finalizing'
                if not complete or (session.deadline is not None and time.monotonic() >= session.deadline):
                    session.reasons.add('cleanup_timeout')
                self._condition.notify()

    def _status(self, session: _Session) -> CaptureStatus:
        with self._condition:
            if session.deadline is not None and time.monotonic() >= session.deadline:
                self._seal(session, complete=False)
            return CaptureStatus(session.session_id, session.state, session.seq, session.stored,
                                 session.dropped, session.queued, session.queued_bytes,
                                 tuple(sorted(session.reasons)), session.failure)

    def shutdown(self, *, wait: bool = False, timeout: float = 5.0) -> bool:
        """Nonblocking by default; wait/join belongs off the UI thread."""
        with self._condition:
            self._shutdown = True
            for session in self._sessions.values():
                self._seal(session, complete=False)
            self._condition.notify()
        if wait:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _failed(self, session: _Session, reason: str) -> None:
        with self._condition:
            session.failure = self._failure = reason
            session.reasons.add(reason)
            session.state = 'failed'
            session.stopped = True

    def _run(self) -> None:
        store: TraceStore | None = None
        try:
            store = self._factory()  # startup/recovery never on producer/UI
            if store.failure:
                with self._condition:
                    self._failure = store.failure
            checkpoint = time.monotonic()
            while True:
                with self._condition:
                    now = time.monotonic()
                    for session in self._sessions.values():
                        if not session.stopped and session.deadline is not None and now >= session.deadline:
                            self._seal(session, complete=False)
                    beginning = next((s for s in self._sessions.values() if not s.begun and s.state != 'failed'), None)
                    sealing = next((s for s in self._sessions.values() if s.stopped and s.queued == 0), None)
                    event = None
                    if beginning is None and sealing is None and self._events:
                        event = self._events.popleft()
                        self._inflight = 1
                    if self._shutdown and not self._sessions and not self._events:
                        break
                if beginning is not None:
                    try:
                        store.begin(beginning.manifest)
                        beginning.begun = True
                    except Exception:  # hardware boundary: never propagate into roast controls
                        self._failed(beginning, store.failure or 'io_error')
                        self._abandon(store, beginning)
                elif sealing is not None:
                    if sealing.state != 'failed':
                        try:
                            terminal = summary_record(sealing.session_id, sealing.seq + 1,
                                max(sealing.mono_ns, time.monotonic_ns() - sealing.origin_ns),
                                sealing.stored, sealing.dropped, sealing.seq - sealing.stored_seq,
                                sealing.reasons)
                            store.seal(sealing.session_id, terminal)
                            with self._condition:
                                sealing.state = 'sealed'
                        except Exception:
                            self._failed(sealing, store.failure or 'io_error')
                            self._abandon(store, sealing)
                    with self._condition:
                        del self._sessions[sealing.session_id]
                elif event is not None:
                    self._write_event(store, event)
                    with self._condition:
                        event.session.queued -= 1
                        event.session.queued_bytes -= event.charge
                        self._bytes -= event.charge
                        self._inflight = 0
                else:
                    with self._condition:
                        self._condition.wait(min(0.05, self._checkpoint_seconds))
                if time.monotonic() - checkpoint >= self._checkpoint_seconds:
                    try:
                        store.checkpoint()
                    except Exception:
                        with self._condition:
                            sessions = tuple(self._sessions.values())
                        for session in sessions:
                            self._failed(session, 'io_error')
                            self._abandon(store, session)
                    checkpoint = time.monotonic()
        except Exception:
            with self._condition:
                self._fatal = True
                self._failure = 'io_error'
                for session in self._sessions.values():
                    self._failed(session, 'io_error')
                    session.dropped += session.queued
                    session.queued = session.queued_bytes = 0
                self._events.clear()
                self._sessions.clear()
                self._bytes = self._inflight = 0
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    with self._condition:
                        self._failure = 'io_error'

    def _abandon(self, store: TraceStore, session: _Session) -> None:
        try:
            store.abandon(session.session_id, session.failure or 'io_error')
        except Exception:
            # Disk may be completely unavailable: sticky snapshot remains truthful.
            with self._condition:
                self._failure = 'io_error'

    def _write_event(self, store: TraceStore, event: _Event) -> None:
        session = event.session
        if session.state == 'failed' or 'event_limit' in session.reasons or 'storage_limit' in session.reasons:
            with self._condition:
                session.dropped += 1
            return
        record: Record = {'kind': event.kind, 'session_id': session.session_id,
                          'seq': event.seq, 'mono_ns': event.mono_ns,
                          'dropped_before': event.seq - session.stored_seq - 1, **event.fields}
        if event.payload is not None:
            record['payload'] = {'encoding': 'base64', 'byte_length': len(event.payload),
                                 'data': base64.b64encode(event.payload).decode('ascii')}
        try:
            reason = store.append(session.session_id, record)
            with self._condition:
                if reason is None:
                    session.stored += 1
                    session.stored_seq = event.seq
                else:
                    session.dropped += 1
                    session.reasons.add(reason)
                    session.failure = self._failure = reason
                    session.stopped = True
                    session.state = 'finalizing'
        except Exception:
            with self._condition:
                session.dropped += 1
            self._failed(session, 'io_error')
            self._abandon(store, session)
