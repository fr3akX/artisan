# Santoker X3 Compact Warm-up Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the X3 Warm-up slider and lower custom button with a compact, report-aware top-bar toggle and numeric target field between ON and START.

**Architecture:** Keep protocol state and validation in `Santoker` and `SantokerWarmupController`. Add a small Qt presentation component for the vertical button/spin-box pair, while `ApplicationWindow` owns capability, lifecycle, command, and signal integration. Identify support with an explicit X3 preset setting instead of hidden semantic controls.

**Tech Stack:** Python 3.12+, PyQt6, asyncio, QSettings/INI machine presets, pytest, pytest-qt-compatible real widgets, Ruff, mypy, pyright, Qt translation tools.

## Global Constraints

- Keep raw `santoker(<target>,<value>)`, semantic command syntax, and all existing Santoker presets backward-compatible.
- Never send Machine ON target `0x7A` as part of Warm-up.
- Warm-up ON sends target `0x7F` before state `0x7E=1`.
- Warm-up OFF precedes CHARGE target `0x80=1`.
- Warm-up is unavailable until a complete accepted packet establishes header readiness and after the first accepted CHARGE until RESET.
- The target range is inclusive 100–300°C; the default is 190°C; Fahrenheit display range/default is 212–572°F / 374°F.
- Typed input commits on Enter or focus loss; spin arrows apply immediately; programmatic updates emit no commands.
- The compact pair is visible only when `[Device] santokerWarmup=true` and never in ArtisanViewer.
- Keep all Qt widget updates on the UI thread via signals/slots.
- Do not require or contact BLE hardware, roasting hardware, cloud services, or network endpoints in tests.
- Existing Santoker Q/X, R-series, and Cube preset files must remain byte-for-byte unchanged.
- Preserve the known unrelated full-suite baseline failures in `test_qcheckcombobox.py`, `test_roastlog.py`, and `test_roastpath.py`; do not weaken or modify them.
- Edit source translation strings and run `src/build-derived.sh`; do not hand-edit generated `.ts` or `.qm` files.

---

## File Structure

### Create

- `src/artisanlib/santoker_warmup_ui.py` — presentation-only Qt frame containing the vertical toggle and numeric input.
- `src/test/unitary/artisanlib/test_santoker_warmup_ui.py` — real-widget tests for layout, keyboard tracking, unit configuration, and signal blocking.

### Modify

- `src/artisanlib/santoker.py` — readiness transition callback after accepted frames and on reset/disconnect.
- `src/artisanlib/santoker_warmup.py` — CHARGE latch and removal of hidden-control lookup helpers once consumers are migrated.
- `src/artisanlib/main.py` — capability persistence, top-bar ownership, signal/slot integration, visibility, target/state reconciliation, and semantic command compatibility.
- `src/artisanlib/canvas.py` — readiness callback wiring, unit-switch refresh, accepted-CHARGE latch, and RESET refresh.
- `src/includes/Machines/Santoker/X3_Master_Bluetooth.aset` — capability flag and removal of old slider/custom-button controls.
- `src/test/unitary/artisanlib/test_santoker.py` — readiness callback behavior.
- `src/test/unitary/artisanlib/test_santoker_warmup.py` — controller, application integration, lifecycle, compatibility, and preset contracts.
- `SANTOKER_X3_PREHEAT_PROTOCOL.md` — compact-control implementation status.
- `src/translations/artisan_*.ts` and `src/translations/artisan_*.qm` — generated translations for the new button/tooltips.

---

### Task 1: Publish Validated Protocol Readiness Transitions

**Files:**
- Modify: `src/artisanlib/santoker.py:141-190, 270-282, 390-448`
- Test: `src/test/unitary/artisanlib/test_santoker.py:1008-1100`

**Interfaces:**
- Consumes: existing complete-frame validation in `Santoker.read_msg()` and `resetProtocolState()`.
- Produces: constructor parameter `ready_handler: Callable[[bool], None] | None = None` and change-only readiness callbacks `True` after an accepted frame, `False` after readiness reset.

- [ ] **Step 1: Add failing readiness callback tests**

