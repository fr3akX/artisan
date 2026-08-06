from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.contract import Namespace
from artisanlib.roastserver.inventory import (
    InventoryContext,
    InventoryCoordinator,
    InventoryCoordinatorError,
    PreparedInventoryCharge,
)
from artisanlib.roastserver.inventory_contract import (
    BeanLot,
    InventoryCommandRequest,
    InventoryProfileLink,
    profile_link_fields,
)
from artisanlib.roastserver.inventory_store import (
    InventoryRoastState,
    InventoryStore,
    InventoryStoreError,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, 123456, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
ORGANIZATION_UUID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_ORGANIZATION_UUID = UUID('22222222-2222-4222-8222-222222222222')
LOT_UUID = UUID('33333333-3333-4333-8333-333333333333')
ROAST_UUID = UUID('44444444-4444-4444-8444-444444444444')
RESERVATION_UUID = UUID('55555555-5555-4555-8555-555555555555')
CLIENT_UUID = UUID('66666666-6666-4666-8666-666666666666')
SECOND_ROAST_UUID = UUID('77777777-7777-4777-8777-777777777777')
SECOND_RESERVATION_UUID = UUID('88888888-8888-4888-8888-888888888888')
OTHER_CLIENT_UUID = UUID('99999999-9999-4999-8999-999999999999')


def namespace_for_test(origin: str, organization_uuid: UUID) -> Namespace:
    digest = hashlib.sha256(f'{origin}\n{organization_uuid}'.encode()).hexdigest()
    return Namespace(origin, organization_uuid, f'namespace-sha256:{digest}')


NAMESPACE = namespace_for_test('https://inventory.example', ORGANIZATION_UUID)
OTHER_NAMESPACE = namespace_for_test(
    'https://other.example', OTHER_ORGANIZATION_UUID
)
LINK = InventoryProfileLink(NAMESPACE, LOT_UUID, 'Test Lot')
CONTEXT = InventoryContext(
    origin=NAMESPACE.origin,
    namespace=NAMESPACE,
    enabled=True,
    previously_authenticated=True,
    client_instance_uuid=CLIENT_UUID,
)
OTHER_CONTEXT = InventoryContext(
    origin=OTHER_NAMESPACE.origin,
    namespace=OTHER_NAMESPACE,
    enabled=True,
    previously_authenticated=True,
    client_instance_uuid=CLIENT_UUID,
)


def bean_lot() -> BeanLot:
    return BeanLot(
        lot_id=LOT_UUID,
        name='Cached Lot Name',
        origin='Kenya',
        varietals=('SL28',),
        processing_method='washed',
        crop_year=2026,
        on_hand_grams=2_000,
        reserved_grams=0,
        available_grams=2_000,
        unresolved_conflict_count=0,
    )


class SequenceFactory:
    def __init__(self, *values: UUID) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> UUID:
        self.calls += 1
        return next(self._values)


class Clock:
    def __init__(self) -> None:
        self._value = NOW

    def __call__(self) -> datetime:
        result = self._value
        self._value += timedelta(seconds=1)
        return result


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InventoryStore]:
    value = InventoryStore(tmp_path / 'inventory')
    value.open()
    value.replace_lots(NAMESPACE, (bean_lot(),), NOW)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def uuid_factory() -> SequenceFactory:
    return SequenceFactory(
        ROAST_UUID,
        RESERVATION_UUID,
        SECOND_ROAST_UUID,
        SECOND_RESERVATION_UUID,
    )


@pytest.fixture
def wake_calls() -> list[None]:
    return []


@pytest.fixture
def coordinator(
    store: InventoryStore,
    uuid_factory: SequenceFactory,
    wake_calls: list[None],
) -> InventoryCoordinator:
    return InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=uuid_factory,
        wake=lambda: wake_calls.append(None),
    )


def profile(
    roast_uuid: UUID = ROAST_UUID,
    *,
    link: InventoryProfileLink = LINK,
    charge: int = 0,
    green_weight: object = 1.2,
    unit: object = 'Kg',
) -> ProfileData:
    value: dict[str, object] = {
        **profile_link_fields(link),
        'roastUUID': roast_uuid.hex,
        'timeindex': [charge],
        'weight': [green_weight, 0.0, unit],
    }
    return cast(ProfileData, value)


