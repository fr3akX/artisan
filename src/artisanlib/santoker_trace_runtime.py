#
# ABOUT
# Desktop ownership and explicit transfer of Santoker diagnostic traces.
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

"""No widgets, queued Qt jobs, filesystem or credential access on producer calls.

One recorder owns the shared store; one daemon worker serializes manual jobs.
Logical cancellation only revokes undisclosed jobs, never their held descriptors.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Protocol

from artisanlib.roastserver.api import RoastServerClient
from artisanlib.roastserver.contract import ServerIdentity
from artisanlib.roastserver.settings import ConnectorSettings, CredentialStore
from artisanlib.santoker_trace import CaptureConfig, SessionHandle, TraceRecorder
from artisanlib.santoker_trace_store import Destination, TraceStore, UploadTicket


class TraceDevice(Protocol):
    @property
    def trace_cleanup_complete(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Selection:
    session_id: str
    uploadable: bool


@dataclass(frozen=True, slots=True)
class _Job:
    sessions: tuple[str, ...]
    upload: bool
    generation: int
    destination: Destination | None


class TraceRuntime:
    PAGE_SIZE = 50

    def __init__(self, root: Path, credentials: CredentialStore, *,
                 client_factory: Callable[[str, str], RoastServerClient] = RoastServerClient,
                 store_factory: Callable[[], TraceStore] | None = None) -> None:
        self._credentials = credentials
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._settled = threading.Event()
        self._store: TraceStore | None = None
        self._closing = False
        self._origin: str | None = None
        self._destination: Destination | None = None
        self._generation = 0
        self._job: _Job | None = None
        self._busy = False
        self._refresh = False
        self._page = 0
        self._revision = 0
        self._selections: tuple[Selection, ...] = ()
        self._more = False
        self._notices: set[str] = set()
        self._reported: set[str] = set()
        self._handles: dict[str, SessionHandle] = {}
        self._retired: dict[str, TraceDevice] = {}
        self._active: SessionHandle | None = None
        self._ordinal = 0
        self._roasting = False

        def create_store() -> TraceStore:
            try:
                store = store_factory() if store_factory is not None else TraceStore(root)
                self._store = store
                return store
            finally:
                self._ready.set()

        self.recorder = TraceRecorder(root, store_factory=create_store)
        self._worker = threading.Thread(target=self._run, name='Santoker trace transfer', daemon=True)
        self._worker.start()

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    @property
    def settled(self) -> bool:
        return self._settled.is_set()

    @property
    def cleanup_complete(self) -> bool:
        with self._lock:
            return self._active is None and all(d.trace_cleanup_complete for d in self._retired.values())

    def settings_changed(self, settings: ConnectorSettings) -> None:
        # Settings identity is persisted data, NEVER current permission evidence.
        with self._lock:
            self._generation += 1
            self._notices.difference_update({'transfer', 'uploaded', 'deleted', 'identity'})
            self._destination = None
            self._origin = settings.origin if settings.enabled and settings.pending_connection is None else None

    def identity_changed(self, identity: ServerIdentity | None) -> None:
        with self._lock:
            self._generation += 1
            self._notices.difference_update({'transfer', 'uploaded', 'deleted', 'identity'})
            self._destination = (None if identity is None or self._origin is None else Destination(
                self._origin, str(identity.organization.id), str(identity.user.id)))

    def notice(self, code: str) -> None:
        # Only fixed categories enter the UI, never exceptions or server responses.
        if code not in {'capture', 'capacity', 'transfer', 'deleted', 'uploaded', 'identity', 'empty'}:
            return
        with self._lock:
            self._notices.add(code)

    def begin(self, config: CaptureConfig) -> SessionHandle | None:
        self._reap() # bound historical handles even across rapid ON/OFF producer calls
        with self._lock:
            if self._closing or self._active is not None:
                return None
            handle = self.recorder.start(config)
            self._active = handle
            self._handles[handle.session_id] = handle
            self._ordinal = 0
            self._roasting = False
        handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        return handle

    def milestone(self, name: str) -> None:
        with self._lock:
            handle = self._active
            if handle is None:
                return
            if name == 'roast_start':
                if self._roasting:
                    return
                self._ordinal += 1
                self._roasting = True
            elif not self._roasting:
                return
            if name == 'roast_end':
                self._roasting = False
            handle.emit('milestone', {'roast_ordinal': self._ordinal, 'name': name})

    def retire(self, device: TraceDevice | None) -> None:
        """After request_close -> safety writes -> stop; retain the EXACT old owner."""
        self.milestone('roast_end')
        with self._lock:
            handle = self._active
            if handle is None:
                return
            self._active = None
            if device is not None:
                self._retired[handle.session_id] = device
            else:
                handle.mark_incomplete('capture_error')
                handle.request_close()
                handle.cleanup_finished()  # construction failed: no transport owner
        self._wake.set()

    def refresh(self, page: int = 0) -> int:
        with self._lock:
            if not self._closing:
                self._page = max(0, page)
                self._refresh = True  # coalesced BEFORE worker admission
                self._wake.set()
            return self._revision

    def snapshot(self) -> tuple[int, tuple[Selection, ...], bool, bool, bool, tuple[str, ...]]:
        self._reap()
        with self._lock:
            notices = tuple(sorted(self._notices))
            self._notices.clear()
            return (self._revision, self._selections, self._more, self._busy,
                    self._destination is not None and not self._closing, notices)

    def submit(self, sessions: tuple[str, ...], *, upload: bool) -> bool:
        with self._lock:
            available = {s.session_id for s in self._selections if not upload or s.uploadable}
            if (self._closing or self._busy or not sessions or len(sessions) > self.PAGE_SIZE
                    or len(set(sessions)) != len(sessions) or not set(sessions) <= available
                    or any(s in self._handles for s in sessions)):
                self._notices.add('capacity')
                return False
            if upload and self._destination is None:
                self._notices.add('identity')
                return False
            self._job = _Job(sessions, upload, self._generation, self._destination)
            self._busy = True  # single outstanding job, including actual HTTP settlement
            self._wake.set()
            return True

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            self._generation += 1
            self._notices.difference_update({'transfer', 'uploaded', 'deleted', 'identity'})
            self._destination = None
            self._wake.set()

    def _reap(self) -> None:
        failure = self.recorder.failure
        with self._lock:
            for session_id, handle in tuple(self._handles.items()):
                status = handle.status()
                if status.failure or status.dropped_events or status.reasons:
                    failure = failure or status.failure or (status.reasons[0] if status.reasons else 'capture_error')
                if (handle is not self._active and status.state in {'sealed', 'failed'}
                        and (session_id not in self._retired or self._retired[session_id].trace_cleanup_complete)):
                    self._handles.pop(session_id)
                    self._retired.pop(session_id, None)
                    self._refresh = True
                    self._wake.set()
            if failure is not None and failure not in self._reported:
                self._reported.add(failure)
                self._notices.add('capacity' if failure in {'queue_overflow', 'storage_limit', 'event_limit'} else 'capture')

    def _permit(self, job: _Job) -> None:
        # Atomic commit point: changes AFTER this lock releases cannot retarget
        # the already committed original request/credential/ticket/receipt.
        with self._lock:
            if self._closing or job.generation != self._generation or job.destination != self._destination:
                raise RuntimeError('authorization_revoked')

    def _upload(self, store: TraceStore, job: _Job, session_id: str) -> None:
        self._permit(job)
        destination = job.destination
        assert destination is not None
        credential = self._credentials.get(destination.origin)
        if not credential:
            raise RuntimeError('credential_unavailable')
        client = self._client_factory(destination.origin, credential)
        ticket: UploadTicket | None = None
        receipt_started = False
        try:
            self._permit(job)
            metadata = store.read_session(session_id)
            if metadata['state'] != 'authorized' or metadata.get('destination') != destination.record():
                store.authorize(session_id, destination)
            ticket = store.prepare(session_id)
            identity = client.test_connection()
            store.begin_upload(ticket, Destination(destination.origin, str(identity.organization.id), str(identity.user.id)))
            with store.open_upload(ticket) as source:
                receipt = client.put_diagnostic_trace(ticket, source, before_disclosure=lambda: self._permit(job))
            # A later identity/ON change does not revoke a matching ORIGINAL receipt.
            receipt_started = True
            store.accept_receipt(ticket, receipt, request_origin=destination.origin)
        except Exception:
            if ticket is not None and not receipt_started:
                store.upload_failed(ticket)
            raise
        finally:
            client.close()

    def _list(self, store: TraceStore) -> None:
        with self._lock:
            page = self._page
            excluded = frozenset(self._handles)
        records = store.list_sessions()
        eligible = [Selection(str(r['session_id']), r['state'] in {'retained', 'authorized'})
                    for r in records if r['state'] in {'retained', 'authorized', 'damaged', 'deletion_pending', 'receipt_persisted'}
                    and r['session_id'] not in excluded]
        start = page * self.PAGE_SIZE
        candidates = tuple(eligible[start:start + self.PAGE_SIZE])
        with self._lock:
            self._selections = tuple(s for s in candidates if s.session_id not in self._handles)
            self._more = len(eligible) > start + self.PAGE_SIZE
            self._revision += 1

    def _run(self) -> None:
        self._ready.wait()
        store = self._store
        while True:
            self._wake.wait(0.1)
            self._wake.clear()
            self._reap()
            with self._lock:
                job, self._job = self._job, None
                refresh, self._refresh = self._refresh, False
                closing = self._closing
            if job is not None:
                try:
                    if store is None:
                        raise RuntimeError('store_unavailable')
                    for session_id in job.sessions:
                        with self._lock:
                            if self._closing or session_id in self._handles:
                                raise RuntimeError('session_busy')
                        if job.upload:
                            self._upload(store, job, session_id)
                        else:
                            store.delete_local(session_id)
                    with self._lock:
                        if job.generation == self._generation:
                            self._notices.add('uploaded' if job.upload else 'deleted')
                except Exception:  # worker boundary: retain; explicit retry only
                    with self._lock:
                        if job.generation == self._generation:
                            self._notices.add('transfer')
                finally:
                    with self._lock:
                        self._busy = False
                    refresh = True
            if refresh and store is not None and not closing:
                try:
                    self._list(store)
                except Exception:
                    self.notice('capture')
            if closing and self.cleanup_complete:
                # Worker is sequential: no descriptor/client can remain in use here.
                while not self.recorder.shutdown(wait=True, timeout=0.1):
                    pass
                self._settled.set()
                return
