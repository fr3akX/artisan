#
# ABOUT
# Artisan Roast Server inventory lifecycle coordination
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# OpenAI, 2026

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from artisanlib.atypes import ProfileData
from artisanlib.roastserver.contract import ContractError, Namespace
from artisanlib.roastserver.inventory_contract import (
    BeanLot,
    InventoryBalance,
    InventoryCommandRequest,
    InventoryProfileLink,
    build_finalize_request,
    build_release_request,
    build_reserve_request,
    green_weight_grams,
    parse_profile_link,
)
from artisanlib.roastserver.inventory_store import (
    InventoryRoastState,
    InventoryStore,
    InventoryStoreError,
)


@dataclass(frozen=True, slots=True)
class InventoryContext:
    origin: str
    namespace: Namespace | None
    enabled: bool
    previously_authenticated: bool
    client_instance_uuid: UUID


@dataclass(frozen=True, slots=True)
class PreparedInventoryCharge:
    tracked: bool
    namespace: Namespace | None
    roast_uuid: UUID | None
    reservation_uuid: UUID | None
    lot_id: UUID | None
    lot_name: str | None
    planned_grams: int | None
    existing: bool


@dataclass(frozen=True, slots=True)
class InventoryNotice:
    code: str
    roast_uuid: UUID | None
    reservation_uuid: UUID | None
    lot_id: UUID | None
    balance: InventoryBalance | None
    conflict_id: UUID | None


class InventoryCoordinatorError(RuntimeError):
    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _PendingInventoryCharge:
    prepared: PreparedInventoryCharge
    client_instance_uuid: UUID
    request: InventoryCommandRequest | None = None


