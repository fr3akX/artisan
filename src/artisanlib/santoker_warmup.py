#
# ABOUT
# Santoker warm-up controller support for Artisan
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the
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
# Marko Luther, 2026

from _thread import RLock
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import logging
from time import monotonic
from typing import Final, Literal, Protocol

from artisanlib.santoker_diagnostics import RestorationState, SantokerDiagnosticsSession


_LOG: Final[logging.Logger] = logging.getLogger(__name__)


MIN_WARMUP_TEMP_C: Final[float] = 100.0
MAX_WARMUP_TEMP_C: Final[float] = 300.0
DEFAULT_WARMUP_TEMP_C: Final[float] = 190.0


def _from_f_to_cstrict(temp_f: float) -> float:
    return (temp_f - 32.0) * (5.0 / 9.0)


def _from_c_to_fstrict(temp_c: float) -> float:
    return (temp_c * 9.0 / 5.0) + 32.0


def _is_valid_temp(temp_c: float) -> bool:
    return MIN_WARMUP_TEMP_C <= temp_c <= MAX_WARMUP_TEMP_C


class WarmupResult(Enum):
    OK = 'ok'
    NO_CONNECTION = 'no_connection'
    NOT_READY = 'not_ready'
    AFTER_CHARGE = 'after_charge'
    OUT_OF_RANGE = 'out_of_range'


class ReconcileOutcome(Enum):
    NONE = 'none'
    WAITING = 'waiting'
    THROTTLED = 'throttled'
    ATTEMPTED = 'attempted'
    CONVERGED = 'converged'
    FORCED_OFF = 'forced_off'


class SantokerWarmupDevice(Protocol):
    def isHeaderReady(self) -> bool: ...

    def getWarmup(self) -> bool | None: ...

    def getReportedWarmupTarget(self) -> float | None: ...

    def setWarmupTarget(self, temp_c: float) -> bool: ...

    def setWarmup(self, enabled: bool) -> bool: ...

    def requestWarmupOn(self, temp_c: float) -> bool: ...


