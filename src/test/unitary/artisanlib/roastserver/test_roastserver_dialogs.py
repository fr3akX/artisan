#
# ABOUT
# Tests for the Artisan Roast Server configuration dialog
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

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import replace
from datetime import UTC, datetime, time as datetime_time
import hashlib
from pathlib import Path
import secrets
import threading
import time
from typing import cast, override
from uuid import UUID

import pytest
from PyQt6.QtCore import QByteArray, QDate, QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton, QWidget

from artisanlib.roastserver.api import ClientFactory
from artisanlib.roastserver.cache import CacheStats, CachedRevision
from artisanlib.roastserver.contract import (
    FailureKind,
    ArchiveFilters,
    IdentityOrganization,
    IdentityUser,
    LabelSummary,
    PublicFailure,
    Revision,
    RoastState,
    RoastSummary,
    ServerIdentity,
    ServerProfileSource,
)
from artisanlib.roastserver.controller import RoastServerController
from artisanlib.roastserver.dialogs import (
    ArchivePageView,
    ArchiveRow,
    FailedJobsModel,
    RoastServerBrowserDialog,
    RoastServerConfigDialog,
    RoastTableModel,
)
from artisanlib.roastserver.inventory_store import (
    FailedInventoryCommand,
    InterruptedReservation,
    InventoryQueueCounts,
)
from artisanlib.roastserver.outbox import FailedJob, QueueCounts
from artisanlib.roastserver.settings import (
    KEYRING_FAILURE_MESSAGE,
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    ConnectorSettings,
    CredentialStoreError,
    SettingsStore,
    namespace_for,
)

ORIGIN = 'https://old.example.test'
NEW_ORIGIN = 'https://example.test'
IDENTITY = ServerIdentity(
    user=IdentityUser(
        id=UUID('11111111-1111-4111-8111-111111111111'),
        email='owner@example.test',
        nickname='Owner',
    ),
    organization=IdentityOrganization(
        id=UUID('22222222-2222-4222-8222-222222222222'),
        name='Roastery',
        slug='roastery',
    ),
    role='admin',
)
FAILED_JOB = FailedJob(
    id='a' * 32,
    roast_uuid=UUID('33333333-3333-4333-8333-333333333333'),
    sha256='b' * 64,
    attempts=3,
    next_attempt_at=datetime(2026, 8, 1, 12, 30, tzinfo=UTC),
    error_code='profile_rejected',
    error_message='<b>Rejected & unsafe-looking</b>',
    updated_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
)
INVENTORY_NAMESPACE = namespace_for(ORIGIN, IDENTITY.organization.id)
FAILED_INVENTORY = FailedInventoryCommand(
    id='c' * 32,
    namespace=INVENTORY_NAMESPACE,
    roast_uuid=UUID('55555555-5555-4555-8555-555555555555'),
    lot_id=UUID('66666666-6666-4666-8666-666666666666'),
    reservation_uuid=UUID('77777777-7777-4777-8777-777777777777'),
    operation='reserve',
    attempts=2,
    error_code='invalid_inventory_transition',
    error_message='<b>Inventory rejected</b>',
    updated_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
)
INTERRUPTED = InterruptedReservation(
    namespace=INVENTORY_NAMESPACE,
    roast_uuid=FAILED_INVENTORY.roast_uuid,
    lot_id=FAILED_INVENTORY.lot_id,
    lot_name='Interrupted lot',
    reservation_uuid=FAILED_INVENTORY.reservation_uuid,
    planned_grams=1_000,
    lifecycle='reserved',
    updated_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
)
SAFE_FAILURE = PublicFailure(
    kind=FailureKind.OFFLINE,
    code='offline',
    message='<a href="https://unsafe.test">Offline</a>',
    retryable=True,
)


def assert_secret_absent(secret: str, value: object) -> None:
    rendered = value if isinstance(value, str) else repr(value)
    if secret in rendered:
        pytest.fail('runtime secret exposed by dialog boundary', pytrace=False)


class BlockingDialogClient:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def __enter__(self) -> BlockingDialogClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError('blocked dialog authentication timed out')
        self.completed.set()
        return IDENTITY


class WritableCredentialStore:
    def __init__(self, origin: str, credential: str | None) -> None:
        self.values: dict[str, str] = {}
        if credential is not None:
            self.values[origin] = credential

    @override
    def __repr__(self) -> str:
        return '<WritableCredentialStore credentials=<redacted>>'

    def get(self, origin: str) -> str | None:
        return self.values.get(origin)

    def set(self, origin: str, credential: str) -> None:
        self.values[origin] = credential

    def delete(self, origin: str) -> None:
        self.values.pop(origin, None)


class StagedActivationClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._authentication_count = 0
        self.final_auth_entered = threading.Event()
        self.release_final_auth = threading.Event()

    def __enter__(self) -> StagedActivationClient:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def test_connection(self) -> ServerIdentity:
        with self._lock:
            self._authentication_count += 1
            final_auth = self._authentication_count == 2
        if final_auth:
            self.final_auth_entered.set()
            if not self.release_final_auth.wait(timeout=5):
                raise RuntimeError('blocked final authentication timed out')
        return IDENTITY


class RejectingCredentialStore:
    def __init__(self) -> None:
        self.set_calls = 0
        self.delete_calls = 0

    @override
    def __repr__(self) -> str:
        return '<RejectingCredentialStore credential=<redacted>>'

    def get(self, origin: str) -> str | None:
        del origin
        return None

    def set(self, origin: str, credential: str) -> None:
        del origin, credential
        self.set_calls += 1
        raise CredentialStoreError

    def delete(self, origin: str) -> None:
        del origin
        self.delete_calls += 1