def assert_error(code: str, call: object) -> None:
    assert callable(call)
    with pytest.raises(InventoryCoordinatorError) as exc_info:
        call()
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def test_no_lot_is_untracked_and_returns_fixed_warning(
    coordinator: InventoryCoordinator,
    wake_calls: list[None],
) -> None:
    prepared = coordinator.prepare_charge(CONTEXT, None, None, 0, 'bogus')

    assert prepared == PreparedInventoryCharge(False, None, None, None, None, None, None, False)
    notice = coordinator.commit_charge(prepared)
    assert notice.code == 'inventory_untracked'
    assert notice.roast_uuid is None
    assert wake_calls == []


def test_selected_lot_blocks_when_connector_is_disabled(
    coordinator: InventoryCoordinator,
) -> None:
    disabled = InventoryContext(
        CONTEXT.origin,
        CONTEXT.namespace,
        False,
        CONTEXT.previously_authenticated,
        CONTEXT.client_instance_uuid,
    )
    assert_error(
        'connector_disabled',
        lambda: coordinator.prepare_charge(disabled, LINK, None, 1.0, 'Kg'),
    )


@pytest.mark.parametrize(
    ('context', 'link', 'code'),
    [
        (
            InventoryContext(NAMESPACE.origin, None, True, True, CLIENT_UUID),
            LINK,
            'inventory_namespace_inactive',
        ),
        (
            InventoryContext(NAMESPACE.origin, NAMESPACE, True, False, CLIENT_UUID),
            LINK,
            'inventory_namespace_inactive',
        ),
        (CONTEXT, InventoryProfileLink(OTHER_NAMESPACE, LOT_UUID, 'Lot'), 'inventory_namespace_stale'),
        (
            InventoryContext('https://wrong.example', NAMESPACE, True, True, CLIENT_UUID),
            LINK,
            'inventory_namespace_stale',
        ),
    ],
)
def test_selected_lot_requires_active_matching_known_namespace(
    coordinator: InventoryCoordinator,
    context: InventoryContext,
    link: InventoryProfileLink,
    code: str,
) -> None:
    assert_error(
        code,
        lambda: coordinator.prepare_charge(context, link, None, 1.0, 'Kg'),
    )


def test_selected_lot_must_exist_in_complete_cache(
    coordinator: InventoryCoordinator,
) -> None:
    missing = InventoryProfileLink(NAMESPACE, SECOND_ROAST_UUID, 'Missing')
    assert_error(
        'inventory_lot_unavailable',
        lambda: coordinator.prepare_charge(CONTEXT, missing, None, 1.0, 'Kg'),
    )


@pytest.mark.parametrize(
    ('weight', 'unit'),
    [(0, 'Kg'), (-1, 'Kg'), (float('nan'), 'Kg'), (1, 'unknown'), (True, 'Kg')],
)
def test_selected_lot_requires_valid_green_weight(
    coordinator: InventoryCoordinator,
    uuid_factory: SequenceFactory,
    weight: object,
    unit: object,
) -> None:
    assert_error(
        'inventory_weight_invalid',
        lambda: coordinator.prepare_charge(CONTEXT, LINK, None, weight, unit),
    )
    assert uuid_factory.calls == 0


def test_preparation_generates_ids_without_store_side_effects_and_offline_is_allowed(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
    uuid_factory: SequenceFactory,
) -> None:
    prepared = coordinator.prepare_charge(CONTEXT, LINK, None, 1.25, 'Kg')

    assert prepared == PreparedInventoryCharge(
        True,
        NAMESPACE,
        ROAST_UUID,
        RESERVATION_UUID,
        LOT_UUID,
        'Cached Lot Name',
        1_250,
        False,
    )
    assert uuid_factory.calls == 2
    assert store.roast_state(NAMESPACE, ROAST_UUID) is None


def test_commit_is_durable_before_wake_and_repeated_commit_is_idempotent(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
    wake_calls: list[None],
) -> None:
    prepared = coordinator.prepare_charge(CONTEXT, LINK, None, 1.25, 'Kg')
    first = coordinator.commit_charge(prepared)
    state = store.roast_state(NAMESPACE, ROAST_UUID)

    assert state is not None
    assert state.lifecycle == 'reserve_queued'
    assert first.code == 'inventory_reservation_queued'
    assert wake_calls == [None]

    second = coordinator.commit_charge(prepared)
    assert second.code == 'inventory_reservation_queued'
    assert wake_calls == [None]


