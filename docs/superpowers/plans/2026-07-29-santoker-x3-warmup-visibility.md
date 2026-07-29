# Santoker X3 Warm-up Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide the X3 Warm-up controls while Artisan is fully OFF, reveal them immediately after ON or START, and preserve the cached target across OFF/ON cycles.

**Architecture:** Keep `ApplicationWindow.updateSantokerWarmupControls()` as the sole Warm-up state renderer and extend its visibility predicate with Artisan's `qmc.flagon`/`qmc.flagstart` state. Reuse `ApplicationWindow.updateControlsVisibility()` as the lifecycle integration point because established ON, START, and OFF paths already invoke it after changing their state, and add the missing direct refresh after STOP changes `flagstart`; readiness remains responsible only for toggle enablement.

**Tech Stack:** Python 3.12+, PyQt6, pytest, unittest.mock, Ruff, mypy, pyright, codespell, pre-commit.

## Global Constraints

- The controls are visible only when `santokerWarmup=true`, ArtisanViewer is inactive, and monitoring or recording is active.
- ON or START reveals the controls immediately; visibility must not wait for a Santoker object or valid packet.
- Before protocol readiness, the target remains editable while the toggle stays disabled and unchecked.
- Full OFF hides the pair without resetting the desired Celsius target or sending any command.
- A transport disconnect while Artisan remains logically ON leaves the pair visible but disables and unchecks the toggle.
- Existing target-first ON, OFF-before-CHARGE, CHARGE latch, RESET, report reconciliation, raw commands, presets, and non-X3 behavior must remain unchanged.
- No test may connect to BLE hardware, roasting hardware, cloud services, or network endpoints.
- No translation, generated UI, profile schema, protocol, preset, or dependency changes are required.
- Run Python tooling from `src/` with the existing `.venv`.
- The full-suite acceptance baseline is `2445 passed, 7 skipped, 11` known unrelated Qt Mock failures before these new tests are added; no additional failures are acceptable.

---

### Task 1: Make Warm-up visibility follow ON/START state

**Files:**
- Modify: `src/artisanlib/main.py:12026-12049`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py:221-254, 1339-1428`

**Interfaces:**
- Consumes: `qmc.flagon: bool`, `qmc.flagstart: bool`, `ApplicationWindow.santokerWarmup: bool`, and `app.artisanviewerMode: bool`.
- Produces: `ApplicationWindow.updateSantokerWarmupControls() -> None` with visibility defined by capability, viewer mode, and ON/START state.
- Preserves: `SantokerWarmupController.desired_temp_c`, `Santoker.isHeaderReady()`, and all existing widget enablement/state behavior.

- [ ] **Step 1: Extend the compact-window test helper with explicit Artisan state**

Change `compact_window()` so existing behavior-focused tests model an active monitoring session by default, while new visibility tests can model OFF and recording-only states:

```python
def compact_window(
    controls: SantokerWarmupControls,
    controller: SantokerWarmupController,
    device: FakeWarmupDevice | None,
    *,
    unit: str = 'C',
    charge_index: int = -1,
    capability: bool = True,
    monitoring: bool = True,
    recording: bool = False,
) -> SimpleNamespace:
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=False),
        qmc=SimpleNamespace(
            mode_tempsliders=unit,
            timeindex=[charge_index],
            flagon=monitoring,
            flagstart=recording,
        ),
        santokerWarmup=capability,
        santoker=device,
        santokerWarmupController=controller,
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': 'off-style', 'ON': 'on-style'},
        extraeventsactionstrings=[],
        buttonStates=[],
        setExtraEventButtonStyleSignal=Mock(),
        reportSantokerWarmupResult=Mock(),
        sendmessage=Mock(),
    )