Extend the existing valid, invalid, truncated, and disconnect tests with a `Mock` callback:

```python
@pytest.mark.asyncio
async def test_readiness_handler_tracks_only_accepted_transitions(self) -> None:
    sender = Santoker()
    ready_handler = Mock()
    receiver = Santoker(connect_using_ble=True, ready_handler=ready_handler)
    packet = sender.create_msg(Santoker.WARMUP_TEMP, 1900)

    await read_packet(receiver, packet)
    await read_packet(receiver, packet)
    receiver.resetProtocolState()
    receiver.resetProtocolState()

    assert ready_handler.call_args_list == [call(True), call(False)]
```

For CRC, tail, and truncated packets, assert `ready_handler.assert_not_called()`. Extend the disconnect test to assert the `False` callback occurs before the external disconnected handler observes state.

- [ ] **Step 2: Run the new protocol tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py::TestSantokerWarmupProtocol::test_readiness_handler_tracks_only_accepted_transitions -v
pytest test/unitary/artisanlib/test_santoker.py -k 'header_ready or readiness or disconnect_handler' -v
```

Expected: FAIL because `Santoker.__init__()` does not accept `ready_handler` and no readiness callback is emitted.

- [ ] **Step 3: Implement change-only readiness notification**

Add `_ready_handler` to `__slots__`, accept/store the typed callback, and centralize transitions:

```python
def _setHeaderReady(self, ready: bool) -> None:
    if ready != self._header_ready:
        self._header_ready = ready
        if self._ready_handler is not None:
            try:
                self._ready_handler(ready)
            except Exception as e:  # pylint: disable=broad-except
                _log.exception(e)
```

In `read_msg()`, assign the validated candidate header and then call `_setHeaderReady(True)` only after CRC/tail acceptance. In `resetProtocolState()`, call `_setHeaderReady(False)` before resetting reported state. Do not invoke readiness from transport connection alone.

- [ ] **Step 4: Run protocol tests GREEN**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py -v
ruff check artisanlib/santoker.py test/unitary/artisanlib/test_santoker.py
```

Expected: all Santoker tests pass; Ruff reports no issues.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/artisanlib/santoker.py src/test/unitary/artisanlib/test_santoker.py
git commit -m "Add Santoker protocol readiness callbacks"
```

---

### Task 2: Build the Compact Qt Presentation Component

**Files:**
- Create: `src/artisanlib/santoker_warmup_ui.py`
- Create: `src/test/unitary/artisanlib/test_santoker_warmup_ui.py`

**Interfaces:**
- Consumes: display units `Literal['C', 'F']`, integer display values, and main-window styles.
- Produces:
  - `SantokerWarmupControls(QFrame)`
  - signal `enabledChanged: pyqtSignal(bool)` from user clicks
  - signal `targetChanged: pyqtSignal(int)` from committed edits/arrow steps
  - public widgets `button: QPushButton` and `target: QSpinBox`
  - `configureTarget(unit: Literal['C', 'F'], value: float) -> None`
  - `setState(enabled: bool) -> None`

- [ ] **Step 1: Write failing real-widget tests**

Create the test module with `QT_QPA_PLATFORM=offscreen` set before PyQt imports and a module-scoped `QApplication` fixture. Cover vertical order, checkability, range/default, keyboard behavior, and programmatic blocking:

```python
def test_compact_controls_layout_and_defaults(qapplication: QApplication) -> None:
    controls = SantokerWarmupControls()

    assert controls.layout().itemAt(0).widget() is controls.button
    assert controls.layout().itemAt(1).widget() is controls.target
    assert controls.button.isCheckable()
    assert not controls.button.isChecked()
    assert not controls.target.keyboardTracking()


def test_configure_target_blocks_user_signal(qapplication: QApplication) -> None:
    controls = SantokerWarmupControls()
    changed = Mock()
    controls.targetChanged.connect(changed)

    controls.configureTarget('F', 374.0)

    assert (controls.target.minimum(), controls.target.maximum()) == (212, 572)
    assert controls.target.value() == 374
    assert controls.target.suffix() == ' °F'
    changed.assert_not_called()
