import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from PyQt6.QtGui import QClipboard
from PyQt6.QtWidgets import (
    QApplication,
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
    from PyQt6.QtWidgets import QLabel

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
) -> None:
    _ = qapplication
    device_dialog = QWidget()
    device_dialog.okEvent = Mock()
    device_dialog.cancelEvent = Mock()
    device_dialog.accept = Mock()
    device_dialog.reject = Mock()
    aw = SimpleNamespace(showSantokerDiagnostics=Mock())

    button = create_santoker_diagnostics_button(aw.showSantokerDiagnostics, device_dialog)
    button.click()

    aw.showSantokerDiagnostics.assert_called_once_with()
    device_dialog.okEvent.assert_not_called()
    device_dialog.cancelEvent.assert_not_called()
    device_dialog.accept.assert_not_called()
    device_dialog.reject.assert_not_called()


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

    assert '[older entries discarded: 2]' in _value_label_text(dialog, 'valueDiscarded')
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
