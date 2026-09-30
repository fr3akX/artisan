# Copyright (C) 2026 The Artisan team. AGPLv3+; see LICENSE.
"""Offscreen actual Qt presentation and application lifecycle hooks; fake IO only."""
from __future__ import annotations

import ast
from collections.abc import Iterator
import inspect
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import PyQt6.QtCore as qt_core
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QDialog, QPushButton, QWidget
import pytest

from artisanlib.main import ApplicationWindow
from artisanlib.canvas import tgraphcanvas
from artisanlib.santoker_trace_ui import TracePresentation
from test_santoker_trace_runtime import CONFIG, Rig, wait_for


@pytest.fixture(autouse=True)
def native_qt_core(monkeypatch: pytest.MonkeyPatch) -> None:
    # Other suites replace this entry during collection. Restore only for our
    # call-time metadata imports; monkeypatch restores the prior state afterward.
    monkeypatch.setitem(sys.modules, 'PyQt6.QtCore', qt_core)


@pytest.fixture(scope='module')
def app() -> QApplication:
    instance = QApplication.instance()
    assert instance is None or isinstance(instance, QApplication)
    return instance or QApplication([])


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    instance = Rig(tmp_path / 'traces')
    try:
        yield instance
    finally:
        instance.finish()


def presentation(app: QApplication, rig: Rig) -> tuple[QWidget, TracePresentation, list[tuple[int, str]]]:
    del app
    window = QWidget()
    notices: list[tuple[int, str]] = []
    ui = TracePresentation(window, rig.runtime, lambda message: notices.append((threading.get_ident(), message)))
    return window, ui, notices


def refresh(ui: TracePresentation, rig: Rig, **kwargs: Any) -> None:
    revision = rig.runtime.snapshot()[0]
    ui.previous_sessions(**kwargs)
    wait_for(lambda: rig.runtime.snapshot()[0] > revision)
    ui.poll()


def test_previous_prompt_is_nonmodal_dismiss_retains_and_never_uploads(app: QApplication, rig: Rig) -> None:
    old = rig.retained()
    new = rig.begin()
    window, ui, _ = presentation(app, rig)
    refresh(ui, rig, current_session=new.session_id)
    dialog = window.findChild(QDialog)
    assert dialog is not None and not dialog.isModal()
    assert new.status().state == 'active'
    dialog.close()
    assert (rig.root / f'{old.session_id}.gz').exists()
    assert not rig.client.entered.is_set()
    ui.poll()
    assert not rig.client.entered.is_set()
    window.close()


def test_previous_cleanup_transition_prompts_but_never_current_off(app: QApplication, rig: Rig) -> None:
    old = rig.begin()
    device = rig.end(old, complete=False)
    new = rig.begin()
    window, ui, _ = presentation(app, rig)
    refresh(ui, rig, current_session=new.session_id)
    assert window.findChild(QDialog) is None
    device.trace_cleanup_complete = True
    old.cleanup_finished()
    wait_for(lambda: old.status().state == 'sealed')
    revision = rig.runtime.snapshot()[0]
    rig.runtime.refresh()
    wait_for(lambda: rig.runtime.snapshot()[0] > revision)
    ui.poll()
    dialog = window.findChild(QDialog)
    assert dialog is not None
    dialog.close()
    rig.end(new)
    wait_for(lambda: new.status().state == 'sealed')
    ui.poll()
    assert not rig.client.entered.is_set()
    window.close()


def test_manual_upload_click_uses_fixed_old_ids_and_gui_only_notifications(app: QApplication, rig: Rig) -> None:
    old = rig.retained()
    rig.authorize()
    window, ui, notices = presentation(app, rig)
    refresh(ui, rig, manual=True)
    dialog = window.findChild(QDialog)
    assert dialog is not None
    current = rig.begin()
    upload = next(button for button in dialog.findChildren(QPushButton) if button.text() == 'Upload and delete')
    upload.click()
    wait_for(lambda: rig.client.closed)
    ui.poll()
    assert rig.client.tickets[0].session_id == old.session_id
    assert current.status().state == 'active'
    assert all(thread == threading.get_ident() for thread, _message in notices)
    window.close()


def test_shutdown_deadline_is_not_cleanup_and_does_not_release_blocked_stream(app: QApplication, rig: Rig) -> None:
    old = rig.retained()
    rig.authorize()
    rig.client.after.clear()
    assert rig.runtime.submit((old.session_id,), upload=True)
    assert rig.client.disclosed.wait(5)
    current = rig.begin()
    rig.end(current, complete=False)
    current.cleanup_timed_out()
    window, ui, notices = presentation(app, rig)
    finished: list[bool] = []
    ui.shutdown(finished.append, seconds=0)
    ui.shutdown(lambda _clean: pytest.fail('repeated shutdown replaced original callback'))
    ui.poll()
    assert finished == [False]
    assert not rig.runtime.settled
    assert rig.client.source is not None and not rig.client.source.closed
    assert any('incomplete' in message for _thread, message in notices)
    assert rig.runtime.begin(CONFIG) is None
    window.close()


