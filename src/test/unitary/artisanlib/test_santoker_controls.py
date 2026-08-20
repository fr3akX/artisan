from dataclasses import dataclass, field

import pytest

from artisanlib.santoker_controls import (
    AIR,
    DRUM,
    POWER,
    ControlReconcileOutcome,
    SantokerControlController,
)


@dataclass
class FakeControlDevice:
    ready: bool = True
    power: int = 70
    air: int = 80
    drum: int = 30
    power_fresh: bool = True
    air_fresh: bool = True
    drum_fresh: bool = True
    writes_succeed: bool = True
    calls: list[tuple[bytes, int]] = field(default_factory=list)

    def isHeaderReady(self) -> bool:
        return self.ready

    def getPower(self) -> int:
        return self.power

    def isPowerFresh(self) -> bool:
        return self.power_fresh

    def getAir(self) -> int:
        return self.air

    def isAirFresh(self) -> bool:
        return self.air_fresh

    def getDrum(self) -> int:
        return self.drum

    def isDrumFresh(self) -> bool:
        return self.drum_fresh

    def setPower(self, value: int) -> bool:
        return self._write(POWER, value)

    def setAir(self, value: int) -> bool:
        return self._write(AIR, value)

    def setDrum(self, value: int) -> bool:
        return self._write(DRUM, value)

    def _write(self, target: bytes, value: int) -> bool:
        if not self.ready or not 0 <= value <= 100 or not self.writes_succeed:
            return False
        self.calls.append((target, value))
        return True

    def lose_control_freshness(self) -> None:
        self.power_fresh = False
        self.air_fresh = False
        self.drum_fresh = False


def test_charge_snapshots_literal_control_state_and_restores_in_safe_order() -> None:
    device = FakeControlDevice(power=70, air=80, drum=30)
    controller = SantokerControlController()

    controller.mark_charge(device)

    assert controller.intended_controls() == {POWER: 70, AIR: 80, DRUM: 30}

    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()
    device.power = device.air = device.drum = 0

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [(DRUM, 30), (AIR, 80)]

    device.drum = 30
    device.air = 80
    device.drum_fresh = True
    device.air_fresh = True
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [(DRUM, 30), (AIR, 80), (POWER, 70)]


def test_active_commands_override_charge_snapshot_and_loss_telemetry() -> None:
    device = FakeControlDevice(power=70, air=80, drum=30)
    controller = SantokerControlController()
    controller.mark_charge(device)

    controller.note_control_request(POWER, 71, active_roast=True)
    controller.note_control_request(AIR, 81, active_roast=True)
    controller.note_control_request(DRUM, 31, active_roast=True)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    assert controller.intended_controls() == {POWER: 71, AIR: 81, DRUM: 31}
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [(DRUM, 31), (AIR, 81)]


def test_loss_telemetry_fills_controls_that_were_invalid_at_charge() -> None:
    device = FakeControlDevice(power=70, air=-1, drum=101)
    controller = SantokerControlController()
    controller.mark_charge(device)

    device.air = 80
    device.drum = 30
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.intended_controls() == {POWER: 70, AIR: 80, DRUM: 30}


def test_command_during_rx_silence_replaces_pending_target() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    controller.note_control_request(DRUM, 45, active_roast=True)

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [(DRUM, 45), (AIR, 80)]


def test_known_heater_target_waits_for_every_known_mechanical_target() -> None:
    now = [10.0]
    device = FakeControlDevice()
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )

    device.drum_fresh = True
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.THROTTLED
    )
    assert device.calls == [(DRUM, 30), (AIR, 80)]

    device.air_fresh = True
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls[-1] == (POWER, 70)


def test_unavailable_mechanical_targets_do_not_block_power() -> None:
    device = FakeControlDevice(air=-1, drum=-1)
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.power_fresh = False

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [(POWER, 70)]


def test_retries_use_one_second_throttle_and_fixed_order() -> None:
    now = [10.0]
    device = FakeControlDevice()
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    now[0] = 10.999
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.THROTTLED
    )
    now[0] = 11.0
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [
        (DRUM, 30),
        (AIR, 80),
        (DRUM, 30),
        (AIR, 80),
    ]


def test_recovery_converges_only_after_fresh_matching_reports() -> None:
    device = FakeControlDevice(air=-1, drum=-1)
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.power_fresh = False

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert controller.restoration_pending()

    device.power = 70
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.THROTTLED
    )

    device.power_fresh = True
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.CONVERGED
    )
    assert not controller.restoration_pending()


def test_recovery_waits_for_protocol_readiness_and_failed_writes() -> None:
    device = FakeControlDevice(ready=False)
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.WAITING
    )
    assert device.calls == []

    device.ready = True
    device.writes_succeed = False
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.WAITING
    )
    assert device.calls == []


def test_mismatch_without_transport_loss_never_actuates_controls() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    device.power = device.air = device.drum = 0

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.NONE
    )
    assert device.calls == []


def test_transport_loss_outside_active_roast_does_not_arm_recovery() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)

    controller.note_transport_loss(active_roast=False, device=device)

    assert not controller.restoration_pending()
    assert (
        controller.reconcile_after_frame(-1, 0, device)
        is ControlReconcileOutcome.NONE
    )
    assert device.calls == []


@pytest.mark.parametrize('target', [POWER, AIR, DRUM])
@pytest.mark.parametrize('value', [-1, 101, 1.5, True])
def test_invalid_or_inactive_requests_are_not_retained(target: bytes, value: object) -> None:
    controller = SantokerControlController()

    controller.note_control_request(target, value, active_roast=True)  # type: ignore[arg-type]
    controller.note_control_request(target, 50, active_roast=False)

    assert controller.intended_controls() == {}


def test_unknown_control_request_is_not_retained() -> None:
    controller = SantokerControlController()

    controller.note_control_request(b'\x00', 50, active_roast=True)

    assert controller.intended_controls() == {}


@pytest.mark.parametrize(
    'boundary',
    [
        SantokerControlController.mark_drop,
        SantokerControlController.reset_roast,
        SantokerControlController.start_monitoring,
        SantokerControlController.stop_monitoring,
    ],
)
def test_lifecycle_boundary_clears_intent_and_pending_recovery(
    boundary: object,
) -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    boundary(controller)  # type: ignore[operator]

    assert controller.intended_controls() == {}
    assert not controller.restoration_pending()
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.NONE
    )
    assert device.calls == []


def test_drop_frame_clears_intent_before_recovery_can_actuate() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert (
        controller.reconcile_after_frame(1, 5, device)
        is ControlReconcileOutcome.NONE
    )
    assert controller.intended_controls() == {}
    assert device.calls == []


def test_non_active_frame_cancels_pending_recovery_without_actuating() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert (
        controller.reconcile_after_frame(-1, 0, device)
        is ControlReconcileOutcome.NONE
    )
    assert not controller.restoration_pending()
    assert controller.intended_controls() == {POWER: 70, AIR: 80, DRUM: 30}
    assert device.calls == []


def test_new_charge_replaces_previous_roast_state() -> None:
    first_device = FakeControlDevice(power=70, air=80, drum=30)
    next_device = FakeControlDevice(power=40, air=50, drum=60)
    controller = SantokerControlController()
    controller.mark_charge(first_device)
    controller.note_transport_loss(active_roast=True, device=first_device)

    controller.mark_charge(next_device)

    assert controller.intended_controls() == {POWER: 40, AIR: 50, DRUM: 60}
    assert not controller.restoration_pending()
