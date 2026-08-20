from dataclasses import dataclass, field

import pytest

from artisanlib.santoker_controls import (
    AIR,
    CONTROL_TARGETS,
    DRUM,
    HEATING_ON,
    MACHINE_ON,
    PERCENTAGE_TARGETS,
    POWER,
    ControlReconcileOutcome,
    SantokerControlController,
)


@dataclass
class FakeControlDevice:
    ready: bool = True
    machine_on: int = 1
    heating_on: int = 1
    power: int = 70
    air: int = 80
    drum: int = 30
    machine_on_fresh: bool = True
    heating_on_fresh: bool = True
    power_fresh: bool = True
    air_fresh: bool = True
    drum_fresh: bool = True
    writes_succeed: bool = True
    attempts: list[tuple[bytes, int]] = field(default_factory=list)
    calls: list[tuple[bytes, int]] = field(default_factory=list)

    def isHeaderReady(self) -> bool:
        return self.ready

    def getMachineOn(self) -> int:
        return self.machine_on

    def isMachineOnFresh(self) -> bool:
        return self.machine_on_fresh

    def getHeatingOn(self) -> int:
        return self.heating_on

    def isHeatingOnFresh(self) -> bool:
        return self.heating_on_fresh

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

    def setMachineOn(self, value: int) -> bool:
        return self._write(MACHINE_ON, value)

    def setHeatingOn(self, value: int) -> bool:
        return self._write(HEATING_ON, value)

    def setPower(self, value: int) -> bool:
        return self._write(POWER, value)

    def setAir(self, value: int) -> bool:
        return self._write(AIR, value)

    def setDrum(self, value: int) -> bool:
        return self._write(DRUM, value)

    def _write(self, target: bytes, value: int) -> bool:
        self.attempts.append((target, value))
        valid = (
            type(value) is int
            and (
                value in (0, 1)
                if target in (MACHINE_ON, HEATING_ON)
                else 0 <= value <= 100
            )
        )
        if not self.ready or not valid or not self.writes_succeed:
            return False
        self.calls.append((target, value))
        if target == MACHINE_ON:
            self.machine_on_fresh = False
        elif target == HEATING_ON:
            self.heating_on_fresh = False
        elif target == POWER:
            self.power_fresh = False
        elif target == AIR:
            self.air_fresh = False
        else:
            self.drum_fresh = False
        return True

    def report(self, target: bytes, value: int) -> None:
        if target == MACHINE_ON:
            self.machine_on = value
            self.machine_on_fresh = True
        elif target == HEATING_ON:
            self.heating_on = value
            self.heating_on_fresh = True
        elif target == POWER:
            self.power = value
            self.power_fresh = True
        elif target == AIR:
            self.air = value
            self.air_fresh = True
        elif target == DRUM:
            self.drum = value
            self.drum_fresh = True

    def lose_control_freshness(self) -> None:
        self.machine_on_fresh = False
        self.heating_on_fresh = False
        self.power_fresh = False
        self.air_fresh = False
        self.drum_fresh = False


def test_power_cycle_restores_full_state_in_official_app_order() -> None:
    device = FakeControlDevice(
        machine_on=0,
        heating_on=0,
        drum=0,
        air=0,
        power=0,
    )
    controller = SantokerControlController()
    controller.mark_charge(device)
    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 0,
        AIR: 0,
        POWER: 0,
    }
    controller.note_control_request(DRUM, 30, active_roast=True)
    controller.note_control_request(AIR, 80, active_roast=True)
    controller.note_control_request(POWER, 70, active_roast=True)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    expected = [
        (MACHINE_ON, 1),
        (HEATING_ON, 1),
        (DRUM, 30),
        (AIR, 80),
        (POWER, 70),
    ]
    for target, value in expected:
        assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
        assert device.calls[-1] == (target, value)
        device.report(target, value)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.CONVERGED


def test_operating_mode_defaults_do_not_write_and_percentage_targets_are_snapshotted() -> None:
    device = FakeControlDevice(machine_on=0, heating_on=-1, drum=30, air=80, power=70)
    controller = SantokerControlController()

    controller.mark_charge(device)

    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 30,
        AIR: 80,
        POWER: 70,
    }
    assert device.attempts == []
    assert PERCENTAGE_TARGETS == (DRUM, AIR, POWER)
    assert CONTROL_TARGETS == (MACHINE_ON, HEATING_ON, DRUM, AIR, POWER)


