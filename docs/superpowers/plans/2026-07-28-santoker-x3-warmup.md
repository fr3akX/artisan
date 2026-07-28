# Santoker X3 Master Warm-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, first-class Santoker X3 Master warm-up control with a unit-aware target slider, stateful toggle, incoming-state reconciliation, and a dedicated Bluetooth preset.

**Architecture:** Extend `Santoker` with named warm-up protocol state and validated wire commands. Put unit conversion and roast-state decisions in a small, Qt-independent controller, then connect it to Artisan's IO command dispatcher and UI through existing Qt signal/slot patterns. Keep the raw `santoker()` command and all existing presets unchanged.

**Tech Stack:** Python 3.12+, PyQt6, asyncio, pymodbus RTU CRC, pytest, Ruff, mypy, pyright, Qt INI machine presets.

## Global Constraints

- Do not connect to or issue commands against real roasting hardware during development or tests.
- Preserve the existing `santoker(<target>,<value>)` command semantics exactly.
- Do not modify the existing combined Q + X or R-series Santoker presets.
- The protocol always receives Celsius × 10; UI input follows Artisan's °C/°F temperature-slider mode.
- Warm-up ON is allowed only after a complete accepted Santoker packet and before CHARGE.
- Warm-up ON sends target `0x7F` before state `0x7E = 1`; it never sends Machine ON `0x7A`.
- Use Qt signals/slots for all UI updates originating from BLE, serial, or network callbacks.
- Match project style: single-quoted strings, complete type annotations, and precise exception handling.
- Do not hand-edit generated translations or other derived files; use `src/build-derived.sh` and review its full diff.
- Retain the documentation caveat that findings are based on official-app static analysis without physical X3 verification.

---

## File Structure

### Create

- `src/artisanlib/santoker_warmup.py` — Qt-independent unit conversion, command validation, and roast-state decisions.
- `src/test/unitary/artisanlib/test_santoker_warmup.py` — controller, main-window adapter, and preset contract tests.
- `src/includes/Machines/Santoker/X3_Master_Bluetooth.aset` — dedicated X3 Master Bluetooth controls.

### Modify

- `src/artisanlib/santoker.py` — named targets, state, callbacks, semantic sends, validated-header readiness.
- `src/artisanlib/main.py` — semantic IO commands, controller ownership, messages, and control synchronization.
- `src/artisanlib/canvas.py` — wire Santoker communication callbacks to Qt signals.
- `src/test/unitary/artisanlib/test_santoker.py` — active protocol and frame tests.
- `SANTOKER_X3_PREHEAT_PROTOCOL.md` — mark implemented coverage and retain unresolved gaps.
- `src/translations/*.ts` and other translation derivatives only if changed by `build-derived.sh` for the new messages.

### Interfaces shared between tasks

`artisanlib.santoker.Santoker` will produce:

```python
WARMUP: Final[bytes]
WARMUP_TEMP: Final[bytes]
MIN_WARMUP_TEMP_C: Final[float]
MAX_WARMUP_TEMP_C: Final[float]
DEFAULT_WARMUP_TEMP_C: Final[float]

def isHeaderReady(self) -> bool: ...
def getWarmup(self) -> bool | None: ...
def getWarmupTarget(self) -> float: ...
def setWarmupTarget(self, temp_c: float) -> bool: ...
def setWarmup(self, enabled: bool) -> bool: ...
def resetProtocolState(self) -> None: ...
```

`artisanlib.santoker_warmup` will produce:

```python
class WarmupResult(Enum):
    OK = 'ok'
    NO_CONNECTION = 'no_connection'
    NOT_READY = 'not_ready'
    AFTER_CHARGE = 'after_charge'
    OUT_OF_RANGE = 'out_of_range'

class SantokerWarmupController:
    desired_temp_c: float

    def set_target(
        self,
        display_temp: float,
        unit: Literal['C', 'F'],
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult: ...

    def set_enabled(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult: ...

    def reconcile_reported_state(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> bool: ...

    def accept_reported_target(self, temp_c: float) -> None: ...
    def target_for_display(self, unit: Literal['C', 'F']) -> float: ...

def find_warmup_slider(commands: Sequence[str]) -> int | None: ...
def find_warmup_buttons(commands: Sequence[str]) -> list[int]: ...
```

---

### Task 1: Add semantic warm-up protocol commands

**Files:**
- Modify: `src/artisanlib/santoker.py:104-370`
- Test: `src/test/unitary/artisanlib/test_santoker.py`

**Interfaces:**
- Consumes: existing `create_msg(target: bytes, value: int)` and `send_msg(target: bytes, value: int)`.
- Produces: warm-up constants, constructor callbacks, getters, `setWarmupTarget()`, and `setWarmup()` listed in the shared interfaces.

- [ ] **Step 1: Replace the active source-inspection-only warm-up coverage with failing behavioral tests**

Add tests that instantiate `Santoker` without starting a transport and patch `send_msg` at class level because `Santoker` uses `__slots__`:

