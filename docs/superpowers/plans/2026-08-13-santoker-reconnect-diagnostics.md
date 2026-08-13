# Santoker Reconnect Restoration and Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve safe Santoker warm-up intent through automatic reconnects and provide a bounded, read-only diagnostics window with decoded state and raw RX/TX evidence.

**Architecture:** Add a locked, Qt-independent session recorder that owns immutable diagnostics snapshots and formatted reports. Instrument `Santoker` at its transport/parser boundaries, extend `SantokerWarmupController` to own desired-versus-reported state and rate-limited restoration, and cross worker/UI boundaries with Qt signals. A modeless Qt dialog reads snapshots incrementally and never sends machine commands.

**Tech Stack:** Python 3.12+, PyQt6, asyncio, Bleak integration, pymodbus RTU CRC, dataclasses, `threading.RLock`, pytest, Ruff, mypy, pyright, Qt translation tools.

## Global Constraints

- Do not connect to BLE, Wi-Fi, serial, cloud, or roasting hardware in tests or validation.
- Preserve `santoker(<target>,<value>)`, `santokerWarmup(<enabled>)`, and `santokerWarmupTemp(<value>)` semantics and packet encoding.
- Never send Machine ON target `0x7A` as part of reconnect restoration.
- Warm-up ON/restoration sends target `0x7F` before state `0x7E = 1`.
- Automatic reconnect restoration is allowed only within the same monitoring session and only before CHARGE.
- A transport connection callback alone must not restore warm-up; a complete accepted Santoker frame must establish protocol readiness first.
- Restoration retries occur only on complete accepted frames and no more than once per second using a monotonic clock.
- Explicit OFF, CHARGE, and monitoring stop cancel pending desired ON; RESET never resurrects it.
- Diagnostics capture starts at monitoring start for primary device `134`, retains at most 5,000 events, survives dialog close/reopen, and is replaced by the next Santoker monitoring session.
- Diagnostics UI and exports are strictly read-only and must not include addresses, hosts/IPs, credentials, account data, profile content, or unrelated logs.
- All widget changes and restoration sends triggered by worker callbacks run in Qt main-thread slots.
- Use single-quoted strings, complete type annotations, and nearby AGPLv3+ production-module headers.
- Do not hand-edit `src/uic`, generated help, protobuf outputs, `.qm`, or `.ts` derivatives; run `src/build-derived.sh` and retain only expected generated changes.
- Release documentation must state that official-app reconnect behavior is based on static analysis of Android app v26.7.7 and still needs physical X3 verification.

---

## File Structure

### Create

- `src/artisanlib/santoker_diagnostics.py` — locked session state, immutable events/snapshots, bounded retention, safe formatting.
- `src/artisanlib/santoker_diagnostics_ui.py` — modeless read-only snapshot/history window, copy, and UTF-8 text export.
- `src/test/unitary/artisanlib/test_santoker_diagnostics.py` — recorder, immutability, retention, formatting, concurrency, and privacy tests.
- `src/test/unitary/artisanlib/test_santoker_diagnostics_ui.py` — real-widget refresh, entry-button, copy/save, and lifecycle tests.

### Modify

- `src/artisanlib/santoker.py` — optional recorder, connection/frame callbacks, accepted/rejected RX capture, exact TX capture, and reported-target separation.
- `src/artisanlib/santoker_warmup.py` — desired/report state, disconnect lifecycle, restoration state, one-second monotonic throttling, and monitoring-stop cancellation.
- `src/artisanlib/main.py` — diagnostics ownership, Qt frame signal, main-thread reconciliation, desired-intent rendering, and modeless dialog ownership.
- `src/artisanlib/canvas.py` — monitoring-session creation/completion and recorder injection into `Santoker`.
- `src/artisanlib/devices.py` — translated **Diagnostics…** button in the Santoker group.
- `src/test/unitary/artisanlib/test_santoker.py` — parser/transport instrumentation and desired/report protocol contracts.
- `src/test/unitary/artisanlib/test_santoker_warmup.py` — controller restoration, safety, lifecycle, and Qt boundary tests.
- `SANTOKER_X3_PREHEAT_PROTOCOL.md` — reconnect behavior and physical-verification caveat.
- `docs/superpowers/specs/2026-08-13-santoker-reconnect-diagnostics-design.md` — status changes from approved to implemented after verification.
- `wiki/ReleaseHistory.md` — user-facing reconnect/diagnostics note with static-analysis caveat.
- `src/translations/artisan_*.ts` and `src/translations/artisan_*.qm` — generated only through `src/build-derived.sh`.

## Shared Interfaces

`artisanlib.santoker_diagnostics` produces these exact public types and methods:

```python
TransportKind = Literal['BLE', 'Wi-Fi', 'serial']
DiagnosticCategory = Literal[
    'session', 'connection', 'protocol', 'state', 'restoration', 'rx', 'tx'
]
DiagnosticDirection = Literal['RX', 'TX']
DiagnosticField = Literal[
    'board_c', 'bt_c', 'et_c', 'ir_c', 'bt_ror_c', 'et_ror_c',
    'power', 'fan', 'drum', 'charge', 'dry', 'fcs', 'scs', 'drop'
]

class RestorationState(Enum):
    IDLE = 'idle'
    WAITING_FOR_DATA = 'waiting for valid data'
    PENDING = 'pending convergence'
    CONVERGED = 'converged'
    BLOCKED_BY_CHARGE = 'blocked by CHARGE'

@dataclass(frozen=True)
class SantokerDiagnosticEvent:
    sequence: int
    timestamp_utc: datetime
    category: DiagnosticCategory
    direction: DiagnosticDirection | None
    description: str
    packet: bytes | None

@dataclass(frozen=True)
class SantokerDiagnosticsState:
    session_started_utc: datetime
    session_ended_utc: datetime | None
    monitoring_active: bool
    transport: TransportKind
    connected: bool
    protocol_ready: bool
    active_header: str | None
    reconnect_count: int
    last_packet_utc: datetime | None
    board_c: float | None
    bt_c: float | None
    et_c: float | None
    ir_c: float | None
    bt_ror_c: float | None
    et_ror_c: float | None
    power: int | None
    fan: int | None
    drum: int | None
    desired_warmup: bool | None
    desired_target_c: float | None
    reported_warmup: bool | None
    reported_target_c: float | None
    restoration_state: RestorationState
    last_restoration_attempt_utc: datetime | None
    charge_latched: bool
    retained_event_count: int
    discarded_event_count: int

@dataclass(frozen=True)
class SantokerDiagnosticsView:
    state: SantokerDiagnosticsState
    events: tuple[SantokerDiagnosticEvent, ...]
    first_retained_sequence: int | None
    last_sequence: int

class SantokerDiagnosticsSession:
    def __init__(
        self,
        transport: TransportKind,
        *,
        now_utc: Callable[[], datetime] = utc_now,
        max_events: int = 5000,
    ) -> None: ...
    def view(self, after_sequence: int = 0) -> SantokerDiagnosticsView: ...
    def format_report(self) -> str: ...
    def record_event(
        self,
        category: DiagnosticCategory,
        description: str,
        *,
        direction: DiagnosticDirection | None = None,
        packet: bytes | None = None,
    ) -> None: ...
    def record_connection_attempt(self) -> None: ...
    def record_connected(self) -> None: ...
    def record_disconnected(self) -> None: ...
    def record_protocol(self, ready: bool, header: bytes | None) -> None: ...
    def record_rx(self, packet: bytes, description: str, *, accepted: bool) -> None: ...
    def record_tx(self, packet: bytes, description: str) -> None: ...
    def record_decoded(self, field: DiagnosticField, value: float | int | bool) -> None: ...
    def record_desired_warmup(self, enabled: bool | None, target_c: float) -> None: ...
    def record_reported_warmup(self, enabled: bool | None) -> None: ...
    def record_reported_target(self, target_c: float | None) -> None: ...
    def record_restoration(
        self, state: RestorationState, description: str, *, attempted: bool = False
    ) -> None: ...
    def record_charge_latch(self, latched: bool) -> None: ...
    def stop(self) -> None: ...
```

