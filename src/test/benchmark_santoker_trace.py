#
# ABOUT
# Synthetic local recorder admission benchmark; no hardware or network.
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Run from src: python test/benchmark_santoker_trace.py (PYTHONPATH=.).

Latency and tracemalloc are separate runs. This qualifies neither Raspberry Pi
nor full Artisan/parser/control behavior. Temporary stores are owned by this run.
"""
from __future__ import annotations

import json
from pathlib import Path
import platform
import statistics
import tempfile
import threading
import time
import tracemalloc
from uuid import uuid4

from artisanlib.santoker_trace import CaptureConfig, TraceRecorder
from artisanlib.santoker_trace_contract import RX_UUID, validate_trace
from artisanlib.santoker_trace_store import TraceStore

CONFIG = CaptureConfig('4.0.0', 'linux', '6.1', 'x86_64')
ATTEMPTS = 20000


def burst(*, memory: bool) -> dict[str, int | float]:
    release = threading.Event()
    with tempfile.TemporaryDirectory(prefix='artisan-trace-benchmark-') as directory:
        root = Path(directory)

        def factory() -> TraceStore:
            if not release.wait(30):
                raise RuntimeError('benchmark stalled')
            return TraceStore(root)

        recorder = TraceRecorder(root, store_factory=factory)
        handle = recorder.start(CONFIG)
        fields = {'connection_id': str(uuid4()), 'direction': 'rx', 'characteristic': RX_UUID}
        payload = bytes(range(20))
        timings: list[int] = []
        accepted_timings: list[int] = []
        if memory:
            tracemalloc.start()
        try:
            for _ in range(ATTEMPTS):
                if memory:
                    handle.emit('rx', fields, payload=payload)
                else:
                    start = time.perf_counter_ns()
                    accepted = handle.emit('rx', fields, payload=payload)
                    elapsed = time.perf_counter_ns() - start
                    timings.append(elapsed)
                    if accepted:
                        accepted_timings.append(elapsed)
            snapshot = handle.status()
            result: dict[str, int | float] = {
                'attempts': ATTEMPTS, 'admitted': snapshot.queued_events,
                'rejected': snapshot.dropped_events, 'charged_bytes': snapshot.queued_bytes,
            }
            if memory:
                current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                result.update(traced_current_bytes=current, traced_peak_bytes=peak)
            else:
                timings.sort()
                accepted_timings.sort()
                result.update(p50_us=statistics.median(timings) / 1000,
                              p99_us=timings[int(len(timings) * 0.99)] / 1000,
                              maximum_us=max(timings) / 1000,
                              admitted_p50_us=statistics.median(accepted_timings) / 1000,
                              admitted_p99_us=accepted_timings[int(len(accepted_timings) * 0.99)] / 1000)
            handle.cleanup_finished()
            drain_start = time.perf_counter()
            release.set()
            if not recorder.shutdown(wait=True, timeout=30):
                raise RuntimeError('benchmark writer did not finish')
            if handle.status().state != 'sealed':
                raise RuntimeError('benchmark trace failed')
            with (root / f'{handle.session_id}.gz').open('rb') as source:
                validated = validate_trace(source)
            result['drain_ms'] = (time.perf_counter() - drain_start) * 1000
            result['artifact_bytes'] = validated.byte_size
            return result
        finally:
            release.set()
            recorder.shutdown(wait=True, timeout=30)
            if tracemalloc.is_tracing():
                tracemalloc.stop()


if __name__ == '__main__':
    print(json.dumps({'python': platform.python_version(), 'system': platform.system(),
                      'architecture': platform.machine(), 'stalled_latency': burst(memory=False),
                      'stalled_memory': burst(memory=True)}, indent=2))