class FakeController(QObject):
    settingsChanged = pyqtSignal(object)
    identityChanged = pyqtSignal(object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    inventoryQueueChanged = pyqtSignal(object)
    inventoryFailedChanged = pyqtSignal(object)
    inventoryRecoveryRequired = pyqtSignal(object)
    inventoryRefreshFinished = pyqtSignal(str)
    cacheStatsChanged = pyqtSignal(object)
    operationFailed = pyqtSignal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.test_origins: list[str] = []
        self.candidate_digests: list[str] = []
        self.apply_calls: list[tuple[str, bool, bool, int]] = []
        self.retry_calls: list[str] = []
        self.remove_calls: list[str] = []
        self.inventory_retry_calls: list[str] = []
        self.inventory_namespace = INVENTORY_NAMESPACE
        self.inventory_enabled = True
        self.refresh_calls = 0
        self.clear_calls = 0
        self.remove_credential_calls = 0
        self.invalidate_calls = 0
        self.cancel_calls: list[str] = []
        self.saved_geometries: list[bytes] = []

    def test_connection(self, origin: str, candidate: str) -> str:
        self.test_origins.append(origin)
        self.candidate_digests.append(hashlib.sha256(candidate.encode()).hexdigest())
        return 'opaque-test-id'

    def apply_options(
        self,
        origin: str,
        enabled: bool,
        automatic_upload: bool,
        cache_limit_bytes: int,
    ) -> None:
        self.apply_calls.append((origin, enabled, automatic_upload, cache_limit_bytes))

    def invalidate_connection_proof(self) -> None:
        self.invalidate_calls += 1

    def cancel_connection_test(self, request_id: str) -> None:
        self.cancel_calls.append(request_id)

    def remove_credential(self) -> None:
        self.remove_credential_calls += 1

    def refresh_queue(self) -> None:
        self.refresh_calls += 1

    def retry_job(self, job_id: str) -> None:
        self.retry_calls.append(job_id)

    def remove_job(self, job_id: str) -> None:
        self.remove_calls.append(job_id)

    def retry_inventory_command(self, command_id: str) -> None:
        self.inventory_retry_calls.append(command_id)

    def inventory_context(self) -> object:
        return type('Context', (), {
            'namespace': self.inventory_namespace,
            'enabled': self.inventory_enabled,
        })()

    def clear_unused_cache(self) -> None:
        self.clear_calls += 1

    def save_configuration_geometry(self, geometry: QByteArray) -> None:
        self.saved_geometries.append(bytes(geometry.data()))


@pytest.fixture(scope='module')
def qapp() -> QApplication:
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    return cast(QApplication, application)


@pytest.fixture
def settings() -> ConnectorSettings:
    return ConnectorSettings(
        origin=ORIGIN,
        enabled=False,
        automatic_upload=False,
        client_instance_uuid=UUID('44444444-4444-4444-8444-444444444444'),
        identity=None,
        cache_limit_bytes=512 * 1024 * 1024,
        configuration_geometry=None,
        browser_geometry=None,
        pending_connection=None,
    )


@pytest.fixture
def controller() -> FakeController:
    return FakeController()


@pytest.fixture
def dialog(
    qapp: QApplication,
    controller: FakeController,
    settings: ConnectorSettings,
) -> Generator[RoastServerConfigDialog]:
    value = RoastServerConfigDialog(controller, settings)
    value.show()
    qapp.processEvents()
    yield value
    value.hide()
    value.deleteLater()
    qapp.processEvents()


def activate(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
    *, automatic_upload: bool = False,
    enabled: bool = False,
) -> ConnectorSettings:
    active = replace(
        settings,
        origin=dialog.server_edit.text(),
        identity=IDENTITY,
        automatic_upload=automatic_upload,
        enabled=enabled,
    )
    controller.settingsChanged.emit(active)
    controller.identityChanged.emit(IDENTITY)
    QApplication.processEvents()
    return active


def test_config_credential_uses_password_echo_and_auto_upload_starts_disabled(
    dialog: RoastServerConfigDialog,
) -> None:
    assert dialog.credential_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.credential_edit.contextMenuPolicy() is Qt.ContextMenuPolicy.NoContextMenu
    assert not dialog.automatic_upload_check.isChecked()
    assert not dialog.automatic_upload_check.isEnabled()
    assert dialog.isModal() is False


def test_config_remote_http_origin_without_path_is_rejected_locally(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    dialog.server_edit.setText('http://remote.example.test')
    dialog.credential_edit.setText(secrets.token_urlsafe(32))
    dialog.test_button.click()

    assert controller.test_origins == []
    assert dialog.credential_edit.text() == ''
    assert dialog.error_label.text() == 'Enter a valid HTTPS origin.'
    assert dialog.error_label.textFormat() is Qt.TextFormat.PlainText
    assert not dialog.error_label.openExternalLinks()


@pytest.mark.parametrize(
    ('raw', 'canonical'),
    [
        ('http://LOCALHOST:80/', 'http://localhost'),
        ('http://127.0.0.1:8000/', 'http://127.0.0.1:8000'),
        ('http://[::1]:8000/', 'http://[::1]:8000'),
    ],
)
def test_config_loopback_http_origins_are_accepted_and_canonicalized(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    raw: str,
    canonical: str,
) -> None:
    dialog.server_edit.setText(raw)
    dialog.credential_edit.setText(secrets.token_urlsafe(32))

    dialog.test_button.click()

    assert controller.test_origins == [canonical]


def test_config_successful_test_uses_opaque_controller_flow_and_renders_identity(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
    qapp: QApplication,
) -> None:
    candidate = secrets.token_urlsafe(48)
    candidate_digest = hashlib.sha256(candidate.encode()).hexdigest()
    dialog.server_edit.setText('https://EXAMPLE.test:443/')
    dialog.credential_edit.setText(candidate)
    clicked = QSignalSpy(dialog.test_button.clicked)

    dialog.test_button.click()

    assert len(clicked) == 1
    assert controller.test_origins == [NEW_ORIGIN]
    assert controller.candidate_digests == [candidate_digest]
    assert dialog.credential_edit.text() == ''
    assert not dialog.automatic_upload_check.isEnabled()
    assert_secret_absent(candidate, dialog)
    assert_secret_absent(candidate, dialog.failed_model)
    assert_secret_absent(candidate, controller.__dict__)
    clipboard = qapp.clipboard()
    assert clipboard is not None
    assert_secret_absent(candidate, clipboard.text())

    active = replace(settings, origin=NEW_ORIGIN, identity=IDENTITY)
    controller.settingsChanged.emit(active)
    controller.identityChanged.emit(IDENTITY)
    qapp.processEvents()

    assert dialog.server_edit.text() == NEW_ORIGIN
    assert dialog.identity_label.text() == 'Owner — Roastery (admin)'
    assert dialog.automatic_upload_check.isEnabled()


def test_credential_copy_never_places_runtime_secret_on_clipboard(
    dialog: RoastServerConfigDialog,
    qapp: QApplication,
) -> None:
    candidate = secrets.token_urlsafe(40)
    clipboard = qapp.clipboard()
    assert clipboard is not None
    clipboard.clear()
    dialog.credential_edit.setText(candidate)
    dialog.credential_edit.selectAll()
    QTest.keyClick(  # type: ignore[call-overload]
        dialog.credential_edit,  # pyright: ignore[reportArgumentType]
        Qt.Key.Key_C,  # pyright: ignore[reportArgumentType]
        Qt.KeyboardModifier.ControlModifier,  # pyright: ignore[reportArgumentType]
    )
    qapp.processEvents()

    assert_secret_absent(candidate, clipboard.text())
    dialog.credential_edit.clear()


def test_config_edit_without_local_proof_always_invalidates_controller(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    dialog.server_edit.setText(NEW_ORIGIN)
    assert controller.invalidate_calls == 1

    dialog.credential_edit.setText(secrets.token_urlsafe(24))
    assert controller.invalidate_calls == 2


def test_config_any_origin_or_credential_edit_immediately_revokes_auto_proof(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    activate(dialog, controller, settings, automatic_upload=True, enabled=True)
    assert dialog.automatic_upload_check.isChecked()
    assert dialog.automatic_upload_check.isEnabled()

    dialog.server_edit.setText('https://candidate.example.test')

    assert not dialog.automatic_upload_check.isChecked()
    assert not dialog.automatic_upload_check.isEnabled()
    assert dialog.identity_label.text() == 'Not connected'
    assert controller.invalidate_calls == 1
    assert controller.apply_calls == []
    controller.settingsChanged.emit(
        replace(settings, enabled=True, automatic_upload=False, identity=IDENTITY)
    )
    assert dialog.server_edit.text() == 'https://candidate.example.test'

    dialog.server_edit.setText(ORIGIN)
    dialog.credential_edit.setText(secrets.token_urlsafe(24))
    dialog.test_button.click()
    activate(dialog, controller, settings, automatic_upload=True, enabled=True)
    dialog.credential_edit.setText(secrets.token_urlsafe(24))

    assert not dialog.automatic_upload_check.isEnabled()
    assert controller.invalidate_calls == 4
    assert controller.apply_calls == []


def test_config_edit_cancels_an_opaque_test_transaction_immediately(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    dialog.credential_edit.setText(secrets.token_urlsafe(24))
    dialog.test_button.click()
    assert controller.invalidate_calls == 1

    dialog.server_edit.setText('https://newer.example.test')

    assert controller.invalidate_calls == 2
    assert not dialog.automatic_upload_check.isEnabled()


def test_config_enable_and_automatic_upload_save_only_after_current_proof(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    dialog.enabled_check.click()
    assert controller.apply_calls == []
    assert not dialog.enabled_check.isChecked()

    activate(dialog, controller, settings)
    dialog.enabled_check.click()
    dialog.automatic_upload_check.click()

    assert controller.apply_calls == [
        (ORIGIN, True, False, 512 * 1024 * 1024),
        (ORIGIN, True, True, 512 * 1024 * 1024),
    ]


def test_config_failed_rows_counts_cache_and_plain_failure_survive_refresh_failure(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    resets = QSignalSpy(dialog.failed_model.modelReset)
    controller.queueChanged.emit(QueueCounts(2, 3, 4, 1, 9))
    controller.failedJobsChanged.emit((FAILED_JOB,))
    controller.cacheStatsChanged.emit(CacheStats(1_572_864, 2))
    controller.operationFailed.emit('queue', SAFE_FAILURE)

    assert len(resets) == 1
    assert dialog.pending_count_label.text() == '2'
    assert dialog.retrying_count_label.text() == '3'
    assert dialog.paused_count_label.text() == '4'
    assert dialog.failed_count_label.text() == '1'
    assert dialog.failed_model.rowCount() == 1
    assert dialog.cache_label.text() == '1.5 MiB (2 revisions)'
    assert dialog.error_label.text() == SAFE_FAILURE.message
    assert dialog.error_label.textFormat() is Qt.TextFormat.PlainText
    assert not dialog.error_label.openExternalLinks()


def test_failed_model_has_stable_safe_columns_and_values() -> None:
    model = FailedJobsModel()
    model.set_jobs((FAILED_JOB,))

    assert model.columnCount() == 7
    assert [
        model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for column in range(model.columnCount())
    ] == ['Roast UUID', 'Attempts', 'Next try', 'Category', 'Message', 'Retry', 'Remove']
    assert model.data(model.index(0, 0)) == str(FAILED_JOB.roast_uuid)
    assert model.data(model.index(0, 1)) == 3
    assert model.data(model.index(0, 2)) == '2026-08-01 12:30 UTC'
    assert model.data(model.index(0, 3)) == 'profile_rejected'
    assert model.data(model.index(0, 4)) == FAILED_JOB.error_message
    assert model.data(model.index(0, 5)) is None
    assert model.job_id(0) == FAILED_JOB.id
    assert model.flags(model.index(0, 0)) == (
        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    )


def test_inventory_queue_failed_retry_same_only_and_interrupted_aggregate(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    controller.inventoryQueueChanged.emit(InventoryQueueCounts(4, 3, 2, 1, 9))
    controller.inventoryFailedChanged.emit((FAILED_INVENTORY,))
    controller.inventoryRecoveryRequired.emit((INTERRUPTED,))

    assert dialog.inventory_pending_count_label.text() == '4'
    assert dialog.inventory_retrying_count_label.text() == '3'
    assert dialog.inventory_paused_count_label.text() == '2'
    assert dialog.inventory_failed_count_label.text() == '1'
    assert dialog.inventory_interrupted_count_label.text() == '1'
    assert dialog.inventory_failed_model.rowCount() == 1
    assert dialog.inventory_failed_view.accessibleName()
    assert dialog.inventory_failed_model.data(
        dialog.inventory_failed_model.index(0, 0),
        Qt.ItemDataRole.AccessibleTextRole,
    )
    retry = dialog.inventory_failed_view.indexWidget(
        dialog.inventory_failed_model.index(0, 6))
    assert isinstance(retry, QPushButton)
    assert 'same command' in retry.text().lower()
    retry.click()
    assert controller.inventory_retry_calls == [FAILED_INVENTORY.id]
    assert dialog.inventory_failed_model.columnCount() == 7
    assert all(
        'remove' not in str(dialog.inventory_failed_model.headerData(
            column, Qt.Orientation.Horizontal)).lower()
        for column in range(dialog.inventory_failed_model.columnCount())
    )


def test_inventory_old_namespace_retry_disabled_and_unsupported_is_inventory_only(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    controller.inventory_namespace = namespace_for(
        NEW_ORIGIN, UUID('88888888-8888-4888-8888-888888888888'))
    controller.inventoryFailedChanged.emit((FAILED_INVENTORY,))
    retry = dialog.inventory_failed_view.indexWidget(
        dialog.inventory_failed_model.index(0, 6))
    assert isinstance(retry, QPushButton)
    assert not retry.isEnabled()

    controller.operationFailed.emit(
        'inventory-operation',
        PublicFailure(
            FailureKind.INVENTORY_UNSUPPORTED,
            'inventory_unsupported',
            '<b>arbitrary server message</b>',
            False,
        ),
    )
    assert dialog.inventory_status_label.text() == 'Server does not support inventory.'
    assert dialog.inventory_status_label.textFormat() is Qt.TextFormat.PlainText
    assert dialog.error_label.text() == ''


def test_inventory_unsupported_status_clears_on_context_change_and_refresh_success(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    unsupported = PublicFailure(
        FailureKind.INVENTORY_UNSUPPORTED,
        'inventory_unsupported',
        'ignored unsafe server detail',
        False,
    )
    controller.operationFailed.emit('inventory-a', unsupported)
    assert dialog.inventory_status_label.text() == 'Server does not support inventory.'

    new_identity = replace(
        IDENTITY,
        organization=replace(
            IDENTITY.organization,
            id=UUID('88888888-8888-4888-8888-888888888888'),
        ),
    )
    new_settings = replace(settings, origin=NEW_ORIGIN, identity=new_identity)
    controller.inventory_namespace = namespace_for(
        NEW_ORIGIN, new_identity.organization.id
    )
    controller.settingsChanged.emit(new_settings)

    assert dialog.inventory_status_label.text() == ''

    controller.operationFailed.emit('inventory-b', unsupported)
    assert dialog.inventory_status_label.text() == 'Server does not support inventory.'
    controller.inventoryRefreshFinished.emit('refresh-b')
    assert dialog.inventory_status_label.text() == ''


def test_failed_table_per_row_retry_remove_and_refresh_are_controller_only(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    qapp: QApplication,
) -> None:
    controller.failedJobsChanged.emit((FAILED_JOB,))
    qapp.processEvents()

    retry = dialog.failed_view.indexWidget(dialog.failed_model.index(0, 5))
    remove = dialog.failed_view.indexWidget(dialog.failed_model.index(0, 6))
    assert isinstance(retry, QPushButton)
    assert isinstance(remove, QPushButton)
    assert retry.accessibleName() == f'Retry {FAILED_JOB.roast_uuid}'
    assert remove.accessibleName() == f'Remove {FAILED_JOB.roast_uuid}'

    retry.click()
    remove.click()
    dialog.refresh_button.click()

    assert controller.retry_calls == [FAILED_JOB.id]
    assert controller.remove_calls == [FAILED_JOB.id]
    assert controller.refresh_calls == 1
    assert dialog.failed_model.rowCount() == 1


def test_keyring_failure_uses_fixed_translated_action_text(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    controller.operationFailed.emit(
        'connection',
        PublicFailure(FailureKind.KEYRING, 'keyring', 'unsafe backend details', False),
    )
    assert dialog.error_label.text() == KEYRING_FAILURE_MESSAGE


def test_disable_and_remove_credential_retain_queue_and_cache_data(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    activate(dialog, controller, settings, enabled=True)
    controller.queueChanged.emit(QueueCounts(2, 0, 1, 1, 0))
    controller.failedJobsChanged.emit((FAILED_JOB,))
    controller.cacheStatsChanged.emit(CacheStats(100, 1))

    dialog.enabled_check.click()
    dialog.remove_credential_button.click()

    assert controller.apply_calls[-1] == (ORIGIN, False, False, 512 * 1024 * 1024)
    assert controller.remove_credential_calls == 1
    assert dialog.failed_model.rowCount() == 1
    assert dialog.pending_count_label.text() == '2'
    assert dialog.cache_label.text() == '100 bytes (1 revision)'
    assert not dialog.automatic_upload_check.isEnabled()


def test_cache_limit_is_bounded_and_clear_requires_active_configuration(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    assert dialog.cache_limit_spin.minimum() == MIN_CACHE_LIMIT_BYTES // (1024 * 1024)
    assert dialog.cache_limit_spin.maximum() == MAX_CACHE_LIMIT_BYTES // (1024 * 1024)
    assert not dialog.clear_cache_button.isEnabled()

    activate(dialog, controller, settings, enabled=True)
    assert dialog.clear_cache_button.isEnabled()
    dialog.cache_limit_spin.setValue(1024)
    dialog.cache_limit_spin.editingFinished.emit()
    dialog.clear_cache_button.click()

    assert controller.apply_calls[-1] == (ORIGIN, True, False, 1024 * 1024 * 1024)
    assert controller.clear_calls == 1


def test_accessible_labels_exact_focus_order_default_and_close_reachability(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    activate(dialog, controller, settings)
    assert dialog.server_label.buddy() is dialog.server_edit
    assert dialog.credential_label.buddy() is dialog.credential_edit
    assert dialog.cache_limit_label.buddy() is dialog.cache_limit_spin
    assert dialog.server_edit.accessibleName() == 'Server origin'
    assert dialog.credential_edit.accessibleName() == 'Credential'
    assert dialog.failed_view.accessibleName() == 'Failed uploads'
    assert dialog.test_button.isDefault()
    assert dialog.test_button.autoDefault()

    close_button = dialog.button_box.button(dialog.button_box.StandardButton.Close)
    assert close_button is not None
    expected: tuple[QWidget, ...] = (
        dialog.server_edit,
        dialog.credential_edit,
        dialog.test_button,
        dialog.automatic_upload_check,
        dialog.enabled_check,
        dialog.cache_limit_spin,
        close_button,
    )
    for current, following in zip(expected, expected[1:], strict=False):
        current.setFocus()
        QTest.keyClick(current, Qt.Key.Key_Tab)  # type: ignore[call-overload]
        assert dialog.focusWidget() is following


@pytest.mark.parametrize('close_path', ['escape', 'button', 'window-manager'])
def test_each_close_path_cancels_exact_blocked_test_and_ignores_late_identity(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
    qapp: QApplication,
    close_path: str,
) -> None:
    candidate = secrets.token_urlsafe(40)
    dialog.server_edit.setText(NEW_ORIGIN)
    dialog.credential_edit.setText(candidate)
    dialog.test_button.click()
    dialog.error_label.setText('Testing')

    if close_path == 'escape':
        QTest.keyClick(dialog, Qt.Key.Key_Escape)  # type: ignore[call-overload]
    elif close_path == 'button':
        close_button = dialog.button_box.button(dialog.button_box.StandardButton.Close)
        assert close_button is not None
        close_button.click()
    else:
        dialog.close()
    qapp.processEvents()

    assert not dialog.isVisible()
    assert controller.cancel_calls == ['opaque-test-id']
    assert dialog.credential_edit.text() == ''
    assert dialog.error_label.text() == ''
    assert controller.saved_geometries
    assert_secret_absent(candidate, dialog)
    assert_secret_absent(candidate, controller.__dict__)

    active = replace(settings, origin=NEW_ORIGIN, identity=IDENTITY)
    controller.settingsChanged.emit(active)
    controller.identityChanged.emit(IDENTITY)
    qapp.processEvents()

    assert dialog.identity_label.text() == 'Not connected'
    assert not dialog.automatic_upload_check.isEnabled()
    assert controller.remove_credential_calls == 0


@pytest.mark.parametrize('close_path', ['escape', 'button', 'window-manager'])
def test_each_close_path_cancels_real_blocked_auth_without_keyring_or_identity(
    qapp: QApplication,
    tmp_path: Path,
    close_path: str,
) -> None:
    settings_store = SettingsStore(
        QSettings(str(tmp_path / 'dialog-close.ini'), QSettings.Format.IniFormat)
    )
    settings_store.set_origin(ORIGIN)
    credentials = RejectingCredentialStore()
    client = BlockingDialogClient()
    controller = RoastServerController(
        settings=settings_store,
        credentials=credentials,
        data_root=tmp_path / 'dialog-close-data',
        client_factory=cast(ClientFactory, lambda _origin, _credential: client),
        profile_validator=lambda _path: None,
    )
    value = RoastServerConfigDialog(controller, settings_store.load())
    value.show()
    controller.start()
    qapp.processEvents()
    candidate = secrets.token_urlsafe(40)
    try:
        value.credential_edit.setText(candidate)
        value.test_button.click()
        assert client.entered.wait(timeout=2)

        if close_path == 'escape':
            QTest.keyClick(value, Qt.Key.Key_Escape)  # type: ignore[call-overload]
        elif close_path == 'button':
            close_button = value.button_box.button(value.button_box.StandardButton.Close)
            assert close_button is not None
            close_button.click()
        else:
            value.close()
        qapp.processEvents()

        assert controller._credential_vault.size() == 0
        client.release.set()
        assert client.completed.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            qapp.processEvents()
            if (
                not controller._worker._credential_transactions
                and controller._worker._configuration is not None
            ):
                break
            time.sleep(0.001)
        for _ in range(10):
            qapp.processEvents()
            time.sleep(0.001)

        assert credentials.set_calls == 0
        assert credentials.delete_calls == 0
        assert settings_store.load().identity is None
        assert value.identity_label.text() == 'Not connected'
        assert not value.automatic_upload_check.isEnabled()
        assert_secret_absent(candidate, value)
        assert_secret_absent(candidate, controller)
    finally:
        client.release.set()
        assert controller.shutdown(2_000)
        value.hide()
        value.deleteLater()
        qapp.processEvents()


def test_real_startup_edit_without_test_revokes_backend_proof_and_automatic_upload(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    settings_store = SettingsStore(
        QSettings(str(tmp_path / 'dialog-startup-edit.ini'), QSettings.Format.IniFormat)
    )
    settings_store.set_origin(ORIGIN)
    settings_store.save_connection(ORIGIN, IDENTITY)
    settings_store.save_options(True, True, 64 * 1024 * 1024)
    persisted_credential = secrets.token_urlsafe(32)
    credentials = WritableCredentialStore(ORIGIN, persisted_credential)
    client = BlockingDialogClient()
    controller = RoastServerController(
        settings=settings_store,
        credentials=credentials,
        data_root=tmp_path / 'dialog-startup-edit-data',
        client_factory=cast(ClientFactory, lambda _origin, _credential: client),
        profile_validator=lambda _path: None,
    )
    value = RoastServerConfigDialog(controller, settings_store.load())
    value.show()
    controller.start()
    assert client.entered.wait(timeout=2)
    try:
        value.server_edit.setText(NEW_ORIGIN)
        assert not settings_store.load().automatic_upload

        client.release.set()
        assert client.completed.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            qapp.processEvents()
            configuration = controller._worker._configuration
            if configuration is not None and not configuration.enabled:
                break
            time.sleep(0.001)
        for _ in range(10):
            qapp.processEvents()
            time.sleep(0.001)

        loaded = settings_store.load()
        assert controller._proof is None
        assert controller._identity is None
        assert controller._active_namespace(require_enabled=False) is None
        assert not loaded.enabled and not loaded.automatic_upload
        assert controller._worker._configuration is not None
        assert not controller._worker._configuration.enabled
        assert value.identity_label.text() == 'Not connected'
        assert not value.automatic_upload_check.isEnabled()
        assert credentials.values[ORIGIN] == persisted_credential
    finally:
        client.release.set()
        assert controller.shutdown(2_000)
        value.hide()
        value.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize('old_credential', [None, 'old-credential'])
def test_real_hide_after_emitted_activation_rolls_back_keyring_settings_and_auth(
    qapp: QApplication,
    tmp_path: Path,
    old_credential: str | None,
) -> None:
    settings_store = SettingsStore(
        QSettings(str(tmp_path / 'dialog-delayed-activation.ini'), QSettings.Format.IniFormat)
    )
    settings_store.set_origin(ORIGIN)
    previous = settings_store.load()
    credentials = WritableCredentialStore(ORIGIN, old_credential)
    client = StagedActivationClient()
    controller = RoastServerController(
        settings=settings_store,
        credentials=credentials,
        data_root=tmp_path / 'dialog-delayed-activation-data',
        client_factory=cast(ClientFactory, lambda _origin, _credential: client),
        profile_validator=lambda _path: None,
    )
    activation_emitted = threading.Event()
    direct_connect = cast(
        Callable[[Callable[[str, object], None], Qt.ConnectionType], object],
        controller._worker.connectionActivated.connect,
    )
    direct_connect(
        lambda _transaction_id, _identity: activation_emitted.set(),
        Qt.ConnectionType.DirectConnection,
    )
    value = RoastServerConfigDialog(controller, previous)
    value.show()
    controller.start()
    qapp.processEvents()
    candidate = secrets.token_urlsafe(40)
    try:
        value.credential_edit.setText(candidate)
        value.test_button.click()
        deadline = time.monotonic() + 2
        while not client.final_auth_entered.is_set():
            qapp.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError('worker did not enter final authentication')
            time.sleep(0.001)

        client.release_final_auth.set()
        assert activation_emitted.wait(timeout=2)
        value.reject()
        assert not value.isVisible()
        qapp.processEvents()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            qapp.processEvents()
            if not controller._worker._credential_transactions:
                break
            time.sleep(0.001)

        restored = settings_store.load()
        assert replace(restored, configuration_geometry=None) == previous
        assert restored.configuration_geometry is not None
        if old_credential is None:
            assert ORIGIN not in credentials.values
        else:
            assert credentials.values[ORIGIN] == old_credential
        assert controller._proof is None
        assert controller._identity is None
        assert controller._worker._credential is None
        assert controller._worker._authorized_target is None
        assert controller._worker._credential_transactions == {}
        assert_secret_absent(candidate, controller)
        assert_secret_absent(candidate, value)
    finally:
        client.release_final_auth.set()
        assert controller.shutdown(2_000)
        value.hide()
        value.deleteLater()
        qapp.processEvents()


def test_closing_idle_dialog_preserves_established_proof(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    settings: ConnectorSettings,
    qapp: QApplication,
) -> None:
    activate(dialog, controller, settings, enabled=True)

    QTest.keyClick(dialog, Qt.Key.Key_Escape)  # type: ignore[call-overload]
    qapp.processEvents()

    assert controller.cancel_calls == []
    assert controller.invalidate_calls == 0
    assert dialog.identity_label.text() == 'Owner — Roastery (admin)'
    assert dialog.automatic_upload_check.isEnabled()


def test_configuration_geometry_round_trip_is_saved_and_restored_onscreen(
    qapp: QApplication,
    controller: FakeController,
    settings: ConnectorSettings,
) -> None:
    source = QDialog()
    source.setGeometry(30, 40, 780, 620)
    geometry = source.saveGeometry()
    restored_settings = replace(settings, configuration_geometry=geometry)
    first = RoastServerConfigDialog(controller, restored_settings)
    first.show()
    qapp.processEvents()

    first_screen = first.screen()
    assert first_screen is not None
    screen = first_screen.availableGeometry()
    if screen.width() >= first.frameGeometry().width() and screen.height() >= first.frameGeometry().height():
        assert screen.contains(first.frameGeometry())
    first.close()
    qapp.processEvents()
    assert controller.saved_geometries[-1]

    offscreen_source = QDialog()
    offscreen_source.setGeometry(screen.right() + 10_000, screen.bottom() + 10_000, 900, 700)
    bounded = RoastServerConfigDialog(
        controller,
        replace(settings, configuration_geometry=offscreen_source.saveGeometry()),
    )
    bounded.show()
    qapp.processEvents()
    bounded_screen = bounded.screen()
    assert bounded_screen is not None
    bounded_available = bounded_screen.availableGeometry()
    if (
        bounded_available.width() >= bounded.minimumWidth()
        and bounded_available.height() >= bounded.minimumHeight()
    ):
        assert bounded_available.contains(bounded.frameGeometry())

    first.deleteLater()
    bounded.close()
    bounded.deleteLater()
    source.deleteLater()
    offscreen_source.deleteLater()
    qapp.processEvents()


BROWSER_ROAST_ONE = UUID('55555555-5555-4555-8555-555555555555')
BROWSER_ROAST_TWO = UUID('66666666-6666-4666-8666-666666666666')
BROWSER_NAMESPACE = namespace_for(ORIGIN, IDENTITY.organization.id)
BROWSER_REVISION = Revision(
    revision_number=2,
    sha256='a' * 64,
    byte_size=128,
    parser_version='browser-test',
    parse_state='parsed',
    parse_diagnostic_code=None,
    parse_diagnostic_message=None,
    uploaded_at=datetime(2026, 8, 1, 13, tzinfo=UTC),
    metadata=(),
    reparse_recommended=False,
)
BROWSER_LABEL = LabelSummary(
    label_uuid=UUID('77777777-7777-4777-8777-777777777777'),
    name='<b>Plain & curated</b>',
    color='green',
    archived=False,
)
BROWSER_ARCHIVED_LABEL = LabelSummary(
    label_uuid=UUID('88888888-8888-4888-8888-888888888888'),
    name='Former label',
    color='violet',
    archived=True,
)


def browser_summary(
    roast_uuid: UUID,
    *,
    roast_at: datetime,
    state: RoastState = 'parsed',
    title: str | None = 'Browser roast',
) -> RoastSummary:
    return RoastSummary(
        roast_uuid=roast_uuid,
        state=state,
        roast_at=roast_at,
        title=title,
        batch_prefix='B',
        batch_number=12,
        batch_position=3,
        operator='Operator',
        machine='Test drum',
        machine_setup='Setup',
        temperature_unit='C',
        duration_seconds=600,
        green_weight_kg=1.0,
        roasted_weight_kg=0.85,
        revision_count=0 if state == 'awaiting_profile' else 2,
        updated_at=roast_at,
        labels=(BROWSER_LABEL,),
    )


BROWSER_SUMMARY_ONE = browser_summary(
    BROWSER_ROAST_ONE, roast_at=datetime(2026, 8, 1, 12, tzinfo=UTC)
)
BROWSER_SUMMARY_TWO = browser_summary(
    BROWSER_ROAST_TWO, roast_at=datetime(2026, 8, 2, 12, tzinfo=UTC)
)
BROWSER_CACHED = CachedRevision(
    namespace=BROWSER_NAMESPACE,
    roast=BROWSER_SUMMARY_ONE,
    revision=BROWSER_REVISION,
    path=Path('/tmp/browser-one.alog'),
    sidecar_path=Path('/tmp/browser-one.json'),
    downloaded_at=datetime(2026, 8, 1, 13, tzinfo=UTC),
)
BROWSER_SOURCE = ServerProfileSource(
    namespace=BROWSER_NAMESPACE,
    roast_uuid=BROWSER_ROAST_ONE,
    revision_number=2,
    sha256='a' * 64,
    stale=True,
)
BROWSER_FAILURE = PublicFailure(
    kind=FailureKind.OFFLINE,
    code='offline',
    message='Offline / server unavailable.',
    retryable=True,
)


class FakeBrowserController(QObject):
    archivePageReady = pyqtSignal(str, object)
    operationFailed = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    profileReady = pyqtSignal(str, object)
    cachedFallbackReady = pyqtSignal(str, object)
    identityChanged = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.browse_calls: list[tuple[str, ArchiveFilters, bool]] = []
        self.load_more_calls: list[str] = []
        self.open_roast_calls: list[tuple[str, UUID]] = []
        self.open_cached_calls: list[tuple[str, CachedRevision]] = []
        self.cancel_open_calls: list[str] = []
        self.resolved_cached: dict[tuple[UUID, int, str], CachedRevision] = {}
        self.saved_geometries: list[bytes] = []
        self._counter = 0

    def _request_id(self, prefix: str) -> str:
        self._counter += 1
        return f'{prefix}-{self._counter}'

    def browse(self, filters: ArchiveFilters, refresh: bool = True) -> str:
        request_id = self._request_id('browse')
        self.browse_calls.append((request_id, filters, refresh))
        return request_id

    def load_more(self) -> str | None:
        request_id = self._request_id('more')
        self.load_more_calls.append(request_id)
        return request_id

    def open_roast(self, roast_uuid: UUID) -> str:
        request_id = self._request_id('online-open')
        self.open_roast_calls.append((request_id, roast_uuid))
        return request_id

    def open_cached(self, cached: CachedRevision) -> str:
        request_id = self._request_id('cached-open')
        self.open_cached_calls.append((request_id, cached))
        return request_id

    def cancel_open(self, request_id: str) -> None:
        self.cancel_open_calls.append(request_id)

    def close_browser(self) -> None:
        return

    def cached_revision_for(
        self, source: ServerProfileSource
    ) -> CachedRevision | None:
        return self.resolved_cached.get(
            (source.roast_uuid, source.revision_number, source.sha256)
        )

    def save_browser_geometry(self, geometry: QByteArray) -> None:
        self.saved_geometries.append(bytes(geometry.data()))


@pytest.fixture
def browser_controller() -> FakeBrowserController:
    return FakeBrowserController()


@pytest.fixture
def browser(
    qapp: QApplication,
    browser_controller: FakeBrowserController,
    settings: ConnectorSettings,
) -> Generator[RoastServerBrowserDialog]:
    value = RoastServerBrowserDialog(browser_controller, settings)
    value.show()
    qapp.processEvents()
    yield value
    value.hide()
    value.deleteLater()
    qapp.processEvents()


def online_browser_page(
    *rows: RoastSummary, next_cursor: str | None = None
) -> ArchivePageView:
    return ArchivePageView(
        rows=tuple(ArchiveRow(row, None, None, False) for row in rows),
        next_cursor=next_cursor,
        online=True,
        retained_error=None,
    )


def cached_browser_page(*cached: CachedRevision) -> ArchivePageView:
    return ArchivePageView(
        rows=tuple(
            ArchiveRow(
                item.roast,
                item.revision.revision_number,
                item.revision.sha256,
                True,
                item,
            )
            for item in cached
        ),
        next_cursor=None,
        online=False,
        retained_error=BROWSER_FAILURE,
    )


def emit_current_page(
    browser_controller: FakeBrowserController,
    page: ArchivePageView,
) -> str:
    request_id = browser_controller.browse_calls[-1][0]
    browser_controller.archivePageReady.emit(request_id, page)
    return request_id


def test_browser_model_is_read_only_plain_accessible_newest_first_and_bounded() -> None:
    model = RoastTableModel(max_rows=50)
    older = ArchiveRow(
        BROWSER_SUMMARY_ONE,
        BROWSER_REVISION.revision_number,
        BROWSER_REVISION.sha256,
        True,
        BROWSER_CACHED,
    )
    newer = ArchiveRow(BROWSER_SUMMARY_TWO, None, None, False)

    model.set_page(ArchivePageView((older, newer), 'next', True, None), append=False)

    assert model.rowCount() == 2
    assert model.columnCount() == 8
    assert model.roast_uuids() == (BROWSER_ROAST_TWO, BROWSER_ROAST_ONE)
    assert [
        model.headerData(column, Qt.Orientation.Horizontal)
        for column in range(model.columnCount())
    ] == ['Roast date', 'Title', 'Batch', 'Machine', 'Labels', 'Parse state', 'Revisions', 'Cache']
    label_index = model.index(1, 4)
    assert model.data(label_index) == '<b>Plain & curated</b> (Green)'
    assert model.data(label_index, Qt.ItemDataRole.EditRole) is None
    assert model.flags(label_index) == (
        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    )
    assert model.data(
        model.index(1, 0), RoastTableModel.RoastUuidRole
    ) == BROWSER_ROAST_ONE
    assert model.data(
        model.index(1, 0), RoastTableModel.CachedRevisionRole
    ) == BROWSER_CACHED
    assert '<b>Plain & curated</b> (Green)' in cast(
        str,
        model.data(model.index(1, 0), Qt.ItemDataRole.AccessibleTextRole),
    )
    decoration = model.data(label_index, Qt.ItemDataRole.DecorationRole)
    assert isinstance(decoration, QColor)
    assert decoration.name() == '#16a34a'

    archived = replace(
        BROWSER_SUMMARY_ONE,
        labels=(BROWSER_ARCHIVED_LABEL,),
    )
    model.set_page(
        ArchivePageView((ArchiveRow(archived, None, None, False),), None, True, None),
        append=False,
    )
    archived_index = model.index(0, 4)
    assert model.data(archived_index) == 'Former label (Violet, archived)'
    assert 'archived' in cast(
        str,
        model.data(archived_index, Qt.ItemDataRole.AccessibleTextRole),
    )

    many = tuple(
        ArchiveRow(
            replace(BROWSER_SUMMARY_ONE, roast_uuid=UUID(int=number + 1)),
            None,
            None,
            False,
        )
        for number in range(60)
    )
    model.set_page(ArchivePageView(many, 'more', True, None), append=False)
    assert model.rowCount() == 50
    assert not model.has_more()


def test_browser_filter_requests_are_trimmed_capped_debounced_and_utc_inclusive(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
) -> None:
    browser_controller.browse_calls.clear()
    browser.search_edit.setText(f'  {"x" * 205}  ')
    QTest.qWait(150)  # type: ignore[call-arg, arg-type]
    assert browser_controller.browse_calls == []
    QTest.qWait(200)  # type: ignore[call-arg, arg-type]
    assert len(browser_controller.browse_calls) == 1
    assert browser_controller.browse_calls[-1][1].search == 'x' * 200
    first_request = browser_controller.browse_calls[-1][0]
    browser_controller.archivePageReady.emit(
        first_request, online_browser_page(BROWSER_SUMMARY_ONE)
    )

    parsed_index = browser.state_combo.findData('parse_failed')
    assert parsed_index >= 0
    browser.state_combo.setCurrentIndex(parsed_index)
    browser.machine_edit.setText(f'  {"m" * 105}  ')
    browser.start_date_edit.setDate(QDate(2026, 7, 1))
    browser.end_date_edit.setDate(QDate(2026, 7, 31))
    browser.start_date_check.setChecked(True)
    browser.end_date_check.setChecked(True)
    browser.refresh_button.click()

    filters = browser_controller.browse_calls[-1][1]
    assert filters.state == 'parse_failed'
    assert filters.machine == 'm' * 100
    assert filters.roast_at_from == datetime(2026, 7, 1, tzinfo=UTC)
    assert filters.roast_at_to == datetime.combine(
        datetime(2026, 7, 31, tzinfo=UTC).date(), datetime_time.max, UTC
    )
    assert filters.roast_at_from.tzinfo is UTC
    assert filters.roast_at_to.tzinfo is UTC


def test_browser_refresh_failure_retains_rows_and_stale_response_is_ignored(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
) -> None:
    emit_current_page(browser_controller, online_browser_page(BROWSER_SUMMARY_ONE))
    assert browser.roast_model.roast_uuids() == (BROWSER_ROAST_ONE,)

    browser.refresh_button.click()
    refresh_id = browser_controller.browse_calls[-1][0]
    browser_controller.operationFailed.emit(refresh_id, BROWSER_FAILURE)
    assert browser.roast_model.roast_uuids() == (BROWSER_ROAST_ONE,)
    assert browser.error_label.text() == BROWSER_FAILURE.message

    browser.refresh_button.click()
    current_id = browser_controller.browse_calls[-1][0]
    browser_controller.archivePageReady.emit(
        refresh_id, online_browser_page(BROWSER_SUMMARY_TWO)
    )
    assert browser.roast_model.roast_uuids() == (BROWSER_ROAST_ONE,)
    browser_controller.archivePageReady.emit(
        current_id, online_browser_page(BROWSER_SUMMARY_TWO)
    )
    assert browser.roast_model.roast_uuids() == (BROWSER_ROAST_TWO,)
    assert browser.error_label.text() == ''


def test_browser_load_more_deduplicates_and_visible_fallback_matches_scroll_paging(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
    qapp: QApplication,
) -> None:
    emit_current_page(
        browser_controller,
        online_browser_page(BROWSER_SUMMARY_ONE, next_cursor='cursor'),
    )
    assert browser.load_more_button.isVisible()
    assert browser.load_more_button.isEnabled()
    assert browser.load_more_button.accessibleName() == 'Load more server roasts'

    browser.load_more_button.click()
    more_id = browser_controller.load_more_calls[-1]
    browser_controller.archivePageReady.emit(
        more_id,
        online_browser_page(BROWSER_SUMMARY_ONE, BROWSER_SUMMARY_TWO),
    )
    assert browser.roast_model.roast_uuids() == (
        BROWSER_ROAST_TWO,
        BROWSER_ROAST_ONE,
    )

    browser.refresh_button.click()
    emit_current_page(
        browser_controller,
        online_browser_page(BROWSER_SUMMARY_ONE, next_cursor='another'),
    )
    before = len(browser_controller.load_more_calls)
    scroll = browser.roast_view.verticalScrollBar()
    assert scroll is not None
    scroll.setRange(0, 100)
    scroll.setValue(98)
    qapp.processEvents()
    assert len(browser_controller.load_more_calls) == before + 1


def test_browser_retained_offline_page_merges_verified_cache_and_keeps_server_description(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
) -> None:
    server_summary = replace(BROWSER_SUMMARY_ONE, title='Authoritative server title')
    emit_current_page(
        browser_controller,
        online_browser_page(server_summary, BROWSER_SUMMARY_TWO, next_cursor='next'),
    )
    offline_summary = replace(BROWSER_SUMMARY_ONE, title=None, machine=None, labels=())
    verified = replace(BROWSER_CACHED, roast=offline_summary)

    browser.refresh_button.click()
    refresh_id = browser_controller.browse_calls[-1][0]
    browser_controller.archivePageReady.emit(refresh_id, cached_browser_page(verified))

    retained = browser.roast_model.row(
        browser.roast_model.roast_uuids().index(BROWSER_ROAST_ONE)
    )
    assert retained is not None
    assert retained.roast.title == 'Authoritative server title'
    assert retained.roast.machine == BROWSER_SUMMARY_ONE.machine
    assert retained.cached == verified
    assert retained.stale
    assert not browser.roast_model.has_more()
    browser.select_roast(BROWSER_ROAST_ONE)
    assert browser.open_button.isEnabled()
    browser.open_button.click()
    assert browser_controller.open_cached_calls[-1][1] == verified

    browser._finish_open()
    browser.select_roast(BROWSER_ROAST_TWO)
    assert not browser.open_button.isEnabled()


def test_browser_offline_open_revalidates_cached_and_tracks_only_matching_profile(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
) -> None:
    emit_current_page(browser_controller, cached_browser_page(BROWSER_CACHED))
    browser.select_roast(BROWSER_ROAST_ONE)
    assert browser.open_button.isEnabled()
    assert browser.status_label.text() == 'Offline — cached copies may be stale.'

    browser.open_button.click()
    open_id, cached = browser_controller.open_cached_calls[-1]
    assert cached == BROWSER_CACHED
    assert not browser.open_button.isEnabled()
    assert browser.progress_bar.isVisible()
    assert browser.cancel_open_button.isVisible()

    wrong = replace(BROWSER_SOURCE, revision_number=1)
    browser_controller.profileReady.emit(str(BROWSER_CACHED.path), wrong)
    assert browser.progress_bar.isVisible()
    browser_controller.profileReady.emit(str(BROWSER_CACHED.path), BROWSER_SOURCE)
    assert not browser.progress_bar.isVisible()
    assert browser.status_label.text() == 'Opened verified cached revision 2 (stale).'
    assert BROWSER_SOURCE.stale
    assert open_id not in browser_controller.cancel_open_calls


def test_browser_refresh_during_open_accepts_detail_revision_advance_and_updates_row(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
) -> None:
    emit_current_page(browser_controller, online_browser_page(BROWSER_SUMMARY_ONE))
    browser.select_roast(BROWSER_ROAST_ONE)
    browser.open_button.click()
    assert browser.progress_bar.isVisible()

    browser.refresh_button.click()
    emit_current_page(browser_controller, online_browser_page(BROWSER_SUMMARY_ONE))
    advanced = replace(
        BROWSER_SOURCE,
        revision_number=3,
        sha256='c' * 64,
        stale=False,
    )
    advanced_cached = replace(
        BROWSER_CACHED,
        roast=replace(BROWSER_SUMMARY_ONE, revision_count=3),
        revision=replace(BROWSER_REVISION, revision_number=3, sha256='c' * 64),
        path=Path('/tmp/advanced.alog'),
    )
    browser_controller.resolved_cached[
        (advanced.roast_uuid, advanced.revision_number, advanced.sha256)
    ] = advanced_cached
    browser_controller.profileReady.emit('/tmp/advanced.alog', advanced)

    assert not browser.progress_bar.isVisible()
    assert browser.status_label.text() == 'Opened verified server revision 3.'
    row = browser.roast_model.row(0)
    assert row is not None
    assert row.roast.revision_count == 3
    assert row.cached_revision == 3
    assert row.cached_sha256 == 'c' * 64
    assert row.cached == advanced_cached

    browser_controller.onlineChanged.emit(False)
    browser.select_roast(BROWSER_ROAST_ONE)
    assert browser.open_button.isEnabled()
    browser.open_button.click()
    assert browser_controller.open_cached_calls[-1][1] == advanced_cached


def test_browser_awaiting_profile_disabled_fallback_is_confirmed_and_close_cancels(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    awaiting = browser_summary(
        BROWSER_ROAST_TWO,
        roast_at=datetime(2026, 8, 2, tzinfo=UTC),
        state='awaiting_profile',
    )
    emit_current_page(
        browser_controller,
        online_browser_page(awaiting, BROWSER_SUMMARY_ONE),
    )
    browser.select_roast(BROWSER_ROAST_TWO)
    assert not browser.open_button.isEnabled()

    browser.select_roast(BROWSER_ROAST_ONE)
    browser.open_button.click()
    online_id = browser_controller.open_roast_calls[-1][0]
    monkeypatch.setattr(
        browser,
        '_confirm_cached_fallback',
        lambda _cached: True,
    )
    browser_controller.operationFailed.emit(online_id, BROWSER_FAILURE)
    browser_controller.cachedFallbackReady.emit(online_id, BROWSER_CACHED)
    cached_id, fallback = browser_controller.open_cached_calls[-1]
    assert fallback == BROWSER_CACHED
    assert cached_id != online_id

    browser.close()
    qapp.processEvents()
    assert not browser.isVisible()
    assert browser_controller.cancel_open_calls[-1] == cached_id
    assert browser_controller.saved_geometries
    browser_controller.profileReady.emit(str(BROWSER_CACHED.path), BROWSER_SOURCE)
    assert not browser.isVisible()


def test_browser_namespace_change_clears_rows_selection_requests_and_progress(
    browser: RoastServerBrowserDialog,
    browser_controller: FakeBrowserController,
) -> None:
    emit_current_page(browser_controller, online_browser_page(BROWSER_SUMMARY_ONE))
    browser.select_roast(BROWSER_ROAST_ONE)
    browser.open_button.click()
    open_id = browser_controller.open_roast_calls[-1][0]

    browser_controller.identityChanged.emit(IDENTITY)

    assert browser.roast_model.rowCount() == 0
    assert browser.roast_view.currentIndex().isValid() is False
    assert not browser.progress_bar.isVisible()
    assert browser_controller.cancel_open_calls[-1] == open_id
    assert not browser.load_more_button.isEnabled()


def test_browser_is_modeless_plain_accessible_keyboard_reachable_and_onscreen(
    browser: RoastServerBrowserDialog,
) -> None:
    assert not browser.isModal()
    assert browser.error_label.textFormat() is Qt.TextFormat.PlainText
    assert browser.status_label.textFormat() is Qt.TextFormat.PlainText
    assert not browser.error_label.openExternalLinks()
    assert browser.search_label.buddy() is browser.search_edit
    assert browser.machine_label.buddy() is browser.machine_edit
    assert browser.roast_view.accessibleName() == 'Server roast archive'
    assert browser.open_button.accessibleName() == 'Open selected server roast'
    assert browser.load_more_button.accessibleName() == 'Load more server roasts'
    assert '&' in browser.cancel_open_button.text()
    browser.cancel_open_button.show()
    browser.load_more_button.setEnabled(True)
    browser.open_button.setEnabled(True)
    browser.load_more_button.setFocus(Qt.FocusReason.OtherFocusReason)
    QTest.keyClick(browser.load_more_button, Qt.Key.Key_Tab)  # type: ignore[call-overload]
    assert QApplication.focusWidget() is browser.cancel_open_button
    QTest.keyClick(browser.cancel_open_button, Qt.Key.Key_Tab)  # type: ignore[call-overload]
    assert QApplication.focusWidget() is browser.open_button
    browser.cancel_open_button.hide()
    assert not hasattr(browser, 'comments_edit')
    assert not browser.roast_view.editTriggers()
    screen = browser.screen()
    assert screen is not None
    available = screen.availableGeometry()
    if (
        available.width() >= browser.minimumWidth()
        and available.height() >= browser.minimumHeight()
    ):
        assert available.contains(browser.frameGeometry())
