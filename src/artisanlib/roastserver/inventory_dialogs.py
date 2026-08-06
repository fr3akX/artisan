#
# ABOUT
# Artisan Roast Server inventory dialogs
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

from collections.abc import Callable
from datetime import datetime
from typing import Final, Literal, Protocol, cast, override
from uuid import UUID

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    pyqtSlot,
)
from PyQt6.QtGui import QBrush, QColor, QCloseEvent, QKeyEvent, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from artisanlib.roastserver.contract import Namespace, PublicFailure
from artisanlib.roastserver.inventory_contract import BeanLot
from artisanlib.roastserver.inventory_store import (
    InterruptedReservation,
    LotCacheSnapshot,
)
from artisanlib.util import render_weight

_ROOT_INDEX: Final[QModelIndex] = QModelIndex()
_WARNING_BRUSH: Final[QBrush] = QBrush(QColor('#b91c1c'))


def _tr(text: str) -> str:
    return QApplication.translate('RoastServerInventory', text)


class _Signal(Protocol):
    def connect(self, slot: Callable[..., object]) -> object: ...
    def disconnect(self, slot: Callable[..., object]) -> object: ...


class InventoryRecoveryDialogController(Protocol):
    inventoryRecoveryRequired: _Signal

    def resolve_interrupted_inventory(
        self,
        roast_uuid: UUID,
        action: Literal['finalize', 'release', 'keep'],
    ) -> object: ...