```

Use `controls.target.stepUp()` to assert arrow steps emit immediately. Use `controls.target.lineEdit().setText('205')` to assert typing does not emit until Enter/focus commit; use `QTest.keyClick(..., Qt.Key.Key_Return)` for the commit.

- [ ] **Step 2: Run the new UI tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup_ui.py -v
```

Expected: collection FAIL because `artisanlib.santoker_warmup_ui` does not exist.

- [ ] **Step 3: Implement the presentation-only frame**

Create the production module with the project AGPL header and no protocol/controller imports. Build a zero-margin `QVBoxLayout`, a checkable translated `WARM-UP` button, and an integer spin box below it:

```python
class SantokerWarmupControls(QFrame):
    enabledChanged = pyqtSignal(bool)
    targetChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.button = QPushButton(QApplication.translate('Button', 'WARM-UP'))
        self.button.setCheckable(True)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setToolTip(QApplication.translate('Tooltip', 'Santoker warm-up'))
        self.target = QSpinBox()
        self.target.setKeyboardTracking(False)
        self.target.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.target.setToolTip(QApplication.translate('Tooltip', 'Warm-up target'))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.button)
        layout.addWidget(self.target)
        self.button.clicked.connect(self.enabledChanged.emit)
        self.target.valueChanged.connect(self.targetChanged.emit)
```

`configureTarget()` must block `target` signals in `try/finally`, set C/F range and suffix, and round the display value. `setState()` blocks button signals and calls `setChecked()`.

- [ ] **Step 4: Run UI tests GREEN and type checks**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup_ui.py -v
ruff check artisanlib/santoker_warmup_ui.py test/unitary/artisanlib/test_santoker_warmup_ui.py
mypy artisanlib/santoker_warmup_ui.py
```

Expected: all component tests pass; Ruff and mypy report no issues.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/artisanlib/santoker_warmup_ui.py src/test/unitary/artisanlib/test_santoker_warmup_ui.py
git commit -m "Add compact Santoker warm-up controls"
```

---

### Task 3: Add X3 Capability and Top-Bar Ownership

**Files:**
- Modify: `src/artisanlib/main.py:100-125, 1440-1530, 1838-1855, 3218-3245, 3938-3960, 18495-18530, 19960-19985, 20590-20610`
- Modify: `src/includes/Machines/Santoker/X3_Master_Bluetooth.aset:6-13`
- Test: `src/test/unitary/artisanlib/test_santoker_warmup.py`
- Test: `src/test/unitary/artisanlib/test_santoker_warmup_ui.py`

**Interfaces:**
- Consumes: `SantokerWarmupControls` from Task 2.
- Produces:
  - application setting/attribute `santokerWarmup: bool`
  - widget attribute `santokerWarmupControls: SantokerWarmupControls`
  - `updateSantokerWarmupControls() -> None` for visibility, display unit/value, and safe enabled state.

- [ ] **Step 1: Add failing capability and ownership tests**

Extend the preset contract to require:

```python
assert config.getboolean('Device', 'santokerWarmup')
```

Add unbound-method tests using a real `SantokerWarmupControls` and a typed `SimpleNamespace` to prove capability visibility and safe initial state:

```python
@pytest.mark.parametrize(
    ('capability', 'viewer', 'visible'),
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_window_updates_compact_control_visibility(
    qapplication: QApplication, capability: bool, viewer: bool, visible: bool
) -> None:
    controls = SantokerWarmupControls()
    window = SimpleNamespace(
        app=SimpleNamespace(artisanviewerMode=viewer),
        qmc=SimpleNamespace(mode_tempsliders='C', timeindex=[-1]),
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

The preset contract proves the capability is supplied by X3 settings. Review the load/save diff directly to ensure the key is read beside `santokerBLE` and written beside the same Santoker settings; the default-value visibility test proves absent settings remain false.

- [ ] **Step 2: Run capability tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -k 'capability or compact_control_visibility or preset_contract' -v
```

Expected: FAIL because the setting, widget attribute, and update method do not exist.

