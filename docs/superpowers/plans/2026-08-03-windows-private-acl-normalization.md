# Windows Private ACL Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the Roast Server outbox and cache to initialize on Windows while retaining exact private ACL verification for regular files and directories.

**Architecture:** `_WindowsNativeLayer` will construct and verify object-type-specific ACE inheritance flags. Directories retain object/container inheritance; regular files use the zero flags to which Windows normalizes file ACLs. All SID, rights, ACE-count, DACL-protection, readonly, and reparse-point checks remain strict.

**Tech Stack:** Python 3.12+, ctypes Win32 security APIs, pytest, PyQt6 application packaging, AppVeyor Windows build.

## Global Constraints

- Never weaken the protected single-current-user full-control DACL invariant.
- Directories require `OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE`; regular files require `0` ACE inheritance flags.
- Preserve readonly verification for mode `0o400` and writable verification for other private modes.
- Preserve POSIX behavior unchanged.
- Never access roasting hardware, BLE hardware, external cloud accounts, or bearer credentials in automated tests.
- Deliver through `ci/appveyor-unsigned-pr` / PR #2 so AppVeyor uses its unsigned pull-request build path.

---

### Task 1: Normalize Windows ACL inheritance by object type

**Files:**
- Modify: `src/artisanlib/roastserver/_filesystem.py:565-750`
- Test: `src/test/unitary/artisanlib/roastserver/test_outbox.py:2015-2130,2230-2260`

**Interfaces:**
- Consumes: `_WindowsNativeLayer._attributes(handle: int) -> int`, `_FILE_ATTRIBUTE_DIRECTORY`, `_OBJECT_INHERIT_ACE`, and `_CONTAINER_INHERIT_ACE`.
- Produces: `_security_descriptor(sid: str, *, directory: bool) -> tuple[Any, Any]` and `_verify_private_dacl(dacl: Any, expected_sid: Any, *, protected: bool, expected_flags: int) -> None`.

- [ ] **Step 1: Add failing portable ACL parser tests**

Update `test_windows_private_acl_parser_requires_exact_valid_sid_ace` so it defines directory and file ACEs, accepts each only with its matching expected flags, and rejects cross-object or unexpected flags:

```python
directory_flags = (
    layer_type._OBJECT_INHERIT_ACE | layer_type._CONTAINER_INHERIT_ACE
)
directory_ace = (
    layer_type._ACCESS_ALLOWED_ACE_TYPE,
    directory_flags,
    layer_type._FILE_ALL_ACCESS,
    True,
    0,
    True,
)
file_ace = (
    layer_type._ACCESS_ALLOWED_ACE_TYPE,
    0,
    layer_type._FILE_ALL_ACCESS,
    True,
    0,
    True,
)
for ace, expected_flags in ((directory_ace, directory_flags), (file_ace, 0)):
    layer, expected_sid = _windows_acl_layer((ace,))
    layer._verify_private_dacl(
        ctypes.c_void_p(1),
        expected_sid,
        protected=True,
        expected_flags=expected_flags,
    )

for ace, expected_flags in ((directory_ace, 0), (file_ace, directory_flags)):
    layer, expected_sid = _windows_acl_layer((ace,))
    with pytest.raises(OSError, match='ACL'):
        layer._verify_private_dacl(
            ctypes.c_void_p(1),
            expected_sid,
            protected=True,
            expected_flags=expected_flags,
        )
```

Retain the existing malformed ACE cases and pass `expected_flags=directory_flags` to each existing verifier invocation.

- [ ] **Step 2: Add a native Windows file/directory regression test**

Add a gated test that exercises the real Win32 ACL implementation on both object types:

```python
@pytest.mark.win32
def test_windows_runtime_private_acl_accepts_normalized_file_flags(
    tmp_path: Path,
) -> None:
    directory = tmp_path / 'private'
    directory.mkdir()
    regular_file = directory / 'lock'
    regular_file.touch()
    native = outbox_module._WINDOWS_NATIVE
    assert native is not None

    native.set_private_permissions(directory, 0o700)
    native.set_private_permissions(regular_file, 0o600)

    native.verify_private_permissions(directory, 0o700)
    native.verify_private_permissions(regular_file, 0o600)
```

- [ ] **Step 3: Run the portable tests to verify RED**

Run from `src/`:

```bash
pytest test/unitary/artisanlib/roastserver/test_outbox.py::test_windows_private_acl_parser_requires_exact_valid_sid_ace -q
```

