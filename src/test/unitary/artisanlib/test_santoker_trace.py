# Copyright (C) 2026 The Artisan team. AGPLv3+; see LICENSE.
"""Hardware/network-free local recorder and durable job fault tests."""
from __future__ import annotations

import base64
from collections.abc import Buffer
from dataclasses import replace
import gzip
import io
import json
import os
from pathlib import Path
import threading
import time
from typing import cast, override
from uuid import uuid4

import pytest

from artisanlib.roastserver import _filesystem as fs
from artisanlib.santoker_trace import CaptureConfig, SessionHandle, TraceRecorder
from artisanlib.santoker_trace_contract import RX_UUID, Json, Record, TraceLimits, validate_trace
from artisanlib.santoker_trace_store import (
    Destination, StoreError, TraceStore, UploadTicket, line_bytes, summary_record, utc_now,
)

CONFIG = CaptureConfig('4.0.0', 'linux', '6.1', 'x86_64')
LIMITS = TraceLimits(compressed_bytes=32768, expanded_bytes=16384, record_bytes=2048, events=20)


def make_store(root: Path, *, limits: TraceLimits = LIMITS, disk_bytes: int = 262144) -> TraceStore:
    return TraceStore(root, limits=limits, preparation_bytes=limits.compressed_bytes,
                      failure_bytes=16384, disk_bytes=disk_bytes)


def wait_sealed(handle: SessionHandle) -> None:
    deadline = time.monotonic() + 5
    while handle.status().state not in {'sealed', 'failed'} and time.monotonic() < deadline:
        time.sleep(0.005)
    assert handle.status().state == 'sealed', handle.status()


def records(root: Path, session_id: str) -> list[Record]:
    data = (root / f'{session_id}.gz').read_bytes()
    validate_trace(io.BytesIO(data), expected_session_id=session_id, limits=LIMITS)
    return [json.loads(line) for line in gzip.decompress(data).splitlines()]


def new_journal(store: TraceStore) -> str:
    session_id = str(uuid4())
    store.begin(CONFIG.manifest(session_id, time.monotonic_ns()))
    return session_id


def seal_empty(store: TraceStore) -> str:
    session_id = new_journal(store)
    store.seal(session_id, summary_record(session_id, 1, 1, 0, 0, 0, set()))
    return session_id


def status_event(session_id: str, seq: int = 1, dropped: int = 0) -> Record:
    return {'kind': 'status', 'session_id': session_id, 'seq': seq, 'mono_ns': seq,
            'dropped_before': dropped, 'severity': 'info', 'code': 'session_on'}


def upload_ticket(store: TraceStore, session_id: str) -> UploadTicket:
    destination = Destination('https://EXAMPLE.com:443/', str(uuid4()), str(uuid4()))
    store.authorize(session_id, destination)
    ticket = store.prepare(session_id)
    store.begin_upload(ticket, destination)
    return ticket


def receipt(ticket: UploadTicket) -> Record:
    return {'id': str(uuid4()), 'session_id': ticket.session_id,
            'organization_id': ticket.destination.organization_id,
            'uploader_user_id': ticket.destination.uploader_user_id,
            'sha256': ticket.sha256, 'byte_size': ticket.byte_size,
            'stored_at': utc_now(), 'storage_status': 'stored'}


def test_raw_copy_order_close_and_immutable_old_sink(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path, store_factory=lambda: make_store(tmp_path))
    try:
        old = recorder.start(CONFIG)
        data = bytearray(b'\x00\xffnoisy\r\n\x80')
        original = bytes(data)
        assert old.emit('rx', {'connection_id': str(uuid4()), 'direction': 'rx',
                               'characteristic': RX_UUID}, payload=data)
        data[:] = b'changed'
        old.request_close()
        assert old.status().state == 'closing'
        assert old.emit('status', {'severity': 'info', 'code': 'cleanup_finished'})
        old.cleanup_finished()
        assert not old.emit('status', {'severity': 'info', 'code': 'session_on'})
        fresh = recorder.start(CONFIG)
        assert fresh.session_id != old.session_id
        fresh.cleanup_timed_out()
        wait_sealed(old)
        wait_sealed(fresh)
        trace = records(tmp_path, old.session_id)
        assert base64.b64decode(cast(dict[str, str], trace[1]['payload'])['data']) == original
        assert trace[-1]['completion'] == 'complete'
        assert trace[-1]['last_seq'] == 3
        assert records(tmp_path, fresh.session_id)[-1]['reasons'] == ['cleanup_timeout']
    finally:
        assert recorder.shutdown(wait=True)


