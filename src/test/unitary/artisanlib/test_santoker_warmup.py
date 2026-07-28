import csv
import os
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, override

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QSlider

from artisanlib.main import ApplicationWindow


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


def test_semantic_control_lookup() -> None:
    from artisanlib.santoker_warmup import find_warmup_buttons, find_warmup_slider

    assert find_warmup_slider([
        'santoker(ca,{})',
        '',
        'santokerWarmupTemp({})',
        'santoker(fa,{})',
    ]) == 2
    assert find_warmup_buttons([
        'santoker(fa,10)',
        'santokerWarmup(1 - $)',
        'santokerWarmup(0)',
    ]) == [1, 2]


def test_window_updates_only_semantic_warmup_buttons() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    style_signal = Mock()
    window = SimpleNamespace(
        extraeventsactionstrings=['santoker(fa,10)', 'santokerWarmup(1 - $)'],
        buttonStates=[0, 0],
        setExtraEventButtonStyleSignal=style_signal,
    )

    ApplicationWindow.setSantokerWarmupButtonState(cast(ApplicationWindow, window), True)

    assert window.buttonStates == [0, 1]
    style_signal.emit.assert_called_once_with(1, 'pressed')


def test_window_moves_warmup_slider_without_firing_action() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock, call

    from artisanlib.santoker_warmup import SantokerWarmupController

    slider = Mock()
    slider.blockSignals = Mock()
    window = SimpleNamespace(
        eventslidercommands=['', '', 'santokerWarmupTemp({})', ''],
        extraeventsactionstrings=['santokerWarmup(1 - $)'],
        eventslidermin=[0, 0, 100, 0],
        eventslidermax=[100, 100, 300, 100],
        qmc=SimpleNamespace(mode_tempsliders='C'),
        santokerWarmupController=SantokerWarmupController(),
        slider3=slider,
        moveslider=Mock(),
    )

    ApplicationWindow.santokerWarmupTargetChanged(cast(ApplicationWindow, window), 190.0)

    assert window.santokerWarmupController.desired_temp_c == 190.0
    assert slider.blockSignals.call_args_list == [call(True), call(False)]
    window.moveslider.assert_called_once_with(2, 190.0, forceLCDupdate=True)