```

Existing tests that use `compact_window()` continue to represent monitoring ON unless they explicitly override `monitoring` or `recording`.

- [ ] **Step 2: Write failing state-based visibility tests**

Replace the existing capability/viewer visibility parameterization with state-aware cases:

```python
@pytest.mark.parametrize(
    ('capability', 'viewer', 'monitoring', 'recording', 'visible'),
    [
        (True, False, False, False, False),
        (True, False, True, False, True),
        (True, False, False, True, True),
        (False, False, True, False, False),
        (True, True, True, False, False),
    ],
    ids=['fully-off', 'monitoring', 'recording', 'no-capability', 'viewer'],
)
def test_window_updates_compact_control_visibility(
    qapplication: QApplication,
    capability: bool,
    viewer: bool,
    monitoring: bool,
    recording: bool,
    visible: bool,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=viewer),
        qmc=SimpleNamespace(
            mode_tempsliders='C',
            timeindex=[-1],
            flagon=monitoring,
            flagstart=recording,
        ),
        santokerWarmup=capability,
        santoker=None,
        santokerWarmupController=SantokerWarmupController(),
        santokerWarmupControls=controls,
        pushbuttonstyles={'OFF': '', 'ON': ''},
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isHidden() is (not visible)
    assert not controls.button.isEnabled()
    assert controls.target.isEnabled() is visible
```

Add a target-retention test that transitions the same fake window through OFF, ON, and OFF without sending commands:

```python
def test_warmup_target_survives_off_on_visibility_cycle(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    device = FakeWarmupDevice(ready=False, warmup=False)
    window = compact_window(
        controls,
        controller,
        device,
        monitoring=False,
        recording=False,
    )

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.isHidden()

    window.qmc.flagon = True
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.isVisible()
    assert controls.target.value() == 205
    assert controls.target.isEnabled()
    assert not controls.button.isEnabled()

    window.qmc.flagon = False
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.isHidden()
    assert controller.desired_temp_c == 205.0
    assert device.calls == []
```

Add a logically-ON disconnect test:

```python
def test_transport_disconnect_keeps_warmup_visible_while_monitoring(
    qapplication: QApplication,
) -> None:
    del qapplication
    controls = SantokerWarmupControls()
    window = compact_window(
        controls,
        SantokerWarmupController(desired_temp_c=205.0),
        FakeWarmupDevice(ready=True, warmup=True),
    )
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))
    assert controls.button.isEnabled()
    assert controls.button.isChecked()

    window.santoker = None
    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.isVisible()
    assert controls.target.isEnabled()
    assert not controls.button.isEnabled()
    assert not controls.button.isChecked()
    assert controls.target.value() == 205
```

Add `flagon=True, flagstart=False` to the standalone fake `qmc` objects in the existing readiness and post-CHARGE tests so they continue to model an active session.

- [ ] **Step 3: Run the new tests to verify RED**

Run:

```bash
cd src
.venv/bin/pytest \
  test/unitary/artisanlib/test_santoker_warmup.py::test_window_updates_compact_control_visibility \
  test/unitary/artisanlib/test_santoker_warmup.py::test_warmup_target_survives_off_on_visibility_cycle \
  test/unitary/artisanlib/test_santoker_warmup.py::test_transport_disconnect_keeps_warmup_visible_while_monitoring \
  -v
```

Expected: the `fully-off` case and OFF phase of target retention fail because the current implementation ignores `flagon` and `flagstart`. The monitoring-disconnect test should already pass and acts as a preserved safety contract.

- [ ] **Step 4: Implement the minimal visibility predicate**

Change only the visibility expression in `ApplicationWindow.updateSantokerWarmupControls()`:

```python
def updateSantokerWarmupControls(self) -> None:
    visible = (
        bool(self.santokerWarmup)
        and not self.app.artisanviewerMode
        and (self.qmc.flagon or self.qmc.flagstart)
    )
```

Leave target configuration, readiness, pre-CHARGE gating, checked state, styles, and enablement unchanged.

- [ ] **Step 5: Run focused Task 1 tests GREEN**

Run:

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup.py -v
.venv/bin/ruff check artisanlib/main.py test/unitary/artisanlib/test_santoker_warmup.py
.venv/bin/mypy artisanlib/main.py test/unitary/artisanlib/test_santoker_warmup.py
```

