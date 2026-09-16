#
# ABOUT
# Bounded opt-in Santoker BLE transport trace hooks.
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

"""Opt-in Santoker transport ownership, independent of UI monitoring generations.

The legacy ClientBLE path is intentionally unchanged. Each traced owner retains
its loop/client/SDK operations until cleanup actually settles, even after the
recorder deadline. No device acknowledgement is inferred from SDK completion.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
import time
from typing import TYPE_CHECKING, TypeVar, override
from uuid import uuid4

from artisanlib.async_comm import AsyncLoopThread, IteratorReader
from artisanlib.ble_port import ble
from artisanlib.santoker_trace import Scalar, SessionHandle
from artisanlib.santoker_trace_contract import RX_UUID, TX_UUID

if TYPE_CHECKING:
    from bleak import BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from artisanlib.ble_port import ClientBLE

_T = TypeVar('_T')


@dataclass(eq=False)
class _Connection:
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    disconnected: threading.Event = field(default_factory=threading.Event)
    client: BleakClient | None = None
    queue: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    pending: int = 0
    pending_bytes: int = 0
    poisoned: bool = False
    subscribed: bool = False
    closing: bool = False


class _Reader(IteratorReader):
    def __init__(self, chunks: AsyncIterator[bytes], trace_parser: Callable[[str], None]) -> None:
        super().__init__(chunks)
        self.trace_parser = trace_parser

    # IteratorReader.readuntil accumulates arbitrary noise. Santoker only needs
    # the single header byte, so discard preceding noise without accumulating it.
    @override
    async def readuntil(self, separator: bytes = b'\n') -> bytes:
        if len(separator) != 1:
            raise ValueError('single byte separator required')
        while True:
            index = self._backlog.find(separator)
            if index >= 0:
                if index:
                    self.trace_parser('noise')
                self._backlog = self._backlog[index + 1:]
                return separator
            if self._backlog:
                self.trace_parser('noise')
            self._backlog = await anext(self._chunks)


class SantokerBLETrace:
    MAX_OWNERS = 16
    MAX_WRITES = 16
    MAX_WRITE_BYTES = 65536
    PARSER_EVENTS = 200
    PARSER_BYTES = 256 * 1024
    WRITE_TIMEOUT = 5.0
    _owners: set[SantokerBLETrace] = set()
    _owners_lock = threading.Lock()

    def __init__(self, owner: ClientBLE, handle: SessionHandle,
                 read_msg: Callable[[asyncio.StreamReader | IteratorReader], Awaitable[None]]) -> None:
        self.owner = owner
        self.handle = handle  # never rebound to a later ON
        self._read_msg = read_msg
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._cleanup = threading.Event()
        self._thread: AsyncLoopThread | None = None
        self._connection: _Connection | None = None
        self._writes = 0
        self._started = False
        self._close_requested = False
        self._future: Future[None] | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None

    @classmethod
    def owners_cleanup_complete(cls) -> bool:
        with cls._owners_lock:
            return not cls._owners

    @property
    def cleanup_complete(self) -> bool:
        """Actual task/SDK disconnect barrier, NOT the recorder sealing status."""
        return self._cleanup.is_set()

    def request_close(self, cleanup_timeout: float = 5.0) -> None:
        """Call BEFORE safety OFF writes; writes remain allowed until stop()."""
        with self._lock:
            if self._close_requested:
                return
            self._close_requested = True
        self.handle.emit('status', {'severity': 'info', 'code': 'off_requested'})
        self.handle.request_close(cleanup_timeout=cleanup_timeout)

    def _state(self, connection: _Connection, state: str) -> None:
        self.handle.emit('connection', {'connection_id': connection.connection_id, 'state': state})

    def start(self, case_sensitive: bool, scan_timeout: float, connect_timeout: float,
              address: str | None) -> None:
        with self._lock:
            if self._started or self._stopped.is_set():
                return  # one owner/immutable handle per ON; never restart it
            self._started = True
        with self._owners_lock:
            if len(self._owners) >= self.MAX_OWNERS:
                self.handle.mark_incomplete('queue_overflow')
                self.handle.emit('status', {'severity': 'error', 'code': 'queue_overflow'})
                self._stopped.set()
                self.handle.request_close()
                self.handle.cleanup_finished()  # no transport was admitted
                self._cleanup.set()
                return
            self._owners.add(self)
        self.owner._running = True
        self._thread = AsyncLoopThread()
        self._future = Future()
        self._future.add_done_callback(self._lifecycle_cancelled)
        self._thread.loop.call_soon_threadsafe(
            self._launch, case_sensitive, scan_timeout, connect_timeout, address)

    def _launch(self, case_sensitive: bool, scan_timeout: float,
                connect_timeout: float, address: str | None) -> None:
        self._lifecycle_task = asyncio.create_task(
            self._run(case_sensitive, scan_timeout, connect_timeout, address))
        self._lifecycle_task.add_done_callback(self._loop_finished)

    def _lifecycle_cancelled(self, proxy: Future[None]) -> None:
        if proxy.cancelled():
            self.stop()
            if self._thread is not None and self._lifecycle_task is not None:
                self._thread.loop.call_soon_threadsafe(self._lifecycle_task.cancel)

    def _loop_finished(self, task: asyncio.Task[None]) -> None:
        # Only the actual lifecycle Task, never its cancelable proxy, releases
        # the private loop. Its finalizer drains independently owned cleanup.
        assert self._future is not None
        error = asyncio.CancelledError() if task.cancelled() else task.exception()
        if self._future.set_running_or_notify_cancel():
            if error is None:
                self._future.set_result(None)
            else:
                self._future.set_exception(error)
        self._lifecycle_task = None
        self._thread = None

    def stop(self) -> None:
        self.handle.request_close()
        self._stopped.set()  # persistent per-owner cancellation, unlike scan Event
        self.owner._running = False
        if not self._started:
            self._cleanup.set()
            self.handle.cleanup_finished()

    async def _settle(self, future: asyncio.Future[_T],
                      cancel: Callable[[], None] | None = None) -> _T:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                self.stop()
                if cancel is not None and not future.done():
                    cancel()  # request cancellation once; repeated cancels still drain
                    cancel = None
        return future.result()

    async def _sdk(self, operation: Awaitable[_T]) -> _T:
        assert ble._asyncLoopThread is not None
        loop = ble._asyncLoopThread.loop
        settled: Future[_T] = Future()
        settled.set_running_or_notify_cancel()  # this settlement record cannot be canceled
        task: asyncio.Future[_T] | None = None

        def finished(actual: asyncio.Future[_T]) -> None:
            if actual.cancelled():
                settled.set_exception(asyncio.CancelledError())
            elif (error := actual.exception()) is not None:
                settled.set_exception(error)
            else:
                settled.set_result(actual.result())

        def submit() -> None:
            nonlocal task
            task = asyncio.ensure_future(operation)
            task.add_done_callback(finished)

        def cancel() -> None:
            if task is not None:
                task.cancel()

        def request_cancel() -> None:
            loop.call_soon_threadsafe(cancel)

        # One sequential non-write SDK operation per lifecycle. Its actual task
        # callback is the only settlement authority; no concurrent proxy is.
        loop.call_soon_threadsafe(submit)
        return await self._settle(asyncio.wrap_future(settled), request_cancel)

    def _notify(self, connection: _Connection, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        # Copy/admit raw RX before all parser, readiness, closing and stale guards.
        self.handle.emit('rx', {'connection_id': connection.connection_id,
                               'characteristic': RX_UUID, 'direction': 'rx'}, payload=data)
        with self._lock:
            if (connection is not self._connection or connection.poisoned or
                    connection.disconnected.is_set() or self._stopped.is_set() or self._thread is None):
                return
            size = len(data)
            if (connection.pending >= self.PARSER_EVENTS or size > 65536 or
                    connection.pending_bytes + size > self.PARSER_BYTES):
                connection.poisoned = True
                self.handle.mark_incomplete('queue_overflow')
                self.handle.emit('status', {'severity': 'error', 'code': 'queue_overflow'})
                self.stop()  # never parse a stream with a silently missing chunk
                return
            connection.pending += 1
            connection.pending_bytes += size
            self._thread.loop.call_soon_threadsafe(connection.queue.put_nowait, bytes(data))

    async def _reader(self, connection: _Connection) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            while True:
                data = await connection.queue.get()
                with self._lock:
                    connection.pending -= 1
                    connection.pending_bytes -= len(data)
                    poisoned = connection.poisoned or self._stopped.is_set()
                if poisoned:
                    raise asyncio.CancelledError
                yield data

        def parsed(result: str) -> None:
            self.handle.emit('parser', {'connection_id': connection.connection_id, 'result': result})

        stream = _Reader(chunks(), parsed)
        while not self._stopped.is_set() and not connection.disconnected.is_set():
            await self._read_msg(stream)

    async def _disconnect(self, connection: _Connection) -> bool:
        client = connection.client
        if client is None:
            return True
        if connection.subscribed and client.is_connected:
            self._state(connection, 'unsubscribe_started')
            try:
                await self._sdk(client.stop_notify(RX_UUID))
                self._state(connection, 'unsubscribed')
            except (Exception, asyncio.CancelledError):  # SDK boundary; actual await settled
                self._state(connection, 'unsubscribe_failed')
        self._state(connection, 'disconnect_started')
        try:
            await self._sdk(client.disconnect())
            if client.is_connected:
                self._state(connection, 'disconnect_failed')
                return False
            self._state(connection, 'disconnected')
            return True
        except (Exception, asyncio.CancelledError):  # SDK boundary; actual await settled
            self._state(connection, 'disconnect_failed')
            return False

    async def _run(self, case_sensitive: bool, scan_timeout: float,
                   connect_timeout: float, address: str | None) -> None:
        clean = True
        connection: _Connection | None = None
        reader: asyncio.Task[None] | None = None
        try:
            while not self._stopped.is_set():
                connection = _Connection()
                self._connection = connection
                self._state(connection, 'scan_started')
                # This legacy synchronous bridge can return AFTER OFF. Its
                # callback closes over this attempt, never a mutable client slot.
                def disconnected(_client: BleakClient, c: _Connection = connection) -> None:
                    c.disconnected.set()

                def observed(client: BleakClient, c: _Connection = connection) -> None:
                    c.client = client

                connected_client, service, name = ble.scan_and_connect(
                    (self.owner._device_descriptions, self.owner._device_manufacturer_keys),
                    set(), case_sensitive, disconnected,
                    scan_timeout, connect_timeout, address, client_observer=observed)
                if connected_client is not None:
                    connection.client = connected_client
                client = connected_client
                if client is not None and client.is_connected:
                    self._state(connection, 'connected')
                else:
                    self._state(connection, 'connect_failed')
                if self._stopped.is_set():
                    break
                if connection.client is not None:
                    valid = (client is not None and service is not None and client.is_connected and
                             any(s.uuid.casefold() == service.casefold() for s in client.services))
                    if valid and client is not None and not connection.disconnected.is_set():
                        self.owner._ble_client = client
                        self.owner._connected_service_uuid = service
                        self.owner._connected_device_name = name
                        self.owner.on_connect()
                        if not self._stopped.is_set():
                            reader = asyncio.create_task(self._reader(connection))
                            self._state(connection, 'subscribe_started')
                            try:
                                def notify(sender: BleakGATTCharacteristic, data: bytearray,
                                           c: _Connection = connection) -> None:
                                    self._notify(c, sender, data)
                                await self._sdk(client.start_notify(RX_UUID, notify))
                                connection.subscribed = True
                                self._state(connection, 'subscribed')
                            except (Exception, asyncio.CancelledError):  # actual SDK await settled
                                self._state(connection, 'subscribe_failed')
                                connection.disconnected.set()
                            while not self._stopped.is_set() and not connection.disconnected.is_set():
                                if reader.done():
                                    reader.result()
                                    raise RuntimeError('parser ended')
                                await asyncio.sleep(0.01)
                    if reader is not None:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                        reader = None
                    # Do not disconnect or release ownership while SDK writes
                    # (including timed out caller waits) can still execute.
                    with self._lock:
                        connection.closing = True
                    while self._writes:
                        await asyncio.sleep(0.01)
                    clean = await self._disconnect(connection)
                    self.owner.on_disconnect()
                    if not clean:
                        break
                    self.owner._ble_client = None
                    connection = None
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            self.stop()
        except Exception:  # contain hardware/parser failures, preserve cleanup ownership
            self.handle.mark_incomplete('capture_error')
            self.handle.emit('status', {'severity': 'error', 'code': 'capture_error'})
        finally:
            self.stop()
            # Repeated lifecycle cancellation must not interrupt reader/write
            # drainage or an unsubscribe/disconnect already in progress.
            await self._settle(asyncio.create_task(self._finish(connection, reader, clean)))

    async def _finish(self, connection: _Connection | None,
                      reader: asyncio.Task[None] | None, clean: bool) -> None:
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        while self._writes:
            await asyncio.sleep(0.01)
        if connection is not None and clean:
            clean = await self._disconnect(connection)
            self.owner.on_disconnect()
        self.owner._connected_service_uuid = None
        self.owner._connected_device_name = None
        if clean:
            self._connection = None
            self.owner._ble_client = None
            self.handle.emit('status', {'severity': 'info', 'code': 'cleanup_finished'})
            self.handle.cleanup_finished()
            self._cleanup.set()
            with self._owners_lock:
                self._owners.discard(self)
        else:
            # Retain the exact unresolved client AND its bounded capacity slot.
            self._connection = connection
            self.owner._ble_client = None if connection is None else connection.client
            self.handle.mark_incomplete('capture_error')
            # A finite deadline is not proof of hardware cleanup; no blind retry.

    def write(self, message: bytes, response: bool = False,
              write_characteristic: str | None = None, chunk: int = 20) -> None:
        if chunk <= 0 or len(message) > self.MAX_WRITE_BYTES:
            raise ValueError('invalid write bounds')
        with self._lock:
            connection = self._connection
            if (self._stopped.is_set() or connection is None or connection.client is None or
                    not connection.client.is_connected or connection.disconnected.is_set() or connection.closing or
                    write_characteristic not in {None, TX_UUID}):
                return
            expires_at = time.monotonic() + self.WRITE_TIMEOUT
            operation = str(uuid4())
            self.handle.emit('command_intent', {'connection_id': connection.connection_id,
                                              'operation_id': operation}, payload=message)
            if self._writes >= self.MAX_WRITES:
                self.handle.mark_incomplete('queue_overflow')
                self.handle.emit('status', {'severity': 'error', 'code': 'queue_overflow'})
                raise BufferError('BLE write capacity exceeded')
            self._writes += 1
        assert ble._asyncLoopThread is not None
        coroutine = self._write(connection, operation, bytes(message), response, chunk, expires_at)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, ble._asyncLoopThread.loop)
        except Exception:
            coroutine.close()
            with self._lock:
                self._writes -= 1
            self.handle.mark_incomplete('capture_error')
            raise
        # Timeout of this synchronous caller NEVER releases SDK ownership or
        # cancels a concurrent Future before its coroutine has entered finally.
        future.result(timeout=max(0.0, expires_at - time.monotonic()) + 0.1)

    async def _write(self, connection: _Connection, operation: str, message: bytes,
                     response: bool, chunk: int, expires_at: float) -> None:
        assert connection.client is not None
        try:
            for index, offset in enumerate(range(0, len(message), chunk)):
                if time.monotonic() >= expires_at:
                    raise TimeoutError  # queued/remaining chunks were never SDK attempts
                fields: dict[str, Scalar] = {'connection_id': connection.connection_id, 'operation_id': operation,
                          'chunk_index': index}
                self.handle.emit('tx_attempt', {**fields, 'characteristic': TX_UUID,
                    'direction': 'tx', 'response': response}, payload=message[offset:offset+chunk])
                outcome = 'error'
                try:
                    async with asyncio.timeout(max(0.0, expires_at - time.monotonic())) as deadline:
                        await connection.client.write_gatt_char(TX_UUID, message[offset:offset+chunk], response=response)
                    if deadline.expired() or time.monotonic() >= expires_at:
                        raise TimeoutError
                    outcome = 'written'
                except TimeoutError:
                    outcome = 'timeout'
                    raise
                except asyncio.CancelledError:
                    outcome = 'cancelled'
                    raise
                finally:
                    self.handle.emit('tx_result', {**fields, 'outcome': outcome})
        finally:
            with self._lock:
                self._writes -= 1
