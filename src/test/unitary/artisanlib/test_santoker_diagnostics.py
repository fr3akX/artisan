from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import threading

import pytest

from artisanlib.santoker_diagnostics import SantokerDiagnosticsSession


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