def test_new_preparation_replaces_pending_snapshot_and_uses_latest_client(
    store: InventoryStore,
) -> None:
    wake_calls: list[None] = []
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(RESERVATION_UUID, SECOND_RESERVATION_UUID),
        wake=lambda: wake_calls.append(None),
    )
    client_b = replace(CONTEXT, client_instance_uuid=OTHER_CLIENT_UUID)
    older = coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    latest = coordinator.prepare_charge(client_b, LINK, ROAST_UUID, 1.25, 'Kg')

    assert_error(
        'inventory_preparation_invalid', lambda: coordinator.commit_charge(older)
    )
    coordinator.commit_charge(latest)
    command = store.lease_next(NAMESPACE, LATER, 60)
    assert command is not None
    body = json.loads(command.request_json)
    assert body['client_instance_uuid'] == OTHER_CLIENT_UUID.hex
    assert body['client_reservation_uuid'] == SECOND_RESERVATION_UUID.hex
    assert wake_calls == [None]


def test_altered_and_reconstructed_stale_preparations_are_rejected(
    store: InventoryStore,
) -> None:
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(RESERVATION_UUID, SECOND_RESERVATION_UUID),
        wake=lambda: None,
    )
    stale = coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    current = coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.5, 'Kg')
    reconstructed_stale = PreparedInventoryCharge(
        stale.tracked,
        stale.namespace,
        stale.roast_uuid,
        stale.reservation_uuid,
        stale.lot_id,
        stale.lot_name,
        stale.planned_grams,
        stale.existing,
    )

    assert_error(
        'inventory_preparation_invalid',
        lambda: coordinator.commit_charge(reconstructed_stale),
    )
    assert_error(
        'inventory_preparation_invalid',
        lambda: coordinator.commit_charge(replace(current, planned_grams=1_499)),
    )
    coordinator.commit_charge(current)


def test_untracked_prepare_and_commit_invalidate_pending_tracked_snapshot(
    store: InventoryStore,
) -> None:
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(
            RESERVATION_UUID,
            SECOND_RESERVATION_UUID,
            UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),
        ),
        wake=lambda: None,
    )
    before_untracked_prepare = coordinator.prepare_charge(
        CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg'
    )
    untracked = coordinator.prepare_charge(CONTEXT, None, None, 0, 'bogus')
    assert_error(
        'inventory_preparation_invalid',
        lambda: coordinator.commit_charge(before_untracked_prepare),
    )

    before_untracked_commit = coordinator.prepare_charge(
        CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg'
    )
    assert coordinator.commit_charge(untracked).code == 'inventory_untracked'
    assert_error(
        'inventory_preparation_invalid',
        lambda: coordinator.commit_charge(before_untracked_commit),
    )

    before_new_invalid = coordinator.prepare_charge(
        CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg'
    )
    assert_error(
        'inventory_weight_invalid',
        lambda: coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 0, 'Kg'),
    )
    assert_error(
        'inventory_preparation_invalid',
        lambda: coordinator.commit_charge(before_new_invalid),
    )


def test_store_failure_retains_exact_pending_command_for_retry(
    store: InventoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wake_calls: list[None] = []
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(RESERVATION_UUID),
        wake=lambda: wake_calls.append(None),
    )
    prepared = coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    requests: list[InventoryCommandRequest] = []
    transaction_times: list[datetime] = []
    enqueue_reserve = store.enqueue_reserve

    def fail_once(
        namespace: Namespace,
        request: InventoryCommandRequest,
        lot_name: str,
        now: datetime,
    ) -> InventoryRoastState:
        requests.append(request)
        transaction_times.append(now)
        if len(requests) == 1:
            raise InventoryStoreError('sensitive injected failure')
        return enqueue_reserve(namespace, request, lot_name, now)

    monkeypatch.setattr(store, 'enqueue_reserve', fail_once)

    assert_error(
        'inventory_storage_failed', lambda: coordinator.commit_charge(prepared)
    )
    assert wake_calls == []
    coordinator.commit_charge(prepared)

    assert len(requests) == 2
    assert requests[1] is requests[0]
    assert requests[1].request_json == requests[0].request_json
    assert requests[1].idempotency_key == requests[0].idempotency_key
    assert transaction_times == [NOW, NOW + timedelta(seconds=1)]
    command = store.lease_next(NAMESPACE, LATER, 60)
    assert command is not None
    assert command.request_json == requests[0].request_json
    assert wake_calls == [None]


