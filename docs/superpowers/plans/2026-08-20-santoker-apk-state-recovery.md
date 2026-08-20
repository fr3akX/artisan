# Santoker APK-Parity State Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Santoker Machine ON, Heating ON, drum, airflow, and heater-power intent in official-app order after a confirmed reconnect during an active roast.

**Architecture:** Extend the Santoker protocol object with independently fresh `0x7A` and `0x7B` reports and setters. Generalize the existing active-roast controller to retain mode intent at CHARGE and reconcile exactly one desired target at a time in fixed APK priority, while preserving existing generation, lifecycle, and accepted-frame boundaries.

**Tech Stack:** Python 3.12, PyQt6 signals/slots, pytest, Ruff, mypy, Pyright.

**Spec:** `docs/superpowers/specs/2026-08-20-santoker-apk-state-recovery-design.md`

## Global Constraints

- Automatic writes require confirmed transport loss during a successful CHARGE-to-DROP active roast.
- Successful CHARGE records Machine ON (`0x7A=1`) and Heating ON (`0x7B=1`) intent without sending either command immediately.
- Recovery order is Machine, Heating, Drum, Air, Power; attempt only one target per accepted-frame reconciliation.
- Advance only after a fresh matching report received after that target's write.
- Retry an unresolved target no more than once per second.
- Explicit active-roast `0x7A`/`0x7B` commands supersede CHARGE defaults, including OFF.
- DROP, RESET, monitoring restart, and monitoring stop clear all recovery state.
- Preserve monitoring-generation guards and Qt thread affinity.
- Do not restore warm-up, roast events, cooling, blending, calibration, or configuration.
- Never use live roasting hardware for validation.

---

### Task 1: Machine and Heating protocol state

**Files:**
- Modify: `src/artisanlib/santoker.py`
- Modify: `src/artisanlib/santoker_diagnostics.py`
- Test: `src/test/unitary/artisanlib/test_santoker.py`
- Test: `src/test/unitary/artisanlib/test_santoker_diagnostics.py`

**Interfaces:**
- Produces constants `Santoker.MACHINE_ON == b'\x7A'` and `Santoker.HEATING_ON == b'\x7B'`.
- Produces `getMachineOn() -> int`, `isMachineOnFresh() -> bool`, `setMachineOn(value: int) -> bool`.
- Produces `getHeatingOn() -> int`, `isHeatingOnFresh() -> bool`, `setHeatingOn(value: int) -> bool`.
- Extends `DiagnosticField` and `SantokerDiagnosticsState` with `machine_on` and `heating_on` integer state.

- [ ] **Step 1: Write failing protocol tests**

Add tests beside the existing power/air/drum freshness tests that exercise real `Santoker` objects:

```python
def test_operating_mode_reports_are_independently_fresh() -> None:
    santoker = make_santoker_for_control_test()

    santoker.resetProtocolState()
    assert santoker.getMachineOn() == -1
    assert santoker.getHeatingOn() == -1
    assert not santoker.isMachineOnFresh()
    assert not santoker.isHeatingOnFresh()

    santoker.register_reading(Santoker.MACHINE_ON, b'\x01')
    assert santoker.getMachineOn() == 1
    assert santoker.isMachineOnFresh()
    assert not santoker.isHeatingOnFresh()

    santoker.register_reading(Santoker.HEATING_ON, b'\x00')
    assert santoker.getHeatingOn() == 0
    assert santoker.isHeatingOnFresh()
```

Add parametrized tests proving reports other than `0` and `1` are ignored, and setters reject `-1`, `2`, `True`, and unready protocol state without calling `send_msg`. Prove valid setters call `send_msg` with `MACHINE_ON`/`HEATING_ON` and invalidate only their own freshness.

- [ ] **Step 2: Run protocol tests and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker.py \
  -k 'operating_mode or machine_on or heating_on'
```

Expected: FAIL because the constants and methods do not exist.

- [ ] **Step 3: Write failing diagnostics test**

Add a test that calls:

```python
session.record_decoded('machine_on', 1)
session.record_decoded('heating_on', 0)
view = session.view()
assert view.state.machine_on == 1
assert view.state.heating_on == 0
assert 'machine_on: 1' in [event.description for event in view.events]
assert 'heating_on: 0' in [event.description for event in view.events]
```

Use the diagnostics module's existing snapshot/event accessors exactly as neighboring tests do.

- [ ] **Step 4: Run diagnostics test and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_diagnostics.py \
  -k 'operating_mode'
```

Expected: FAIL because `SantokerDiagnosticsState` lacks both fields.

- [ ] **Step 5: Implement minimal protocol and diagnostics support**

In `Santoker`:

- Add `MACHINE_ON: Final[bytes] = b'\x7A'` and `HEATING_ON: Final[bytes] = b'\x7B'`.
- Add `_machine_on`, `_machine_on_fresh`, `_heating_on`, `_heating_on_fresh` to `__slots__` and initialize values to `-1` and freshness to false.
- Implement getters and setters symmetric with percentage controls, but validate with `type(value) is int and value in {0, 1}`.
- In `register_reading`, accept only `0` or `1`, set freshness, update state, and call `_record_decoded('machine_on', value)` or `_record_decoded('heating_on', value)`.
- In `resetProtocolState`, reset both values to `-1` and freshness flags to false.

In diagnostics, add both fields to `DiagnosticField`, `SantokerDiagnosticsState`, instance storage, snapshot construction, `_STATE_LABELS`, and `record_decoded` assignment.

- [ ] **Step 6: Verify GREEN and regressions**

Run:

```bash
cd src
.venv/bin/python -m pytest -q \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py
.venv/bin/ruff check artisanlib/santoker.py artisanlib/santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py
.venv/bin/mypy artisanlib/santoker.py artisanlib/santoker_diagnostics.py
```

Expected: all selected tests and static checks pass.

- [ ] **Step 7: Commit**

```bash
git add src/artisanlib/santoker.py src/artisanlib/santoker_diagnostics.py \
  src/test/unitary/artisanlib/test_santoker.py \
  src/test/unitary/artisanlib/test_santoker_diagnostics.py
git commit -m 'Track Santoker operating modes'
```

### Task 2: Ordered APK-style desired-state controller

**Files:**
- Modify: `src/artisanlib/santoker_controls.py`
- Modify if required: `src/artisanlib/main.py`
- Modify: `src/test/unitary/artisanlib/test_santoker_controls.py`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py`

**Interfaces:**
- Consumes the six mode methods from Task 1 in `SantokerControlDevice`.
- Produces `MACHINE_ON`, `HEATING_ON`, `PERCENTAGE_TARGETS`, and ordered `CONTROL_TARGETS`.
- Keeps the public `SantokerControlController` lifecycle and reconciliation signatures unchanged.
- Existing `santokerSendMessage()`, `markSantokerCharge()`, ready-false handling, accepted-frame callback, generation guard, and lifecycle clearing remain the application boundary.

- [ ] **Step 1: Extend the real fake device and write the failing power-cycle test**

Extend `FakeControlDevice` with integer mode values, independent freshness, and setters. Make `_write()` invalidate the written target's freshness.

Add this behavior test:

```python
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
```

`FakeControlDevice.report()` must update the matching reported value and freshness without touching the controller.

- [ ] **Step 2: Run the power-cycle test and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q \
  test/unitary/artisanlib/test_santoker_controls.py::test_power_cycle_restores_full_state_in_official_app_order
```

Expected: FAIL because mode targets are absent and current recovery attempts drum and air together.

- [ ] **Step 3: Add failing target-validation and sequencing tests**

Add focused tests proving:

- `mark_charge()` records `{MACHINE_ON: 1, HEATING_ON: 1}` even when the device reports both modes off or unknown, without calling a setter.
- Explicit active-roast `MACHINE_ON=0` and `HEATING_ON=0` replace those defaults and are later restored as OFF.
- Mode values `-1`, `2`, `True`, and `1.0` are rejected; percentage targets retain the exact `0..100` rule.
- A pending target blocks every later target until a fresh matching post-write report arrives.
- Failed writes and nonmatching reports retry only that target at `>=1.0` seconds.
- A control request during disconnection replaces only its own pending target and retry history.
- No-loss mismatch and all existing lifecycle boundaries remain non-actuating.

- [ ] **Step 4: Write failing application integration tests before controller implementation**

In `test_santoker_warmup.py`, build a real `SantokerControlController` and deterministic fake Santoker device using the established minimalist `ApplicationWindow` fixtures. Add tests proving:

1. `markSantokerCharge()` records Machine and Heating ON without device writes.
2. Generation-guarded ready-false arms recovery.
3. Accepted frames produce one attempt at a time in exact `7A`, `7B`, `C0`, `CA`, `FA` order after each fake report.
4. The existing translated `Connected` status appears exactly once.
5. Warning logs contain each exact target/value.
6. `santokerSendMessage(MACHINE_ON, 0)` and `santokerSendMessage(HEATING_ON, 0)` update active-roast intent before forwarding the raw commands.
7. The same mode commands outside an active roast do not create recovery intent, and an old generation cannot send them to a replacement connection.
8. DROP and successful reset clear mode defaults with the existing percentage intent.

- [ ] **Step 5: Run controller and application groups and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_controls.py \
  -k 'operating_mode or official_app_order or single_target or explicit_mode or target_validation'
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_warmup.py \
  -k 'full_operating_state_recovery or operating_mode_command'