```python
from unittest.mock import Mock, call, patch


def test_warmup_packets_use_expected_targets_and_crc() -> None:
    from artisanlib.santoker import Santoker

    santoker = Santoker()

    assert santoker.create_msg(b'\x7f', 1900) == bytes.fromhex(
        'eea57f02040300076cf260fffcffff'
    )
    assert santoker.create_msg(b'\x7e', 1) == bytes.fromhex(
        'eea57e02040300000131bdfffcffff'
    )
    assert santoker.create_msg(b'\x7e', 0) == bytes.fromhex(
        'eea57e020403000000f07dfffcffff'
    )


def test_start_warmup_sends_target_before_on() -> None:
    from artisanlib.santoker import Santoker

    state_handler = Mock()
    santoker = Santoker(warmup_handler=state_handler)
    santoker._header_ready = True  # protocol readiness is covered via frames in Task 2

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert santoker.setWarmupTarget(190.0)
        assert santoker.setWarmup(True)

    assert send_msg.call_args_list == [
        call(Santoker.WARMUP_TEMP, 1900),
        call(Santoker.WARMUP, 1),
    ]
    assert santoker.getWarmup() is True
    state_handler.assert_called_once_with(True)


def test_target_change_is_cached_while_inactive() -> None:
    from artisanlib.santoker import Santoker

    santoker = Santoker()
    santoker._header_ready = True

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert santoker.setWarmupTarget(205.5)

    send_msg.assert_not_called()
    assert santoker.getWarmupTarget() == 205.5


def test_target_change_is_sent_while_active() -> None:
    from artisanlib.santoker import Santoker

    santoker = Santoker()
    santoker._header_ready = True
    santoker._warmup = True

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert santoker.setWarmupTarget(205.5)

    send_msg.assert_called_once_with(Santoker.WARMUP_TEMP, 2055)


@pytest.mark.parametrize('temp_c', [99.9, 300.1])
def test_warmup_target_rejects_out_of_range_values(temp_c: float) -> None:
    from artisanlib.santoker import Santoker

    santoker = Santoker()
    santoker._header_ready = True

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert not santoker.setWarmupTarget(temp_c)

    send_msg.assert_not_called()
    assert santoker.getWarmupTarget() == Santoker.DEFAULT_WARMUP_TEMP_C


def test_start_warmup_rejects_unready_header() -> None:
    from artisanlib.santoker import Santoker

    santoker = Santoker()

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert not santoker.setWarmup(True)

    send_msg.assert_not_called()
    assert santoker.getWarmup() is None


def test_stop_warmup_sends_only_off() -> None:
    from artisanlib.santoker import Santoker

    santoker = Santoker()
    santoker._header_ready = True
    santoker._warmup = True

    with patch.object(Santoker, 'send_msg') as send_msg:
        assert santoker.setWarmup(False)

    send_msg.assert_called_once_with(Santoker.WARMUP, 0)
    assert santoker.getWarmup() is False
```

Keep existing import-isolation fixtures. Remove or replace only assertions made obsolete by the new constructor signature or constants.

- [ ] **Step 2: Run the new protocol tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py -k 'warmup or target_change or start_warmup or stop_warmup' -v
```

Expected: failures because warm-up constants, state, callback arguments, and methods do not exist.

- [ ] **Step 3: Add constants, constructor state, slots, and semantic methods**

In `Santoker`, add typed constants and extend `__slots__`:

```python
WARMUP: Final[bytes] = b'\x7E'
WARMUP_TEMP: Final[bytes] = b'\x7F'
MIN_WARMUP_TEMP_C: Final[float] = 100.0
MAX_WARMUP_TEMP_C: Final[float] = 300.0
DEFAULT_WARMUP_TEMP_C: Final[float] = 190.0
```

Extend the constructor after `drop_handler` without changing existing positional behavior:

```python
warmup_handler: Callable[[bool | None], None] | None = None,
warmup_temp_handler: Callable[[float], None] | None = None,
warmup_target: float = DEFAULT_WARMUP_TEMP_C,
```

Initialize:

```python
self._header_ready: bool = False
self._warmup: bool | None = None
self._warmup_target: float = self.DEFAULT_WARMUP_TEMP_C
self._reported_warmup_target: float | None = None
self._warmup_handler = warmup_handler
self._warmup_temp_handler = warmup_temp_handler
if self.MIN_WARMUP_TEMP_C <= warmup_target <= self.MAX_WARMUP_TEMP_C:
    self._warmup_target = warmup_target
```

Add methods following the module's existing getter naming:

```python
def isHeaderReady(self) -> bool:
    return self._header_ready


def getWarmup(self) -> bool | None:
    return self._warmup


def getWarmupTarget(self) -> float:
    return self._warmup_target


def _setWarmupState(self, enabled: bool | None) -> None:
    if enabled != self._warmup:
        self._warmup = enabled
        if self._warmup_handler is not None:
            try:
                self._warmup_handler(enabled)
            except Exception as e:  # pylint: disable=broad-except
                _log.exception(e)


def setWarmupTarget(self, temp_c: float) -> bool:
    if not self.MIN_WARMUP_TEMP_C <= temp_c <= self.MAX_WARMUP_TEMP_C:
        return False
    self._warmup_target = temp_c
    if self._warmup is True:
        if not self._header_ready:
            return False
        self.send_msg(self.WARMUP_TEMP, int(round(temp_c * 10)))
    return True


def setWarmup(self, enabled: bool) -> bool:
    if not self._header_ready:
        return False
    if enabled:
        self.send_msg(self.WARMUP_TEMP, int(round(self._warmup_target * 10)))
        self.send_msg(self.WARMUP, 1)
    else:
        self.send_msg(self.WARMUP, 0)
    self._setWarmupState(enabled)
    return True
```

Do not add model-power, machine-on, heating, cooling, or blend behavior.

- [ ] **Step 4: Run the focused protocol tests and verify GREEN**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py -k 'warmup or target_change or start_warmup or stop_warmup' -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Run static checks on the protocol module**

Run:

```bash
cd src
ruff check artisanlib/santoker.py test/unitary/artisanlib/test_santoker.py
mypy artisanlib/santoker.py
```

Expected: both commands pass. If project mypy does not accept a file argument under its configuration, run `mypy` and record unrelated failures separately.

- [ ] **Step 6: Commit semantic protocol support**

```bash
git add src/artisanlib/santoker.py src/test/unitary/artisanlib/test_santoker.py
git commit -m "Add Santoker warm-up protocol commands"
```

---

### Task 2: Decode warm-up reports and establish validated header readiness

**Files:**
- Modify: `src/artisanlib/santoker.py:213-357`
- Test: `src/test/unitary/artisanlib/test_santoker.py`

**Interfaces:**
- Consumes: `WARMUP`, `WARMUP_TEMP`, `_setWarmupState()`, and constructor callbacks from Task 1.
- Produces: readiness only after complete accepted frames, report reconciliation, and `resetProtocolState()`.

- [ ] **Step 1: Write failing report and frame-readiness tests**

Add a stream helper and active tests:

```python
async def read_packet(santoker: object, packet: bytes) -> None:
    import asyncio

    stream = asyncio.StreamReader()
    stream.feed_data(packet)
    stream.feed_eof()
    await santoker.read_msg(stream)  # type: ignore[attr-defined]


