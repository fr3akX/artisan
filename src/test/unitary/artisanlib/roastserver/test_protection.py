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
import time
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


def test_registry_read_guard_linearizes_prune_with_protect_and_release(
    tmp_path: Path,
) -> None:
    registry = ProtectionRegistry()
    first_path = tmp_path / 'first.alog'
    second_path = tmp_path / 'second.alog'
    first = registry.protect(NAMESPACE, first_path)
    guard_entered = threading.Event()
    allow_guard_exit = threading.Event()
    transitions_finished = threading.Event()
    guarded_paths: list[frozenset[Path]] = []

    def guarded_prune() -> None:
        with registry.read_guard(NAMESPACE) as paths:
            guarded_paths.append(paths)
            guard_entered.set()
            assert allow_guard_exit.wait(2)

    def transition() -> None:
        second = registry.protect(
            NAMESPACE, second_path, expected=first)
        assert registry.release(second)
        transitions_finished.set()

    prune_thread = threading.Thread(target=guarded_prune)
    transition_thread = threading.Thread(target=transition)
    prune_thread.start()
    assert guard_entered.wait(2)
    transition_thread.start()
    time.sleep(0.02)
    assert not transitions_finished.is_set()
    allow_guard_exit.set()
    prune_thread.join(2)
    transition_thread.join(2)

    assert guarded_paths == [frozenset({first_path.absolute()})]
    assert transitions_finished.is_set()
    assert registry.current() is None


def test_registry_transaction_guard_blocks_prune_until_rollback_is_complete(
    tmp_path: Path,
) -> None:
    registry = ProtectionRegistry()
    path = tmp_path / 'protected.alog'
    token = registry.protect(NAMESPACE, path)
    prune_entered = threading.Event()

    def guarded_prune() -> None:
        with registry.read_guard(NAMESPACE) as paths:
            assert paths == frozenset({path.absolute()})
            prune_entered.set()

    with registry.transaction_guard(token):
        prune_thread = threading.Thread(target=guarded_prune)
        prune_thread.start()
        assert not prune_entered.wait(0.02)
        assert registry.release(token)
        assert registry.restore(token, expected=None)
        assert not prune_entered.is_set()

    prune_thread.join(2)
    assert prune_entered.is_set()
    assert registry.current() is token


def test_registry_reentrant_cas_never_releases_or_restores_wrong_token(
    tmp_path: Path,
) -> None:
    registry = ProtectionRegistry()
    first = registry.protect(NAMESPACE, tmp_path / 'first.alog')
    second = registry.protect(
        NAMESPACE, tmp_path / 'second.alog', expected=first)

    assert not registry.release(first)
    assert not registry.restore(first, expected=None)
    assert registry.current() is second