def test_shutdown_success_waits_for_worker_and_reports_true_barrier(app: QApplication, rig: Rig) -> None:
    rig.retained()
    window, ui, notices = presentation(app, rig)
    finished: list[bool] = []
    ui.shutdown(finished.append)
    wait_for(lambda: rig.runtime.settled)
    ui.poll()
    ui.poll()
    assert finished == [True]
    assert not any('Shutdown will continue' in message for _thread, message in notices)
    window.close()


@pytest.mark.parametrize(('device', 'ble', 'simulator'), [(134, False, None), (1, True, None), (134, True, object())])
def test_begin_capture_excludes_nonble_otherdevice_and_simulator(rig: Rig, device: int, ble: bool, simulator: object) -> None:
    window = SimpleNamespace(qmc=SimpleNamespace(device=device), santokerBLE=ble, simulator=simulator,
                             santokerTraceRuntime=rig.runtime)
    assert ApplicationWindow.beginSantokerTrace(window) is None  # type: ignore[arg-type]
    assert not rig.handles
    assert rig.runtime.cleanup_complete


def test_begin_config_failure_does_not_escape_into_device_control(rig: Rig) -> None:
    window = SimpleNamespace(qmc=SimpleNamespace(device=134, delay=0, mode='C'), santokerBLE=True, simulator=None,
                             santokerTraceRuntime=rig.runtime)
    assert ApplicationWindow.beginSantokerTrace(window) is None  # type: ignore[arg-type]
    assert 'capture' in rig.runtime.snapshot()[5]


def test_off_order_retains_exact_device_and_repeated_off_is_safe(rig: Rig) -> None:
    calls: list[str] = []
    handle = rig.begin()

    class Device:
        trace_enabled = True
        trace_cleanup_complete = False

        def request_trace_close(self) -> None:
            calls.append('close')
            handle.request_close()

        def stop(self) -> None:
            calls.append('stop')

    device = Device()
    window = SimpleNamespace(santoker=device, santokerTraceRuntime=rig.runtime, santokerDiagnosticsSession=None,
        santokerWarmupController=SimpleNamespace(stop_monitoring=lambda _device: calls.append('safety')))
    ApplicationWindow.stopSantokerMonitoring(window)  # type: ignore[arg-type]
    assert calls == ['close', 'safety', 'stop']
    assert window.santoker is None
    assert not rig.runtime.cleanup_complete
    ApplicationWindow.stopSantokerMonitoring(window)  # type: ignore[arg-type]
    assert calls.count('close') == calls.count('stop') == 1
    device.trace_cleanup_complete = True
    handle.cleanup_finished()


def test_global_ble_close_requires_all_actual_owners_even_with_true_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    from artisanlib import ble_port
    from artisanlib.santoker_ble_trace import SantokerBLETrace

    close = Mock()
    exit_app = Mock()
    monkeypatch.setattr(ble_port.ble, 'close', close)
    monkeypatch.setattr(QApplication, 'exit', exit_app)
    monkeypatch.setattr(SantokerBLETrace, 'owners_cleanup_complete', classmethod(lambda _cls: False))
    ApplicationWindow.finishTraceShutdown(True)
    ApplicationWindow.finishTraceShutdown(False)
    close.assert_not_called()
    assert exit_app.call_count == 2
    monkeypatch.setattr(SantokerBLETrace, 'owners_cleanup_complete', classmethod(lambda _cls: True))
    ApplicationWindow.finishTraceShutdown(True)
    close.assert_called_once()


def test_repeated_menu_and_window_close_stay_deferred() -> None:
    window = SimpleNamespace(traceShutdownPending=True)
    window.closeApp = lambda: ApplicationWindow.closeApp(window)  # type: ignore[arg-type]
    assert not window.closeApp()
    ApplicationWindow.fileQuit(window)  # type: ignore[arg-type]
    event = QCloseEvent()
    ApplicationWindow.closeEvent(window, event)  # type: ignore[arg-type]
    assert not event.isAccepted()