Protocol headers are exposed in diagnostics as uppercase `A5`/`B5` strings, never as transport addresses.

`Santoker` adds these compatible interfaces:

```python
def __init__(
    ...,
    ready_handler: Callable[[bool], None] | None = None,
    frame_handler: Callable[[], None] | None = None,
    diagnostics: SantokerDiagnosticsSession | None = None,
) -> None: ...

def getReportedWarmupTarget(self) -> float | None: ...
def requestWarmupOn(self, temp_c: float) -> bool: ...
```

`SantokerWarmupDevice` extends its existing methods with:

```python
class SantokerWarmupDevice(Protocol):
    def isHeaderReady(self) -> bool: ...
    def getWarmup(self) -> bool | None: ...
    def getReportedWarmupTarget(self) -> float | None: ...
    def setWarmupTarget(self, temp_c: float) -> bool: ...
    def setWarmup(self, enabled: bool) -> bool: ...
    def requestWarmupOn(self, temp_c: float) -> bool: ...
```

`SantokerWarmupController` adds these exact interfaces:

```python
class ReconcileOutcome(Enum):
    NONE = 'none'
    WAITING = 'waiting'
    ATTEMPTED = 'attempted'
    THROTTLED = 'throttled'
    CONVERGED = 'converged'
    FORCED_OFF = 'forced_off'

class SantokerWarmupController:
    def attach_diagnostics(self, session: SantokerDiagnosticsSession | None) -> None: ...
    def desired_enabled(self) -> bool | None: ...
    def reported_enabled(self) -> bool | None: ...
    def reported_target_c(self) -> float | None: ...
    def restoration_state(self) -> RestorationState: ...
    def note_transport_loss(self) -> None: ...
    def accept_reported_state(self, enabled: bool | None) -> None: ...
    def accept_reported_target(self, temp_c: float) -> None: ...
    def reconcile_after_frame(
        self,
        charge_index: int,
        device: SantokerWarmupDevice | None,
    ) -> ReconcileOutcome: ...
    def stop_monitoring(self, device: SantokerWarmupDevice | None) -> None: ...
```

`ApplicationWindow` adds these lifecycle helpers so `canvas.py` keeps only device selection/orchestration:

```python
def startSantokerDiagnosticsSession(self) -> SantokerDiagnosticsSession: ...
def stopSantokerMonitoring(self) -> None: ...
def showSantokerDiagnostics(self) -> None: ...
```

---

### Task 1: Build the bounded diagnostics session model

**Files:**
- Create: `src/artisanlib/santoker_diagnostics.py`
- Create: `src/test/unitary/artisanlib/test_santoker_diagnostics.py`

**Interfaces:**
- Consumes: only Python standard-library types; no Qt or Artisan application imports.
- Produces: all `santoker_diagnostics` shared interfaces above.

- [ ] **Step 1: Write failing immutable snapshot and event-order tests**

Create deterministic UTC timestamps and assert frozen values, sequence order, and incremental retrieval:

```python
from dataclasses import FrozenInstanceError
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from artisanlib.santoker_diagnostics import SantokerDiagnosticsSession


def clock() -> Callable[[], datetime]:
    current = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        value = current
        current += timedelta(milliseconds=1)
        return value

    return now


def test_snapshot_is_immutable_and_events_are_incremental() -> None:
    session = SantokerDiagnosticsSession('BLE', now_utc=clock())
    session.record_connected()
    session.record_tx(b'\xee\xb5', 'warm-up request')

    full = session.view()
    incremental = session.view(after_sequence=full.events[-2].sequence)

    assert [event.sequence for event in full.events] == [1, 2, 3]
    assert [event.category for event in incremental.events] == ['tx']
    assert full.events[-1].packet == b'\xee\xb5'
    with pytest.raises(FrozenInstanceError):
        full.state.connected = False  # type: ignore[misc]
```

The constructor records sequence 1 as monitoring start. Use UTC-aware timestamps only.

- [ ] **Step 2: Write failing retention, unknown rendering, privacy, and concurrency tests**

Add exact contracts:

```python
def test_retention_reports_exact_discard_count() -> None:
    session = SantokerDiagnosticsSession('serial', max_events=3)
    for value in range(5):
        session.record_event('state', f'value={value}')

    view = session.view()
    assert [event.description for event in view.events] == [
        'value=2', 'value=3', 'value=4'
    ]
    assert view.state.retained_event_count == 3
    assert view.state.discarded_event_count == 3  # start event plus value=0 and value=1
    assert '[older entries discarded: 3]' in session.format_report()


def test_report_uses_unknown_and_excludes_connection_identity() -> None:
    session = SantokerDiagnosticsSession('Wi-Fi')
    report = session.format_report()

    assert 'board: unknown' in report
    assert 'reported warm-up: unknown' in report
    assert 'host' not in report.lower()
    assert 'ip address' not in report.lower()
    assert 'password' not in report.lower()
```

Use four `threading.Thread` workers with 250 unique `record_event()` calls each. After joining, assert 1,001 unique, strictly increasing sequence numbers and no exceptions. Add a deduplication test proving repeated `record_decoded('power', 40)` updates snapshot state but emits one decoded state-transition event.

