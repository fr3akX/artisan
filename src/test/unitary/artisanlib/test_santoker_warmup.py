from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path

import pytest


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

    from artisanlib.main import ApplicationWindow

    style_signal = Mock()
    window = SimpleNamespace(
        extraeventsactionstrings=['santoker(fa,10)', 'santokerWarmup(1 - $)'],
        buttonStates=[0, 0],
        setExtraEventButtonStyleSignal=style_signal,
    )

    ApplicationWindow.setSantokerWarmupButtonState(window, True)

    assert window.buttonStates == [0, 1]
    style_signal.emit.assert_called_once_with(1, 'pressed')


def test_window_moves_warmup_slider_without_firing_action() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock, call

    from artisanlib.main import ApplicationWindow
    from artisanlib.santoker_warmup import SantokerWarmupController

    slider = Mock()
    slider.blockSignals = Mock()
    window = SimpleNamespace(
        eventslidercommands=['', '', 'santokerWarmupTemp({})', ''],
        eventslidermin=[0, 0, 100, 0],
        eventslidermax=[100, 100, 300, 100],
        qmc=SimpleNamespace(mode_tempsliders='C'),
        santokerWarmupController=SantokerWarmupController(),
        slider3=slider,
        moveslider=Mock(),
    )

    ApplicationWindow.santokerWarmupTargetChanged(window, 190.0)

    assert window.santokerWarmupController.desired_temp_c == 190.0
    assert slider.blockSignals.call_args_list == [call(True), call(False)]
    window.moveslider.assert_called_once_with(2, 190.0, forceLCDupdate=True)


def test_window_rejected_warmup_restoration_uses_signal_from_worker_thread() -> None:
    from threading import Thread
    from types import SimpleNamespace
    from unittest.mock import Mock

    from artisanlib.main import ApplicationWindow
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
            results.append(ApplicationWindow.setSantokerWarmup(window, True))
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

    slider_commands = config.get('Sliders', 'slidercommands')
    assert 'santoker(ca,{})' in slider_commands
    assert 'santokerWarmupTemp({})' in slider_commands
    assert 'santoker(fa,{})' in slider_commands
    assert config.get('Sliders', 'eventslidertemp') == '0, 0, 1, 0'
    assert config.get('Sliders', 'slidermin') == '0, 0, 100, 0'
    assert config.get('Sliders', 'slidermax') == '100, 100, 300, 100'
    assert config.get('Sliders', 'slidervisibilities') == '1, 0, 1, 1'

    warmup_actions = config.get('ExtraEventButtons', 'extraeventsactionstrings')
    assert 'santokerWarmup(1 - $)' in warmup_actions
    assert 'WARM-UP' in config.get('ExtraEventButtons', 'extraeventslabels')

    charge_actions = config.get('DefaultButtons', 'buttonactionstrings')
    assert charge_actions.index('santokerWarmup(0)') < charge_actions.index('santoker(80,1)')

    assert Santoker.DEFAULT_WARMUP_TEMP_C == 190.0