- [ ] **Step 3: Persist capability and create the top control**

In `ApplicationWindow`:

- import `SantokerWarmupControls`;
- add `santokerWarmup` and `santokerWarmupControls` to `__slots__`;
- initialize `self.santokerWarmup = False` with other Santoker settings;
- load with `toBool(settings.value('santokerWarmup', self.santokerWarmup))`;
- save through `settingsSetValue(..., 'santokerWarmup', self.santokerWarmup, ...)`;
- construct the frame with the ON/START buttons;
- size it to the existing top-button height and apply `pushbuttonstyles['OFF']` initially;
- insert it after `buttonONOFF` and before `buttonSTARTSTOP` in `level1layout`;
- keep it hidden initially and in ArtisanViewer.

Add `santokerWarmup=true` to the X3 preset without removing old controls yet.

Implement `updateSantokerWarmupControls()` so it:

1. computes visibility from capability and viewer mode;
2. configures the field from controller target and current slider-display unit;
3. queries `self.santoker.isHeaderReady()` when a device exists;
4. disables the button unless visible, ready, and `self.qmc.timeindex[0] == -1`;
5. keeps the target enabled whenever visible; and
6. applies checked/unchecked styles without emitting actions.

Call it near `updateControlsVisibility()` at the end of `settingsLoad()` so machine preset loads immediately update the top bar.

- [ ] **Step 4: Run capability/UI integration tests GREEN**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -k 'capability or compact_control_visibility or preset_contract' -v
pytest test/unitary/artisanlib/test_santoker_warmup_ui.py -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
ruff check artisanlib/main.py test/unitary/artisanlib/test_santoker_warmup.py
```

Expected: selected tests, smoke tests, and Ruff pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/artisanlib/main.py src/includes/Machines/Santoker/X3_Master_Bluetooth.aset src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Place X3 warm-up controls in top bar"
```

---

### Task 4: Integrate Numeric Input, Reports, and Readiness

**Files:**
- Modify: `src/artisanlib/main.py:1445-1455, 4300-4315, 8840-8875, 18020-18145`
- Modify: `src/artisanlib/canvas.py:12289-12310, 13220-13245`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py:150-360, 500-620`
- Test: `src/test/unitary/artisanlib/test_santoker_warmup_ui.py`

**Interfaces:**
- Consumes: `ready_handler(bool)` from Task 1, component signals from Task 2, capability from Task 3, and existing controller methods.
- Produces:
  - Qt signal `santokerWarmupReadySignal = pyqtSignal(bool)`
  - slots `santokerWarmupReadyChanged(ready: bool) -> None` and `santokerWarmupTargetEdited(display_temp: int) -> None`
  - direct compact-control reconciliation for readiness, state, and target.

- [ ] **Step 1: Replace old slider/button tests with failing compact-control behavior tests**

Delete tests whose asserted behavior is specifically the old semantic slider/custom button:

- semantic control lookup;
- custom-button style lookup;
- Warm-up slider movement;
- preset-load slider initialization;
- runtime slider-3 conversion;
- no-event custom-button processing.

Replace them with tests that use `SantokerWarmupControls` and verify:

```python
def test_reported_target_updates_spinbox_without_command(
    qapplication: QApplication,
) -> None:
    controls = SantokerWarmupControls()
    device = FakeWarmupDevice()
    controller = SantokerWarmupController()
    changed = Mock()
    controls.targetChanged.connect(changed)
    window = compact_window(controls, controller, device, unit='C')

    ApplicationWindow.santokerWarmupTargetChanged(
        cast(ApplicationWindow, window), 205.0
    )

    assert controls.target.value() == 205
    assert controller.desired_temp_c == 205.0
    changed.assert_not_called()
    assert device.calls == []
```

Add cases for:

- readiness false/true enabling only before CHARGE;
- clicking the real checkable button calling target-first controller behavior;
- rejected clicks reverting check state;
- target field edits caching while inactive and sending while active;
- valid `0x7E` report updating checked state without command emission;
- C→F and F→C refresh preserving controller target and unrelated event sliders.

- [ ] **Step 2: Run integration tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -k 'spinbox or readiness or compact or reported_target or temperature_mode' -v
```