def test_warmup_reports_update_state_and_target() -> None:
    from artisanlib.santoker import Santoker

    state_handler = Mock()
    target_handler = Mock()
    santoker = Santoker(
        warmup_handler=state_handler,
        warmup_temp_handler=target_handler,
    )

    santoker.register_reading(Santoker.WARMUP, b'\x00\x00\x01')
    santoker.register_reading(Santoker.WARMUP, b'\x00\x00\x01')
    santoker.register_reading(Santoker.WARMUP_TEMP, (1900).to_bytes(3, 'big'))
    santoker.register_reading(Santoker.WARMUP_TEMP, (1900).to_bytes(3, 'big'))

    assert santoker.getWarmup() is True
    assert santoker.getWarmupTarget() == 190.0
    state_handler.assert_called_once_with(True)
    target_handler.assert_called_once_with(190.0)


@pytest.mark.parametrize(
    ('target', 'data'),
    [
        (b'\x7e', b'\x00\x00\x02'),
        (b'\x7f', (999).to_bytes(3, 'big')),
        (b'\x7f', (3001).to_bytes(3, 'big')),
    ],
)
def test_invalid_warmup_reports_are_ignored(target: bytes, data: bytes) -> None:
    from artisanlib.santoker import Santoker

    state_handler = Mock()
    target_handler = Mock()
    santoker = Santoker(
        warmup_handler=state_handler,
        warmup_temp_handler=target_handler,
    )

    santoker.register_reading(target, data)

    assert santoker.getWarmup() is None
    assert santoker.getWarmupTarget() == Santoker.DEFAULT_WARMUP_TEMP_C
    state_handler.assert_not_called()
    target_handler.assert_not_called()


@pytest.mark.asyncio
async def test_complete_a5_packet_sets_ble_header_ready() -> None:
    from artisanlib.santoker import Santoker

    sender = Santoker()
    receiver = Santoker(connect_using_ble=True)
    packet = sender.create_msg(Santoker.WARMUP_TEMP, 1900)

    await read_packet(receiver, packet)

    assert receiver.isHeaderReady()
    assert receiver.HEADER == Santoker.HEADER_WIFI
    assert receiver.getWarmupTarget() == 190.0


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['crc', 'tail'])
async def test_invalid_complete_packet_does_not_set_header_ready(corruption: str) -> None:
    from artisanlib.santoker import Santoker

    sender = Santoker()
    receiver = Santoker(connect_using_ble=True)
    packet = bytearray(sender.create_msg(Santoker.WARMUP_TEMP, 1900))
    if corruption == 'crc':
        packet[-5] ^= 0x01  # Santoker currently verifies the second CRC byte
    else:
        packet[-1] ^= 0x01

    await read_packet(receiver, bytes(packet))

    assert not receiver.isHeaderReady()
    assert receiver.HEADER == Santoker.HEADER_BT


@pytest.mark.asyncio
async def test_truncated_packet_does_not_set_header_ready() -> None:
    import asyncio

    from artisanlib.santoker import Santoker

    sender = Santoker()
    receiver = Santoker(connect_using_ble=True)
    packet = sender.create_msg(Santoker.WARMUP_TEMP, 1900)

    with pytest.raises(asyncio.IncompleteReadError):
        await read_packet(receiver, packet[:-3])

    assert not receiver.isHeaderReady()
    assert receiver.HEADER == Santoker.HEADER_BT


def test_protocol_reset_retains_desired_target() -> None:
    from artisanlib.santoker import Santoker

    state_handler = Mock()
    santoker = Santoker(warmup_handler=state_handler)
    santoker._header_ready = True
    with patch.object(Santoker, 'send_msg'):
        assert santoker.setWarmupTarget(195.0)
        assert santoker.setWarmup(True)

    santoker.resetProtocolState()

    assert not santoker.isHeaderReady()
    assert santoker.getWarmup() is None
    assert santoker.getWarmupTarget() == 195.0
    assert state_handler.call_args_list[-1] == call(None)
```

If `pytest-asyncio` is not enabled in this suite, replace the async markers with `asyncio.run(read_packet(...))` inside synchronous tests.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py -k 'report or header_ready or protocol_reset' -v
```

Expected: failures because readings are ignored, readiness is never set, candidate headers are adopted too early, and reset does not exist.

- [ ] **Step 3: Decode `0x7E` and `0x7F` reports**

Extend `register_reading()` after event handling:

```python
elif target == self.WARMUP:
    if value in {0, 1}:
        self._setWarmupState(bool(value))
    elif self._logging:
        _log.debug('invalid warm-up state: %s', value)
elif target == self.WARMUP_TEMP:
    temp_c = value / 10.0
    if self.MIN_WARMUP_TEMP_C <= temp_c <= self.MAX_WARMUP_TEMP_C:
        changed = temp_c != self._reported_warmup_target
        self._reported_warmup_target = temp_c
        self._warmup_target = temp_c
        if changed and self._warmup_temp_handler is not None:
            try:
                self._warmup_temp_handler(temp_c)
            except Exception as e:  # pylint: disable=broad-except
                _log.exception(e)
    elif self._logging:
        _log.debug('invalid warm-up target: %s', temp_c)
```

Place `WARMUP`/`WARMUP_TEMP` handling before generic unsupported/unknown logging.

- [ ] **Step 4: Defer header adoption until the full packet is accepted**

In `read_msg()`, store the candidate instead of immediately mutating `self.HEADER`:

```python
snd_header_byte = await stream.readexactly(1)
if snd_header_byte == self.HEADER_BT[1:2]:
    candidate_header = self.HEADER_BT
elif snd_header_byte == self.HEADER_WIFI[1:2]:
    candidate_header = self.HEADER_WIFI
else:
    return
```

After the existing code-header, data, CRC, and tail checks pass:

```python
self.HEADER = candidate_header
self._header_ready = True
self.register_reading(target, data)
```

Do not tighten the existing partial Santoker CRC policy in this feature.

- [ ] **Step 5: Reset connection-derived state and wrap disconnect handlers**

Add:

```python
def resetProtocolState(self) -> None:
    self._header_ready = False
    self._reported_warmup_target = None
    self._setWarmupState(None)
```

Ensure BLE and `AsyncComm` disconnect paths invoke `resetProtocolState()` before the external disconnect callback. Use one internal wrapper so the external callback fires once per disconnect:

```python
def on_disconnected() -> None:
    self.resetProtocolState()
    if disconnected_handler is not None:
        disconnected_handler()
```

Pass this wrapper to the superclass and BLE client instead of passing the external handler directly. Preserve existing connected callback behavior.

- [ ] **Step 6: Run protocol tests and static checks**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py -v
ruff check artisanlib/santoker.py test/unitary/artisanlib/test_santoker.py
```

Expected: all active Santoker tests pass and Ruff reports no findings.

- [ ] **Step 7: Commit report decoding and readiness**

```bash
git add src/artisanlib/santoker.py src/test/unitary/artisanlib/test_santoker.py
git commit -m "Track Santoker warm-up reports and header readiness"
```

---

### Task 3: Add a Qt-independent warm-up controller

**Files:**
- Create: `src/artisanlib/santoker_warmup.py`
- Create: `src/test/unitary/artisanlib/test_santoker_warmup.py`

**Interfaces:**
- Consumes: the `Santoker` warm-up interface from Tasks 1 and 2 through a structural `Protocol`.
- Produces: `WarmupResult`, `SantokerWarmupController`, `find_warmup_slider()`, and `find_warmup_buttons()`.

- [ ] **Step 1: Write failing controller tests with a fake device**

Create the test file with no Qt dependency for these tests:

```python
from dataclasses import dataclass, field

import pytest


@dataclass
class FakeWarmupDevice:
    ready: bool = True
    warmup: bool | None = False
    calls: list[tuple[str, object]] = field(default_factory=list)

    def isHeaderReady(self) -> bool:
        return self.ready

    def getWarmup(self) -> bool | None:
        return self.warmup

    def setWarmupTarget(self, temp_c: float) -> bool:
        self.calls.append(('target', temp_c))
        return 100.0 <= temp_c <= 300.0

    def setWarmup(self, enabled: bool) -> bool:
        self.calls.append(('enabled', enabled))
        self.warmup = enabled
        return self.ready


def test_controller_converts_fahrenheit_and_updates_device() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController()

    result = controller.set_target(374.0, 'F', device)

    assert result is WarmupResult.OK
    assert controller.desired_temp_c == pytest.approx(190.0)
    assert device.calls == [('target', pytest.approx(190.0))]
    assert controller.target_for_display('F') == pytest.approx(374.0)


@pytest.mark.parametrize(('value', 'unit'), [(99.0, 'C'), (573.0, 'F')])
def test_controller_rejects_out_of_range_target(value: float, unit: str) -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController()

    result = controller.set_target(value, unit, device)  # type: ignore[arg-type]

    assert result is WarmupResult.OUT_OF_RANGE
    assert controller.desired_temp_c == 190.0
    assert device.calls == []


def test_controller_stores_target_without_connection() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    controller = SantokerWarmupController()

    assert controller.set_target(195.0, 'C', None) is WarmupResult.OK
    assert controller.desired_temp_c == 195.0


@pytest.mark.parametrize(
    ('device', 'charge_index', 'expected'),
    [
        (None, -1, 'no_connection'),
        (FakeWarmupDevice(ready=False), -1, 'not_ready'),
        (FakeWarmupDevice(), 10, 'after_charge'),
    ],
)
def test_controller_rejects_unsafe_start(
    device: FakeWarmupDevice | None,
    charge_index: int,
    expected: str,
) -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    controller = SantokerWarmupController()

    assert controller.set_enabled(True, charge_index, device).value == expected
    assert device is None or device.calls == []


def test_controller_starts_with_desired_target() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice()
    controller = SantokerWarmupController(desired_temp_c=190.0)

    result = controller.set_enabled(True, -1, device)

    assert result is WarmupResult.OK
    assert device.calls == [('target', 190.0), ('enabled', True)]


def test_controller_does_not_send_redundant_off_while_inactive() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController, WarmupResult

    device = FakeWarmupDevice(warmup=False)
    controller = SantokerWarmupController()

    assert controller.set_enabled(False, 10, device) is WarmupResult.OK
    assert device.calls == []


def test_post_charge_on_report_is_forced_off() -> None:
    from artisanlib.santoker_warmup import SantokerWarmupController

    device = FakeWarmupDevice(warmup=True)
    controller = SantokerWarmupController()

    assert controller.reconcile_reported_state(True, 10, device)
    assert device.calls == [('enabled', False)]


def test_semantic_control_lookup() -> None:
    from artisanlib.santoker_warmup import find_warmup_buttons, find_warmup_slider

    assert find_warmup_slider([
        'santoker(ca,{})',
        '',
        'santokerWarmupTemp({})',
        'santoker(fa,{})',
    ]) == 2
    assert find_warmup_buttons([
        'santoker(fa,10)',
        'santokerWarmup(1 - $)',
        'santokerWarmup(0)',
    ]) == [1, 2]
```

- [ ] **Step 2: Run the controller tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -v
```

Expected: import failure because `artisanlib.santoker_warmup` does not exist.

- [ ] **Step 3: Implement the typed controller module**

Use the standard Artisan AGPL header from nearby production modules. Implement:

