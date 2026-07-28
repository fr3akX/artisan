# AGENTS.md

## Project overview

Artisan is a production PyQt6 desktop application for recording, analyzing, and
controlling coffee roasts. It runs on macOS, Windows, Linux, and Raspberry Pi,
and integrates with many serial, USB, Bluetooth, network, PLC, scale, and PID
devices. Changes must preserve cross-platform behavior and must not assume that
real roasting hardware is available.

The project is licensed under AGPLv3+. Follow the copyright/license header style
of nearby production modules when adding a Python file.

## Repository map

- `src/artisan.py`: small executable entry point and command-line dispatch.
- `src/artisanlib/main.py`: application bootstrap and the main window. This is a
  very large integration module; keep focused logic in smaller modules when
  practical.
- `src/artisanlib/canvas.py`: roast graph, profile state, plotting, and sampling
  orchestration.
- `src/artisanlib/`: core UI, profile, calculation, import/export, protocol, and
  hardware integration modules. Device-specific code generally has its own
  module.
- `src/artisanlib/atypes.py`: shared `TypedDict` profile and settings schemas.
  Profile compatibility changes often need updates here as well as in
  load/save/import code and tests.
- `src/plus/`: artisan.plus account, inventory, scheduling, synchronization,
  and cloud-service integration.
- `src/test/`: pytest suite, split into `unitary`, `sanity`, `smoke`, and `uat`.
  Importer fixtures and expected profile data live under `src/test/sanity/data`.
- `src/ui/`: Qt Designer source files. `src/uic/` is generated Python.
- `src/proto/`: protobuf schemas and generated Python/stub files.
- `doc/help_dialogs/Input_files/`: help source spreadsheets;
  `doc/help_dialogs/Output_html/` and `src/help/` contain derived help.
- `src/translations/`: Qt `.ts` sources and compiled `.qm` catalogs.
- `wiki/`: installation, source-running, build, translation, and release docs.
- `src/pyproject.toml`: authoritative pytest, Ruff, mypy, pyright, pylint,
  coverage, and codespell configuration.

Native roast profiles use `.alog`; importers also support numerous vendor CSV,
JSON, XLS/XLSX, ZIP, and ROP formats. Preserve backward compatibility with
existing profiles and settings.

## Environment and running

Use Python 3.12 or newer. CI currently exercises Python 3.14. Run Python tooling
from `src/` so packages and `pyproject.toml` are resolved correctly.

```bash
cd src
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-dev.txt
python3 artisan.py
```

Dependencies are intentionally pinned in `src/requirements.txt`; do not update
unrelated pins. PyQt tests require platform Qt libraries. CI runs pytest on
macOS because a bare Ubuntu runner lacks required EGL/Qt libraries.

## Validation

Prefer the narrowest relevant checks while iterating, then broaden them before
finishing. From `src/`:

```bash
pytest test/unitary/artisanlib/test_util.py
pytest test/unitary/artisanlib/test_util.py::TestClass::test_name
pytest                                  # complete configured suite
ruff check .
mypy
pyright
codespell
```

CI's pylint invocation is:

```bash
pylint --disable=C,R,E0401,E0611 --extension-pkg-allow-list=PyQt6 \
  --load-plugins=pylint.extensions.no_self_use,pylint.extensions.private_import artisanlib
pylint --disable=C,R,E0401,E0611 --extension-pkg-allow-list=PyQt6 \
  --load-plugins=pylint.extensions.no_self_use,pylint.extensions.private_import plus
```

From the repository root, `pre-commit run --all-files` applies repository-wide
YAML/XML/AST, whitespace, quote, and Python-upgrade checks. Report checks that
could not run, especially failures caused by missing Qt system libraries or
hardware.

## Coding conventions

- Match the style of the module being changed. The codebase uses single-quoted
  strings and Python 3.12 syntax. Do not perform drive-by formatting in the
  very large legacy modules.
- Add complete type annotations to new and changed functions. Mypy is configured
  to reject untyped definitions/calls, incomplete definitions, implicit
  optionals, and unqualified ignores. Use precise `# type: ignore[code]` only
  when necessary.
- Use shared structures from `artisanlib.atypes` rather than duplicating profile
  dictionaries. Treat profile keys, event arrays, units, and time indices as
  compatibility-sensitive serialized data.
- Put reusable calculations and parsing outside Qt widgets where possible. Keep
  platform- and device-specific behavior localized.
- UI-visible text should use `QApplication.translate(context, text)` with a
  stable, appropriate context. Do not casually change source translation text.
- Respect Qt thread affinity. Communicate from workers to the UI through
  signals/slots; do not update widgets directly from worker, sampling, BLE, or
  network threads. Preserve existing semaphore, timer, and disconnect cleanup.
- Never require a live device, cloud account, or network call in unit tests.
  Mock those boundaries and test byte/protocol parsing deterministically.
- Avoid broad exception handling unless it is at an existing hardware or UI
  boundary where failure must be contained; preserve useful logging.

## Tests

- Add or update focused tests under the matching `src/test/unitary/artisanlib`
  or `src/test/unitary/plus` path.
- Use `pytest.approx` for floating-point calculations and parameterization for
  protocol variants and edge cases.
- Many Qt/plus tests install module mocks before importing application modules.
  Preserve this ordering and restore `sys.modules`/global state: mock leakage
  can make the suite order-dependent. Run both the targeted test and the full
  suite after changing isolation code.
- Importer behavior is covered by source fixture plus expected JSON pairs in
  `src/test/sanity/data/`. Update snapshots only when the format change is
  intentional, and inspect compatibility-sensitive differences.
- Platform-specific tests may use `@pytest.mark.darwin`, `.linux`, or `.win32`;
  these markers are registered in `src/conftest.py`.

## Generated and derived files

Do not hand-edit generated outputs:

- Edit `src/ui/*.ui`, not `src/uic/*.py`.
- Edit help spreadsheets/sources, not generated help Python/HTML directly.
- Edit protobuf `.proto` schemas, not `*_pb2.py` or `*_pb2.pyi`; regenerate both
  with a compatible protobuf toolchain.
- Translation catalogs and compiled files are managed by Qt translation tools.

For UI, help, or translation source changes, regenerate tracked derivatives
from `src/` with `./build-derived.sh` (or `build-derived-win.bat` on Windows).
The script can rewrite many files, so review the complete diff and keep only
expected generated changes. It requires Qt tools (`pyuic6`, `pylupdate6`, and
`lrelease`).

## Change discipline

Keep changes scoped, preserve public/profile/device compatibility, and do not
modify build, packaging, release metadata, translations, or dependency versions
unless the task requires it. Never exercise control commands against real
roasting hardware as part of validation. Check `git diff` and `git status`
before finishing, including generated artifacts and test fixture changes.
