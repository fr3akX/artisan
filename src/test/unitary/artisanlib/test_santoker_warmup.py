import csv
import os
from collections.abc import Iterator
from configparser import ConfigParser
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast, override
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QSlider

from artisanlib.main import ApplicationWindow, EventActionThread
from artisanlib.santoker_controls import (
    AIR,
    DRUM,
    HEATING_ON,
    MACHINE_ON,
    POWER,
    SantokerControlController,
)
from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult
from artisanlib.santoker_warmup_ui import SantokerWarmupControls

INVENTORY_SELECTION = (
    'https://inventory.example.test',
    '11111111111141118111111111111111',
    '22222222222242228222222222222222',
    'Fixture lot',
)
INVENTORY_ROAST_UUID = '33333333333343338333333333333333'


def parse_ini_array(value: str) -> list[str]:
    return next(csv.reader([value], skipinitialspace=True))


@pytest.fixture(scope='module')
def qapplication() -> QApplication:
    app = QApplication.instance()
    if app is None:
        return QApplication([])
    return cast(QApplication, app)


@dataclass
class FakeWarmupDevice:
    ready: bool = True
    warmup: bool | None = False
    reported_target: float | None = None
    calls: list[tuple[str, object]] = field(default_factory=list)

    def isHeaderReady(self) -> bool:
        return self.ready

    def getWarmup(self) -> bool | None:
        return self.warmup

    def getReportedWarmupTarget(self) -> float | None:
        return self.reported_target

    def setWarmupTarget(self, temp_c: float) -> bool:
        self.calls.append(('target', temp_c))
        return 100.0 <= temp_c <= 300.0

    def setWarmup(self, enabled: bool) -> bool:
        self.calls.append(('enabled', enabled))
        self.warmup = enabled
        return self.ready

    def requestWarmupOn(self, temp_c: float) -> bool:
        if not self.ready:
            return False
        if not self.setWarmupTarget(temp_c):
            return False
        return self.setWarmup(True)

@dataclass
class ParserWarmupDevice(FakeWarmupDevice):
    machine_on: int = 1
    heating_on: int = 1
    power: int = 70
    air: int = 80
    drum: int = 30
    machine_on_fresh: bool = True
    heating_on_fresh: bool = True
    power_fresh: bool = True
    air_fresh: bool = True
    drum_fresh: bool = True

    def getMachineOn(self) -> int:
        return self.machine_on

    def isMachineOnFresh(self) -> bool:
        return self.machine_on_fresh

    def getHeatingOn(self) -> int:
        return self.heating_on

    def isHeatingOnFresh(self) -> bool:
        return self.heating_on_fresh

    def getPower(self) -> int:
        return self.power

    def isPowerFresh(self) -> bool:
        return self.power_fresh

    def getAir(self) -> int:
        return self.air

    def isAirFresh(self) -> bool:
        return self.air_fresh

    def getDrum(self) -> int:
        return self.drum

    def isDrumFresh(self) -> bool:
        return self.drum_fresh

    def setMachineOn(self, value: int) -> bool:
        self.machine_on_fresh = False
        self.calls.append(('raw', (MACHINE_ON, value)))
        return self.ready

    def setHeatingOn(self, value: int) -> bool:
        self.heating_on_fresh = False
        self.calls.append(('raw', (HEATING_ON, value)))
        return self.ready

    def setPower(self, value: int) -> bool:
        self.power_fresh = False
        self.calls.append(('raw', (POWER, value)))
        return self.ready

    def setAir(self, value: int) -> bool:
        self.air_fresh = False
        self.calls.append(('raw', (AIR, value)))
        return self.ready

    def setDrum(self, value: int) -> bool:
        self.drum_fresh = False
        self.calls.append(('raw', (DRUM, value)))
        return self.ready

    def send_msg(self, target: bytes, value: int) -> None:
        self.calls.append(('raw', (target, value)))

    def report(self, target: bytes, value: int) -> None:
        if target == MACHINE_ON:
            self.machine_on = value
            self.machine_on_fresh = True
        elif target == HEATING_ON:
            self.heating_on = value
            self.heating_on_fresh = True
        elif target == POWER:
            self.power = value
            self.power_fresh = True
        elif target == AIR:
            self.air = value
            self.air_fresh = True
        elif target == DRUM:
            self.drum = value
            self.drum_fresh = True


def test_controller_converts_fahrenheit_and_updates_device() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController()

    result = controller.set_target(374.0, 'F', device)

    assert result is WarmupResult.OK
    assert controller.desired_temp_c == pytest.approx(190.0)
    assert device.calls == [('target', pytest.approx(190.0))]
    assert controller.target_for_display('F') == pytest.approx(374.0)


def test_fahrenheit_target_is_canonical_and_converges_without_resend() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController

    device = FakeWarmupDevice(ready=True, warmup=True, reported_target=190.6)
    controller = SantokerWarmupController()

    assert controller.set_target(375.0, 'F', None) is WarmupResult.OK
    assert controller.desired_temp_c == 190.6
    assert controller.target_for_display('F') == pytest.approx(375.08)
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()

    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.CONVERGED
    assert device.calls == []


@pytest.mark.parametrize(('value', 'unit'), [(99.0, 'C'), (573.0, 'F')])
def test_controller_rejects_out_of_range_target(value: float, unit: str) -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController()

    result = controller.set_target(value, unit, device)  # type: ignore[arg-type]

    assert result is WarmupResult.OUT_OF_RANGE
    assert controller.desired_temp_c == 190.0
    assert device.calls == []


@pytest.mark.parametrize('temp_c', [100.0, 300.0])
def test_controller_accepts_inclusive_target_boundaries(temp_c: float) -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController()

    assert controller.set_target(temp_c, 'C', device) is WarmupResult.OK
    assert controller.desired_temp_c == temp_c
    assert device.calls == [('target', temp_c)]


def test_controller_stores_target_without_connection() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    controller = SantokerWarmupController()

    assert controller.set_target(195.0, 'C', None) is WarmupResult.OK
    assert controller.desired_temp_c == 195.0


@pytest.mark.parametrize(
    ('device', 'charge_index', 'expected'),
    [
        (None, -1, 'no_connection'),
        (FakeWarmupDevice(ready=False), -1, 'not_ready'),
        (FakeWarmupDevice(), 10, 'after_charge'),
    ],
)
def test_controller_rejects_unsafe_start(
    device: FakeWarmupDevice | None,
    charge_index: int,
    expected: str,
) -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    controller = SantokerWarmupController()

    assert controller.set_enabled(True, charge_index, device).value == expected
    assert device is None or device.calls == []


def test_charge_latch_blocks_on_until_reset() -> None:
    from artisanlib.santoker_warmup import WarmupResult

    controller = SantokerWarmupController()
    device = FakeWarmupDevice()

    controller.mark_charge()
    assert controller.is_charge_latched()
    assert controller.set_enabled(True, -1, device) is WarmupResult.AFTER_CHARGE

    controller.reset_charge()
    assert not controller.is_charge_latched()
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK


def test_real_santoker_active_target_edit_sends_only_target_and_keeps_diagnostics_on() -> None:
    from artisanlib.santoker import Santoker
    from artisanlib.santoker_diagnostics import SantokerDiagnosticsSession

    diagnostics = SantokerDiagnosticsSession('Wi-Fi')
    device = Santoker(diagnostics=diagnostics)
    device._header_ready = True
    controller = SantokerWarmupController()
    controller.attach_diagnostics(diagnostics)

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert controller.set_enabled(True, -1, device) is WarmupResult.OK
        device.register_reading(Santoker.WARMUP, b'\x00\x00\x01')
        send_msg.reset_mock()
        assert controller.set_target(205.0, 'C', device) is WarmupResult.OK

    send_msg.assert_called_once_with(Santoker.WARMUP_TEMP, 2050)
    state = diagnostics.view().state
    assert state.desired_warmup is True
    assert state.desired_target_c == 205.0


def test_controller_starts_with_desired_target() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController(desired_temp_c=190.0)

    result = controller.set_enabled(True, -1, device)

    assert result is WarmupResult.OK
    assert device.calls == [('target', 190.0), ('enabled', True)]


def test_controller_does_not_send_redundant_off_while_inactive() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(warmup=False)
    controller = SantokerWarmupController()

    assert controller.set_enabled(False, 10, device) is WarmupResult.OK
    assert device.calls == []


def test_accept_reported_target_only_updates_reported() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    controller = SantokerWarmupController(desired_temp_c=205.0)

    controller.accept_reported_target(190.0)
    controller.accept_reported_target(225.5)
    controller.accept_reported_target(99.0)
    controller.accept_reported_target(None)

    assert controller.desired_temp_c == 205.0
    assert controller.reported_target() is None


def test_reconnect_restores_target_then_on_after_valid_frame() -> None:
    from artisanlib.santoker_warmup import (
        ReconcileOutcome,
        RestorationState,
        SantokerWarmupController,
        WarmupResult,
    )

    now = [10.0]
    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=190.0)
    controller = SantokerWarmupController(
        desired_temp_c=205.0,
        monotonic_clock=lambda: now[0],
    )

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()

    device.ready = False
    controller.note_transport_loss()
    assert controller.desired_enabled() is True
    assert controller.restoration_state() is RestorationState.WAITING_FOR_DATA
    assert device.calls == []

    device.ready = True
    device.warmup = False
    device.reported_target = 190.0
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert device.calls == [('target', 205.0), ('enabled', True)]


def test_reconnect_readiness_notification_without_reconcile_sends_nothing() -> None:
    from artisanlib.santoker_warmup import RestorationState, SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(ready=True, warmup=True)
    controller = SantokerWarmupController()

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()

    device.ready = False
    controller.note_transport_loss()
    device.ready = True

    assert controller.desired_enabled() is True
    assert controller.restoration_state() is RestorationState.WAITING_FOR_DATA
    assert device.calls == []


def test_reconcile_after_frame_attempts_on_unknown_reports() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=None)
    controller = SantokerWarmupController(
        desired_temp_c=205.0,
    )

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert device.calls == [('target', 205.0), ('enabled', True)]


def test_reconcile_after_frame_throttles_duplicate_frames() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController, WarmupResult

    calls = [10.0]
    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=190.0)
    controller = SantokerWarmupController(
        desired_temp_c=205.0,
        monotonic_clock=lambda: calls[0],
    )

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()

    calls[0] = 10.0
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert device.calls == [('target', 205.0), ('enabled', True)]
    device.calls.clear()

    calls[0] = 10.2
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.THROTTLED
    assert device.calls == []

    calls[0] = 11.0
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert device.calls == [('target', 205.0), ('enabled', True)]


def test_reconcile_after_frame_matching_target_and_report_converges() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(ready=True, warmup=True, reported_target=190.0)
    controller = SantokerWarmupController(desired_temp_c=190.0)

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.CONVERGED
    assert device.calls == []


@pytest.mark.parametrize('reported_target', [None, 99.0, 300.1])
def test_reconcile_after_frame_known_target_regresses_to_attempt_on_unknown_value(
    reported_target: float | None,
) -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(ready=True, warmup=True, reported_target=190.0)
    controller = SantokerWarmupController(desired_temp_c=190.0)

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()

    device.reported_target = reported_target
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert controller.reported_target() is None
    assert device.calls == [('target', 190.0), ('enabled', True)]