Expected: FAIL because `_verify_private_dacl` does not accept `expected_flags` and still requires directory flags unconditionally.

- [ ] **Step 4: Implement object-type-specific SDDL and verification**

Change `_security_descriptor` to generate inheritance flags only for directories:

```python
def _security_descriptor(self, sid: str, *, directory: bool) -> tuple[Any, Any]:
    # existing ctypes setup remains unchanged
    ace_flags = 'OICI' if directory else ''
    sddl = f'D:P(A;{ace_flags};FA;;;{sid})'
    # existing conversion and DACL extraction remain unchanged
```

Change `_verify_private_dacl` to consume the exact expected flags:

```python
def _verify_private_dacl(
    self,
    dacl: Any,
    expected_sid: Any,
    *,
    protected: bool,
    expected_flags: int,
) -> None:
    # existing ACL parsing remains unchanged
    if (
        header.AceType != self._ACCESS_ALLOWED_ACE_TYPE
        or header.AceFlags != expected_flags
        or header.AceSize < sid_offset + 8
        or header.AceSize % self._ctypes.sizeof(_WindowsDword) != 0
    ):
        raise OSError(errno.EACCES, 'Windows ACL entry is not an exact allow ACE')
```

In both `set_private_permissions` and `verify_private_permissions`, derive object type from the already-open final handle:

```python
directory = bool(self._attributes(final) & self._FILE_ATTRIBUTE_DIRECTORY)
```

Pass `directory=directory` to `_security_descriptor`, and pass either directory inheritance flags or `0` as `expected_flags` to `_verify_private_dacl`.

- [ ] **Step 5: Run focused ACL and outbox tests to verify GREEN**

Run from `src/`:

```bash
pytest test/unitary/artisanlib/roastserver/test_outbox.py -q
pytest test/unitary/artisanlib/roastserver/test_cache.py -q
```

Expected on Linux: all portable tests pass; native tests are skipped by platform markers.

- [ ] **Step 6: Run static checks for changed files**

Run from `src/`:

```bash
ruff check artisanlib/roastserver/_filesystem.py test/unitary/artisanlib/roastserver/test_outbox.py
mypy artisanlib/roastserver/_filesystem.py
pyright artisanlib/roastserver/_filesystem.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit the implementation**

```bash
git add artisanlib/roastserver/_filesystem.py \
  test/unitary/artisanlib/roastserver/test_outbox.py
git commit -m "fix(roastserver): accept normalized Windows file ACLs"
```

### Task 2: Verify and deliver the Windows build

**Files:**
- Modify only if verification identifies a defect: files from Task 1.

**Interfaces:**
- Consumes: the object-type-specific ACL implementation from Task 1.
- Produces: a pushed PR #2 revision and an unsigned Windows installer for physical validation.

- [ ] **Step 1: Run the focused connector regression suite**

Run from `src/`:

```bash
pytest test/unitary/artisanlib/roastserver \
  test/unitary/artisanlib/test_main_roastserver.py \
  test/unitary/artisanlib/test_canvas_roastserver.py -q
```

Expected: all Linux-applicable tests pass and native Windows tests skip.

- [ ] **Step 2: Run branch hygiene checks**

From the worktree root:

```bash
git diff --check master...HEAD
git status --short
git log --oneline master..HEAD
```

Expected: no whitespace errors; only intentional committed changes; clean status.

- [ ] **Step 3: Push PR #2**

```bash
git push fork ci/appveyor-unsigned-pr
```

Expected: `fork/ci/appveyor-unsigned-pr` advances to the implementation commit and AppVeyor starts an unsigned PR build.

- [ ] **Step 4: Verify AppVeyor packaging**

```bash
gh pr checks 2 --repo fr3akX/artisan --watch
```

Expected: AppVeyor succeeds for Windows, macOS, and Linux. If the aggregate check remains pending after all jobs finish, verify individual jobs through the public AppVeyor API before reporting.

- [ ] **Step 5: Physically validate on Windows**

Install the PR #2 Windows artifact, start Artisan, and verify:

```text
%LOCALAPPDATA%\artisan-scope\Artisan\roastserver\outbox\outbox.sqlite3 exists
%LOCALAPPDATA%\artisan-scope\Artisan\roastserver\cache exists
```

Then confirm the persisted credential causes `/api/v1/auth/me` on restart and the Roast Server dialog reports the saved identity rather than `Not connected`.
