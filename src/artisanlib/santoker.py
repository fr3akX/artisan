#
# ABOUT
# Santoker Network support for Artisan
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# Marko Luther, 2023

import asyncio
import logging

from pymodbus.framer.rtu import FramerRTU
from collections.abc import Callable, Awaitable
from typing import Final, TYPE_CHECKING, override

from artisanlib.santoker_diagnostics import DiagnosticField, SantokerDiagnosticsSession

if TYPE_CHECKING:
    from artisanlib.atypes import SerialSettings # pylint: disable=unused-import
    from bleak.backends.characteristic import BleakGATTCharacteristic  # pylint: disable=unused-import

from artisanlib.async_comm import AsyncComm, AsyncIterable, IteratorReader
from artisanlib.ble_port import ClientBLE

_log: Final[logging.Logger] = logging.getLogger(__name__)

class SantokerCube_BLE(ClientBLE):

    # Santoker Cube (RFstar) service and characteristics UUIDs
    SANTOKER_CUBE_NAME:Final[str] = 'SANTOKER'
    SANTOKER_CUBE_SERVICE_UUID:Final[str] = '6e400001-b5a3-f393-e0a9-e50e24dcca9e' # Nordic UART Service # advertised service UUID
    SANTOKER_CUBE_NOTIFY_UUID:Final[str] = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'
    SANTOKER_CUBE_WRTIE_UUID:Final[str] = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'

    def __init__(self,
                    read_msg:Callable[[asyncio.StreamReader|IteratorReader], Awaitable[None]],
                    connected_handler:Callable[[], None]|None = None,
                    disconnected_handler:Callable[[], None]|None = None):
        super().__init__()

        # Protocol parser variables
        self._read_queue : asyncio.Queue[bytes]|None = None
        self._read_msg:Callable[[asyncio.StreamReader|IteratorReader], Awaitable[None]] = read_msg

        # handlers
        self._connected_handler = connected_handler
        self._disconnected_handler = disconnected_handler

        self.add_device_description(self.SANTOKER_CUBE_SERVICE_UUID, self.SANTOKER_CUBE_NAME)
        self.add_notify(self.SANTOKER_CUBE_NOTIFY_UUID, self.notify_callback)
        self.add_write(self.SANTOKER_CUBE_SERVICE_UUID, self.SANTOKER_CUBE_WRTIE_UUID)

    def notify_callback(self, _sender:'BleakGATTCharacteristic', data:bytearray) -> None:
        if hasattr(self, '_async_loop_thread') and self._async_loop_thread is not None and self._read_queue is not None:
            if self._logging:
                _log.debug('notify: %s => %s', self._read_queue.qsize(), data)
            asyncio.run_coroutine_threadsafe(
                    self._read_queue.put(bytes(data)),
                    self._async_loop_thread.loop)

    @override
    def on_connect(self) -> None: # pylint: disable=no-self-use
        if self._connected_handler is not None:
            try:
                self._connected_handler()
            except Exception as e: # pylint: disable=broad-except
                _log.exception(e)

    @override
    def on_disconnect(self) -> None: # pylint: disable=no-self-use
        if self._disconnected_handler is not None:
            try:
                self._disconnected_handler()
            except Exception as e: # pylint: disable=broad-except
                _log.exception(e)

    async def reader(self) -> None:
        self._read_queue = asyncio.Queue(maxsize=200) # queue needs to be started in the current async event loop!
        stream = IteratorReader(AsyncIterable(self._read_queue))
        while True:
            await self._read_msg(stream)

    @override
    def on_start(self) -> None:
        if hasattr(self, '_async_loop_thread') and self._async_loop_thread is not None:
            # start the reader
            asyncio.run_coroutine_threadsafe(
                    self.reader(),
                    self._async_loop_thread.loop)



