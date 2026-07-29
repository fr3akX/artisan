import csv
import os
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast, override
from unittest.mock import Mock

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMainWindow, QSlider

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
) -> SimpleNamespace:
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(mode_tempsliders=unit, timeindex=[charge_index]),
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
    window.qmc = SimpleNamespace(mode_tempsliders='C', timeindex=[-1])
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

    for _ in range(10):
        qapplication.processEvents()
        if controls.target.value() == 205:
            break

    assert controls.target.value() == 205
    window.deleteLater()
    qapplication.processEvents()


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
    )
    window.qmc = canvas

    tgraphcanvas.adjustTempSliders(cast(tgraphcanvas, canvas))

    assert canvas.mode_tempsliders == new_unit
    assert controls.target.value() == expected_target
    assert window.santokerWarmupController.desired_temp_c == 190.0
    assert [slider.value() for slider in sliders[1:]] == [20, 30, 40]
    for action in slider_actions:
        action.assert_not_called()

def test_warmup_on_and_charge_are_serialized() -> None:
    from threading import Barrier, Event, Thread
    from types import SimpleNamespace
    from unittest.mock import Mock

    from artisanlib.santoker_warmup import SantokerWarmupController

    target_started = Barrier(2)
    release_target = Event()
    on_completed = Event()
    call_order: list[str] = []
    errors: list[BaseException] = []

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

    device = BlockingWarmupDevice()
    qmc = SimpleNamespace(timeindex=[-1])
    window = SimpleNamespace(
        eventslidercommands=['', '', 'santokerWarmupTemp({})', ''],
        extraeventsactionstrings=['santokerWarmup(1 - $)'],
        qmc=qmc,
        santokerWarmup=True,
        santoker=device,
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupButtonStateSignal=Mock(),
        reportSantokerWarmupResult=Mock(),
    )

    def run_on() -> None:
        try:
            assert ApplicationWindow.setSantokerWarmup(
                cast(ApplicationWindow, window), True
            )
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.append(exc)

    def charge_action() -> None:
        qmc.timeindex[0] = 0
        call_order.append('charge')
        assert on_completed.is_set()
        assert ApplicationWindow.setSantokerWarmup(
            cast(ApplicationWindow, window), False
        )
        call_order.append('raw:80')

    def run_charge() -> None:
        try:
            ApplicationWindow.runSantokerWarmupCharge(
                cast(ApplicationWindow, window), charge_action
            )
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.append(exc)

    on_thread = Thread(target=run_on)
    on_thread.start()
    target_started.wait(timeout=3)

    charge_thread = Thread(target=run_charge)
    charge_thread.start()
    charge_thread.join(timeout=0.2)
    release_target.set()
    on_thread.join(timeout=3)
    charge_thread.join(timeout=3)

    if errors:
        raise errors[0]
    assert not on_thread.is_alive()
    assert not charge_thread.is_alive()
    assert call_order == ['target:190', 'on', 'charge', 'off', 'raw:80']
    assert call_order.index('off') < call_order.index('raw:80')


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
        qmc=SimpleNamespace(timeindex=[10]),
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
        qmc=SimpleNamespace(timeindex=[10]),
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


@pytest.mark.parametrize(
    ('capability', 'viewer', 'visible'),
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_window_updates_compact_control_visibility(
    qapplication: QApplication, capability: bool, viewer: bool, visible: bool
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=viewer),
        qmc=SimpleNamespace(mode_tempsliders='C', timeindex=[-1]),
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


def test_window_enables_compact_button_when_ready_before_charge(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(mode_tempsliders='C', timeindex=[-1]),
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
        qmc=SimpleNamespace(mode_tempsliders='C', timeindex=[0]),
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
        qmc=SimpleNamespace(mode_tempsliders='C', timeindex=[-1]),
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
        'santokerWarmupTemp({})',
        'santoker(fa,{})',
    ]
    assert parse_ini_array(config.get('Sliders', 'eventslidertemp')) == [
        '0', '0', '1', '0'
    ]
    assert parse_ini_array(config.get('Sliders', 'slidermin')) == [
        '0', '0', '100', '0'
    ]
    assert parse_ini_array(config.get('Sliders', 'slidermax')) == [
        '100', '100', '300', '100'
    ]
    assert parse_ini_array(config.get('Sliders', 'slidervisibilities')) == [
        '1', '0', '1', '1'
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
        event_array_keys, 9
    )
    assert event_arrays['extraeventsactions'] == [
        '6', '6', '0', '6', '6', '0', '6', '6', '6'
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
        'santokerWarmup(1 - $)',
    ]
    assert event_arrays['extraeventstypes'] == [
        '8', '8', '4', '5', '5', '4', '6', '6', '4'
    ]
    assert event_arrays['extraeventsvalues'] == [
        '-2', '2', '0', '-2', '2', '0', '-2', '2', '0'
    ]
    assert event_arrays['extraeventsvisibility'] == [
        '1', '1', '0', '1', '1', '0', '0', '0', '1'
    ]
    assert event_arrays['extraeventslabels'][-1] == 'WARM-UP'

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