Expected: FAIL because callbacks and report handlers still target old slider/custom controls.

- [ ] **Step 3: Wire readiness and compact control interactions**

In `ApplicationWindow`:

- add/connect `santokerWarmupReadySignal`;
- connect `santokerWarmupControls.enabledChanged` to `setSantokerWarmup`;
- connect `santokerWarmupControls.targetChanged` to `santokerWarmupTargetEdited`;
- make `setSantokerWarmupButtonState()` call component `setState()` and apply `pushbuttonstyles['ON']` when checked or `['OFF']` when unchecked;
- make `santokerWarmupTargetChanged()` accept the controller report and call component `configureTarget()`;
- make `santokerWarmupStateChanged()` gate on `self.santokerWarmup`, reconcile state, and update the component;
- make `santokerWarmupReadyChanged()` refresh controls only for capable settings;
- make `santokerWarmupTargetEdited()` call existing `setSantokerWarmupTarget(float)`;
- retain `santokerWarmupButtonStateSignal` as the queued restoration boundary for semantic commands invoked from `EventActionThread`.

In `canvas.py`, pass:

```python
ready_handler=self.aw.santokerWarmupReadySignal.emit
```

when constructing `Santoker`.

Replace `initializeSantokerWarmupSlider()` with compact target configuration. In `adjustTempSliders()`, call `self.aw.updateSantokerWarmupControls()` after changing `mode_tempsliders`; do not touch slider 3 on behalf of Warm-up.

- [ ] **Step 4: Run integration tests GREEN in both import orders**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup_ui.py -v
pytest test/unitary/artisanlib/test_santoker_warmup_ui.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker.py -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
ruff check artisanlib/main.py artisanlib/canvas.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup_ui.py
mypy
```

Expected: focused tests pass in both orders; smoke, Ruff, and mypy pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/artisanlib/main.py src/artisanlib/canvas.py src/test/unitary/artisanlib/test_santoker_warmup.py src/test/unitary/artisanlib/test_santoker_warmup_ui.py
git commit -m "Wire compact X3 warm-up interactions"
```

---

### Task 5: Latch CHARGE Until RESET and Preserve Serialization

**Files:**
- Modify: `src/artisanlib/santoker_warmup.py:20-175`
- Modify: `src/artisanlib/main.py:18020-18145`
- Modify: `src/artisanlib/canvas.py:7979-8320, 14340-14535`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py`

**Interfaces:**
- Consumes: controller serialization and compact-control update method.
- Produces:
  - `SantokerWarmupController.mark_charge() -> None`
  - `SantokerWarmupController.reset_charge() -> None`
  - `SantokerWarmupController.is_charge_latched() -> bool`
  - ON/report gating that considers either the latch or serialized `charge_index`.

- [ ] **Step 1: Add failing CHARGE-latch lifecycle tests**

Add controller tests:

```python
def test_charge_latch_blocks_on_until_reset() -> None:
    controller = SantokerWarmupController()
    device = FakeWarmupDevice()

    controller.mark_charge()
    assert controller.is_charge_latched()
    assert controller.set_enabled(True, -1, device) is WarmupResult.AFTER_CHARGE

    controller.reset_charge()
    assert not controller.is_charge_latched()
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
```

Extend the deterministic barrier test so the accepted CHARGE calls `mark_charge()` inside the same serialized section and proves:

- ON completes before CHARGE takes the lock;
- OFF precedes raw `0x80`;
- a later ON attempt remains rejected even if an undo sets `timeindex[0] = -1`;
- RESET clears the latch;
- the compact button remains disabled until reset and readiness.

Add a late `0x7E=1` report case after CHARGE undo that still forces OFF.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -k 'charge_latch or serialized or after_charge or reset' -v
```

Expected: FAIL because the controller has no persistent CHARGE latch.

- [ ] **Step 3: Implement the controller latch and lifecycle hooks**

Add an internal non-init field:

```python
_charge_latched: bool = field(default=False, init=False, repr=False, compare=False)
```