class InventoryCoordinator:
    def __init__(
        self,
        store: InventoryStore,
        *,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
        wake: Callable[[], None],
    ) -> None:
        self._store = store
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._wake = wake
        self._pending_charge: _PendingInventoryCharge | None = None

    def prepare_charge(
        self,
        context: InventoryContext,
        link: InventoryProfileLink | None,
        roast_uuid: UUID | None,
        green_weight: object,
        weight_unit: object,
    ) -> PreparedInventoryCharge:
        self._pending_charge = None
        if link is None:
            return PreparedInventoryCharge(
                False, None, None, None, None, None, None, False
            )
        self._require_current_namespace(context, link.namespace)
        if roast_uuid is not None and not isinstance(roast_uuid, UUID):
            raise InventoryCoordinatorError('inventory_roast_uuid_invalid')

        if roast_uuid is not None:
            existing = self._roast_state(link.namespace, roast_uuid)
            if existing is not None:
                if existing.terminal_intent is not None or existing.lifecycle in {
                    'finalized',
                    'released',
                }:
                    raise InventoryCoordinatorError('inventory_roast_terminal')
                if existing.lot_id != link.lot_id:
                    raise InventoryCoordinatorError('inventory_reservation_mismatch')
                return self._prepared_from_state(existing)

        lot = next(
            (
                item
                for item in self._cached_lots(link.namespace)
                if item.lot_id == link.lot_id
            ),
            None,
        )
        if lot is None:
            raise InventoryCoordinatorError('inventory_lot_unavailable')
        planned_grams = green_weight_grams(green_weight, weight_unit)
        if planned_grams is None:
            raise InventoryCoordinatorError('inventory_weight_invalid')

        selected_roast_uuid = roast_uuid or self._new_uuid()
        reservation_uuid = self._new_uuid()
        prepared = PreparedInventoryCharge(
            True,
            link.namespace,
            selected_roast_uuid,
            reservation_uuid,
            lot.lot_id,
            lot.name,
            planned_grams,
            False,
        )
        self._pending_charge = _PendingInventoryCharge(
            prepared, context.client_instance_uuid
        )
        return prepared

    def commit_charge(
        self, prepared: PreparedInventoryCharge
    ) -> InventoryNotice:
        if not prepared.tracked:
            self._pending_charge = None
            return InventoryNotice(
                'inventory_untracked', None, None, None, None, None
            )
        namespace, roast_uuid, reservation_uuid, lot_id, lot_name, planned_grams = (
            self._tracked_values(prepared)
        )
        existing = self._roast_state(namespace, roast_uuid)
        if existing is not None:
            self._require_prepared_matches_state(prepared, existing)
            self._pending_charge = None
            return self._notice(self._reservation_notice_code(existing), existing)

        pending = self._pending_charge
        if pending is None or pending.prepared != prepared:
            raise InventoryCoordinatorError('inventory_preparation_invalid')
        try:
            now = self._clock()
            request = pending.request
            if request is None:
                request = build_reserve_request(
                    client_instance_uuid=pending.client_instance_uuid,
                    reservation_uuid=reservation_uuid,
                    roast_uuid=roast_uuid,
                    lot_id=lot_id,
                    planned_grams=planned_grams,
                    occurred_at=now,
                )
                pending = _PendingInventoryCharge(
                    pending.prepared, pending.client_instance_uuid, request
                )
                self._pending_charge = pending
            state = self._store.enqueue_reserve(namespace, request, lot_name, now)
        except (InventoryStoreError, ValueError):
            raise InventoryCoordinatorError('inventory_storage_failed') from None
        self._pending_charge = None
        self._wake()
        return self._notice('inventory_reservation_queued', state)

    def finalize_saved_profile(
        self,
        context: InventoryContext,
        profile: ProfileData,
    ) -> InventoryNotice | None:
        mapping = cast(Mapping[str, object], profile)
        link = self._profile_link(mapping)
        namespace = context.namespace
        if (
            link is None
            or namespace is None
            or context.origin != namespace.origin
            or link.namespace != namespace
        ):
            return None
        roast_uuid = self._canonical_profile_roast_uuid(mapping.get('roastUUID'))
        if roast_uuid is None or not self._profile_has_charge(mapping):
            return None
        state = self._roast_state(namespace, roast_uuid)
        if (
            state is None
            or state.lot_id != link.lot_id
            or state.terminal_intent is not None
            or state.lifecycle in {'finalized', 'released'}
        ):
            return None

        actual_grams = self._profile_green_weight(mapping)
        try:
            now = self._clock()
            request = build_finalize_request(
                client_instance_uuid=context.client_instance_uuid,
                reservation_uuid=state.reservation_uuid,
                roast_uuid=state.roast_uuid,
                lot_id=state.lot_id,
                planned_grams=state.planned_grams,
                actual_grams=actual_grams,
                occurred_at=now,
            )
            updated = self._store.enqueue_finalize(
                namespace, request, actual_grams, now
            )
        except (InventoryStoreError, ValueError):
            raise InventoryCoordinatorError('inventory_storage_failed') from None
        self._wake()
        code = (
            'inventory_finalization_queued'
            if actual_grams is not None
            else 'inventory_planned_weight_used'
        )
        return self._notice(code, updated)

    def release_for_reset(
        self,
        context: InventoryContext,
        roast_uuid: UUID | None,
    ) -> InventoryNotice | None:
        namespace = context.namespace
        if namespace is None or roast_uuid is None or context.origin != namespace.origin:
            return None
        state = self._roast_state(namespace, roast_uuid)
        if (
            state is None
            or state.terminal_intent is not None
            or state.lifecycle in {'finalized', 'released'}
        ):
            return None
        updated = self._enqueue_release(context.client_instance_uuid, state)
        self._wake()
        return self._notice('inventory_release_queued', updated)

    def resolve_interrupted(
        self,
        context: InventoryContext,
        roast_uuid: UUID,
        action: Literal['finalize', 'release', 'keep'],
    ) -> InventoryNotice:
        if action not in {'finalize', 'release', 'keep'}:
            raise InventoryCoordinatorError('inventory_recovery_action_invalid')
        try:
            matches = tuple(
                item
                for item in self._store.interrupted_reservations()
                if item.roast_uuid == roast_uuid
            )
        except InventoryStoreError:
            raise InventoryCoordinatorError('inventory_storage_failed') from None
        if len(matches) != 1:
            raise InventoryCoordinatorError('inventory_recovery_unavailable')
        interrupted = matches[0]
        state = self._roast_state(interrupted.namespace, roast_uuid)
        if state is None or state.terminal_intent is not None:
            raise InventoryCoordinatorError('inventory_recovery_unavailable')
        if action == 'keep':
            return self._notice('inventory_recovery_kept_pending', state)
        if action == 'release':
            updated = self._enqueue_release(context.client_instance_uuid, state)
            code = 'inventory_recovery_release_queued'
        else:
            try:
                now = self._clock()
                request = build_finalize_request(
                    client_instance_uuid=context.client_instance_uuid,
                    reservation_uuid=state.reservation_uuid,
                    roast_uuid=state.roast_uuid,
                    lot_id=state.lot_id,
                    planned_grams=state.planned_grams,
                    actual_grams=None,
                    occurred_at=now,
                )
                updated = self._store.enqueue_finalize(
                    state.namespace, request, None, now
                )
            except (InventoryStoreError, ValueError):
                raise InventoryCoordinatorError('inventory_storage_failed') from None
            code = 'inventory_recovery_finalization_queued'
        self._wake()
        return self._notice(code, updated)

    def is_locked(
        self,
        namespace: Namespace,
        roast_uuid: UUID | None,
        profile_has_charge: bool,
    ) -> bool:
        if profile_has_charge:
            return True
        return roast_uuid is not None and self._roast_state(namespace, roast_uuid) is not None

    @staticmethod
    def _require_current_namespace(
        context: InventoryContext, namespace: Namespace
    ) -> None:
        if not context.enabled:
            raise InventoryCoordinatorError('connector_disabled')
        if context.namespace is None or not context.previously_authenticated:
            raise InventoryCoordinatorError('inventory_namespace_inactive')
        if (
            context.origin != context.namespace.origin
            or namespace != context.namespace
        ):
            raise InventoryCoordinatorError('inventory_namespace_stale')

    def _cached_lots(self, namespace: Namespace) -> tuple[BeanLot, ...]:
        try:
            return self._store.cached_lots(namespace)
        except InventoryStoreError:
            raise InventoryCoordinatorError('inventory_storage_failed') from None

    def _roast_state(
        self, namespace: Namespace, roast_uuid: UUID
    ) -> InventoryRoastState | None:
        try:
            return self._store.roast_state(namespace, roast_uuid)
        except (InventoryStoreError, ValueError):
            raise InventoryCoordinatorError('inventory_storage_failed') from None

    def _new_uuid(self) -> UUID:
        value = self._uuid_factory()
        if not isinstance(value, UUID):
            raise InventoryCoordinatorError('inventory_uuid_invalid')
        return value

    @staticmethod
    def _prepared_from_state(
        state: InventoryRoastState,
    ) -> PreparedInventoryCharge:
        return PreparedInventoryCharge(
            True,
            state.namespace,
            state.roast_uuid,
            state.reservation_uuid,
            state.lot_id,
            state.lot_name,
            state.planned_grams,
            True,
        )

    @staticmethod
    def _tracked_values(
        prepared: PreparedInventoryCharge,
    ) -> tuple[Namespace, UUID, UUID, UUID, str, int]:
        values = (
            prepared.namespace,
            prepared.roast_uuid,
            prepared.reservation_uuid,
            prepared.lot_id,
            prepared.lot_name,
            prepared.planned_grams,
        )
        namespace, roast_uuid, reservation_uuid, lot_id, lot_name, planned_grams = values
        if not (
            isinstance(namespace, Namespace)
            and isinstance(roast_uuid, UUID)
            and isinstance(reservation_uuid, UUID)
            and isinstance(lot_id, UUID)
            and isinstance(lot_name, str)
            and type(planned_grams) is int
        ):
            raise InventoryCoordinatorError('inventory_preparation_invalid')
        return namespace, roast_uuid, reservation_uuid, lot_id, lot_name, planned_grams

    @staticmethod
    def _require_prepared_matches_state(
        prepared: PreparedInventoryCharge, state: InventoryRoastState
    ) -> None:
        if (
            prepared.namespace != state.namespace
            or prepared.roast_uuid != state.roast_uuid
            or prepared.reservation_uuid != state.reservation_uuid
            or prepared.lot_id != state.lot_id
            or prepared.lot_name != state.lot_name
            or prepared.planned_grams != state.planned_grams
        ):
            raise InventoryCoordinatorError('inventory_reservation_mismatch')

    def _enqueue_release(
        self, client_instance_uuid: UUID, state: InventoryRoastState
    ) -> InventoryRoastState:
        try:
            now = self._clock()
            request = build_release_request(
                client_instance_uuid=client_instance_uuid,
                reservation_uuid=state.reservation_uuid,
                roast_uuid=state.roast_uuid,
                lot_id=state.lot_id,
                planned_grams=state.planned_grams,
                occurred_at=now,
            )
            return self._store.enqueue_release(state.namespace, request, now)
        except (InventoryStoreError, ValueError):
            raise InventoryCoordinatorError('inventory_storage_failed') from None

    @staticmethod
    def _profile_link(
        mapping: Mapping[str, object],
    ) -> InventoryProfileLink | None:
        try:
            return parse_profile_link(mapping)
        except ContractError:
            return None

    @staticmethod
    def _canonical_profile_roast_uuid(value: object) -> UUID | None:
        if not isinstance(value, str) or len(value) != 32:
            return None
        try:
            roast_uuid = UUID(value)
        except (ValueError, AttributeError, TypeError):
            return None
        return roast_uuid if roast_uuid.hex == value else None

    @staticmethod
    def _profile_has_charge(mapping: Mapping[str, object]) -> bool:
        value = mapping.get('timeindex')
        if not isinstance(value, list):
            return False
        timeindex = cast(list[object], value)
        return (
            bool(timeindex)
            and type(timeindex[0]) is int
            and timeindex[0] != -1
        )

    @staticmethod
    def _profile_green_weight(mapping: Mapping[str, object]) -> int | None:
        value = mapping.get('weight')
        if not isinstance(value, list):
            return None
        weight = cast(list[object], value)
        if len(weight) < 3:
            return None
        return green_weight_grams(weight[0], weight[2])

    @staticmethod
    def _reservation_notice_code(state: InventoryRoastState) -> str:
        return {
            'reserve_queued': 'inventory_reservation_queued',
            'reserved': 'inventory_reserved',
            'finalize_queued': 'inventory_finalization_queued',
            'finalized': 'inventory_finalized',
            'release_queued': 'inventory_release_queued',
            'released': 'inventory_released',
            'paused': 'inventory_paused',
            'failed': 'inventory_failed',
        }[state.lifecycle]

    @staticmethod
    def _notice(code: str, state: InventoryRoastState) -> InventoryNotice:
        return InventoryNotice(
            code,
            state.roast_uuid,
            state.reservation_uuid,
            state.lot_id,
            state.balance,
            state.conflict_id,
        )
