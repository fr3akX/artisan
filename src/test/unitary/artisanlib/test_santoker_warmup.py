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

from artisanlib.main import ApplicationWindow
from artisanlib.santoker_warmup import SantokerWarmupController
from artisanlib.santoker_warmup_ui import SantokerWarmupControls


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
    calls: list[tuple[str, object]] = field(default_factory=list)

    def isHeaderReady(self) -> bool:
        return self.ready

    def getWarmup(self) -> bool | None:
        return self.warmup

    def setWarmupTarget(self, temp_c: float) -> bool:
        self.calls.append(('target', temp_c))
        return 100.0 <= temp_c <= 300.0

    def setWarmup(self, enabled: bool) -> bool:
        self.calls.append(('enabled', enabled))
        self.warmup = enabled
        return self.ready


@dataclass
class ParserWarmupDevice(FakeWarmupDevice):
    def send_msg(self, target: bytes, value: int) -> None:
        self.calls.append(('raw', (target, value)))


def test_controller_converts_fahrenheit_and_updates_device() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController()

    result = controller.set_target(374.0, 'F', device)

    assert result is WarmupResult.OK
    assert controller.desired_temp_c == pytest.approx(190.0)
    assert device.calls == [('target', pytest.approx(190.0))]
    assert controller.target_for_display('F') == pytest.approx(374.0)


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


@pytest.mark.parametrize(
    ('reported_temp_c', 'expected_temp_c'),
    [(99.9, 190.0), (300.1, 190.0), (100.0, 100.0), (225.5, 225.5), (300.0, 300.0)],
)
def test_accept_reported_target_validates_inclusive_range(
    reported_temp_c: float, expected_temp_c: float
) -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    controller = SantokerWarmupController()

    controller.accept_reported_target(reported_temp_c)

    assert controller.desired_temp_c == expected_temp_c


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

    assert controls.target.value() == 205
    assert controller.desired_temp_c == 205.0
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

    assert controls.button.isChecked()
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
    controller = BarrierWarmupController()
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
    ApplicationWindow.santokerWarmupStateChanged(
        cast(ApplicationWindow, window), True
    )

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
    assert controls.button.isEnabled() is enabled_after_reset
    assert not controls.button.isChecked()
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
    window = compact_window(
        controls,
        SantokerWarmupController(desired_temp_c=205.0),
        FakeWarmupDevice(ready=True, warmup=True),
    )
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.button.isEnabled()
    assert controls.button.isChecked()

    window.santoker = None
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isVisible()
    assert controls.target.isEnabled()
    assert not controls.button.isEnabled()
    assert not controls.button.isChecked()
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
