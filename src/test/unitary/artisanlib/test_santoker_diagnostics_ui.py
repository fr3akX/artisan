import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, Mock

import pytest

from PyQt6.QtGui import QClipboard
from PyQt6.QtWidgets import (
    QApplication,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QWidget,
)

from artisanlib.devices import DeviceAssignmentDlg
from artisanlib.main import ApplicationWindow
from artisanlib.santoker_diagnostics import SantokerDiagnosticsSession
from artisanlib.santoker_diagnostics_ui import (
    SantokerDiagnosticsDialog,
    create_santoker_diagnostics_button,
)


def _normalize_text(value: str) -> str:
    return value.replace('\r\n', '\n').replace('\r', '\n')


def _value_label_text(dialog: SantokerDiagnosticsDialog, object_name: str) -> str:
    label = dialog.findChild(QLabel, object_name)
    if label is None:
        raise AssertionError(f'expected label: {object_name}')
    return label.text()


def _format_event_lines(session: SantokerDiagnosticsSession) -> list[str]:
    lines: list[str] = []
    for event in session.view().events:
        line = f'{event.sequence:>6} {event.timestamp_utc.isoformat()} {event.category}'
        if event.direction is not None:
            line += f' {event.direction}'
        line += f': {event.description}'
        if event.packet is not None:
            packet_text = ' '.join(f'{value:02X}' for value in event.packet)
            if packet_text:
                line += f' {packet_text}'
        lines.append(line)
    return lines