def test_explicit_mode_off_replaces_charge_defaults_and_is_restored_off() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_control_request(MACHINE_ON, 0, active_roast=True)
    controller.note_control_request(HEATING_ON, 0, active_roast=True)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    assert controller.intended_controls()[MACHINE_ON] == 0
    assert controller.intended_controls()[HEATING_ON] == 0
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (MACHINE_ON, 0)
    device.report(MACHINE_ON, 0)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (HEATING_ON, 0)


@pytest.mark.parametrize('target', [MACHINE_ON, HEATING_ON])
@pytest.mark.parametrize('value', [-1, 2, True, 1.0])
def test_operating_mode_target_validation_rejects_non_binary_integers(
    target: bytes, value: object
) -> None:
    controller = SantokerControlController()

    controller.note_control_request(target, value, active_roast=True)  # type: ignore[arg-type]

    assert controller.intended_controls() == {}


@pytest.mark.parametrize('target', PERCENTAGE_TARGETS)
@pytest.mark.parametrize(
    ('value', 'accepted'),
    [(-1, False), (0, True), (100, True), (101, False), (True, False), (1.0, False)],
)
def test_percentage_target_validation_retains_exact_integer_range(
    target: bytes, value: object, accepted: bool
) -> None:
    controller = SantokerControlController()

    controller.note_control_request(target, value, active_roast=True)  # type: ignore[arg-type]

    assert controller.intended_controls() == ({target: value} if accepted else {})


