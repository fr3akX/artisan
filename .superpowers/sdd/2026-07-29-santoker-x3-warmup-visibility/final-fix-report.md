# Santoker X3 warm-up final fix report

## Status

Both findings were technically feasible and fixed in scope. No hardware, BLE,
network, cloud, or roasting equipment was used. The parser-path test is not
blocked: the production action-6 semantic/raw command parser was exercised with
a hardware-free fake device.

## Root causes

1. `Santoker.register_reading()` compared a valid 0x7F report only with
   `_reported_warmup_target`. A local `setWarmupTarget()` update changed
   `_warmup_target` but not the reported value, so a repeated authoritative
   machine report could be de-duplicated incorrectly.
2. The existing CHARGE-ordering test called the OFF operation and recorded the
   raw command manually instead of executing the production command parser.

## RED evidence

Initial bare-system attempt (before dependency setup):

```text
cd src && pytest -q test/unitary/artisanlib/test_santoker.py::TestSantokerWarmupProtocol::test_repeated_target_report_reconciles_local_target_update test/unitary/artisanlib/test_santoker_warmup.py::test_parser_executes_warmup_off_before_raw_santoker_command
ERROR: ModuleNotFoundError: No module named 'numpy'
```

After installing the pinned requirements into `/tmp/artisan-santoker-venv`
(outside the repository), the corrected RED command was:

```text
cd src && QT_QPA_PLATFORM=offscreen /tmp/artisan-santoker-venv/bin/pytest -q test/unitary/artisanlib/test_santoker.py::TestSantokerWarmupProtocol::test_repeated_target_report_reconciles_local_target_update test/unitary/artisanlib/test_santoker_warmup.py::test_parser_executes_warmup_off_before_raw_santoker_command
```

Relevant result before the production fix:

```text
collected 2 items
...test_repeated_target_report_reconciles_local_target_update F
...test_parser_executes_warmup_off_before_raw_santoker_command .
========================= 1 failed, 1 passed in 1.04s =========================
```

The target regression failed because the callback list was `[call(190.0)]`
where `[call(190.0), call(190.0)]` was required. The parser test passed once
its test-only window adapter delegated `setSantokerWarmup()` to the production
method; the earlier setup error was not treated as product evidence.

## GREEN evidence

```text
cd src && QT_QPA_PLATFORM=offscreen /tmp/artisan-santoker-venv/bin/pytest -q test/unitary/artisanlib/test_santoker.py::TestSantokerWarmupProtocol::test_repeated_target_report_reconciles_local_target_update test/unitary/artisanlib/test_santoker_warmup.py::test_parser_executes_warmup_off_before_raw_santoker_command
```

```text
============================== 2 passed in 0.96s ===============================
```

Required focused Santoker tests, both import orders:

```text
cd src && QT_QPA_PLATFORM=offscreen /tmp/artisan-santoker-venv/bin/pytest -q test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py
# 92 passed in 2.38s

cd src && QT_QPA_PLATFORM=offscreen /tmp/artisan-santoker-venv/bin/pytest -q test/unitary/artisanlib/test_santoker_warmup.py test/unitary/artisanlib/test_santoker.py
# 92 passed in 2.52s
```

Required static checks:

```text
cd src && /tmp/artisan-santoker-venv/bin/ruff check artisanlib/santoker.py test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py
# exit 0; no output

cd src && /tmp/artisan-santoker-venv/bin/mypy artisanlib/santoker.py test/unitary/artisanlib/test_santoker.py test/unitary/artisanlib/test_santoker_warmup.py
# Success: no issues found in 3 source files
```

Additional `git diff --check` and Python compile checks passed.

## Files changed

- `src/artisanlib/santoker.py`: notify when a valid report differs from either
  the prior report or the current optimistic target, before state overwrite.
- `src/test/unitary/artisanlib/test_santoker.py`: add the local-divergence,
  authoritative-report, and repeated-report de-duplication regression.
- `src/test/unitary/artisanlib/test_santoker_warmup.py`: add a parser-boundary
  integration test using the exact `santokerWarmup(0);santoker(80,1)` command and
  a hardware-free fake device.

## Self-review

- Callback duplication: unchanged reports with no local divergence remain
  de-duplicated; the new callback is emitted only when the report differs from
  the previous report or optimistic protocol target.
- State mutation order: `changed` is computed before either target field is
  overwritten; then both protocol fields are reconciled before the callback.
- Parser realism: the test enters `eventaction_internal()` action 6, executes
  both semicolon-separated production parser branches, delegates semantic OFF
  through `ApplicationWindow.setSantokerWarmup()`, and routes raw 0x80 through
  `ApplicationWindow.santokerSendMessage()`.
- Scope: one production condition and focused tests only; no preset contract,
  control lifecycle, signal boundary, or non-X3 behavior was changed.

## Commit and concerns

The report is included in the single final commit created after this report was
written; the commit hash is returned by the dispatch response. No parser-path
blocker remains. The only environment concern was the initially bare system
Python; verification used the pinned dependencies in an external temporary
virtual environment, leaving repository files and dependency pins unchanged.