- [ ] **Step 3: Run the model tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics.py -v
```

Expected: collection fails because `artisanlib.santoker_diagnostics` does not exist.

- [ ] **Step 4: Implement frozen public values and locked mutable storage**

Use `threading.RLock`, `collections.deque(maxlen=max_events)`, a monotonically increasing integer sequence, and an internal mutable dictionary for decoded values. Increment `discarded_event_count` before each append when the deque is full. Copy `packet` with `bytes(packet)` and return events in tuples.

Every mutator first checks `monitoring_active`; `stop()` appends `monitoring stopped`, sets end time/active state, and is idempotent. `record_connection_attempt()` records `transport start requested` without connection identity. `record_connected()` counts the first connection as zero reconnects and each later disconnected→connected transition as one reconnect. `record_disconnected()` clears connected/readiness/header, reported warm-up/target, and current machine readings to `None`, while retaining last-packet time and desired intent.

Render packet bytes as uppercase spaced hex:

```python
def _format_packet(packet: bytes | None) -> str:
    return '' if packet is None else ' '.join(f'{value:02X}' for value in packet)
```

`format_report()` starts with `Artisan Santoker Diagnostics v1`, renders every `SantokerDiagnosticsState` field in fixed order, uses `unknown` for `None`, emits `[older entries discarded: N]` when `N > 0`, then renders retained events chronologically and ends with exactly one newline. `record_protocol()` converts recognized byte headers to `A5`/`B5`; any other non-`None` header is rendered `unknown`. `record_restoration(..., attempted=True)` updates `last_restoration_attempt_utc`. The model never accepts or accesses host, port, BLE address, file path, profile, settings, account, or log objects.

- [ ] **Step 5: Run model tests and static checks GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics.py -v
.venv/bin/ruff check artisanlib/santoker_diagnostics.py test/unitary/artisanlib/test_santoker_diagnostics.py
.venv/bin/mypy artisanlib/santoker_diagnostics.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit the diagnostics model**

```bash
git add src/artisanlib/santoker_diagnostics.py \
  src/test/unitary/artisanlib/test_santoker_diagnostics.py
git commit -m "Add Santoker diagnostics session model"
```

---

### Task 2: Instrument Santoker RX, TX, connection, and reported state

**Files:**
- Modify: `src/artisanlib/santoker.py:130-490`
- Modify: `src/test/unitary/artisanlib/test_santoker.py:850-end`

**Interfaces:**
- Consumes: `SantokerDiagnosticsSession` from Task 1.
- Produces: optional constructor recorder/frame callback, exact RX/TX events, `getReportedWarmupTarget()`, and `requestWarmupOn()`.

- [ ] **Step 1: Write failing TX and reported-target separation tests**

Add tests proving the same frame object content reaches diagnostics and transport, and reports never replace desired target:

```python
def test_send_records_exact_frame_passed_to_transport() -> None:
    session = SantokerDiagnosticsSession('Wi-Fi')
    santoker = Santoker(diagnostics=session)

    with patch.object(Santoker, 'send') as send:
        santoker.send_msg(Santoker.WARMUP, 1)

    packet = send.call_args.args[0]
    event = session.view().events[-1]
    assert event.direction == 'TX'
    assert event.packet == packet
    assert 'requested' in event.description


def test_reported_target_does_not_replace_desired_target() -> None:
    santoker = Santoker(warmup_target=205.0)
    santoker.register_reading(Santoker.WARMUP_TEMP, (1900).to_bytes(3, 'big'))

    assert santoker.getWarmupTarget() == 205.0
    assert santoker.getReportedWarmupTarget() == 190.0
```

Update existing tests that currently expect `getWarmupTarget()` to follow `0x7F`; assert `getReportedWarmupTarget()` instead. Update send tests so command requests do not mutate `getWarmup()`: it remains a report value and changes only through valid `0x7E` input.

- [ ] **Step 2: Write failing accepted/rejected frame and connection tests**

Use `read_packet()` and parameterized corruptions for exact rejection descriptions:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('packet_edit', 'reason', 'available_length'),
    [
        (lambda data: data.__setitem__(4, 0x03), 'invalid code header', 5),
        (lambda data: data.__setitem__(5, 0x02), 'invalid data length', 6),
        (lambda data: data.__setitem__(-5, data[-5] ^ 1), 'CRC mismatch', 11),
        (lambda data: data.__setitem__(-1, 0), 'invalid tail', 15),
    ],
)
async def test_rejected_frame_records_reason(
    packet_edit: Callable[[bytearray], None],
    reason: str,
    available_length: int,
) -> None:
    session = SantokerDiagnosticsSession('BLE')
    sender = Santoker()
    receiver = Santoker(connect_using_ble=True, diagnostics=session)
    packet = bytearray(sender.create_msg(Santoker.WARMUP_TEMP, 1900))
    packet_edit(packet)

    await read_packet(receiver, bytes(packet))

    event = session.view().events[-1]
    assert event.category == 'rx'
    assert event.direction == 'RX'
    assert reason in event.description
    assert event.packet == bytes(packet[:available_length])
```

Import `Callable` from `collections.abc` for the parameterized edits. Add separate invalid-second-header and truncated-frame tests; truncated parsing still raises `asyncio.IncompleteReadError` after recording available candidate bytes. Add an accepted-frame test asserting raw RX, decoded target/value, ready/header snapshot, and one `frame_handler()` call per accepted duplicate frame. Invoke the internal connected/disconnected handlers and assert reconnect count, readiness reset, machine-state reset, and reported warm-up/target reset occur before the external disconnect callback. Parameterize TX coverage across socket and BLE fakes and assert each receives the exact packet retained by diagnostics.

Add failure-containment tests with a mock diagnostics object whose `record_protocol()`, `record_decoded()`, `record_rx()`, and `record_tx()` each raise `RuntimeError('recorder failed')`. A valid frame must still become ready/decode, and a TX request must still reach the mocked transport.

- [ ] **Step 3: Run protocol tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker.py \
  -k 'diagnostic or reported_target or rejected_frame or accepted_frame or reconnect' -v
```

Expected: failures because the recorder, frame callback, reported-target getter, and candidate-byte capture do not exist.

- [ ] **Step 4: Add optional instrumentation and preserve parser behavior**

Extend `__slots__` and constructor with `_diagnostics` and `_frame_handler`. Wrap recorder calls in a private helper that catches `Exception` and logs with `_log.exception()` so diagnostics can never alter protocol acceptance or writes.

Add one local async helper that appends successful/partial reads exactly once:

```python
async def read_candidate(size: int) -> bytes:
    try:
        part = await stream.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        candidate.extend(exc.partial)
        self._record_rejected(bytes(candidate), 'truncated frame')
        raise
    candidate.extend(part)
    return part