```python
from dataclasses import dataclass
from enum import Enum
from collections.abc import Sequence
from typing import Literal, Protocol

from artisanlib.util import fromCtoFstrict, fromFtoCstrict


MIN_WARMUP_TEMP_C = 100.0
MAX_WARMUP_TEMP_C = 300.0
DEFAULT_WARMUP_TEMP_C = 190.0


class WarmupResult(Enum):
    OK = 'ok'
    NO_CONNECTION = 'no_connection'
    NOT_READY = 'not_ready'
    AFTER_CHARGE = 'after_charge'
    OUT_OF_RANGE = 'out_of_range'


class SantokerWarmupDevice(Protocol):
    def isHeaderReady(self) -> bool: ...
    def getWarmup(self) -> bool | None: ...
    def setWarmupTarget(self, temp_c: float) -> bool: ...
    def setWarmup(self, enabled: bool) -> bool: ...


@dataclass
class SantokerWarmupController:
    desired_temp_c: float = DEFAULT_WARMUP_TEMP_C

    def set_target(
        self,
        display_temp: float,
        unit: Literal['C', 'F'],
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult:
        temp_c = fromFtoCstrict(display_temp) if unit == 'F' else display_temp
        if not MIN_WARMUP_TEMP_C <= temp_c <= MAX_WARMUP_TEMP_C:
            return WarmupResult.OUT_OF_RANGE
        self.desired_temp_c = temp_c
        if device is not None and not device.setWarmupTarget(temp_c):
            return WarmupResult.OUT_OF_RANGE
        return WarmupResult.OK

    def set_enabled(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> WarmupResult:
        if device is None:
            return WarmupResult.NO_CONNECTION
        if not device.isHeaderReady():
            return WarmupResult.NOT_READY
        if enabled and charge_index > -1:
            return WarmupResult.AFTER_CHARGE
        if not enabled and device.getWarmup() is not True:
            return WarmupResult.OK
        if enabled and not device.setWarmupTarget(self.desired_temp_c):
            return WarmupResult.OUT_OF_RANGE
        if not device.setWarmup(enabled):
            return WarmupResult.NOT_READY
        return WarmupResult.OK

    def reconcile_reported_state(
        self,
        enabled: bool,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> bool:
        unsafe = enabled and charge_index > -1
        if unsafe and device is not None and device.isHeaderReady():
            device.setWarmup(False)
        return unsafe

    def accept_reported_target(self, temp_c: float) -> None:
        if MIN_WARMUP_TEMP_C <= temp_c <= MAX_WARMUP_TEMP_C:
            self.desired_temp_c = temp_c

    def target_for_display(self, unit: Literal['C', 'F']) -> float:
        return fromCtoFstrict(self.desired_temp_c) if unit == 'F' else self.desired_temp_c


def find_warmup_slider(commands: Sequence[str]) -> int | None:
    return next(
        (i for i, command in enumerate(commands) if command.strip().startswith('santokerWarmupTemp(')),
        None,
    )


def find_warmup_buttons(commands: Sequence[str]) -> list[int]:
    return [
        i
        for i, command in enumerate(commands)
        if command.strip().startswith('santokerWarmup(')
    ]
```

If importing `artisanlib.util` pulls Qt into the test unexpectedly, replace only the two conversions with typed local formulas and test them with `pytest.approx`; do not import `main.py` into this controller.

- [ ] **Step 4: Run controller tests and type checks**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -v
ruff check artisanlib/santoker_warmup.py test/unitary/artisanlib/test_santoker_warmup.py
mypy artisanlib/santoker_warmup.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit the controller**

```bash
git add src/artisanlib/santoker_warmup.py src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Add Santoker warm-up application controller"
```

---

### Task 4: Integrate semantic commands and state with the Artisan UI

**Files:**
- Modify: `src/artisanlib/main.py:1412-1488,1832-1842,4270-4305,8398-8840,9400-9610,17980-18000`
- Modify: `src/artisanlib/canvas.py:13212-13238`
- Test: `src/test/unitary/artisanlib/test_santoker_warmup.py`
- Test: `src/test/smoke/artisanlib/test_main_smoke.py`

**Interfaces:**
- Consumes: `SantokerWarmupController`, `WarmupResult`, lookup helpers, and Task 1/2 protocol callbacks.
- Produces: `santokerWarmupTemp()` and `santokerWarmup()` IO commands, UI synchronization slots, and threaded callback wiring.

- [ ] **Step 1: Add failing adapter tests for button and slider synchronization**

Append tests that call small `ApplicationWindow` methods unbound on lightweight fakes. Import `ApplicationWindow` using the same Qt/module isolation pattern as `test_main_smoke.py`:

```python
from types import SimpleNamespace
from unittest.mock import Mock, call


def test_window_updates_only_semantic_warmup_buttons() -> None:
    from artisanlib.main import ApplicationWindow

    style_signal = Mock()
    window = SimpleNamespace(
        extraeventsactionstrings=['santoker(fa,10)', 'santokerWarmup(1 - $)'],
        buttonStates=[0, 0],
        setExtraEventButtonStyleSignal=style_signal,
    )

    ApplicationWindow.setSantokerWarmupButtonState(window, True)

    assert window.buttonStates == [0, 1]
    style_signal.emit.assert_called_once_with(1, 'pressed')


def test_window_moves_warmup_slider_without_firing_action() -> None:
    from artisanlib.main import ApplicationWindow
    from artisanlib.santoker_warmup import SantokerWarmupController

    slider = Mock()
    slider.blockSignals = Mock()
    window = SimpleNamespace(
        eventslidercommands=['', '', 'santokerWarmupTemp({})', ''],
        eventslidermin=[0, 0, 100, 0],
        eventslidermax=[100, 100, 300, 100],
        qmc=SimpleNamespace(mode_tempsliders='C'),
        santokerWarmupController=SantokerWarmupController(),
        slider3=slider,
        moveslider=Mock(),
    )

    ApplicationWindow.santokerWarmupTargetChanged(window, 190.0)

    assert window.santokerWarmupController.desired_temp_c == 190.0
    assert slider.blockSignals.call_args_list == [call(True), call(False)]
    window.moveslider.assert_called_once_with(2, 190.0, forceLCDupdate=True)
```

If a PyQt-decorated method rejects the `SimpleNamespace`, keep the state calculation in a typed module-level helper in `main.py` and let the real slot call it; test the helper without constructing `QMainWindow`.

