# Copyright (C) 2026 The Artisan team. AGPLv3+; see LICENSE.
"""Actual traced transport path with fake SDK boundaries; no BLE/network access."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError as FutureCancelledError
import gzip
import io
import json
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import Mock

import pytest
from pymodbus.framer.rtu import FramerRTU

from artisanlib.async_comm import AsyncLoopThread
from artisanlib import santoker_ble_trace as transport
from artisanlib.santoker import Santoker
from artisanlib.santoker_trace import CaptureConfig, SessionHandle, TraceRecorder
from artisanlib.santoker_trace_contract import RX_UUID, TX_UUID, validate_trace

SERVICE = '6e400001-b5a3-f393-e0a9-e50e24dcca9e'
CONFIG = CaptureConfig('4.0.0', 'linux', '6.1', 'x86_64')


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert predicate()


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.services = [Mock(uuid=SERVICE)]
        self.notify: Any = None
        self.disconnected: Any = None
        self.subscribe_error = False
        self.disconnect_error = False
        self.subscribe_data: bytes | None = None
        self.disconnect_data: bytes | None = None
        self.disconnect_gate = threading.Event()
        self.disconnect_gate.set()
        self.write_gate = threading.Event()
        self.write_gate.set()
        self.disconnect_started = threading.Event()
        self.writes: list[tuple[str, bytes, bool]] = []
        self.write_error: BaseException | None = None

    async def start_notify(self, uuid: str, callback: Any) -> None:
        assert uuid == RX_UUID
        self.notify = callback
        if self.subscribe_data is not None:
            callback(None, bytearray(self.subscribe_data))
        if self.subscribe_error:
            raise RuntimeError('not subscribed')

    async def stop_notify(self, uuid: str) -> None:
        assert uuid == RX_UUID

    async def disconnect(self) -> None:
        self.disconnect_started.set()
        if self.disconnect_data is not None and self.notify is not None:
            self.notify(None, bytearray(self.disconnect_data))
        while not self.disconnect_gate.is_set():
            await asyncio.sleep(0.001)
        if self.disconnect_error:
            raise RuntimeError('not disconnected')
        self.is_connected = False
        self.disconnected(self)

    async def write_gatt_char(self, uuid: str, data: bytes, *, response: bool) -> None:
        self.writes.append((uuid, bytes(data), response))
        while not self.write_gate.is_set():
            await asyncio.sleep(0.001)
        if self.write_error is not None:
            raise self.write_error


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.recorder = TraceRecorder(root)
        self.clients: list[FakeClient] = []
        self.sessions: list[tuple[Santoker, SessionHandle]] = []
        self.scan_gate = threading.Event()
        self.scan_gate.set()
        self.scan_entered = threading.Event()
        self.scan_count = 0

    def scan(self, _descriptions: Any, _blacklist: Any, _case: bool, callback: Any,
             _scan_timeout: float, _connect_timeout: float, _address: str | None, *,
             client_observer: Callable[[Any], None]) -> tuple[Any, str, str]:
        index = self.scan_count
        self.scan_count += 1
        self.scan_entered.set()
        assert self.scan_gate.wait(5)
        client = self.clients[index]
        client.disconnected = callback
        client_observer(client)
        return client, SERVICE, 'not persisted'

    def start(self, client: FakeClient | None = None) -> tuple[Santoker, SessionHandle]:
        self.clients.append(client or FakeClient())
        handle = self.recorder.start(CONFIG)
        santoker = Santoker(connect_using_ble=True, trace_handle=handle)
        self.sessions.append((santoker, handle))
        santoker.start()
        return santoker, handle

    def events(self, handle: SessionHandle) -> list[dict[str, Any]]:
        wait_for(lambda: handle.status().state in {'sealed', 'failed'})
        assert handle.status().state == 'sealed', handle.status()
        raw = (self.root / f'{handle.session_id}.gz').read_bytes()
        validate_trace(io.BytesIO(raw))
        return [json.loads(line) for line in gzip.decompress(raw).splitlines()]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    instance = Rig(tmp_path / 'traces')
    sdk = AsyncLoopThread()
    fake_ble = Mock(_asyncLoopThread=sdk, scan_and_connect=instance.scan)
    monkeypatch.setattr(transport, 'ble', fake_ble)
    yield instance
    instance.scan_gate.set()
    for client in instance.clients:
        client.disconnect_error = False
        client.disconnect_gate.set()
        client.write_gate.set()
    for santoker, _handle in instance.sessions:
        santoker.stop()
    for santoker, _handle in instance.sessions:
        wait_for(lambda s=santoker: s.trace_cleanup_complete)
    assert instance.recorder.shutdown(wait=True)
    sdk.loop.call_soon_threadsafe(sdk.loop.stop)


def test_delayed_connect_after_off_is_disconnected_without_subscription(rig: Rig) -> None:
    rig.scan_gate.clear()
    santoker, handle = rig.start()
    wait_for(rig.scan_entered.is_set)
    santoker.request_trace_close()
    santoker.stop()
    assert not santoker.trace_cleanup_complete
    rig.scan_gate.set()
    wait_for(lambda: santoker.trace_cleanup_complete)
    assert rig.clients[0].notify is None
    assert not santoker.isHeaderReady()
    events = rig.events(handle)
    assert 'subscribed' not in [e.get('state') for e in events]
    assert events[-1]['completion'] == 'complete'


def test_raw_before_ready_and_during_cleanup_and_safety_write(rig: Rig) -> None:
    client = FakeClient()
    client.subscribe_data = b'pre-ready noise'
    client.disconnect_data = b'closing noise'
    client.disconnect_gate.clear()
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    assert not santoker.isHeaderReady()
    santoker.request_trace_close()
    santoker.send_msg(santoker.WARMUP, 0)
    santoker.stop()
    wait_for(client.disconnect_started.is_set)
    assert not santoker.trace_cleanup_complete
    assert handle.status().state == 'closing'
    client.disconnect_gate.set()
    wait_for(lambda: santoker.trace_cleanup_complete)
    events = rig.events(handle)
    rx = [base64.b64decode(e['payload']['data']) for e in events if e['kind'] == 'rx']
    assert rx == [b'pre-ready noise', b'closing noise']
    assert any(e['kind'] == 'command_intent' for e in events)
    assert any(e.get('outcome') == 'written' for e in events)
    assert not any(e['kind'] == 'device_ack' for e in events)
    assert events[-1]['completion'] == 'complete'


def test_cleanup_timeout_does_not_release_transport_or_relabel_complete(rig: Rig) -> None:
    client = FakeClient()
    client.disconnect_gate.clear()
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    santoker.request_trace_close(0.02)
    santoker.stop()
    wait_for(client.disconnect_started.is_set)
    events = rig.events(handle)
    assert not santoker.trace_cleanup_complete
    assert events[-1]['reasons'] == ['cleanup_timeout']
    assert santoker._ble_client is not None
    assert santoker._ble_client._trace_transport in transport.SantokerBLETrace._owners
    client.disconnect_gate.set()
    wait_for(lambda: santoker.trace_cleanup_complete)
    assert rig.events(handle) == events


def test_stale_callbacks_across_reconnect_and_new_session(rig: Rig) -> None:
    first = FakeClient()
    santoker, handle = rig.start(first)
    wait_for(lambda: first.notify is not None)
    second = FakeClient()
    rig.clients.append(second)
    first.is_connected = False
    first.disconnected(first)
    wait_for(lambda: second.notify is not None)
    first.notify(None, bytearray(incoming(2, b'\xee\xa5')))
    first.disconnected(first)
    time.sleep(0.03)
    assert not santoker.isHeaderReady()
    assert second.is_connected
    assert rig.scan_count == 2
    second.notify(None, bytearray(incoming(1, b'\xee\xa5')))
    wait_for(santoker.isHeaderReady)
    santoker.stop()
    wait_for(lambda: santoker.trace_cleanup_complete)
    other, other_handle = rig.start()
    wait_for(lambda: rig.clients[2].notify is not None)
    first.notify(None, bytearray(b'late sealed callback'))
    first.disconnected(first)
    assert not other.isHeaderReady()
    other.stop()
    events = rig.events(handle)
    ids = [e['connection_id'] for e in events if e['kind'] == 'rx']
    assert len(set(ids)) == 2
    assert not any(e['kind'] == 'rx' for e in rig.events(other_handle))


def test_subscription_failure_is_not_success(rig: Rig) -> None:
    client = FakeClient()
    client.subscribe_error = True
    # Hold cleanup to inspect and stop before another scan.
    client.disconnect_gate.clear()
    santoker, handle = rig.start(client)
    wait_for(client.disconnect_started.is_set)
    santoker.stop()
    client.disconnect_gate.set()
    events = rig.events(handle)
    states = [e.get('state') for e in events]
    assert 'subscribe_failed' in states
    assert 'subscribed' not in states
    assert 'unsubscribe_started' not in states


@pytest.mark.parametrize('error,outcome', [(None, 'written'), (RuntimeError(), 'error'),
                                          (asyncio.CancelledError(), 'cancelled')])
def test_actual_chunks_and_sdk_results(rig: Rig, error: BaseException | None, outcome: str) -> None:
    client = FakeClient()
    client.write_error = error
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    payload = b'0123456789'
    if error is None:
        santoker._ble_client.send(payload, True, TX_UUID, 4)
    else:
        with pytest.raises((RuntimeError, FutureCancelledError)):
            santoker._ble_client.send(payload, True, TX_UUID, 4)
    santoker.stop()
    events = rig.events(handle)
    attempts = [e for e in events if e['kind'] == 'tx_attempt']
    results = [e for e in events if e['kind'] == 'tx_result']
    chunks = [payload[i:i+4] for i in range(0, len(payload), 4)] if error is None else [payload[:4]]
    assert [base64.b64decode(e['payload']['data']) for e in attempts] == chunks
    assert client.writes == [(TX_UUID, chunk, True) for chunk in chunks]
    assert [e['chunk_index'] for e in attempts] == list(range(len(chunks)))
    assert {e['operation_id'] for e in attempts + results} == {attempts[0]['operation_id']}
    assert {e['connection_id'] for e in attempts + results} == {attempts[0]['connection_id']}
    assert [e['outcome'] for e in results] == [outcome] * len(chunks)


def test_write_timeout_awaits_actual_cancellation_before_cleanup(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    entered = threading.Event()
    release = threading.Event()

    async def delayed_cancel(*_args: Any, **_kwargs: Any) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            raise

    monkeypatch.setattr(client, 'write_gatt_char', delayed_cancel)
    monkeypatch.setattr(transport.SantokerBLETrace, 'WRITE_TIMEOUT', 0.02)
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    with pytest.raises(TimeoutError):
        santoker._ble_client.send(b'timeout')
    assert entered.is_set()
    santoker.stop()
    time.sleep(0.02)
    assert not santoker.trace_cleanup_complete
    assert not client.disconnect_started.is_set()
    release.set()
    events = rig.events(handle)
    assert [e['outcome'] for e in events if e['kind'] == 'tx_result'] == ['timeout']


def incoming(size: int, header: bytes) -> bytes:
    # Independently construct incoming telemetry (not Santoker.create_msg).
    data = (42).to_bytes(size, 'big') if size else b''
    body = b'\x02\x04' + bytes([size]) + data
    return header + b'\xfa' + body + FramerRTU.compute_CRC(body).to_bytes(2, 'big') + b'\xff\xfc\xff\xff'


@pytest.mark.parametrize('size', [0, 1, 2, 3, 4])
@pytest.mark.parametrize('header', [b'\xee\xa5', b'\xee\xb5'])
def test_incoming_lengths_and_learned_header_over_ble(rig: Rig, size: int, header: bytes) -> None:
    santoker, handle = rig.start()
    client = rig.clients[0]
    wait_for(lambda: client.notify is not None)
    frame = incoming(size, header)
    # Fragmentation must survive the bounded callback handoff.
    client.notify(None, bytearray(frame[:4]))
    client.notify(None, bytearray(frame[4:]))
    if 1 <= size <= 3:
        wait_for(santoker.isHeaderReady)
        assert header == santoker.HEADER
        assert santoker.getPower() == 42
    else:
        time.sleep(0.03)
        assert not santoker.isHeaderReady()
        assert santoker.getPower() == -1
    santoker.stop()
    assert len([e for e in rig.events(handle) if e['kind'] == 'rx']) == 2


def test_parser_overflow_is_bounded_visible_and_stops_partial_stream(rig: Rig) -> None:
    santoker, handle = rig.start()
    client = rig.clients[0]
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    traced = santoker._ble_client._trace_transport
    assert traced is not None and traced._thread is not None
    blocked = threading.Event()
    release = threading.Event()

    def block_loop() -> None:
        blocked.set()
        assert release.wait(5)

    traced._thread.loop.call_soon_threadsafe(block_loop)
    wait_for(blocked.is_set)
    for _ in range(250):
        client.notify(None, bytearray(b'\xee'))
    assert traced._connection is not None
    assert traced._connection.pending == 200
    assert traced._connection.poisoned
    assert traced._stopped.is_set()
    release.set()
    events = rig.events(handle)
    assert events[-1]['completion'] == 'incomplete'
    assert events[-1]['reasons'] == ['queue_overflow']
    assert events[-1]['dropped_events'] == 0  # raw RX was NOT lost
    assert len([e for e in events if e['kind'] == 'rx']) == 250
    assert not santoker.isHeaderReady()


def test_tcp_ignores_handle_and_no_sink_uses_legacy_path(rig: Rig) -> None:
    handle = rig.recorder.start(CONFIG)
    tcp = Santoker(trace_handle=handle)
    assert tcp._ble_client is None
    tcp.request_trace_close()
    assert handle.status().attempted_events == 0
    legacy = Santoker(connect_using_ble=True)
    assert legacy._ble_client is not None
    assert legacy._ble_client._trace_transport is None
    handle.cleanup_finished()


def test_owner_capacity_failure_is_visible_without_starting_transport(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.SantokerBLETrace, 'MAX_OWNERS', 0)
    santoker, handle = rig.start()
    assert santoker.trace_cleanup_complete
    assert not rig.scan_entered.is_set()
    assert rig.events(handle)[-1]['reasons'] == ['queue_overflow']


@pytest.mark.parametrize('failure', ['exception', 'timeout', 'late_after_off'])
def test_constructed_client_owned_through_failed_or_late_connect(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from artisanlib import ble_port

    client = FakeClient()
    entered = threading.Event()
    release = threading.Event()
    client.disconnect_gate.clear()
    real_bridge = ble_port.BLE()
    real_bridge._asyncLoopThread = transport.ble._asyncLoopThread

    async def scan(*_args: Any) -> tuple[Any, str]:
        return Mock(name='device'), SERVICE

    async def connect() -> None:
        entered.set()
        if failure == 'exception':
            raise RuntimeError('constructed but connect failed')
        if failure == 'timeout':
            await asyncio.sleep(10)
        else:
            while not release.is_set():
                await asyncio.sleep(0.001)

    def construct(*_args: Any, **kwargs: Any) -> FakeClient:
        client.disconnected = kwargs['disconnected_callback']
        return client

    monkeypatch.setattr(client, 'connect', connect, raising=False)
    monkeypatch.setattr(real_bridge, '_scan', scan)
    monkeypatch.setattr(ble_port, 'BleakClient', construct)
    monkeypatch.setattr(transport.ble, 'scan_and_connect', real_bridge.scan_and_connect)
    handle = rig.recorder.start(CONFIG)
    santoker = Santoker(connect_using_ble=True, trace_handle=handle)
    rig.sessions.append((santoker, handle))
    rig.clients.append(client)
    santoker.start(connect_timeout=0.03 if failure == 'timeout' else 5)
    wait_for(entered.is_set)
    santoker.request_trace_close(0.02)
    santoker.stop()
    if failure == 'late_after_off':
        events = rig.events(handle)
        assert events[-1]['reasons'] == ['cleanup_timeout']
        assert not client.disconnect_started.is_set()
        assert not santoker.trace_cleanup_complete
        release.set()
    wait_for(client.disconnect_started.is_set)
    assert not santoker.trace_cleanup_complete
    assert client.notify is None
    client.disconnect_gate.set()
    wait_for(lambda: santoker.trace_cleanup_complete)
    events = rig.events(handle)
    assert 'subscribed' not in [e.get('state') for e in events]
    assert not client.is_connected
    # The fixture owns the shared fake SDK loop; do not stop it via BLE.__del__.
    real_bridge._asyncLoopThread = None


def test_error_on_second_chunk_does_not_attempt_third(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()

    async def write(uuid: str, data: bytes, *, response: bool) -> None:
        client.writes.append((uuid, bytes(data), response))
        if len(client.writes) == 2:
            raise RuntimeError('second chunk')

    monkeypatch.setattr(client, 'write_gatt_char', write)
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    with pytest.raises(RuntimeError):
        santoker._ble_client.send(b'abcdef', chunk=2)
    santoker.stop()
    events = rig.events(handle)
    assert [e['outcome'] for e in events if e['kind'] == 'tx_result'] == ['written', 'error']
    assert [entry[1] for entry in client.writes] == [b'ab', b'cd']


def test_failed_disconnect_never_sets_cleanup_barrier(rig: Rig) -> None:
    client = FakeClient()
    client.disconnect_error = True
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    santoker.request_trace_close(0.02)
    santoker.stop()
    events = rig.events(handle)
    assert not santoker.trace_cleanup_complete
    assert {'capture_error', 'cleanup_timeout'} <= set(events[-1]['reasons'])
    assert santoker._ble_client is not None
    traced = santoker._ble_client._trace_transport
    assert traced is not None
    wait_for(lambda: traced._thread is None)
    assert traced in transport.SantokerBLETrace._owners
    retained = traced._connection is not None and traced._connection.client is client
    owner_retained = traced.owner._ble_client is client
    # Test-only quiescence cleanup, not a production retry or invented barrier.
    client.disconnect_error = False
    asyncio.run_coroutine_threadsafe(client.disconnect(), transport.ble._asyncLoopThread.loop).result(5)
    with transport.SantokerBLETrace._owners_lock:
        transport.SantokerBLETrace._owners.discard(traced)
    rig.sessions.remove((santoker, handle))
    assert retained and owner_retained
    assert not santoker.trace_cleanup_complete


def test_parser_byte_budget_includes_not_yet_run_callbacks(rig: Rig) -> None:
    santoker, handle = rig.start()
    client = rig.clients[0]
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    traced = santoker._ble_client._trace_transport
    assert traced is not None and traced._thread is not None
    blocked = threading.Event()
    release = threading.Event()

    def block_loop() -> None:
        blocked.set()
        assert release.wait(5)

    traced._thread.loop.call_soon_threadsafe(block_loop)
    wait_for(blocked.is_set)
    for _ in range(7):
        client.notify(None, bytearray(b'x' * 40000))
    assert traced._connection is not None
    assert traced._connection.pending == 6
    assert traced._connection.pending_bytes == 240000
    assert traced._connection.poisoned
    release.set()
    assert rig.events(handle)[-1]['reasons'] == ['queue_overflow']


def test_write_admission_bound_is_explicit_and_never_an_sdk_attempt(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.SantokerBLETrace, 'MAX_WRITES', 0)
    santoker, handle = rig.start()
    client = rig.clients[0]
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    with pytest.raises(BufferError, match='capacity exceeded'):
        santoker._ble_client.send(b'intent only')
    assert client.writes == []
    santoker.stop()
    events = rig.events(handle)
    assert len([e for e in events if e['kind'] == 'command_intent']) == 1
    assert not any(e['kind'] in {'tx_attempt', 'tx_result'} for e in events)
    assert events[-1]['reasons'] == ['queue_overflow']


def test_notification_copy_and_unsubscribe_failure(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()

    async def fail_unsubscribe(_uuid: str) -> None:
        raise RuntimeError('SDK rejected unsubscribe')

    monkeypatch.setattr(client, 'stop_notify', fail_unsubscribe)
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    payload = bytearray(b'original bytes')
    client.notify(None, payload)
    payload[:] = b'mutated bytes!'
    santoker.stop()
    events = rig.events(handle)
    assert [base64.b64decode(e['payload']['data']) for e in events if e['kind'] == 'rx'] == [b'original bytes']
    states = [e.get('state') for e in events]
    assert 'unsubscribe_failed' in states
    assert 'unsubscribed' not in states
    assert events[-1]['completion'] == 'complete'  # actual disconnect still settled


@pytest.mark.parametrize('method', ['start_notify', 'stop_notify', 'disconnect'])
@pytest.mark.parametrize('cancel_proxy', [False, True])
def test_nonwrite_sdk_settlement_survives_repeated_lifecycle_cancellation(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, method: str, cancel_proxy: bool) -> None:
    client = FakeClient()
    entered = threading.Event()
    cancelling = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    sdk_tasks: list[asyncio.Task[Any]] = []
    original = getattr(client, method)

    async def deferred(*args: Any, **kwargs: Any) -> None:
        task = asyncio.current_task()
        assert task is not None
        sdk_tasks.append(task)
        entered.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                cancelling.set()  # SDK cleanup itself can defer repeated cancels
        await original(*args, **kwargs)
        settled.set()

    monkeypatch.setattr(client, method, deferred)
    santoker, handle = rig.start(client)
    assert santoker._ble_client is not None
    traced = santoker._ble_client._trace_transport
    assert traced is not None and traced._thread is not None
    private = traced._thread  # retain for deterministic RED-case test teardown
    if method != 'start_notify':
        wait_for(lambda: client.notify is not None)
        santoker.stop()
    wait_for(entered.is_set)

    async def lifecycle_task() -> asyncio.Task[Any]:
        return next(task for task in asyncio.all_tasks()
                    if task.get_coro().__name__ == '_run')

    task = asyncio.run_coroutine_threadsafe(lifecycle_task(), private.loop).result(5)
    transport.ble._asyncLoopThread.loop.call_soon_threadsafe(sdk_tasks[0].cancel)
    wait_for(cancelling.is_set)
    if cancel_proxy:
        assert traced._future is not None
        traced._future.cancel()
    else:
        private.loop.call_soon_threadsafe(task.cancel)
    time.sleep(0.02)
    private.loop.call_soon_threadsafe(task.cancel)
    transport.ble._asyncLoopThread.loop.call_soon_threadsafe(sdk_tasks[0].cancel)
    time.sleep(0.02)
    observed = (traced.cleanup_complete, traced._thread is not None,
                traced in transport.SantokerBLETrace._owners, settled.is_set())
    release.set()
    wait_for(settled.is_set)
    assert observed == (False, True, True, False)
    wait_for(lambda: santoker.trace_cleanup_complete)
    events = rig.events(handle)
    assert events[-1]['completion'] == 'complete'
    assert not any(e['kind'] == 'device_ack' for e in events)


def test_command_deadline_stops_cumulative_chunks(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(transport.SantokerBLETrace, 'WRITE_TIMEOUT', 0.08)

    async def slow(uuid: str, data: bytes, *, response: bool) -> None:
        client.writes.append((uuid, bytes(data), response))
        await asyncio.sleep(0.055)

    monkeypatch.setattr(client, 'write_gatt_char', slow)
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    with pytest.raises(TimeoutError):
        santoker._ble_client.send(b'abcdefgh', chunk=2)
    santoker.stop()
    events = rig.events(handle)
    assert [data for _uuid, data, _response in client.writes] == [b'ab', b'cd']
    assert [e['outcome'] for e in events if e['kind'] == 'tx_result'] == ['written', 'timeout']


def test_command_expired_in_sdk_queue_has_no_first_attempt(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.SantokerBLETrace, 'WRITE_TIMEOUT', 0.02)
    santoker, handle = rig.start()
    client = rig.clients[0]
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    traced = santoker._ble_client._trace_transport
    assert traced is not None
    blocked = threading.Event()
    release = threading.Event()

    def block_sdk() -> None:
        blocked.set()
        assert release.wait(5)

    transport.ble._asyncLoopThread.loop.call_soon_threadsafe(block_sdk)
    wait_for(blocked.is_set)
    try:
        with pytest.raises(TimeoutError):
            santoker._ble_client.send(b'expired')
        assert traced._writes == 1  # timeout did not release queued ownership
        santoker.stop()
        assert not santoker.trace_cleanup_complete
    finally:
        release.set()
    events = rig.events(handle)
    assert not client.writes
    assert not any(e['kind'] in {'tx_attempt', 'tx_result'} for e in events)


def test_expired_write_suppressing_cancel_cannot_resume_chunks(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    cancelling = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(transport.SantokerBLETrace, 'WRITE_TIMEOUT', 0.02)

    async def deferred(uuid: str, data: bytes, *, response: bool) -> None:
        client.writes.append((uuid, bytes(data), response))
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelling.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            # Deliberately suppress cancellation, as a native SDK wrapper might.

    monkeypatch.setattr(client, 'write_gatt_char', deferred)
    santoker, handle = rig.start(client)
    wait_for(lambda: client.notify is not None)
    assert santoker._ble_client is not None
    try:
        with pytest.raises(TimeoutError):
            santoker._ble_client.send(b'abcd', chunk=2)
        assert cancelling.is_set()
        santoker.stop()
        assert not santoker.trace_cleanup_complete
        assert not client.disconnect_started.is_set()
    finally:
        release.set()
    events = rig.events(handle)
    assert [data for _uuid, data, _response in client.writes] == [b'ab']
    assert [e['outcome'] for e in events if e['kind'] == 'tx_result'] == ['timeout']


def test_proxy_cancel_before_lifecycle_launch_keeps_loop_until_actual_completion(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = transport.SantokerBLETrace._launch

    def delayed_launch(self: transport.SantokerBLETrace, case: bool, scan: float,
                       connect: float, address: str | None) -> None:
        entered.set()
        assert release.wait(5)
        original(self, case, scan, connect, address)

    monkeypatch.setattr(transport.SantokerBLETrace, '_launch', delayed_launch)
    santoker, handle = rig.start()
    wait_for(entered.is_set)
    assert santoker._ble_client is not None
    traced = santoker._ble_client._trace_transport
    assert traced is not None and traced._future is not None
    try:
        traced._future.cancel()
        assert traced._thread is not None
        assert not santoker.trace_cleanup_complete
    finally:
        release.set()
    wait_for(lambda: santoker.trace_cleanup_complete)
    wait_for(lambda: traced._thread is None)
    assert not rig.scan_entered.is_set()
    assert rig.events(handle)[-1]['completion'] == 'complete'