```

In `read_msg()`:

1. discard bytes before the first `0xEE` without recording them;
2. start a `bytearray` candidate at `0xEE`;
3. use `read_candidate()` for every remaining field;
4. require the fixed data-length byte `0x03`, then record and return for invalid second header, code header, data length, CRC, or tail;
5. only after all checks pass, adopt the candidate header, set readiness, decode the reading, record accepted RX, and invoke `_frame_handler()` inside the same callback-containment boundary.

Record decoded state only when its value changes; every accepted raw frame is still retained. Map `BOARD/BT/ET/IR/BT_ROR/ET_ROR/POWER/AIR/DRUM` to `board_c/bt_c/et_c/ir_c/bt_ror_c/et_ror_c/power/fan/drum`, map `CHARGE/DRY/FCs/SCs/DROP` to their milestone fields, and use the specialized reported warm-up/target methods for `0x7E/0x7F`. `record_rx(..., accepted=True)` updates `last_packet_utc`; rejected candidates do not. `resetProtocolState()` records ready false/header unknown and clears only reported warm-up/target.

Construct TX once:

```python
def send_msg(self, target: bytes, value: int) -> None:
    packet = self.create_msg(target, value)
    self._record_tx(packet, target, value)
    if self._connect_using_ble and self._ble_client is not None:
        self._ble_client.send(packet)
    else:
        self.send(packet)
```

Create internal connected/disconnected wrappers for both `AsyncComm` and `SantokerCube_BLE`. They record transport transitions before calling existing external callbacks and never record host, port, serial path, or BLE address.

- [ ] **Step 5: Separate requested and reported warm-up state**

Stop assigning `_warmup_target` in `register_reading(WARMUP_TEMP, ...)`; update only `_reported_warmup_target` and call the target handler on report changes. Stop calling `_setWarmupState()` from `setWarmup()`; requested writes do not masquerade as reports.

Add:

```python
def getReportedWarmupTarget(self) -> float | None:
    return self._reported_warmup_target


def requestWarmupOn(self, temp_c: float) -> bool:
    if not self._header_ready or not self.MIN_WARMUP_TEMP_C <= temp_c <= self.MAX_WARMUP_TEMP_C:
        return False
    self._warmup_target = temp_c
    self.send_msg(self.WARMUP_TEMP, int(round(temp_c * 10)))
    self.send_msg(self.WARMUP, 1)
    return True
```

Make `setWarmup(True)` delegate to `requestWarmupOn(self._warmup_target)` and retain `setWarmup(False)` as one OFF request. Existing raw command encoding and partial-CRC compatibility remain unchanged.

- [ ] **Step 6: Run complete protocol tests and static checks GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker.py -v
.venv/bin/ruff check artisanlib/santoker.py test/unitary/artisanlib/test_santoker.py
.venv/bin/mypy artisanlib/santoker.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit protocol instrumentation**

```bash
git add src/artisanlib/santoker.py src/test/unitary/artisanlib/test_santoker.py
git commit -m "Instrument Santoker protocol diagnostics"
```

---

### Task 3: Add desired/report separation and rate-limited restoration

**Files:**
- Modify: `src/artisanlib/santoker_warmup.py:25-end`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py:1-220`

**Interfaces:**
- Consumes: `RestorationState`, optional diagnostics session, and `SantokerWarmupDevice.requestWarmupOn()`/`getReportedWarmupTarget()`.
- Produces: all controller shared interfaces and `ReconcileOutcome`.

- [ ] **Step 1: Extend the fake device and write failing reconnect tests**

Add these methods to `FakeWarmupDevice`:

```python
reported_target: float | None = None

def getReportedWarmupTarget(self) -> float | None:
    return self.reported_target


def requestWarmupOn(self, temp_c: float) -> bool:
    if not self.ready:
        return False
    self.calls.extend([('target', temp_c), ('enabled', True)])
    return True
```

Use an injected mutable monotonic clock and test this exact sequence:

```python
def test_reconnect_restores_target_then_on_after_valid_frame() -> None:
    now = [10.0]
    device = FakeWarmupDevice(ready=True, warmup=False, reported_target=190.0)
    controller = SantokerWarmupController(
        desired_temp_c=205.0,
        monotonic_clock=lambda: now[0],
    )
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.calls.clear()

    device.ready = False
    controller.note_transport_loss()
    assert controller.desired_enabled() is True
    assert controller.restoration_state() is RestorationState.WAITING_FOR_DATA
    assert device.calls == []

    device.ready = True
    device.warmup = False
    device.reported_target = 190.0
    assert controller.reconcile_after_frame(-1, device) is ReconcileOutcome.ATTEMPTED
    assert device.calls == [('target', 205.0), ('enabled', True)]
```

Add tests for connection/readiness notification without `reconcile_after_frame()` sending nothing; unknown reports triggering an immediate first attempt; duplicate frames at `10.2` returning `THROTTLED`; a frame at `11.0` attempting again; matching target+ON returning `CONVERGED`; and no blind timer activity when no frame method is called.

- [ ] **Step 2: Write failing OFF, CHARGE, RESET, and stop tests**

Cover these exact contracts:

- OFF while `device is None` returns `WarmupResult.OK`, sets desired false, and sends nothing.
- OFF while unready returns OK and cancels a waiting restoration.
- `mark_charge()` remembers that a safety OFF is required when desired or reported state was ON, clears desired ON, and records blocked state.
- after `mark_charge()`, the existing semantic OFF sends `0x7E = 0` before raw CHARGE even when the last report was false/unknown; a failed readiness race retains the safety-OFF requirement for the next valid frame.
- `reset_charge()` clears only the latch; desired remains false and cannot be resurrected.
- `stop_monitoring()` requests OFF once when ready and desired/reported ON, then clears desired/report/restoration even if the write returns false.
- `accept_reported_target(190.0)` updates only reported target and leaves `desired_temp_c == 205.0`.
- two threads racing OFF and `note_transport_loss()` finish with desired false and no restoration attempt;
- a diagnostics mock that raises from every recorder method cannot change command results or controller state.

- [ ] **Step 3: Run controller tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup.py \
  -k 'reconnect or restoration or desired or reported or monitoring_stop' -v