Expected: all commands exit 0; no test expects the X3 pair to be visible in a fully OFF state.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/artisanlib/main.py src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Hide X3 warm-up controls while off"
```

---

### Task 2: Refresh Warm-up visibility through existing lifecycle updates

**Files:**
- Modify: `src/artisanlib/main.py:12003-12025, 20031-20032`
- Modify: `src/artisanlib/canvas.py:14242-14249`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py`
- Modify: `docs/superpowers/specs/2026-07-29-santoker-x3-warmup-visibility-design.md:3`

**Interfaces:**
- Consumes: existing calls to `ApplicationWindow.updateControlsVisibility() -> None` from ON, START, and OFF lifecycle paths, plus `tphasescanvas.OffRecorder() -> None` for STOP.
- Produces: each generic control-visibility refresh invokes `self.updateSantokerWarmupControls() -> None` exactly once, and STOP refreshes immediately after setting `flagstart = False`.
- Preserves: existing `showControls(False)`/`hideControls(False)` behavior and user-configured `controlsflags`.

- [ ] **Step 1: Write the failing lifecycle integration test**

Add a Qt-independent test proving the established generic visibility refresh also refreshes the X3 control:

```python
@pytest.mark.parametrize(
    ('flagon', 'flagstart', 'controls_flag'),
    [(False, False, False), (True, False, True), (False, True, True)],
    ids=['off', 'monitoring', 'recording'],
)
def test_generic_control_visibility_refreshes_warmup_controls(
    flagon: bool,
    flagstart: bool,
    controls_flag: bool,
) -> None:
    refresh_warmup = Mock()
    window = SimpleNamespace(
        qmc=SimpleNamespace(flagon=flagon, flagstart=flagstart),
        controlsflags=[False, True, True],
        controlsVisible=Mock(return_value=not controls_flag),
        showControls=Mock(),
        hideControls=Mock(),
        updateSantokerWarmupControls=refresh_warmup,
    )

    ApplicationWindow.updateControlsVisibility(cast(ApplicationWindow, window))

    if controls_flag:
        window.showControls.assert_called_once_with(False)
        window.hideControls.assert_not_called()
    else:
        window.hideControls.assert_called_once_with(False)
        window.showControls.assert_not_called()
    refresh_warmup.assert_called_once_with()
```

The fake `controlsVisible()` result intentionally reports the opposite state so the existing generic show/hide branch is also exercised.

Add a source-order contract for the otherwise expensive-to-construct STOP lifecycle boundary:

```python
def test_recording_stop_refreshes_warmup_after_flag_change() -> None:
    import inspect

    from artisanlib.canvas import tphasescanvas

    source = inspect.getsource(tphasescanvas.OffRecorder)

    flag_change = source.index('self.flagstart = False')
    refresh = source.index('self.aw.updateSantokerWarmupControls()')
    assert flag_change < refresh
```

- [ ] **Step 2: Run the integration tests to verify RED**

Run:

```bash
cd src
.venv/bin/pytest \
  test/unitary/artisanlib/test_santoker_warmup.py::test_generic_control_visibility_refreshes_warmup_controls \
  test/unitary/artisanlib/test_santoker_warmup.py::test_recording_stop_refreshes_warmup_after_flag_change \
  -v
```

Expected: generic visibility cases fail because `updateControlsVisibility()` does not yet call `updateSantokerWarmupControls()`, and the STOP contract fails because `OffRecorder()` has no Warm-up refresh.

- [ ] **Step 3: Add the lifecycle refresh hooks**

Append one call after the existing generic show/hide decision:

```python
def updateControlsVisibility(self) -> None:
    if self.qmc.flagstart:
        visible = self.controlsflags[2]
    elif self.qmc.flagon:
        visible = self.controlsflags[1]
    else:
        visible = self.controlsflags[0]
    if visible:
        self.showControls(False)
    else:
        self.hideControls(False)
    self.updateSantokerWarmupControls()
```

Because settings loading currently calls both methods consecutively, remove the now-redundant explicit Warm-up call:

The resulting settings-load sequence is:

```python
self.updateControlsVisibility()
self.updateReadingsLCDsVisibility()
```

Immediately after STOP changes the recording flag in `tphasescanvas.OffRecorder()`, add the missing state refresh:

