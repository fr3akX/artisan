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

from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import secrets
from typing import cast
from uuid import UUID

import pytest
from PyQt6.QtCore import QByteArray, QObject, Qt, pyqtSignal
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from artisanlib.roastserver.cache import CacheStats
from artisanlib.roastserver.contract import (
    FailureKind,
    IdentityOrganization,
    IdentityUser,
    PublicFailure,
    ServerIdentity,
)
from artisanlib.roastserver.dialogs import FailedJobsModel, RoastServerConfigDialog
from artisanlib.roastserver.outbox import FailedJob, QueueCounts
from artisanlib.roastserver.settings import (
    KEYRING_FAILURE_MESSAGE,
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    ConnectorSettings,
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
SAFE_FAILURE = PublicFailure(
    kind=FailureKind.OFFLINE,
    code='offline',
    message='<a href="https://unsafe.test">Offline</a>',
    retryable=True,
)


class FakeController(QObject):
    settingsChanged = pyqtSignal(object)
    identityChanged = pyqtSignal(object)
    queueChanged = pyqtSignal(object)
    failedJobsChanged = pyqtSignal(object)
    cacheStatsChanged = pyqtSignal(object)
    operationFailed = pyqtSignal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.test_origins: list[str] = []
        self.candidate_digests: list[str] = []
        self.apply_calls: list[tuple[str, bool, bool, int]] = []
        self.retry_calls: list[str] = []
        self.remove_calls: list[str] = []
        self.refresh_calls = 0
        self.clear_calls = 0
        self.remove_credential_calls = 0
        self.invalidate_calls = 0
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

    def remove_credential(self) -> None:
        self.remove_credential_calls += 1

    def refresh_queue(self) -> None:
        self.refresh_calls += 1

    def retry_job(self, job_id: str) -> None:
        self.retry_calls.append(job_id)

    def remove_job(self, job_id: str) -> None:
        self.remove_calls.append(job_id)

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
) -> Generator[RoastServerConfigDialog, None, None]:
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


def test_config_invalid_origin_is_rejected_locally(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    dialog.server_edit.setText('http://remote.example.test/path')
    dialog.credential_edit.setText(secrets.token_urlsafe(32))
    dialog.test_button.click()

    assert controller.test_origins == []
    assert dialog.credential_edit.text() == ''
    assert dialog.error_label.text() == 'Enter a valid HTTPS origin.'
    assert dialog.error_label.textFormat() is Qt.TextFormat.PlainText
    assert not dialog.error_label.openExternalLinks()


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
    assert candidate not in repr(dialog)
    assert candidate not in repr(dialog.failed_model)
    assert candidate not in repr(controller.__dict__)
    clipboard = qapp.clipboard()
    assert clipboard is not None
    assert candidate not in clipboard.text()

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

    assert clipboard.text() != candidate
    dialog.credential_edit.clear()


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
    activate(dialog, controller, settings, automatic_upload=True, enabled=True)
    dialog.credential_edit.setText(secrets.token_urlsafe(24))

    assert not dialog.automatic_upload_check.isEnabled()
    assert controller.invalidate_calls == 2
    assert controller.apply_calls == []


def test_config_edit_cancels_an_opaque_test_transaction_immediately(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
) -> None:
    dialog.credential_edit.setText(secrets.token_urlsafe(24))
    dialog.test_button.click()
    assert controller.invalidate_calls == 0

    dialog.server_edit.setText('https://newer.example.test')

    assert controller.invalidate_calls == 1
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


def test_accessible_labels_focus_order_and_escape_hide_modeless_dialog(
    dialog: RoastServerConfigDialog,
    controller: FakeController,
    qapp: QApplication,
) -> None:
    assert dialog.server_label.buddy() is dialog.server_edit
    assert dialog.credential_label.buddy() is dialog.credential_edit
    assert dialog.cache_limit_label.buddy() is dialog.cache_limit_spin
    assert dialog.server_edit.accessibleName() == 'Server origin'
    assert dialog.credential_edit.accessibleName() == 'Credential'
    assert dialog.failed_view.accessibleName() == 'Failed uploads'

    dialog.server_edit.setFocus()
    assert dialog.focusWidget() is dialog.server_edit
    assert dialog.nextInFocusChain() is not dialog
    QTest.keyClick(dialog, Qt.Key.Key_Escape)  # type: ignore[call-overload]
    qapp.processEvents()

    assert not dialog.isVisible()
    assert controller.saved_geometries


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
    assert screen.intersects(first.frameGeometry())
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
    assert bounded_screen.availableGeometry().intersects(bounded.frameGeometry())

    first.deleteLater()
    bounded.close()
    bounded.deleteLater()
    source.deleteLater()
    offscreen_source.deleteLater()
    qapp.processEvents()