def _device_assignment_app_mock() -> MagicMock:
    aw = MagicMock()
    aw.qmc = MagicMock()
    aw.ser = MagicMock()
    aw.app.artisanviewerMode = True

    for name in (
        'santokerSerial',
        'santokerBLE',
        'kaleidoSerial',
        'scale1_dedicated_for_green_only',
        'scale2_dedicated_for_roasted_only',
        'taskWebDisplayGreenActive',
        'taskWebDisplayRoastedActive',
        'two_bucket_mode',
    ):
        setattr(aw, name, False)
    for name in (
        'container1_idx',
        'container2_idx',
        'scale1_model',
        'scale2_model',
        'kaleidoPort',
        'mugmaPort',
        'colorTrack_mean_window_size',
        'colorTrack_median_window_size',
        'green_task_precision',
        'automatic_registration_period',
        'taskWebDisplayGreenPort',
        'taskWebDisplayRoastedPort',
    ):
        setattr(aw, name, 0)
    for name in (
        'scale1_name',
        'scale1_id',
        'scale2_name',
        'scale2_id',
        'kaleidoHost',
        'mugmaHost',
        'roasthubs_org_id',
        'roasthubs_machine_id',
        'roasthubs_token',
        'shelly_3EMPro_host',
        'shelly_PlusPlug_host',
        'locale_str',
        'taskWebDisplayRoastedIndexPath',
    ):
        setattr(aw, name, '')
    aw.santokerHost = 'configured.local'
    aw.santokerPort = 1234
    aw.santokerEventFlags = [False] * 7
    aw.kaleidoEventFlags = []
    aw.nLCDS = 10
    aw.ETname = 'ET'
    aw.BTname = 'BT'

    qmc = aw.qmc
    for name in (
        'BTcurve',
        'BTlcd',
        'Controlbuttonflag',
        'ETcurve',
        'ETlcd',
        'PIDbuttonflag',
        'device_logging',
        'phidget1045_async',
        'phidget1200_async',
        'phidget1200_2_async',
        'phidgetRemoteFlag',
        'phidgetRemoteOnlyFlag',
        'yoctoRemoteFlag',
    ):
        setattr(qmc, name, False)
    for name in (
        'ambientHumiditySource',
        'ambientPressureSource',
        'ambientTempSource',
        'ambient_humidity_device',
        'ambient_pressure_device',
        'ambient_temperature_device',
        'device',
        'elevation',
        'phidget1045_changeTrigger',
        'phidget1045_dataRate',
        'phidget1046_dataRate',
        'phidget1200_2_changeTrigger',
        'phidget1200_2_dataRate',
        'phidget1200_2_formula',
        'phidget1200_2_wire',
        'phidget1200_changeTrigger',
        'phidget1200_dataRate',
        'phidget1200_formula',
        'phidget1200_wire',
        'phidgetDAQ1400_inputMode',
        'phidgetDAQ1400_powerSupply',
        'phidgetPort',
        'YOCTO_dataRate',
    ):
        setattr(qmc, name, 0)
    qmc.phidget1045_emissivity = 0.0
    qmc.YOCTO_emissivity = 0.0
    for name in ('BTfunction', 'ETfunction', 'phidgetPassword', 'phidgetServerID', 'yoctoServerID'):
        setattr(qmc, name, '')
    qmc.devices = ['Dummy']
    qmc.extradevices = []
    qmc.device_name_subst.side_effect = lambda value: value

    string_lists = (
        'YOCTO_dataRatesStrings',
        'humiditydevicefunctionlist',
        'phidget1018_changeTriggersStrings',
        'phidget1045_changeTriggersStrings',
        'phidget1046_formulaValues',
        'phidget1046_gainValues',
        'phidget1048_changeTriggersStrings',
        'phidget1200_changeTriggersStrings',
        'phidget1200_dataRatesStrings',
        'phidget1200_formulaValues',
        'phidget1200_wireValues',
        'phidgetDAQ1400_inputModeStrings',
        'phidgetDAQ1400_powerSupplyStrings',
        'phidgetVCP100x_voltageRangeStrings',
        'phidget_dataRatesStrings',
        'pressuredevicefunctionlist',
        'temperaturedevicefunctionlist',
    )
    for name in string_lists:
        setattr(qmc, name, ['0'])
    value_lists = (
        'YOCTO_dataRatesValues',
        'phidget1018_changeTriggersValues',
        'phidget1045_changeTriggersValues',
        'phidget1048_changeTriggersValues',
        'phidget1200_changeTriggersValues',
        'phidget1200_dataRatesValues',
        'phidgetVCP100x_voltageRangeValues',
        'phidget_dataRatesValues',
    )
    for name in value_lists:
        setattr(qmc, name, [0])
    qmc.phidget1018_async = [False] * 8
    qmc.phidget1018_ratio = [False] * 8
    qmc.phidget1018_changeTriggers = [0] * 8
    qmc.phidget1018_dataRates = [0] * 8
    qmc.phidgetVCP100x_voltageRanges = [0] * 8
    qmc.phidget1046_async = [False] * 4
    qmc.phidget1048_async = [False] * 4
    qmc.phidget1046_formula = [0] * 4
    qmc.phidget1046_gain = [1] * 4
    qmc.phidget1048_types = [1] * 4
    qmc.YOCTO_async = [False]

    aw.ser.externalprogram = ''
    aw.ser.externaloutprogram = ''
    aw.ser.externaloutprogramFlag = False
    aw.ser.controlETpid = [0, 1]
    aw.ser.readBTpid = [2, 1]
    aw.ser.showFujiLCDs = False
    aw.ser.useModbusPort = False
    aw.ser.arduinoETChannel = 'None'
    aw.ser.arduinoBTChannel = 'None'
    aw.ser.arduinoATChannel = 'None'
    aw.ser.ArduinoFILT = [0] * 4
    return aw


class _SessionViewFailure:
    def view(self, _after_sequence: int = 0) -> None:
        raise RuntimeError('view failed')


@pytest.fixture(scope='module')
def qapplication() -> QApplication:
    app = QApplication.instance()
    if app is None:
        created = QApplication([])
        yield created
        created.quit()
        return
    yield cast(QApplication, app)


def test_no_session_dialog_is_read_only(qapplication: QApplication) -> None:
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: None)
    dialog.show()
    qapplication.processEvents()

    assert 'No Santoker monitoring session captured' in dialog.history.toPlainText()
    assert dialog.history.isReadOnly()
    assert not dialog.findChildren(QLineEdit)
    assert {
        button.text() for button in dialog.findChildren(QPushButton)
    } == {
        'Copy All',
        'Save as Text…',
        'Close',
    }


def test_button_factory_invokes_callback_once(qapplication: QApplication) -> None:
    _ = qapplication
    callback = Mock()
    button = create_santoker_diagnostics_button(callback)
    button.click()

    callback.assert_called_once_with()


