#
# ABOUT
# Santoker diagnostics session model.
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
# Marko Luther, 2026

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import RLock
from typing import Literal

TransportKind = Literal['BLE', 'Wi-Fi', 'serial']
DiagnosticCategory = Literal[
    'session', 'connection', 'protocol', 'state', 'restoration', 'rx', 'tx'
]
DiagnosticDirection = Literal['RX', 'TX']
DiagnosticField = Literal[
    'board_c', 'bt_c', 'et_c', 'ir_c', 'bt_ror_c', 'et_ror_c',
    'power', 'fan', 'drum', 'charge', 'dry', 'fcs', 'scs', 'drop'
]


class RestorationState(Enum):
    IDLE = 'idle'
    WAITING_FOR_DATA = 'waiting for valid data'
    PENDING = 'pending convergence'
    CONVERGED = 'converged'
    BLOCKED_BY_CHARGE = 'blocked by CHARGE'


@dataclass(frozen=True)
class SantokerDiagnosticEvent:
    sequence: int
    timestamp_utc: datetime
    category: DiagnosticCategory
    direction: DiagnosticDirection | None
    description: str
    packet: bytes | None


@dataclass(frozen=True)
class SantokerDiagnosticsState:
    session_started_utc: datetime
    session_ended_utc: datetime | None
    monitoring_active: bool
    transport: TransportKind
    connected: bool
    protocol_ready: bool
    active_header: str | None
    reconnect_count: int
    last_packet_utc: datetime | None
    board_c: float | None
    bt_c: float | None
    et_c: float | None
    ir_c: float | None
    bt_ror_c: float | None
    et_ror_c: float | None
    power: int | None
    fan: int | None
    drum: int | None
    desired_warmup: bool | None
    desired_target_c: float | None
    reported_warmup: bool | None
    reported_target_c: float | None
    restoration_state: RestorationState
    last_restoration_attempt_utc: datetime | None
    charge_latched: bool
    retained_event_count: int
    discarded_event_count: int


@dataclass(frozen=True)
class SantokerDiagnosticsView:
    state: SantokerDiagnosticsState
    events: tuple[SantokerDiagnosticEvent, ...]
    first_retained_sequence: int | None
    last_sequence: int


def utc_now() -> datetime:
    return datetime.now(UTC)


def _format_packet(packet: bytes | None) -> str:
    return '' if packet is None else ' '.join(f'{value:02X}' for value in packet)


def _value_to_report(value: datetime | RestorationState | bool | float | int | None) -> str:
    if value is None:
        return 'unknown'
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds')
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        return f'{value:.1f}' if value.is_integer() else f'{value:g}'
    return str(value)


_STATE_LABELS: list[tuple[str, str]] = [
    ('session_started_utc', 'session started'),
    ('session_ended_utc', 'session ended'),
    ('monitoring_active', 'monitoring active'),
    ('transport', 'transport'),
    ('connected', 'connected'),
    ('protocol_ready', 'ready'),
    ('active_header', 'header'),
    ('reconnect_count', 'reconnect count'),
    ('last_packet_utc', 'last packet'),
    ('board_c', 'board'),
    ('bt_c', 'bean'),
    ('et_c', 'environment'),
    ('ir_c', 'infrared'),
    ('bt_ror_c', 'bean ror'),
    ('et_ror_c', 'environment ror'),
    ('power', 'power'),
    ('fan', 'fan'),
    ('drum', 'drum'),
    ('desired_warmup', 'desired warm-up'),
    ('desired_target_c', 'desired target'),
    ('reported_warmup', 'reported warm-up'),
    ('reported_target_c', 'reported target'),
    ('restoration_state', 'restoration'),
    ('last_restoration_attempt_utc', 'last restoration attempt'),
    ('charge_latched', 'CHARGE latch'),
    ('retained_event_count', 'retained events'),
    ('discarded_event_count', 'discarded events'),
]