All access uses `serialized()`. `set_enabled(True, ...)` and `reconcile_reported_state(True, ...)` treat `self._charge_latched or charge_index > -1` as post-CHARGE.

In `_markCharge()`, once a new CHARGE index is successfully assigned and while `runSantokerWarmupCharge()` still holds the controller lock, call `self.aw.santokerWarmupController.mark_charge()` and `self.aw.updateSantokerWarmupControls()`. The refresh must uncheck and disable the button before scheduling the existing CHARGE event action. Do not clear the latch when CHARGE is undone.

After a successful `reset()` clears measurements, call `reset_charge()` and refresh the compact controls. If RESET disconnected the Santoker object, the button remains disabled; if an internal reset retained a ready connection, it may enable again.

Change capability gates in `runSantokerWarmupCharge()`, report reconciliation, and UI refresh from `has_warmup_controls(...)` to `self.santokerWarmup`.

- [ ] **Step 4: Run lifecycle and regression tests GREEN**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -v
pytest test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup_ui.py -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
ruff check artisanlib/santoker_warmup.py artisanlib/main.py artisanlib/canvas.py test/unitary/artisanlib/test_santoker_warmup.py
mypy
pyright
```

Expected: all focused/smoke tests and static checks pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/artisanlib/santoker_warmup.py src/artisanlib/main.py src/artisanlib/canvas.py src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Latch X3 warm-up safety after charge"
```

---

### Task 6: Remove Legacy Preset Controls and Strengthen Contracts