def test_diagnostics_ownership_reuses_and_reopens_completed_session(
    qapplication: QApplication,
) -> None:
    window = QMainWindow()
    window.santokerDiagnosticsSession = None
    window.santokerDiagnosticsDialog = None

    ApplicationWindow.showSantokerDiagnostics(window)
    first_dialog = window.santokerDiagnosticsDialog
    assert first_dialog is not None
    assert first_dialog.isVisible()

    ApplicationWindow.showSantokerDiagnostics(window)
    assert window.santokerDiagnosticsDialog is first_dialog
    assert first_dialog.isVisible()

    first_dialog.close()
    qapplication.processEvents()
    assert not first_dialog.isVisible()

    session = SantokerDiagnosticsSession('Wi-Fi')
    session.record_event('state', 'completed session history')
    session.stop()
    window.santokerDiagnosticsSession = session

    ApplicationWindow.showSantokerDiagnostics(window)
    qapplication.processEvents()

    assert window.santokerDiagnosticsDialog is first_dialog
    assert first_dialog.isVisible()
    assert 'completed session history' in first_dialog.history.toPlainText()


def test_diagnostics_ownership_follows_new_session(qapplication: QApplication) -> None:
    first_session = SantokerDiagnosticsSession('serial')
    window = QMainWindow()
    window.santokerDiagnosticsSession = first_session
    window.santokerDiagnosticsDialog = None

    ApplicationWindow.showSantokerDiagnostics(window)
    dialog = window.santokerDiagnosticsDialog

    second_session = SantokerDiagnosticsSession('BLE')
    second_session.record_event('state', 'new session history')
    window.santokerDiagnosticsSession = second_session
    dialog.refresh()
    qapplication.processEvents()

    assert window.santokerDiagnosticsDialog is dialog
    assert _value_label_text(dialog, 'valueTransport') == 'BLE'
    assert 'new session history' in dialog.history.toPlainText()


def test_diagnostics_entry_button_does_not_close_device_config(
    qapplication: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qapplication
    ok_event = Mock()
    cancel_event = Mock()
    accept = Mock()
    reject = Mock()
    monkeypatch.setattr(DeviceAssignmentDlg, 'okEvent', ok_event)
    monkeypatch.setattr(DeviceAssignmentDlg, 'cancelEvent', cancel_event)
    monkeypatch.setattr(DeviceAssignmentDlg, 'accept', accept)
    monkeypatch.setattr(DeviceAssignmentDlg, 'reject', reject)

    aw = _device_assignment_app_mock()
    parent = QWidget()
    device_dialog = DeviceAssignmentDlg(parent, aw)
    button = device_dialog.santokerDiagnosticsButton
    santoker_group = next(
        group for group in device_dialog.findChildren(QGroupBox) if group.title() == 'Santoker'
    )
    santoker_layout = santoker_group.layout()
    assert santoker_layout is not None

    device_dialog.santokerHost.setText('pending.local')
    device_dialog.santokerPort.setText('9999')
    device_dialog.santokerEventFlags[0].setChecked(True)
    transport_buttons = (
        device_dialog.santokerSerialFlag,
        device_dialog.santokerNetworkFlag,
        device_dialog.santokerBLEFlag,
    )
    for transport_button in transport_buttons:
        transport_button.blockSignals(True)
    device_dialog.santokerSerialFlag.setChecked(True)
    for transport_button in transport_buttons:
        transport_button.blockSignals(False)

    assert device_dialog.santokerHost.text() == 'pending.local'
    assert device_dialog.santokerPort.text() == '9999'
    assert device_dialog.santokerSerialFlag.isChecked()
    assert device_dialog.santokerEventFlags[0].isChecked()
    assert button.isEnabled()

    button.click()

    aw.showSantokerDiagnostics.assert_called_once_with()
    assert button.parentWidget() is santoker_group
    assert santoker_layout.indexOf(button) >= 0
    ok_event.assert_not_called()
    cancel_event.assert_not_called()
    accept.assert_not_called()
    reject.assert_not_called()
    assert aw.santokerHost == 'configured.local'
    assert aw.santokerPort == 1234
    assert not aw.santokerSerial
    assert not aw.santokerBLE
    assert aw.santokerEventFlags == [False] * 7


def test_diagnostics_entry_is_only_in_santoker_group() -> None:
    source = inspect.getsource(DeviceAssignmentDlg.__init__)

    assert 'self.aw.showSantokerDiagnostics' in source
    assert 'santokerVBox.addWidget(self.santokerDiagnosticsButton)' in source
    assert source.count('addWidget(self.santokerDiagnosticsButton)') == 1


def test_diagnostics_shutdown_closes_timer_and_preserves_completed_session(
    qapplication: QApplication,
) -> None:
    session = SantokerDiagnosticsSession('Wi-Fi')
    session.stop()
    window = QMainWindow()
    window.santokerDiagnosticsSession = session
    window.santokerDiagnosticsDialog = SantokerDiagnosticsDialog(window, lambda: session)
    window.santokerDiagnosticsDialog.show()
    qapplication.processEvents()
    assert window.santokerDiagnosticsDialog._refresh_timer.isActive()

    window.scale_manager = SimpleNamespace(
        disconnect_all_signal=SimpleNamespace(emit=Mock()),
        disconnect_all_slot=Mock(),
    )
    window.full_screen_mode_active = False
    window.qmc = SimpleNamespace(
        device=-1,
        flagon=False,
        flagsamplingthreadrunning=False,
        phidgetManager=None,
    )
    window.simulator = object()
    window.WebLCDs = False
    window.taskWebDisplayGreenActive = False
    window.taskWebDisplayRoastedActive = False
    window.scheduleFlag = False
    window.schedule_window = None
    window.LargeLCDsFlag = False
    window.LargeDeltaLCDsFlag = False
    window.LargePIDLCDsFlag = False
    window.LargeScaleLCDsFlag = False
    window.LargeExtraLCDsFlag = False
    window.LargePhasesLCDsFlag = False
    window.comparator = None
    window.ser = SimpleNamespace(R1=None)
    window.closeserialports = Mock()

    ApplicationWindow.stopActivities(window)
    qapplication.processEvents()

    assert not window.santokerDiagnosticsDialog.isVisible()
    assert not window.santokerDiagnosticsDialog._refresh_timer.isActive()
    assert window.santokerDiagnosticsSession is session
    assert session.view().state.session_ended_utc is not None


def test_refresh_appends_history_incrementally(qapplication: QApplication) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('Wi-Fi')
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)

    session.record_event('state', 'first state change')
    dialog.refresh()
    baseline = _normalize_text(dialog.history.toPlainText())

    session.record_event('state', 'second state change')
    dialog.refresh()

    after = _normalize_text(dialog.history.toPlainText())
    baseline_lines = baseline.splitlines()
    after_lines = after.splitlines()

    assert len(after_lines) == len(baseline_lines) + 1
    assert len([line for line in after_lines if 'first state change' in line]) == 1
    assert len([line for line in after_lines if 'second state change' in line]) == 1


