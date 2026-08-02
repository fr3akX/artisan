#
# ABOUT
# Artisan Roast Server modeless configuration dialog
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

# Widgets are initialized synchronously by _build_ui() from __init__.
# pyright: reportUninitializedInstanceVariable=false

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Final, Protocol, cast, override

from PyQt6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QModelIndex,
    QObject,
    QRect,
    Qt,
    pyqtSlot,
)
from PyQt6.QtGui import QCloseEvent, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from artisanlib.roastserver.contract import FailureKind, PublicFailure, ServerIdentity
from artisanlib.roastserver.origin import SettingsError, canonical_origin
from artisanlib.roastserver.settings import (
    KEYRING_FAILURE_MESSAGE,
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    ConnectorSettings,
)

_MEBIBYTE: Final[int] = 1024 * 1024
_GENERIC_OPERATION_FAILURE: Final[str] = 'The operation could not be completed.'
_TEST_CONNECTION_MESSAGE: Final[str] = (
    'Test the connection before enabling Roast Server or automatic upload.'
)
_ENTER_CREDENTIAL_MESSAGE: Final[str] = 'Enter a credential.'
_NOT_CONNECTED: Final[str] = 'Not connected'
_ROOT_INDEX: Final[QModelIndex] = QModelIndex()


class _FailedJob(Protocol):
    id: str
    roast_uuid: object
    attempts: int
    next_attempt_at: datetime | None
    error_code: str
    error_message: str


class _QueueCounts(Protocol):
    pending: int
    retrying: int
    paused: int
    failed: int


class _CacheStats(Protocol):
    byte_count: int
    revision_count: int


class _Signal(Protocol):
    def connect(self, slot: Callable[..., object]) -> object: ...


class RoastServerDialogController(Protocol):
    settingsChanged: _Signal
    identityChanged: _Signal
    queueChanged: _Signal
    failedJobsChanged: _Signal
    cacheStatsChanged: _Signal
    operationFailed: _Signal

    def test_connection(self, origin: str, candidate: str) -> str: ...

    def apply_options(
        self,
        origin: str,
        enabled: bool,
        automatic_upload: bool,
        cache_limit_bytes: int,
    ) -> None: ...

    def invalidate_connection_proof(self) -> None: ...
    def remove_credential(self) -> None: ...
    def refresh_queue(self) -> None: ...
    def retry_job(self, job_id: str) -> None: ...
    def remove_job(self, job_id: str) -> None: ...
    def clear_unused_cache(self) -> None: ...
    def save_configuration_geometry(self, geometry: QByteArray) -> None: ...


def _tr(text: str) -> str:
    return QApplication.translate('RoastServer', text)


class FailedJobsModel(QAbstractTableModel):
    _HEADERS: Final[tuple[str, ...]] = (
        'Roast UUID',
        'Attempts',
        'Next try',
        'Category',
        'Message',
        'Retry',
        'Remove',
    )

    def __init__(self, parent: QObject | None = None) -> None:
        # QObject is accepted by Qt even though Qt's generated annotation names
        # QAbstractItemModel's parent as QObject rather than QWidget.
        super().__init__(parent)
        self._jobs: tuple[object, ...] = ()

    @override
    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._jobs)

    @override
    def columnCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    @override
    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation is Qt.Orientation.Horizontal
            and 0 <= section < len(self._HEADERS)
        ):
            return _tr(self._HEADERS[section])
        return None

    @override
    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            role != Qt.ItemDataRole.DisplayRole
            or not index.isValid()
            or not 0 <= index.row() < len(self._jobs)
        ):
            return None
        job = cast(_FailedJob, self._jobs[index.row()])
        column = index.column()
        if column == 0:
            return str(job.roast_uuid)
        if column == 1:
            return job.attempts
        if column == 2:
            return _format_next_attempt(job.next_attempt_at)
        if column == 3:
            return job.error_code
        if column == 4:
            return job.error_message
        return None

    @override
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_jobs(self, jobs: tuple[object, ...]) -> None:
        self.beginResetModel()
        self._jobs = tuple(jobs)
        self.endResetModel()

    def job_id(self, row: int) -> str | None:
        if not 0 <= row < len(self._jobs):
            return None
        return cast(_FailedJob, self._jobs[row]).id

    def roast_uuid(self, row: int) -> str | None:
        if not 0 <= row < len(self._jobs):
            return None
        return str(cast(_FailedJob, self._jobs[row]).roast_uuid)


