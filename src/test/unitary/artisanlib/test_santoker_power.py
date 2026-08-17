from dataclasses import dataclass, field

from artisanlib.santoker_power import (
    PowerReconcileOutcome,
    SantokerPowerController,
)


@dataclass
class FakePowerDevice:
    ready: bool = True
    power: int = 0
    power_fresh: bool = True
    update_report_on_write: bool = False
    calls: list[int] = field(default_factory=list)

    def isHeaderReady(self) -> bool:
        return self.ready

    def getPower(self) -> int:
        return self.power

    def isPowerFresh(self) -> bool:
        return self.power_fresh

    def setPower(self, value: int) -> bool:
        if not self.ready or not 0 <= value <= 100:
            return False
        self.calls.append(value)
        if self.update_report_on_write:
            self.power = value
        return True


def test_reconnect_restores_latest_power_requested_during_rx_silence() -> None:
    device = FakePowerDevice(power=70)
    controller = SantokerPowerController()

    for value in (80, 90, 100):
        controller.note_power_request(value, active_roast=True)
    controller.note_transport_loss()
    device.power = 0

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [100]
    assert controller.desired_power() == 100
    assert controller.restoration_pending()


def test_stale_matching_report_neither_suppresses_replay_nor_false_convergence() -> None:
    now = [10.0]
    device = FakePowerDevice(power=70, power_fresh=False)
    controller = SantokerPowerController(monotonic_clock=lambda: now[0])
    controller.note_power_request(70, active_roast=True)
    controller.note_transport_loss()

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.ATTEMPTED
    )
    now[0] = 11.0
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [70, 70]
    assert controller.restoration_pending()


def test_reconciliation_throttles_retries_and_converges_on_fresh_report() -> None:
    now = [10.0]
    device = FakePowerDevice(power=0, power_fresh=False)
    controller = SantokerPowerController(monotonic_clock=lambda: now[0])
    controller.note_power_request(100, active_roast=True)
    controller.note_transport_loss()

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.ATTEMPTED
    )
    now[0] = 10.2
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.THROTTLED
    )
    assert device.calls == [100]

    now[0] = 11.0
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.ATTEMPTED
    )
    assert device.calls == [100, 100]

    device.power = 100
    device.power_fresh = True
    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.CONVERGED
    )
    assert not controller.restoration_pending()


def test_reconciliation_waits_for_protocol_readiness() -> None:
    device = FakePowerDevice(ready=False, power=0)
    controller = SantokerPowerController()
    controller.note_power_request(90, active_roast=True)
    controller.note_transport_loss()

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.WAITING
    )
    assert device.calls == []
    assert controller.restoration_pending()


def test_power_is_not_restored_without_a_transport_loss() -> None:
    device = FakePowerDevice(power=0)
    controller = SantokerPowerController()
    controller.note_power_request(90, active_roast=True)

    assert (
        controller.reconcile_after_frame(1, 0, device)
        is PowerReconcileOutcome.NONE
    )
    assert device.calls == []


def test_valid_precharge_frame_cancels_power_restoration_attempt() -> None:
    device = FakePowerDevice(power=0)
    controller = SantokerPowerController()
    controller.note_power_request(90, active_roast=True)
    controller.note_transport_loss()

    assert (
        controller.reconcile_after_frame(-1, 0, device)
        is PowerReconcileOutcome.NONE
    )
    assert device.calls == []
    assert not controller.restoration_pending()
    assert controller.desired_power() == 90


def test_drop_clears_desired_power_and_pending_restoration() -> None:
    device = FakePowerDevice(power=0)
    controller = SantokerPowerController()
    controller.note_power_request(90, active_roast=True)
    controller.note_transport_loss()

    assert (
        controller.reconcile_after_frame(1, 5, device)
        is PowerReconcileOutcome.NONE
    )
    assert device.calls == []
    assert controller.desired_power() is None
    assert not controller.restoration_pending()


def test_roast_reset_clears_desired_power() -> None:
    controller = SantokerPowerController()
    controller.note_power_request(90, active_roast=True)
    controller.note_transport_loss()

    controller.reset_roast()

    assert controller.desired_power() is None
    assert not controller.restoration_pending()


def test_stop_monitoring_clears_desired_power() -> None:
    controller = SantokerPowerController()
    controller.note_power_request(90, active_roast=True)
    controller.note_transport_loss()

    controller.stop_monitoring()

    assert controller.desired_power() is None
    assert not controller.restoration_pending()


def test_invalid_raw_power_does_not_replace_safe_desired_value() -> None:
    controller = SantokerPowerController()
    controller.note_power_request(80, active_roast=True)

    controller.note_power_request(101, active_roast=True)

    assert controller.desired_power() == 80


def test_power_requested_outside_active_roast_is_not_restoration_intent() -> None:
    controller = SantokerPowerController()

    controller.note_power_request(90, active_roast=False)

    assert controller.desired_power() is None
    assert not controller.restoration_pending()
