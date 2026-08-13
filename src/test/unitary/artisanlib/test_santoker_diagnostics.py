from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import inspect
import os
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from artisanlib.santoker_diagnostics import SantokerDiagnosticsSession
from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from artisanlib.main import ApplicationWindow


@pytest.fixture(scope='module')
def qapplication() -> Any:
    app = QApplication.instance()
    if app is None:
        return QApplication([])
    return app


def clock() -> Callable[[], datetime]:
    current = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        value = current
        current += timedelta(milliseconds=1)
        return value

    return now


def test_snapshot_is_immutable_and_events_are_incremental() -> None:
    session = SantokerDiagnosticsSession('BLE', now_utc=clock())
    session.record_connected()
    session.record_tx(b'\xee\xb5', 'warm-up request')

    full = session.view()
    incremental = session.view(after_sequence=full.events[-2].sequence)

    assert [event.sequence for event in full.events] == [1, 2, 3]
    assert [event.category for event in incremental.events] == ['tx']
    assert full.events[-1].packet == b'\xee\xb5'

    with pytest.raises(FrozenInstanceError):
        full.state.connected = False  # type: ignore[misc]


def test_retention_reports_exact_discard_count() -> None:
    session = SantokerDiagnosticsSession('serial', max_events=3)
    for value in range(5):
        session.record_event('state', f'value={value}')

    view = session.view()
    assert [event.description for event in view.events] == [
        'value=2',
        'value=3',
        'value=4',
    ]
    assert view.state.retained_event_count == 3
    assert view.state.discarded_event_count == 3
    assert '[older entries discarded: 3]' in session.format_report()


def test_report_uses_unknown_and_excludes_connection_identity() -> None:
    session = SantokerDiagnosticsSession('Wi-Fi')
    report = session.format_report()

    assert 'board: unknown' in report
    assert 'reported warm-up: unknown' in report
    assert 'host' not in report.lower()
    assert 'ip address' not in report.lower()
    assert 'password' not in report.lower()


def test_record_decoded_power_updates_state_and_emits_one_transition_per_change() -> None:
    session = SantokerDiagnosticsSession('Wi-Fi')

    session.record_decoded('power', 40)
    session.record_decoded('power', 40)
    session.record_decoded('power', 50)

    view = session.view()
    power_events = [
        event for event in view.events
        if event.category == 'state' and event.description.startswith('power:')
    ]

    assert view.state.power == 50
    assert [event.description for event in power_events] == ['power: 40', 'power: 50']


def test_record_event_is_thread_safe() -> None:
    session = SantokerDiagnosticsSession('serial')
    exceptions: list[BaseException] = []

    def worker(thread_id: int) -> None:
        for value in range(250):
            try:
                session.record_event('state', f'thread-{thread_id}-value={value}')
            except BaseException as exc:  # pragma: no cover - defensive
                exceptions.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    view = session.view()
    assert not exceptions
    assert [event.sequence for event in view.events] == list(range(1, 1002))
    assert len({event.sequence for event in view.events}) == 1001
    assert len(view.events) == 1001


def test_max_events_is_bounded_to_5000() -> None:
    with pytest.raises(ValueError, match='between 1 and 5000'):
        SantokerDiagnosticsSession('Wi-Fi', max_events=0)
    with pytest.raises(ValueError, match='between 1 and 5000'):
        SantokerDiagnosticsSession('Wi-Fi', max_events=5001)


def test_reconnect_count_tracks_disconnected_to_connected_transitions_only_after_first_connect() -> None:
    session = SantokerDiagnosticsSession('Wi-Fi')

    session.record_disconnected()
    session.record_connected()
    assert session.view().state.reconnect_count == 0

    session.record_disconnected()
    session.record_connected()
    assert session.view().state.reconnect_count == 1

    session.record_disconnected()
    session.record_connected()
    assert session.view().state.reconnect_count == 2


def test_init_rejects_naive_now_utc_clock() -> None:
    def now() -> datetime:
        return datetime(2026, 8, 13, 12, 0)  # noqa: DTZ001

    with pytest.raises(ValueError, match=r'now_utc\(\) must return timezone-aware UTC datetime values'):
        SantokerDiagnosticsSession('BLE', now_utc=now)


def test_init_rejects_non_utc_now_utc_clock() -> None:
    def now() -> datetime:
        return datetime(2026, 8, 13, 12, 0, tzinfo=timezone(timedelta(hours=2)))

    with pytest.raises(ValueError, match=r'now_utc\(\) must return timezone-aware UTC datetime values'):
        SantokerDiagnosticsSession('BLE', now_utc=now)


def test_non_utc_clock_rejected_after_initialization() -> None:
    values: list[datetime] = [
        datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 13, 12, 0, tzinfo=timezone(timedelta(hours=1))),
    ]
    index = 0

    def now() -> datetime:
        nonlocal index
        value = values[index]
        index += 1
        return value

    session = SantokerDiagnosticsSession('BLE', now_utc=now)

    with pytest.raises(ValueError, match=r'now_utc\(\) must return timezone-aware UTC datetime values'):
        session.record_connected()


