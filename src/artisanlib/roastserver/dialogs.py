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
from datetime import UTC, datetime, time
from functools import partial
from typing import Final, Protocol, cast, override
from uuid import UUID

from PyQt6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QDate,
    QModelIndex,
    QObject,
    QRect,
    Qt,
    QTimer,
    pyqtSlot,
)
from PyQt6.QtGui import QCloseEvent, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QMessageBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from artisanlib.roastserver.cache import CachedRevision
from artisanlib.roastserver.contract import (
    MAX_ARCHIVE_MACHINE_CHARS,
    MAX_ARCHIVE_SEARCH_CHARS,
    ArchiveFilters,
    FailureKind,
    PublicFailure,
    RoastState,
    ServerIdentity,
    ServerProfileSource,
)
from artisanlib.roastserver.controller import ArchivePageView, ArchiveRow
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
_MAX_BROWSER_ROWS: Final[int] = 5_000
_SEARCH_DEBOUNCE_MS: Final[int] = 300


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


class RoastServerBrowserController(Protocol):
    archivePageReady: _Signal
    operationFailed: _Signal
    onlineChanged: _Signal
    profileReady: _Signal
    cachedFallbackReady: _Signal
    identityChanged: _Signal

    def browse(self, filters: ArchiveFilters, refresh: bool = True) -> str: ...
    def load_more(self) -> str | None: ...
    def open_roast(self, roast_uuid: UUID) -> str: ...
    def open_cached(self, cached: CachedRevision) -> str: ...
    def cancel_open(self, request_id: str) -> None: ...
    def save_browser_geometry(self, geometry: QByteArray) -> None: ...


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
    def cancel_connection_test(self, request_id: str) -> None: ...
    def remove_credential(self) -> None: ...
    def refresh_queue(self) -> None: ...
    def retry_job(self, job_id: str) -> None: ...
    def remove_job(self, job_id: str) -> None: ...
    def clear_unused_cache(self) -> None: ...
    def save_configuration_geometry(self, geometry: QByteArray) -> None: ...


def _tr(text: str) -> str:
    return QApplication.translate('RoastServer', text)


