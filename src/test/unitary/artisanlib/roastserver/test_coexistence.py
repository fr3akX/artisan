#
# ABOUT
# Tests that the Artisan Roast Server connector coexists with artisan.plus.
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

import ast
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


_CONTROLLER_ISOLATION_SCRIPT = r'''
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock
from uuid import UUID

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

app = QCoreApplication.instance() or QCoreApplication([])
app.setApplicationName('ArtisanCoexistenceTest')
app.setOrganizationName('artisan-scope')
app.artisanviewerMode = False
order = sys.argv[3]
module_names = (
    'plus.config', 'plus.controller', 'plus.register', 'plus.sync',
    'artisanlib.roastserver.controller',
)
if order == 'connector-first':
    module_names = tuple(reversed(module_names))
for module_name in module_names:
    importlib.import_module(module_name)

import plus.config
import plus.controller
import plus.register
import plus.sync
from artisanlib.roastserver.controller import RoastServerController
from artisanlib.roastserver.settings import (
    DEFAULT_CACHE_LIMIT_BYTES,
    DEFAULT_ORIGIN,
    ConnectorSettings,
)


class CoexistenceWorker(QObject):
    connectionTested = pyqtSignal(str, object)
    credentialCommitted = pyqtSignal(str, object)
    connectionActivated = pyqtSignal(str, object)
    connectionRollbackFinished = pyqtSignal(str, bool)
    pendingConnectionRecoveryRequired = pyqtSignal(str, object)
    configurationValidated = pyqtSignal(object)
    credentialRemoved = pyqtSignal(str)
    operationFailed = pyqtSignal(str, object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    archivePageReady = pyqtSignal(str, object)
    browseFinished = pyqtSignal(str)
    downloadStaged = pyqtSignal(str, object)
    cachedReady = pyqtSignal(str, object)
    cachedFallbackReady = pyqtSignal(str, object)
    cachePublished = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    stopped = pyqtSignal()

    def no_op(self, *_args: object) -> None:
        pass

    start = no_op
    configure = no_op
    test_connection = no_op
    commit_connection = no_op
    finalize_connection = no_op
    acknowledge_connection_activation = no_op
    rollback_connection = no_op
    cancel_connection_transaction = no_op
    remove_credential = no_op
    enqueue_saved = no_op
    refresh = no_op
    retry_job = no_op
    remove_job = no_op
    browse = no_op
    open_online = no_op
    open_cached = no_op
    publish_staged = no_op
    discard_staged = no_op
    update_protected_paths = no_op
    clear_unused = no_op
    stop = no_op


class SentinelSettingsStore:
    def __init__(self, settings: ConnectorSettings) -> None:
        self.settings = settings
        self.load_calls = 0

    def load(self) -> ConnectorSettings:
        self.load_calls += 1
        return self.settings


tmp_path = Path(sys.argv[1])
connected = sys.argv[2] == 'connected'
plus_status_action = Mock()
plus_uuid_register = Mock()
plus_uuid_lookup = Mock(return_value='existing-plus.alog')
plus_sync = Mock()
plus_sync_hash = Mock(return_value='existing-sync-hash')
plus_connected_before = connected
plus_token_before = 'existing-plus-session-token' if connected else None
plus_app_window_before = object()
plus.config.connected = plus_connected_before
plus.config.token = plus_token_before
plus.config.app_window = plus_app_window_before
plus.config.status_action = plus_status_action
plus.register.addPath = plus_uuid_register
plus.register.getPath = plus_uuid_lookup
plus.sync.sync = plus_sync
plus.controller.updateSyncRecordHashAndSync = plus_sync_hash
plus_modules_before = {
    name: module for name, module in sys.modules.items()
    if name == 'plus' or name.startswith('plus.')
}

settings_value = ConnectorSettings(
    origin=DEFAULT_ORIGIN,
    enabled=False,
    automatic_upload=False,
    client_instance_uuid=UUID('11111111-1111-4111-8111-111111111111'),
    identity=None,
    cache_limit_bytes=DEFAULT_CACHE_LIMIT_BYTES,
    configuration_geometry=None,
    browser_geometry=None,
    pending_connection=None,
)
settings_store = SentinelSettingsStore(settings_value)
connector_credentials = object()
connector_outbox = object()
connector_cache = object()
connector_worker = CoexistenceWorker()
observed: dict[str, object] = {}
connector_root = tmp_path / 'artisan-data' / 'roastserver'


def outbox_factory(path: Path, _clock: object) -> object:
    observed['outbox_root'] = path
    return connector_outbox


def cache_factory(path: Path) -> object:
    observed['cache_root'] = path
    return connector_cache


def worker_factory(**kwargs: object) -> CoexistenceWorker:
    observed.update(kwargs)
    observed['worker'] = connector_worker
    return connector_worker


controller = RoastServerController(
    settings=settings_store,
    credentials=connector_credentials,
    data_root=connector_root,
    client_factory=lambda *_args: None,
    profile_validator=lambda _path: None,
    outbox_factory=outbox_factory,
    cache_factory=cache_factory,
    worker_factory=worker_factory,
)

assert settings_store.settings is settings_value
assert settings_store.load_calls == 1
assert observed['outbox'] is connector_outbox
assert observed['cache'] is connector_cache
assert observed['credentials'] is connector_credentials
assert observed['worker'] is connector_worker
plus_status_action.setEnabled.assert_not_called()
plus_status_action.setIcon.assert_not_called()
plus_uuid_register.assert_not_called()
plus_uuid_lookup.assert_not_called()
plus_sync.assert_not_called()
plus_sync_hash.assert_not_called()
assert plus.config.connected is plus_connected_before
assert plus.config.token == plus_token_before
assert plus.config.app_window is plus_app_window_before
assert plus.config.status_action is plus_status_action
assert all(sys.modules.get(name) is module for name, module in plus_modules_before.items())

plus_root = tmp_path / 'artisan-plus'
plus_paths = {
    plus_root / plus.config.outbox_cache,
    plus_root / plus.config.uuid_cache,
    plus_root / plus.config.sync_cache,
}
assert observed['outbox_root'] == connector_root / 'outbox'
assert observed['cache_root'] == connector_root / 'cache'
assert connector_root not in plus_paths
assert observed['outbox_root'] not in plus_paths
assert observed['cache_root'] not in plus_paths
assert controller.shutdown()
print(json.dumps({
    'connected': plus.config.connected,
    'token': plus.config.token,
    'plus_modules': sorted(plus_modules_before),
    'outbox_root': str(observed['outbox_root'].relative_to(tmp_path)),
    'cache_root': str(observed['cache_root'].relative_to(tmp_path)),
    'register_calls': plus_uuid_register.call_count,
    'sync_calls': plus_sync.call_count,
}, sort_keys=True))
'''