@pytest.mark.parametrize(('events', 'byte_limit'), [(2, 100000), (10, 1300)])
def test_stalled_writer_admission_exact_gaps_and_budget(tmp_path: Path, events: int, byte_limit: int) -> None:
    entered, release = threading.Event(), threading.Event()

    def factory() -> TraceStore:
        entered.set()
        assert release.wait(5)
        return make_store(tmp_path)

    recorder = TraceRecorder(tmp_path, queue_events=events, queue_bytes=byte_limit, store_factory=factory)
    try:
        assert entered.wait(2)
        handle = recorder.start(CONFIG)
        accepted = sum(handle.emit('status', {'severity': 'info', 'code': 'session_on'}) for _ in range(30))
        snapshot = handle.status()
        assert snapshot.queued_events == accepted <= events
        assert snapshot.queued_bytes <= byte_limit
        assert snapshot.dropped_events == 30 - accepted
        handle.cleanup_finished()
        release.set()
        wait_sealed(handle)
        terminal = records(tmp_path, handle.session_id)[-1]
        assert terminal['accepted_events'] == accepted
        assert terminal['dropped_events'] == 30 - accepted
        assert terminal['dropped_before'] == 30 - accepted
        assert terminal['last_seq'] == 31
        assert terminal['reasons'] == ['queue_overflow']
    finally:
        release.set()
        recorder.shutdown(wait=True)


def test_gap_between_accepted_records(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path, store_factory=lambda: make_store(tmp_path))
    try:
        handle = recorder.start(CONFIG)
        assert handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        assert not handle.emit('status', {'secret': 'not an allowed field'})
        assert handle.emit('status', {'severity': 'info', 'code': 'off_requested'})
        handle.cleanup_finished()
        wait_sealed(handle)
        trace = records(tmp_path, handle.session_id)
        assert trace[2]['seq'] == 3 and trace[2]['dropped_before'] == 1
        assert trace[-1]['completion'] == 'incomplete'
    finally:
        recorder.shutdown(wait=True)


def test_rapid_sessions_bounded_and_no_io_on_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    caller = threading.get_ident()
    original = fs.create_generated_file

    def checked_create(root: Path, path: Path, mode: int = 0o600) -> int:
        assert threading.get_ident() != caller
        return original(root, path, mode)

    monkeypatch.setattr(fs, 'create_generated_file', checked_create)

    def factory() -> TraceStore:
        assert release.wait(5)
        return make_store(tmp_path)

    recorder = TraceRecorder(tmp_path, max_sessions=3, store_factory=factory)
    try:
        handles = [recorder.start(CONFIG) for _ in range(100)]
        assert len({h.session_id for h in handles}) == 100
        assert sum(h.status().state == 'failed' for h in handles) == 97
        assert len(recorder._sessions) == 3
        for handle in handles:
            handle.cleanup_finished()
        release.set()
        for handle in handles[:3]:
            wait_sealed(handle)
        assert recorder.failure == 'queue_overflow'
    finally:
        release.set()
        recorder.shutdown(wait=True)


def test_cleanup_deadline(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path, store_factory=lambda: make_store(tmp_path))
    try:
        handle = recorder.start(CONFIG)
        handle.request_close(cleanup_timeout=0.01)
        wait_sealed(handle)
        before = handle.status().attempted_events
        assert not handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        handle.cleanup_finished()
        assert handle.status().attempted_events == before
        assert records(tmp_path, handle.session_id)[-1]['reasons'] == ['cleanup_timeout']
    finally:
        recorder.shutdown(wait=True)


@pytest.mark.parametrize('cap', ['events', 'bytes', 'disk', 'record'])
def test_caps_seal_prefix_and_reserved_summary(tmp_path: Path, cap: str) -> None:
    limits = LIMITS
    disk = 262144
    if cap == 'events':
        limits = TraceLimits(compressed_bytes=32768, expanded_bytes=16384, record_bytes=2048, events=2)
    if cap == 'bytes':
        limits = TraceLimits(compressed_bytes=32768, expanded_bytes=3000, record_bytes=2048, events=20)
    if cap == 'disk':
        disk = 32768 + 16384 + 2048 + 2400
    recorder = TraceRecorder(tmp_path, store_factory=lambda: make_store(tmp_path, limits=limits, disk_bytes=disk))
    try:
        handle = recorder.start(CONFIG)
        for _ in range(19):
            handle.emit('rx', {'connection_id': str(uuid4()), 'direction': 'rx', 'characteristic': RX_UUID},
                        payload=b'x' * (2000 if cap == 'record' else 100))
        handle.cleanup_finished()
        wait_sealed(handle)
        with (tmp_path / f'{handle.session_id}.gz').open('rb') as stream:
            trace = validate_trace(stream, limits=limits)
        assert trace.summary['completion'] == 'incomplete'
        assert trace.expanded_size <= limits.expanded_bytes
        assert cast(int, trace.summary['accepted_events']) < limits.events
        assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= disk
        assert trace.summary['reasons'] == [{'events': 'event_limit', 'bytes': 'storage_limit',
                                            'disk': 'storage_limit', 'record': 'capture_error'}[cap]]
    finally:
        recorder.shutdown(wait=True)