class RoastTableModel(QAbstractTableModel):
    RoastUuidRole: Final[int] = int(Qt.ItemDataRole.UserRole) + 1
    CachedRevisionRole: Final[int] = int(Qt.ItemDataRole.UserRole) + 2
    StaleRole: Final[int] = int(Qt.ItemDataRole.UserRole) + 3

    _HEADERS: Final[tuple[str, ...]] = (
        'Roast date',
        'Title',
        'Batch',
        'Machine',
        'Labels',
        'Parse state',
        'Revisions',
        'Cache',
    )

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        max_rows: int = _MAX_BROWSER_ROWS,
    ) -> None:
        super().__init__(parent)
        self._rows: tuple[ArchiveRow, ...] = ()
        self._next_cursor: str | None = None
        self._max_rows = max(1, max_rows)

    @override
    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

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
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.RoastUuidRole:
            return row.roast.roast_uuid
        if role == self.CachedRevisionRole:
            return row.cached
        if role == self.StaleRole:
            return row.stale
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return ', '.join(self._display_values(row))
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = self._display_values(row)
        if not 0 <= index.column() < len(values):
            return None
        return values[index.column()]

    @override
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_page(self, page: ArchivePageView, *, append: bool) -> None:
        combined = (*self._rows, *page.rows) if append else page.rows
        by_uuid: dict[UUID, ArchiveRow] = {}
        for row in combined:
            by_uuid[row.roast.roast_uuid] = row
        ordered = sorted(
            by_uuid.values(),
            key=lambda row: (row.roast.roast_at, row.roast.roast_uuid.hex),
            reverse=True,
        )
        bounded = tuple(ordered[: self._max_rows])
        self.beginResetModel()
        self._rows = bounded
        self._next_cursor = (
            page.next_cursor
            if len(ordered) <= self._max_rows and len(bounded) < self._max_rows
            else None
        )
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = ()
        self._next_cursor = None
        self.endResetModel()

    def row(self, row: int) -> ArchiveRow | None:
        if not 0 <= row < len(self._rows):
            return None
        return self._rows[row]

    def roast_uuids(self) -> tuple[UUID, ...]:
        return tuple(row.roast.roast_uuid for row in self._rows)

    def has_more(self) -> bool:
        return self._next_cursor is not None and len(self._rows) < self._max_rows

    @staticmethod
    def _display_values(row: ArchiveRow) -> tuple[str, ...]:
        roast = row.roast
        labels = ', '.join(label.name for label in roast.labels)
        states: dict[RoastState, str] = {
            'awaiting_profile': _tr('Awaiting profile'),
            'parsed': _tr('Parsed'),
            'parse_failed': _tr('Parse failed'),
        }
        if row.cached_revision is None:
            cache = _tr('Not cached')
        elif row.stale:
            cache = _tr('Stale cached copy')
        else:
            cache = _tr('Cached')
        return (
            roast.roast_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC'),
            roast.title or '',
            _format_batch(
                roast.batch_prefix,
                roast.batch_number,
                roast.batch_position,
            ),
            roast.machine or '',
            labels,
            states[roast.state],
            str(roast.revision_count),
            cache,
        )


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
        self._ignored_operations: set[str] = set()
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
        close_button = self.button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is None:
            raise RuntimeError('Close button is unavailable.')

        self.test_button.setDefault(True)
        self.test_button.setAutoDefault(True)
        for button in (
            self.remove_credential_button,
            self.refresh_button,
            self.clear_cache_button,
            close_button,
        ):
            button.setAutoDefault(False)

        QWidget.setTabOrder(self.server_edit, self.credential_edit)
        QWidget.setTabOrder(self.credential_edit, self.test_button)
        QWidget.setTabOrder(self.test_button, self.automatic_upload_check)
        QWidget.setTabOrder(self.automatic_upload_check, self.enabled_check)
        QWidget.setTabOrder(self.enabled_check, self.cache_limit_spin)
        QWidget.setTabOrder(self.cache_limit_spin, close_button)

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
        self._connection_dirty = True
        self._testing_operation = None
        self._invalidate_proof(
            persist_automatic_off=True,
            invalidate_controller=True,
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
        if (
            (self._connection_dirty and self._testing_operation is None)
            or not _looks_like_identity(value)
            or value != self._settings.identity
        ):
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
        if operation in self._ignored_operations:
            return
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
        operation = self._testing_operation
        self._testing_operation = None
        if operation is not None:
            self._ignored_operations.add(operation)
            self._connection_dirty = True
            try:
                self._controller.cancel_connection_test(operation)
            except RuntimeError:
                pass
        self._clear_candidate()
        self._set_error('')
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
        QTimer.singleShot(0, self._bound_to_screen)

    def _bound_to_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        client = self.geometry()
        horizontal_frame_margin = max(0, frame.width() - client.width())
        vertical_frame_margin = max(0, frame.height() - client.height())
        if frame.width() > available.width() or frame.height() > available.height():
            client_width = max(1, available.width() - horizontal_frame_margin)
            client_height = max(1, available.height() - vertical_frame_margin)
            self.resize(client_width, client_height)
            frame = self.frameGeometry()
        width = min(frame.width(), available.width())
        height = min(frame.height(), available.height())
        left = min(max(frame.left(), available.left()), available.right() - width + 1)
        top = min(max(frame.top(), available.top()), available.bottom() - height + 1)
        bounded_top_left = QRect(left, top, width, height).topLeft()
        if frame.topLeft() != bounded_top_left:
            self.move(bounded_top_left)


class RoastServerBrowserDialog(QDialog):
    search_label: QLabel
    search_edit: QLineEdit
    state_combo: QComboBox
    machine_label: QLabel
    machine_edit: QLineEdit
    start_date_check: QCheckBox
    start_date_edit: QDateEdit
    end_date_check: QCheckBox
    end_date_edit: QDateEdit
    refresh_button: QPushButton
    status_label: QLabel
    error_label: QLabel
    roast_model: RoastTableModel
    roast_view: QTableView
    load_more_button: QPushButton
    progress_bar: QProgressBar
    cancel_open_button: QPushButton
    open_button: QPushButton
    button_box: QDialogButtonBox

    def __init__(
        self,
        controller: object,
        settings: ConnectorSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = cast(RoastServerBrowserController, controller)
        self._refresh_request: str | None = None
        self._more_request: str | None = None
        self._open_request: str | None = None
        self._open_expected: tuple[UUID, int, str | None] | None = None
        self._online = False
        self._shown_once = False
        self._view_generation = 0
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DEBOUNCE_MS)

        self.setWindowTitle(_tr('Server Roasts'))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(760, 520)
        self.resize(1_040, 700)
        self._build_ui()
        self._connect_controller()
        if settings is not None and settings.browser_geometry is not None:
            self.restoreGeometry(settings.browser_geometry)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        filters_group = QGroupBox(_tr('Filters'), self)
        filters = QFormLayout(filters_group)

        self.search_label = QLabel(_tr('&Search:'), filters_group)
        self.search_edit = QLineEdit(filters_group)
        self.search_edit.setAccessibleName(_tr('Search server roasts'))
        self.search_edit.setMaxLength(MAX_ARCHIVE_SEARCH_CHARS * 5)
        self.search_label.setBuddy(self.search_edit)
        filters.addRow(self.search_label, self.search_edit)

        state_label = QLabel(_tr('Parse &state:'), filters_group)
        self.state_combo = QComboBox(filters_group)
        self.state_combo.setAccessibleName(_tr('Parse state'))
        self.state_combo.addItem(_tr('All parse states'), None)
        self.state_combo.addItem(_tr('Awaiting profile'), 'awaiting_profile')
        self.state_combo.addItem(_tr('Parsed'), 'parsed')
        self.state_combo.addItem(_tr('Parse failed'), 'parse_failed')
        state_label.setBuddy(self.state_combo)
        filters.addRow(state_label, self.state_combo)

        self.machine_label = QLabel(_tr('&Machine:'), filters_group)
        self.machine_edit = QLineEdit(filters_group)
        self.machine_edit.setAccessibleName(_tr('Machine'))
        self.machine_edit.setMaxLength(MAX_ARCHIVE_MACHINE_CHARS * 5)
        self.machine_label.setBuddy(self.machine_edit)
        filters.addRow(self.machine_label, self.machine_edit)

        start_row = QWidget(filters_group)
        start_layout = QHBoxLayout(start_row)
        start_layout.setContentsMargins(0, 0, 0, 0)
        self.start_date_check = QCheckBox(_tr('From UTC'), start_row)
        self.start_date_edit = QDateEdit(QDate.currentDate(), start_row)
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat('yyyy-MM-dd')
        self.start_date_edit.setAccessibleName(_tr('Start roast date UTC'))
        self.start_date_edit.setEnabled(False)
        start_layout.addWidget(self.start_date_check)
        start_layout.addWidget(self.start_date_edit)
        start_layout.addStretch(1)
        filters.addRow(_tr('Start date:'), start_row)

        end_row = QWidget(filters_group)
        end_layout = QHBoxLayout(end_row)
        end_layout.setContentsMargins(0, 0, 0, 0)
        self.end_date_check = QCheckBox(_tr('Through UTC'), end_row)
        self.end_date_edit = QDateEdit(QDate.currentDate(), end_row)
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat('yyyy-MM-dd')
        self.end_date_edit.setAccessibleName(_tr('End roast date UTC'))
        self.end_date_edit.setEnabled(False)
        end_layout.addWidget(self.end_date_check)
        end_layout.addWidget(self.end_date_edit)
        end_layout.addStretch(1)
        filters.addRow(_tr('End date:'), end_row)

        filter_actions = QHBoxLayout()
        filter_actions.addStretch(1)
        self.refresh_button = QPushButton(_tr('&Refresh'), filters_group)
        self.refresh_button.setAccessibleName(_tr('Refresh server roasts'))
        filter_actions.addWidget(self.refresh_button)
        filters.addRow(filter_actions)
        layout.addWidget(filters_group)

        self.status_label = QLabel(_tr('Offline.'), self)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setOpenExternalLinks(False)
        self.status_label.setAccessibleName(_tr('Server archive status'))
        layout.addWidget(self.status_label)

        self.roast_model = RoastTableModel(self)
        self.roast_view = QTableView(self)
        self.roast_view.setModel(self.roast_model)
        self.roast_view.setAccessibleName(_tr('Server roast archive'))
        self.roast_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.roast_view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.roast_view.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.roast_view.setAlternatingRowColors(True)
        self.roast_view.setSortingEnabled(False)
        vertical_header = self.roast_view.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        horizontal_header = self.roast_view.horizontalHeader()
        if horizontal_header is not None:
            horizontal_header.setStretchLastSection(True)
        layout.addWidget(self.roast_view, 1)

        paging_row = QHBoxLayout()
        paging_row.addStretch(1)
        self.load_more_button = QPushButton(_tr('Load &more'), self)
        self.load_more_button.setAccessibleName(_tr('Load more server roasts'))
        self.load_more_button.setEnabled(False)
        paging_row.addWidget(self.load_more_button)
        layout.addLayout(paging_row)

        self.error_label = QLabel('', self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setOpenExternalLinks(False)
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName(_tr('Server archive error'))
        layout.addWidget(self.error_label)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setAccessibleName(_tr('Opening server roast'))
        self.progress_bar.hide()
        self.cancel_open_button = QPushButton(_tr('Cancel open'), self)
        self.cancel_open_button.setAccessibleName(_tr('Cancel opening server roast'))
        self.cancel_open_button.hide()
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.cancel_open_button)
        layout.addLayout(progress_row)

        action_row = QHBoxLayout()
        self.open_button = QPushButton(_tr('&Open'), self)
        self.open_button.setAccessibleName(_tr('Open selected server roast'))
        self.open_button.setEnabled(False)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self
        )
        close_button = self.button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is None:
            raise RuntimeError('Close button is unavailable.')
        action_row.addStretch(1)
        action_row.addWidget(self.open_button)
        action_row.addWidget(self.button_box)
        layout.addLayout(action_row)

        self.open_button.setDefault(True)
        self.open_button.setAutoDefault(True)
        for button in (
            self.refresh_button,
            self.load_more_button,
            self.cancel_open_button,
            close_button,
        ):
            button.setAutoDefault(False)

        QWidget.setTabOrder(self.search_edit, self.state_combo)
        QWidget.setTabOrder(self.state_combo, self.machine_edit)
        QWidget.setTabOrder(self.machine_edit, self.start_date_check)
        QWidget.setTabOrder(self.start_date_check, self.start_date_edit)
        QWidget.setTabOrder(self.start_date_edit, self.end_date_check)
        QWidget.setTabOrder(self.end_date_check, self.end_date_edit)
        QWidget.setTabOrder(self.end_date_edit, self.refresh_button)
        QWidget.setTabOrder(self.refresh_button, self.roast_view)
        QWidget.setTabOrder(self.roast_view, self.load_more_button)
        QWidget.setTabOrder(self.load_more_button, self.open_button)
        QWidget.setTabOrder(self.open_button, close_button)

        self._search_timer.timeout.connect(self._refresh)
        self.search_edit.textChanged.connect(self._schedule_refresh)
        self.state_combo.currentIndexChanged.connect(self._schedule_refresh)
        self.machine_edit.editingFinished.connect(self._refresh)
        self.start_date_check.toggled.connect(self._start_date_toggled)
        self.end_date_check.toggled.connect(self._end_date_toggled)
        self.start_date_edit.dateChanged.connect(self._date_changed)
        self.end_date_edit.dateChanged.connect(self._date_changed)
        self.refresh_button.clicked.connect(self._refresh)
        selection_model = self.roast_view.selectionModel()
        if selection_model is not None:
            selection_model.currentChanged.connect(self._selection_changed)
        self.load_more_button.clicked.connect(self._load_more)
        scroll = self.roast_view.verticalScrollBar()
        if scroll is not None:
            scroll.valueChanged.connect(self._scroll_changed)
        self.open_button.clicked.connect(self._open_selected)
        self.cancel_open_button.clicked.connect(self._cancel_open)
        self.button_box.rejected.connect(self.reject)

    def _connect_controller(self) -> None:
        self._controller.archivePageReady.connect(self._on_archive_page)
        self._controller.operationFailed.connect(self._on_operation_failed)
        self._controller.onlineChanged.connect(self._on_online_changed)
        self._controller.profileReady.connect(self._on_profile_ready)
        fallback_signal = getattr(self._controller, 'cachedFallbackReady', None)
        if hasattr(fallback_signal, 'connect'):
            cast(_Signal, fallback_signal).connect(self._on_cached_fallback)
        identity_signal = getattr(self._controller, 'identityChanged', None)
        if hasattr(identity_signal, 'connect'):
            cast(_Signal, identity_signal).connect(self._on_identity_changed)

    @pyqtSlot()
    def _schedule_refresh(self) -> None:
        self._search_timer.start()

    @pyqtSlot(bool)
    def _start_date_toggled(self, checked: bool) -> None:
        self.start_date_edit.setEnabled(checked)
        self._schedule_refresh()

    @pyqtSlot(bool)
    def _end_date_toggled(self, checked: bool) -> None:
        self.end_date_edit.setEnabled(checked)
        self._schedule_refresh()

    @pyqtSlot()
    def _date_changed(self) -> None:
        self._schedule_refresh()

    def _filters(self) -> ArchiveFilters:
        search = self.search_edit.text().strip()[:MAX_ARCHIVE_SEARCH_CHARS]
        machine = self.machine_edit.text().strip()[:MAX_ARCHIVE_MACHINE_CHARS]
        state = cast(RoastState | None, self.state_combo.currentData())
        start = (
            datetime.combine(self.start_date_edit.date().toPyDate(), time.min, UTC)
            if self.start_date_check.isChecked()
            else None
        )
        end = (
            datetime.combine(self.end_date_edit.date().toPyDate(), time.max, UTC)
            if self.end_date_check.isChecked()
            else None
        )
        return ArchiveFilters(
            search=search or None,
            state=state,
            machine=machine or None,
            roast_at_from=start,
            roast_at_to=end,
        )

    @pyqtSlot()
    def _refresh(self) -> None:
        self._search_timer.stop()
        self._view_generation += 1
        self._more_request = None
        try:
            self._refresh_request = self._controller.browse(
                self._filters(), refresh=True
            )
        except RuntimeError:
            self._refresh_request = None
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
            return
        self.refresh_button.setEnabled(False)
        self.load_more_button.setEnabled(False)

    @pyqtSlot()
    def _load_more(self) -> None:
        if self._more_request is not None or not self.roast_model.has_more():
            return
        try:
            self._more_request = self._controller.load_more()
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
            return
        if self._more_request is not None:
            self.load_more_button.setEnabled(False)

    @pyqtSlot(int)
    def _scroll_changed(self, value: int) -> None:
        scroll = self.roast_view.verticalScrollBar()
        if scroll is None:
            return
        proximity = max(2, scroll.pageStep() // 5)
        if scroll.maximum() - value <= proximity and self.load_more_button.isEnabled():
            self._load_more()

    @pyqtSlot(str, object)
    def _on_archive_page(self, request_id: str, value: object) -> None:
        if not isinstance(value, ArchivePageView):
            return
        append = request_id == self._more_request
        refresh = request_id == self._refresh_request
        if not append and not refresh:
            return
        self._online = value.online
        if value.retained_error is not None and self.roast_model.rowCount() > 0:
            self._set_error(value.retained_error.message)
        else:
            self.roast_model.set_page(value, append=append)
            self._set_error(
                '' if value.retained_error is None else value.retained_error.message
            )
        if append:
            self._more_request = None
        else:
            self._refresh_request = None
            self.refresh_button.setEnabled(True)
        self._render_connection_status()
        self._render_load_more()
        self._selection_changed()

    @pyqtSlot(str, object)
    def _on_operation_failed(self, operation: str, value: object) -> None:
        failure = (
            value
            if isinstance(value, PublicFailure)
            else PublicFailure(
                FailureKind.INVALID_RESPONSE,
                FailureKind.INVALID_RESPONSE.value,
                _tr(_GENERIC_OPERATION_FAILURE),
                False,
            )
        )
        if operation in {self._refresh_request, 'refresh', 'browse'}:
            self.refresh_button.setEnabled(True)
            self._set_error(failure.message)
            self._render_load_more()
        elif operation == self._more_request:
            self._set_error(failure.message)
            self._more_request = None
            self._render_load_more()
        elif operation == self._open_request:
            self._set_error(failure.message)
            self._finish_open(clear_request=not failure.retryable)

    @pyqtSlot(bool)
    def _on_online_changed(self, online: bool) -> None:
        self._online = online
        self._render_connection_status()
        self._selection_changed()

    @pyqtSlot(QModelIndex, QModelIndex)
    def _selection_changed(
        self,
        _current: QModelIndex = _ROOT_INDEX,
        _previous: QModelIndex = _ROOT_INDEX,
    ) -> None:
        row = self._selected_row()
        enabled = (
            not self.progress_bar.isVisible()
            and row is not None
            and row.roast.state != 'awaiting_profile'
            and (self._online or row.cached is not None)
        )
        self.open_button.setEnabled(enabled)

    def _selected_row(self) -> ArchiveRow | None:
        current = self.roast_view.currentIndex()
        if not current.isValid():
            return None
        return self.roast_model.row(current.row())

    def select_roast(self, roast_uuid: UUID) -> None:
        try:
            row = self.roast_model.roast_uuids().index(roast_uuid)
        except ValueError:
            return
        self.roast_view.selectRow(row)
        self.roast_view.setCurrentIndex(self.roast_model.index(row, 0))
        self._selection_changed()

    @pyqtSlot()
    def _open_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.roast.state == 'awaiting_profile':
            return
        try:
            if self._online:
                request_id = self._controller.open_roast(row.roast.roast_uuid)
                expected_sha = (
                    row.cached_sha256
                    if row.cached_revision == row.roast.revision_count
                    else None
                )
            elif row.cached is not None:
                request_id = self._controller.open_cached(row.cached)
                expected_sha = row.cached.revision.sha256
            else:
                return
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
            return
        self._start_open(
            request_id,
            row.roast.roast_uuid,
            row.roast.revision_count,
            expected_sha,
        )

    def _start_open(
        self,
        request_id: str,
        roast_uuid: UUID,
        revision_number: int,
        sha256: str | None,
    ) -> None:
        self._open_request = request_id
        self._open_expected = (roast_uuid, revision_number, sha256)
        self.progress_bar.show()
        self.cancel_open_button.show()
        self.open_button.setEnabled(False)
        self._set_error('')

    @pyqtSlot()
    def _cancel_open(self) -> None:
        request_id = self._open_request
        cancel = getattr(self._controller, 'cancel_open', None)
        if request_id is not None and callable(cancel):
            try:
                cast(Callable[[str], None], cancel)(request_id)
            except RuntimeError:
                self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
        self._finish_open()

    @pyqtSlot(str, object)
    def _on_profile_ready(self, _path: str, value: object) -> None:
        expected = self._open_expected
        if self._open_request is None or expected is None:
            return
        if not isinstance(value, ServerProfileSource):
            return
        roast_uuid, revision_number, sha256 = expected
        if (
            value.roast_uuid != roast_uuid
            or value.revision_number != revision_number
            or (sha256 is not None and value.sha256 != sha256)
        ):
            return
        if value.stale:
            self.status_label.setText(
                _tr('Opened verified cached revision {revision} (stale).').format(
                    revision=value.revision_number
                )
            )
        else:
            self.status_label.setText(
                _tr('Opened verified server revision {revision}.').format(
                    revision=value.revision_number
                )
            )
        self._finish_open()

    @pyqtSlot(str, object)
    def _on_cached_fallback(self, request_id: str, value: object) -> None:
        expected = self._open_expected
        if (
            request_id != self._open_request
            or expected is None
            or not isinstance(value, CachedRevision)
            or value.roast.roast_uuid != expected[0]
            or value.revision.revision_number != expected[1]
            or (expected[2] is not None and value.revision.sha256 != expected[2])
        ):
            return
        if not self._confirm_cached_fallback(value):
            self._finish_open()
            return
        try:
            cached_request = self._controller.open_cached(value)
        except RuntimeError:
            self._set_error(_tr(_GENERIC_OPERATION_FAILURE))
            self._finish_open()
            return
        self._start_open(
            cached_request,
            value.roast.roast_uuid,
            value.revision.revision_number,
            value.revision.sha256,
        )

    def _confirm_cached_fallback(self, _cached: CachedRevision) -> bool:
        message = QMessageBox(self)
        message.setWindowTitle(_tr('Roast Server'))
        message.setIcon(QMessageBox.Icon.Question)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText(
            _tr(
                'The server is unavailable. Open the previously verified cached copy?'
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        message.setDefaultButton(QMessageBox.StandardButton.No)
        return message.exec() == QMessageBox.StandardButton.Yes

    def _finish_open(self, *, clear_request: bool = True) -> None:
        if clear_request:
            self._open_request = None
            self._open_expected = None
        self.progress_bar.hide()
        self.cancel_open_button.hide()
        self._selection_changed()

    @pyqtSlot(object)
    def _on_identity_changed(self, _identity: object) -> None:
        self._view_generation += 1
        self._refresh_request = None
        self._more_request = None
        self._cancel_open()
        self.roast_model.clear()
        self.roast_view.clearSelection()
        self.roast_view.setCurrentIndex(QModelIndex())
        self.load_more_button.setEnabled(False)
        self._online = False
        self._set_error('')
        self._render_connection_status()

    def _render_load_more(self) -> None:
        self.load_more_button.setEnabled(
            self._more_request is None and self.roast_model.has_more()
        )

    def _render_connection_status(self) -> None:
        if self._online:
            self.status_label.setText(_tr('Online.'))
        elif self.roast_model.rowCount() > 0:
            self.status_label.setText(_tr('Offline — cached copies may be stale.'))
        else:
            self.status_label.setText(_tr('Offline.'))

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)

    def _prepare_hide(self) -> None:
        self._search_timer.stop()
        self._view_generation += 1
        self._refresh_request = None
        self._more_request = None
        self._cancel_open()
        save_geometry = getattr(self._controller, 'save_browser_geometry', None)
        if callable(save_geometry):
            try:
                cast(Callable[[QByteArray], None], save_geometry)(self.saveGeometry())
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
        QTimer.singleShot(0, self._bound_to_screen)
        if not self._shown_once or self._refresh_request is None:
            self._shown_once = True
            QTimer.singleShot(0, self._refresh)

    def _bound_to_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        client = self.geometry()
        horizontal_frame_margin = max(0, frame.width() - client.width())
        vertical_frame_margin = max(0, frame.height() - client.height())
        if frame.width() > available.width() or frame.height() > available.height():
            self.resize(
                max(1, available.width() - horizontal_frame_margin),
                max(1, available.height() - vertical_frame_margin),
            )
            frame = self.frameGeometry()
        width = min(frame.width(), available.width())
        height = min(frame.height(), available.height())
        left = min(max(frame.left(), available.left()), available.right() - width + 1)
        top = min(max(frame.top(), available.top()), available.bottom() - height + 1)
        bounded_top_left = QRect(left, top, width, height).topLeft()
        if frame.topLeft() != bounded_top_left:
            self.move(bounded_top_left)


def _format_batch(
    prefix: str | None,
    number: int | None,
    position: int | None,
) -> str:
    result = prefix or ''
    if number is not None:
        result = f'{result}{number}'
    if position is not None:
        result = f'{result}/{position}' if result else str(position)
    return result


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


__all__ = [
    'ArchivePageView',
    'ArchiveRow',
    'FailedJobsModel',
    'RoastServerBrowserDialog',
    'RoastServerConfigDialog',
    'RoastTableModel',
]