def test_refresh_rebuilds_retained_history_when_evicted(qapplication: QApplication) -> None:
    session = SantokerDiagnosticsSession('serial', max_events=2)
    session.record_event('state', 'first')
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)

    dialog.refresh()
    dialog.close()
    qapplication.processEvents()

    session.record_event('state', 'second')
    session.record_event('state', 'third')

    dialog.show()
    qapplication.processEvents()
    dialog.refresh()

    assert _value_label_text(dialog, 'valueDiscarded') == '[older entries discarded: 2]'
    assert _normalize_text(dialog.history.toPlainText()).splitlines() == _format_event_lines(session)


def test_dialog_shows_all_sections_and_unknown_fields(qapplication: QApplication) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('Wi-Fi')
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)
    dialog.refresh()

    assert _value_label_text(dialog, 'valueMonitoring')
    assert _value_label_text(dialog, 'valueTransport') == 'Wi-Fi'
    assert _value_label_text(dialog, 'valueConnected')
    assert _value_label_text(dialog, 'valueReady')
    assert _value_label_text(dialog, 'valueHeader')
    assert _value_label_text(dialog, 'valueReconnectCount')
    assert _value_label_text(dialog, 'valueLastPacket')
    assert _value_label_text(dialog, 'valueBoard')
    assert _value_label_text(dialog, 'valueBean')
    assert _value_label_text(dialog, 'valueEnvironment')
    assert _value_label_text(dialog, 'valueInfrared')
    assert _value_label_text(dialog, 'valueBeanRor')
    assert _value_label_text(dialog, 'valueEnvironmentRor')
    assert _value_label_text(dialog, 'valuePower')
    assert _value_label_text(dialog, 'valueFan')
    assert _value_label_text(dialog, 'valueDrum')
    assert _value_label_text(dialog, 'valueDesiredWarmup')
    assert _value_label_text(dialog, 'valueDesiredWarmupTarget')
    assert _value_label_text(dialog, 'valueReportedWarmup')
    assert _value_label_text(dialog, 'valueReportedWarmupTarget')
    assert _value_label_text(dialog, 'valueRestoration')
    assert _value_label_text(dialog, 'valueChargeLatch')

    for key in (
        'valueBoard',
        'valueBean',
        'valueEnvironment',
        'valueInfrared',
        'valueBeanRor',
        'valueEnvironmentRor',
        'valuePower',
        'valueFan',
        'valueDrum',
        'valueDesiredWarmup',
        'valueDesiredWarmupTarget',
        'valueReportedWarmup',
        'valueReportedWarmupTarget',
    ):
        assert 'unknown' in _value_label_text(dialog, key).lower()