def test_successful_and_durable_commits_clear_pending_snapshot(
    coordinator: InventoryCoordinator,
) -> None:
    prepared = coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    coordinator.commit_charge(prepared)
    assert coordinator._pending_charge is None

    coordinator.commit_charge(prepared)
    assert coordinator._pending_charge is None


def test_undo_and_recharge_reuse_existing_reservation(
    coordinator: InventoryCoordinator,
    uuid_factory: SequenceFactory,
) -> None:
    first = coordinator.prepare_charge(CONTEXT, LINK, None, 1.25, 'Kg')
    coordinator.commit_charge(first)
    second = coordinator.prepare_charge(CONTEXT, LINK, first.roast_uuid, 9.0, 'Kg')

    assert second.existing
    assert second.reservation_uuid == first.reservation_uuid
    assert second.planned_grams == 1_250
    assert second.lot_name == 'Cached Lot Name'
    assert uuid_factory.calls == 2


def test_finalize_queues_valid_actual_weight_once(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
    wake_calls: list[None],
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )

    notice = coordinator.finalize_saved_profile(CONTEXT, profile())
    assert notice is not None
    assert notice.code == 'inventory_finalization_queued'
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None
    assert state.actual_grams == 1_200
    assert state.terminal_intent == 'finalize'
    assert wake_calls == [None, None]

    assert coordinator.finalize_saved_profile(CONTEXT, profile()) is None
    assert wake_calls == [None, None]


@pytest.mark.parametrize(
    ('weight', 'unit'),
    [(None, 'Kg'), (0, 'Kg'), (float('inf'), 'Kg'), (1.2, 'unknown')],
)
def test_finalize_falls_back_to_planned_weight_with_fixed_warning(
    store: InventoryStore,
    weight: object,
    unit: object,
) -> None:
    wake_calls: list[None] = []
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(RESERVATION_UUID),
        wake=lambda: wake_calls.append(None),
    )
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )

    notice = coordinator.finalize_saved_profile(
        CONTEXT, profile(green_weight=weight, unit=unit)
    )
    assert notice is not None
    assert notice.code == 'inventory_planned_weight_used'
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None
    assert state.actual_grams is None
    assert wake_calls == [None, None]


def test_finalize_requires_matching_link_canonical_uuid_charge_and_local_state(
    coordinator: InventoryCoordinator,
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )
    stale = profile(link=InventoryProfileLink(OTHER_NAMESPACE, LOT_UUID, 'Lot'))
    noncanonical = cast(ProfileData, {**profile(), 'roastUUID': str(ROAST_UUID)})
    uncharged = profile(charge=-1)
    other = profile(roast_uuid=SECOND_ROAST_UUID)

    assert coordinator.finalize_saved_profile(CONTEXT, stale) is None
    assert coordinator.finalize_saved_profile(CONTEXT, noncanonical) is None
    assert coordinator.finalize_saved_profile(CONTEXT, uncharged) is None
    assert coordinator.finalize_saved_profile(CONTEXT, other) is None


def test_save_after_namespace_change_finalizes_original_profile_namespace(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )

    notice = coordinator.finalize_saved_profile(OTHER_CONTEXT, profile())

    assert notice is not None
    assert notice.code == 'inventory_finalization_queued'
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None and state.terminal_intent == 'finalize'
    assert store.roast_state(OTHER_NAMESPACE, ROAST_UUID) is None
    assert store.counts(NAMESPACE).pending == 2
    assert store.counts(OTHER_NAMESPACE).pending == 0


def test_reset_after_namespace_change_releases_unique_original_reservation(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )

    notice = coordinator.release_for_reset(OTHER_CONTEXT, ROAST_UUID)

    assert notice is not None
    assert notice.code == 'inventory_release_queued'
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None and state.terminal_intent == 'release'
    assert store.roast_state(OTHER_NAMESPACE, ROAST_UUID) is None
    assert store.counts(NAMESPACE).pending == 2
    assert store.counts(OTHER_NAMESPACE).pending == 0


def test_reset_fails_closed_when_roast_uuid_exists_in_two_namespaces(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )
    store.replace_lots(OTHER_NAMESPACE, (bean_lot(),), LATER)
    other_link = InventoryProfileLink(OTHER_NAMESPACE, LOT_UUID, 'Other Lot')
    other_coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(SECOND_RESERVATION_UUID),
        wake=lambda: None,
    )
    other_coordinator.commit_charge(
        other_coordinator.prepare_charge(
            OTHER_CONTEXT, other_link, ROAST_UUID, 1.25, 'Kg'
        )
    )

    assert_error(
        'inventory_reservation_ambiguous',
        lambda: coordinator.release_for_reset(OTHER_CONTEXT, ROAST_UUID),
    )
    state_a = store.roast_state(NAMESPACE, ROAST_UUID)
    state_b = store.roast_state(OTHER_NAMESPACE, ROAST_UUID)
    assert state_a is not None and state_a.terminal_intent is None
    assert state_b is not None and state_b.terminal_intent is None