**Files:**
- Modify: `src/includes/Machines/Santoker/X3_Master_Bluetooth.aset`
- Modify: `src/artisanlib/santoker_warmup.py`
- Modify: `src/artisanlib/main.py`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py:620-end`

**Interfaces:**
- Consumes: capability-driven top control from Tasks 3–5.
- Produces: final X3 preset with no Warm-up slider/custom button and no production dependency on `find_warmup_slider()`, `find_warmup_buttons()`, or `has_warmup_controls()`.

- [ ] **Step 1: Change preset contract tests first**

Update the contract to expect the original Q/X-style slider arrays with Warm-up removed:

```python
assert parse_ini_array(config.get('Sliders', 'slidercommands')) == [
    'santoker(ca,{})', 'santoker(c0,{})', '', 'santoker(fa,{})'
]
assert parse_ini_array(config.get('Sliders', 'eventslidertemp')) == ['0', '0', '0', '0']
assert parse_ini_array(config.get('Sliders', 'slidervisibilities')) == ['1', '0', '0', '1']
```

Require every parallel custom-button array to contain eight entries, require no `santokerWarmup(` custom action/`WARM-UP` label, and retain:

```python
assert default_actions[0].split(';') == [
    'santokerWarmup(0)', 'santoker(80,1)'
]
```

Assert exact extra communication arrays still contain two entries, telemetry remains `135, 136`, and no command includes `santoker(7a`.

Snapshot hashes or `git diff --exit-code` comparisons must prove all pre-existing Santoker preset files are unchanged.

- [ ] **Step 2: Run preset contract test and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py::test_x3_master_bluetooth_preset_contract -v
```

Expected: FAIL because slider 3 and the ninth custom button are still configured.

- [ ] **Step 3: Remove old controls and dead lookup logic**

Restore slider 3 entries to inactive values while retaining the four-element parallel arrays. Remove the ninth custom button entry from every parallel array and restore `buttonpalette` metadata lengths only through Artisan UI if serialized palette data must change; do not hand-synthesize Qt `@Variant` data.

After all application consumers use `self.santokerWarmup`, remove `find_warmup_slider()`, `find_warmup_buttons()`, `has_warmup_controls()`, their imports, and their obsolete tests. Keep semantic command parsing and typed controller methods.

- [ ] **Step 4: Run preset and compatibility tests GREEN**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py::test_x3_master_bluetooth_preset_contract -v
pytest test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup_ui.py -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
ruff check artisanlib/santoker_warmup.py artisanlib/main.py test/unitary/artisanlib/test_santoker_warmup.py
```

Expected: preset contract, focused tests, smoke, and Ruff pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/includes/Machines/Santoker/X3_Master_Bluetooth.aset src/artisanlib/santoker_warmup.py src/artisanlib/main.py src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Replace X3 warm-up preset controls"
```

---

### Task 7: Update Documentation, Generate Translations, and Verify

**Files:**
- Modify: `SANTOKER_X3_PREHEAT_PROTOCOL.md`
- Modify: `docs/superpowers/specs/2026-07-29-santoker-x3-compact-warmup-controls-design.md`
- Generate: `src/translations/artisan_*.ts`
- Generate: `src/translations/artisan_*.qm`
- Verify all files changed since `d5fd1b1d3887c2f4a05b99fd0dbf13414549d8ae`

**Interfaces:**
- Consumes: final implementation from Tasks 1–6.
- Produces: accurate operator/developer documentation, generated translation catalogs, and final branch verification evidence.

- [ ] **Step 1: Update implementation documentation**

Change the protocol document's UI section from slider/custom-button wording to:

- compact top-bar toggle between ON and START;
- numeric target below the button;
- explicit X3 capability;
- typed versus arrow commit behavior;
- readiness/CHARGE/RESET enablement; and
- physical verification note that Warm-up activation has been observed on an X3 while full report/target behavior remains bounded by available device testing.

Mark the compact-control design status `Implemented` only after Tasks 1–6 pass. Retain the static-analysis caveat for unverified protocol areas.

- [ ] **Step 2: Run translation generation**

From `src/`:

```bash
./build-derived.sh
```

Expected: exit 0. Retain only source locations/messages for `WARM-UP`, `Santoker warm-up`, and `Warm-up target`, plus deterministic `.qm` recompilation. Revert unrelated generated churn.

- [ ] **Step 3: Run focused verification in both orders**

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup_ui.py -v
pytest test/unitary/artisanlib/test_santoker_warmup_ui.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker.py -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
ruff check artisanlib/santoker.py artisanlib/santoker_warmup.py artisanlib/santoker_warmup_ui.py artisanlib/main.py artisanlib/canvas.py test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup_ui.py
mypy
pyright
codespell ../SANTOKER_X3_PREHEAT_PROTOCOL.md ../docs/superpowers/specs/2026-07-29-santoker-x3-compact-warmup-controls-design.md
```

Expected: all focused/smoke tests and static checks pass.

- [ ] **Step 4: Run full-suite baseline comparison**

```bash
cd src
pytest
```

Expected: no Santoker or compact-control failures. If the known repository baseline remains, report exactly the 11 unrelated failures in `test_qcheckcombobox.py`, `test_roastlog.py`, and `test_roastpath.py`; do not call the full suite green.

- [ ] **Step 5: Run repository hygiene and inspect compatibility**

```bash
git diff --check
git diff d5fd1b1d3887c2f4a05b99fd0dbf13414549d8ae..HEAD -- src/includes/Machines/Santoker
git diff --name-only d5fd1b1d3887c2f4a05b99fd0dbf13414549d8ae..HEAD
src/.venv/bin/pre-commit run --files $(git diff --name-only d5fd1b1d3887c2f4a05b99fd0dbf13414549d8ae..HEAD)
```

Expected: diff-check and feature-scoped hooks pass; only `X3_Master_Bluetooth.aset` changes under existing Santoker presets. Confirm no `0x7A` command, no raw-command parser regression, no hand-edited generated output, and a clean worktree after the final commit.

- [ ] **Step 6: Commit Task 7**

```bash
git add SANTOKER_X3_PREHEAT_PROTOCOL.md docs/superpowers/specs/2026-07-29-santoker-x3-compact-warmup-controls-design.md src/translations
git commit -m "Document compact X3 warm-up controls"
```

- [ ] **Step 7: Final branch review and push only after approval**

Request a whole-branch review against the design and this plan. Fix all Critical/Important findings with focused RED/GREEN tests, rerun affected checks, and compare full-suite output to the known baseline. Push `feature/santoker-x3-warmup` to `fork` only after the reviewer finds no open Critical/Important issues and the user approves integration.