class Santoker(AsyncComm):

    HEADER_WIFI:Final[bytes] = b'\xEE\xA5'
    HEADER_BT:Final[bytes] = b'\xEE\xB5'
    CODE_HEADER:Final[bytes] = b'\x02\x04'
    TAIL:Final[bytes] = b'\xff\xfc\xff\xff'

    # data targets
    BOARD:Final[bytes] = b'\xF0'
    BT:Final[bytes] = b'\xF1'
    ET:Final[bytes] = b'\xF2'
    OLD_BT:Final[bytes] = b'\xF3'
    OLD_ET:Final[bytes] = b'\xF4'
    BT_ROR:Final[bytes] = b'\xF5'
    ET_ROR:Final[bytes] = b'\xF6'
    IR:Final[bytes] = b'\xF8'
    POWER:Final[bytes] = b'\xFA'
    AIR:Final[bytes] = b'\xCA'
    DRUM:Final[bytes] = b'\xC0'
    WARMUP:Final[bytes] = b'\x7E'
    WARMUP_TEMP:Final[bytes] = b'\x7F'
    MIN_WARMUP_TEMP_C:Final[float] = 100.0
    MAX_WARMUP_TEMP_C:Final[float] = 300.0
    DEFAULT_WARMUP_TEMP_C:Final[float] = 190.0
    #
    CHARGE:Final = b'\x80'
    DRY:Final = b'\x81'
    FCs:Final = b'\x82'
    SCs:Final = b'\x83'
    DROP:Final = b'\x84'
    #
    # unsupported commands:
    MIN_POWER = b'\x85'
    MAX_POWER = b'\x86'
    BT_CALIB = b'\x87'
    ET_CALIB = b'\x88'

    __slots__ = [
        'HEADER', '_charge_handler', '_dry_handler', '_fcs_handler', '_scs_handler', '_drop_handler',
        '_warmup_handler', '_warmup_temp_handler', '_ready_handler', '_board', '_bt', '_et',
        '_bt_ror', '_et_ror', '_ir', '_power', '_air', '_drum', '_CHARGE', '_DRY', '_FCs',
        '_SCs', '_DROP', '_header_ready', '_warmup', '_warmup_target', '_reported_warmup_target',
        '_desired_warmup', '_connect_using_ble', '_ble_client', '_diagnostics', '_frame_handler'
    ]

    def __init__(self, host:str = '127.0.0.1', port:int = 8080, serial:'SerialSettings|None' = None,
                connect_using_ble:bool = False,
                connected_handler:Callable[[], None]|None = None,
                disconnected_handler:Callable[[], None]|None = None,
                charge_handler:Callable[[], None]|None = None,
                dry_handler:Callable[[], None]|None = None,
                fcs_handler:Callable[[], None]|None = None,
                scs_handler:Callable[[], None]|None = None,
                drop_handler:Callable[[], None]|None = None,
                warmup_handler:Callable[[bool | None], None]|None = None,
                warmup_temp_handler:Callable[[float], None]|None = None,
                warmup_target: float = DEFAULT_WARMUP_TEMP_C,
                ready_handler:Callable[[bool], None]|None = None,
                diagnostics: SantokerDiagnosticsSession | None = None,
                frame_handler:Callable[[], None]|None = None) -> None:

        self._diagnostics: SantokerDiagnosticsSession | None = diagnostics
        self._frame_handler:Callable[[], None]|None = frame_handler
        self._desired_warmup:bool | None = None

        def _connected() -> None:
            self._record_connected()
            if connected_handler is not None:
                try:
                    connected_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)

        def _disconnected() -> None:
            self.resetProtocolState()
            self._record_disconnected()
            if disconnected_handler is not None:
                try:
                    disconnected_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)

        super().__init__(host, port, serial, _connected, _disconnected)

        self.HEADER:bytes = (self.HEADER_BT if connect_using_ble else self.HEADER_WIFI)
        self._connect_using_ble:bool = connect_using_ble

        # handlers
        self._charge_handler:Callable[[], None]|None = charge_handler
        self._dry_handler:Callable[[], None]|None = dry_handler
        self._fcs_handler:Callable[[], None]|None = fcs_handler
        self._scs_handler:Callable[[], None]|None = scs_handler
        self._drop_handler:Callable[[], None]|None = drop_handler
        self._warmup_handler:Callable[[bool | None], None]|None = warmup_handler
        self._warmup_temp_handler:Callable[[float], None]|None = warmup_temp_handler
        self._ready_handler:Callable[[bool], None]|None = ready_handler

        self._header_ready:bool = False
        self._warmup:bool | None = None
        self._warmup_target:float = self.DEFAULT_WARMUP_TEMP_C
        self._reported_warmup_target:float | None = None
        if self.MIN_WARMUP_TEMP_C <= warmup_target <= self.MAX_WARMUP_TEMP_C:
            self._warmup_target = warmup_target

        # current readings
        self._board:float = -1  # board temperature in °C
        self._bt:float = -1     # bean temperature in °C
        self._et:float = -1     # environmental temperature in °C
        self._bt_ror:float = -1 # bean temperature rate-of-rise in C°/min
        self._et_ror:float = -1 # environmental temperature rate-of-rise in C°/min
        self._ir:float = -1     # IR temperature in °C
        self._power:int = -1    # heater power in % [0-100]
        self._air:int = -1      # fan speed in % [0-100]
        self._drum:int = -1     # drum speed in % [0-100]

        # current roast state
        self._CHARGE:bool = False
        self._DRY:bool = False
        self._FCs:bool = False
        self._SCs:bool = False
        self._DROP:bool = False

        self._ble_client:SantokerCube_BLE|None = \
                (SantokerCube_BLE(self.read_msg, _connected, _disconnected) if self._connect_using_ble else None)


    # external API to access machine state

    def getBoard(self) -> float:
        return self._board
    def getBT(self) -> float:
        return self._bt
    def getET(self) -> float:
        return self._et
    def getBT_RoR(self) -> float:
        return self._bt_ror
    def getET_RoR(self) -> float:
        return self._et_ror
    def getIR(self) -> float:
        return self._ir
    def getPower(self) -> int:
        return self._power
    def getAir(self) -> int:
        return self._air
    def getDrum(self) -> int:
        return self._drum
    def isHeaderReady(self) -> bool:
        return self._header_ready
    def getWarmup(self) -> bool | None:
        return self._warmup
    def getWarmupTarget(self) -> float:
        return self._warmup_target

    def getReportedWarmupTarget(self) -> float | None:
        return self._reported_warmup_target

    def _safe_record(self, callback: Callable[[], None]) -> None:
        if self._diagnostics is None:
            return
        try:
            callback()
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)

    def _record_connected(self) -> None:
        if self._diagnostics is None:
            return
        self._safe_record(self._diagnostics.record_connected)

    def _record_disconnected(self) -> None:
        if self._diagnostics is None:
            return
        self._safe_record(self._diagnostics.record_disconnected)

    def _record_protocol(self, ready: bool, header: bytes | None) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_protocol(ready, header[1:2] if header is not None else None))

    def _record_rx(self, packet: bytes, description: str, accepted: bool) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_rx(packet, description, accepted=accepted))

    def _record_tx(self, packet: bytes, target: bytes, value: int) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_tx(
            packet,
            f'{target.hex()}={value}',
        ))

    def _record_decoded(self, field: DiagnosticField, value: float | int | bool) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_decoded(field, value))

    def _record_desired_warmup(self) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_desired_warmup(self._desired_warmup, self._warmup_target))

    def _record_reported_warmup(self) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_reported_warmup(self._warmup))

    def _record_reported_target(self) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_reported_target(self._reported_warmup_target))

    def _record_frame(self) -> None:
        if self._frame_handler is not None:
            try:
                self._frame_handler()
            except Exception as e: # pylint: disable=broad-except
                _log.exception(e)

    def _setHeaderReady(self, ready: bool) -> None:
        if ready != self._header_ready:
            self._header_ready = ready
            self._record_protocol(ready, self.HEADER if ready else None)
            if self._ready_handler is not None:
                try:
                    self._ready_handler(ready)
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)

    def _setWarmupState(self, enabled: bool | None) -> None:
        if enabled != self._warmup:
            self._warmup = enabled
            self._record_reported_warmup()
            if self._warmup_handler is not None:
                try:
                    self._warmup_handler(enabled)
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)

    def requestWarmupOn(self, temp_c: float) -> bool:
        if not self._header_ready:
            return False
        if not self.MIN_WARMUP_TEMP_C <= temp_c <= self.MAX_WARMUP_TEMP_C:
            return False
        self._warmup_target = temp_c
        self._record_desired_warmup()
        self.send_msg(self.WARMUP_TEMP, int(round(temp_c * 10)))
        self.send_msg(self.WARMUP, 1)
        return True

    def setWarmupTarget(self, temp_c: float) -> bool:
        if not self.MIN_WARMUP_TEMP_C <= temp_c <= self.MAX_WARMUP_TEMP_C:
            return False
        self._warmup_target = temp_c
        self._record_desired_warmup()
        if self._desired_warmup is True:
            if not self._header_ready:
                return False
            self.send_msg(self.WARMUP_TEMP, int(round(temp_c * 10)))
        return True

    def setWarmup(self, enabled: bool) -> bool:
        if not self._header_ready:
            return False
        self._desired_warmup = enabled
        self._record_desired_warmup()
        if enabled:
            return self.requestWarmupOn(self._warmup_target)
        self.send_msg(self.WARMUP, 0)
        return True

    def resetReadings(self) -> None:
        self._board = -1
        self._bt = -1
        self._et = -1
        self._bt_ror = -1
        self._et_ror = -1
        self._ir = -1
        self._power = -1
        self._air = -1
        self._drum = -1

    def resetProtocolState(self) -> None:
        self._setHeaderReady(False)
        self._reported_warmup_target = None
        self._setWarmupState(None)

    # message decoder

    def register_reading(self, target:bytes, data:bytes) -> None:
        # if self._logging:
        value:int
        # convert data into the integer data
        if target in {self.BT_ROR, self.ET_ROR}:
            # first for bits of the RoR data contain the sign of the value
            if len(data) != 2:
                return
            unsigned_data = bytearray(data)
            unsigned_data[0] = data[0] & 15 # clear first 4 bits
            value = int.from_bytes(bytes(unsigned_data), 'big')
            if data[0] & 240 != 176: # first 4 bits not a positive sign (b1011)
                value = - value
        else:
            value = int.from_bytes(data, 'big')

        if target == self.BOARD:
            board_c = value / 10.0
            if board_c != self._board:
                self._board = board_c
                self._record_decoded('board_c', board_c)
        elif target in {self.BT, self.OLD_BT}:
            bt_c = value / 10.0
            bt = (bt_c if self._bt == -1 else (2*bt_c + self._bt)/3)
            if bt != self._bt:
                self._bt = bt
                self._record_decoded('bt_c', bt)
            if self._logging:
                _log.debug('BT: %s',self._bt)
        elif target in {self.ET, self.OLD_ET}:
            et_c = value / 10.0
            et = (et_c if self._et == -1 else (2*et_c + self._et)/3)
            if et != self._et:
                self._et = et
                self._record_decoded('et_c', et)
            if self._logging:
                _log.debug('ET: %s',self._et)
        elif target == self.BT_ROR:
            bt_ror_c = value / 10.0
            if bt_ror_c != self._bt_ror:
                self._bt_ror = bt_ror_c
                self._record_decoded('bt_ror_c', bt_ror_c)
        elif target == self.ET_ROR:
            et_ror_c = value / 10.0
            if et_ror_c != self._et_ror:
                self._et_ror = et_ror_c
                self._record_decoded('et_ror_c', et_ror_c)
        elif target == self.IR:
            ir_c = value / 10.0
            if ir_c != self._ir:
                self._ir = ir_c
                self._record_decoded('ir_c', ir_c)
        elif target == self.POWER:
            if value != self._power:
                self._power = value
                self._record_decoded('power', value)
        elif target == self.AIR:
            if value != self._air:
                self._air = value
                self._record_decoded('fan', value)
        elif target == self.DRUM:
            if value != self._drum:
                self._drum = value
                self._record_decoded('drum', value)

        elif target == self.CHARGE:
            b = bool(value)
            if b and b != self._CHARGE and self._charge_handler is not None:
                try:
                    self._charge_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)
            if b != self._CHARGE:
                self._CHARGE = b
                self._record_decoded('charge', b)
        elif target == self.DRY:
            b = bool(value)
            if b and b != self._DRY and self._dry_handler is not None:
                try:
                    self._dry_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)
            if b != self._DRY:
                self._DRY = b
                self._record_decoded('dry', b)
        elif target == self.FCs:
            b = bool(value)
            if b and b != self._FCs and self._fcs_handler is not None:
                try:
                    self._fcs_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)
            if b != self._FCs:
                self._FCs = b
                self._record_decoded('fcs', b)
        elif target == self.SCs:
            b = bool(value)
            if b and b != self._SCs and self._scs_handler is not None:
                try:
                    self._scs_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)
            if b != self._SCs:
                self._SCs = b
                self._record_decoded('scs', b)
        elif target == self.DROP:
            b = bool(value)
            if b and b != self._DROP and self._drop_handler is not None:
                try:
                    self._drop_handler()
                except Exception as e: # pylint: disable=broad-except
                    _log.exception(e)
            if b != self._DROP:
                self._DROP = b
                self._record_decoded('drop', b)
        elif target == self.WARMUP:
            if value in {0, 1}:
                self._setWarmupState(bool(value))
            elif self._logging:
                _log.debug('invalid warm-up state: %s', value)
        elif target == self.WARMUP_TEMP:
            temp_c = value / 10.0
            if self.MIN_WARMUP_TEMP_C <= temp_c <= self.MAX_WARMUP_TEMP_C:
                if temp_c != self._reported_warmup_target:
                    self._reported_warmup_target = temp_c
                    if self._warmup_temp_handler is not None:
                        try:
                            self._warmup_temp_handler(temp_c)
                        except Exception as e: # pylint: disable=broad-except
                            _log.exception(e)
                    self._record_reported_target()
            elif self._logging:
                _log.debug('invalid warm-up target: %s', temp_c)