def test_release_for_reset_queues_once_and_terminal_intents_are_exclusive(
    coordinator: InventoryCoordinator,
    store: InventoryStore,
    wake_calls: list[None],
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )

    notice = coordinator.release_for_reset(CONTEXT, ROAST_UUID)
    assert notice is not None
    assert notice.code == 'inventory_release_queued'
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None
    assert state.terminal_intent == 'release'
    assert coordinator.release_for_reset(CONTEXT, ROAST_UUID) is None
    assert coordinator.finalize_saved_profile(CONTEXT, profile()) is None
    assert wake_calls == [None, None]


def test_finalize_intent_prevents_release(
    coordinator: InventoryCoordinator,
) -> None:
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )
    assert coordinator.finalize_saved_profile(CONTEXT, profile()) is not None
    assert coordinator.release_for_reset(CONTEXT, ROAST_UUID) is None


def test_loaded_charge_and_local_reservation_are_locked(
    coordinator: InventoryCoordinator,
) -> None:
    assert coordinator.is_locked(NAMESPACE, None, True)
    assert not coordinator.is_locked(NAMESPACE, ROAST_UUID, False)

    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )
    assert coordinator.is_locked(NAMESPACE, ROAST_UUID, False)
    assert not coordinator.is_locked(OTHER_NAMESPACE, ROAST_UUID, False)


def test_cross_install_save_and_reset_are_no_ops(
    coordinator: InventoryCoordinator,
    wake_calls: list[None],
) -> None:
    assert coordinator.finalize_saved_profile(CONTEXT, profile()) is None
    assert coordinator.release_for_reset(CONTEXT, ROAST_UUID) is None
    assert wake_calls == []


@pytest.mark.parametrize(
    ('action', 'expected_code', 'expected_intent', 'wake_count'),
    [
        ('finalize', 'inventory_recovery_finalization_queued', 'finalize', 2),
        ('release', 'inventory_recovery_release_queued', 'release', 2),
        ('keep', 'inventory_recovery_kept_pending', None, 1),
    ],
)
def test_interrupted_recovery_actions_work_for_inactive_namespace(
    store: InventoryStore,
    action: str,
    expected_code: str,
    expected_intent: str | None,
    wake_count: int,
) -> None:
    wake_calls: list[None] = []
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(RESERVATION_UUID),
        wake=lambda: wake_calls.append(None),
    )
    coordinator.commit_charge(
        coordinator.prepare_charge(CONTEXT, LINK, ROAST_UUID, 1.25, 'Kg')
    )
    inactive = InventoryContext(
        OTHER_NAMESPACE.origin, None, False, False, CLIENT_UUID
    )

    notice = coordinator.resolve_interrupted(
        inactive,
        ROAST_UUID,
        cast(Literal['finalize', 'release', 'keep'], action),
    )
    assert notice.code == expected_code
    state = store.roast_state(NAMESPACE, ROAST_UUID)
    assert state is not None
    assert state.terminal_intent == expected_intent
    assert len(wake_calls) == wake_count


def test_recovery_rejects_unknown_action_with_fixed_error(
    coordinator: InventoryCoordinator,
) -> None:
    assert_error(
        'inventory_recovery_action_invalid',
        lambda: coordinator.resolve_interrupted(
            CONTEXT,
            ROAST_UUID,
            cast(Literal['finalize', 'release', 'keep'], 'discard'),
        ),
    )


def test_store_failures_use_fixed_error_and_do_not_wake(
    store: InventoryStore,
    wake_calls: list[None],
) -> None:
    coordinator = InventoryCoordinator(
        store,
        clock=Clock(),
        uuid_factory=SequenceFactory(ROAST_UUID, RESERVATION_UUID),
        wake=lambda: wake_calls.append(None),
    )
    prepared = coordinator.prepare_charge(CONTEXT, LINK, None, 1.25, 'Kg')
    store.close()

    assert_error(
        'inventory_storage_failed', lambda: coordinator.commit_charge(prepared)
    )
    assert wake_calls == []