def test_reconcile_after_frame_no_blind_timer_activity() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    monotonic_calls: list[float] = []

    def monotonic_clock() -> float:
        monotonic_calls.append(1.0)
        return 10.0

    device = FakeWarmupDevice(ready=True)
    controller = SantokerWarmupController(monotonic_clock=monotonic_clock)

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()
    controller.note_transport_loss()
    assert monotonic_calls == []


def test_off_without_device_keeps_state() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    controller = SantokerWarmupController()

    assert controller.set_enabled(False, -1, None) is WarmupResult.OK
    assert controller.desired_enabled() is False


def test_off_unready_cancels_waiting_restoration() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(ready=True)
    controller = SantokerWarmupController()

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    controller.note_transport_loss()
    device.ready = False

    assert controller.set_enabled(False, -1, device) is WarmupResult.OK
    assert controller.desired_enabled() is False
    assert device.calls == [('target', 190.0), ('enabled', True)]


def test_mark_charge_records_safety_off_requirement() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController

    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=190.0)
    controller = SantokerWarmupController(desired_temp_c=205.0)

    assert controller.set_enabled(True, -1, device) is not None
    device.calls.clear()
    controller.mark_charge()

    assert controller.desired_enabled() is False
    assert controller.restoration_state().value == 'blocked by CHARGE'
    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.FORCED_OFF
    assert device.calls == [('enabled', False)]


def test_mark_charge_failed_race_retains_safety_off() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController

    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=190.0)
    controller = SantokerWarmupController(desired_temp_c=205.0)

    assert controller.set_enabled(True, -1, device) is not None
    controller.mark_charge()
    device.ready = False
    assert controller.set_enabled(False, -1, device) is not None

    device.ready = True
    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.FORCED_OFF


def test_charge_safety_off_is_immediate_then_throttled_until_reported_off() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController

    now = [10.0]
    device = FakeWarmupDevice(ready=True, warmup=True, reported_target=190.0)
    controller = SantokerWarmupController(monotonic_clock=lambda: now[0])
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()
    controller.mark_charge()

    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.FORCED_OFF
    assert device.calls == [('enabled', False)]

    device.warmup = True
    now[0] = 10.2
    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.THROTTLED
    assert device.calls == [('enabled', False)]

    now[0] = 11.0
    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.FORCED_OFF
    assert device.calls == [('enabled', False), ('enabled', False)]


def test_mark_charge_retries_failed_off_until_success() -> None:
    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController

    class OneShotOffRejectingDevice(FakeWarmupDevice):
        reject_off_once: bool = True

        @override
        def setWarmup(self, enabled: bool) -> bool:  # type: ignore[override]
            self.calls.append(('enabled', enabled))
            if enabled is False and self.reject_off_once:
                self.reject_off_once = False
                self.ready = False
                return False
            self.warmup = enabled
            return self.ready

    device = OneShotOffRejectingDevice(ready=True, warmup=True, reported_target=190.0)
    controller = SantokerWarmupController(desired_temp_c=205.0)

    assert controller.set_enabled(True, -1, device) is not None
    device.calls.clear()
    controller.mark_charge()

    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.WAITING
    assert device.calls == [('enabled', False)]

    device.ready = True
    assert controller.reconcile_after_frame(0, device) is ReconcileOutcome.FORCED_OFF
    assert device.calls == [('enabled', False), ('enabled', False)]


def test_reset_charge_only_clears_charge_latch_not_intent() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    device = FakeWarmupDevice(ready=True)
    controller = SantokerWarmupController()

    controller.set_enabled(True, -1, device)
    controller.mark_charge()
    controller.reset_charge()

    assert not controller.is_charge_latched()
    assert controller.desired_enabled() is False


def test_stop_monitoring_retries_pending_safety_off_after_readiness_recovers() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    device = FakeWarmupDevice(ready=True, warmup=True)
    controller = SantokerWarmupController()
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    controller.mark_charge()
    device.ready = False
    assert controller.set_enabled(False, 0, device) is WarmupResult.OK
    device.calls.clear()

    device.ready = True
    device.warmup = False
    controller.stop_monitoring(device)

    assert device.calls == [('enabled', False)]


def test_stop_monitoring_clears_state_after_safe_off() -> None:
    from artisanlib.santoker_warmup import RestorationState, SantokerWarmupController

    class FailingWarmupDevice(FakeWarmupDevice):
        @override
        def setWarmup(self, enabled: bool) -> bool:  # type: ignore[override]
            self.calls.append(('enabled', enabled))
            self.warmup = enabled
            return bool(enabled)

    device = FailingWarmupDevice(ready=True, warmup=True)
    controller = SantokerWarmupController()

    assert controller.set_enabled(True, -1, device) is not None
    controller.stop_monitoring(device)

    assert controller.desired_enabled() is None
    assert controller.restoration_state() is RestorationState.IDLE
    assert ('enabled', False) in device.calls


def test_diagnostics_failures_do_not_change_command_results_or_state() -> None:
    from unittest.mock import Mock

    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController, WarmupResult

    class FailingDiagnostics:
        def __init__(self) -> None:
            for method in (
                'record_desired_warmup',
                'record_reported_warmup',
                'record_reported_target',
                'record_restoration',
                'record_charge_latch',
            ):
                setattr(self, method, Mock(side_effect=RuntimeError('recorder failed')))

    device = FakeWarmupDevice(ready=True, reported_target=99.0)
    controller = SantokerWarmupController()
    controller.attach_diagnostics(FailingDiagnostics())

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    assert controller.desired_enabled() is True
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert controller.reported_target() is None


def test_diagnostics_records_cleared_reported_target() -> None:
    from unittest.mock import Mock

    from artisanlib.santoker_warmup import ReconcileOutcome, SantokerWarmupController, WarmupResult

    diagnostics = Mock()
    diagnostics.record_desired_warmup = Mock()
    diagnostics.record_reported_warmup = Mock()
    diagnostics.record_reported_target = Mock()
    diagnostics.record_restoration = Mock()
    diagnostics.record_charge_latch = Mock()

    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=99.0)
    controller = SantokerWarmupController()
    controller.attach_diagnostics(diagnostics)

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    diagnostics.record_reported_target.assert_called_with(None)
    assert controller.reported_target() is None

    # no command side effect from diagnostics failure checks
    assert diagnostics.record_desired_warmup.call_count >= 1

def test_post_charge_on_report_is_forced_off() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    device = FakeWarmupDevice(warmup=True)
    controller = SantokerWarmupController()

    assert controller.reconcile_reported_state(True, 10, device)
    assert device.calls == [('enabled', False)]


def test_charge_latch_forces_late_on_report_off_after_undo() -> None:
    device = FakeWarmupDevice(warmup=True)
    controller = SantokerWarmupController()
    controller.mark_charge()

    assert controller.reconcile_reported_state(True, -1, device)
    assert device.calls == [('enabled', False)]


class ImmediateBoolSignal:
    def __init__(self, slot: object) -> None:
        self.slot = slot
        self.emissions: list[bool] = []

    def emit(self, enabled: bool) -> None:
        self.emissions.append(enabled)
        self.slot(enabled)  # type: ignore[operator]


class ImmediateSignal:
    def __init__(self, slot: object) -> None:
        self.slot = slot
        self.emissions = 0

    def emit(self) -> None:
        self.emissions += 1
        self.slot()  # type: ignore[operator]


def compact_window(
    controls: SantokerWarmupControls,
    controller: SantokerWarmupController,
    device: FakeWarmupDevice | None,
    *,
    unit: str = 'C',
    charge_index: int = -1,
    capability: bool = True,
    monitoring: bool = True,
    recording: bool = False,
) -> SimpleNamespace:
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(
            mode_tempsliders=unit,
            timeindex=[charge_index],
            flagon=monitoring,
            flagstart=recording,
        ),
        santokerWarmup=capability,
        santoker=device,
        santokerWarmupController=controller,
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': 'off-style', 'ON': 'on-style'},
        extraeventsactionstrings=[],
        buttonStates=[],
        setExtraEventButtonStyleSignal=Mock(),
        reportSantokerWarmupResult=Mock(),
        prepareRoastServerInventoryCharge=Mock(return_value=object()),
        commitRoastServerInventoryCharge=Mock(return_value=''),
        sendmessage=Mock(),
    )
    window.santokerWarmupButtonStateSignal = ImmediateBoolSignal(
        lambda enabled: ApplicationWindow.setSantokerWarmupButtonState(
            cast(ApplicationWindow, window), enabled
        )
    )
    window.santokerWarmupControlsRefreshSignal = ImmediateSignal(
        lambda: ApplicationWindow.refreshSantokerWarmupControls(
            cast(ApplicationWindow, window)
        )
    )
    return window


@dataclass
class TrackingSemaphore:
    trace: list[str]
    locked: bool = False

    def acquire(self, _count: int) -> None:
        assert not self.locked
        self.locked = True
        self.trace.append('semaphore-acquired')

    def available(self) -> int:
        return int(not self.locked)

    def release(self, _count: int) -> None:
        assert self.locked
        self.locked = False
        self.trace.append('semaphore-released')


class ResetTrackingController(SantokerWarmupController):
    def __init__(self, semaphore: TrackingSemaphore, trace: list[str]) -> None:
        super().__init__()
        self.semaphore = semaphore
        self.trace = trace

    @override
    def reset_charge(self) -> None:
        assert not self.semaphore.locked
        self.trace.append('reset-charge')
        super().reset_charge()


