#
# ABOUT
# Thread-safe Roast Server cache protection ownership registry
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# OpenAI, 2026

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Final, override

from artisanlib.roastserver.contract import Namespace, ServerProfileSource

_UNSET: Final[object] = object()


@dataclass(frozen=True, slots=True)
class ProtectionToken:
    """Secret-free identity for one synchronously protected cache entry."""

    serial: int
    namespace: Namespace
    path: Path
    source: ServerProfileSource | None = None


class ProtectionRegistry:
    """Linearizable ownership shared by the UI controller and cache worker."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._serial = 0
        self._current: ProtectionToken | None = None

    @override
    def __repr__(self) -> str:
        return '<ProtectionRegistry>'

    def current(self) -> ProtectionToken | None:
        with self._lock:
            return self._current

    def paths(self, namespace: Namespace) -> frozenset[Path]:
        with self.read_guard(namespace) as paths:
            return paths

    @contextmanager
    def read_guard(self, namespace: Namespace) -> Iterator[frozenset[Path]]:
        """Hold ownership stable while a cache operation uses its paths."""
        with self._lock:
            current = self._current
            if current is None or current.namespace != namespace:
                yield frozenset()
            else:
                yield frozenset({current.path})

    @contextmanager
    def transaction_guard(
        self, expected: ProtectionToken | None
    ) -> Iterator[None]:
        """Prevent pruning while an exact-owner transaction is in flight."""
        with self._lock:
            if self._current is not expected:
                raise RuntimeError('cache protection ownership changed')
            yield

    def protect(
        self,
        namespace: Namespace,
        path: Path,
        source: ServerProfileSource | None = None,
        *,
        expected: ProtectionToken | None | object = _UNSET,
    ) -> ProtectionToken:
        canonical_path = _absolute_path(path)
        with self._lock:
            if expected is not _UNSET and self._current is not expected:
                raise RuntimeError('cache protection ownership changed')
            self._serial += 1
            token = ProtectionToken(
                self._serial, namespace, canonical_path, source)
            self._current = token
            return token

    def release(self, expected: ProtectionToken) -> bool:
        with self._lock:
            if self._current is not expected:
                return False
            self._current = None
            return True

    def restore(
        self,
        token: ProtectionToken | None,
        *,
        expected: ProtectionToken | None,
    ) -> bool:
        """Restore the exact token only if ownership still matches expected."""
        with self._lock:
            if self._current is not expected:
                return False
            self._current = token
            return True


def _absolute_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError('cache protection path is invalid')
    return Path(os.path.abspath(os.fspath(path)))