```

Expected: failures because the controller still treats reports as desired state and has no restoration lifecycle.

- [ ] **Step 4: Implement locked controller state and typed outcomes**

Add fields under the existing `RLock`:

```python
_desired_enabled: bool | None = field(default=None, init=False, repr=False)
_reported_enabled: bool | None = field(default=None, init=False, repr=False)
_reported_target_c: float | None = field(default=None, init=False, repr=False)
_restoration_state: RestorationState = field(default=RestorationState.IDLE, init=False)
_last_attempt_monotonic: float | None = field(default=None, init=False, repr=False)
_safety_off_pending: bool = field(default=False, init=False, repr=False)
monotonic_clock: Callable[[], float] = field(default=monotonic, repr=False, compare=False)
_diagnostics: SantokerDiagnosticsSession | None = field(default=None, init=False, repr=False)
```

`set_enabled(True, ...)` validates device/readiness/CHARGE, calls `requestWarmupOn(desired_temp_c)`, and records desired ON only after success. `set_enabled(False, ...)` first captures whether desired ON, reported ON, or `_safety_off_pending` requires an OFF, then records desired false. It returns OK without writing when disconnected/unready; when ready and OFF is required, it requests OFF once and clears `_safety_off_pending` only after the request is accepted.

`mark_charge()` sets `_safety_off_pending` before clearing desired ON whenever desired/reported state was ON. `reset_charge()` never sets desired ON. `stop_monitoring()` clears `_safety_off_pending` after its final safe OFF attempt.

`reconcile_after_frame()` performs this ordered decision:

1. if CHARGE is latched/indexed, clear desired ON, force OFF when `_safety_off_pending` or reported ON requires it, and return `FORCED_OFF` or `NONE`;
2. if desired is not ON, set `IDLE` and return `NONE`;
3. if device is absent/unready, set `WAITING_FOR_DATA` and return `WAITING`;
4. read reported warm-up and target from the device into controller report fields;
5. if both match desired, set `CONVERGED` and return `CONVERGED`;
6. if less than 1.0 monotonic second has elapsed, set `PENDING`, record throttle, and return `THROTTLED`;
7. update the attempt timestamp, call `requestWarmupOn(desired_temp_c)`, set `PENDING`, and return `ATTEMPTED` on success or `WAITING` on a readiness race.

`note_transport_loss()` clears reported fields and attempt timestamp, preserving desired target/ON. `stop_monitoring()` preserves `desired_temp_c` but clears desired enabled intent to `None`, reported fields, attempt timing, and restoration state after requesting OFF when safe. `attach_diagnostics()` immediately synchronizes current desired target/state and CHARGE latch into the new session. Every later state mutation mirrors safe desired/reported/restoration/CHARGE data into the optional diagnostics session; recorder exceptions are logged and contained.

- [ ] **Step 5: Run controller and protocol tests GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup.py -v
.venv/bin/pytest test/unitary/artisanlib/test_santoker.py -v
.venv/bin/ruff check artisanlib/santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_warmup.py
.venv/bin/mypy artisanlib/santoker_warmup.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit restoration logic**

```bash
git add src/artisanlib/santoker_warmup.py \
  src/test/unitary/artisanlib/test_santoker_warmup.py
git commit -m "Restore Santoker warm-up after reconnect"
```

---

### Task 4: Integrate monitoring lifecycle and Qt-thread reconciliation

**Files:**
- Modify: `src/artisanlib/main.py:220-245,1450-1535,1860-1885,4345-4370,12439-12467,19648-19765`
- Modify: `src/artisanlib/canvas.py:13275-13330,13520-13545`
- Modify: `src/test/unitary/artisanlib/test_santoker_warmup.py`
- Modify: `src/test/unitary/artisanlib/test_santoker_diagnostics.py`

**Interfaces:**
- Consumes: Tasks 1–3 recorder/protocol/controller APIs.
- Produces: `santokerFrameSignal`, `startSantokerDiagnosticsSession()`, `stopSantokerMonitoring()`, one current/completed session on `ApplicationWindow`, desired-intent top-bar rendering, and start/stop lifecycle integration.

- [ ] **Step 1: Write failing main-thread and desired-intent UI tests**

Extend `compact_window()` with a fake diagnostics session and add:

```python
def test_pending_reconnect_keeps_desired_button_checked_and_disabled(
    qapplication: QApplication,
) -> None:
    controls = SantokerWarmupControls()
    controller = SantokerWarmupController(desired_temp_c=205.0)
    device = FakeWarmupDevice(ready=True)
    assert controller.set_enabled(True, -1, device) is WarmupResult.OK
    device.ready = False
    controller.note_transport_loss()
    window = compact_window(controls, controller, device)

    ApplicationWindow.updateSantokerWarmupControls(cast(ApplicationWindow, window))

    assert controls.button.isChecked()
    assert not controls.button.isEnabled()
```

Use a real `ApplicationWindow.__new__`/`QMainWindow.__init__` object as in the existing worker test. Emit the new frame signal from a Python worker thread, process `QEvent.Type.MetaCall`, and assert `requestWarmupOn()` ran on the Qt main thread. Add a transport-connected callback test proving it records connection but emits no frame signal and sends no restoration command.

- [ ] **Step 2: Write failing session lifecycle tests**

Test `ApplicationWindow.startSantokerDiagnosticsSession()` and `stopSantokerMonitoring()` unbound on typed `SimpleNamespace` fakes. Assert:

- start selects BLE, serial, or Wi-Fi from the two flags without passing address/host;
- start replaces a prior completed session, attaches the new session to the controller, and records `transport start requested` before `Santoker.start()` is called by Canvas;
- automatic disconnect never calls `stopSantokerMonitoring()` and keeps the same session object and desired ON;
- stop calls `controller.stop_monitoring()` before `santoker.stop()`, sets `santoker=None`, then calls `session.stop()`;
- the `canvas.py` device-134 branch calls the start helper before constructing/starting `Santoker`, while non-134 branches contain no call to that helper. Verify this final branch-selection contract with `inspect.getsource()` indices rather than constructing the complete monitoring graph.

- [ ] **Step 3: Run integration tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup.py \
  -k 'pending_reconnect or frame_signal or monitoring_session' -v
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics.py \
  -k 'lifecycle or replacement' -v
```

Expected: failures because application/session ownership and the frame signal are absent.

- [ ] **Step 4: Add application ownership and queued frame reconciliation**

Import diagnostics types under normal/type-checking imports, add `santokerDiagnosticsSession` and `santokerDiagnosticsDialog` to `ApplicationWindow.__slots__`, and initialize both to `None`. Add/connect:

```python
santokerFrameSignal = pyqtSignal()
self.santokerFrameSignal.connect(
    self.santokerFrameAccepted,
    type=Qt.ConnectionType.QueuedConnection,
)
```

Change the existing ready, warm-up state, and warm-up target signal connections to explicit `Qt.ConnectionType.QueuedConnection` as well. Signals emitted from one protocol worker retain ready→report→frame queue order, and every corresponding slot runs on the GUI thread.

Implement the main-thread slot:

```python
@pyqtSlot()
def santokerFrameAccepted(self) -> None:
    outcome = self.santokerWarmupController.reconcile_after_frame(
        self.qmc.timeindex[0], self.santoker
    )
    self.refreshSantokerWarmupControls()
    if outcome is ReconcileOutcome.FORCED_OFF:
        self.sendmessage(QApplication.translate(
            'Message',
            'Santoker warm-up reported ON after CHARGE; sending OFF',
        ))
```