def test_refresh_handles_session_provider_exception(qapplication: QApplication) -> None:
    _ = qapplication
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(
        parent,
        lambda: (_ for _ in ()).throw(RuntimeError('provider failed')),
    )
    dialog.show()
    dialog.refresh()

    assert QApplication.translate('Message', 'Diagnostics unavailable') in dialog.history.toPlainText()
    assert dialog.isVisible()


def test_refresh_handles_view_failure(qapplication: QApplication) -> None:
    _ = qapplication
    parent = QWidget()

    def session_provider() -> _SessionViewFailure:
        return _SessionViewFailure()

    dialog = SantokerDiagnosticsDialog(parent, session_provider)
    dialog.show()
    dialog.refresh()

    assert QApplication.translate('Message', 'Diagnostics unavailable') in dialog.history.toPlainText()
    assert dialog.isVisible()
    assert all(child.isEnabled() for child in dialog.findChildren(QPushButton))


def test_copy_all_writes_normalized_report_to_clipboard(
    qapplication: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('BLE')
    session.record_event('state', 'copy-check')
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)
    expected = _normalize_text(session.format_report())
    clipboard = Mock(spec=QClipboard)

    monkeypatch.setattr(QApplication, 'clipboard', lambda: clipboard)
    dialog._copy_all()

    clipboard.setText.assert_called_once_with(expected)


def test_copy_reports_error_on_formatter_failure(
    qapplication: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('BLE')
    monkeypatch.setattr(
        session,
        'format_report',
        lambda: (_ for _ in ()).throw(RuntimeError('format failed')),
    )
    warning = Mock()
    monkeypatch.setattr(QMessageBox, 'warning', warning)

    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)
    dialog._copy_all()
    warning.assert_called_once()


def test_save_as_text_is_cancelled_without_writes(
    qapplication: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('BLE')
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)
    write_mock = Mock()

    monkeypatch.setattr(
        'PyQt6.QtWidgets.QFileDialog.getSaveFileName',
        lambda *_args, **_kwargs: ('', ''),
    )
    monkeypatch.setattr(Path, 'write_text', write_mock)

    dialog._save_as_text()

    write_mock.assert_not_called()


def test_save_as_text_writes_utf8_normalized_report(
    qapplication: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('BLE')
    target = tmp_path / 'diagnostics.txt'
    expected = 'line1\nline2\n'

    monkeypatch.setattr(
        session,
        'format_report',
        lambda: 'line1\r\nline2\r\n',
    )
    monkeypatch.setattr(
        'PyQt6.QtWidgets.QFileDialog.getSaveFileName',
        lambda *_args, **_kwargs: (str(target), 'Text Files (*.txt)'),
    )

    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)
    dialog._save_as_text()

    saved_bytes = target.read_bytes()
    assert saved_bytes == expected.encode()
    assert b'\r' not in saved_bytes


def test_save_as_text_reports_disk_full_without_losing_session(
    qapplication: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qapplication
    session = SantokerDiagnosticsSession('serial')
    session.record_event('state', 'state-change')
    target = tmp_path / 'report.txt'
    initial = session.format_report()
    warning = Mock()

    monkeypatch.setattr(
        'PyQt6.QtWidgets.QFileDialog.getSaveFileName',
        lambda *_args, **_kwargs: (str(target), 'Text Files (*.txt)'),
    )
    monkeypatch.setattr(QMessageBox, 'warning', warning)
    monkeypatch.setattr(Path, 'write_text', Mock(side_effect=OSError('disk full')))

    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: session)
    dialog._save_as_text()
    warning.assert_called_once()
    assert session.format_report() == initial
