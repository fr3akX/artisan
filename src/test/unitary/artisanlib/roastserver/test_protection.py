#
# ABOUT
# Tests for synchronous Roast Server cache protection ownership.
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

from __future__ import annotations

from pathlib import Path
import threading
from uuid import UUID

from artisanlib.roastserver.contract import Namespace
from artisanlib.roastserver.protection import ProtectionRegistry


NAMESPACE = Namespace(
    origin='https://archive.example.test',
    organization_id=UUID('11111111-1111-4111-8111-111111111111'),
    key='archive-example-test--11111111111141118111111111111111',
)


def test_registry_replaces_and_restores_the_exact_previous_token(
    tmp_path: Path,
) -> None:
    registry = ProtectionRegistry()
    first_path = tmp_path / 'first.alog'
    second_path = tmp_path / 'second.alog'

    first = registry.protect(NAMESPACE, first_path)
    second = registry.protect(NAMESPACE, second_path, expected=first)

    assert registry.current() is second
    assert registry.paths(NAMESPACE) == frozenset({second_path.absolute()})
    assert registry.restore(first, expected=second)
    assert registry.current() is first
    assert registry.paths(NAMESPACE) == frozenset({first_path.absolute()})
    assert not registry.restore(second, expected=second)


def test_registry_release_is_identity_checked_and_thread_safe(tmp_path: Path) -> None:
    registry = ProtectionRegistry()
    path = tmp_path / 'protected.alog'
    token = registry.protect(NAMESPACE, path)
    observations: list[frozenset[Path]] = []

    def observe() -> None:
        for _ in range(100):
            observations.append(registry.paths(NAMESPACE))

    threads = [threading.Thread(target=observe) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert registry.release(token)
    for thread in threads:
        thread.join()

    assert registry.current() is None
    assert observations
    assert set(observations) <= {frozenset({path.absolute()}), frozenset()}
    assert not registry.release(token)
    assert 'secret' not in repr(registry).lower()
