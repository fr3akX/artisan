# Santoker Active-Roast Control Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically restore intended Santoker drum, fan, and heater settings after an active-roast transport loss and valid reconnect.

**Architecture:** Generalize the existing power-only recovery controller into a focused three-control state machine. Santoker protocol objects expose independent freshness and safe setters for all three controls; the main window owns lifecycle integration, while the canvas marks successful CHARGE and DROP boundaries.

**Tech Stack:** Python 3.12, PyQt6 signals/slots, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-20-santoker-control-recovery-design.md`

## Global Constraints

- Restore controls only after confirmed transport loss during CHARGE-to-DROP active roasting.
- Restore and confirm drum and fan before restoring heater power.
- Never restore Machine ON or warm-up through this controller.
- Keep all hardware boundaries deterministic and mocked in tests; do not use live roasting hardware.
- Preserve monitoring-generation guards and Qt thread affinity.
- Retain only integer control values from 0 through 100.

---

### Task 1: Santoker control protocol freshness

**Files:**
- Modify: `src/artisanlib/santoker.py`
- Test: `src/test/unitary/artisanlib/test_santoker.py`

**Interfaces:**
- Produces: `isAirFresh() -> bool`, `isDrumFresh() -> bool`, `setAir(value: int) -> bool`, and `setDrum(value: int) -> bool` alongside the existing power interface.

- [ ] **Step 1: Write failing protocol tests**

Add tests that construct a Santoker instance, verify power/fan/drum freshness are false after `resetProtocolState()`, register one control reading at a time, and verify only that control becomes fresh. Add setter tests proving ready-state writes use `AIR` and `DRUM`, while invalid values and an unready protocol produce no write.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker.py -k 'control_freshness or control_setters'
```

Expected: failures because fan/drum freshness methods and setters do not exist.

- [ ] **Step 3: Implement minimal protocol support**

Add `_air_fresh` and `_drum_fresh` booleans initialized to false. Mark them true only when their target is registered and false in `resetProtocolState()`. Add range-checked, header-ready `setAir()` and `setDrum()` methods symmetric with `setPower()`.

- [ ] **Step 4: Verify GREEN**

Run the focused tests and then all of `test_santoker.py`.

- [ ] **Step 5: Commit**

```bash
git add src/artisanlib/santoker.py src/test/unitary/artisanlib/test_santoker.py
git commit -m 'Track Santoker control freshness'
```

### Task 2: Generalized active-roast control state machine

**Files:**
- Create: `src/artisanlib/santoker_controls.py`
- Create: `src/test/unitary/artisanlib/test_santoker_controls.py`
- Delete: `src/artisanlib/santoker_power.py`
- Delete: `src/test/unitary/artisanlib/test_santoker_power.py`

**Interfaces:**
- Produces: `SantokerControlController.mark_charge(device)`, `note_control_request(target, value, active_roast)`, `note_transport_loss(active_roast, device)`, `reconcile_after_frame(charge_index, drop_index, device)`, `mark_drop()`, `reset_roast()`, `start_monitoring()`, and `stop_monitoring()`.
- Produces: `ControlReconcileOutcome` values `NONE`, `WAITING`, `THROTTLED`, `ATTEMPTED`, and `CONVERGED`.

- [ ] **Step 1: Write failing state-machine tests**