def successful_reset_canvas(
    controls: SantokerWarmupControls,
    controller: ResetTrackingController,
    device: FakeWarmupDevice | None,
    semaphore: TrackingSemaphore,
    trace: list[str],
) -> SimpleNamespace:
    window = compact_window(controls, controller, device)
    window.santokerControlController = Mock()
    window.pushbuttonstyles['STOP'] = 'stop-style'
    window.centralWidget = Mock(return_value=None)
    window.restoreExtraDeviceSettingsBackup = Mock()
    window.soundpopSignal = Mock()
    window.simulator = None
    window.AUClcd = Mock()
    for name in (
        'buttonFCs',
        'buttonFCe',
        'buttonSCs',
        'buttonSCe',
        'buttonRESET',
        'buttonCHARGE',
        'buttonDROP',
        'buttonDRY',
        'buttonCOOL',
        'buttonONOFF',
        'buttonSTARTSTOP',
    ):
        setattr(window, name, Mock())
    window.pidcontrol = SimpleNamespace(pidActive=True)
    window.fujipid = SimpleNamespace(sv=0)
    window.resetBBPMetrics = Mock()
    window.eNumberSpinBox = Mock()
    window.lineEvent = Mock()
    window.etypeComboBox = Mock()
    window.valueEdit = Mock()
    window.resetKeyboardButtonMarks = Mock()
    window.setTimerColorSignal = Mock()
    window.ntb = Mock()
    window.lastbuttonpressed = -1
    window.updateWindowTitle = Mock()
    window.hideDefaultButtons = Mock()
    window.enableEditMenus = Mock()
    window.updatePhasesLCDs = Mock()
    window.updateAUCLCD = Mock()
    window.updatePlusStatus = Mock()
    window.announce_current_ui_mode = Mock()
    window.autoAdjustAxis = Mock()
    window.releaseRoastServerInventory = Mock(return_value=True)

    canvas = SimpleNamespace(
        aw=window,
        checkSaved=Mock(return_value=True),
        flagOpenCompleted=False,
        designerflag=False,
        profileDataSemaphore=semaphore,
        resetTimer=Mock(),
        batchprefix='',
        roastpropertiesflag=False,
        flagKeepON=False,
        weight=(0.0, 0.0, 'g'),
        volume=(0.0, 0.0, 'l'),
        roastServerInventoryOrigin=INVENTORY_SELECTION[0],
        roastServerInventoryOrganizationUUID=INVENTORY_SELECTION[1],
        roastServerBeanLotUUID=INVENTORY_SELECTION[2],
        roastServerBeanLotName=INVENTORY_SELECTION[3],
        roastUUID=INVENTORY_ROAST_UUID,
        density_roasted=(0.0, 0.0, 1, 0.0),
        timex=[],
        timeindex=[0],
        mode_tempsliders='C',
        flagon=True,
        flagstart=False,
        meterreads_default=[],
        crossmarker=False,
        disconnect_designer=Mock(),
        canvas=Mock(),
        analyzer_connect_id=None,
        flavorlabels=[],
        deleteAnnoPositions=Mock(),
        alarmflag=[],
        backgroundprofile=None,
        backgroundprofile_moved_x=0,
        backgroundprofile_moved_y=0,
        autotimex=False,
        background=False,
        locktimex=False,
        locktimex_start=0.0,
        locktimex_end=1200.0,
        chargemintime=-120.0,
        resetmaxtime=1200.0,
        endofx=1200.0,
        redraw=Mock(),
        adderror=Mock(),
        timealign=Mock(),
    )

    def clear_measurements() -> None:
        assert not semaphore.locked
        trace.append('measurements-cleared')
        canvas.timeindex[0] = -1

    def update_warmup_controls() -> None:
        assert not semaphore.locked
        assert not controller.is_charge_latched()
        trace.append('controls-refreshed')
        ApplicationWindow.updateSantokerWarmupControls(
            cast(ApplicationWindow, window)
        )

    canvas.clearMeasurements = clear_measurements
    window.qmc = canvas
    window.updateSantokerWarmupControls = update_warmup_controls
    return canvas


def test_reported_target_updates_spinbox_without_command(
    qapplication: QApplication,
) -> None:
    del qapplication
    from unittest.mock import Mock

    controls = SantokerWarmupControls()
    device = FakeWarmupDevice()
    controller = SantokerWarmupController()
    changed = Mock()
    controls.targetChanged.connect(changed)
    window = compact_window(controls, controller, device, unit='C')

    ApplicationWindow.santokerWarmupTargetChanged(
        cast(ApplicationWindow, window), 205.0
    )

    assert controls.target.value() == 190.0
    assert controller.desired_temp_c == 190.0
    assert controller.reported_target() == 205.0
    changed.assert_not_called()
    assert device.calls == []