@pytest.mark.parametrize(
    ('current_unit', 'expected_target'), [('C', 190), ('F', 374)]
)
def test_warmup_preset_load_initializes_after_temperature_metadata(
    current_unit: str, expected_target: int
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock, call

    from artisanlib.santoker_warmup import SantokerWarmupController

    src_dir = Path(__file__).parents[3]
    preset = src_dir / 'includes' / 'Machines' / 'Santoker' / 'X3_Master_Bluetooth.aset'
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(preset, encoding='utf-8') == [str(preset)]

    commands = parse_ini_array(config.get('Sliders', 'slidercommands'))
    min_values = [int(value) for value in parse_ini_array(config.get('Sliders', 'slidermin'))]
    max_values = [int(value) for value in parse_ini_array(config.get('Sliders', 'slidermax'))]
    temp_flags = [int(value) for value in parse_ini_array(
        config.get('Sliders', 'eventslidertemp')
    )]
    preset_unit = config.get('Sliders', 'ModeTempSliders')
    assert preset_unit == 'C'

    # 300 reproduces the value left by premature Fahrenheit clamping.
    displayed_values = [0, 0, 300, 0]
    sliders = [Mock() for _ in range(4)]
    for index, slider in enumerate(sliders):
        slider.value.side_effect = lambda index=index: displayed_values[index]

    def moveslider(index: int, value: float, *, forceLCDupdate: bool = False) -> None:
        del forceLCDupdate
        displayed_values[index] = int(round(value))

    qmc = SimpleNamespace(mode_tempsliders=current_unit)
    window = SimpleNamespace(
        slider1=sliders[0],
        slider2=sliders[1],
        slider3=sliders[2],
        slider4=sliders[3],
        eventslidercommands=commands,
        eventslidermin=min_values,
        eventslidermax=max_values,
        eventslidervalues=displayed_values,
        eventslidertemp=[0, 0, 0, 0],
        extraeventsactionstrings=['santokerWarmup(1 - $)'],
        qmc=qmc,
        santokerWarmupController=SantokerWarmupController(),
        moveslider=moveslider,
        updateSliderLCD=Mock(),
    )

    # settingsLoad() applies base limits before loading the temperature metadata.
    ApplicationWindow.updateSliderMinMax(cast(ApplicationWindow, window))
    window.eventslidertemp = temp_flags
    qmc.mode_tempsliders = preset_unit
    if current_unit == 'F':
        window.eventslidermin[2] = 212
        window.eventslidermax[2] = 572
    qmc.mode_tempsliders = current_unit
    sliders[2].blockSignals.reset_mock()

    ApplicationWindow.initializeSantokerWarmupSlider(
        cast(ApplicationWindow, window)
    )

    assert displayed_values[2] == expected_target
    assert window.santokerWarmupController.desired_temp_c == 190.0
    assert sliders[2].blockSignals.call_args_list == [call(True), call(False)]


@pytest.mark.parametrize(
    (
        'initial_unit',
        'new_unit',
        'initial_target',
        'initial_min',
        'initial_max',
        'expected_target',
        'expected_min',
        'expected_max',
    ),
    [
        ('C', 'F', 190, 100, 300, 374, 212, 572),
        ('F', 'C', 374, 212, 572, 190, 100, 300),
    ],
    ids=['celsius-to-fahrenheit', 'fahrenheit-to-celsius'],
)
def test_runtime_temperature_mode_switch_restores_warmup_target(
    qapplication: QApplication,
    initial_unit: str,
    new_unit: str,
    initial_target: int,
    initial_min: int,
    initial_max: int,
    expected_target: int,
    expected_min: int,
    expected_max: int,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from artisanlib.canvas import tgraphcanvas
    from artisanlib.santoker_warmup import SantokerWarmupController

    del qapplication
    slider_values = [10, 20, initial_target, 40]
    sliders = [QSlider(Qt.Orientation.Vertical) for _ in range(4)]
    min_values = [0, 0, initial_min, 0]
    max_values = [100, 100, initial_max, 100]
    for index, slider in enumerate(sliders):
        slider.setRange(min_values[index], max_values[index])
        slider.setValue(slider_values[index])

    slider_actions = [Mock() for _ in sliders]
    for slider, action in zip(sliders, slider_actions, strict=True):
        slider.valueChanged.connect(action)

    controller = SantokerWarmupController()
    pidcontrol = SimpleNamespace(conv2celsius=Mock(), conv2fahrenheit=Mock())
    window = SimpleNamespace(
        slider1=sliders[0],
        slider2=sliders[1],
        slider3=sliders[2],
        slider4=sliders[3],
        eventslidercommands=['', '', 'santokerWarmupTemp({})', ''],
        eventslidermin=min_values,
        eventslidermax=max_values,
        eventslidervalues=slider_values,
        eventslidertemp=[0, 0, 1, 0],
        santokerWarmupController=controller,
        pidcontrol=pidcontrol,
        updateSliderLCD=Mock(),
    )
    canvas = SimpleNamespace(
        aw=window,
        mode=new_unit,
        mode_tempsliders=initial_unit,
    )
    window.qmc = canvas
    window.moveslider = lambda index, value, forceLCDupdate=False: (
        ApplicationWindow.moveslider(
            cast(ApplicationWindow, window), index, value, forceLCDupdate
        )
    )
    window.updateSliderMinMax = lambda: ApplicationWindow.updateSliderMinMax(
        cast(ApplicationWindow, window)
    )
    window.initializeSantokerWarmupSlider = (
        lambda: ApplicationWindow.initializeSantokerWarmupSlider(
            cast(ApplicationWindow, window)
        )
    )

    tgraphcanvas.adjustTempSliders(cast(tgraphcanvas, canvas))

    assert canvas.mode_tempsliders == new_unit
    assert sliders[2].minimum() == expected_min
    assert sliders[2].maximum() == expected_max
    assert sliders[2].value() == expected_target
    assert slider_values == [10, 20, expected_target, 40]
    assert controller.desired_temp_c == 190.0
    assert [slider.value() for slider in sliders if slider is not sliders[2]] == [
        10, 20, 40
    ]
    for action in slider_actions:
        action.assert_not_called()


def test_warmup_button_uses_real_no_event_processing_path() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from artisanlib.santoker_warmup import SantokerWarmupController

    src_dir = Path(__file__).parents[3]
    preset = src_dir / 'includes' / 'Machines' / 'Santoker' / 'X3_Master_Bluetooth.aset'
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(preset, encoding='utf-8') == [str(preset)]

    actions = [int(value) for value in parse_ini_array(
        config.get('ExtraEventButtons', 'extraeventsactions')
    )]
    action_strings = parse_ini_array(
        config.get('ExtraEventButtons', 'extraeventsactionstrings')
    )
    event_types = [int(value) for value in parse_ini_array(
        config.get('ExtraEventButtons', 'extraeventstypes')
    )]
    event_values = [float(value) for value in parse_ini_array(
        config.get('ExtraEventButtons', 'extraeventsvalues')
    )]
    warmup_button = action_strings.index('santokerWarmup(1 - $)')

    slider_values = [20, 30, 190, 40]
    last_values = slider_values.copy()
    block_ticks = [0, 0, 0, 0]
    event_record_signal = Mock()
    overwrite_signal = Mock()
    eventaction = Mock()

    def moveslider(slider: int, value: float) -> None:
        slider_values[slider] = int(value)

    controller = SantokerWarmupController()
    window = SimpleNamespace(
        extraeventstypes=event_types,
        extraeventsvalues=event_values,
        extraeventsactionstrings=action_strings,
        extraeventsactions=actions,
        mark_last_button_pressed=False,
        lastbuttonpressed=-1,
        qmc=SimpleNamespace(
            eventsInternal2ExternalValue=lambda value: value,
            flagstart=True,
            eventRecordSignal=event_record_signal,
            eventRecordOverwriteValueSignal=overwrite_signal,
        ),
        eventslidermax=[100, 100, 300, 100],
        eventslidermin=[0, 0, 100, 0],
        extraeventsactionslastvalue=last_values,
        block_quantification_sampling_ticks=block_ticks,
        sampling_ticks_to_block_quantifiction=4,
        calcEventValue=lambda _event_type, value: value,
        eventaction=eventaction,
        moveslider=moveslider,
        santokerWarmupController=controller,
    )

    ApplicationWindow.recordextraevent(
        cast(ApplicationWindow, window), warmup_button, updateButtons=False
    )

    assert controller.desired_temp_c == 190.0
    assert slider_values == [20, 30, 190, 40]
    assert last_values == [20, 30, 190, 40]
    assert block_ticks == [0, 0, 0, 0]
    event_record_signal.emit.assert_not_called()
    overwrite_signal.emit.assert_not_called()
    eventaction.assert_called_once_with(
        6, 'santokerWarmup(1 - $)', parallel=True, eventtype=-1
    )


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


def test_x3_master_bluetooth_preset_contract() -> None:
    from artisanlib.santoker import Santoker

    src_dir = Path(__file__).parents[3]
    preset = src_dir / 'includes' / 'Machines' / 'Santoker' / 'X3_Master_Bluetooth.aset'
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(preset, encoding='utf-8') == [str(preset)]

    assert config.get('General', 'roastertype_setup') == 'Santoker X3 Master BT'
    assert config.getint('Device', 'id') == 134
    assert config.getboolean('Device', 'santokerBLE')

    assert parse_ini_array(config.get('ExtraDev', 'extradevices')) == ['135', '136']

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