@pytest.mark.parametrize(
    ('serial', 'ble', 'expected_transport'),
    [
        (False, True, 'BLE'),
        (True, False, 'serial'),
        (False, False, 'Wi-Fi'),
        (True, True, 'BLE'),
    ],
)
def test_monitoring_session_lifecycle_selects_transport_without_endpoint(
    qapplication: Any,
    serial: bool,
    ble: bool,
    expected_transport: str,
) -> None:
    del qapplication
    controller = Mock(spec=SantokerWarmupController)
    window = SimpleNamespace(
        santokerSerial=serial,
        santokerBLE=ble,
        santokerDiagnosticsSession=None,
        santokerWarmupController=controller,
    )

    session = ApplicationWindow.startSantokerDiagnosticsSession(
        cast(ApplicationWindow, window)
    )

    assert session.transport == expected_transport
    assert window.santokerDiagnosticsSession is session
    controller.attach_diagnostics.assert_called_once_with(session)
    session.record_connection_attempt()
    assert session.view().events[-1].description == 'transport start requested'


def test_monitoring_session_replacement_preserves_completed_snapshot(
    qapplication: Any,
) -> None:
    del qapplication
    previous = SantokerDiagnosticsSession('serial')
    previous.stop()
    previous_snapshot = previous.view()
    controller = Mock(spec=SantokerWarmupController)
    window = SimpleNamespace(
        santokerSerial=False,
        santokerBLE=False,
        santokerDiagnosticsSession=previous,
        santokerWarmupController=controller,
    )

    replacement = ApplicationWindow.startSantokerDiagnosticsSession(
        cast(ApplicationWindow, window)
    )

    assert replacement is not previous
    assert replacement.transport == 'Wi-Fi'
    assert previous.view() == previous_snapshot
    controller.attach_diagnostics.assert_called_once_with(replacement)


def test_automatic_disconnect_keeps_monitoring_session_and_desired_on(
    qapplication: Any,
) -> None:
    del qapplication
    device = Mock()
    device.isHeaderReady.return_value = True
    device.requestWarmupOn.return_value = True
    controller = SantokerWarmupController(desired_temp_c=205.0)
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    session = SantokerDiagnosticsSession('BLE')
    controller.attach_diagnostics(session)
    stop_monitoring = Mock()
    window = SimpleNamespace(
        santokerWarmup=True,
        santoker=device,
        santokerWarmupController=controller,
        santokerDiagnosticsSession=session,
        stopSantokerMonitoring=stop_monitoring,
    )

    ApplicationWindow.santokerWarmupReadyChanged(
        cast(ApplicationWindow, window), False
    )

    stop_monitoring.assert_not_called()
    assert window.santokerDiagnosticsSession is session
    assert session.view().state.monitoring_active
    assert controller.desired_enabled() is True
    device.requestWarmupOn.assert_called_once_with(205.0)


def test_stop_monitoring_lifecycle_orders_and_is_idempotent(qapplication: Any) -> None:
    del qapplication
    trace: list[str] = []
    controller = Mock(spec=SantokerWarmupController)
    controller.stop_monitoring.side_effect = lambda _device: trace.append('controller')
    santoker = Mock()
    santoker.stop.side_effect = lambda: trace.append('santoker')
    session = Mock(spec=SantokerDiagnosticsSession)
    session.stop.side_effect = lambda: trace.append('session')
    window = SimpleNamespace(
        santokerWarmupController=controller,
        santoker=santoker,
        santokerDiagnosticsSession=session,
    )

    ApplicationWindow.stopSantokerMonitoring(cast(ApplicationWindow, window))

    assert trace == ['controller', 'santoker', 'session']
    assert window.santoker is None
    controller.stop_monitoring.assert_called_once_with(santoker)

    ApplicationWindow.stopSantokerMonitoring(cast(ApplicationWindow, window))

    assert trace == ['controller', 'santoker', 'session', 'controller', 'session']
    santoker.stop.assert_called_once_with()


def test_canvas_monitoring_lifecycle_branch_selection(qapplication: Any) -> None:
    from artisanlib.canvas import tgraphcanvas

    del qapplication
    source = inspect.getsource(tgraphcanvas.OnMonitor)
    helper = 'self.aw.startSantokerDiagnosticsSession()'
    add_device = source.index('# ADD DEVICE:')
    helper_index = source.index(helper, add_device)
    simulator_branch = source.index('if not bool(self.aw.simulator):', add_device)
    device_branch = source.index('elif self.device == 134:', simulator_branch)
    attempt = source.index(
        'santoker_diagnostics_session.record_connection_attempt()', device_branch
    )
    construction = source.index('self.aw.santoker = Santoker(', attempt)
    start = source.index('self.aw.santoker.start()', construction)

    assert source.count(helper) == 1
    selection = source[source.rindex('santoker_diagnostics_session =', add_device, helper_index):simulator_branch]
    assert 'if self.device == 134' in selection
    assert 'else None' in selection
    assert helper_index < simulator_branch < device_branch
    assert device_branch < attempt < construction < start
    assert source[attempt:construction].strip() == (
        'santoker_diagnostics_session.record_connection_attempt()'
    )
    assert 'diagnostics=santoker_diagnostics_session' in source[construction:start]
    assert 'frame_handler=self.aw.santokerFrameSignal.emit' in source[construction:start]
    assert 'getWarmupTarget()' not in source

    stop_source = inspect.getsource(tgraphcanvas.OffMonitorCloseDown)
    assert stop_source.count('self.aw.stopSantokerMonitoring()') == 1
    assert stop_source.index('if self.device == 134:') < stop_source.index(
        'self.aw.stopSantokerMonitoring()'
    )

    shutdown_source = inspect.getsource(ApplicationWindow.stopActivities)
    assert shutdown_source.count('self.stopSantokerMonitoring()') == 1
    assert shutdown_source.index('if self.qmc.device == 134:') < shutdown_source.index(
        'self.stopSantokerMonitoring()'
    ) < shutdown_source.index('self.qmc.ToggleMonitor()')