@dataclass
class SantokerWarmupController:
    desired_temp_c: float = DEFAULT_WARMUP_TEMP_C
    _serialization_lock: RLock = field(
        default_factory=RLock, init=False, repr=False, compare=False
    )
    _charge_latched: bool = field(default=False, init=False, repr=False, compare=False)
    _desired_enabled: bool | None = field(default=None, init=False, repr=False, compare=False)
    _reported_enabled: bool | None = field(default=None, init=False, repr=False, compare=False)
    _reported_target_c: float | None = field(default=None, init=False, repr=False, compare=False)
    _restoration_state: RestorationState = field(default=RestorationState.IDLE, init=False)
    _last_attempt_monotonic: float | None = field(default=None, init=False, repr=False, compare=False)
    _safety_off_pending: bool = field(default=False, init=False, repr=False, compare=False)
    monotonic_clock: Callable[[], float] = field(default=monotonic, repr=False, compare=False)
    _diagnostics: SantokerDiagnosticsSession | None = field(default=None, init=False, repr=False)

    @contextmanager
    def serialized(self) -> Iterator[None]:
        with self._serialization_lock:
            yield

    def _safe_record(self, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            _LOG.exception('SantokerWarmup diagnostics recorder failed')

    def _record_desired_state(self) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(
            lambda: diagnostics.record_desired_warmup(
                self._desired_enabled,
                self.desired_temp_c,
            ),
        )

    def _record_reported_state(self) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_reported_warmup(self._reported_enabled))
        self._safe_record(
            lambda: diagnostics.record_reported_target(self._reported_target_c),
        )

    def _record_restoration_state(self, state: RestorationState, *, attempted: bool = False) -> None:
        self._restoration_state = state
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(
            lambda: diagnostics.record_restoration(
                state,
                state.value,
                attempted=attempted,
            ),
        )

    def _record_charge_latch(self) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            return
        self._safe_record(lambda: diagnostics.record_charge_latch(self._charge_latched))

    def _set_reported_target(self, temp_c: float | None) -> None:
        if temp_c is None or not _is_valid_temp(temp_c):
            self._reported_target_c = None
        else:
            self._reported_target_c = temp_c
        self._record_reported_state()

    def attach_diagnostics(self, diagnostics: SantokerDiagnosticsSession | None) -> None:
        with self.serialized():
            self._diagnostics = diagnostics
            self._record_desired_state()
            self._record_reported_state()
            self._record_charge_latch()
            self._record_restoration_state(self._restoration_state)

    def is_charge_latched(self) -> bool:
        with self.serialized():
            return self._charge_latched

    def desired_enabled(self) -> bool | None:
        with self.serialized():
            return self._desired_enabled

    def reported_target(self) -> float | None:
        with self.serialized():
            return self._reported_target_c

    def restoration_state(self) -> RestorationState:
        with self.serialized():
            return self._restoration_state

    def set_target(
        self,
        display_temp: float,
        unit: Literal['C', 'F'],
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult:
        with self.serialized():
            temp_c = _from_f_to_cstrict(display_temp) if unit == 'F' else display_temp
            if not _is_valid_temp(temp_c):
                return WarmupResult.OUT_OF_RANGE
            self.desired_temp_c = temp_c
            if device is not None and not device.setWarmupTarget(temp_c):
                return WarmupResult.OUT_OF_RANGE
            self._record_desired_state()
            return WarmupResult.OK

    def set_enabled(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult:
        with self.serialized():
            if enabled:
                if device is None:
                    return WarmupResult.NO_CONNECTION
                if not device.isHeaderReady():
                    return WarmupResult.NOT_READY
                if self._charge_latched or charge_index > -1:
                    return WarmupResult.AFTER_CHARGE
                if not device.requestWarmupOn(self.desired_temp_c):
                    return WarmupResult.OUT_OF_RANGE
                self._desired_enabled = True
                self._record_desired_state()
                self._record_restoration_state(RestorationState.IDLE)
                return WarmupResult.OK

            requires_off = (
                self._desired_enabled is True
                or self._reported_enabled is True
                or self._safety_off_pending
            )
            if not requires_off and device is not None:
                requires_off = device.getWarmup() is True
            self._desired_enabled = False
            self._record_desired_state()
            if (
                device is not None
                and device.isHeaderReady()
                and requires_off
                and device.setWarmup(False)
            ):
                self._safety_off_pending = False
            self._last_attempt_monotonic = None
            self._record_restoration_state(RestorationState.IDLE)
            return WarmupResult.OK

    def mark_charge(self) -> None:
        with self.serialized():
            if self._desired_enabled is True or self._reported_enabled is True:
                self._safety_off_pending = True
            self._desired_enabled = False
            self._charge_latched = True
            self._record_charge_latch()
            self._record_desired_state()
            self._record_restoration_state(RestorationState.BLOCKED_BY_CHARGE)

    def reset_charge(self) -> None:
        with self.serialized():
            self._charge_latched = False
            self._record_charge_latch()

    def note_transport_loss(self) -> None:
        with self.serialized():
            self._reported_enabled = None
            self._reported_target_c = None
            self._last_attempt_monotonic = None
            self._record_reported_state()
            self._record_restoration_state(RestorationState.WAITING_FOR_DATA)

    def stop_monitoring(self, device: SantokerWarmupDevice | None) -> None:
        with self.serialized():
            requires_off = (
                self._desired_enabled is True
                or self._reported_enabled is True
            )
            if device is not None and device.isHeaderReady() and requires_off:
                device.setWarmup(False)
            self._desired_enabled = None
            self._reported_enabled = None
            self._reported_target_c = None
            self._last_attempt_monotonic = None
            self._safety_off_pending = False
            self._record_desired_state()
            self._record_reported_state()
            self._record_restoration_state(RestorationState.IDLE)

    def reconcile_after_frame(
        self,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> ReconcileOutcome:
        with self.serialized():
            if self._charge_latched or charge_index > -1:
                if device is not None and device.isHeaderReady():
                    self._reported_enabled = device.getWarmup()
                self._record_reported_state()

                reconcile_outcome = ReconcileOutcome.NONE
                if self._safety_off_pending or self._reported_enabled is True:
                    if device is not None and device.isHeaderReady():
                        if device.setWarmup(False):
                            self._safety_off_pending = False
                            reconcile_outcome = ReconcileOutcome.FORCED_OFF
                            self._record_restoration_state(RestorationState.BLOCKED_BY_CHARGE)
                        else:
                            self._record_restoration_state(
                                RestorationState.WAITING_FOR_DATA,
                            )
                            reconcile_outcome = ReconcileOutcome.WAITING
                    else:
                        self._record_restoration_state(
                            RestorationState.WAITING_FOR_DATA,
                        )
                        reconcile_outcome = ReconcileOutcome.WAITING
                else:
                    self._record_restoration_state(RestorationState.BLOCKED_BY_CHARGE)
                self._desired_enabled = False
                self._record_desired_state()
                return reconcile_outcome

            if self._desired_enabled is not True:
                self._record_restoration_state(RestorationState.IDLE)
                return ReconcileOutcome.NONE

            if device is None or not device.isHeaderReady():
                self._record_restoration_state(RestorationState.WAITING_FOR_DATA)
                return ReconcileOutcome.WAITING

            self._reported_enabled = device.getWarmup()
            self._set_reported_target(device.getReportedWarmupTarget())

            if (
                self._reported_enabled is True
                and self._reported_target_c == self.desired_temp_c
            ):
                self._record_restoration_state(RestorationState.CONVERGED)
                return ReconcileOutcome.CONVERGED

            now = self.monotonic_clock()
            if self._last_attempt_monotonic is not None and (
                now - self._last_attempt_monotonic < 1.0
            ):
                self._record_restoration_state(RestorationState.PENDING)
                return ReconcileOutcome.THROTTLED

            self._last_attempt_monotonic = now
            requested = device.requestWarmupOn(self.desired_temp_c)
            if requested:
                self._record_restoration_state(
                    RestorationState.PENDING,
                    attempted=True,
                )
                return ReconcileOutcome.ATTEMPTED
            self._record_restoration_state(RestorationState.WAITING_FOR_DATA)
            return ReconcileOutcome.WAITING

    def reconcile_reported_state(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> bool:
        with self.serialized():
            self._reported_enabled = enabled
            self._record_reported_state()
            return (
                self.reconcile_after_frame(charge_index, device)
                is ReconcileOutcome.FORCED_OFF
            )

    def accept_reported_target(self, temp_c: float) -> None:
        with self.serialized():
            self._set_reported_target(temp_c)

    def target_for_display(self, unit: Literal['C', 'F']) -> float:
        with self.serialized():
            return (
                _from_c_to_fstrict(self.desired_temp_c)
                if unit == 'F'
                else self.desired_temp_c
            )
