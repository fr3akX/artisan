# Copyright (C) 2026 The Artisan team. AGPLv3+; see LICENSE.
"""Real recorder/store, deterministic blocked HTTP/keyring; no external services."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
import gzip
import json
from pathlib import Path
import threading
import time
from typing import Any, BinaryIO, cast
from uuid import uuid4

import pytest

from artisanlib.roastserver.api import RoastServerClient
from artisanlib.roastserver.contract import IdentityOrganization, IdentityUser, ServerIdentity
from artisanlib.roastserver.settings import ConnectorSettings
from artisanlib.santoker_trace import CaptureConfig, SessionHandle
from artisanlib.santoker_trace_runtime import TraceRuntime
from artisanlib.santoker_trace_store import StoreError, TraceStore, UploadTicket

CONFIG = CaptureConfig('4.0.0', 'linux', '6.1', 'x86_64')
IDENTITY = ServerIdentity(IdentityUser(uuid4(), 'not-persisted', 'not-persisted'),
                          IdentityOrganization(uuid4(), 'not-persisted', 'not-persisted'), 'member')
SETTINGS = ConnectorSettings('https://example.invalid', True, False, uuid4(), IDENTITY,
                             512 * 1024**2, None, None, None)


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert predicate()


class Device:
    trace_cleanup_complete = False


class Credentials:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.gate = threading.Event()
        self.gate.set()

    def get(self, _origin: str) -> str:
        self.calls.append(threading.get_ident())
        assert self.gate.wait(5)
        return 'fake-credential'

    def set(self, _origin: str, _credential: str) -> None:
        raise AssertionError('never write credentials')

    def delete(self, _origin: str) -> None:
        raise AssertionError('never delete credentials')


class Client:
    def __init__(self) -> None:
        self.before = threading.Event()
        self.before.set()
        self.after = threading.Event()
        self.after.set()
        self.entered = threading.Event()
        self.disclosed = threading.Event()
        self.closed = False
        self.fail = False
        self.source: BinaryIO | None = None
        self.tickets: list[UploadTicket] = []
        self.threads: list[int] = []
        self.identity = IDENTITY

    def test_connection(self) -> ServerIdentity:
        self.threads.append(threading.get_ident())
        return self.identity

    def put_diagnostic_trace(self, ticket: UploadTicket, source: BinaryIO, *,
                             before_disclosure: Callable[[], None]) -> dict[str, Any]:
        self.source = source
        self.entered.set()
        assert self.before.wait(5)
        before_disclosure()
        self.tickets.append(ticket)
        self.disclosed.set()
        assert self.after.wait(5)
        assert not source.closed
        if self.fail:
            raise RuntimeError('secret exception must never reach presentation')
        return {'id': str(uuid4()), 'session_id': ticket.session_id,
                'organization_id': ticket.destination.organization_id,
                'uploader_user_id': ticket.destination.uploader_user_id,
                'sha256': ticket.sha256, 'byte_size': ticket.byte_size,
                'stored_at': '2026-08-13T12:00:00.000000Z', 'storage_status': 'stored'}

    def close(self) -> None:
        self.closed = True


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.credentials = Credentials()
        self.client = Client()
        self.store: TraceStore | None = None
        self.store_thread: int | None = None
        self.handles: list[SessionHandle] = []
        self.devices: list[Device] = []

        def store_factory() -> TraceStore:
            self.store_thread = threading.get_ident()
            self.store = TraceStore(root)
            return self.store

        self.runtime = TraceRuntime(root, self.credentials, store_factory=store_factory,
            client_factory=lambda _origin, _credential: cast(RoastServerClient, self.client))

    def begin(self) -> SessionHandle:
        handle = self.runtime.begin(CONFIG)
        assert handle is not None
        self.handles.append(handle)
        return handle

    def end(self, handle: SessionHandle, *, complete: bool = True) -> Device:
        device = Device()
        self.devices.append(device)
        handle.request_close()
        self.runtime.retire(device)
        if complete:
            device.trace_cleanup_complete = True
            handle.cleanup_finished()
        return device

    def retained(self) -> SessionHandle:
        handle = self.begin()
        self.end(handle)
        wait_for(lambda: handle.status().state == 'sealed')
        revision = self.runtime.refresh()
        wait_for(lambda: self.runtime.snapshot()[0] > revision)
        assert handle.session_id in {s.session_id for s in self.runtime.snapshot()[1]}
        return handle

    def authorize(self) -> None:
        self.runtime.settings_changed(SETTINGS)
        self.runtime.identity_changed(IDENTITY)

    def finish(self) -> None:
        self.credentials.gate.set()
        self.client.before.set()
        self.client.after.set()
        for device in self.devices:
            device.trace_cleanup_complete = True
        self.runtime.retire(None)
        for handle in self.handles:
            handle.request_close()
            handle.cleanup_finished()
        self.runtime.shutdown()
        wait_for(lambda: self.runtime.settled)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    instance = Rig(tmp_path / 'traces')
    try:
        yield instance
    finally:
        instance.finish()


def test_local_capture_never_requires_server_or_auto_upload(rig: Rig) -> None:
    handle = rig.retained()
    assert rig.credentials.calls == []
    assert not rig.client.entered.is_set()
    assert rig.store_thread != threading.get_ident()
    assert not rig.runtime.submit((handle.session_id,), upload=True)
    rig.runtime.settings_changed(SETTINGS)  # persisted settings.identity cannot grant permission
    assert not rig.runtime.submit((handle.session_id,), upload=True)
    rig.runtime.identity_changed(IDENTITY)
    assert rig.runtime.submit((handle.session_id,), upload=True)
    wait_for(lambda: rig.client.closed)
    assert rig.store is not None
    assert rig.store.read_session(handle.session_id)['state'] == 'removed'
    assert all(t != threading.get_ident() for t in rig.credentials.calls + rig.client.threads)


def test_new_on_while_upload_blocked_preserves_original_stream_and_store(rig: Rig) -> None:
    old = rig.retained()
    rig.authorize()
    rig.client.after.clear()
    assert rig.runtime.submit((old.session_id,), upload=True)
    assert rig.client.disclosed.wait(5)
    new = rig.begin()
    assert new.session_id != old.session_id
    assert new.emit('status', {'severity': 'info', 'code': 'protocol_ready'})
    assert not rig.runtime.submit((old.session_id,), upload=True)  # bounded before queueing
    rig.end(new)
    rig.runtime.shutdown()
    time.sleep(0.03)
    assert not rig.runtime.settled
    assert rig.store is not None
    with pytest.raises(StoreError, match='session_busy'):
        rig.store.close()
    assert rig.client.source is not None and not rig.client.source.closed
    rig.client.after.set()
    wait_for(lambda: rig.runtime.settled)
    assert rig.client.source.closed
    assert json.loads((rig.root / f'{old.session_id}.json').read_text())['state'] == 'removed'
    assert (rig.root / f'{new.session_id}.gz').exists()


@pytest.mark.parametrize('stage', ['credential', 'before_body'])
def test_stale_generation_cannot_disclose_and_requires_explicit_retry(rig: Rig, stage: str) -> None:
    handle = rig.retained()
    rig.authorize()
    if stage == 'credential':
        rig.credentials.gate.clear()
    else:
        rig.client.before.clear()
    assert rig.runtime.submit((handle.session_id,), upload=True)
    if stage == 'credential':
        wait_for(lambda: bool(rig.credentials.calls))
    else:
        assert rig.client.entered.wait(5)
    rig.runtime.identity_changed(None)
    rig.credentials.gate.set()
    rig.client.before.set()
    wait_for(lambda: not rig.runtime.snapshot()[3])
    assert not rig.client.disclosed.is_set()
    assert (rig.root / f'{handle.session_id}.gz').exists()
    rig.runtime.identity_changed(IDENTITY)
    time.sleep(0.02)
    assert not rig.client.disclosed.is_set()
    rig.client.closed = False
    assert rig.runtime.submit((handle.session_id,), upload=True)
    wait_for(lambda: rig.client.closed)
    assert rig.client.disclosed.is_set()


def test_late_receipt_updates_only_original_after_account_and_session_change(rig: Rig) -> None:
    old = rig.retained()
    rig.authorize()
    rig.client.after.clear()
    assert rig.runtime.submit((old.session_id,), upload=True)
    assert rig.client.disclosed.wait(5)
    rig.runtime.settings_changed(replace(SETTINGS, origin='https://replacement.invalid'))
    rig.runtime.identity_changed(replace(IDENTITY, user=replace(IDENTITY.user, id=uuid4())))
    current = rig.begin()
    rig.client.after.set()
    wait_for(lambda: rig.client.closed)
    assert rig.store is not None
    assert rig.store.read_session(old.session_id)['state'] == 'removed'
    assert current.status().state == 'active'
    assert 'uploaded' not in rig.runtime.snapshot()[5]
    assert rig.client.tickets[0].session_id == old.session_id


def test_wrong_fresh_identity_retains_and_does_not_send_body(rig: Rig) -> None:
    handle = rig.retained()
    rig.authorize()
    rig.client.identity = replace(IDENTITY, user=replace(IDENTITY.user, id=uuid4()))
    assert rig.runtime.submit((handle.session_id,), upload=True)
    wait_for(lambda: rig.client.closed)
    assert not rig.client.disclosed.is_set()
    assert (rig.root / f'{handle.session_id}.gz').exists()


def test_failure_never_retries_and_delete_is_explicit(rig: Rig) -> None:
    handle = rig.retained()
    rig.authorize()
    rig.client.fail = True
    assert rig.runtime.submit((handle.session_id,), upload=True)
    wait_for(lambda: not rig.runtime.snapshot()[3])
    assert len(rig.client.tickets) == 1
    time.sleep(0.03)
    assert len(rig.client.tickets) == 1
    assert (rig.root / f'{handle.session_id}.gz').exists()
    wait_for(lambda: bool(rig.runtime.snapshot()[1]))
    assert rig.runtime.submit((handle.session_id,), upload=False)
    wait_for(lambda: not (rig.root / f'{handle.session_id}.gz').exists())
    assert len(rig.client.tickets) == 1


def test_cleanup_timeout_is_not_owner_barrier_or_upload_permission(rig: Rig) -> None:
    handle = rig.begin()
    device = rig.end(handle, complete=False)
    handle.cleanup_timed_out()
    wait_for(lambda: handle.status().state == 'sealed')
    revision = rig.runtime.refresh()
    wait_for(lambda: rig.runtime.snapshot()[0] > revision)
    assert not rig.runtime.snapshot()[1]
    assert not rig.runtime.submit((handle.session_id,), upload=False)
    rig.runtime.shutdown()
    time.sleep(0.03)
    assert not rig.runtime.cleanup_complete
    assert not rig.runtime.settled
    device.trace_cleanup_complete = True
    wait_for(lambda: rig.runtime.settled)


def test_multiple_roasts_and_idempotent_off_are_one_session(rig: Rig) -> None:
    handle = rig.begin()
    for name in ('charge', 'roast_end', 'roast_start', 'roast_start', 'charge', 'dry_end',
                 'fc_start', 'fc_end', 'sc_start', 'sc_end', 'drop', 'cool_end', 'roast_end',
                 'roast_end', 'roast_start', 'charge'):
        rig.runtime.milestone(name)
    rig.end(handle)
    rig.runtime.retire(None)
    wait_for(lambda: handle.status().state == 'sealed')
    events = [json.loads(line) for line in gzip.decompress((rig.root / f'{handle.session_id}.gz').read_bytes()).splitlines()]
    markers = [(e['roast_ordinal'], e['name']) for e in events if e['kind'] == 'milestone']
    assert markers == [(1, name) for name in ('roast_start', 'charge', 'dry_end', 'fc_start', 'fc_end',
        'sc_start', 'sc_end', 'drop', 'cool_end', 'roast_end')] + [(2, 'roast_start'), (2, 'charge'), (2, 'roast_end')]
    assert events[1]['code'] == 'session_on'
    assert events[-1]['completion'] == 'complete'


def test_disk_start_failure_is_fixed_notice_and_does_not_raise_on_producer(tmp_path: Path) -> None:
    def failing_store() -> TraceStore:
        raise OSError('secret disk path')

    runtime = TraceRuntime(tmp_path / 'traces', Credentials(), store_factory=failing_store)
    handle = runtime.begin(CONFIG)
    assert handle is not None
    wait_for(lambda: runtime.recorder.failure is not None)
    assert 'capture' in runtime.snapshot()[5]
    assert runtime.snapshot()[5] == ()  # sticky capture failure is deduplicated
    runtime.retire(None)
    runtime.shutdown()
    wait_for(lambda: runtime.settled)


def test_capacity_failure_is_visible_without_old_trace_eviction(rig: Rig) -> None:
    retained = rig.retained()
    handle = rig.begin()
    handle.mark_incomplete('storage_limit')
    assert 'capacity' in rig.runtime.snapshot()[5]
    assert (rig.root / f'{retained.session_id}.gz').exists()


def test_account_change_discards_already_queued_original_completion_notice(rig: Rig) -> None:
    old = rig.retained()
    rig.authorize()
    assert rig.runtime.submit((old.session_id,), upload=True)
    wait_for(lambda: not rig.runtime._busy)
    rig.runtime.identity_changed(None)
    assert 'uploaded' not in rig.runtime.snapshot()[5]
    assert rig.store is not None and rig.store.read_session(old.session_id)['state'] == 'removed'