`ready=False` calls `note_transport_loss()` and refreshes. `ready=True` only refreshes; it does not restore. State/target slots call controller `accept_reported_*()` and refresh without copying reported values into desired state. Remove the old `santokerWarmupStateChanged()` direct call to `reconcile_reported_state()` so restoration/safety writes occur only from `santokerFrameAccepted()`. Accepted-frame ordering invokes the frame slot after protocol report signals from the same worker.

Change `updateSantokerWarmupControls()` so checked/style state is `pre_charge and controller.desired_enabled() is True`; readiness still controls button enablement. This keeps desired ON checked-but-disabled during reconnect and prevents stale OFF reports from visually cancelling pending intent.

- [ ] **Step 5: Create and complete sessions in Canvas**

Implement the helpers in `ApplicationWindow`:

```python
def startSantokerDiagnosticsSession(self) -> SantokerDiagnosticsSession:
    transport: TransportKind = (
        'BLE' if self.santokerBLE else
        'serial' if self.santokerSerial else
        'Wi-Fi'
    )
    session = SantokerDiagnosticsSession(transport)
    self.santokerDiagnosticsSession = session
    self.santokerWarmupController.attach_diagnostics(session)
    return session


def stopSantokerMonitoring(self) -> None:
    self.santokerWarmupController.stop_monitoring(self.santoker)
    if self.santoker is not None:
        self.santoker.stop()
        self.santoker = None
    if self.santokerDiagnosticsSession is not None:
        self.santokerDiagnosticsSession.stop()
```

When monitoring starts with `self.device == 134`, call `session = self.aw.startSantokerDiagnosticsSession()` before the real/simulated transport branch. In the real-device branch, call `session.record_connection_attempt()` immediately before constructing/starting `Santoker`, then pass `diagnostics=session` and `frame_handler=self.aw.santokerFrameSignal.emit`. A simulator session records monitoring but no transport attempt. Do not emit the desired target from `getWarmupTarget()` as a reported target at construction.

On explicit monitoring stop for device 134, call `self.aw.stopSantokerMonitoring()` outside the `not simulator` hardware-disconnect guard, in place of the inline Santoker stop block. In `ApplicationWindow.stopActivities()`, replace the direct device-134 `stop()`/`None` branch with `stopSantokerMonitoring()` so application shutdown also requests OFF and freezes diagnostics before the later monitor toggle. Both controller/session stop operations are idempotent.

This keeps capture active through requested OFF and protocol disconnect/reset, then freezes the completed session. Automatic reconnects never execute this stop path and reuse the same objects.

- [ ] **Step 6: Run integration, protocol, and UI tests in both orders**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_warmup_ui.py -v
.venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup_ui.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker.py -v
.venv/bin/ruff check artisanlib/main.py artisanlib/canvas.py \
  test/unitary/artisanlib/test_santoker_warmup.py
```

Expected: both pytest orders and Ruff exit 0 with no worker-thread widget mutation.

- [ ] **Step 7: Commit lifecycle integration**

```bash
git add src/artisanlib/main.py src/artisanlib/canvas.py \
  src/test/unitary/artisanlib/test_santoker_warmup.py \
  src/test/unitary/artisanlib/test_santoker_diagnostics.py
git commit -m "Integrate Santoker reconnect lifecycle"
```

---

### Task 5: Build the read-only diagnostics window

**Files:**
- Create: `src/artisanlib/santoker_diagnostics_ui.py`
- Create: `src/test/unitary/artisanlib/test_santoker_diagnostics_ui.py`

**Interfaces:**
- Consumes: `Callable[[], SantokerDiagnosticsSession | None]` and Task 1 views/reports.
- Produces: `SantokerDiagnosticsDialog` and `create_santoker_diagnostics_button()`.

```python
def create_santoker_diagnostics_button(
    callback: Callable[[], None], parent: QWidget | None = None
) -> QPushButton: ...

class SantokerDiagnosticsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        session_provider: Callable[[], SantokerDiagnosticsSession | None],
    ) -> None: ...
    def refresh(self) -> None: ...
```

- [ ] **Step 1: Write failing no-session and read-only widget tests**

Set `QT_QPA_PLATFORM=offscreen` before PyQt imports. Instantiate real widgets and assert:

```python
def test_no_session_dialog_is_read_only(qapplication: QApplication) -> None:
    parent = QWidget()
    dialog = SantokerDiagnosticsDialog(parent, lambda: None)
    dialog.show()
    qapplication.processEvents()

    assert 'No Santoker monitoring session captured' in dialog.history.toPlainText()
    assert dialog.history.isReadOnly()
    assert not dialog.findChildren(QLineEdit)
    assert {button.text() for button in dialog.findChildren(QPushButton)} == {
        'Copy All', 'Save as Text…', 'Close'
    }
```

Also instantiate the button factory, assert translated text `Diagnostics…`, click it, and verify exactly one callback invocation.

- [ ] **Step 2: Write failing incremental refresh and eviction tests**

Create an active session, open the dialog, call `refresh()`, append one event, call `refresh()` again, and assert the original history line appears once and only the new line is appended. With `max_events=2`, overflow while the dialog is closed, reopen/refresh, and assert the retained range is rebuilt and `[older entries discarded: N]` is displayed.

Assert labels cover monitoring, transport, connected, ready, header, reconnect count, last packet, all machine fields, desired/reported warm-up and target, restoration, and CHARGE latch. Unknown fields must show `unknown`.

- [ ] **Step 3: Write failing copy/save/error tests**

Patch `QApplication.clipboard()` and assert **Copy All** uses `session.format_report()`. Patch `QFileDialog.getSaveFileName()` for:

- cancellation: no file operation and no message box;
- success: UTF-8 content exactly equals the report with normalized `\n` endings;
- `OSError('disk full')`: `QMessageBox.warning()` is called, the dialog stays visible, and session history remains unchanged.

- [ ] **Step 4: Run dialog tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py -v
```

Expected: collection fails because the UI module does not exist.

- [ ] **Step 5: Implement the modeless incremental dialog**

Use a plain `QDialog` with `WA_DeleteOnClose=False`, translated title `Santoker Diagnostics`, read-only `QLabel` values, and a read-only `QPlainTextEdit` history. Build three translated `QGroupBox` sections named `Session and Connection`, `Machine State`, and `Warm-up State`; place the history under a translated `History` label. Do not create editable fields, context command actions, clear/retry/reconnect controls, target inputs, or machine-control signals.