class SantokerDiagnosticsSession:
    def __init__(
        self,
        transport: TransportKind,
        *,
        now_utc: Callable[[], datetime] = utc_now,
        max_events: int = 5000,
    ) -> None:
        if not isinstance(max_events, int) or isinstance(max_events, bool):
            raise ValueError('max_events must be an actual integer')
        if not 1 <= max_events <= 5000:
            raise ValueError('max_events must be between 1 and 5000')

        self._transport: TransportKind = transport
        self._now_utc = now_utc
        self._max_events = max_events
        self._lock = RLock()
        self._events: deque[SantokerDiagnosticEvent] = deque(maxlen=max_events)

        self._sequence = 1
        self._discarded_event_count = 0

        self._monitoring_active = True
        self._session_started_utc = self._utc_now()
        self._session_ended_utc: datetime | None = None
        self._connected = False
        self._protocol_ready = False
        self._active_header: str | None = None
        self._reconnect_count = 0
        self._was_disconnected = False
        self._ever_connected = False
        self._last_packet_utc: datetime | None = None

        self._board_c: float | None = None
        self._bt_c: float | None = None
        self._et_c: float | None = None
        self._ir_c: float | None = None
        self._bt_ror_c: float | None = None
        self._et_ror_c: float | None = None
        self._power: int | None = None
        self._fan: int | None = None
        self._drum: int | None = None
        self._desired_warmup: bool | None = None
        self._desired_target_c: float | None = None
        self._reported_warmup: bool | None = None
        self._reported_target_c: float | None = None
        self._restoration_state = RestorationState.IDLE
        self._last_restoration_attempt_utc: datetime | None = None
        self._charge_latched = False

        self._decoded_values: dict[DiagnosticField, float | int | bool] = {}

        self._append_event(
            category='session',
            description='monitoring started',
            direction=None,
            packet=None,
        )

    @property
    def transport(self) -> TransportKind:
        return self._transport

    @property
    def max_events(self) -> int:
        return self._max_events

    def _utc_now(self) -> datetime:
        timestamp = self._now_utc()
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError('now_utc() must return timezone-aware UTC datetime values')
        return timestamp

    def _append_event(
        self,
        *,
        category: DiagnosticCategory,
        description: str,
        direction: DiagnosticDirection | None,
        packet: bytes | None,
    ) -> None:
        if not self._monitoring_active:
            return

        if len(self._events) == self._max_events:
            self._discarded_event_count += 1

        self._events.append(
            SantokerDiagnosticEvent(
                sequence=self._sequence,
                timestamp_utc=self._utc_now(),
                category=category,
                direction=direction,
                description=description,
                packet=bytes(packet) if packet is not None else None,
            ),
        )
        self._sequence += 1

    def _snapshot_state(self) -> SantokerDiagnosticsState:
        return SantokerDiagnosticsState(
            session_started_utc=self._session_started_utc,
            session_ended_utc=self._session_ended_utc,
            monitoring_active=self._monitoring_active,
            transport=self._transport,
            connected=self._connected,
            protocol_ready=self._protocol_ready,
            active_header=self._active_header,
            reconnect_count=self._reconnect_count,
            last_packet_utc=self._last_packet_utc,
            board_c=self._board_c,
            bt_c=self._bt_c,
            et_c=self._et_c,
            ir_c=self._ir_c,
            bt_ror_c=self._bt_ror_c,
            et_ror_c=self._et_ror_c,
            power=self._power,
            fan=self._fan,
            drum=self._drum,
            desired_warmup=self._desired_warmup,
            desired_target_c=self._desired_target_c,
            reported_warmup=self._reported_warmup,
            reported_target_c=self._reported_target_c,
            restoration_state=self._restoration_state,
            last_restoration_attempt_utc=self._last_restoration_attempt_utc,
            charge_latched=self._charge_latched,
            retained_event_count=len(self._events),
            discarded_event_count=self._discarded_event_count,
        )

    def view(self, after_sequence: int = 0) -> SantokerDiagnosticsView:
        with self._lock:
            filtered_events = tuple(
                event for event in self._events
                if event.sequence > after_sequence
            )
            return SantokerDiagnosticsView(
                state=self._snapshot_state(),
                events=filtered_events,
                first_retained_sequence=self._events[0].sequence if self._events else None,
                last_sequence=self._sequence - 1,
            )

    def format_report(self) -> str:
        with self._lock:
            state = self._snapshot_state()
            event_lines: list[str] = []
            for event in self._events:
                line = f'{event.sequence:>6} {event.timestamp_utc.isoformat()} '
                line += f'{event.category}'
                if event.direction is not None:
                    line += f' {event.direction}'
                line += f': {event.description}'
                packet_text = _format_packet(event.packet)
                if packet_text:
                    line += f' {packet_text}'
                event_lines.append(line)

            values = {
                field: getattr(state, field)
                for field, _ in _STATE_LABELS
            }
            values['retained_event_count'] = state.retained_event_count
            values['discarded_event_count'] = state.discarded_event_count

            lines = ['Artisan Santoker Diagnostics v1']
            for field, label in _STATE_LABELS:
                lines.append(f'{label}: {_value_to_report(values[field])}')

            if state.discarded_event_count > 0:
                lines.append(f'[older entries discarded: {state.discarded_event_count}]')

            lines.extend(event_lines)
            return '\n'.join(lines) + '\n'

    def record_event(
        self,
        category: DiagnosticCategory,
        description: str,
        *,
        direction: DiagnosticDirection | None = None,
        packet: bytes | None = None,
    ) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            self._append_event(
                category=category,
                description=description,
                direction=direction,
                packet=packet,
            )

    def record_connection_attempt(self) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            self._append_event(
                category='connection',
                description='transport start requested',
                direction=None,
                packet=None,
            )

    def record_connected(self) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            if self._connected:
                return
            if self._was_disconnected and self._ever_connected:
                self._reconnect_count += 1
            self._connected = True
            self._was_disconnected = False
            self._ever_connected = True
            self._append_event(
                category='connection',
                description='connected',
                direction=None,
                packet=None,
            )

    def record_disconnected(self) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            self._connected = False
            self._protocol_ready = False
            self._active_header = None
            self._reported_warmup = None
            self._reported_target_c = None

            self._board_c = None
            self._bt_c = None
            self._et_c = None
            self._ir_c = None
            self._bt_ror_c = None
            self._et_ror_c = None
            self._power = None
            self._fan = None
            self._drum = None

            self._decoded_values = {}
            self._was_disconnected = self._ever_connected
            self._append_event(
                category='connection',
                description='disconnected',
                direction=None,
                packet=None,
            )

    def record_protocol(self, ready: bool, header: bytes | None) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            if header == b'\xA5':
                active_header = 'A5'
            elif header == b'\xB5':
                active_header = 'B5'
            elif header is None:
                active_header = None
            else:
                active_header = 'unknown'

            self._protocol_ready = ready
            self._active_header = active_header

            self._append_event(
                category='protocol',
                description=f'protocol ready={ready} header={active_header}',
                direction=None,
                packet=None,
            )

    def record_rx(self, packet: bytes, description: str, *, accepted: bool) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            if accepted:
                self._last_packet_utc = self._utc_now()
            self._append_event(
                category='rx',
                direction='RX',
                description=description,
                packet=bytes(packet),
            )

    def record_tx(self, packet: bytes, description: str) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            self._append_event(
                category='tx',
                direction='TX',
                description=description,
                packet=bytes(packet),
            )

    def record_decoded(self, field: DiagnosticField, value: float | int | bool) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            previous = self._decoded_values.get(field)
            if previous == value:
                return
            self._decoded_values[field] = value

            if field in {'board_c', 'bt_c', 'et_c', 'ir_c'}:
                setattr(self, f'_{field}', float(value))
            elif field == 'bt_ror_c':
                self._bt_ror_c = float(value)
            elif field == 'et_ror_c':
                self._et_ror_c = float(value)
            elif field == 'power':
                self._power = int(value)
            elif field == 'fan':
                self._fan = int(value)
            elif field == 'drum':
                self._drum = int(value)
            elif field == 'charge':
                self._charge_latched = bool(value)

            self._append_event(
                category='state',
                direction=None,
                description=f'{field}: {value}',
                packet=None,
            )

    def record_desired_warmup(self, enabled: bool | None, target_c: float) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            changed = False
            if self._desired_warmup != enabled:
                self._desired_warmup = enabled
                changed = True
            if self._desired_target_c != target_c:
                self._desired_target_c = target_c
                changed = True
            if changed:
                self._append_event(
                    category='state',
                    direction=None,
                    description=f'desired warm-up: {enabled}, target={target_c}',
                    packet=None,
                )

    def record_reported_warmup(self, enabled: bool | None) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            if self._reported_warmup == enabled:
                return
            self._reported_warmup = enabled
            self._append_event(
                category='state',
                direction=None,
                description=f'reported warm-up: {enabled}',
                packet=None,
            )

    def record_reported_target(self, target_c: float | None) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            if self._reported_target_c == target_c:
                return
            self._reported_target_c = target_c
            self._append_event(
                category='state',
                direction=None,
                description=f'reported target: {target_c}',
                packet=None,
            )

    def record_restoration(
        self,
        state: RestorationState,
        description: str,
        *,
        attempted: bool = False,
    ) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            self._restoration_state = state
            if attempted:
                self._last_restoration_attempt_utc = self._utc_now()

            self._append_event(
                category='restoration',
                direction=None,
                description=f'{state.value}: {description}',
                packet=None,
            )

    def record_charge_latch(self, latched: bool) -> None:
        with self._lock:
            if not self._monitoring_active:
                return
            if self._charge_latched == latched:
                return
            self._charge_latched = latched
            self._append_event(
                category='state',
                direction=None,
                description=f'CHARGE latch: {latched}',
                packet=None,
            )

    def stop(self) -> None:
        with self._lock:
            if not self._monitoring_active:
                return

            self._append_event(
                category='session',
                direction=None,
                description='monitoring stopped',
                packet=None,
            )
            self._monitoring_active = False
            self._session_ended_utc = self._utc_now()


__all__ = [
    'TransportKind',
    'DiagnosticCategory',
    'DiagnosticDirection',
    'DiagnosticField',
    'RestorationState',
    'SantokerDiagnosticEvent',
    'SantokerDiagnosticsState',
    'SantokerDiagnosticsView',
    'SantokerDiagnosticsSession',
    '_format_packet',
]