```python
self.flagstart = False
self.aw.updateSantokerWarmupControls()
```

Do not modify Santoker transport callbacks: readiness, target, state, and disconnect paths still need their direct Warm-up refreshes independently of generic control visibility.

- [ ] **Step 4: Run Task 2 tests GREEN**

Run:

```bash
cd src
.venv/bin/pytest \
  test/unitary/artisanlib/test_santoker_warmup.py::test_generic_control_visibility_refreshes_warmup_controls \
  test/unitary/artisanlib/test_santoker_warmup.py::test_recording_stop_refreshes_warmup_after_flag_change \
  test/unitary/artisanlib/test_santoker_warmup.py::test_window_updates_compact_control_visibility \
  test/unitary/artisanlib/test_santoker_warmup.py::test_warmup_target_survives_off_on_visibility_cycle \
  test/unitary/artisanlib/test_santoker_warmup.py::test_transport_disconnect_keeps_warmup_visible_while_monitoring \
  -v
```

Expected: all tests pass. The ON, START, and OFF lifecycle paths refresh through `updateControlsVisibility()`; the only required `canvas.py` change is the explicit STOP refresh shown above.

- [ ] **Step 5: Run focused regression tests in both import orders**

Run:

```bash
cd src
.venv/bin/pytest \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_warmup_ui.py
.venv/bin/pytest \
  test/unitary/artisanlib/test_santoker_warmup_ui.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker.py
```

Expected: both commands pass with identical test counts and no Qt crash.

- [ ] **Step 6: Mark the visibility design implemented and commit Task 2**

Change the design status to:

```markdown
**Status:** Implemented
```

Then commit:

```bash
git add \
  src/artisanlib/main.py \
  src/artisanlib/canvas.py \
  src/test/unitary/artisanlib/test_santoker_warmup.py \
  docs/superpowers/specs/2026-07-29-santoker-x3-warmup-visibility-design.md
git commit -m "Refresh X3 warm-up visibility on roast state changes"
```

---

### Task 3: Branch-level verification

**Files:**
- Verify only; no production or test files should change.

**Interfaces:**
- Consumes: Task 1 and Task 2 commits.
- Produces: fresh verification evidence and a clean worktree ready for review/push.

- [ ] **Step 1: Run focused main-window and smoke tests**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_main.py
.venv/bin/pytest test/smoke/artisanlib/test_main_smoke.py
```

Expected: `test_main.py` has only its registered Linux platform skip; smoke tests pass.

- [ ] **Step 2: Run static checks**

```bash
cd src
.venv/bin/ruff check .
.venv/bin/mypy
. .venv/bin/activate
pyright
codespell
```

Expected: each command exits 0. Pyright must run with the virtual environment activated so PyQt6 and other installed packages resolve.

- [ ] **Step 3: Run the full pytest suite and compare the baseline**

```bash
cd src
. .venv/bin/activate
pytest
```

Expected: all newly added visibility tests pass. The only allowed failures are the documented 11 Qt Mock failures in `test_qcheckcombobox.py`, `test_roastlog.py`, and `test_roastpath.py`; the pass count increases by the number of new tests.

- [ ] **Step 4: Run repository hygiene checks**

From the worktree root:

```bash
git diff --check
base=$(git merge-base master HEAD)
mapfile -t files < <(git diff --name-only "$base")
. src/.venv/bin/activate
pre-commit run --files "${files[@]}"
git status --short
git log -3 --oneline
```

Expected: diff and feature-scoped pre-commit checks pass; status is clean; the design and two implementation commits are present.

- [ ] **Step 5: Request whole-change review**

Review the visibility spec and the diff from `b0c55532f^` through HEAD. Reject any finding that would:

- expose the pair while fully OFF;
- delay visibility until protocol readiness;
- hide the pair on transport loss while Artisan remains ON;
- reset or transmit the cached target on OFF;
- weaken pre-CHARGE/readiness gating; or
- affect non-X3 machine configurations.

Fix any Critical or Important findings using TDD, rerun the narrowest affected checks, and repeat the branch-level verification before claiming completion.