A 250 ms `QTimer(self)` calls `refresh()` on the GUI thread. Track `_session: SantokerDiagnosticsSession | None`, `_last_sequence`, and `_first_retained_sequence`. Keeping the object reference prevents identity reuse. On a new session or eviction gap, rebuild retained history once; otherwise append only events newer than `_last_sequence`. Update a dedicated discarded-count label on every refresh without rebuilding history. `showEvent()` starts/refreshes the timer; `closeEvent()` stops it and hides/closes only the window.

For export:

```python
normalized = session.format_report().replace('\r\n', '\n').replace('\r', '\n')
Path(filename).write_text(normalized, encoding='utf-8', newline='\n')
```

Copy uses the same normalized report. File-dialog cancellation returns immediately. Catch `OSError` and show translated Error/Failed-to-save text without changing the session. Catch/log unexpected exceptions from `session_provider()`, `view()`, and `format_report()`; refresh shows translated `Diagnostics unavailable`, while copy/save shows a translated warning and leaves the window/session intact. Add tests with fake providers/views/formatters raising `RuntimeError('format failed')`.

- [ ] **Step 6: Run real-widget tests and static checks GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py -v
.venv/bin/ruff check artisanlib/santoker_diagnostics_ui.py \
  test/unitary/artisanlib/test_santoker_diagnostics_ui.py
.venv/bin/mypy artisanlib/santoker_diagnostics_ui.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit the diagnostics window**

```bash
git add src/artisanlib/santoker_diagnostics_ui.py \
  src/test/unitary/artisanlib/test_santoker_diagnostics_ui.py
git commit -m "Add Santoker diagnostics window"
```

---

### Task 6: Add Device Configuration entry and modeless ownership

**Files:**
- Modify: `src/artisanlib/devices.py:1165-1335`
- Modify: `src/artisanlib/main.py:1480-1535,1870-1885,23290-23350,26965-27000`
- Modify: `src/test/unitary/artisanlib/test_santoker_diagnostics_ui.py`

**Interfaces:**
- Consumes: Task 5 dialog/button factory and `ApplicationWindow.santokerDiagnosticsSession`.
- Produces: `ApplicationWindow.showSantokerDiagnostics() -> None` and one reusable dialog instance.

- [ ] **Step 1: Write failing entry-point and ownership tests**

Use a real `QMainWindow` fake with `santokerDiagnosticsSession=None` and `santokerDiagnosticsDialog=None`. Call the unbound application method twice and assert the same visible dialog instance is raised rather than replaced. Close it, assign a completed session, call the method again, and assert the same instance displays completed history.

Instantiate the real factory button used by `DeviceAssignmentDlg`, click it, and assert it calls `aw.showSantokerDiagnostics()` without calling the Device dialog's `okEvent()`, `cancelEvent()`, `accept()`, or `reject()`. Add an `inspect.getsource(DeviceAssignmentDlg.__init__)` contract asserting the factory receives `self.aw.showSantokerDiagnostics` and `santokerVBox.addWidget(self.santokerDiagnosticsButton)` places it in the Santoker group.

- [ ] **Step 2: Run entry tests and verify RED**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py \
  -k 'entry or ownership or reopen' -v
```

Expected: failures because the application method and Device Configuration button are absent.

- [ ] **Step 3: Add the translated Santoker-group button**

In `DeviceAssignmentDlg.__init__()`, create and store:

```python
self.santokerDiagnosticsButton = create_santoker_diagnostics_button(
    self.aw.showSantokerDiagnostics,
    self,
)
```

Add it to the Santoker group beneath event flags with stretches for alignment. It remains enabled regardless of monitoring state and does not read/apply the dialog's host, port, transport, or event-widget values.

- [ ] **Step 4: Own and raise one modeless dialog in ApplicationWindow**

Implement:

```python
@pyqtSlot()
def showSantokerDiagnostics(self) -> None:
    if self.santokerDiagnosticsDialog is None:
        self.santokerDiagnosticsDialog = SantokerDiagnosticsDialog(
            self,
            lambda: self.santokerDiagnosticsSession,
        )
    self.santokerDiagnosticsDialog.refresh()
    self.santokerDiagnosticsDialog.show()
    self.santokerDiagnosticsDialog.raise_()
    self.santokerDiagnosticsDialog.activateWindow()
```

The session provider lambda makes an already-open window follow a newly created monitoring session. Closing Device Configuration does not close diagnostics. In application shutdown, stop/close the diagnostics dialog timer/window with the other owned modeless windows; do not clear the completed session merely because the window closes.

- [ ] **Step 5: Run UI, warm-up, and smoke tests GREEN**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py -v
.venv/bin/pytest test/unitary/artisanlib/test_santoker_warmup.py -v
.venv/bin/pytest test/smoke/artisanlib/test_main_smoke.py -v
.venv/bin/ruff check artisanlib/devices.py artisanlib/main.py \
  test/unitary/artisanlib/test_santoker_diagnostics_ui.py
```

Expected: all commands exit 0 when Qt platform libraries are available. Record the exact Qt import/platform error if the environment blocks smoke tests; do not replace the real-widget coverage with mocks.

- [ ] **Step 6: Commit Device Configuration integration**

```bash
git add src/artisanlib/devices.py src/artisanlib/main.py \
  src/test/unitary/artisanlib/test_santoker_diagnostics_ui.py
git commit -m "Expose Santoker diagnostics in device config"
```

---

### Task 7: Update protocol/release documentation and generated translations

**Files:**
- Modify: `SANTOKER_X3_PREHEAT_PROTOCOL.md`
- Modify: `docs/superpowers/specs/2026-08-13-santoker-reconnect-diagnostics-design.md`
- Modify: `wiki/ReleaseHistory.md`
- Generate: `src/translations/artisan_*.ts`
- Generate: `src/translations/artisan_*.qm`

**Interfaces:**
- Consumes: completed behavior from Tasks 1–6.
- Produces: accurate user/developer documentation and generated localization catalogs.

- [ ] **Step 1: Update protocol documentation with exact reconnect behavior**

In `SANTOKER_X3_PREHEAT_PROTOCOL.md`, document:

- desired `0x7E`/`0x7F` intent survives automatic BLE/Wi-Fi/serial reconnect only during one monitoring session;
- restoration waits for a complete CRC/header/code/length/tail-valid frame;
- restoration sends target then ON and retries only on accepted frames at most once per second;
- OFF, CHARGE, or monitoring stop cancels restoration;
- diagnostics are read-only, bounded to 5,000 current-session events, and available at **Config → Device → Santoker → Diagnostics…**;
- writes are requests, not acknowledgements; and
- Android v26.7.7 behavior is based on static analysis and requires physical X3 verification.

Do not claim physical acknowledgement, report cadence, or firmware support that tests cannot establish.

- [ ] **Step 2: Add the release-history note**