class InterruptedReservationsModel(QAbstractTableModel):
    RecordRole: Final[int] = int(Qt.ItemDataRole.UserRole) + 1
    _HEADERS: Final[tuple[str, ...]] = (
        'Lot',
        'Roast',
        'Planned',
        'Namespace',
        'Status',
    )
    _STATES: Final[dict[str, str]] = {
        'reserve_queued': 'Reserve queued',
        'reserved': 'Reserved',
        'finalize_queued': 'Finalize queued',
        'finalized': 'Finalized',
        'release_queued': 'Release queued',
        'released': 'Released',
        'paused': 'Paused',
        'failed': 'Failed',
    }

    def __init__(
        self,
        records: tuple[InterruptedReservation, ...] = (),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._records = tuple(records)

    @property
    def records(self) -> tuple[InterruptedReservation, ...]:
        return self._records

    def replace_records(self, records: tuple[InterruptedReservation, ...]) -> None:
        self.beginResetModel()
        self._records = tuple(records)
        self.endResetModel()

    def record_at(self, row: int) -> InterruptedReservation | None:
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    @override
    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._records)

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
        record = self.record_at(index.row()) if index.isValid() else None
        if record is None:
            return None
        if role == self.RecordRole:
            return record
        values = self._display_values(record)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return ', '.join(values)
        if role == Qt.ItemDataRole.DisplayRole and 0 <= index.column() < len(values):
            return values[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    @override
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    @classmethod
    def _display_values(cls, record: InterruptedReservation) -> tuple[str, ...]:
        namespace = _tr('{origin} — organization {organization}').format(
            origin=record.namespace.origin,
            organization=str(record.namespace.organization_id),
        )
        return (
            record.lot_name,
            str(record.roast_uuid),
            render_weight(float(record.planned_grams), 0, 0),
            namespace,
            _tr(cls._STATES[record.lifecycle]),
        )


class InterruptedReservationsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        controller: object,
        records: tuple[InterruptedReservation, ...],
        active_namespace: Namespace | None,
    ) -> None:
        super().__init__(parent)
        self._controller = cast(InventoryRecoveryDialogController, controller)
        self._active_namespace = active_namespace
        self._pending: tuple[Namespace, UUID, Literal['finalize', 'release', 'keep']] | None = None
        self._signals_connected = False

        self.setWindowTitle(_tr('Interrupted inventory reservations'))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        intro = QLabel(
            _tr('Choose how to resolve inventory reservations interrupted before completion.'),
            self,
        )
        intro.setTextFormat(Qt.TextFormat.PlainText)
        intro.setWordWrap(True)

        self.model = InterruptedReservationsModel(records, self)
        self.tableView = QTableView(self)
        self.tableView.setModel(self.model)
        self.tableView.setAccessibleName(_tr('Interrupted inventory reservations'))
        self.tableView.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tableView.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tableView.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        vertical_header = self.tableView.verticalHeader()
        if vertical_header is not None:
            vertical_header.hide()
        header = self.tableView.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self.noticeLabel = QLabel('', self)
        self.noticeLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.noticeLabel.setOpenExternalLinks(False)
        self.noticeLabel.setWordWrap(True)
        self.noticeLabel.setAccessibleName(_tr('Inventory recovery status'))

        self.finalizeButton = QPushButton(_tr('Finalize planned'), self)
        self.finalizeButton.setAccessibleName(_tr('Finalize selected reservation using planned weight'))
        self.releaseButton = QPushButton(_tr('Release'), self)
        self.releaseButton.setAccessibleName(_tr('Release selected inventory reservation'))
        self.keepButton = QPushButton(_tr('Keep pending'), self)
        self.keepButton.setAccessibleName(_tr('Keep selected inventory reservation pending'))
        self.buttonBox = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)

        actions = QHBoxLayout()
        actions.addWidget(self.finalizeButton)
        actions.addWidget(self.releaseButton)
        actions.addWidget(self.keepButton)
        actions.addStretch(1)
        actions.addWidget(self.buttonBox)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.tableView, 1)
        layout.addWidget(self.noticeLabel)
        layout.addLayout(actions)

        selection = self.tableView.selectionModel()
        if selection is not None:
            selection.selectionChanged.connect(self._selection_changed)
        self.finalizeButton.clicked.connect(lambda: self._resolve('finalize'))
        self.releaseButton.clicked.connect(lambda: self._resolve('release'))
        self.keepButton.clicked.connect(lambda: self._resolve('keep'))
        self.buttonBox.rejected.connect(self.reject)
        self._controller.inventoryRecoveryRequired.connect(self._recovery_changed)
        self._signals_connected = True
        if records:
            self.tableView.selectRow(0)
        self._selection_changed()
        self.resize(980, 430)

    def set_active_namespace(self, namespace: Namespace | None) -> None:
        self._active_namespace = namespace
        self._pending = None
        self._selection_changed()

    def clean_up(self) -> None:
        if not self._signals_connected:
            return
        self._signals_connected = False
        try:
            self._controller.inventoryRecoveryRequired.disconnect(self._recovery_changed)
        except (RuntimeError, TypeError):
            pass

    @pyqtSlot()
    def _selection_changed(self) -> None:
        record = self._selected_record()
        active = record is not None and record.namespace == self._active_namespace
        waiting = self._pending is not None
        self.finalizeButton.setEnabled(active and not waiting)
        self.releaseButton.setEnabled(active and not waiting)
        self.keepButton.setEnabled(record is not None and not waiting)
        if record is not None and not active and not waiting:
            self.noticeLabel.setText(
                _tr('This reservation is outside the current authenticated namespace. Only Keep pending is available.')
            )

    @pyqtSlot(object)
    def _recovery_changed(self, value: object) -> None:
        if not isinstance(value, tuple) or not all(
            isinstance(record, InterruptedReservation) for record in value
        ):
            return
        selected = self._selected_record()
        selected_key = None if selected is None else (selected.namespace, selected.roast_uuid)
        pending = self._pending
        records = cast('tuple[InterruptedReservation, ...]', value)
        self.model.replace_records(records)
        self._pending = None
        target_key = selected_key if pending is None else pending[:2]
        if target_key is not None:
            for row, record in enumerate(self.model.records):
                if (record.namespace, record.roast_uuid) == target_key:
                    self.tableView.selectRow(row)
                    break
        if pending is not None:
            action = pending[2]
            if action == 'keep':
                self.noticeLabel.setText(
                    _tr('Reservation kept pending. This notice remains until it is finalized or released.')
                )
            else:
                self.noticeLabel.setText(
                    _tr('Inventory recovery state updated.')
                )
        self._selection_changed()

    def _selected_record(self) -> InterruptedReservation | None:
        selection = self.tableView.selectionModel()
        if selection is None:
            return None
        rows = selection.selectedRows()
        return None if not rows else self.model.record_at(rows[0].row())

    def _resolve(self, action: Literal['finalize', 'release', 'keep']) -> None:
        if self._pending is not None:
            return
        record = self._selected_record()
        if record is None:
            return
        if record.namespace != self._active_namespace:
            self.noticeLabel.setText(
                _tr('This reservation cannot be changed from the current Roast Server organization. Keep pending remains selected.')
            )
            return
        self._pending = (record.namespace, record.roast_uuid, action)
        self.noticeLabel.setText(_tr('Waiting for inventory recovery state.'))
        self._selection_changed()
        try:
            self._controller.resolve_interrupted_inventory(record.roast_uuid, action)
        except (RuntimeError, ValueError):
            self._pending = None
            self.noticeLabel.setText(
                _tr('Inventory recovery could not be stored. The reservation remains pending.')
            )
            self._selection_changed()


