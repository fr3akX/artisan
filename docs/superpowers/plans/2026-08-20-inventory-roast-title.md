# Inventory Roast Title and Pytest Collection Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make successful inventory-backed CHARGE events name the roast from the inventory item and local timestamp, while restoring terminating macOS pytest CI collection.

**Architecture:** Add a narrow `ApplicationWindow` helper for title formatting and replacement confirmation, then invoke it from `tgraphcanvas._markCharge()` after successful CHARGE and semaphore release. Add concise IDs to large-byte outbox parametrizations so verbose pytest never serializes 16 MiB values into node IDs, and restore the ordinary single-job pytest workflow.

**Tech Stack:** Python 3.12+, PyQt6 `QMessageBox`, `datetime`, pytest, GitHub Actions YAML.

**Spec:** `docs/superpowers/specs/2026-08-20-inventory-roast-title-design.md`

## Global Constraints

- Generate `Inventory item name – YYYY-MM-DD HH:mm` with local time at successful CHARGE.
- Automatically replace only the translated default `Roaster Scope` or an empty title.
- Ask Yes/No before replacing any non-default title, with No as the default.
- Declining title replacement must not cancel CHARGE.
- Failed or cancelled CHARGE and CHARGE without a selected lot must not change the title.
- Do not hold `profileDataSemaphore` while showing a dialog.
- Preserve inventory reservation ordering and all Santoker CHARGE behavior.
- Keep tests independent of live hardware, network, and cloud services.
- Use single-quoted strings and complete annotations for changed production functions.

---

## File Structure

### Create

- `docs/superpowers/specs/2026-08-20-inventory-roast-title-design.md` — approved behavior and architecture.
- `docs/superpowers/plans/2026-08-20-inventory-roast-title.md` — executable implementation plan.

### Modify

- `src/artisanlib/main.py` — title candidate formatting, replacement confirmation, and title assignment.
- `src/artisanlib/canvas.py` — successful post-CHARGE integration point after semaphore release.
- `src/test/unitary/artisanlib/test_main.py` — focused title helper tests.
- `src/test/unitary/artisanlib/test_canvas.py` — CHARGE success/failure and lock-boundary tests.
- `src/test/unitary/artisanlib/roastserver/test_outbox.py` — concise IDs for large byte parameters.
- `src/test/unitary/test_ci_workflows.py` — collection-output regression and ordinary-workflow assertions.
- `.github/workflows/pytest.yaml` — remove temporary diagnostic matrices and restore the normal pytest job.

---

### Task 1: Bound pytest collection output and restore CI

**Files:**
- Modify: `src/test/unitary/test_ci_workflows.py`
- Modify: `src/test/unitary/artisanlib/roastserver/test_outbox.py:627-642`
- Modify: `.github/workflows/pytest.yaml`

**Interfaces:**
- Consumes: pytest's `ids=` option and the existing workflow loader `_workflow(path)`.
- Produces: concise outbox node IDs and a `Test with pytest` workflow step running `pytest` from `src`.

- [ ] **Step 1: Add a failing regression test for concise large-content IDs**

Add an AST-based assertion to `test_ci_workflows.py` that finds both top-level `@pytest.mark.parametrize('content', ...)` decorators in `test_outbox.py` and requires an `ids` keyword containing two short string literals:

```python
import ast

OUTBOX_TEST_PATH = (
    REPOSITORY_ROOT / 'src/test/unitary/artisanlib/roastserver/test_outbox.py'
)


def test_outbox_large_content_parameters_have_concise_ids() -> None:
    module = ast.parse(OUTBOX_TEST_PATH.read_text(encoding='utf-8'))
    content_parameterizations = [
        decorator
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == 'parametrize'
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == 'content'
    ]
    assert len(content_parameterizations) == 2
    for parameterization in content_parameterizations:
        ids = next(
            (keyword.value for keyword in parameterization.keywords if keyword.arg == 'ids'),
            None,
        )
        assert isinstance(ids, (ast.List, ast.Tuple))
        assert len(ids.elts) == 2
        assert all(
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and len(element.value) <= 32
            for element in ids.elts
        )
```