#        elif self._logging and target in {self.MIN_POWER, self.MAX_POWER, self.BT_CALIB, self.ET_CALIB}:
#            _log.debug('unsupported data target %s', target)
#        elif self._logging:
#            _log.debug('unknown data target %s', target)


    # asyncio read implementation

    # https://www.oreilly.com/library/view/using-asyncio-in/9781492075325/ch04.html
    @override
    async def read_msg(self, stream: asyncio.StreamReader|IteratorReader) -> None:
        candidate:bytearray = bytearray()

        async def read_candidate(size: int) -> bytes:
            try:
                part = await stream.readexactly(size)
            except asyncio.IncompleteReadError as exc:
                partial = bytes(exc.partial)
                if partial:
                    candidate.extend(partial)
                if candidate:
                    self._record_rx(bytes(candidate), 'truncated frame', accepted=False)
                raise
            candidate.extend(part)
            return part

        # look for the first header byte
        read_bytes = bytearray(await stream.readuntil(self.HEADER[0:1]))
        candidate = bytearray(read_bytes[-1:])

        # check for the second header byte
        snd_header_byte = await read_candidate(1)
        if snd_header_byte == self.HEADER_BT[1:2]:
            candidate_header = self.HEADER_BT
        elif snd_header_byte == self.HEADER_WIFI[1:2]:
            candidate_header = self.HEADER_WIFI
        else:
            self._record_rx(bytes(candidate), 'invalid second header', accepted=False)
            return

        # read the data target (BT, ET,..)
        target = await read_candidate(1)
        # read code header
        code2 = await read_candidate(2)
        if code2 != self.CODE_HEADER:
            self._record_rx(bytes(candidate), 'invalid code header', accepted=False)
            return

        # read the data length
        data_len = await read_candidate(1)
        if data_len != b'\x03':
            self._record_rx(bytes(candidate), 'invalid data length', accepted=False)
            return

        data = await read_candidate(int.from_bytes(data_len, 'big'))

        # read and check CRC over code header+length+data
        crc = await read_candidate(2)
        calculated_crc = FramerRTU.compute_CRC(self.CODE_HEADER + data_len + data).to_bytes(2, 'big')
        if self._verify_crc and crc[1] != calculated_crc[1]: # we only check the second CRC bit!
            self._record_rx(bytes(candidate), 'CRC mismatch', accepted=False)
            if self._logging:
                _log.debug('CRC error')
            return

        # check tail
        tail = await read_candidate(4)
        if tail != self.TAIL:
            self._record_rx(bytes(candidate), 'invalid tail', accepted=False)
            return

        # full message decoded
        self.HEADER = candidate_header
        self._setHeaderReady(True)
        self._record_rx(bytes(candidate), 'accepted frame', accepted=True)
        self.register_reading(target, data)
        self._record_frame()

    # send message interface

    # message encoder for values as unsigned integers
    def create_msg(self, target:bytes, value: int) -> bytes:
        data_len = 3 #(value.bit_length() + 7) // 8
        data = self.CODE_HEADER + data_len.to_bytes(1, 'big') + value.to_bytes(data_len, 'big')
        crc: bytes = FramerRTU.compute_CRC(data).to_bytes(2, 'big')
        return self.HEADER + target + data + crc + self.TAIL

    def send_msg(self, target:bytes, value: int) -> None:
        packet:bytes = self.create_msg(target, value)
        self._record_tx(packet, target, value)
        if self._connect_using_ble and hasattr(self, '_ble_client') and self._ble_client is not None:
            # send via BLE
            if self._logging:
                _log.debug('send_msg(%s,%s): %s',target,value,packet)
            self._ble_client.send(packet)
        else:
            # send via socket
            self.send(packet)


    @override
    def start(self, connect_timeout:float=5) -> None:
        if self._connect_using_ble and hasattr(self, '_ble_client') and self._ble_client is not None:
            self._ble_client.setLogging(self._logging)
            self._ble_client.start(case_sensitive=False, scan_timeout=5, connect_timeout=connect_timeout)
        else:
            super().start(connect_timeout)

    @override
    def stop(self) -> None:
        self.resetProtocolState()
        if self._connect_using_ble and hasattr(self, '_ble_client') and self._ble_client is not None:
            self._ble_client.stop()
            #del self._ble_client # on this level the released object should be automatically collected by the GC
            self._ble_client = None
        else:
            super().stop()


def main() -> None:
    import time
    santoker = Santoker(host = '10.10.100.254', port = 20001)
    santoker.start()
    for _ in range(4):
        print('>>> hallo')
        santoker.send_msg(santoker.POWER,1000) # set power to 1000Hz
        time.sleep(1)
        print('BT',santoker.getBT())
        time.sleep(1)
    santoker.stop()
    time.sleep(1)
    #print('thread alive?',santoker._thread.is_alive())

if __name__ == '__main__':
    main()