def test_connector_production_package_has_no_plus_imports() -> None:
    package_root = Path('artisanlib/roastserver')
    violations: list[str] = []
    for source_path in sorted(package_root.rglob('*.py')):
        tree = ast.parse(source_path.read_text(encoding='utf-8'), source_path.as_posix())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == 'plus' or alias.name.startswith('plus.')
                for alias in node.names
            ):
                violations.append(f'{source_path}:{node.lineno}')
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and (node.module == 'plus' or node.module.startswith('plus.'))
            ):
                violations.append(f'{source_path}:{node.lineno}')
    assert violations == []


@pytest.mark.parametrize('connected', [False, True])
def test_controller_construction_preserves_actual_plus_sentinels_in_both_orders(
    tmp_path: Path,
    connected: bool,
) -> None:
    environment = os.environ.copy()
    environment['QT_QPA_PLATFORM'] = 'offscreen'
    fingerprints: list[str] = []
    for order in ('connector-first', 'plus-first'):
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                textwrap.dedent(_CONTROLLER_ISOLATION_SCRIPT),
                str(tmp_path / order),
                'connected' if connected else 'disconnected',
                order,
            ],
            cwd=Path.cwd(),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        fingerprints.append(result.stdout.strip().splitlines()[-1])
    assert fingerprints[0] == fingerprints[1]


def test_known_plus_node_and_connector_nodes_pass_in_fresh_subprocess_both_orders() -> None:
    environment = os.environ.copy()
    environment['QT_QPA_PLATFORM'] = 'offscreen'
    plus_node = 'test/unitary/plus/test_sync.py::TestAddSync::test_add_sync_successful'
    connector_node = 'test/unitary/artisanlib/roastserver/test_protection.py'
    summaries: list[str] = []
    for nodes in ((plus_node, connector_node), (connector_node, plus_node)):
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', '-q', *nodes],
            cwd=Path.cwd(),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        summaries.append(result.stdout.strip().splitlines()[-1])
    assert summaries[0].split(' in ')[0] == summaries[1].split(' in ')[0]