- [ ] **Step 2: Run adapter tests and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -k 'window_' -v
```

Expected: failures because the signals and synchronization methods do not exist.

- [ ] **Step 3: Add controller ownership and Qt signals in `ApplicationWindow`**

Import the controller normally because it is Qt-independent:

```python
from artisanlib.santoker_warmup import (
    SantokerWarmupController,
    WarmupResult,
    find_warmup_buttons,
    find_warmup_slider,
)
```

Add class signals:

```python
santokerWarmupStateSignal = pyqtSignal(object)
santokerWarmupTargetSignal = pyqtSignal(float)
```

Add `santokerWarmupController` to `__slots__` and initialize beside existing Santoker settings:

```python
self.santokerWarmupController = SantokerWarmupController()
```

Connect signals beside existing UI signal connections:

```python
self.santokerWarmupStateSignal.connect(self.santokerWarmupStateChanged)
self.santokerWarmupTargetSignal.connect(self.santokerWarmupTargetChanged)
```

- [ ] **Step 4: Add semantic control synchronization methods**

Implement methods with no protocol access in the widget update paths:

```python
def setSantokerWarmupButtonState(self, enabled: bool) -> None:
    for button in find_warmup_buttons(self.extraeventsactionstrings):
        if button < len(self.buttonStates):
            self.buttonStates[button] = int(enabled)
            self.setExtraEventButtonStyleSignal.emit(
                button,
                'pressed' if enabled else 'normal',
            )


@pyqtSlot(float)
def santokerWarmupTargetChanged(self, temp_c: float) -> None:
    self.santokerWarmupController.accept_reported_target(temp_c)
    slider = find_warmup_slider(self.eventslidercommands)
    if slider is None:
        return
    unit = 'F' if self.qmc.mode_tempsliders == 'F' else 'C'
    value = self.santokerWarmupController.target_for_display(unit)
    widgets = [self.slider1, self.slider2, self.slider3, self.slider4]
    widgets[slider].blockSignals(True)
    try:
        self.moveslider(slider, value, forceLCDupdate=True)
    finally:
        widgets[slider].blockSignals(False)


@pyqtSlot(object)
def santokerWarmupStateChanged(self, state: object) -> None:
    if state is None:
        self.setSantokerWarmupButtonState(False)
        return
    if not isinstance(state, bool):
        return
    unsafe = self.santokerWarmupController.reconcile_reported_state(
        state,
        self.qmc.timeindex[0],
        self.santoker,
    )
    self.setSantokerWarmupButtonState(False if unsafe else state)
    if unsafe:
        self.sendmessage(QApplication.translate(
            'Message',
            'Santoker warm-up reported ON after CHARGE; sending OFF',
        ))
```

Inside the existing signal-blocked `try` block in `updateSliderMinMax()`, initialize a semantic warm-up slider only when its runtime value is outside the configured range:

```python
warmup_slider = find_warmup_slider(self.eventslidercommands)
if (
    warmup_slider is not None
    and not (
        self.eventslidermin[warmup_slider]
        <= self.eventslidervalues[warmup_slider]
        <= self.eventslidermax[warmup_slider]
    )
):
    unit = 'F' if self.qmc.mode_tempsliders == 'F' else 'C'
    self.moveslider(
        warmup_slider,
        self.santokerWarmupController.target_for_display(unit),
        forceLCDupdate=True,
    )
```

The method already blocks all four slider signals before this `try`, so this initializes the new preset to 190°C/374°F without transmitting and without affecting existing presets.

- [ ] **Step 5: Add semantic command methods and result messages**

Add:

```python
def reportSantokerWarmupResult(self, result: WarmupResult) -> None:
    messages = {
        WarmupResult.NO_CONNECTION: QApplication.translate(
            'Message', 'Santoker roaster is not connected'
        ),
        WarmupResult.NOT_READY: QApplication.translate(
            'Message', 'Waiting for Santoker roaster data'
        ),
        WarmupResult.AFTER_CHARGE: QApplication.translate(
            'Message', 'Santoker warm-up is only available before CHARGE'
        ),
        WarmupResult.OUT_OF_RANGE: QApplication.translate(
            'Message', 'Santoker warm-up target must be between 100 and 300°C'
        ),
    }
    if result in messages:
        self.sendmessage(messages[result])


def setSantokerWarmupTarget(self, display_temp: float) -> bool:
    unit = 'F' if self.qmc.mode_tempsliders == 'F' else 'C'
    result = self.santokerWarmupController.set_target(
        display_temp,
        unit,
        self.santoker,
    )
    self.reportSantokerWarmupResult(result)
    return result is WarmupResult.OK


def setSantokerWarmup(self, enabled: bool) -> bool:
    result = self.santokerWarmupController.set_enabled(
        enabled,
        self.qmc.timeindex[0],
        self.santoker,
    )
    self.reportSantokerWarmupResult(result)
    accepted = result is WarmupResult.OK
    QTimer.singleShot(
        0,
        lambda: self.setSantokerWarmupButtonState(enabled if accepted else False),
    )
    return accepted
```

The zero-delay update runs after the custom button's default toggle logic, so rejected commands reliably restore the inactive style.

- [ ] **Step 6: Dispatch semantic IO commands before raw `santoker()`**

In the IO command loop, place the longer temperature prefix first:

```python
elif c.startswith('santokerWarmupTemp'):
    args = c[len('santokerWarmupTemp'):]
    if args.startswith('(') and args.endswith(')'):
        value = float(eval(args[1:-1][:eval_limit]))  # pylint: disable=eval-used
        self.setSantokerWarmupTarget(value)

elif c.startswith('santokerWarmup'):
    args = c[len('santokerWarmup'):]
    if args.startswith('(') and args.endswith(')'):
        enabled = bool(eval(args[1:-1][:eval_limit]))  # pylint: disable=eval-used
        self.setSantokerWarmup(enabled)

elif c.startswith('santoker'):
    # retain the existing raw command body unchanged
```

Update the nearby IO command help comments with the two exact signatures and behavior.

- [ ] **Step 7: Wire protocol callbacks through Canvas signals**

When constructing `Santoker` in `canvas.py`, add keyword arguments:

```python
warmup_handler=lambda state: self.aw.santokerWarmupStateSignal.emit(state),
warmup_temp_handler=lambda temp_c: self.aw.santokerWarmupTargetSignal.emit(temp_c),
warmup_target=self.aw.santokerWarmupController.desired_temp_c,
```

Immediately after construction, initialize the semantic slider without writing to the roaster:

```python
self.aw.santokerWarmupTargetSignal.emit(self.aw.santoker.getWarmupTarget())
```

Keep all existing event handlers and connection messages intact. Ensure Task 2's internal disconnect wrapper emits `None` through `warmup_handler`, which resets the button via the queued signal.

- [ ] **Step 8: Run adapter, protocol, and smoke tests**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py -v
pytest test/unitary/artisanlib/test_santoker.py -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
ruff check artisanlib/main.py artisanlib/canvas.py artisanlib/santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_warmup.py
```