- [ ] **Step 2: Run the regression test and verify RED**

Run from `src/`:

```bash
.venv/bin/pytest test/unitary/test_ci_workflows.py::test_outbox_large_content_parameters_have_concise_ids -v
```

Expected: FAIL because both matching decorators currently omit `ids`.

- [ ] **Step 3: Add concise IDs and restore the workflow**

Change the two content parametrizations to:

```python
@pytest.mark.parametrize(
    'content',
    [b'x', b'x' * MAX_PROFILE_BYTES],
    ids=['minimum', 'maximum'],
)
```

and:

```python
@pytest.mark.parametrize(
    'content',
    [b'', b'x' * (MAX_PROFILE_BYTES + 1)],
    ids=['empty', 'overflow'],
)
```

Restore `.github/workflows/pytest.yaml` to the pre-diagnostic single macOS job named `runner / pytest`, with the step:

```yaml
      - name: Test with pytest
        working-directory: src
        run: |
          pytest
```

- [ ] **Step 4: Verify GREEN and bounded collection output**

Run from `src/`:

```bash
.venv/bin/pytest test/unitary/test_ci_workflows.py -v
.venv/bin/pytest --collect-only -q test/unitary/artisanlib/roastserver/test_outbox.py > /tmp/outbox-collect.txt
wc -c /tmp/outbox-collect.txt
```

Expected: workflow tests PASS and collection output is well below 100 KiB.

- [ ] **Step 5: Commit the collection fix**

```bash
git add .github/workflows/pytest.yaml src/test/unitary/test_ci_workflows.py \
  src/test/unitary/artisanlib/roastserver/test_outbox.py
git commit -m 'Fix verbose outbox test collection'
```

---

### Task 2: Add inventory-derived title behavior

**Files:**
- Modify: `src/test/unitary/artisanlib/test_main.py`
- Modify: `src/artisanlib/main.py:4960-4990`
- Modify: `src/test/unitary/artisanlib/test_canvas.py:1194-1562`
- Modify: `src/artisanlib/canvas.py:14450-14680`

**Interfaces:**
- Consumes: `qmc.roastServerBeanLotName`, `qmc.title`, translated `Roaster Scope`, and successful `charge_marked` state.
- Produces: `ApplicationWindow.updateRoastNameFromInventoryAtCharge() -> None`.

- [ ] **Step 1: Write failing title-helper tests**

Add focused tests that construct `ApplicationWindow.__new__(ApplicationWindow)` with a minimal `qmc`, patch local time to return `2026-08-20 14:37`, and assert:

```python
@pytest.mark.parametrize('current_title', ['', 'Roaster Scope'])
def test_inventory_charge_title_replaces_default_without_prompt(
    current_title: str,
) -> None:
    window = inventory_title_window(current_title)
    with patch('artisanlib.main.datetime.datetime') as datetime_class, patch(
        'artisanlib.main.QMessageBox.question'
    ) as question:
        datetime_class.now.return_value.astimezone.return_value.strftime.return_value = (
            '2026-08-20 14:37'
        )
        window.updateRoastNameFromInventoryAtCharge()
    assert window.qmc.title == 'Historical lot – 2026-08-20 14:37'
    question.assert_not_called()
```

Add separate tests where a custom title is replaced on Yes, preserved on No, and where `roastServerBeanLotName = None` leaves the title unchanged without asking.

- [ ] **Step 2: Run helper tests and verify RED**

Run from `src/`:

```bash
.venv/bin/pytest test/unitary/artisanlib/test_main.py -k 'inventory_charge_title' -v
```

Expected: FAIL with missing `updateRoastNameFromInventoryAtCharge`.

- [ ] **Step 3: Implement the minimal ApplicationWindow helper**

Add:

```python
def updateRoastNameFromInventoryAtCharge(self) -> None:
    inventory_name = self.qmc.roastServerBeanLotName
    if not inventory_name:
        return
    timestamp = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')
    candidate = f'{inventory_name} – {timestamp}'
    default_title = QApplication.translate('Scope Title', 'Roaster Scope')
    if self.qmc.title not in {'', default_title}:
        reply = QMessageBox.question(
            self,
            QApplication.translate('Message', 'Replace Roast Name'),
            QApplication.translate(
                'Message', 'Replace the current roast name with "{0}"?'
            ).format(candidate),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
    self.qmc.title = candidate
```