Create a real fake device implementing the six getters/freshness methods and three setters. Cover literal snapshots such as `{POWER: 70, AIR: 80, DRUM: 30}`; command precedence; transport-loss arming; DRUM/AIR writes before POWER; power blocking until fresh mechanical confirmation; one-second retry throttling; convergence; and every lifecycle boundary in the spec.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker_controls.py
```

Expected: collection failure because `artisanlib.santoker_controls` does not exist.

- [ ] **Step 3: Implement the minimal controller**

Implement target metadata in the fixed order `DRUM`, `AIR`, `POWER`. Snapshot only valid values, preserve outgoing-command precedence, retain a pending-target set after loss, compare only fresh telemetry, and block POWER while a known DRUM or AIR target remains unresolved. Use a monotonic clock and a single one-second retry interval.

- [ ] **Step 4: Verify GREEN and remove superseded power module**

Run the new tests, then migrate any power-only scenarios that remain behaviorally relevant before deleting the old module and tests.

- [ ] **Step 5: Commit**

```bash
git add src/artisanlib/santoker_controls.py src/test/unitary/artisanlib/test_santoker_controls.py src/artisanlib/santoker_power.py src/test/unitary/artisanlib/test_santoker_power.py
git commit -m 'Restore Santoker controls after reconnect'
```

### Task 3: Application lifecycle integration and user warning

**Files:**
- Modify: `src/artisanlib/main.py`
- Modify: `src/artisanlib/canvas.py`
- Test: `src/test/unitary/artisanlib/test_santoker_warmup.py`
- Test: `src/test/unitary/artisanlib/test_canvas.py`

**Interfaces:**
- Consumes: `SantokerControlController` from Task 2.
- Produces: `ApplicationWindow.markSantokerCharge()` and existing lifecycle methods wired to the generalized controller.

- [ ] **Step 1: Write failing integration tests**

Add tests proving successful CHARGE calls `markSantokerCharge()` only after the profile semaphore is released, failed or cancelled CHARGE does not call it, outgoing FA/CA/C0 commands are tracked only during an active roast, transport loss snapshots and arms recovery, DROP/RESET/monitoring boundaries clear it, and stale-generation commands remain ignored. Add a reconnect test asserting the UI receives one existing translated `Connected` status while exact targets and values are available to application logging/TX diagnostics.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd src
.venv/bin/python -m pytest -q \
  test/unitary/artisanlib/test_canvas.py::TestInventoryCharge \
  test/unitary/artisanlib/test_santoker_warmup.py -k 'control or reconnect or generation or charge or drop'
```

Expected: failures because CHARGE snapshot and generalized tracking are not wired.

- [ ] **Step 3: Implement minimal integration**

Replace `santokerPowerController` with `santokerControlController`, calculate active-roast state from `flagstart`, CHARGE, and DROP, pass the live device to CHARGE/loss snapshots, track only FA/CA/C0 sends, and reconcile from accepted frames. Emit one `Connected` status on the first recovery attempt and log ordered target/value restoration at warning level.

- [ ] **Step 4: Verify GREEN**

Run the focused integration tests, all Santoker unit tests, and `git diff --check`.

- [ ] **Step 5: Commit**

```bash
git add src/artisanlib/main.py src/artisanlib/canvas.py src/test/unitary/artisanlib/test_santoker_warmup.py src/test/unitary/artisanlib/test_canvas.py
git commit -m 'Recover Santoker controls during active roasts'
```

### Task 4: Final validation and review

**Files:**
- Modify only if validation reveals a regression.

- [ ] **Step 1: Run static checks**

```bash
cd src
.venv/bin/ruff check artisanlib/santoker.py artisanlib/santoker_controls.py artisanlib/main.py artisanlib/canvas.py test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_controls.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_canvas.py
.venv/bin/mypy artisanlib/santoker.py artisanlib/santoker_controls.py artisanlib/main.py artisanlib/canvas.py
```

- [ ] **Step 2: Run focused and full tests**

```bash
cd src
.venv/bin/python -m pytest -q test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_controls.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_canvas.py
.venv/bin/python -m pytest
```

Compare any full-suite failures with the established 12 local mock-related baseline failures; no new failure is acceptable.

- [ ] **Step 3: Inspect repository state**

```bash
git diff --check
git status --short
git log --oneline -5
```

- [ ] **Step 4: Request fresh-context review**

Review the exact new commit range for hardware safety, lifecycle clearing, thread affinity, stale generation handling, test quality, and scope.

- [ ] **Step 5: Push and verify PR checks**

Push `feature/santoker-reconnect-diagnostics`, then verify macOS/Python 3.14 pytest, Ruff, mypy, Pylint, codespell, and Windows installer checks.
