#
# ABOUT
# Santoker active-roast power reconnect support for Artisan
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# Marko Luther, 2026

from _thread import RLock
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Final, Protocol


MIN_POWER: Final[int] = 0
MAX_POWER: Final[int] = 100
POWER_TARGET: Final[bytes] = b'\xFA'


class PowerReconcileOutcome(Enum):
    NONE = 'none'
    WAITING = 'waiting'
    THROTTLED = 'throttled'
    ATTEMPTED = 'attempted'
    CONVERGED = 'converged'


class SantokerPowerDevice(Protocol):
    def isHeaderReady(self) -> bool: ...

    def getPower(self) -> int: ...

    def isPowerFresh(self) -> bool: ...

    def setPower(self, value: int) -> bool: ...


@dataclass
class SantokerPowerController:
    monotonic_clock: Callable[[], float] = field(
        default=monotonic, repr=False, compare=False
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)
    _desired_power: int | None = field(default=None, init=False, repr=False)
    _restoration_pending: bool = field(default=False, init=False, repr=False)
    _last_attempt_monotonic: float | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def desired_power(self) -> int | None:
        with self._lock:
            return self._desired_power

    def restoration_pending(self) -> bool:
        with self._lock:
            return self._restoration_pending

    def note_power_request(self, value: int, *, active_roast: bool) -> None:
        with self._lock:
            if active_roast and MIN_POWER <= value <= MAX_POWER:
                self._desired_power = value
                if self._restoration_pending:
                    self._last_attempt_monotonic = None

    def note_transport_loss(self) -> None:
        with self._lock:
            self._restoration_pending = self._desired_power is not None
            self._last_attempt_monotonic = None

    def mark_drop(self) -> None:
        with self._lock:
            self._clear()

    def start_monitoring(self) -> None:
        with self._lock:
            self._clear()

    def reset_roast(self) -> None:
        with self._lock:
            self._clear()

    def stop_monitoring(self) -> None:
        with self._lock:
            self._clear()

    def _clear(self) -> None:
        self._desired_power = None
        self._restoration_pending = False
        self._last_attempt_monotonic = None

    def reconcile_after_frame(
        self,
        charge_index: int,
        drop_index: int,
        device: SantokerPowerDevice | None,
    ) -> PowerReconcileOutcome:
        with self._lock:
            if drop_index > 0:
                self._clear()
                return PowerReconcileOutcome.NONE

            if charge_index < 0:
                self._restoration_pending = False
                self._last_attempt_monotonic = None
                return PowerReconcileOutcome.NONE

            if not self._restoration_pending or self._desired_power is None:
                return PowerReconcileOutcome.NONE

            if device is None or not device.isHeaderReady():
                return PowerReconcileOutcome.WAITING

            if self._last_attempt_monotonic is not None:
                if device.isPowerFresh() and device.getPower() == self._desired_power:
                    self._restoration_pending = False
                    self._last_attempt_monotonic = None
                    return PowerReconcileOutcome.CONVERGED

                now = self.monotonic_clock()
                if now - self._last_attempt_monotonic < 1.0:
                    return PowerReconcileOutcome.THROTTLED
            else:
                now = self.monotonic_clock()

            self._last_attempt_monotonic = now
            if device.setPower(self._desired_power):
                return PowerReconcileOutcome.ATTEMPTED
            return PowerReconcileOutcome.WAITING