Place it beside the existing inventory CHARGE coordinator methods.

- [ ] **Step 4: Verify helper tests GREEN**

Run from `src/`:

```bash
.venv/bin/pytest test/unitary/artisanlib/test_main.py -k 'inventory_charge_title' -v
```

Expected: all title-helper tests PASS.

- [ ] **Step 5: Write failing canvas integration tests**

Extend the successful CHARGE test so `aw.updateRoastNameFromInventoryAtCharge` asserts the profile semaphore is released when called. Extend the commit-failure test to assert the helper was not called.

- [ ] **Step 6: Run canvas integration tests and verify RED**

Run from `src/`:

```bash
.venv/bin/pytest test/unitary/artisanlib/test_canvas.py::TestInventoryCharge -v
```

Expected: FAIL because `_markCharge()` does not call the helper.

- [ ] **Step 7: Call the helper after successful CHARGE and lock release**

Immediately after the `finally` that releases `profileDataSemaphore`, add:

```python
if charge_marked:
    self.aw.updateRoastNameFromInventoryAtCharge()
```

Keep this before `timealign()` so the ordinary redraw includes the new title.

- [ ] **Step 8: Verify title and CHARGE tests GREEN**

Run from `src/`:

```bash
.venv/bin/pytest test/unitary/artisanlib/test_main.py -k 'inventory_charge_title' -v
.venv/bin/pytest test/unitary/artisanlib/test_canvas.py::TestInventoryCharge -v
```

Expected: all selected tests PASS.

- [ ] **Step 9: Commit the title feature**

```bash
git add src/artisanlib/main.py src/artisanlib/canvas.py \
  src/test/unitary/artisanlib/test_main.py src/test/unitary/artisanlib/test_canvas.py \
  docs/superpowers/specs/2026-08-20-inventory-roast-title-design.md \
  docs/superpowers/plans/2026-08-20-inventory-roast-title.md
git commit -m 'Name inventory roasts at charge'
```

---

### Task 3: Validate the complete PR

**Files:**
- Verify all modified files.

**Interfaces:**
- Consumes: completed Tasks 1 and 2.
- Produces: evidence that the focused behavior, collection path, static checks, and repository diff are sound.

- [ ] **Step 1: Run focused regression suites**

From `src/`:

```bash
.venv/bin/pytest test/unitary/test_ci_workflows.py \
  test/unitary/artisanlib/roastserver/test_outbox.py \
  test/unitary/artisanlib/test_main.py -k 'inventory_charge_title or inventory_charge' \
  -v
.venv/bin/pytest test/unitary/artisanlib/test_canvas.py::TestInventoryCharge -v
```

Expected: PASS without collection stalls.

- [ ] **Step 2: Run static checks on changed Python files**

From `src/`:

```bash
.venv/bin/ruff check artisanlib/main.py artisanlib/canvas.py \
  test/unitary/test_ci_workflows.py \
  test/unitary/artisanlib/roastserver/test_outbox.py \
  test/unitary/artisanlib/test_main.py test/unitary/artisanlib/test_canvas.py
.venv/bin/mypy artisanlib/main.py artisanlib/canvas.py
```

Expected: PASS.

- [ ] **Step 3: Run full pytest**

From `src/`:

```bash
.venv/bin/pytest
```

Expected: suite terminates; classify any known unrelated mock-isolation failures separately.

- [ ] **Step 4: Inspect repository state**

```bash
git status --short
git diff --check
git diff 3ac4fc8d --stat
git log --oneline --decorate -12
```

Expected: no uncommitted files, no whitespace errors, and no temporary diagnostic files or workflow matrices.

- [ ] **Step 5: Push and observe CI**

```bash
git push fork feature/santoker-reconnect-diagnostics
```

Expected: PR #7 starts the ordinary macOS pytest job and collection progresses without multi-megabyte node IDs.