def test_single_target_blocks_later_targets_until_fresh_matching_report() -> None:
    now = [10.0]
    device = FakeControlDevice()
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls == [(MACHINE_ON, 1)]
    assert controller.last_reconciliation_attempts() == ((MACHINE_ON, 1),)

    now[0] = 11.0
    device.report(MACHINE_ON, 0)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls == [(MACHINE_ON, 1), (MACHINE_ON, 1)]

    device.report(MACHINE_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (HEATING_ON, 1)


def test_failed_write_retries_only_first_target_after_one_second() -> None:
    now = [10.0]
    device = FakeControlDevice(writes_succeed=False)
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.WAITING
    assert device.attempts == [(MACHINE_ON, 1)]
    now[0] = 10.999
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.THROTTLED
    now[0] = 11.0
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.WAITING
    assert device.attempts == [(MACHINE_ON, 1), (MACHINE_ON, 1)]


def test_disconnected_request_replaces_only_its_target_and_retry_history() -> None:
    now = [10.0]
    device = FakeControlDevice()
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    device.report(MACHINE_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    device.report(HEATING_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (DRUM, 30)

    now[0] = 10.2
    controller.note_control_request(AIR, 82, active_roast=True)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.THROTTLED
    assert device.calls[-1] == (DRUM, 30)

    controller.note_control_request(DRUM, 32, active_roast=True)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (DRUM, 32)
    device.report(DRUM, 32)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (AIR, 82)


def test_charge_snapshots_literal_control_state_and_restores_in_safe_order() -> None:
    device = FakeControlDevice(power=70, air=80, drum=30)
    controller = SantokerControlController()

    controller.mark_charge(device)

    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 30,
        AIR: 80,
        POWER: 70,
    }

    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()
    device.power = device.air = device.drum = 0

    expected = [
        (MACHINE_ON, 1),
        (HEATING_ON, 1),
        (DRUM, 30),
        (AIR, 80),
        (POWER, 70),
    ]
    for target, value in expected:
        assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
        assert device.calls[-1] == (target, value)
        device.report(target, value)


def test_active_commands_override_charge_snapshot_and_loss_telemetry() -> None:
    device = FakeControlDevice(power=70, air=80, drum=30)
    controller = SantokerControlController()
    controller.mark_charge(device)

    controller.note_control_request(POWER, 71, active_roast=True)
    controller.note_control_request(AIR, 81, active_roast=True)
    controller.note_control_request(DRUM, 31, active_roast=True)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 31,
        AIR: 81,
        POWER: 71,
    }
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [(MACHINE_ON, 1)]


def test_loss_telemetry_fills_controls_that_were_invalid_at_charge() -> None:
    device = FakeControlDevice(power=70, air=-1, drum=101)
    controller = SantokerControlController()
    controller.mark_charge(device)

    device.air = 80
    device.drum = 30
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 30,
        AIR: 80,
        POWER: 70,
    }


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
    assert device.calls == [(MACHINE_ON, 1)]
    device.report(MACHINE_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    device.report(HEATING_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (DRUM, 45)


def test_mechanical_confirmation_must_follow_its_restoration_write() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert device.isDrumFresh()
    assert device.isAirFresh()
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    device.report(MACHINE_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    device.report(HEATING_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert not device.isDrumFresh()
    assert device.isAirFresh()

    device.air_fresh = True
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.THROTTLED
    assert device.calls[-1] == (DRUM, 30)

    device.report(DRUM, 30)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (AIR, 80)


def test_known_heater_target_waits_for_every_known_mechanical_target() -> None:
    now = [10.0]
    device = FakeControlDevice()
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.lose_control_freshness()

    for target, value in (
        (MACHINE_ON, 1),
        (HEATING_ON, 1),
        (DRUM, 30),
    ):
        assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
        assert device.calls[-1] == (target, value)
        device.report(target, value)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (AIR, 80)
    assert device.calls.count((POWER, 70)) == 0

    device.report(AIR, 80)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
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
    device.report(MACHINE_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    device.report(HEATING_ON, 1)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert device.calls[-1] == (POWER, 70)


def test_reconciliation_attempts_are_reported_as_an_ordered_immutable_snapshot() -> None:
    device = FakeControlDevice()
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.ATTEMPTED
    )
    assert controller.last_reconciliation_attempts() == ((MACHINE_ON, 1),)

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is ControlReconcileOutcome.THROTTLED
    )
    assert controller.last_reconciliation_attempts() == ()


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
    assert device.calls == [(MACHINE_ON, 1), (MACHINE_ON, 1)]


def test_recovery_converges_only_after_fresh_matching_reports() -> None:
    device = FakeControlDevice(air=-1, drum=-1)
    controller = SantokerControlController()
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)
    device.power_fresh = False

    for target in (MACHINE_ON, HEATING_ON):
        assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
        assert controller.restoration_pending()
        device.report(target, 1)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.ATTEMPTED
    assert controller.restoration_pending()

    device.power = 70
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.THROTTLED

    device.power_fresh = True
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.CONVERGED
    assert not controller.restoration_pending()


def test_failed_writes_are_throttled_for_one_second() -> None:
    now = [10.0]
    device = FakeControlDevice(writes_succeed=False)
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.WAITING
    assert device.attempts == [(MACHINE_ON, 1)]

    now[0] = 10.5
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.THROTTLED
    assert device.attempts == [(MACHINE_ON, 1)]

    now[0] = 11.0
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.WAITING
    assert device.attempts == [(MACHINE_ON, 1), (MACHINE_ON, 1)]


def test_failed_write_throttle_survives_a_later_target_update() -> None:
    now = [10.0]
    device = FakeControlDevice(power=-1, air=-1, writes_succeed=False)
    controller = SantokerControlController(monotonic_clock=lambda: now[0])
    controller.mark_charge(device)
    controller.note_transport_loss(active_roast=True, device=device)

    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.WAITING
    assert device.attempts == [(MACHINE_ON, 1)]

    now[0] = 10.2
    controller.note_control_request(AIR, 80, active_roast=True)
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.THROTTLED
    assert device.attempts == [(MACHINE_ON, 1)]

    now[0] = 11.0
    assert controller.reconcile_after_frame(1, 0, device) is ControlReconcileOutcome.WAITING
    assert device.attempts == [(MACHINE_ON, 1), (MACHINE_ON, 1)]


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
    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 30,
        AIR: 80,
        POWER: 70,
    }
    assert device.calls == []


def test_new_charge_replaces_previous_roast_state() -> None:
    first_device = FakeControlDevice(power=70, air=80, drum=30)
    next_device = FakeControlDevice(power=40, air=50, drum=60)
    controller = SantokerControlController()
    controller.mark_charge(first_device)
    controller.note_transport_loss(active_roast=True, device=first_device)

    controller.mark_charge(next_device)

    assert controller.intended_controls() == {
        MACHINE_ON: 1,
        HEATING_ON: 1,
        DRUM: 60,
        AIR: 50,
        POWER: 40,
    }
    assert not controller.restoration_pending()
