#
# ABOUT
# Santoker warm-up controller support for Artisan
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
# Marko Luther, 2026

from _thread import RLock
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal, Protocol


MIN_WARMUP_TEMP_C: Final[float] = 100.0
MAX_WARMUP_TEMP_C: Final[float] = 300.0
DEFAULT_WARMUP_TEMP_C: Final[float] = 190.0


def _from_f_to_cstrict(temp_f: float) -> float:
    return (temp_f - 32.0) * (5.0 / 9.0)


def _from_c_to_fstrict(temp_c: float) -> float:
    return (temp_c * 9.0 / 5.0) + 32.0


class WarmupResult(Enum):
    OK = 'ok'
    NO_CONNECTION = 'no_connection'
    NOT_READY = 'not_ready'
    AFTER_CHARGE = 'after_charge'
    OUT_OF_RANGE = 'out_of_range'


class SantokerWarmupDevice(Protocol):
    def isHeaderReady(self) -> bool: ...

    def getWarmup(self) -> bool | None: ...

    def setWarmupTarget(self, temp_c: float) -> bool: ...

    def setWarmup(self, enabled: bool) -> bool: ...


@dataclass
class SantokerWarmupController:
    desired_temp_c: float = DEFAULT_WARMUP_TEMP_C
    _serialization_lock: RLock = field(
        default_factory=RLock, init=False, repr=False, compare=False
    )
    _charge_latched: bool = field(
        default=False, init=False, repr=False, compare=False
    )

    @contextmanager
    def serialized(self) -> Iterator[None]:
        with self._serialization_lock:
            yield

    def mark_charge(self) -> None:
        with self.serialized():
            self._charge_latched = True

    def reset_charge(self) -> None:
        with self.serialized():
            self._charge_latched = False

    def is_charge_latched(self) -> bool:
        with self.serialized():
            return self._charge_latched

    def set_target(
        self,
        display_temp: float,
        unit: Literal['C', 'F'],
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult:
        with self.serialized():
            temp_c = _from_f_to_cstrict(display_temp) if unit == 'F' else display_temp
            if not MIN_WARMUP_TEMP_C <= temp_c <= MAX_WARMUP_TEMP_C:
                return WarmupResult.OUT_OF_RANGE
            self.desired_temp_c = temp_c
            if device is not None and not device.setWarmupTarget(temp_c):
                return WarmupResult.OUT_OF_RANGE
            return WarmupResult.OK

    def set_enabled(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult:
        with self.serialized():
            if device is None:
                return WarmupResult.NO_CONNECTION
            if not device.isHeaderReady():
                return WarmupResult.NOT_READY
            if enabled and (self._charge_latched or charge_index > -1):
                return WarmupResult.AFTER_CHARGE
            if not enabled and device.getWarmup() is not True:
                return WarmupResult.OK
            if enabled and not device.setWarmupTarget(self.desired_temp_c):
                return WarmupResult.OUT_OF_RANGE
            if not device.setWarmup(enabled):
                return WarmupResult.NOT_READY
            return WarmupResult.OK

    def reconcile_reported_state(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> bool:
        with self.serialized():
            unsafe = enabled and (self._charge_latched or charge_index > -1)
            if unsafe and device is not None and device.isHeaderReady():
                device.setWarmup(False)
            return unsafe

    def accept_reported_target(self, temp_c: float) -> None:
        with self.serialized():
            if MIN_WARMUP_TEMP_C <= temp_c <= MAX_WARMUP_TEMP_C:
                self.desired_temp_c = temp_c

    def target_for_display(self, unit: Literal['C', 'F']) -> float:
        with self.serialized():
            return (
                _from_c_to_fstrict(self.desired_temp_c)
                if unit == 'F'
                else self.desired_temp_c
            )