def test_source_hooks_capture_before_connection_and_roast_boundary_after_on() -> None:
    monitor = inspect.getsource(tgraphcanvas.OnMonitor)
    assert monitor.index('trace_handle = self.aw.beginSantokerTrace()') < monitor.index('self.aw.santoker = Santoker(')
    assert monitor.index('self.aw.santoker.start()') < monitor.index('previous_sessions(')
    start = inspect.getsource(tgraphcanvas.OnRecorder)
    assert start.index('self.flagstart = True') < start.index('self.OnMonitor()') < start.index("markSantokerTrace('roast_start')")
    stop = inspect.getsource(tgraphcanvas.OffRecorder)
    assert stop.index('self.flagstart = False') < stop.index("markSantokerTrace('roast_end')")
    # Milestone hooks live in successful set branches, never undo assignments.
    tree = ast.parse(inspect.getsource(tgraphcanvas))
    names = {'dry_end', 'fc_start', 'fc_end', 'sc_start', 'sc_end', 'cool_end'}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'markSantokerTrace' and node.args and isinstance(node.args[0], ast.Constant)]
    assert names <= {node.args[0].value for node in calls}  # type: ignore[attr-defined]


@pytest.mark.parametrize('traced', [False, True])
def test_first_close_preserves_no_trace_exit_and_defers_trace_exit(app: QApplication, traced: bool) -> None:
    del app
    runtime = Mock()
    ui = Mock() if traced else None
    finish = Mock()
    window = SimpleNamespace(traceShutdownPending=False, quitAction=Mock(),
        qmc=SimpleNamespace(safesaveflag=False, checkSaved=lambda: True, flagKeepON=True, roastUUID=None),
        santokerTracePresentation=ui, santokerTraceRuntime=runtime, roastserver_controller=None,
        cleanUpRoastServerInventoryPresentation=Mock(), releaseRoastServerInventory=lambda _id: True,
        stopActivities=Mock(), closeEventSettings=Mock(), finishTraceShutdown=finish)
    assert ApplicationWindow.closeApp(window) is (not traced)  # type: ignore[arg-type]
    window.stopActivities.assert_called_once()
    window.closeEventSettings.assert_called_once()
    if traced:
        assert window.traceShutdownPending
        runtime.shutdown.assert_called_once()
        assert ui is not None
        ui.shutdown.assert_called_once_with(finish)
        finish.assert_not_called()
        assert not ApplicationWindow.closeApp(window)  # type: ignore[arg-type]
        window.stopActivities.assert_called_once()
    else:
        finish.assert_called_once_with(True)
        runtime.shutdown.assert_not_called()


@pytest.mark.parametrize('direct_start', [False, True])
def test_actual_begin_helper_marks_direct_start_once_before_followup_hook(rig: Rig, direct_start: bool) -> None:
    window = SimpleNamespace(qmc=SimpleNamespace(device=134, delay=1000, mode='F', flagstart=direct_start),
        santokerBLE=True, simulator=None, santokerTraceRuntime=rig.runtime)
    handle = ApplicationWindow.beginSantokerTrace(window)  # type: ignore[arg-type]
    assert handle is not None
    rig.handles.append(handle)
    rig.runtime.milestone('roast_start')  # OnRecorder hook (idempotent for direct START)
    rig.end(handle)
    wait_for(lambda: handle.status().state == 'sealed')
    import gzip
    import json
    records = [json.loads(line) for line in gzip.decompress((rig.root / f'{handle.session_id}.gz').read_bytes()).splitlines()]
    assert records[0]['temperature_unit'] == 'F'
    assert [r['name'] for r in records if r['kind'] == 'milestone'] == ['roast_start', 'roast_end']


def test_repeated_on_cannot_replace_active_santoker_and_shutdown_rejects_start() -> None:
    canvas = SimpleNamespace(device=134, flagon=True, aw=SimpleNamespace(santokerBLE=True))
    tgraphcanvas.OnMonitor(canvas)  # type: ignore[arg-type]
    closing_canvas = SimpleNamespace(aw=SimpleNamespace(traceShutdownPending=True))
    tgraphcanvas.OnRecorder(closing_canvas)  # type: ignore[arg-type]


def test_drop_zero_or_undo_state_does_not_emit_milestone(rig: Rig) -> None:
    handle = rig.begin()
    rig.runtime.milestone('roast_start')
    window = SimpleNamespace(qmc=SimpleNamespace(timeindex=[-1, 0, 0, 0, 0, 0, 0, 0]),
                             santokerTraceRuntime=rig.runtime)
    ApplicationWindow.markSantokerDrop(window)  # type: ignore[arg-type]
    rig.end(handle)
    wait_for(lambda: handle.status().state == 'sealed')
    import gzip
    import json
    events = [json.loads(line) for line in gzip.decompress((rig.root / f'{handle.session_id}.gz').read_bytes()).splitlines()]
    assert [e['name'] for e in events if e['kind'] == 'milestone'] == ['roast_start', 'roast_end']