Expected: all selected tests pass. If smoke tests cannot start because Qt system libraries are missing, record the exact import/runtime error; do not weaken the tests.

- [ ] **Step 9: Commit the application integration**

```bash
git add src/artisanlib/main.py src/artisanlib/canvas.py \
  src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Integrate Santoker X3 warm-up controls"
```

---

### Task 5: Add the dedicated X3 Master Bluetooth preset

**Files:**
- Create: `src/includes/Machines/Santoker/X3_Master_Bluetooth.aset`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py`

**Interfaces:**
- Consumes: semantic command names from Task 4.
- Produces: a user-selectable X3 Bluetooth setup with airflow, warm-up target, fire, stateful warm-up, and existing roast events.

- [ ] **Step 1: Write a failing preset contract test**

Add:

```python
from configparser import ConfigParser
from pathlib import Path


def test_x3_master_bluetooth_preset_contract() -> None:
    from artisanlib.santoker import Santoker

    src_dir = Path(__file__).parents[3]
    preset = src_dir / 'includes' / 'Machines' / 'Santoker' / 'X3_Master_Bluetooth.aset'
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(preset, encoding='utf-8') == [str(preset)]

    assert config.get('General', 'roastertype_setup') == 'Santoker X3 Master BT'
    assert config.getint('Device', 'id') == 134
    assert config.getboolean('Device', 'santokerBLE')

    slider_commands = config.get('Sliders', 'slidercommands')
    assert 'santoker(ca,{})' in slider_commands
    assert 'santokerWarmupTemp({})' in slider_commands
    assert 'santoker(fa,{})' in slider_commands
    assert config.get('Sliders', 'eventslidertemp') == '0, 0, 1, 0'
    assert config.get('Sliders', 'slidermin') == '0, 0, 100, 0'
    assert config.get('Sliders', 'slidermax') == '100, 100, 300, 100'
    assert config.get('Sliders', 'slidervisibilities') == '1, 0, 1, 1'

    warmup_actions = config.get('ExtraEventButtons', 'extraeventsactionstrings')
    assert 'santokerWarmup(1 - $)' in warmup_actions
    assert 'WARM-UP' in config.get('ExtraEventButtons', 'extraeventslabels')

    charge_actions = config.get('DefaultButtons', 'buttonactionstrings')
    assert charge_actions.index('santokerWarmup(0)') < charge_actions.index('santoker(80,1)')

    assert Santoker.DEFAULT_WARMUP_TEMP_C == 190.0
```

- [ ] **Step 2: Run the preset test and verify RED**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py::test_x3_master_bluetooth_preset_contract -v
```

Expected: failure because the preset does not exist.

- [ ] **Step 3: Create the preset from the existing Q + X Bluetooth setup**

Copy only as a starting point:

```bash
cp src/includes/Machines/Santoker/Q_+_X_Series_Bluetooth.aset \
  src/includes/Machines/Santoker/X3_Master_Bluetooth.aset
```

Make these intentional changes in the new file:

```ini
[General]
roastertype_setup=Santoker X3 Master BT

[events]
etypes=Air, Drum, Warm-up, Power, --
```

Retain device `134`, host/port defaults, BLE enabled, existing extra devices `135, 136`, telemetry names, and roast event visibility.

Change the CHARGE action so semantic OFF precedes the existing charge target:

```ini
buttonactionstrings="santokerWarmup(0);santoker(80,1)", "santoker(81,1)", "santoker(82,1)", , "santoker(83,1)", , "santoker(84,1)",
```

Configure slider 3:

```ini
[Sliders]
ModeTempSliders=C
eventslidertemp=0, 0, 1, 0
slideractions=11, 11, 11, 11
slidercommands="santoker(ca,{})", "santoker(c0,{})", "santokerWarmupTemp({})", "santoker(fa,{})"
sliderfactors=1, 1, 1, 1
slidermax=100, 100, 300, 100
slidermin=0, 0, 100, 0
slidervisibilities=1, 0, 1, 1
```

Append a ninth custom event button with these exact parallel-array values; leave `extraeventsbuttonsflags=0, 1, 1` unchanged because it controls palette visibility by Artisan state, not individual buttons:

```ini
extraeventbuttoncolor=#ad0427, #ad0427, #808080, #45a6cf, #45a6cf, #808080, #49b260, #49b260, #808080
extraeventbuttontextcolor=white, white, white, white, white, white, white, white, white
extraeventsactions=6, 6, 0, 6, 6, 0, 6, 6, 6
extraeventsactionstrings="santoker(fa,{})", "santoker(fa,{})", , "santoker(ca,{})", "santoker(ca,{})", , "santoker(c0,{})", "santoker(c0,{})", "santokerWarmup(1 - $)"
extraeventsdescriptions=, , , , , , , ,
extraeventslabels=-10%\n\\t, +10%\n\\t, , -10%\n\\t, +10%\n\\t, , -10%\n\\t, +10%\n\\t, WARM-UP
extraeventstypes=8, 8, 4, 5, 5, 4, 6, 6, 2
extraeventsvalues=-2, 2, 0, -2, 2, 0, -2, 2, 0
extraeventsvisibility=1, 1, 0, 1, 1, 0, 0, 0, 1
```

Update the serialized `buttonpalette` through Artisan's button-palette UI if the new ninth button is not rendered by the parallel arrays alone. Do not manually synthesize Qt `@Variant` binary data. If UI regeneration is needed, save the new preset from Artisan, then review that only the new preset changed.

