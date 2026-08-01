RED
- Added `src/test/unitary/artisanlib/roastserver/test_settings.py` first.
- Ran `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py -v`.
- Result: collection failed with `ModuleNotFoundError: No module named 'artisanlib.roastserver.settings'`.

GREEN
- Implemented `src/artisanlib/roastserver/settings.py` with canonical origin validation, immutable connector settings, QSettings persistence, namespace/account hashing, and fixed-message keyring wrapping.
- Added focused settings/keyring tests with temporary INI `QSettings` and a fake backend.
- Automatic upload stays opt-in only for a confirmed identity on the current origin; origin changes clear identity and disable upload.

TESTS
- `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py -v`
- `cd src && .venv/bin/ruff check artisanlib/roastserver/settings.py test/unitary/artisanlib/roastserver/test_settings.py`
- `cd src && .venv/bin/mypy artisanlib/roastserver/settings.py test/unitary/artisanlib/roastserver/test_settings.py`
- `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver -v`
- `cd src && .venv/bin/python -c "from artisanlib.roastserver.settings import ConnectorSettings, SettingsStore, SystemCredentialStore"`

FILES
- `src/artisanlib/roastserver/settings.py`
- `src/test/unitary/artisanlib/roastserver/test_settings.py`
- `.superpowers/sdd/2026-08-01-artisan-archive-connector/task-2-report.md`

SELF-REVIEW
- Stored data is confined to `RoastServer/...` QSettings keys and never includes credentials.
- Keyring failures always raise the fixed public message and do not expose backend text or the secret.
- Canonicalization enforces HTTPS except exact loopback HTTP, lowercases DNS names, preserves bracketed IPv6, removes default ports, and rejects paths/query/fragment/userinfo.
- Cache limits are bounded to 64 MiB-4 GiB and the client instance UUID is persisted once and reused.

CONCERNS
- None.

FIX-1 RED
- Extended `src/test/unitary/artisanlib/roastserver/test_settings.py` first for localhost HTTP, strict authority rejection, invalid-origin repair invalidation, malformed security booleans, malformed UUID/identity, `QByteArray` copy isolation, and keyring error redaction.
- Ran `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py -q`.
- Result: 20 failures covering localhost HTTP rejection, permissive authority parsing, stale identity/origin repair, and unsafe boolean coercion.

FIX-1 GREEN
- Tightened `src/artisanlib/roastserver/settings.py` to allow only canonical loopback HTTP origins, validate authority components strictly with `idna` UTS46/std3 normalization, reject noncanonical IP forms and empty explicit ports, and normalize valid DNS hosts to lower A-label form.
- Invalid or wrongly typed stored origins now repair to `DEFAULT_ORIGIN` while atomically clearing identity fields and persisting `enabled=false` and `automaticUpload=false`; malformed security booleans repair to persisted `false` and malformed identity/UUID values fail closed.
- Expanded connector settings/keyring coverage for malformed `QSettings` values, partial identity cleanup, `QByteArray` mutation isolation, localhost acceptance, adversarial authority forms, and fixed public keyring errors across get/set/delete.

FIX-1 TESTS
- `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py -q`
- `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver -q`
- `cd src && .venv/bin/ruff check artisanlib/roastserver/settings.py test/unitary/artisanlib/roastserver/test_settings.py`
- `cd src && .venv/bin/mypy artisanlib/roastserver/settings.py test/unitary/artisanlib/roastserver/test_settings.py`

FIX-1 FILES
- `src/artisanlib/roastserver/settings.py`
- `src/test/unitary/artisanlib/roastserver/test_settings.py`
- `.superpowers/sdd/2026-08-01-artisan-archive-connector/task-2-report.md`

FIX-1 CONCERNS
- `mypy` reported the existing informational note `pyproject.toml: note: unused section(s): module = ['libusb_package.*', 'uic.*']` but returned success.

FIX-2 RED
- Added parameterized regression coverage for malformed bracketed HTTPS authorities that previously let `urllib.parse.urlsplit` ValueError escape.
- Verified valid bracketed IPv6 origin handling remains intact.

FIX-2 GREEN
- Wrapped the `urlsplit` boundary in `src/artisanlib/roastserver/settings.py` so malformed bracketed authorities now raise `SettingsError('Enter a valid HTTPS origin.')` from `None`.
- Kept canonical valid IPv6 origins accepted, including `https://[::1]`.
- Extended `src/test/unitary/artisanlib/roastserver/test_settings.py` to assert the fixed public message, absent cause, and no raw-input leakage in `str()`/`repr()`.

FIX-2 TESTS
- `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver/test_settings.py`
- `cd src && .venv/bin/pytest test/unitary/artisanlib/roastserver -q`
- `cd src && .venv/bin/ruff check artisanlib/roastserver test/unitary/artisanlib/roastserver`
- `cd src && .venv/bin/mypy artisanlib/roastserver/settings.py test/unitary/artisanlib/roastserver/test_settings.py`
- `cd src && .venv/bin/mypy artisanlib/roastserver test/unitary/artisanlib/roastserver`

FIX-2 FILES
- `src/artisanlib/roastserver/settings.py`
- `src/test/unitary/artisanlib/roastserver/test_settings.py`
- `.superpowers/sdd/2026-08-01-artisan-archive-connector/task-2-report.md`

FIX-2 CONCERNS
- `mypy` reported the existing informational note `pyproject.toml: note: unused section(s): module = ['libusb_package.*', 'uic.*']` but returned success on the targeted file check.
- Full connector `mypy` still reports the pre-existing `test/unitary/artisanlib/roastserver/test_contract.py:285` `arg-type` error.