@pytest.mark.parametrize(
    ('ready', 'charge_index', 'enabled'),
    [(False, -1, False), (True, -1, True), (True, 0, False)],
    ids=['not-ready', 'ready-before-charge', 'ready-after-charge'],
)
def test_compact_readiness_enables_only_before_charge(
    qapplication: QApplication,
    ready: bool,
    charge_index: int,
    enabled: bool,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = compact_window(
        controls,
        SantokerWarmupController(),
        FakeWarmupDevice(ready=ready, warmup=False),
        charge_index=charge_index,
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.button.isEnabled() is enabled
    assert not controls.button.isChecked()


def test_compact_button_click_sends_target_before_enable(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    device = FakeWarmupDevice(ready=True, warmup=False)
    window = compact_window(
        controls,
        SantokerWarmupController(desired_temp_c=205.0),
        device,
    )
    controls.enabledChanged.connect(
        lambda enabled: ApplicationWindow.setSantokerWarmup(
            cast(ApplicationWindow, window), enabled
        )
    )

    controls.button.click()

    assert device.calls == [('target', 205.0), ('enabled', True)]
    assert controls.button.isChecked()
    assert window.santokerWarmupButtonStateSignal.emissions == [True]


def test_rejected_compact_button_click_restores_check_state(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    device = FakeWarmupDevice(ready=False, warmup=False)
    window = compact_window(controls, SantokerWarmupController(), device)
    controls.enabledChanged.connect(
        lambda enabled: ApplicationWindow.setSantokerWarmup(
            cast(ApplicationWindow, window), enabled
        )
    )

    controls.button.click()

    assert not controls.button.isChecked()
    assert device.calls == []
    assert window.santokerWarmupButtonStateSignal.emissions == [False]


def test_target_field_edit_caches_while_inactive_and_sends_while_active(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    device = FakeWarmupDevice(ready=True, warmup=False)
    controller = SantokerWarmupController()
    window = compact_window(controls, controller, device)

    ApplicationWindow.santokerWarmupTargetEdited(cast(ApplicationWindow, window), 205)

    assert controller.desired_temp_c == 205.0
    assert device.calls == []

    device.warmup = True
    ApplicationWindow.santokerWarmupTargetEdited(cast(ApplicationWindow, window), 210)

    assert controller.desired_temp_c == 210.0
    assert device.calls == [('target', 210.0)]


def test_pending_reconnect_keeps_desired_button_checked_and_disabled(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    device = FakeWarmupDevice(ready=True, warmup=False)

    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.ready = False
    controller.note_transport_loss()

    window = compact_window(controls, controller, device)
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.button.isChecked()
    assert not controls.button.isEnabled()


def test_ready_false_marks_transport_loss_without_emit_or_restore(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    device = FakeWarmupDevice(ready=True, warmup=False)
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK

    window = compact_window(controls, controller, device)
    frame_signal = Mock()
    window.santokerFrameSignal = frame_signal
    calls = len(device.calls)

    device.ready = False
    ApplicationWindow.santokerWarmupReadyChanged(cast(ApplicationWindow, window), False)
    ApplicationWindow.santokerWarmupReadyChanged(cast(ApplicationWindow, window), True)

    assert frame_signal.emit.call_count == 0
    assert len(device.calls) == calls
    assert controller.desired_enabled() is True


def test_full_operating_state_recovery_charge_records_mode_defaults_without_writes() -> None:
    device = ParserWarmupDevice(machine_on=0, heating_on=-1)
    controller = SantokerControlController()
    window = SimpleNamespace(
        santoker=device,
        santokerControlController=controller,
        santokerControlRecoveryReported=True,
    )

    ApplicationWindow.markSantokerCharge(cast(ApplicationWindow, window))

    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 30,
        AIR: 80,
        POWER: 70,
    }
    assert device.calls == []
    assert window.santokerControlRecoveryReported is False


def test_full_operating_state_recovery_is_generation_guarded_ordered_and_logged(
    qapplication: QApplication,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del qapplication
    device = ParserWarmupDevice(
        ready=True,
        warmup=False,
        machine_on=0,
        heating_on=0,
        power=0,
        air=0,
        drum=0,
    )
    window = compact_window(
        SantokerWarmupControls(),
        SantokerWarmupController(),
        device,
        charge_index=1,
        recording=True,
    )
    window.qmc.timeindex = [1, 0, 0, 0, 0, 0, 0, 0]
    window.santokerMonitoringGeneration = 2
    window.santokerControlController = SantokerControlController()
    window.santokerWarmupReadyChanged = lambda ready: (
        ApplicationWindow.santokerWarmupReadyChanged(
            cast(ApplicationWindow, window), ready
        )
    )
    window.santokerFrameAccepted = lambda: ApplicationWindow.santokerFrameAccepted(
        cast(ApplicationWindow, window)
    )
    window.refreshSantokerWarmupControls = lambda: None
    ApplicationWindow.markSantokerCharge(cast(ApplicationWindow, window))

    for target, value in ((DRUM, 31), (AIR, 81), (POWER, 71)):
        ApplicationWindow.santokerSendMessage(
            cast(ApplicationWindow, window), target, value
        )
    device.calls.clear()

    device.ready = False
    ApplicationWindow.santokerWarmupReadyChangedForGeneration(
        cast(ApplicationWindow, window), 1, False
    )
    assert not window.santokerControlController.restoration_pending()
    ApplicationWindow.santokerWarmupReadyChangedForGeneration(
        cast(ApplicationWindow, window), 2, False
    )
    assert window.santokerControlController.restoration_pending()

    device.ready = True
    device.machine_on_fresh = device.heating_on_fresh = False
    device.power_fresh = device.air_fresh = device.drum_fresh = False
    expected = [
        (MACHINE_ON, 1),
        (HEATING_ON, 1),
        (DRUM, 31),
        (AIR, 81),
        (POWER, 71),
    ]
    for target, value in expected:
        ApplicationWindow.santokerFrameAcceptedForGeneration(
            cast(ApplicationWindow, window), 2
        )
        assert device.calls[-1] == ('raw', (target, value))
        device.report(target, value)

    ApplicationWindow.santokerFrameAcceptedForGeneration(
        cast(ApplicationWindow, window), 2
    )

    assert device.calls == [('raw', item) for item in expected]
    window.sendmessage.assert_called_once_with('Connected')
    assert [record.getMessage() for record in caplog.records] == [
        'Santoker control restore target=7A value=1',
        'Santoker control restore target=7B value=1',
        'Santoker control restore target=C0 value=31',
        'Santoker control restore target=CA value=81',
        'Santoker control restore target=FA value=71',
    ]


def test_operating_mode_commands_update_active_intent_before_raw_forwarding() -> None:
    controller = SantokerControlController()

    class IntentObservingDevice(ParserWarmupDevice):
        def send_msg(self, target: bytes, value: int) -> None:
            assert controller.intended_controls()[target] == value
            super().send_msg(target, value)

    device = IntentObservingDevice()
    window = SimpleNamespace(
        santoker=device,
        santokerControlController=controller,
        qmc=SimpleNamespace(
            flagstart=True,
            timeindex=[1, 0, 0, 0, 0, 0, 0, 0],
        ),
    )

    ApplicationWindow.santokerSendMessage(
        cast(ApplicationWindow, window), MACHINE_ON, 0
    )
    ApplicationWindow.santokerSendMessage(
        cast(ApplicationWindow, window), HEATING_ON, 0
    )

    assert controller.intended_controls() == {MACHINE_ON: 0, HEATING_ON: 0}
    assert device.calls == [
        ('raw', (MACHINE_ON, 0)),
        ('raw', (HEATING_ON, 0)),
    ]


@pytest.mark.parametrize('target', [MACHINE_ON, HEATING_ON])
def test_operating_mode_command_outside_roast_and_stale_generation_is_not_intent(
    target: bytes,
) -> None:
    device = ParserWarmupDevice()
    controller = SantokerControlController()
    window = SimpleNamespace(
        santokerMonitoringGeneration=2,
        santoker=device,
        santokerControlController=controller,
        qmc=SimpleNamespace(
            flagstart=False,
            timeindex=[-1, 0, 0, 0, 0, 0, 0, 0],
        ),
    )

    ApplicationWindow.santokerSendMessageForGeneration(
        cast(ApplicationWindow, window), 2, target, 0
    )
    assert device.calls == [('raw', (target, 0))]
    assert controller.intended_controls() == {}

    window.qmc.flagstart = True
    window.qmc.timeindex[0] = 1
    ApplicationWindow.santokerSendMessageForGeneration(
        cast(ApplicationWindow, window), 1, target, 1
    )
    assert device.calls == [('raw', (target, 0))]
    assert controller.intended_controls() == {}


def test_cancelled_recovery_does_not_report_connected(
    qapplication: QApplication,
) -> None:
    del qapplication
    device = ParserWarmupDevice(ready=True, warmup=False)
    controller = SantokerControlController()
    window = compact_window(
        SantokerWarmupControls(),
        SantokerWarmupController(),
        device,
        charge_index=1,
        recording=True,
    )
    window.qmc.timeindex = [1, 0, 0, 0, 0, 0, 0, 0]
    window.santokerControlController = controller
    window.refreshSantokerWarmupControls = lambda: None
    controller.mark_charge(device)

    device.ready = False
    ApplicationWindow.santokerWarmupReadyChanged(
        cast(ApplicationWindow, window), False
    )
    window.qmc.flagstart = False
    window.qmc.timeindex[0] = -1
    device.ready = True

    ApplicationWindow.santokerFrameAccepted(cast(ApplicationWindow, window))

    window.sendmessage.assert_not_called()
    assert not controller.restoration_pending()


def test_initially_unready_active_roast_control_request_does_not_replay(
    qapplication: QApplication,
) -> None:
    del qapplication
    device = ParserWarmupDevice(ready=False, warmup=False, air=-1, drum=-1)
    controller = SantokerControlController()
    window = compact_window(
        SantokerWarmupControls(),
        SantokerWarmupController(),
        device,
        charge_index=1,
        recording=True,
    )
    window.qmc.timeindex = [1, 0, 0, 0, 0, 0, 0, 0]
    window.santokerControlController = controller
    window.refreshSantokerWarmupControls = lambda: None

    ApplicationWindow.santokerSendMessage(
        cast(ApplicationWindow, window), POWER, 90
    )
    device.ready = True
    ApplicationWindow.santokerFrameAccepted(cast(ApplicationWindow, window))

    raw_calls = [
        cast(tuple[bytes, int], payload)
        for kind, payload in device.calls
        if kind == 'raw'
    ]
    assert raw_calls == [(POWER, 90)]
    assert controller.intended_controls() == {POWER: 90}
    assert not controller.restoration_pending()


@pytest.mark.parametrize('target', [POWER, AIR, DRUM])
@pytest.mark.parametrize(
    ('recording', 'charge_index', 'drop_index'),
    [
        (False, 1, 0),
        (True, -1, 0),
        (True, 1, 5),
    ],
    ids=['not-recording', 'precharge', 'postdrop'],
)
def test_control_command_outside_active_roast_is_not_restoration_intent(
    target: bytes,
    recording: bool,
    charge_index: int,
    drop_index: int,
) -> None:
    device = ParserWarmupDevice(ready=True)
    controller = SantokerControlController()
    window = SimpleNamespace(
        santoker=device,
        santokerControlController=controller,
        qmc=SimpleNamespace(
            flagstart=recording,
            timeindex=[charge_index, 0, 0, 0, 0, 0, drop_index, 0],
        ),
    )

    ApplicationWindow.santokerSendMessage(
        cast(ApplicationWindow, window), target, 90
    )

    assert device.calls == [('raw', (target, 90))]
    assert controller.intended_controls() == {}


def test_non_control_target_is_never_restoration_intent() -> None:
    device = ParserWarmupDevice(ready=True)
    controller = SantokerControlController()
    window = SimpleNamespace(
        santoker=device,
        santokerControlController=controller,
        qmc=SimpleNamespace(
            flagstart=True,
            timeindex=[1, 0, 0, 0, 0, 0, 0, 0],
        ),
    )

    ApplicationWindow.santokerSendMessage(
        cast(ApplicationWindow, window), b'\x7c', 1
    )

    assert device.calls == [('raw', (b'\x7c', 1))]
    assert controller.intended_controls() == {}


@pytest.mark.parametrize(
    ('recording', 'charge_index', 'drop_index', 'armed'),
    [
        (True, 1, 0, True),
        (False, 1, 0, False),
        (True, -1, 0, False),
        (True, 1, 5, False),
    ],
)
def test_confirmed_transport_loss_arms_only_during_active_roast(
    recording: bool,
    charge_index: int,
    drop_index: int,
    armed: bool,
) -> None:
    device = ParserWarmupDevice(ready=False)
    controller = SantokerControlController()
    window = SimpleNamespace(
        santoker=device,
        santokerWarmup=False,
        santokerControlController=controller,
        qmc=SimpleNamespace(
            flagstart=recording,
            timeindex=[charge_index, 0, 0, 0, 0, 0, drop_index, 0],
        ),
    )

    ApplicationWindow.santokerWarmupReadyChanged(
        cast(ApplicationWindow, window), False
    )

    assert controller.restoration_pending() is armed
    assert controller.intended_controls() == (
        {POWER: 70, AIR: 80, DRUM: 30} if armed else {}
    )


@pytest.mark.parametrize('target', [POWER, AIR, DRUM])
def test_stale_generation_control_command_does_not_cross_monitoring_sessions(
    target: bytes,
) -> None:
    device = ParserWarmupDevice()
    controller = SantokerControlController()
    window = SimpleNamespace(
        santokerMonitoringGeneration=2,
        santoker=device,
        santokerControlController=controller,
        qmc=SimpleNamespace(
            flagstart=True,
            timeindex=[1, 0, 0, 0, 0, 0, 0, 0],
        ),
    )

    ApplicationWindow.santokerSendMessageForGeneration(
        cast(ApplicationWindow, window), 1, target, 90
    )
    assert device.calls == []
    assert controller.intended_controls() == {}

    ApplicationWindow.santokerSendMessageForGeneration(
        cast(ApplicationWindow, window), 2, target, 90
    )
    assert device.calls == [('raw', (target, 90))]
    assert controller.intended_controls() == {target: 90}


def test_event_action_thread_captures_monitoring_generation_at_creation() -> None:
    window = SimpleNamespace(
        santokerMonitoringGeneration=4,
        eventaction_internal=Mock(),
    )
    thread = EventActionThread(cast(Any, window), 6, 'santoker(fa,90)', None)
    window.santokerMonitoringGeneration = 5

    thread.run()

    window.eventaction_internal.assert_called_once_with(
        6, 'santoker(fa,90)', None, 4
    )


def test_multiple_event_forwards_monitoring_generation_to_nested_action() -> None:
    window = SimpleNamespace(
        simulator=False,
        lastbuttonpressed=-1,
        recordextraevent=Mock(),
    )

    ApplicationWindow.eventaction_internal(
        cast(ApplicationWindow, window),
        3,
        '1',
        None,
        4,
    )

    window.recordextraevent.assert_called_once_with(
        0,
        parallel=False,
        updateButtons=False,
        santoker_generation=4,
    )


def test_nested_extra_event_forwards_monitoring_generation_to_command() -> None:
    window = SimpleNamespace(
        extraeventstypes=[9],
        mark_last_button_pressed=False,
        lastbuttonpressed=-1,
        extraeventsvalues=[90],
        extraeventsactionstrings=['santoker(fa,{})'],
        extraeventsactions=[6],
        buttonStates=[0],
        eventaction=Mock(),
        qmc=SimpleNamespace(
            eventsInternal2ExternalValue=lambda value: value,
            flagstart=False,
        ),
    )

    ApplicationWindow.recordextraevent(
        cast(ApplicationWindow, window),
        0,
        parallel=False,
        updateButtons=False,
        santoker_generation=4,
    )

    window.eventaction.assert_called_once_with(
        6,
        'santoker(fa,90)',
        parallel=False,
        santoker_generation=4,
    )


def test_successful_charge_snapshots_live_santoker_controls() -> None:
    device = ParserWarmupDevice()
    control_controller = Mock()
    window = SimpleNamespace(
        santoker=device,
        santokerControlController=control_controller,
    )

    ApplicationWindow.markSantokerCharge(cast(ApplicationWindow, window))

    control_controller.mark_charge.assert_called_once_with(device)


def test_drop_immediately_clears_control_restoration_intent() -> None:
    device = ParserWarmupDevice()
    control_controller = SantokerControlController()
    control_controller.mark_charge(device)
    window = SimpleNamespace(
        santokerControlController=control_controller,
        santokerControlRecoveryReported=True,
    )

    ApplicationWindow.markSantokerDrop(cast(ApplicationWindow, window))

    assert control_controller.intended_controls() == {}
    assert not control_controller.restoration_pending()
    assert window.santokerControlRecoveryReported is False
    canvas_source = Path('artisanlib/canvas.py').read_text(encoding='utf-8')
    assert 'self.aw.markSantokerDrop()' in canvas_source


def test_worker_frame_signal_triggers_frame_reconciliation_on_qt_main_thread(
    qapplication: QApplication,
) -> None:
    del qapplication
    from threading import Thread, get_ident

    class MainThreadWarmupDevice(FakeWarmupDevice):
        thread_ids: list[int]

        def __init__(self) -> None:
            super().__init__(ready=True, warmup=False)
            self.thread_ids = []

        @override
        def requestWarmupOn(self, temp_c: float) -> bool:
            self.thread_ids.append(get_ident())
            return super().requestWarmupOn(temp_c)

        @override
        def getReportedWarmupTarget(self) -> float | None:
            return None

    controls = SantokerWarmupControls()
    device = MainThreadWarmupDevice()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    controller.note_transport_loss()

    window = cast(Any, ApplicationWindow.__new__(ApplicationWindow))
    QMainWindow.__init__(window)
    window.app = SimpleNamespace(artisanviewerMode=False)
    window.qmc = SimpleNamespace(
        mode_tempsliders='C', timeindex=[-1], flagon=True, flagstart=False
    )
    window.santokerWarmup = True
    window.santoker = device
    window.santokerWarmupController = controller
    window.santokerWarmupControls = controls
    window.pushbuttonstyles = {'OFF': 'off-style', 'ON': 'on-style'}
    window.reportSantokerWarmupResult = Mock()
    window.sendmessage = Mock()
    window.santokerWarmupControlsRefreshSignal = Mock()
    window.santokerFrameSignal.connect(
        window.santokerFrameAccepted,
        type=Qt.ConnectionType.QueuedConnection,
    )

    main_thread = get_ident()
    baseline = len(device.thread_ids)

    def emit_frame() -> None:
        window.santokerFrameSignal.emit()

    thread = Thread(target=emit_frame)
    thread.start()
    thread.join(timeout=3)

    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)

    assert not thread.is_alive()
    assert len(device.thread_ids) == baseline + 1
    assert device.thread_ids[-1] == main_thread


def test_queued_stop_callbacks_do_not_leak_into_replacement_session(
    qapplication: QApplication,
) -> None:
    del qapplication
    from artisanlib.santoker import Santoker
    from artisanlib.santoker_warmup import RestorationState

    window = cast(Any, ApplicationWindow.__new__(ApplicationWindow))
    QMainWindow.__init__(window)
    window.qmc = SimpleNamespace(timeindex=[-1], flagstart=False)
    window.santokerWarmup = True
    window.santokerWarmupControls = None
    window.santokerWarmupController = SantokerWarmupController(desired_temp_c=205.0)
    window.santokerDiagnosticsSession = None
    window.santokerSerial = False
    window.santokerBLE = False
    window.santokerWarmupReadySignal.connect(
        window.santokerWarmupReadyChanged,
        type=Qt.ConnectionType.QueuedConnection,
    )
    window.santokerWarmupStateSignal.connect(
        window.santokerWarmupStateChanged,
        type=Qt.ConnectionType.QueuedConnection,
    )

    first_session = window.startSantokerDiagnosticsSession()
    first_santoker = Santoker(
        ready_handler=window.santokerWarmupReadySignal.emit,
        warmup_handler=window.santokerWarmupStateSignal.emit,
        diagnostics=first_session,
    )
    window.santoker = first_santoker
    first_santoker._setHeaderReady(True)
    first_santoker._setWarmupState(True)
    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)
    assert window.santokerWarmupController.set_enabled(
        True, -1, first_santoker
    ) is WarmupResult.OK

    window.stopSantokerMonitoring()

    assert window.santoker is None
    assert window.santokerWarmupController.restoration_state() is RestorationState.IDLE
    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)
    assert window.santokerWarmupController.restoration_state() is RestorationState.IDLE

    replacement_session = window.startSantokerDiagnosticsSession()
    assert replacement_session is not first_session
    assert replacement_session.view().state.restoration_state is RestorationState.IDLE

    replacement_santoker = Santoker(
        ready_handler=window.santokerWarmupReadySignal.emit,
        warmup_handler=window.santokerWarmupStateSignal.emit,
        diagnostics=replacement_session,
    )
    window.santoker = replacement_santoker
    replacement_santoker._setHeaderReady(True)
    replacement_santoker._setWarmupState(True)
    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)
    assert window.santokerWarmupController.set_enabled(
        True, -1, replacement_santoker
    ) is WarmupResult.OK

    replacement_santoker.resetProtocolState()
    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)

    assert window.santoker is replacement_santoker
    assert window.santokerWarmupController.desired_enabled() is True
    assert (
        window.santokerWarmupController.restoration_state()
        is RestorationState.WAITING_FOR_DATA
    )
    assert (
        replacement_session.view().state.restoration_state
        is RestorationState.WAITING_FOR_DATA
    )


def test_queued_old_generation_callbacks_cannot_mutate_replacement_device(
    qapplication: QApplication,
) -> None:
    del qapplication
    from artisanlib.santoker_warmup import RestorationState

    controls = SantokerWarmupControls()
    replacement = FakeWarmupDevice(ready=True, warmup=True, reported_target=205.0)
    controller = SantokerWarmupController(desired_temp_c=205.0)
    assert controller.set_enabled(True, -1, replacement) is WarmupResult.OK
    replacement.calls.clear()

    window = cast(Any, ApplicationWindow.__new__(ApplicationWindow))
    QMainWindow.__init__(window)
    window.app = SimpleNamespace(artisanviewerMode=False)
    window.qmc = SimpleNamespace(
        mode_tempsliders='C', timeindex=[-1], flagon=True, flagstart=False
    )
    window.santokerWarmup = True
    window.santoker = replacement
    window.santokerWarmupController = controller
    window.santokerWarmupControls = controls
    window.pushbuttonstyles = {'OFF': 'off-style', 'ON': 'on-style'}
    window.sendmessage = Mock()
    window.santokerMonitoringGeneration = 2
    window.santokerWarmupReadyGenerationSignal.connect(
        window.santokerWarmupReadyChangedForGeneration,
        type=Qt.ConnectionType.QueuedConnection,
    )
    window.santokerWarmupStateGenerationSignal.connect(
        window.santokerWarmupStateChangedForGeneration,
        type=Qt.ConnectionType.QueuedConnection,
    )
    window.santokerFrameGenerationSignal.connect(
        window.santokerFrameAcceptedForGeneration,
        type=Qt.ConnectionType.QueuedConnection,
    )
    window.santokerCallbackGenerationSignal.connect(
        window.santokerCallbackForGeneration,
        type=Qt.ConnectionType.QueuedConnection,
    )
    stale_device_callback = Mock()

    window.santokerWarmupReadyGenerationSignal.emit(1, False)
    window.santokerWarmupStateGenerationSignal.emit(1, False)
    window.santokerFrameGenerationSignal.emit(1)
    window.santokerCallbackGenerationSignal.emit(1, stale_device_callback)
    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)

    stale_device_callback.assert_not_called()
    assert controller.desired_enabled() is True
    assert controller.restoration_state() is RestorationState.IDLE
    assert replacement.calls == []


def test_transport_connected_records_without_frame_signal_or_restoration() -> None:
    from artisanlib.santoker import Santoker
    from artisanlib.santoker_diagnostics import SantokerDiagnosticsSession

    session = SantokerDiagnosticsSession('Wi-Fi')
    frame_handler = Mock()
    santoker = Santoker(diagnostics=session, frame_handler=frame_handler)

    assert santoker._connected_handler is not None
    santoker._connected_handler()

    view = session.view()
    assert view.state.connected
    assert [event.description for event in view.events] == [
        'monitoring started',
        'connected',
    ]
    assert not any(event.direction == 'TX' for event in view.events)
    frame_handler.assert_not_called()


def test_santoker_protocol_signals_are_queued_to_gui_slots() -> None:
    import inspect

    source = inspect.getsource(ApplicationWindow.__init__)
    signal_names = (
        'santokerWarmupReadySignal',
        'santokerWarmupStateSignal',
        'santokerWarmupTargetSignal',
        'santokerFrameSignal',
    )
    for signal_name in signal_names:
        connection = source[source.index(f'self.{signal_name}.connect('):]
        connection = connection[:connection.index(')\n')]
        assert 'Qt.ConnectionType.QueuedConnection' in connection


def test_worker_target_edit_queues_compact_refresh_to_gui_signal(
    qapplication: QApplication,
) -> None:
    del qapplication
    from threading import Thread, get_ident

    class RecordingWarmupDevice(FakeWarmupDevice):
        thread_ids: list[int]

        def __init__(self) -> None:
            super().__init__(ready=True, warmup=True)
            self.thread_ids = []

        @override
        def setWarmupTarget(self, temp_c: float) -> bool:
            self.thread_ids.append(get_ident())
            return super().setWarmupTarget(temp_c)

    controls = SantokerWarmupControls()
    controls.configureTarget('C', 190.0)
    device = RecordingWarmupDevice()
    controller = SantokerWarmupController()
    window = cast(Any, ApplicationWindow.__new__(ApplicationWindow))
    QMainWindow.__init__(window)
    window.app = SimpleNamespace(artisanviewerMode=False)
    window.qmc = SimpleNamespace(
        mode_tempsliders='C', timeindex=[-1], flagon=True, flagstart=False
    )
    window.santokerWarmup = True
    window.santoker = device
    window.santokerWarmupController = controller
    window.santokerWarmupControls = controls
    window.pushbuttonstyles = {'OFF': 'off-style', 'ON': 'on-style'}
    window.reportSantokerWarmupResult = Mock()
    refresh_signal = getattr(window, 'santokerWarmupControlsRefreshSignal', None)
    if refresh_signal is not None:
        refresh_signal.connect(window.refreshSantokerWarmupControls)
    results: list[bool] = []
    errors: list[BaseException] = []

    def run_action() -> None:
        try:
            results.append(window.setSantokerWarmupTarget(205.0))
        except BaseException as exc:  # pragma: no cover - re-raised in the test thread
            errors.append(exc)

    thread = Thread(target=run_action)
    thread.start()
    thread.join(timeout=3)

    if errors:
        raise errors[0]
    assert not thread.is_alive()
    assert results == [True]
    assert controller.desired_temp_c == 205.0
    assert device.calls == [('target', 205.0)]
    assert device.thread_ids and device.thread_ids[0] != get_ident()
    assert controls.target.value() == 190
    assert hasattr(window, 'santokerWarmupControlsRefreshSignal')

    QCoreApplication.sendPostedEvents(window, QEvent.Type.MetaCall)

    assert controls.target.value() == 205


def test_warmup_report_updates_compact_state_without_command(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    device = FakeWarmupDevice(ready=True, warmup=True)
    window = compact_window(controls, SantokerWarmupController(), device)
    changed = Mock()
    controls.enabledChanged.connect(changed)

    ApplicationWindow.santokerWarmupStateChanged(cast(ApplicationWindow, window), True)

    assert not controls.button.isChecked()
    changed.assert_not_called()
    assert device.calls == []


@pytest.mark.parametrize(
    ('initial_unit', 'new_unit', 'expected_target'),
    [('C', 'F', 374), ('F', 'C', 190)],
    ids=['celsius-to-fahrenheit', 'fahrenheit-to-celsius'],
)
def test_temperature_mode_switch_refreshes_compact_target_without_slider_side_effects(
    qapplication: QApplication,
    initial_unit: str,
    new_unit: str,
    expected_target: int,
) -> None:
    del qapplication
    from unittest.mock import Mock

    from artisanlib.canvas import tgraphcanvas

    controls = SantokerWarmupControls()
    controls.configureTarget(cast(Literal['C', 'F'], initial_unit), 190.0 if initial_unit == 'C' else 374.0)
    slider_values = [10, 20, 30, 40]
    sliders = [QSlider(Qt.Orientation.Vertical) for _ in range(4)]
    for index, slider in enumerate(sliders):
        slider.setRange(0, 500)
        slider.setValue(slider_values[index])
    slider_actions = [Mock() for _ in sliders]
    for slider, action in zip(sliders, slider_actions, strict=True):
        slider.valueChanged.connect(action)
    pidcontrol = SimpleNamespace(conv2celsius=Mock(), conv2fahrenheit=Mock())
    window = compact_window(controls, SantokerWarmupController(), None, unit=initial_unit)
    window.slider1 = sliders[0]
    window.slider2 = sliders[1]
    window.slider3 = sliders[2]
    window.slider4 = sliders[3]
    window.eventslidermin = [100, 0, 0, 0]
    window.eventslidermax = [300, 100, 100, 100]
    window.eventslidervalues = slider_values
    window.eventslidertemp = [1, 0, 0, 0]
    window.eventslidercommands = ['', '', '', '']
    window.pidcontrol = pidcontrol
    window.updateSliderLCD = Mock()
    window.updateSliderMinMax = lambda: ApplicationWindow.updateSliderMinMax(
        cast(ApplicationWindow, window)
    )
    window.updateSantokerWarmupControls = lambda: ApplicationWindow.updateSantokerWarmupControls(
        cast(ApplicationWindow, window)
    )
    canvas = SimpleNamespace(
        aw=window,
        mode=new_unit,
        mode_tempsliders=initial_unit,
        timeindex=[-1],
        flagon=True,
        flagstart=False,
    )
    window.qmc = canvas

    tgraphcanvas.adjustTempSliders(cast(tgraphcanvas, canvas))

    assert canvas.mode_tempsliders == new_unit
    assert controls.target.value() == expected_target
    assert window.santokerWarmupController.desired_temp_c == 190.0
    assert [slider.value() for slider in sliders[1:]] == [20, 30, 40]
    for action in slider_actions:
        action.assert_not_called()


def test_warmup_on_and_charge_are_serialized(
    qapplication: QApplication,
) -> None:
    from threading import Barrier, Event, Thread, get_ident

    from artisanlib.canvas import tgraphcanvas

    target_started = Barrier(2)
    release_target = Event()
    charge_waiting_for_lock = Event()
    on_completed = Event()
    call_order: list[str] = []
    errors: list[BaseException] = []
    main_thread_id = get_ident()

    class BarrierWarmupController(SantokerWarmupController):
        observe_charge = False

        @contextmanager
        @override
        def serialized(self) -> Iterator[None]:
            if self.observe_charge and get_ident() == main_thread_id:
                self.observe_charge = False
                charge_waiting_for_lock.set()
            with super().serialized():
                yield

    class BlockingWarmupDevice(FakeWarmupDevice):
        @override
        def setWarmupTarget(self, temp_c: float) -> bool:
            call_order.append(f'target:{temp_c:g}')
            target_started.wait(timeout=3)
            if not release_target.wait(timeout=3):
                raise TimeoutError('target transmission was not released')
            return True

        @override
        def setWarmup(self, enabled: bool) -> bool:
            call_order.append('on' if enabled else 'off')
            self.warmup = enabled
            if enabled:
                on_completed.set()
            return True

    @dataclass
    class FakeSemaphore:
        locked: bool = False

        def acquire(self, _count: int) -> None:
            assert not self.locked
            self.locked = True

        def available(self) -> int:
            return int(not self.locked)

        def release(self, _count: int) -> None:
            assert self.locked
            self.locked = False

    @dataclass
    class FakeChargeButton:
        flat: bool = False
        animating: bool = False

        def isFlat(self) -> bool:
            return self.flat

        def setFlat(self, flat: bool) -> None:
            self.flat = flat

        def startAnimation(self) -> None:
            self.animating = True

        def stopAnimation(self) -> None:
            self.animating = False

    del qapplication
    controls = SantokerWarmupControls()
    controls.setState(True)
    controls.button.setEnabled(True)
    safety_now = [10.0]
    controller = BarrierWarmupController(monotonic_clock=lambda: safety_now[0])
    device = BlockingWarmupDevice()
    window = compact_window(controls, controller, device)
    window.santokerWarmupButtonStateSignal = Mock()
    charge_button = FakeChargeButton()
    window.buttonCHARGE = charge_button
    window.ntb = SimpleNamespace(_nav_stack=list)
    window.soundpopSignal = Mock()
    window.pidcontrol = SimpleNamespace(pidOnCHARGE=False, pidActive=False)
    window.setTimerColorSignal = Mock()
    window.eventslidervisibilities = [False] * 4
    window.arabicReshape = lambda text: text
    window.onMarkMoveToNext = Mock()
    window.openPropertiesSignal = Mock()
    window.markSantokerCharge = Mock()
    window.updateRoastNameFromInventoryAtCharge = Mock()

    canvas = SimpleNamespace(
        aw=window,
        mode_tempsliders='C',
        timeindex=[-1],
        profileDataSemaphore=FakeSemaphore(),
        flagon=True,
        flagstart=True,
        fileDirtySignal=Mock(),
        autoChargeIdx=0,
        device=134,
        timex=[5.0],
        chargeTimerPeriod=0,
        locktimex=False,
        locktimex_start=0.0,
        chargemintime=-120.0,
        resetmaxtime=1200.0,
        fixmaxtime=False,
        endofx=1200.0,
        xaxistosm=Mock(),
        BTcurve=False,
        ETcurve=False,
        updateProjection=Mock(),
        buttonactions=[6],
        buttonactionstrings=['santokerWarmup(0);santoker(80,1)'],
        timealign=Mock(),
        LCDdecimalplaces=0,
        temp2=[200.0],
        mode='C',
        roastpropertiesAutoOpenFlag=False,
        l_annotations=[],
        l_annotations_dict={},
        ystep_down=0,
        ystep_up=0,
        _tgraphcanvas__dijkstra_to_ascii=lambda text: text,
        adderror=Mock(),
    )
    window.qmc = canvas
    window.updateSantokerWarmupControls = lambda: ApplicationWindow.updateSantokerWarmupControls(
        cast(ApplicationWindow, window)
    )

    def charge_event_action(_action: int, _command: str) -> None:
        assert controller.is_charge_latched()
        assert not controls.button.isChecked()
        assert not controls.button.isEnabled()
        assert ApplicationWindow.setSantokerWarmup(
            cast(ApplicationWindow, window), False
        )
        call_order.append('raw:80')

    window.eventactionx = charge_event_action

    def run_on() -> None:
        try:
            assert ApplicationWindow.setSantokerWarmup(
                cast(ApplicationWindow, window), True
            )
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.append(exc)

    def release_on_when_charge_waits() -> None:
        try:
            if not charge_waiting_for_lock.wait(timeout=3):
                raise TimeoutError('CHARGE did not wait for the controller lock')
            release_target.set()
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.append(exc)

    def charge_action() -> None:
        call_order.append('charge')
        assert on_completed.is_set()
        tgraphcanvas._markCharge(cast(tgraphcanvas, canvas))

    on_thread = Thread(target=run_on)
    on_thread.start()
    target_started.wait(timeout=3)

    controller.observe_charge = True
    release_thread = Thread(target=release_on_when_charge_waits)
    release_thread.start()
    ApplicationWindow.runSantokerWarmupCharge(
        cast(ApplicationWindow, window), charge_action
    )
    on_thread.join(timeout=3)
    release_thread.join(timeout=3)

    if errors:
        raise errors[0]
    assert not on_thread.is_alive()
    assert not release_thread.is_alive()
    assert call_order == ['target:190', 'on', 'charge', 'off', 'raw:80']
    assert call_order.index('off') < call_order.index('raw:80')
    assert controller.is_charge_latched()
    assert not controls.button.isChecked()
    assert not controls.button.isEnabled()

    ApplicationWindow.runSantokerWarmupCharge(
        cast(ApplicationWindow, window),
        lambda: tgraphcanvas._markCharge(cast(tgraphcanvas, canvas)),
    )

    assert canvas.timeindex[0] == -1
    assert controller.is_charge_latched()
    assert not controls.button.isChecked()
    assert not controls.button.isEnabled()
    before_rejected_on = call_order.copy()
    assert not ApplicationWindow.setSantokerWarmup(
        cast(ApplicationWindow, window), True
    )
    assert call_order == before_rejected_on

    device.warmup = True
    safety_now[0] = 11.0
    window.refreshSantokerWarmupControls = lambda: (
        ApplicationWindow.refreshSantokerWarmupControls(cast(ApplicationWindow, window))
    )
    ApplicationWindow.santokerFrameAccepted(cast(ApplicationWindow, window))

    assert call_order == before_rejected_on + ['off']
    assert not controls.button.isChecked()
    assert not controls.button.isEnabled()

    device.ready = False
    controller.reset_charge()
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert not controller.is_charge_latched()
    assert not controls.button.isEnabled()

    device.ready = True
    ApplicationWindow.santokerWarmupReadyChanged(
        cast(ApplicationWindow, window), True
    )

    assert controls.button.isEnabled()
    assert not controls.button.isChecked()


@pytest.mark.parametrize(
    ('device', 'enabled_after_reset'),
    [(None, False), (FakeWarmupDevice(ready=True), True)],
    ids=['disconnected', 'retained-ready'],
)
def test_successful_reset_clears_charge_latch_after_profile_unlock(
    qapplication: QApplication,
    device: FakeWarmupDevice | None,
    enabled_after_reset: bool,
) -> None:
    from artisanlib.canvas import tgraphcanvas

    del qapplication
    trace: list[str] = []
    semaphore = TrackingSemaphore(trace)
    controller = ResetTrackingController(semaphore, trace)
    controller.mark_charge()
    controls = SantokerWarmupControls()
    controls.setState(True)
    controls.button.setEnabled(True)
    canvas = successful_reset_canvas(
        controls, controller, device, semaphore, trace
    )

    assert tgraphcanvas.reset(
        cast(tgraphcanvas, canvas),
        redraw=False,
        soundOn=False,
        fireResetAction=False,
    )

    assert trace == [
        'semaphore-acquired',
        'semaphore-released',
        'measurements-cleared',
        'reset-charge',
        'controls-refreshed',
    ]
    assert not controller.is_charge_latched()
    canvas.aw.santokerControlController.reset_roast.assert_called_once_with()
    assert controls.button.isEnabled() is enabled_after_reset
    assert not controls.button.isChecked()
    assert (
        canvas.roastServerInventoryOrigin,
        canvas.roastServerInventoryOrganizationUUID,
        canvas.roastServerBeanLotUUID,
        canvas.roastServerBeanLotName,
    ) == INVENTORY_SELECTION
    canvas.aw.releaseRoastServerInventory.assert_called_once_with(INVENTORY_ROAST_UUID)
    canvas.adderror.assert_not_called()


def test_caught_reset_exception_does_not_clear_charge_latch(
    qapplication: QApplication,
) -> None:
    from artisanlib.canvas import tgraphcanvas

    del qapplication
    trace: list[str] = []
    semaphore = TrackingSemaphore(trace)
    controller = ResetTrackingController(semaphore, trace)
    controller.mark_charge()
    controls = SantokerWarmupControls()
    canvas = successful_reset_canvas(
        controls, controller, FakeWarmupDevice(), semaphore, trace
    )
    canvas.resetTimer.side_effect = RuntimeError('timer reset failed')

    assert tgraphcanvas.reset(
        cast(tgraphcanvas, canvas),
        redraw=False,
        soundOn=False,
        fireResetAction=False,
    )

    assert controller.is_charge_latched()
    assert 'reset-charge' not in trace
    assert 'controls-refreshed' not in trace
    assert (
        canvas.roastServerInventoryOrigin,
        canvas.roastServerInventoryOrganizationUUID,
        canvas.roastServerBeanLotUUID,
        canvas.roastServerBeanLotName,
    ) == INVENTORY_SELECTION
    canvas.aw.releaseRoastServerInventory.assert_called_once_with(INVENTORY_ROAST_UUID)
    canvas.adderror.assert_called_once()


def test_uncaught_reset_exception_does_not_clear_charge_latch(
    qapplication: QApplication,
) -> None:
    from artisanlib.canvas import tgraphcanvas

    del qapplication
    trace: list[str] = []
    semaphore = TrackingSemaphore(trace)
    controller = ResetTrackingController(semaphore, trace)
    controller.mark_charge()
    controls = SantokerWarmupControls()
    canvas = successful_reset_canvas(
        controls, controller, FakeWarmupDevice(), semaphore, trace
    )
    canvas.aw.updatePhasesLCDs.side_effect = RuntimeError('phase refresh failed')

    with pytest.raises(RuntimeError, match='phase refresh failed'):
        tgraphcanvas.reset(
            cast(tgraphcanvas, canvas),
            redraw=False,
            soundOn=False,
            fireResetAction=False,
        )

    assert controller.is_charge_latched()
    assert 'reset-charge' not in trace
    assert 'controls-refreshed' not in trace
    assert (
        canvas.roastServerInventoryOrigin,
        canvas.roastServerInventoryOrganizationUUID,
        canvas.roastServerBeanLotUUID,
        canvas.roastServerBeanLotName,
    ) == INVENTORY_SELECTION
    canvas.aw.releaseRoastServerInventory.assert_called_once_with(INVENTORY_ROAST_UUID)


def test_cancelled_reset_does_not_clear_charge_latch(
    qapplication: QApplication,
) -> None:
    from artisanlib.canvas import tgraphcanvas

    del qapplication
    controller = SantokerWarmupController()
    controller.mark_charge()
    canvas = SimpleNamespace(
        aw=SimpleNamespace(
            centralWidget=Mock(return_value=None),
            santokerWarmupController=controller,
            updateSantokerWarmupControls=Mock(),
        ),
        checkSaved=Mock(return_value=False),
    )

    assert not tgraphcanvas.reset(
        cast(tgraphcanvas, canvas),
        redraw=False,
        soundOn=False,
        fireResetAction=False,
    )
    assert controller.is_charge_latched()
    canvas.aw.updateSantokerWarmupControls.assert_not_called()


def test_parser_executes_warmup_off_before_raw_santoker_command() -> None:
    device = ParserWarmupDevice(warmup=True)
    window = SimpleNamespace(
        simulator=False,
        qmc=SimpleNamespace(
            weight=[0.0, 0.0, 'g'],
            flagstart=False,
            flagon=False,
            timeindex=[-1],
        ),
        lastbuttonpressed=-1,
        lastIOResult=None,
        buttonlist=[],
        buttonStates=[],
        santokerWarmup=True,
        santoker=device,
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupButtonStateSignal=Mock(),
        reportSantokerWarmupResult=Mock(),
    )

    def set_warmup(enabled: bool) -> bool:
        return ApplicationWindow.setSantokerWarmup(
            cast(ApplicationWindow, window), enabled
        )

    window.setSantokerWarmup = set_warmup

    def send_message(target: bytes, value: int) -> None:
        ApplicationWindow.santokerSendMessage(
            cast(ApplicationWindow, window), target, value
        )

    window.santokerSendMessageSignal = SimpleNamespace(emit=send_message)

    ApplicationWindow.eventaction_internal(
        cast(ApplicationWindow, window),
        6,
        'santokerWarmup(0);santoker(80,1)',
        None,
    )

    assert device.calls == [('enabled', False), ('raw', (b'\x80', 1))]


@pytest.mark.parametrize(
    ('device', 'timex', 'manual_reading', 'expected_auto_charge'),
    [
        (134, [], None, 1),
        (18, [1.0], (0.0, -1.0, -1.0), 0),
    ],
    ids=['insufficient-data', 'manual-cancellation'],
)
def test_rejected_charge_does_not_set_latch(
    qapplication: QApplication,
    device: int,
    timex: list[float],
    manual_reading: tuple[float, float, float] | None,
    expected_auto_charge: int,
) -> None:
    from artisanlib.canvas import tgraphcanvas

    del qapplication
    controller = SantokerWarmupController()
    trace: list[str] = []
    semaphore = TrackingSemaphore(trace)
    charge_button = Mock()
    charge_button.isFlat.return_value = False
    window = SimpleNamespace(
        ntb=SimpleNamespace(_nav_stack=list),
        soundpopSignal=Mock(),
        buttonCHARGE=charge_button,
        simulator=None,
        ser=SimpleNamespace(NONE=Mock(return_value=manual_reading)),
        santokerWarmupController=controller,
        updateSantokerWarmupControls=Mock(),
        prepareRoastServerInventoryCharge=Mock(return_value=object()),
        commitRoastServerInventoryCharge=Mock(return_value=''),
        sendmessage=Mock(),
    )
    canvas = SimpleNamespace(
        aw=window,
        profileDataSemaphore=semaphore,
        flagstart=True,
        fileDirtySignal=Mock(),
        timeindex=[-1],
        autoChargeIdx=0,
        device=device,
        timex=timex,
        drawmanual=Mock(),
        adderror=Mock(),
    )

    tgraphcanvas._markCharge(cast(tgraphcanvas, canvas))

    assert not controller.is_charge_latched()
    window.updateSantokerWarmupControls.assert_not_called()
    canvas.drawmanual.assert_not_called()
    assert canvas.timeindex == [-1]
    assert canvas.autoChargeIdx == expected_auto_charge
    assert trace == ['semaphore-acquired', 'semaphore-released']
    canvas.adderror.assert_not_called()


def test_existing_qx_preset_does_not_force_warmup_off_after_charge() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from artisanlib.santoker_warmup import SantokerWarmupController

    src_dir = Path(__file__).parents[3]
    preset = src_dir / 'includes' / 'Machines' / 'Santoker' / 'Q_+_X_Series_Bluetooth.aset'
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(preset, encoding='utf-8') == [str(preset)]

    device = FakeWarmupDevice(warmup=True)
    style_signal = Mock()
    window = SimpleNamespace(
        eventslidercommands=parse_ini_array(config.get('Sliders', 'slidercommands')),
        extraeventsactionstrings=parse_ini_array(
            config.get('ExtraEventButtons', 'extraeventsactionstrings')
        ),
        buttonStates=[0] * 8,
        setExtraEventButtonStyleSignal=style_signal,
        setSantokerWarmupButtonState=Mock(),
        qmc=SimpleNamespace(timeindex=[10], flagon=True, flagstart=False),
        santoker=device,
        santokerWarmupController=SantokerWarmupController(),
        sendmessage=Mock(),
    )

    ApplicationWindow.santokerWarmupStateChanged(cast(ApplicationWindow, window), True)

    assert device.calls == []
    assert window.buttonStates == [0] * 8
    style_signal.emit.assert_not_called()
    window.sendmessage.assert_not_called()


def test_window_rejected_warmup_restoration_uses_signal_from_worker_thread() -> None:
    from threading import Thread
    from types import SimpleNamespace
    from unittest.mock import Mock

    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    button_state_signal = Mock()
    window = SimpleNamespace(
        qmc=SimpleNamespace(timeindex=[10], flagon=True, flagstart=False),
        santoker=FakeWarmupDevice(),
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupButtonStateSignal=button_state_signal,
        reportSantokerWarmupResult=Mock(),
    )
    results: list[bool] = []
    errors: list[BaseException] = []

    def run_action() -> None:
        try:
            results.append(ApplicationWindow.setSantokerWarmup(cast(ApplicationWindow, window), True))
        except BaseException as exc:  # pragma: no cover - re-raised in the test thread
            errors.append(exc)

    thread = Thread(target=run_action)
    thread.start()
    thread.join()

    if errors:
        raise errors[0]
    assert results == [False]
    window.reportSantokerWarmupResult.assert_called_once_with(WarmupResult.AFTER_CHARGE)
    button_state_signal.emit.assert_called_once_with(False)
    assert window.santoker.calls == []


def load_warmup_capability(
    window: SimpleNamespace,
    filename: Path,
    *,
    theme: bool = False,
) -> None:
    settings = QSettings(str(filename), QSettings.Format.IniFormat)
    settings.beginGroup('Device')
    try:
        ApplicationWindow.loadSantokerWarmupCapability(
            cast(ApplicationWindow, window), settings, theme=theme
        )
    finally:
        settings.endGroup()


def test_warmup_capability_defaults_false_for_legacy_settings(tmp_path: Path) -> None:
    legacy = tmp_path / 'legacy.aset'
    settings = QSettings(str(legacy), QSettings.Format.IniFormat)
    settings.setValue('Mode', 'C')
    settings.sync()
    window = SimpleNamespace(santokerWarmup=True)

    load_warmup_capability(window, legacy)

    assert window.santokerWarmup is False


def test_warmup_capability_machine_transition_x3_to_qx(tmp_path: Path) -> None:
    machine_dir = Path(__file__).parents[3] / 'includes' / 'Machines' / 'Santoker'
    window = SimpleNamespace()
    window.santokerWarmup = False

    load_warmup_capability(window, machine_dir / 'X3_Master_Bluetooth.aset')
    assert bool(window.santokerWarmup)

    load_warmup_capability(window, machine_dir / 'Q_+_X_Series_Bluetooth.aset')
    assert not bool(window.santokerWarmup)

    persistent = QSettings(
        str(tmp_path / 'persistent.ini'), QSettings.Format.IniFormat
    )
    persistent.setValue('Device/santokerWarmup', True)
    persistent.beginGroup('Device')
    ApplicationWindow.saveSantokerWarmupCapability(
        cast(ApplicationWindow, window),
        persistent,
        {'Device/santokerWarmup': False},
        read_defaults=False,
    )
    persistent.endGroup()
    assert not persistent.contains('Device/santokerWarmup')


@pytest.mark.parametrize('capability', [False, True])
def test_warmup_capability_qsettings_roundtrip(
    tmp_path: Path, capability: bool
) -> None:
    filename = tmp_path / 'settings.aset'
    settings = QSettings(str(filename), QSettings.Format.IniFormat)
    settings.beginGroup('Device')
    source = SimpleNamespace(santokerWarmup=capability)
    ApplicationWindow.saveSantokerWarmupCapability(
        cast(ApplicationWindow, source), settings, None, read_defaults=False
    )
    settings.endGroup()
    settings.sync()

    restored = SimpleNamespace(santokerWarmup=not capability)
    load_warmup_capability(restored, filename)

    assert restored.santokerWarmup is capability


@pytest.mark.parametrize('capability', [False, True])
def test_theme_load_preserves_warmup_capability(
    tmp_path: Path, capability: bool
) -> None:
    theme = tmp_path / 'theme.athm'
    settings = QSettings(str(theme), QSettings.Format.IniFormat)
    settings.setValue('Device/santokerWarmup', not capability)
    settings.sync()
    window = SimpleNamespace(santokerWarmup=capability)

    load_warmup_capability(window, theme, theme=True)

    assert window.santokerWarmup is capability


def machine_selection_window(
    original_capability: bool,
    target_capability: bool,
    refreshes: list[bool],
) -> tuple[SimpleNamespace, QAction]:
    target_name = 'Santoker X3 Master BT' if target_capability else 'Santoker Q + X Series BT'
    action = QAction(target_name)
    action.setData(('target.aset', 'Santoker', 'Santoker'))
    qmc = SimpleNamespace(
        etypes=['Air', 'Drum', 'Damper', 'Burner', '--'],
        etypesdefault=['Air', 'Drum', 'Damper', 'Burner', '--'],
        device=134 if original_capability else 18,
        extradevices=[],
        machinesetup='original',
        roastersize_setup=1.0,
        last_batchsize=1000.0,
        roastersize=1.0,
        roasterheating_setup=1,
        roasterheating=1,
        roastersize_setup_default=1.0,
        roasterheating_setup_default=1,
        heating_types=['Electric'],
        weight=(1.0, 0.0, 'Kg'),
        redraw=Mock(),
    )
    window = SimpleNamespace(
        qmc=qmc,
        modbus=SimpleNamespace(
            host='modbus',
            default_host='default-modbus',
            comport='COM1',
            default_comport='COM0',
            type=0,
        ),
        s7=SimpleNamespace(host='s7', default_host='default-s7'),
        ws=SimpleNamespace(host='ws', default_host='default-ws'),
        kaleidoHost='kaleido',
        kaleido_default_host='default-kaleido',
        mugmaHost='mugma',
        mugma_default_host='default-mugma',
        ser=SimpleNamespace(comport='COM1', default_comport='COM0'),
        santokerSerial=False,
        santokerBLE=False,
        santokerWarmup=original_capability,
        sender=lambda: action,
        sendmessage=Mock(),
        establish_etypes=Mock(),
    )

    def refresh() -> None:
        refreshes.append(window.santokerWarmup)

    def load_settings(**_kwargs: object) -> None:
        window.santokerWarmup = target_capability
        qmc.device = 999
        qmc.roastersize_setup = 1.0
        qmc.roasterheating_setup = 0
        refresh()

    window.updateSantokerWarmupControls = refresh
    window.loadSettings = load_settings
    return window, action


@pytest.mark.parametrize(
    ('original_capability', 'target_capability'),
    [(False, True), (True, False)],
)
def test_canceled_machine_selection_restores_warmup_capability(
    original_capability: bool,
    target_capability: bool,
) -> None:
    refreshes: list[bool] = []
    window, _action = machine_selection_window(
        original_capability, target_capability, refreshes
    )
    dialog = Mock()
    dialog.exec.return_value = False

    with (
        patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes),
        patch('artisanlib.main.ArtisanComboBoxDialog', return_value=dialog),
    ):
        ApplicationWindow.openMachineSettings(cast(ApplicationWindow, window))

    assert window.santokerWarmup is original_capability
    assert refreshes == [target_capability, original_capability]


def test_failed_machine_selection_restores_warmup_capability() -> None:
    refreshes: list[bool] = []
    window, _action = machine_selection_window(False, True, refreshes)

    def failed_load(**_kwargs: object) -> None:
        window.santokerWarmup = True
        window.updateSantokerWarmupControls()
        raise RuntimeError('machine load failed')

    window.loadSettings = failed_load

    with patch.object(
        QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes
    ):
        ApplicationWindow.openMachineSettings(cast(ApplicationWindow, window))

    assert window.santokerWarmup is False
    assert refreshes == [True, False]


@pytest.mark.parametrize(
    ('flagon', 'flagstart', 'controls_flag'),
    [(False, False, False), (True, False, True), (False, True, True)],
    ids=['off', 'monitoring', 'recording'],
)
def test_generic_control_visibility_refreshes_warmup_controls(
    flagon: bool,
    flagstart: bool,
    controls_flag: bool,
) -> None:
    refresh_warmup = Mock()
    window = SimpleNamespace(
        qmc=SimpleNamespace(flagon=flagon, flagstart=flagstart),
        controlsflags=[False, True, True],
        controlsVisible=Mock(return_value=not controls_flag),
        showControls=Mock(),
        hideControls=Mock(),
        updateSantokerWarmupControls=refresh_warmup,
    )

    ApplicationWindow.updateControlsVisibility(cast(ApplicationWindow, window))

    if controls_flag:
        window.showControls.assert_called_once_with(False)
        window.hideControls.assert_not_called()
    else:
        window.hideControls.assert_called_once_with(False)
        window.showControls.assert_not_called()
    refresh_warmup.assert_called_once_with()


def test_recording_stop_refreshes_warmup_after_flag_change() -> None:
    import inspect

    from artisanlib.canvas import tgraphcanvas

    source = inspect.getsource(tgraphcanvas.OffRecorder)

    flag_change = source.index('self.flagstart = False')
    refresh = source.index('self.aw.updateSantokerWarmupControls()')
    assert flag_change < refresh


@pytest.mark.parametrize(
    ('capability', 'viewer', 'monitoring', 'recording', 'visible'),
    [
        (True, False, False, False, False),
        (True, False, True, False, True),
        (True, False, False, True, True),
        (False, False, True, False, False),
        (True, True, True, False, False),
    ],
    ids=['fully-off', 'monitoring', 'recording', 'no-capability', 'viewer'],
)
def test_window_updates_compact_control_visibility(
    qapplication: QApplication,
    capability: bool,
    viewer: bool,
    monitoring: bool,
    recording: bool,
    visible: bool,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=viewer),
        qmc=SimpleNamespace(
            mode_tempsliders='C',
            timeindex=[-1],
            flagon=monitoring,
            flagstart=recording,
        ),
        santokerWarmup=capability,
        santoker=None,
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': '', 'ON': ''},
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isHidden() is (not visible)
    assert not controls.button.isEnabled()
    assert controls.target.isEnabled() is visible


def test_warmup_target_survives_off_on_visibility_cycle(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    device = FakeWarmupDevice(ready=False, warmup=False)
    window = compact_window(
        controls,
        controller,
        device,
        monitoring=False,
        recording=False,
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.isHidden()

    window.qmc.flagon = True
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.isVisible()
    assert controls.target.value() == 205
    assert controls.target.isEnabled()
    assert not controls.button.isEnabled()

    window.qmc.flagon = False
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.isHidden()
    assert controller.desired_temp_c == 205.0
    assert device.calls == []


def test_transport_disconnect_keeps_warmup_visible_while_monitoring(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    device = FakeWarmupDevice(ready=True, warmup=False)
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    window = compact_window(controls, controller, device)
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.button.isEnabled()
    assert controls.button.isChecked()

    window.santoker = None
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isVisible()
    assert controls.target.isEnabled()
    assert not controls.button.isEnabled()
    assert controls.button.isChecked()
    assert controls.target.value() == 205


def test_window_enables_compact_button_when_ready_before_charge(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(
            mode_tempsliders='C', timeindex=[-1], flagon=True, flagstart=False
        ),
        santokerWarmup=True,
        santoker=FakeWarmupDevice(ready=True, warmup=False),
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': '', 'ON': ''},
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isVisible()
    assert controls.button.isEnabled()
    assert not controls.button.isChecked()
    assert controls.target.isEnabled()


def test_window_disables_and_unchecks_compact_button_after_charge(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(
            mode_tempsliders='C', timeindex=[0], flagon=True, flagstart=False
        ),
        santokerWarmup=True,
        santoker=FakeWarmupDevice(ready=True, warmup=True),
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': '', 'ON': ''},
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isVisible()
    assert not controls.button.isEnabled()
    assert not controls.button.isChecked()
    assert controls.target.isEnabled()


def test_window_readiness_callback_refreshes_compact_button(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    device = FakeWarmupDevice(ready=False, warmup=False)
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(
            mode_tempsliders='C',
            timeindex=[-1],
            flagon=True,
            flagstart=False,
        ),
        santokerWarmup=True,
        santoker=device,
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': '', 'ON': ''},
    )
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert not controls.button.isEnabled()

    device.ready = True
    ApplicationWindow.santokerWarmupReadyChanged(cast(ApplicationWindow, window), True)

    assert controls.button.isEnabled()


def test_x3_master_bluetooth_preset_contract() -> None:
    from artisanlib.santoker import Santoker

    src_dir = Path(__file__).parents[3]
    preset = src_dir / 'includes' / 'Machines' / 'Santoker' / 'X3_Master_Bluetooth.aset'
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(preset, encoding='utf-8') == [str(preset)]

    assert config.get('General', 'roastertype_setup') == 'Santoker X3 Master BT'
    assert config.getint('Device', 'id') == 134
    assert config.getboolean('Device', 'santokerBLE')
    assert config.getboolean('Device', 'santokerWarmup')

    extra_devices = parse_ini_array(config.get('ExtraDev', 'extradevices'))
    assert extra_devices == ['135', '136']
    expected_extra_serial = {
        'extrabaudrate': ['19200', '19200'],
        'extrabytesize': ['8', '8'],
        'extracomport': ['COM1', 'COM1'],
        'extraparity': ['E', 'E'],
        'extrastopbits': ['1', '1'],
        'extratimeout': ['0.5', '0.5'],
    }
    extra_serial = {
        key: parse_ini_array(config.get('ExtraComm', key))
        for key in expected_extra_serial
    }
    assert extra_serial == expected_extra_serial
    assert {len(value) for value in extra_serial.values()} == {len(extra_devices)}

    slider_commands = parse_ini_array(config.get('Sliders', 'slidercommands'))
    assert slider_commands == [
        'santoker(ca,{})',
        'santoker(c0,{})',
        '',
        'santoker(fa,{})',
    ]
    assert parse_ini_array(config.get('Sliders', 'eventslidertemp')) == [
        '0', '0', '0', '0'
    ]
    assert parse_ini_array(config.get('Sliders', 'slidermin')) == [
        '0', '0', '0', '0'
    ]
    assert parse_ini_array(config.get('Sliders', 'slidermax')) == [
        '100', '100', '100', '100'
    ]
    assert parse_ini_array(config.get('Sliders', 'slidervisibilities')) == [
        '1', '0', '0', '1'
    ]

    event_array_keys = [
        'extraeventbuttoncolor',
        'extraeventbuttontextcolor',
        'extraeventsactions',
        'extraeventsactionstrings',
        'extraeventsdescriptions',
        'extraeventslabels',
        'extraeventstypes',
        'extraeventsvalues',
        'extraeventsvisibility',
    ]
    event_arrays = {
        key: parse_ini_array(config.get('ExtraEventButtons', key))
        for key in event_array_keys
    }
    assert {key: len(value) for key, value in event_arrays.items()} == dict.fromkeys(
        event_array_keys, 8
    )
    assert event_arrays['extraeventsactions'] == [
        '6', '6', '0', '6', '6', '0', '6', '6'
    ]
    assert event_arrays['extraeventsactionstrings'] == [
        'santoker(fa,{})',
        'santoker(fa,{})',
        '',
        'santoker(ca,{})',
        'santoker(ca,{})',
        '',
        'santoker(c0,{})',
        'santoker(c0,{})',
    ]
    assert event_arrays['extraeventstypes'] == [
        '8', '8', '4', '5', '5', '4', '6', '6'
    ]
    assert event_arrays['extraeventsvalues'] == [
        '-2', '2', '0', '-2', '2', '0', '-2', '2'
    ]
    assert event_arrays['extraeventsvisibility'] == [
        '1', '1', '0', '1', '1', '0', '0', '0'
    ]
    assert 'santokerWarmup(' not in ','.join(event_arrays['extraeventsactionstrings'])
    assert 'WARM-UP' not in event_arrays['extraeventslabels']

    default_actions = parse_ini_array(
        config.get('DefaultButtons', 'buttonactionstrings')
    )
    assert default_actions == [
        'santokerWarmup(0);santoker(80,1)',
        'santoker(81,1)',
        'santoker(82,1)',
        '',
        'santoker(83,1)',
        '',
        'santoker(84,1)',
        '',
    ]
    assert default_actions[0].split(';') == [
        'santokerWarmup(0)', 'santoker(80,1)'
    ]

    all_commands = slider_commands + event_arrays['extraeventsactionstrings'] + default_actions
    assert all('santoker(7a' not in command.lower() for command in all_commands)
    assert Santoker.DEFAULT_WARMUP_TEMP_C == 190.0