@pytest.mark.parametrize('tail', [b'', b'{"kind":', b'not json\n', b'x' * 3000])
def test_recovery_prefix_partial_and_damaged_tail_preserves_evidence(tmp_path: Path, tail: bytes) -> None:
    store = make_store(tmp_path)
    session_id = new_journal(store)
    assert store.append(session_id, status_event(session_id, 3, 2)) is None
    store.checkpoint()
    store.close()  # process dies before terminal summary
    journal = tmp_path / f'{session_id}.journal'
    with journal.open('ab') as stream:
        stream.write(tail)
    original = journal.read_bytes()
    recovered = make_store(tmp_path)
    try:
        trace = records(tmp_path, session_id)
        assert trace[-1]['completion'] == 'incomplete'
        assert 'crash_recovery' in cast(list[str], trace[-1]['reasons'])
        assert trace[-1]['dropped_events'] == 2
        assert trace[-1]['accepted_events'] == 1
        assert journal.read_bytes() == original
        assert recovered.read_session(session_id)['state'] == 'retained'
    finally:
        recovered.close()


def test_damaged_manifest_retained_not_uploaded(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session_id = new_journal(store)
    store.close()
    journal = tmp_path / f'{session_id}.journal'
    journal.write_bytes(b'bad\n')
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'damaged'
        assert journal.read_bytes() == b'bad\n'
        assert not (tmp_path / f'{session_id}.gz').exists()
        assert recovered.failure == 'io_error'
    finally:
        recovered.close()


def test_short_write_never_published_and_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    session_id = new_journal(store)
    original = TraceStore._write

    def partial(stream: io.BufferedIOBase, data: bytes) -> None:
        stream.write(data[:3])
        raise StoreError('io_error')

    monkeypatch.setattr(TraceStore, '_write', staticmethod(partial))
    with pytest.raises(StoreError):
        store.append(session_id, status_event(session_id))
    store.close()
    monkeypatch.setattr(TraceStore, '_write', staticmethod(original))
    recovered = make_store(tmp_path)
    try:
        assert records(tmp_path, session_id)[-1]['completion'] == 'incomplete'
    finally:
        recovered.close()


def test_write_checks_short_return() -> None:
    class Short(io.BytesIO):
        @override
        def write(self, data: Buffer, /) -> int:
            return super().write(bytes(data)[:1])

    with pytest.raises(StoreError, match='io_error'):
        TraceStore._write(Short(), b'long')


def test_fsync_failure_sticky_recorder_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = threading.Event()

    def factory() -> TraceStore:
        store = make_store(tmp_path)
        ready.set()
        return store

    recorder = TraceRecorder(tmp_path, store_factory=factory)
    try:
        assert ready.wait(2)

        def fail_sync(descriptor: int, **kwargs: object) -> None:
            del descriptor, kwargs
            raise OSError('do not persist raw exception')

        monkeypatch.setattr(fs, 'fsync_descriptor', fail_sync)
        handle = recorder.start(CONFIG)
        handle.cleanup_finished()
        deadline = time.monotonic() + 3
        while handle.status().state != 'failed' and time.monotonic() < deadline:
            time.sleep(0.005)
        assert handle.status().state == 'failed'
        assert handle.status().failure == recorder.failure == 'io_error'
        assert not list(tmp_path.glob('*.gz'))
    finally:
        recorder.shutdown(wait=True)


def test_path_guards_private_modes_and_process_lock(tmp_path: Path) -> None:
    root = tmp_path / 'traces'
    store = make_store(root)
    try:
        session_id = seal_empty(store)
        if os.name != 'nt':
            assert root.stat().st_mode & 0o777 == 0o700
            assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
        with pytest.raises(StoreError, match='store_busy'):
            make_store(root)
        with pytest.raises(ValueError):
            store.delete_local('../outside')
        assert store.read_session(session_id)['state'] == 'retained'
    finally:
        store.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX link injection; native DACL/reparse tests belong on Windows')
@pytest.mark.parametrize('location', ['root', 'parent', 'artifact', 'hardlink'])
def test_symlinks_and_hardlinks_rejected_without_external_mutation(tmp_path: Path, location: str) -> None:
    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = outside / 'marker'
    marker.write_text('unchanged')
    before = marker.stat().st_mode
    root = tmp_path / 'traces'
    if location == 'root':
        root.symlink_to(outside)
    elif location == 'parent':
        root.symlink_to(outside)
        root /= 'nested'
    else:
        root.mkdir(mode=0o700)
        target = root / f'{uuid4()}.gz'
        if location == 'artifact':
            target.symlink_to(marker)
        else:
            os.link(marker, target)
    with pytest.raises((StoreError, fs.FilesystemError)):
        make_store(root)
    assert marker.read_text() == 'unchanged'
    assert marker.stat().st_mode == before
    assert not (outside / 'nested').exists()


def test_crash_after_quarantine_rename_recovers_selected_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    unrelated = tmp_path / '.artisan-quarantine-unrelated'
    unrelated.write_bytes(b'unrelated evidence')
    unrelated.chmod(0o600)
    moved: list[Path] = []

    def crash_after_move(root: Path, path: Path, identity: tuple[int, int],
                         **kwargs: object) -> bool:
        del kwargs
        quarantine = root / f'.artisan-quarantine-{identity[0]:x}-{identity[1]:x}-{uuid4().hex}'
        os.replace(path, quarantine)
        moved.append(quarantine)
        raise SystemExit('simulated process death after rename')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(fs, 'unlink_generated_file', crash_after_move)
            with pytest.raises(SystemExit):
                store.delete_local(session_id)
    finally:
        store.close()
    assert moved and moved[0].exists()
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'removed'
        assert not moved[0].exists(), 'removed must not leave the selected trace in quarantine'
        assert unrelated.read_bytes() == b'unrelated evidence'
    finally:
        recovered.close()


@pytest.mark.parametrize('callback', ['failure', 'receipt'])
def test_stale_attempt_callback_cannot_change_new_upload(
        tmp_path: Path, callback: str) -> None:
    store = make_store(tmp_path)
    try:
        session_id = seal_empty(store)
        old = upload_ticket(store, session_id)
        store.upload_failed(old)
        current = store.prepare(session_id)
        store.begin_upload(current, current.destination)
        with pytest.raises(StoreError, match='stale_job'):
            if callback == 'failure':
                store.upload_failed(old)
            else:
                store.accept_receipt(old, receipt(old), request_origin=old.destination.origin)
        assert store.read_session(session_id)['state'] == 'uploading'
        assert current.artifact.exists()
        with pytest.raises(StoreError):
            store.delete_local(session_id)
    finally:
        store.close()


@pytest.mark.parametrize('trailing_damage', [False, True])
def test_recovery_preserves_durable_terminal_tail_loss(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trailing_damage: bool) -> None:
    store = make_store(tmp_path)
    session_id = new_journal(store)
    assert store.append(session_id, status_event(session_id)) is None

    def crash_before_artifact(*_args: object, **_kwargs: object) -> None:
        raise SystemExit('simulated death after durable terminal summary')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, '_artifact', crash_before_artifact)
            with pytest.raises(SystemExit):
                store.seal(session_id, summary_record(
                    session_id, 5, 10, 1, 3, 3, {'queue_overflow'}))
    finally:
        store.close()
    if trailing_damage:
        with (tmp_path / f'{session_id}.journal').open('ab') as stream:
            stream.write(b'garbage after durable summary\n')
    recovered = make_store(tmp_path)
    try:
        terminal = records(tmp_path, session_id)[-1]
        assert terminal['seq'] == 5
        assert terminal['mono_ns'] == 10
        assert terminal['dropped_before'] == 3
        assert terminal['dropped_events'] == 3
        assert terminal['accepted_events'] == 1
        expected_reasons = {'queue_overflow', 'crash_recovery'}
        if trailing_damage:
            expected_reasons.add('capture_error')
        assert set(cast(list[str], terminal['reasons'])) == expected_reasons
        assert terminal['completion'] == 'incomplete'
    finally:
        recovered.close()