class RoastServerConfigDialog(QDialog):
    server_label: QLabel
    server_edit: QLineEdit
    credential_label: QLabel
    credential_edit: QLineEdit
    test_button: QPushButton
    identity_label: QLabel
    enabled_check: QCheckBox
    automatic_upload_check: QCheckBox
    cache_limit_label: QLabel
    cache_limit_spin: QSpinBox
    remove_credential_button: QPushButton
    pending_count_label: QLabel
    retrying_count_label: QLabel
    paused_count_label: QLabel
    failed_count_label: QLabel
    failed_model: FailedJobsModel
    failed_view: QTableView
    refresh_button: QPushButton
    cache_label: QLabel
    clear_cache_button: QPushButton
    error_label: QLabel
    button_box: QDialogButtonBox

    def __init__(
        self,
        controller: object,
        settings: ConnectorSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = cast(RoastServerDialogController, controller)
        self._settings = settings
        self._identity: ServerIdentity | None = None
        self._proof_origin: str | None = None
        self._testing_operation: str | None = None
        self._connection_dirty = False
        self._rendering = False

        self.setWindowTitle(_tr('Roast Server'))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(680, 500)
        self.resize(920, 700)
        self._build_ui()
        self._connect_controller()
        self._render_settings(settings, update_origin=True)
        if settings.configuration_geometry is not None:
            self.restoreGeometry(settings.configuration_geometry)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        connection_group = QGroupBox(_tr('Connection'), self)
        connection_layout = QFormLayout(connection_group)
        self.server_label = QLabel(_tr('&Server origin:'), connection_group)
        self.server_edit = QLineEdit(connection_group)
        self.server_edit.setAccessibleName(_tr('Server origin'))
        self.server_edit.setPlaceholderText(_tr('https://server.example'))
        self.server_label.setBuddy(self.server_edit)
        connection_layout.addRow(self.server_label, self.server_edit)

        self.credential_label = QLabel(_tr('&Credential:'), connection_group)
        credential_row = QWidget(connection_group)
        credential_layout = QHBoxLayout(credential_row)
        credential_layout.setContentsMargins(0, 0, 0, 0)
        self.credential_edit = QLineEdit(credential_row)
        self.credential_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.credential_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.credential_edit.setDragEnabled(False)
        self.credential_edit.setAccessibleName(_tr('Credential'))
        self.test_button = QPushButton(_tr('&Test connection'), credential_row)
        credential_layout.addWidget(self.credential_edit, 1)
        credential_layout.addWidget(self.test_button)
        self.credential_label.setBuddy(self.credential_edit)
        connection_layout.addRow(self.credential_label, credential_row)

        identity_caption = QLabel(_tr('Identity:'), connection_group)
        self.identity_label = QLabel(_tr(_NOT_CONNECTED), connection_group)
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.identity_label.setOpenExternalLinks(False)
        connection_layout.addRow(identity_caption, self.identity_label)
        layout.addWidget(connection_group)

        options_group = QGroupBox(_tr('Options'), self)
        options_layout = QFormLayout(options_group)
        self.enabled_check = QCheckBox(_tr('&Enable Roast Server'), options_group)
        self.automatic_upload_check = QCheckBox(
            _tr('Upload saved roasts &automatically'), options_group
        )
        options_layout.addRow(self.enabled_check)
        options_layout.addRow(self.automatic_upload_check)
        self.cache_limit_label = QLabel(_tr('Cache &limit:'), options_group)
        self.cache_limit_spin = QSpinBox(options_group)
        self.cache_limit_spin.setRange(
            MIN_CACHE_LIMIT_BYTES // _MEBIBYTE,
            MAX_CACHE_LIMIT_BYTES // _MEBIBYTE,
        )
        self.cache_limit_spin.setSuffix(_tr(' MiB'))
        self.cache_limit_spin.setAccessibleName(_tr('Cache limit'))
        self.cache_limit_label.setBuddy(self.cache_limit_spin)
        options_layout.addRow(self.cache_limit_label, self.cache_limit_spin)
        self.remove_credential_button = QPushButton(
            _tr('Remove stored credential'), options_group
        )
        options_layout.addRow(self.remove_credential_button)
        layout.addWidget(options_group)

        queue_group = QGroupBox(_tr('Upload queue'), self)
        queue_layout = QVBoxLayout(queue_group)
        counts_layout = QHBoxLayout()
        self.pending_count_label = self._add_count(counts_layout, 'Pending')
        self.retrying_count_label = self._add_count(counts_layout, 'Retrying')
        self.paused_count_label = self._add_count(counts_layout, 'Paused')
        self.failed_count_label = self._add_count(counts_layout, 'Failed')
        counts_layout.addStretch(1)
        queue_layout.addLayout(counts_layout)

        self.failed_model = FailedJobsModel(self)
        self.failed_view = QTableView(queue_group)
        self.failed_view.setModel(self.failed_model)
        self.failed_view.setAccessibleName(_tr('Failed uploads'))
        self.failed_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.failed_view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.failed_view.setAlternatingRowColors(True)
        vertical_header = self.failed_view.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        horizontal_header = self.failed_view.horizontalHeader()
        if horizontal_header is not None:
            horizontal_header.setStretchLastSection(True)
        queue_layout.addWidget(self.failed_view, 1)
        self.refresh_button = QPushButton(_tr('&Refresh queue'), queue_group)
        queue_layout.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(queue_group, 1)

        cache_row = QHBoxLayout()
        cache_caption = QLabel(_tr('Cached profiles:'), self)
        self.cache_label = QLabel(_format_cache(0, 0), self)
        self.cache_label.setTextFormat(Qt.TextFormat.PlainText)
        self.cache_label.setOpenExternalLinks(False)
        self.clear_cache_button = QPushButton(_tr('Clear unused cache'), self)
        cache_row.addWidget(cache_caption)
        cache_row.addWidget(self.cache_label)
        cache_row.addStretch(1)
        cache_row.addWidget(self.clear_cache_button)
        layout.addLayout(cache_row)

        self.error_label = QLabel('', self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setOpenExternalLinks(False)
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName(_tr('Roast Server status'))
        layout.addWidget(self.error_label)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        layout.addWidget(self.button_box)

        QWidget.setTabOrder(self.server_edit, self.credential_edit)
        QWidget.setTabOrder(self.credential_edit, self.test_button)
        QWidget.setTabOrder(self.test_button, self.enabled_check)
        QWidget.setTabOrder(self.enabled_check, self.automatic_upload_check)
        QWidget.setTabOrder(self.automatic_upload_check, self.cache_limit_spin)
        QWidget.setTabOrder(self.cache_limit_spin, self.remove_credential_button)
        QWidget.setTabOrder(self.remove_credential_button, self.failed_view)
        QWidget.setTabOrder(self.failed_view, self.refresh_button)
        QWidget.setTabOrder(self.refresh_button, self.clear_cache_button)

        self.server_edit.textChanged.connect(self._connection_edited)
        self.credential_edit.textChanged.connect(self._connection_edited)
        self.test_button.clicked.connect(self._test_connection)
        self.enabled_check.toggled.connect(self._enabled_toggled)
        self.automatic_upload_check.toggled.connect(self._automatic_upload_toggled)
        self.cache_limit_spin.editingFinished.connect(self._cache_limit_finished)
        self.remove_credential_button.clicked.connect(self._remove_credential)
        self.refresh_button.clicked.connect(self._refresh_queue)
        self.clear_cache_button.clicked.connect(self._clear_unused_cache)
        self.button_box.rejected.connect(self.reject)

    def _add_count(self, layout: QHBoxLayout, caption: str) -> QLabel:
        layout.addWidget(QLabel(f'{_tr(caption)}:', self))
        value = QLabel('0', self)
        value.setTextFormat(Qt.TextFormat.PlainText)
        value.setOpenExternalLinks(False)
        layout.addWidget(value)
        return value

    def _connect_controller(self) -> None:
        self._controller.settingsChanged.connect(self._on_settings_changed)
        self._controller.identityChanged.connect(self._on_identity_changed)
        self._controller.queueChanged.connect(self._on_queue_changed)
        self._controller.failedJobsChanged.connect(self._on_failed_jobs_changed)
        self._controller.cacheStatsChanged.connect(self._on_cache_stats_changed)
        self._controller.operationFailed.connect(self._on_operation_failed)

    @pyqtSlot()
    def _connection_edited(self) -> None:
        if self._rendering:
            return
        invalidate_controller = (
            self._has_current_proof() or self._testing_operation is not None
        )
        self._connection_dirty = True
        self._testing_operation = None
        self._invalidate_proof(
            persist_automatic_off=True,
            invalidate_controller=invalidate_controller,
        )

    def _invalidate_proof(
        self,
        *,
        persist_automatic_off: bool,
        invalidate_controller: bool = False,
    ) -> None:
        had_proof = self._has_current_proof()
        self._proof_origin = None
        self._identity = None
        self._rendering = True
        try:
            self.automatic_upload_check.setChecked(False)
            self.automatic_upload_check.setEnabled(False)
            self.identity_label.setText(_tr(_NOT_CONNECTED))
            self.clear_cache_button.setEnabled(False)
        finally:
            self._rendering = False
        if persist_automatic_off and (had_proof or invalidate_controller):
            try:
                self._controller.invalidate_connection_proof()
            except RuntimeError:
                self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
                return
            self._settings = replace(self._settings, automatic_upload=False)

    @pyqtSlot()
    def _test_connection(self) -> None:
        self._set_error('')
        try:
            origin = canonical_origin(self.server_edit.text())
        except SettingsError:
            self._clear_candidate()
            self._set_error(_tr('Enter a valid HTTPS origin.'))
            return
        if self.credential_edit.text() == '':
            self._set_error(_tr(_ENTER_CREDENTIAL_MESSAGE))
            return
        try:
            self._testing_operation = self._controller.test_connection(
                origin, self.credential_edit.text()
            )
        except RuntimeError:
            self._testing_operation = None
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
        finally:
            self._clear_candidate()
        self._invalidate_proof(persist_automatic_off=False)

    @pyqtSlot(bool)
    def _enabled_toggled(self, checked: bool) -> None:
        if self._rendering:
            return
        if checked and not self._has_current_proof():
            self._rendering = True
            try:
                self.enabled_check.setChecked(False)
            finally:
                self._rendering = False
            self._set_error(_tr(_TEST_CONNECTION_MESSAGE))
            return
        if not checked:
            self._rendering = True
            try:
                self.automatic_upload_check.setChecked(False)
            finally:
                self._rendering = False
            self._persist_options(enabled=False, automatic_upload=False)
            return
        self._persist_options(enabled=True)

    @pyqtSlot(bool)
    def _automatic_upload_toggled(self, checked: bool) -> None:
        if self._rendering:
            return
        if checked and not self._has_current_proof():
            self._rendering = True
            try:
                self.automatic_upload_check.setChecked(False)
            finally:
                self._rendering = False
            self._set_error(_tr(_TEST_CONNECTION_MESSAGE))
            return
        self._persist_options(automatic_upload=checked)

    @pyqtSlot()
    def _cache_limit_finished(self) -> None:
        if self._rendering:
            return
        self._persist_options(cache_limit_bytes=self.cache_limit_spin.value() * _MEBIBYTE)

    def _persist_options(
        self,
        *,
        enabled: bool | None = None,
        automatic_upload: bool | None = None,
        cache_limit_bytes: int | None = None,
    ) -> None:
        target_enabled = self._settings.enabled if enabled is None else enabled
        target_automatic = (
            self._settings.automatic_upload
            if automatic_upload is None
            else automatic_upload
        )
        if not self._has_current_proof():
            target_automatic = False
        target_limit = (
            self._settings.cache_limit_bytes
            if cache_limit_bytes is None
            else cache_limit_bytes
        )
        try:
            self._controller.apply_options(
                self._settings.origin,
                target_enabled,
                target_automatic,
                target_limit,
            )
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
            self._render_settings(self._settings, update_origin=False)
            return
        self._settings = replace(
            self._settings,
            enabled=target_enabled,
            automatic_upload=target_automatic,
            cache_limit_bytes=target_limit,
        )

    @pyqtSlot()
    def _remove_credential(self) -> None:
        self._set_error('')
        self._invalidate_proof(persist_automatic_off=True)
        try:
            self._controller.remove_credential()
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))

    @pyqtSlot()
    def _refresh_queue(self) -> None:
        try:
            self._controller.refresh_queue()
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))

    @pyqtSlot()
    def _clear_unused_cache(self) -> None:
        if not self._has_current_proof() or not self._settings.enabled:
            return
        try:
            self._controller.clear_unused_cache()
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))

    @pyqtSlot(object)
    def _on_settings_changed(self, value: object) -> None:
        if not _looks_like_settings(value):
            return
        settings = cast(ConnectorSettings, value)
        self._settings = settings
        update_origin = self._testing_operation is None and not self._connection_dirty
        self._render_settings(settings, update_origin=update_origin)
        if self._proof_origin != settings.origin:
            self._invalidate_proof(persist_automatic_off=False)

    def _render_settings(
        self, settings: ConnectorSettings, *, update_origin: bool
    ) -> None:
        self._rendering = True
        try:
            if update_origin:
                self.server_edit.setText(settings.origin)
            self.enabled_check.setChecked(settings.enabled)
            self.cache_limit_spin.setValue(settings.cache_limit_bytes // _MEBIBYTE)
            has_proof = self._has_current_proof()
            self.automatic_upload_check.setEnabled(has_proof)
            self.automatic_upload_check.setChecked(
                settings.automatic_upload if has_proof else False
            )
            self.clear_cache_button.setEnabled(has_proof and settings.enabled)
        finally:
            self._rendering = False

    @pyqtSlot(object)
    def _on_identity_changed(self, value: object) -> None:
        if value is None:
            self._invalidate_proof(persist_automatic_off=False)
            return
        if not _looks_like_identity(value) or value != self._settings.identity:
            return
        try:
            editor_origin = canonical_origin(self.server_edit.text())
        except SettingsError:
            return
        if editor_origin != self._settings.origin or self._settings.pending_connection is not None:
            return
        identity = cast(ServerIdentity, value)
        self._identity = identity
        self._proof_origin = editor_origin
        self._testing_operation = None
        self._connection_dirty = False
        self._rendering = True
        try:
            self.server_edit.setText(editor_origin)
            self.credential_edit.clear()
            self.identity_label.setText(
                f'{identity.user.nickname} — {identity.organization.name} ({_tr(identity.role)})'
            )
            self.automatic_upload_check.setEnabled(True)
            self.automatic_upload_check.setChecked(self._settings.automatic_upload)
            self.clear_cache_button.setEnabled(self._settings.enabled)
        finally:
            self._rendering = False
        self._set_error('')

    @pyqtSlot(object)
    def _on_queue_changed(self, value: object) -> None:
        if not all(hasattr(value, name) for name in ('pending', 'retrying', 'paused', 'failed')):
            return
        counts = cast(_QueueCounts, value)
        self.pending_count_label.setText(str(counts.pending))
        self.retrying_count_label.setText(str(counts.retrying))
        self.paused_count_label.setText(str(counts.paused))
        self.failed_count_label.setText(str(counts.failed))

    @pyqtSlot(object)
    def _on_failed_jobs_changed(self, value: object) -> None:
        if not isinstance(value, tuple):
            return
        jobs = cast('tuple[_FailedJob, ...]', value)
        self.failed_model.set_jobs(jobs)
        self._render_failed_actions()

    def _render_failed_actions(self) -> None:
        for row in range(self.failed_model.rowCount()):
            job_id = self.failed_model.job_id(row)
            roast_uuid = self.failed_model.roast_uuid(row)
            if job_id is None or roast_uuid is None:
                continue
            retry = QPushButton(_tr('Retry'), self.failed_view)
            retry.setAccessibleName(f'{_tr("Retry")} {roast_uuid}')
            retry.clicked.connect(partial(self._retry_job, job_id))
            self.failed_view.setIndexWidget(self.failed_model.index(row, 5), retry)
            remove = QPushButton(_tr('Remove'), self.failed_view)
            remove.setAccessibleName(f'{_tr("Remove")} {roast_uuid}')
            remove.clicked.connect(partial(self._remove_job, job_id))
            self.failed_view.setIndexWidget(self.failed_model.index(row, 6), remove)
        self.failed_view.resizeColumnsToContents()

    def _retry_job(self, job_id: str) -> None:
        try:
            self._controller.retry_job(job_id)
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))

    def _remove_job(self, job_id: str) -> None:
        try:
            self._controller.remove_job(job_id)
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))

    @pyqtSlot(object)
    def _on_cache_stats_changed(self, value: object) -> None:
        if not hasattr(value, 'byte_count') or not hasattr(value, 'revision_count'):
            return
        stats = cast(_CacheStats, value)
        self.cache_label.setText(_format_cache(stats.byte_count, stats.revision_count))

    @pyqtSlot(str, object)
    def _on_operation_failed(self, operation: str, value: object) -> None:
        if not _looks_like_failure(value):
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
            return
        failure = cast(PublicFailure, value)
        if failure.kind is FailureKind.KEYRING:
            self._set_error(_tr(KEYRING_FAILURE_MESSAGE))
        else:
            self._set_error(failure.message)
        if (
            operation == self._testing_operation
            or failure.kind in {FailureKind.CREDENTIAL_REJECTED, FailureKind.KEYRING}
        ):
            self._testing_operation = None
            self._clear_candidate()
            self._invalidate_proof(persist_automatic_off=False)

    def _has_current_proof(self) -> bool:
        return self._identity is not None and self._proof_origin == self._settings.origin

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)

    def _clear_candidate(self) -> None:
        self._rendering = True
        try:
            self.credential_edit.clear()
        finally:
            self._rendering = False

    def _prepare_hide(self) -> None:
        self._clear_candidate()
        try:
            self._controller.save_configuration_geometry(self.saveGeometry())
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))

    @override
    def reject(self) -> None:
        self._prepare_hide()
        self.hide()

    @override
    def closeEvent(self, a0: QCloseEvent | None) -> None:
        self._prepare_hide()
        if a0 is not None:
            a0.accept()
        super().closeEvent(a0)

    @override
    def showEvent(self, a0: QShowEvent | None) -> None:
        super().showEvent(a0)
        self._bound_to_screen()

    def _bound_to_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        current = self.frameGeometry()
        width = min(current.width(), available.width())
        height = min(current.height(), available.height())
        left = min(max(current.left(), available.left()), available.right() - width + 1)
        top = min(max(current.top(), available.top()), available.bottom() - height + 1)
        bounded = QRect(left, top, width, height)
        if current != bounded:
            self.resize(width, height)
            frame_offset = self.frameGeometry().topLeft() - self.geometry().topLeft()
            self.move(bounded.topLeft() - frame_offset)


def _format_next_attempt(value: datetime | None) -> str:
    if value is None:
        return _tr('Not scheduled')
    return value.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')


def _format_cache(byte_count: int, revision_count: int) -> str:
    if byte_count >= _MEBIBYTE:
        amount = f'{byte_count / _MEBIBYTE:.1f} MiB'
    else:
        amount = f'{byte_count} {_tr("bytes")}'
    noun = _tr('revision') if revision_count == 1 else _tr('revisions')
    return f'{amount} ({revision_count} {noun})'


def _looks_like_settings(value: object) -> bool:
    return all(
        hasattr(value, name)
        for name in (
            'origin',
            'enabled',
            'automatic_upload',
            'identity',
            'cache_limit_bytes',
            'configuration_geometry',
            'pending_connection',
        )
    )


def _looks_like_identity(value: object) -> bool:
    return all(hasattr(value, name) for name in ('user', 'organization', 'role'))


def _looks_like_failure(value: object) -> bool:
    return all(hasattr(value, name) for name in ('kind', 'message', 'retryable'))


__all__ = ['FailedJobsModel', 'RoastServerConfigDialog']