Under v4.2.2 **FIXES**, add one bullet linking [fr3akX/artisan issue #6](https://github.com/fr3akX/artisan/issues/6), stating that Santoker warm-up intent is restored after automatic reconnect and a read-only protocol diagnostics window is available. End the bullet with: `Official-app reconnect behavior was established by static analysis; physical X3 verification is pending.`

- [ ] **Step 3: Run documentation checks**

```bash
git diff --check -- SANTOKER_X3_PREHEAT_PROTOCOL.md \
  docs/superpowers/specs/2026-08-13-santoker-reconnect-diagnostics-design.md \
  wiki/ReleaseHistory.md
cd src
.venv/bin/codespell ../SANTOKER_X3_PREHEAT_PROTOCOL.md ../wiki/ReleaseHistory.md \
  ../docs/superpowers/specs/2026-08-13-santoker-reconnect-diagnostics-design.md \
  ../docs/superpowers/plans/2026-08-13-santoker-reconnect-diagnostics.md
```

Expected: no whitespace or spelling findings.

After Tasks 1–6 and their focused checks pass, change the approved design status to `**Status:** Implemented`.

- [ ] **Step 4: Generate translations from source strings**

```bash
cd src
./build-derived.sh
```

Expected: exit 0 and generated catalogs include the new diagnostics title, section labels, buttons, no-session/unavailable text, discarded marker wording, and save-error text. If `pyuic6`, `pylupdate6`, or `lrelease` is unavailable, record the missing executable and leave generated outputs untouched.

- [ ] **Step 5: Review and narrow generated changes**

```bash
git status --short
git diff --stat
git diff -- src/translations
```

Retain only source locations/messages introduced by this feature and deterministic `.qm` recompilation. Revert unrelated generated churn and do not modify UI/protobuf/help derivatives.

- [ ] **Step 6: Commit documentation and generated catalogs**

```bash
git add SANTOKER_X3_PREHEAT_PROTOCOL.md \
  docs/superpowers/specs/2026-08-13-santoker-reconnect-diagnostics-design.md \
  wiki/ReleaseHistory.md src/translations
git commit -m "Document Santoker reconnect diagnostics"
```

If translation tooling was unavailable and no translation files changed, omit `src/translations` from `git add` and report the limitation.

---

### Task 8: Complete branch-level verification and review

**Files:**
- Verify all files changed since design commit `4f21ccbcb`.
- Modify only files needed to fix findings, with a failing test first for behavioral corrections.

**Interfaces:**
- Consumes: complete reconnect/diagnostics feature.
- Produces: fresh evidence, compatibility review, and a clean reviewable branch.

- [ ] **Step 1: Run all focused tests together in both import orders**

```bash
cd src
.venv/bin/pytest test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_warmup_ui.py \
  test/unitary/artisanlib/test_santoker_diagnostics_ui.py -v
.venv/bin/pytest test/unitary/artisanlib/test_santoker_diagnostics_ui.py \
  test/unitary/artisanlib/test_santoker_warmup_ui.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker.py -v
```

Expected: both commands pass with identical counts and no module-mock leakage or Qt crash.

- [ ] **Step 2: Run smoke and static analysis**

```bash
cd src
.venv/bin/pytest test/smoke/artisanlib/test_main_smoke.py -v
.venv/bin/ruff check artisanlib/santoker.py artisanlib/santoker_warmup.py \
  artisanlib/santoker_diagnostics.py artisanlib/santoker_diagnostics_ui.py \
  artisanlib/main.py artisanlib/canvas.py artisanlib/devices.py \
  test/unitary/artisanlib/test_santoker.py \
  test/unitary/artisanlib/test_santoker_warmup.py \
  test/unitary/artisanlib/test_santoker_diagnostics.py \
  test/unitary/artisanlib/test_santoker_diagnostics_ui.py
.venv/bin/mypy
. .venv/bin/activate
pyright
codespell
```

Expected: all commands exit 0. Record exact environment errors for unavailable Qt platform libraries rather than weakening checks.

- [ ] **Step 3: Run the complete configured test suite**

```bash
cd src
. .venv/bin/activate
pytest
```

Expected: no new Santoker, diagnostics, reconnect, UI, or isolation failures. Compare any existing unrelated baseline failures by exact test name and output; do not call the full suite green if failures remain.

- [ ] **Step 4: Run repository hygiene hooks**

From the repository root:

```bash
git diff --check
mapfile -t files < <(git diff --name-only 4f21ccbcb..HEAD)
. src/.venv/bin/activate
pre-commit run --files "${files[@]}"
git status --short
```

Expected: diff check and feature-scoped hooks pass. `.pi-subagents/` is a pre-existing untracked harness artifact and must not be staged.

- [ ] **Step 5: Review every acceptance and safety invariant**

```bash
git diff 4f21ccbcb..HEAD --stat
git diff 4f21ccbcb..HEAD -- src/artisanlib/santoker.py
git diff 4f21ccbcb..HEAD -- src/artisanlib/santoker_warmup.py
git diff 4f21ccbcb..HEAD -- src/artisanlib/santoker_diagnostics.py
git diff 4f21ccbcb..HEAD -- src/artisanlib/santoker_diagnostics_ui.py
git diff 4f21ccbcb..HEAD -- src/artisanlib/main.py src/artisanlib/canvas.py src/artisanlib/devices.py
```

Confirm all of these from code and tests:

- transport connect alone never sends restoration;
- first valid post-reconnect frame can send exactly target then ON;
- retries are accepted-frame-driven and monotonic-throttled to one second;
- desired and reported state/target are never conflated;
- OFF works locally while disconnected;
- CHARGE and monitoring stop clear desired ON, while RESET does not restore it;
- all restoration sends occur in main-thread slots;
- raw command syntax/encoding and Santoker presets are unchanged;
- diagnostics recorder failures cannot block valid RX/TX;
- history is bounded to 5,000 with an exact discard count;
- dialog contains no command controls;
- export contains no address, host/IP, credential, account, path, profile, or unrelated-log data;
- no target `0x7A` send was added; and
- no test or validation contacted hardware/network/cloud.

- [ ] **Step 6: Request whole-change review**

Use the requesting-code-review skill against design commit `4f21ccbcb` and the approved design. Treat unresolved restoration safety, thread-affinity, command-flood, privacy, parser-compatibility, or unbounded-memory findings as blocking. Fix each behavioral finding with a focused RED test, rerun the narrowest affected checks, then repeat Steps 1–5.

- [ ] **Step 7: Commit verification fixes only when needed**

```bash
git add -p
git diff --cached --check
git commit -m "Fix Santoker reconnect verification findings"
```

Do not create an empty commit. Report every passing command, every environment-limited command, generated translation status, residual physical-X3 risk, and final `git status --short` output.