- [ ] **Step 4: Run the preset contract and smoke checks**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker_warmup.py::test_x3_master_bluetooth_preset_contract -v
pytest test/smoke/artisanlib/test_main_smoke.py -v
```

Expected: preset contract passes; main smoke tests pass when Qt libraries are available.

- [ ] **Step 5: Review the preset diff for accidental copied state**

Run:

```bash
git diff -- src/includes/Machines/Santoker/X3_Master_Bluetooth.aset
git diff --check
```

Confirm the new preset contains no local serial port, profile path, account, window geometry, or machine-specific runtime state.

- [ ] **Step 6: Commit the dedicated preset**

```bash
git add src/includes/Machines/Santoker/X3_Master_Bluetooth.aset \
  src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Add Santoker X3 Master Bluetooth preset"
```

---

### Task 6: Update protocol coverage documentation and translations

**Files:**
- Modify: `SANTOKER_X3_PREHEAT_PROTOCOL.md`
- Modify through generator only: `src/translations/*.ts`, `src/translations/*.qm`, and other files changed intentionally by `src/build-derived.sh`

**Interfaces:**
- Consumes: completed protocol and preset behavior from Tasks 1–5.
- Produces: accurate implementation-gap status and translatable operator messages.

- [ ] **Step 1: Update the Artisan coverage matrix**

Change the `0x7E` and `0x7F` rows to record:

```markdown
| `0x7E` | Warm-up on/off | `WARMUP` | Yes, state callback | X3 Master Bluetooth | Yes | Physical X3 verification pending |
| `0x7F` | Warm-up target | `WARMUP_TEMP` | Yes, °C × 10 | X3 Master Bluetooth | Yes | Physical X3 verification pending |
```

Update the functional-gap list to remove missing named state, UI controls, range enforcement, pre-CHARGE checks, and header-readiness checks. Retain these unresolved gaps:

- physical BLE/header verification;
- model and firmware detection;
- setup-target semantics;
- response acknowledgement/report cadence;
- controls outside warm-up scope; and
- raw `santoker()` intentionally bypassing semantic safety.

Add the new preset path and semantic command examples:

```text
santokerWarmupTemp(190)
santokerWarmup(1)
santokerWarmup(0)
```

Explain that the temperature argument uses Artisan's current temperature-slider unit.

- [ ] **Step 2: Run Markdown checks**

Run:

```bash
git diff --check -- SANTOKER_X3_PREHEAT_PROTOCOL.md
codespell SANTOKER_X3_PREHEAT_PROTOCOL.md \
  docs/superpowers/specs/2026-07-28-santoker-x3-warmup-design.md \
  docs/superpowers/plans/2026-07-28-santoker-x3-warmup.md
```

Expected: no whitespace or spelling findings.

- [ ] **Step 3: Regenerate translated derivatives**

Run from `src/`:

```bash
cd src
./build-derived.sh
```

Expected: Qt translation sources gain the new `Message` strings. If Qt tools are unavailable, record which executable is missing and leave generated files untouched rather than hand-editing them.

- [ ] **Step 4: Review and narrow generated changes**

Run:

```bash
git status --short
git diff --stat
git diff -- src/translations
```

Keep only changes caused by the new source messages. Revert unrelated generated churn, timestamps, or derived outputs not required by this feature.

- [ ] **Step 5: Commit documentation and expected derivatives**

```bash
git add SANTOKER_X3_PREHEAT_PROTOCOL.md src/translations
git commit -m "Document Santoker X3 warm-up support"
```

If no translation files changed because tooling was unavailable, omit `src/translations` from `git add` and state that in the commit/implementation report.

---

### Task 7: Complete verification and review

**Files:**
- Review all files changed by Tasks 1–6.
- Modify only files required to fix verification findings.

**Interfaces:**
- Consumes: the complete feature.
- Produces: evidence that the implementation meets the approved design without hardware.

- [ ] **Step 1: Run focused tests together to detect isolation leaks**

Run:

```bash
cd src
pytest test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_warmup.py -v
```

Expected: all tests pass in one process and in either file order. Repeat with reversed file order if either test module mocks `sys.modules`.

- [ ] **Step 2: Run the main smoke test**

Run:

```bash
cd src
pytest test/smoke/artisanlib/test_main_smoke.py -v
```

Expected: pass. A missing EGL/Qt platform library is an environment limitation, not a reason to bypass the smoke test.

- [ ] **Step 3: Run static analysis**

Run:

```bash
cd src
ruff check artisanlib/santoker.py artisanlib/santoker_warmup.py \
  artisanlib/main.py artisanlib/canvas.py \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_warmup.py
mypy
pyright
```

Expected: all checks pass. Investigate every new finding; document unrelated pre-existing failures with exact output.

- [ ] **Step 4: Run the complete configured test suite**

Run:

```bash
cd src
pytest
```

Expected: all configured tests pass when Qt system dependencies are present. No test may require network, BLE, or roasting hardware.

- [ ] **Step 5: Run repository-wide hygiene checks**

Run from the repository root:

```bash
git diff --check
pre-commit run --all-files
```

Expected: all hooks pass. Review any broad formatter or derived-file changes before retaining them.

- [ ] **Step 6: Review behavior and scope against the design**

Use these exact review checks:

```bash
git diff 366ce6530..HEAD --stat
git diff 366ce6530..HEAD -- src/artisanlib/santoker.py
git diff 366ce6530..HEAD -- src/artisanlib/santoker_warmup.py
git diff 366ce6530..HEAD -- src/artisanlib/main.py src/artisanlib/canvas.py
git diff 366ce6530..HEAD -- src/includes/Machines/Santoker
```

Confirm:

- ON sends `0x7F` before `0x7E`;
- target range is 100–300°C;
- default is 190°C;
- Fahrenheit is converted before protocol encoding;
- no `0x7A` command was added;
- unready and post-CHARGE starts are rejected;
- reports reconcile controls without signal loops;
- CHARGE OFF precedes `0x80` in the X3 preset;
- existing presets and raw commands are unchanged; and
- no live communication occurred.

- [ ] **Step 7: Commit verification fixes, if any**

If verification required code or test corrections:

```bash
git add -p
git diff --cached --check
git commit -m "Fix Santoker X3 warm-up verification findings"
```

If no corrections were needed, do not create an empty commit. Record the exact passing commands and any environment-limited commands in the final implementation report.