@pytest.mark.parametrize('tail', [b'[]\n', b'null\n', b'1\n'])
def test_nonobject_tail_does_not_disable_other_sessions(tmp_path: Path, tail: bytes) -> None:
    store = make_store(tmp_path)
    damaged_id = new_journal(store)
    assert store.append(damaged_id, status_event(damaged_id)) is None
    healthy_id = seal_empty(store)
    store.close()
    with (tmp_path / f'{damaged_id}.journal').open('ab') as stream:
        stream.write(tail)
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(healthy_id)['state'] == 'retained'
        terminal = records(tmp_path, damaged_id)[-1]
        assert terminal['accepted_events'] == 1
        assert 'capture_error' in cast(list[str], terminal['reasons'])
        assert recovered.read_session(seal_empty(recovered))['state'] == 'retained'
    finally:
        recovered.close()


def test_receipt_state_without_receipt_is_delete_only_damage(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    metadata = store.read_session(session_id)
    metadata.update(state='receipt_persisted', receipt=None)
    store._save(session_id, metadata)  # Structurally valid but semantically damaged ledger.
    store.close()
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'damaged'
        assert ticket.artifact.exists()
        assert (tmp_path / f'{session_id}.meta-damaged').exists()
        with pytest.raises(StoreError):
            recovered.prepare(session_id)
    finally:
        recovered.close()


def test_retry_identical_bytes_and_no_restart_upload(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    original = ticket.artifact.read_bytes()
    store.upload_failed(ticket)
    retry = store.prepare(session_id)
    assert retry.attempt_id != ticket.attempt_id
    assert replace(retry, attempt_id=ticket.attempt_id) == ticket
    assert retry.artifact.read_bytes() == original
    store.begin_upload(retry, retry.destination)
    store.close()
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'authorized'
        assert ticket.artifact.read_bytes() == original
        next_attempt = recovered.prepare(session_id)
        assert next_attempt.attempt_id not in {ticket.attempt_id, retry.attempt_id}
        assert replace(next_attempt, attempt_id=ticket.attempt_id) == ticket
        assert not any('credential' in json.dumps(row) for row in recovered.list_sessions())
    finally:
        recovered.close()


@pytest.mark.parametrize(('field', 'wrong'), [
    ('session_id', str(uuid4())), ('organization_id', str(uuid4())),
    ('uploader_user_id', str(uuid4())), ('sha256', '0' * 64), ('byte_size', True),
    ('byte_size', 1), ('storage_status', 'pending'), ('id', 'invalid'),
    ('stored_at', '2026-02-30T00:00:00.000000Z'), ('extra', 'credential'),
])
def test_wrong_receipt_never_deletes(tmp_path: Path, field: str, wrong: object) -> None:
    store = make_store(tmp_path)
    try:
        session_id = seal_empty(store)
        ticket = upload_ticket(store, session_id)
        invalid = receipt(ticket)
        invalid[field] = cast(str, wrong)
        with pytest.raises((StoreError, ValueError)):
            store.accept_receipt(ticket, invalid, request_origin=ticket.destination.origin)
        assert ticket.artifact.exists()
        assert store.read_session(session_id)['state'] == 'uploading'
    finally:
        store.close()


def test_account_origin_and_stale_authorization_guard(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        session_id = seal_empty(store)
        ticket = upload_ticket(store, session_id)
        with pytest.raises(StoreError):
            store.delete_local(session_id)
        with pytest.raises(StoreError):
            store.authorize(session_id, ticket.destination)
        with pytest.raises(StoreError):
            store.accept_receipt(ticket, receipt(ticket), request_origin='https://other.example')
        store.upload_failed(ticket)
        different = Destination(ticket.destination.origin, ticket.destination.organization_id, str(uuid4()))
        retry = store.prepare(session_id)
        with pytest.raises(StoreError, match='identity_mismatch'):
            store.begin_upload(retry, different)
        store.upload_failed(retry)
        store.authorize(session_id, different)  # explicit new user action
        new = store.prepare(session_id)
        assert new.authorization_id != ticket.authorization_id
        with pytest.raises(StoreError):
            store.begin_upload(ticket, ticket.destination)
        store.begin_upload(new, different)
    finally:
        store.close()


def test_receipt_is_durable_before_unlink_and_failure_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    valid = receipt(ticket)
    calls = 0
    original = fs.unlink_generated_file

    def fail_unlink(root: Path, path: Path, identity: tuple[int, int], **kwargs: object) -> bool:
        nonlocal calls
        del root, path, identity, kwargs
        calls += 1
        disk = json.loads((tmp_path / f'{session_id}.json').read_bytes())
        assert disk['receipt'] == valid
        assert disk['state'] == 'deletion_pending'
        raise OSError('blocked')

    monkeypatch.setattr(fs, 'unlink_generated_file', fail_unlink)
    with pytest.raises(StoreError, match='deletion_pending'):
        store.accept_receipt(ticket, valid, request_origin=ticket.destination.origin)
    assert calls == 1
    assert ticket.artifact.exists()
    assert store.read_session(session_id)['state'] == 'deletion_pending'
    with pytest.raises(StoreError):
        store.prepare(session_id)
    store.close()
    monkeypatch.setattr(fs, 'unlink_generated_file', original)
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'removed'
        assert recovered.read_session(session_id)['receipt'] == valid
        assert not ticket.artifact.exists()
    finally:
        recovered.close()


@pytest.mark.parametrize('state', ['receipt_persisted', 'deletion_pending'])
@pytest.mark.parametrize('already_unlinked', [False, True])
def test_crash_after_receipt_or_unlink(tmp_path: Path, state: str, already_unlinked: bool) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    metadata = store.read_session(session_id)
    metadata.update(state=state, receipt=receipt(ticket))
    if state == 'deletion_pending':
        identities: list[Json] = []
        for suffix in ('journal', 'gz'):
            info = (tmp_path / f'{session_id}.{suffix}').stat()
            identities.append({'suffix': suffix, 'device': info.st_dev, 'inode': info.st_ino})
        metadata['delete_intent'] = {'authority': 'receipt', 'files': identities}
    store._save(session_id, metadata)
    store.close()
    if already_unlinked:
        ticket.artifact.unlink()
        (tmp_path / f'{session_id}.journal').unlink()
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'removed'
        assert not ticket.artifact.exists()
    finally:
        recovered.close()


def test_crash_receipt_candidate_before_atomic_replace(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    metadata = store.read_session(session_id)
    metadata.update(state='receipt_persisted', receipt=receipt(ticket))
    store.close()
    candidate = tmp_path / f'{session_id}.meta-new'
    descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(line_bytes(metadata))
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'removed'
        assert not ticket.artifact.exists()
    finally:
        recovered.close()


def test_local_delete_selected_only_and_active_rejection(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        active = new_journal(store)
        with pytest.raises(StoreError):
            store.delete_local(active)
        selected, retained = seal_empty(store), seal_empty(store)
        unrelated = tmp_path / 'unrelated'
        unrelated.write_bytes(b'keep')
        unrelated.chmod(0o600)
        store.delete_local(selected)
        assert store.read_session(selected)['state'] == 'removed'
        assert (tmp_path / f'{retained}.gz').exists()
        assert unrelated.read_bytes() == b'keep'
    finally:
        store.close()


def test_inflight_remains_charged_and_writer_failure_retained(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    class StalledStore(TraceStore):
        @override
        def append(self, session_id: str, record: Record) -> str | None:
            del session_id, record
            entered.set()
            assert release.wait(5)
            raise OSError('injected device-independent disk failure')

    recorder = TraceRecorder(tmp_path, queue_events=1, store_factory=lambda: StalledStore(
        tmp_path, limits=LIMITS, preparation_bytes=32768, failure_bytes=16384, disk_bytes=262144))
    try:
        handle = recorder.start(CONFIG)
        assert handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        assert entered.wait(2)
        assert handle.status().queued_events == 1  # popped item is still charged
        for _ in range(100):
            assert not handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        release.set()
        deadline = time.monotonic() + 3
        while handle.status().state != 'failed' and time.monotonic() < deadline:
            time.sleep(0.005)
        assert handle.status().state == 'failed'
        assert recorder.failure == 'io_error'
    finally:
        release.set()
        recorder.shutdown(wait=True)
    recovered = make_store(tmp_path)
    try:
        terminal = records(tmp_path, handle.session_id)[-1]
        assert terminal['completion'] == 'incomplete'
        assert 'io_error' in cast(list[str], terminal['reasons'])
        # Lost RAM counters are unknown, not invented precise drops on recovery.
        assert terminal['dropped_events'] == 0
    finally:
        recovered.close()


def test_concurrent_admission_serializes_sequences_and_clocks(tmp_path: Path) -> None:
    release = threading.Event()

    def factory() -> TraceStore:
        assert release.wait(5)
        return make_store(tmp_path, limits=TraceLimits(compressed_bytes=131072,
            expanded_bytes=65536, record_bytes=2048, events=250), disk_bytes=524288)

    recorder = TraceRecorder(tmp_path, store_factory=factory)
    handle = recorder.start(CONFIG)

    def producer() -> None:
        for _ in range(25):
            assert handle.emit('status', {'severity': 'info', 'code': 'session_on'})

    threads = [threading.Thread(target=producer) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        handle.cleanup_finished()
        release.set()
        wait_sealed(handle)
        with (tmp_path / f'{handle.session_id}.gz').open('rb') as source:
            result = validate_trace(source)
        trace = [json.loads(line) for line in gzip.decompress(
            (tmp_path / f'{handle.session_id}.gz').read_bytes()).splitlines()]
        assert [item['seq'] for item in trace[1:]] == list(range(1, 102))
        assert [item['mono_ns'] for item in trace[1:]] == sorted(item['mono_ns'] for item in trace[1:])
        assert result.summary['completion'] == 'complete'
    finally:
        release.set()
        recorder.shutdown(wait=True)


@pytest.mark.parametrize('suffix', ['part', 'meta-new'])
def test_crash_partial_preparation_evidence_retained(tmp_path: Path, suffix: str) -> None:
    store = make_store(tmp_path)
    session_id = new_journal(store)
    store.append(session_id, status_event(session_id))
    store.close()
    candidate = tmp_path / f'{session_id}.{suffix}'
    fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(b'partial evidence')
    recovered = make_store(tmp_path)
    try:
        terminal = records(tmp_path, session_id)[-1]
        assert terminal['completion'] == 'incomplete'
        assert terminal['accepted_events'] == 1
        evidence_suffix = 'part-damaged' if suffix == 'part' else 'meta-damaged'
        assert (tmp_path / f'{session_id}.{evidence_suffix}').read_bytes() == b'partial evidence'
    finally:
        recovered.close()


def test_disk_exhaustion_does_not_evict_old_artifacts(tmp_path: Path) -> None:
    store = make_store(tmp_path, disk_bytes=65536)
    try:
        session_id = seal_empty(store)
        original = (tmp_path / f'{session_id}.gz').read_bytes()
        # Simulate other retained evidence consuming the entire capture allowance.
        extra = tmp_path / 'unrelated-evidence'
        extra.write_bytes(b'x' * 20000)
        extra.chmod(0o600)
        store._scan_usage()
        with pytest.raises(StoreError, match='storage_limit'):
            new_journal(store)
        assert (tmp_path / f'{session_id}.gz').read_bytes() == original
        assert extra.read_bytes() == b'x' * 20000
    finally:
        store.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX hardlink injection')
def test_lock_hardlink_must_not_chmod_external_file(tmp_path: Path) -> None:
    root = tmp_path / 'traces'
    root.mkdir(mode=0o700)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'unchanged')
    outside.chmod(0o644)
    os.link(outside, root / '.trace.lock')
    with pytest.raises(StoreError, match='unsafe_entry'):
        make_store(root)
    assert outside.stat().st_mode & 0o777 == 0o644
    assert outside.read_bytes() == b'unchanged'


def test_valid_receipt_fsync_failure_never_unlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    original = fs.fsync_descriptor

    def fail(descriptor: int, **kwargs: object) -> None:
        del descriptor, kwargs
        raise OSError('fsync fault')

    monkeypatch.setattr(fs, 'fsync_descriptor', fail)
    with pytest.raises(OSError):
        store.accept_receipt(ticket, receipt(ticket), request_origin=ticket.destination.origin)
    assert ticket.artifact.exists()
    assert store.read_session(session_id)['state'] == 'uploading'
    store.close()
    monkeypatch.setattr(fs, 'fsync_descriptor', original)
    recovered = make_store(tmp_path)
    try:
        # Valid candidate is re-synchronized, published, then deletion resumes.
        assert recovered.read_session(session_id)['state'] == 'removed'
        assert not ticket.artifact.exists()
    finally:
        recovered.close()


def test_corrupt_metadata_delete_only_other_session_recovers(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    damaged = seal_empty(store)
    active = new_journal(store)
    store.close()
    metadata = tmp_path / f'{damaged}.json'
    metadata.write_bytes(b'{truncated')
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(damaged)['state'] == 'damaged'
        assert (tmp_path / f'{damaged}.meta-damaged').read_bytes() == b'{truncated'
        assert recovered.read_session(active)['state'] == 'retained'
        with pytest.raises(StoreError):
            recovered.authorize(damaged, Destination('https://example.com', str(uuid4()), str(uuid4())))
        recovered.delete_local(damaged)
        assert recovered.read_session(damaged)['state'] == 'removed'
    finally:
        recovered.close()


def test_bad_artifact_prepare_failure_retains_identity_and_bytes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        session_id = seal_empty(store)
        store.authorize(session_id, Destination('https://example.com', str(uuid4()), str(uuid4())))
        before = store.read_session(session_id)
        artifact = tmp_path / f'{session_id}.gz'
        artifact.write_bytes(b'broken')
        with pytest.raises(StoreError, match='preparation_failed'):
            store.prepare(session_id)
        after = store.read_session(session_id)
        assert after['destination'] == before['destination']
        assert after['authorization_id'] == before['authorization_id']
        assert after['state'] == 'authorized'
        assert artifact.read_bytes() == b'broken'
    finally:
        store.close()


def test_checkpoint_failure_stops_active_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkpointed = threading.Event()

    def fail_checkpoint(self: TraceStore) -> None:
        del self
        checkpointed.set()
        raise OSError('checkpoint failed')

    monkeypatch.setattr(TraceStore, 'checkpoint', fail_checkpoint)
    recorder = TraceRecorder(tmp_path, checkpoint_seconds=0.05, store_factory=lambda: make_store(tmp_path))
    try:
        handle = recorder.start(CONFIG)
        handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        assert checkpointed.wait(2)
        deadline = time.monotonic() + 2
        while handle.status().state != 'failed' and time.monotonic() < deadline:
            time.sleep(0.005)
        assert handle.status().state == 'failed'
        assert handle.status().failure == 'io_error'
        assert not handle.emit('status', {'severity': 'info', 'code': 'session_on'})
    finally:
        recorder.shutdown(wait=True)
    recovered = make_store(tmp_path)
    try:
        assert records(tmp_path, handle.session_id)[-1]['completion'] == 'incomplete'
    finally:
        recovered.close()


def test_restart_has_no_network_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail('local trace store attempted network access')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    ticket = upload_ticket(store, session_id)
    store.close()
    recovered = make_store(tmp_path)
    try:
        assert recovered.read_session(session_id)['state'] == 'authorized'
        next_attempt = recovered.prepare(session_id)
        assert next_attempt.attempt_id != ticket.attempt_id
        assert replace(next_attempt, attempt_id=ticket.attempt_id) == ticket
    finally:
        recovered.close()


def test_deadline_stops_admission_even_when_writer_stalled(tmp_path: Path) -> None:
    release = threading.Event()

    def factory() -> TraceStore:
        assert release.wait(5)
        return make_store(tmp_path)

    recorder = TraceRecorder(tmp_path, store_factory=factory)
    try:
        handle = recorder.start(CONFIG)
        handle.request_close(cleanup_timeout=0)
        assert not handle.emit('status', {'severity': 'info', 'code': 'session_on'})
        handle.cleanup_finished()  # late barrier must not turn timeout into complete
        assert handle.status().state == 'finalizing'
        assert handle.status().attempted_events == 0
        release.set()
        wait_sealed(handle)
        assert records(tmp_path, handle.session_id)[-1]['reasons'] == ['cleanup_timeout']
    finally:
        release.set()
        recorder.shutdown(wait=True)


def test_closed_store_cannot_mutate_without_process_lock(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session_id = seal_empty(store)
    store.close()
    store.close()
    with pytest.raises(StoreError, match='store_closed'):
        store.delete_local(session_id)
    assert (tmp_path / f'{session_id}.gz').exists()


def test_raw_command_chunk_boundaries_and_scalar_events(tmp_path: Path) -> None:
    from artisanlib.santoker_trace_contract import TX_UUID

    recorder = TraceRecorder(tmp_path, store_factory=lambda: make_store(tmp_path))
    connection_id, operation_id = str(uuid4()), str(uuid4())
    try:
        handle = recorder.start(CONFIG)
        assert handle.emit('connection', {'connection_id': connection_id, 'state': 'connected'})
        assert handle.emit('command_intent', {'connection_id': connection_id, 'operation_id': operation_id},
                           payload=b'\x00\xff\x01\xfe')
        for index, chunk in enumerate((b'\x00\xff', b'\x01\xfe')):
            assert handle.emit('tx_attempt', {'connection_id': connection_id, 'operation_id': operation_id,
                'chunk_index': index, 'characteristic': TX_UUID, 'direction': 'tx', 'response': False}, payload=chunk)
            assert handle.emit('tx_result', {'connection_id': connection_id, 'operation_id': operation_id,
                                            'chunk_index': index, 'outcome': 'written'})
        assert handle.emit('parser', {'connection_id': connection_id, 'result': 'noise'})
        assert handle.emit('control', {'operation_id': operation_id, 'action': 'heating_on', 'value': False})
        assert handle.emit('milestone', {'roast_ordinal': 1, 'name': 'charge'})
        handle.cleanup_finished()
        wait_sealed(handle)
        trace = records(tmp_path, handle.session_id)
        payloads = [base64.b64decode(cast(dict[str, str], event['payload'])['data'])
                    for event in trace if 'payload' in event]
        assert payloads == [b'\x00\xff\x01\xfe', b'\x00\xff', b'\x01\xfe']
        assert all(event['response'] is False for event in trace if event['kind'] == 'tx_attempt')
        assert not any(event['kind'] == 'device_ack' for event in trace)
        assert trace[-1]['completion'] == 'complete'
    finally:
        recorder.shutdown(wait=True)
