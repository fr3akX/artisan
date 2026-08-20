#
# ABOUT
# Santoker active-roast control reconnect support for Artisan
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


MIN_CONTROL_VALUE: Final[int] = 0
MAX_CONTROL_VALUE: Final[int] = 100
POWER: Final[bytes] = b'\xFA'
AIR: Final[bytes] = b'\xCA'
DRUM: Final[bytes] = b'\xC0'
CONTROL_TARGETS: Final[tuple[bytes, ...]] = (DRUM, AIR, POWER)
MECHANICAL_TARGETS: Final[tuple[bytes, ...]] = (DRUM, AIR)
RETRY_INTERVAL: Final[float] = 1.0


class ControlReconcileOutcome(Enum):
    NONE = 'none'
    WAITING = 'waiting'
    THROTTLED = 'throttled'
    ATTEMPTED = 'attempted'
    CONVERGED = 'converged'


class SantokerControlDevice(Protocol):
    def isHeaderReady(self) -> bool: ...

    def getPower(self) -> int: ...

    def isPowerFresh(self) -> bool: ...

    def getAir(self) -> int: ...

    def isAirFresh(self) -> bool: ...

    def getDrum(self) -> int: ...

    def isDrumFresh(self) -> bool: ...

    def setPower(self, value: int) -> bool: ...

    def setAir(self, value: int) -> bool: ...

    def setDrum(self, value: int) -> bool: ...


@dataclass
class SantokerControlController:
    monotonic_clock: Callable[[], float] = field(
        default=monotonic, repr=False, compare=False
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)
    _intended: dict[bytes, int] = field(default_factory=dict, init=False, repr=False)
    _pending: set[bytes] = field(default_factory=set, init=False, repr=False)
    _attempted: set[bytes] = field(default_factory=set, init=False, repr=False)
    _last_attempt_monotonic: dict[bytes, float] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _transport_lost: bool = field(default=False, init=False, repr=False)

    def intended_controls(self) -> dict[bytes, int]:
        with self._lock:
            return dict(self._intended)

    def restoration_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def mark_charge(self, device: SantokerControlDevice | None) -> None:
        with self._lock:
            self._clear()
            if device is not None:
                self._fill_missing_controls(device)

    def note_control_request(
        self, target: bytes, value: int, *, active_roast: bool
    ) -> None:
        with self._lock:
            if (
                active_roast
                and target in CONTROL_TARGETS
                and self._valid_value(value)
            ):
                self._intended[target] = value
                if self._transport_lost:
                    self._pending.add(target)
                    self._attempted.discard(target)
                    self._last_attempt_monotonic.pop(target, None)

    def note_transport_loss(
        self, *, active_roast: bool, device: SantokerControlDevice | None
    ) -> None:
        with self._lock:
            if not active_roast:
                self._cancel_recovery()
                return
            if device is not None:
                self._fill_missing_controls(device)
            self._pending = set(self._intended)
            self._attempted.clear()
            self._last_attempt_monotonic.clear()
            self._transport_lost = True

    def mark_drop(self) -> None:
        with self._lock:
            self._clear()

    def reset_roast(self) -> None:
        with self._lock:
            self._clear()

    def start_monitoring(self) -> None:
        with self._lock:
            self._clear()

    def stop_monitoring(self) -> None:
        with self._lock:
            self._clear()

    def reconcile_after_frame(
        self,
        charge_index: int,
        drop_index: int,
        device: SantokerControlDevice | None,
    ) -> ControlReconcileOutcome:
        with self._lock:
            if drop_index > 0:
                self._clear()
                return ControlReconcileOutcome.NONE

            if charge_index < 0:
                self._cancel_recovery()
                return ControlReconcileOutcome.NONE

            if not self._transport_lost or not self._pending:
                return ControlReconcileOutcome.NONE

            if device is None or not device.isHeaderReady():
                return ControlReconcileOutcome.WAITING

            self._remove_converged_targets(device)
            if not self._pending:
                self._cancel_recovery()
                return ControlReconcileOutcome.CONVERGED

            now = self.monotonic_clock()
            eligible = self._eligible_targets()
            attemptable = [
                target
                for target in eligible
                if target not in self._last_attempt_monotonic
                or now - self._last_attempt_monotonic[target] >= RETRY_INTERVAL
            ]
            if not attemptable:
                return ControlReconcileOutcome.THROTTLED
            return self._attempt_targets(device, attemptable)

    def _fill_missing_controls(self, device: SantokerControlDevice) -> None:
        for target in CONTROL_TARGETS:
            if target not in self._intended:
                value = self._get_value(device, target)
                if self._valid_value(value):
                    self._intended[target] = value

    def _remove_converged_targets(self, device: SantokerControlDevice) -> None:
        for target in CONTROL_TARGETS:
            if (
                target in self._pending
                and target in self._attempted
                and self._is_fresh(device, target)
                and self._get_value(device, target) == self._intended[target]
            ):
                self._pending.remove(target)
                self._attempted.remove(target)

    def _eligible_targets(self) -> list[bytes]:
        mechanical_pending = any(
            target in self._pending for target in MECHANICAL_TARGETS
        )
        return [
            target
            for target in CONTROL_TARGETS
            if target in self._pending and (target != POWER or not mechanical_pending)
        ]

    def _attempt_targets(
        self, device: SantokerControlDevice, targets: list[bytes]
    ) -> ControlReconcileOutcome:
        attempted = False
        for target in targets:
            self._last_attempt_monotonic[target] = self.monotonic_clock()
            if self._set_value(device, target, self._intended[target]):
                self._attempted.add(target)
                attempted = True
        if attempted:
            return ControlReconcileOutcome.ATTEMPTED
        return ControlReconcileOutcome.WAITING

    @staticmethod
    def _valid_value(value: object) -> bool:
        return (
            type(value) is int
            and MIN_CONTROL_VALUE <= value <= MAX_CONTROL_VALUE
        )

    @staticmethod
    def _get_value(device: SantokerControlDevice, target: bytes) -> int:
        if target == DRUM:
            return device.getDrum()
        if target == AIR:
            return device.getAir()
        return device.getPower()

    @staticmethod
    def _is_fresh(device: SantokerControlDevice, target: bytes) -> bool:
        if target == DRUM:
            return device.isDrumFresh()
        if target == AIR:
            return device.isAirFresh()
        return device.isPowerFresh()

    @staticmethod
    def _set_value(
        device: SantokerControlDevice, target: bytes, value: int
    ) -> bool:
        if target == DRUM:
            return device.setDrum(value)
        if target == AIR:
            return device.setAir(value)
        return device.setPower(value)

    def _cancel_recovery(self) -> None:
        self._pending.clear()
        self._attempted.clear()
        self._last_attempt_monotonic.clear()
        self._transport_lost = False

    def _clear(self) -> None:
        self._intended.clear()
        self._cancel_recovery()