```

Expected: FAIL on missing mode behavior and multi-target attempts.

- [ ] **Step 6: Implement minimal ordered reconciliation and integration**

Use these fixed target groups:

```python
MACHINE_ON: Final[bytes] = b'\x7A'
HEATING_ON: Final[bytes] = b'\x7B'
PERCENTAGE_TARGETS: Final[tuple[bytes, ...]] = (DRUM, AIR, POWER)
CONTROL_TARGETS: Final[tuple[bytes, ...]] = (
    MACHINE_ON,
    HEATING_ON,
    DRUM,
    AIR,
    POWER,
)
```

Update the protocol to call target-specific mode or percentage getters, freshness methods, and setters. `mark_charge()` must clear old state, insert both mode defaults, then fill only missing percentage targets from telemetry. `note_transport_loss()` may fill only missing percentage targets.

Replace the current mechanical batch eligibility with first-pending-target selection. On each reconciliation:

- remove only attempted targets with a fresh matching report;
- select the first remaining target in `CONTROL_TARGETS`;
- throttle using that target's own attempt timestamp;
- invoke exactly one setter;
- record exactly one item in `last_reconciliation_attempts()`;
- keep the target pending until fresh confirmation.

Retain existing confirmed-loss, CHARGE/DROP, reset, monitoring, and invalid-state cancellation logic. Expanded `CONTROL_TARGETS` should make existing `ApplicationWindow.santokerSendMessage()` track explicit mode commands. If integration tests expose a boundary, modify only `main.py` while preserving Qt signal routing and generation equality; do not add a reconnect timer or direct worker-thread UI access.

- [ ] **Step 7: Verify GREEN and update existing expectations**

Update older controller tests that intentionally expected simultaneous Drum/Air attempts so they now acknowledge and report one ordered target per call. Do not weaken lifecycle or post-write-freshness assertions. Run bounded application groups rather than the previously hanging aggregate selection.

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_controls.py
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_warmup.py \
  -k 'full_operating_state_recovery or operating_mode_command or control_command or stale_generation or successful_charge or drop_immediately'
.venv/bin/ruff check artisanlib/santoker_controls.py artisanlib/main.py \
  test/unitary/artisanlib/test_santoker_controls.py \
  test/unitary/artisanlib/test_santoker_warmup.py
.venv/bin/mypy artisanlib/santoker_controls.py artisanlib/main.py
.venv/bin/pyright artisanlib/santoker_controls.py \
  test/unitary/artisanlib/test_santoker_controls.py
```

Expected: all selected tests and scoped static checks pass.

- [ ] **Step 8: Commit**

```bash
git add src/artisanlib/santoker_controls.py src/artisanlib/main.py \
  src/test/unitary/artisanlib/test_santoker_controls.py \
  src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m 'Restore Santoker operating state after reconnect'
```

If production integration required no `main.py` change, stage only the three changed files.

### Task 3: Final verification, independent review, and PR validation

**Files:**
- Modify only if a new failing test or review finding proves a defect.

- [ ] **Step 1: Run focused tests separately**

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker.py
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_diagnostics.py
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_controls.py
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_warmup.py
```

Expected: all pass without hanging.

- [ ] **Step 2: Run static validation**

```bash
cd src
.venv/bin/ruff check artisanlib/santoker.py artisanlib/santoker_diagnostics.py \
  artisanlib/santoker_controls.py artisanlib/main.py \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker_controls.py \
  test/unitary/artisanlib/test_santoker_warmup.py
.venv/bin/mypy artisanlib/santoker.py artisanlib/santoker_diagnostics.py \
  artisanlib/santoker_controls.py artisanlib/main.py
```

- [ ] **Step 3: Run the bounded full suite**

```bash
cd src
timeout 600 .venv/bin/python -m pytest --timeout=60 --timeout-method=signal
```

Expected: no hang and no new failures. Compare results with the established local baseline of 3,797 passed, 22 skipped, and 12 unrelated mock failures.

- [ ] **Step 4: Inspect repository state**

```bash
git diff --check
git status --short
git log --oneline 0703792f6..HEAD
```

- [ ] **Step 5: Request fresh-context review**

Review the exact implementation range after `0703792f6` for APK-order fidelity, automatic reactivation safety, post-write report freshness, retry throttling, active-roast and generation boundaries, diagnostics, test quality, and scope. Fix every Critical or Important finding through a new RED/GREEN cycle and request re-review.

- [ ] **Step 6: Push and verify native CI**

Push `feature/santoker-reconnect-diagnostics`, confirm the remote head, and verify macOS/Python 3.14 pytest, Ruff, mypy, Pylint, codespell, build, and Windows installer checks. Do not claim live-hardware recovery until the user performs a new power-cycle test and exports diagnostics if it still fails.