class InventoryLotDialogController(Protocol):
    inventoryLotsChanged: _Signal
    inventoryRefreshFinished: _Signal
    operationFailed: _Signal
    onlineChanged: _Signal
    settingsChanged: _Signal
    identityChanged: _Signal

    def inventory_cache_snapshot(self) -> LotCacheSnapshot | None: ...
    def inventory_lots(self) -> tuple[BeanLot, ...]: ...
    def refresh_inventory_lots(self) -> str: ...


class BeanLotTableModel(QAbstractTableModel):
    LotRole: Final[int] = int(Qt.ItemDataRole.UserRole) + 1
    WarningRole: Final[int] = int(Qt.ItemDataRole.UserRole) + 2

    _HEADERS: Final[tuple[str, ...]] = (
        'Lot',
        'Origin',
        'Varietals',
        'Process',
        'Crop year',
        'Available',
        'Conflicts',
    )

    def __init__(
        self,
        lots: tuple[BeanLot, ...] = (),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._lots = tuple(lots)

    @property
    def lots(self) -> tuple[BeanLot, ...]:
        return self._lots

    def replace_lots(self, lots: tuple[BeanLot, ...]) -> None:
        frozen = tuple(lots)
        self.beginResetModel()
        self._lots = frozen
        self.endResetModel()

    def lot_at(self, row: int) -> BeanLot | None:
        if 0 <= row < len(self._lots):
            return self._lots[row]
        return None

    @override
    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._lots)

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
        lot = self.lot_at(index.row()) if index.isValid() else None
        if lot is None:
            return None
        if role == self.LotRole:
            return lot
        if role == self.WarningRole:
            return self._warning(lot)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(lot, index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() in {5, 6}:
                return self._warning(lot) or None
            return None
        if role == Qt.ItemDataRole.ForegroundRole and (
            (index.column() == 5 and lot.available_grams <= 0)
            or (index.column() == 6 and lot.unresolved_conflict_count > 0)
        ):
            return _WARNING_BRUSH
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in {4, 5, 6}:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _display(lot: BeanLot, column: int) -> str:
        values = (
            lot.name,
            lot.origin or '',
            ', '.join(lot.varietals),
            lot.processing_method or '',
            '' if lot.crop_year is None else str(lot.crop_year),
            render_weight(float(lot.available_grams), 0, 0),
            str(lot.unresolved_conflict_count),
        )
        return values[column] if 0 <= column < len(values) else ''

    @staticmethod
    def _warning(lot: BeanLot) -> str:
        warnings: list[str] = []
        if lot.available_grams < 0:
            warnings.append(_tr('Available inventory is negative.'))
        elif lot.available_grams == 0:
            warnings.append(_tr('No inventory is currently available.'))
        if lot.unresolved_conflict_count:
            warnings.append(
                _tr('{count} unresolved inventory conflict(s).').format(
                    count=lot.unresolved_conflict_count
                )
            )
        return ' '.join(warnings)


class _BeanLotFilterModel(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._needle = ''

    @pyqtSlot(str)
    def set_search_text(self, text: str) -> None:
        self._needle = text.casefold()
        self.invalidateFilter()

    @override
    def filterAcceptsRow(
        self,
        source_row: int,
        source_parent: QModelIndex,
    ) -> bool:
        del source_parent
        model = self.sourceModel()
        if not isinstance(model, BeanLotTableModel):
            return False
        lot = model.lot_at(source_row)
        if lot is None:
            return False
        if not self._needle:
            return True
        values = (
            lot.name,
            lot.origin or '',
            ' '.join(lot.varietals),
            lot.processing_method or '',
            '' if lot.crop_year is None else str(lot.crop_year),
        )
        return any(self._needle in value.casefold() for value in values)


class InventoryLotDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        controller: InventoryLotDialogController,
        *,
        selected_lot_id: UUID | None = None,
        online: bool = False,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        try:
            snapshot = controller.inventory_cache_snapshot()
        except RuntimeError:
            snapshot = None
            self._snapshot_read_failed = True
        else:
            self._snapshot_read_failed = False
        self._cached_at = None if snapshot is None else snapshot.refreshed_at
        self._online = online
        self._pending_refresh: str | None = None
        self._signals_connected = False
        self._shown = False
        self.selected_lot: BeanLot | None = None

        self.setModal(True)
        self.setWindowTitle(_tr('Choose inventory lot'))

        self.model = BeanLotTableModel(
            () if snapshot is None else snapshot.lots, self
        )
        self.proxyModel = _BeanLotFilterModel(self)
        self.proxyModel.setSourceModel(self.model)
        self.proxyModel.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.searchEdit = QLineEdit(self)
        self.searchEdit.setPlaceholderText(_tr('Search inventory lots'))
        self.searchEdit.setClearButtonEnabled(True)
        self.searchEdit.setAccessibleName(_tr('Search inventory lots'))
        self.searchEdit.textChanged.connect(self.proxyModel.set_search_text)

        self.tableView = QTableView(self)
        self.tableView.setModel(self.proxyModel)
        self.tableView.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tableView.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tableView.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tableView.setSortingEnabled(True)
        self.tableView.setAccessibleName(_tr('Inventory lots'))
        vertical_header = self.tableView.verticalHeader()
        if vertical_header is not None:
            vertical_header.hide()
        header = self.tableView.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self.statusLabel = QLabel(self)
        self.statusLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setAccessibleName(_tr('Inventory cache status'))
        self.warningLabel = QLabel(self)
        self.warningLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.warningLabel.setWordWrap(True)
        self.warningLabel.setAccessibleName(_tr('Inventory lot warning'))

        self.refreshButton = QPushButton(_tr('Refresh'), self)
        self.refreshButton.setAccessibleName(_tr('Refresh inventory lots'))
        self.refreshButton.clicked.connect(self.refresh)
        self.clearButton = QPushButton(_tr('Clear'), self)
        self.clearButton.setAccessibleName(_tr('Clear inventory lot selection'))
        self.clearButton.clicked.connect(self._clear)
        self.chooseButton = QPushButton(_tr('Choose'), self)
        self.chooseButton.setAccessibleName(_tr('Choose selected inventory lot'))
        self.chooseButton.setDefault(True)
        self.chooseButton.setEnabled(False)
        self.chooseButton.clicked.connect(self._choose)

        self.buttonBox = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.buttonBox.rejected.connect(self.reject)
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.refreshButton)
        button_layout.addWidget(self.clearButton)
        button_layout.addStretch()
        button_layout.addWidget(self.buttonBox)
        button_layout.addWidget(self.chooseButton)

        layout = QVBoxLayout(self)
        layout.addWidget(self.searchEdit)
        layout.addWidget(self.tableView)
        layout.addWidget(self.warningLabel)
        layout.addWidget(self.statusLabel)
        layout.addLayout(button_layout)

        selection_model = self.tableView.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._selection_changed)
        self.tableView.doubleClicked.connect(self._choose_index)
        self.tableView.activated.connect(self._choose_index)
        self._select_lot(selected_lot_id)
        if self._snapshot_read_failed:
            self._show_snapshot_unavailable(retained=False)
        else:
            self._update_status()
        self._connect_signals()
        self.resize(900, 430)

    def _connect_signals(self) -> None:
        self._controller.inventoryLotsChanged.connect(self._lots_changed)
        self._controller.inventoryRefreshFinished.connect(self._refresh_finished)
        self._controller.operationFailed.connect(self._operation_failed)
        self._controller.onlineChanged.connect(self._online_changed)
        self._controller.settingsChanged.connect(self._context_changed)
        self._controller.identityChanged.connect(self._context_changed)
        self._signals_connected = True

    def clean_up(self) -> None:
        if not self._signals_connected:
            return
        self._signals_connected = False
        for signal, slot in (
            (self._controller.inventoryLotsChanged, self._lots_changed),
            (self._controller.inventoryRefreshFinished, self._refresh_finished),
            (self._controller.operationFailed, self._operation_failed),
            (self._controller.onlineChanged, self._online_changed),
            (self._controller.settingsChanged, self._context_changed),
            (self._controller.identityChanged, self._context_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    @override
    def done(self, a0: int) -> None:
        self.clean_up()
        super().done(a0)

    @override
    def closeEvent(self, a0: QCloseEvent | None) -> None:
        self.clean_up()
        super().closeEvent(a0)

    @override
    def showEvent(self, a0: QShowEvent | None) -> None:
        super().showEvent(a0)
        if not self._shown:
            self._shown = True
            self.refresh()

    @override
    def keyPressEvent(self, a0: QKeyEvent | None) -> None:
        if (
            a0 is not None
            and a0.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
            and self.tableView.hasFocus()
            and self.chooseButton.isEnabled()
        ):
            self._choose()
            return
        super().keyPressEvent(a0)

    @pyqtSlot()
    def refresh(self) -> None:
        if self._pending_refresh is not None:
            return
        self.refreshButton.setEnabled(False)
        self._snapshot_read_failed = False
        try:
            self._pending_refresh = self._controller.refresh_inventory_lots()
        except (RuntimeError, ValueError) as error:
            self.refreshButton.setEnabled(True)
            self.statusLabel.setText(_tr('Inventory refresh failed: {error}').format(error=error))

    @pyqtSlot(object)
    def _lots_changed(self, value: object) -> None:
        if not isinstance(value, tuple) or not all(isinstance(lot, BeanLot) for lot in value):
            return
        selected_id = None
        selected = self._selected_lot()
        if selected is not None:
            selected_id = selected.lot_id
        try:
            snapshot = self._controller.inventory_cache_snapshot()
        except RuntimeError:
            self._snapshot_read_failed = True
            self._show_snapshot_unavailable(retained=True)
            return
        self._snapshot_read_failed = False
        self.model.replace_lots(() if snapshot is None else snapshot.lots)
        self._cached_at = None if snapshot is None else snapshot.refreshed_at
        self._select_lot(selected_id)
        if self._pending_refresh is None:
            self._update_status()

    @pyqtSlot(str)
    def _refresh_finished(self, operation: str) -> None:
        if operation != self._pending_refresh:
            return
        self._pending_refresh = None
        self.refreshButton.setEnabled(True)
        if self._snapshot_read_failed:
            self._show_snapshot_unavailable(retained=True)
            return
        refreshed = _tr('Inventory refreshed. {cached}').format(
            cached=self._cached_status()
        )
        self.statusLabel.setText(
            refreshed if self._online else _tr('Offline. {cached}').format(
                cached=refreshed)
        )

    @pyqtSlot(str, object)
    def _operation_failed(self, operation: str, value: object) -> None:
        if operation != self._pending_refresh:
            return
        self._pending_refresh = None
        self.refreshButton.setEnabled(True)
        message = value.message if isinstance(value, PublicFailure) else _tr('The operation could not be completed.')
        cached = self._cached_status()
        self.statusLabel.setText(
            _tr('{message} Previous cached inventory was retained. {cached}').format(
                message=message,
                cached=cached,
            ).strip()
        )

    @pyqtSlot(bool)
    def _online_changed(self, online: bool) -> None:
        self._online = online
        self._update_status()

    @pyqtSlot(object)
    def _context_changed(self, _value: object) -> None:
        self._pending_refresh = None
        self.refreshButton.setEnabled(True)
        self._snapshot_read_failed = False
        self.model.replace_lots(())
        self._cached_at = None
        try:
            snapshot = self._controller.inventory_cache_snapshot()
        except RuntimeError:
            self._snapshot_read_failed = True
            self._show_snapshot_unavailable(retained=False)
            return
        self.model.replace_lots(() if snapshot is None else snapshot.lots)
        self._cached_at = None if snapshot is None else snapshot.refreshed_at
        self._update_status()

    @pyqtSlot()
    def _selection_changed(self) -> None:
        lot = self._selected_lot()
        self.chooseButton.setEnabled(lot is not None)
        self.warningLabel.setText(
            '' if lot is None else BeanLotTableModel._warning(lot)  # pylint: disable=protected-access
        )

    @pyqtSlot()
    def _choose(self) -> None:
        lot = self._selected_lot()
        if lot is None:
            return
        self.selected_lot = lot
        self.accept()

    @pyqtSlot(QModelIndex)
    def _choose_index(self, _index: QModelIndex) -> None:
        self._choose()

    @pyqtSlot()
    def _clear(self) -> None:
        self.selected_lot = None
        self.accept()

    def _selected_lot(self) -> BeanLot | None:
        selection_model = self.tableView.selectionModel()
        if selection_model is None:
            return None
        rows = selection_model.selectedRows()
        if not rows:
            return None
        source = self.proxyModel.mapToSource(rows[0])
        return self.model.lot_at(source.row())

    def _select_lot(self, lot_id: UUID | None) -> None:
        if lot_id is not None:
            for source_row, lot in enumerate(self.model.lots):
                if lot.lot_id == lot_id:
                    proxy = self.proxyModel.mapFromSource(self.model.index(source_row, 0))
                    if proxy.isValid():
                        self.tableView.selectRow(proxy.row())
                        self.tableView.scrollTo(proxy)
                        return
        self.tableView.clearSelection()
        self._selection_changed()

    def _cached_status(self) -> str:
        if self._cached_at is None:
            return _tr('Cached inventory timestamp is unavailable.')
        return _tr('Cached inventory from {timestamp}.').format(
            timestamp=self._format_timestamp(self._cached_at)
        )

    def _show_snapshot_unavailable(self, *, retained: bool) -> None:
        if retained:
            self.statusLabel.setText(
                _tr('Inventory is unavailable. Previous cached inventory was retained. {cached}').format(
                    cached=self._cached_status()
                )
            )
        else:
            self.statusLabel.setText(_tr('Inventory is unavailable.'))

    def _update_status(self) -> None:
        cached = self._cached_status()
        if self._online:
            self.statusLabel.setText(cached)
        else:
            self.statusLabel.setText(_tr('Offline. {cached}').format(cached=cached))

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        local = value.astimezone() if value.tzinfo is not None else value
        return local.strftime('%Y-%m-%d %H:%M')


__all__ = [
    'BeanLotTableModel',
    'InterruptedReservationsDialog',
    'InterruptedReservationsModel',
    'InventoryLotDialog',
    'InventoryLotDialogController',
    'InventoryRecoveryDialogController',
]
