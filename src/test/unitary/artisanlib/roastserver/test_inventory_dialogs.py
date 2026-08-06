#
# ABOUT
# Tests for the Artisan Roast Server inventory lot chooser and Roast Properties link.
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

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from PyQt6.QtCore import QCoreApplication, QDateTime, QEvent, QObject, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QWidget,
)

from artisanlib import roast_properties
from artisanlib.roast_properties import UI_MODE, editGraphDlg
from artisanlib.roastserver.contract import FailureKind, Namespace, PublicFailure
from artisanlib.roastserver.inventory_contract import BeanLot, InventoryProfileLink
from artisanlib.roastserver.inventory_dialogs import (
    BeanLotTableModel,
    InterruptedReservationsDialog,
    InterruptedReservationsModel,
    InventoryLotDialog,
)
from artisanlib.roastserver.inventory_store import (
    InterruptedReservation,
    InventoryLifecycle,
    LotCacheSnapshot,
)
from artisanlib.roastserver.settings import namespace_for


NAMESPACE = namespace_for(
    'https://inventory.example',
    UUID('11111111-1111-4111-8111-111111111111'),
)
OTHER_NAMESPACE = namespace_for(
    'https://other.example',
    UUID('22222222-2222-4222-8222-222222222222'),
)
LOT = BeanLot(
    UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),
    '<b>Guatemala & Friends</b>',
    'Huehuetenango',
    ('Bourbon', 'Caturra'),
    'washed',
    2025,
    2_000,
    400,
    1_600,
    0,
)
CACHED_AT = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
UPDATED_AT = datetime(2026, 8, 5, 13, 45, tzinfo=UTC)


def rendered_timestamp(value: datetime) -> str:
    return value.astimezone().strftime('%Y-%m-%d %H:%M')


WARNING_LOT = BeanLot(
    UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),
    'Kenya AA',
    'Nyeri',
    ('SL28',),
    'natural',
    2024,
    50,
    75,
    -25,
    2,
)


class FakeController(QObject):
    inventoryLotsChanged = pyqtSignal(object)
    inventoryRefreshFinished = pyqtSignal(str)
    inventoryRecoveryRequired = pyqtSignal(object)
    operationFailed = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    settingsChanged = pyqtSignal(object)
    identityChanged = pyqtSignal(object)

    def __init__(self, lots: tuple[BeanLot, ...] = (LOT, WARNING_LOT)) -> None:
        super().__init__()
        self.lots = lots
        self.refreshes: list[str] = []
        self.active_refresh: str | None = None
        self.namespace: Namespace | None = NAMESPACE
        self.cached_at: datetime | None = CACHED_AT
        self.snapshot_error = False
        self.locked = False
        self.lock_calls: list[tuple[InventoryProfileLink | None, UUID | None, bool]] = []
        self.recovery_calls: list[tuple[UUID, str]] = []
        self.inventoryRefreshFinished.connect(self._refresh_finished)
        self.operationFailed.connect(self._refresh_failed)
        self.settingsChanged.connect(self._context_changed)
        self.identityChanged.connect(self._context_changed)

    @pyqtSlot(str)
    def _refresh_finished(self, operation: str) -> None:
        if operation == self.active_refresh:
            self.active_refresh = None

    @pyqtSlot(str, object)
    def _refresh_failed(self, operation: str, _failure: object) -> None:
        if operation == self.active_refresh:
            self.active_refresh = None

    @pyqtSlot(object)
    def _context_changed(self, _value: object) -> None:
        self.active_refresh = None

    def inventory_cache_snapshot(self) -> LotCacheSnapshot | None:
        if self.snapshot_error:
            raise RuntimeError('inventory_storage_failed')
        if self.namespace is None:
            return None
        return LotCacheSnapshot(self.namespace, self.lots, self.cached_at)

    def inventory_lots(self) -> tuple[BeanLot, ...]:
        snapshot = self.inventory_cache_snapshot()
        return () if snapshot is None else snapshot.lots

    def refresh_inventory_lots(self) -> str:
        if self.active_refresh is not None:
            return self.active_refresh
        request_id = f'refresh-{len(self.refreshes)}'
        self.refreshes.append(request_id)
        self.active_refresh = request_id
        return request_id

    def inventory_context(self) -> SimpleNamespace:
        return SimpleNamespace(namespace=self.namespace)

    def inventory_lot_locked(
        self,
        link: InventoryProfileLink | None,
        roast_uuid: UUID | None,
        profile_has_charge: bool,
    ) -> bool:
        self.lock_calls.append((link, roast_uuid, profile_has_charge))
        return link is not None and self.locked

    def resolve_interrupted_inventory(self, roast_uuid: UUID, action: str) -> None:
        self.recovery_calls.append((roast_uuid, action))


def _recovery(
    namespace: Namespace,
    roast_uuid: UUID,
    *,
    lot_name: str = '<b>Recovery lot</b>',
    lifecycle: str = 'reserved',
) -> InterruptedReservation:
    return InterruptedReservation(
        namespace,
        roast_uuid,
        LOT.lot_id,
        lot_name,
        UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
        1_250,
        cast(InventoryLifecycle, lifecycle),
        UPDATED_AT,
    )


def test_interrupted_model_exact_safe_fields_and_accessibility() -> None:
    item = _recovery(NAMESPACE, UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc'))
    model = InterruptedReservationsModel((item,))

    assert model.rowCount() == 1
    assert [model.headerData(column, Qt.Orientation.Horizontal)
        for column in range(model.columnCount())] == [
            'Lot', 'Roast', 'Planned', 'Namespace', 'Status']
    assert model.data(model.index(0, 0)) == item.lot_name
    assert model.data(model.index(0, 1)) == str(item.roast_uuid)
    assert model.data(model.index(0, 2)) == '1250g'
    assert NAMESPACE.origin in str(model.data(model.index(0, 3)))
    assert str(NAMESPACE.organization_id) in str(model.data(model.index(0, 3)))
    assert model.data(model.index(0, 4)) == 'Reserved'
    accessible = model.data(model.index(0, 0), Qt.ItemDataRole.AccessibleTextRole)
    assert item.lot_name in str(accessible)
    assert model.record_at(0) is item


@pytest.mark.parametrize(
    ('button_name', 'action'),
    [('finalizeButton', 'finalize'), ('releaseButton', 'release'), ('keepButton', 'keep')],
)
def test_inventory_recovery_all_actions_wait_for_resulting_state_signal(
    button_name: str,
    action: str,
) -> None:
    controller = FakeController()
    active = _recovery(NAMESPACE, UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc'))
    old = _recovery(OTHER_NAMESPACE, UUID('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'))
    dialog = InterruptedReservationsDialog(None, controller, (active, old), NAMESPACE)
    try:
        dialog.tableView.selectRow(0)
        button = cast('QPushButton', getattr(dialog, button_name))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert controller.recovery_calls == [(active.roast_uuid, action)]
        assert dialog.model.records == (active, old)
        assert not dialog.finalizeButton.isEnabled()
        assert not dialog.releaseButton.isEnabled()
        assert not dialog.keepButton.isEnabled()

        resulting = (active, old) if action == 'keep' else (old,)
        controller.inventoryRecoveryRequired.emit(resulting)
        assert dialog.model.records == resulting
        if action == 'keep':
            assert 'pending' in dialog.noticeLabel.text().lower()
    finally:
        dialog.clean_up()
        dialog.close()


def test_inventory_recovery_old_namespace_is_visible_keep_only_and_disconnects() -> None:
    controller = FakeController()
    old = _recovery(OTHER_NAMESPACE, UUID('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'))
    baseline = controller.receivers(controller.inventoryRecoveryRequired)
    dialog = InterruptedReservationsDialog(None, controller, (old,), NAMESPACE)
    assert controller.receivers(controller.inventoryRecoveryRequired) == baseline + 1
    dialog.tableView.selectRow(0)
    assert not dialog.finalizeButton.isEnabled()
    assert not dialog.releaseButton.isEnabled()
    assert dialog.keepButton.isEnabled()
    QTest.mouseClick(dialog.keepButton, Qt.MouseButton.LeftButton)
    assert controller.recovery_calls == []
    assert 'current' in dialog.noticeLabel.text().lower()
    dialog.clean_up()
    assert controller.receivers(controller.inventoryRecoveryRequired) == baseline
    dialog.close()


def test_model_has_fixed_columns_renders_weights_and_warns_without_mutating() -> None:
    lots = (LOT, WARNING_LOT)
    model = BeanLotTableModel(lots)

    assert model.rowCount() == 2
    assert model.columnCount() == 7
    assert [
        model.headerData(column, Qt.Orientation.Horizontal)
        for column in range(model.columnCount())
    ] == ['Lot', 'Origin', 'Varietals', 'Process', 'Crop year', 'Available', 'Conflicts']
    assert model.data(model.index(0, 0)) == LOT.name
    assert model.data(model.index(0, 5)) == '1.6kg'
    assert model.data(model.index(1, 5)) == '-25g'
    assert model.data(model.index(1, 6)) == '2'
    assert 'negative' in str(
        model.data(model.index(1, 5), Qt.ItemDataRole.ToolTipRole)
    ).lower()
    assert 'conflict' in str(
        model.data(model.index(1, 6), Qt.ItemDataRole.ToolTipRole)
    ).lower()
    assert model.lot_at(0) is LOT
    assert lots == (LOT, WARNING_LOT)

    zero_model = BeanLotTableModel((replace(WARNING_LOT,
        available_grams=0, unresolved_conflict_count=0),))
    assert 'No inventory' in str(zero_model.data(
        zero_model.index(0, 5), Qt.ItemDataRole.ToolTipRole))


def test_dialog_searches_all_descriptive_fields_case_insensitively() -> None:
    controller = FakeController()
    dialog = InventoryLotDialog(None, controller)
    try:
        for search, expected in (
            ('GUATEMALA', LOT),
            ('huehue', LOT),
            ('caturra', LOT),
            ('WASHED', LOT),
            ('2025', LOT),
            ('sl28', WARNING_LOT),
        ):
            dialog.searchEdit.setText(search)
            assert dialog.proxyModel.rowCount() == 1
            assert dialog.proxyModel.data(dialog.proxyModel.index(0, 0)) == expected.name
    finally:
        dialog.close()


def test_dialog_contains_snapshot_failures_at_construction_and_after_publication() -> None:
    controller = FakeController((LOT,))
    controller.snapshot_error = True
    dialog = InventoryLotDialog(None, controller)
    try:
        assert dialog.model.lots == ()
        assert 'unavailable' in dialog.statusLabel.text().lower()
    finally:
        dialog.close()

    controller.snapshot_error = False
    dialog = InventoryLotDialog(None, controller)
    try:
        dialog.refresh()
        request_id = controller.refreshes[-1]
        controller.snapshot_error = True
        controller.inventoryLotsChanged.emit((WARNING_LOT,))
        assert dialog.model.lots == (LOT,)
        assert rendered_timestamp(CACHED_AT) in dialog.statusLabel.text()
        assert not dialog.refreshButton.isEnabled()
        controller.inventoryRefreshFinished.emit(request_id)
        assert dialog.refreshButton.isEnabled()
        assert dialog.model.lots == (LOT,)
        assert 'retained' in dialog.statusLabel.text().lower()
    finally:
        dialog.close()


def test_dialog_reads_persisted_timestamp_and_correlates_refresh_completion() -> None:
    controller = FakeController((LOT,))
    dialog = InventoryLotDialog(None, controller, online=False)
    try:
        assert rendered_timestamp(CACHED_AT) in dialog.statusLabel.text()
        assert 'offline' in dialog.statusLabel.text().lower()
        dialog.refresh()
        request_id = controller.refreshes[-1]
        assert not dialog.refreshButton.isEnabled()
        controller.operationFailed.emit(
            request_id,
            PublicFailure(FailureKind.OFFLINE, 'offline', 'Connection failed.', True),
        )
        assert dialog.model.lots == (LOT,)
        assert dialog.refreshButton.isEnabled()
        assert 'Connection failed.' in dialog.statusLabel.text()
        assert rendered_timestamp(CACHED_AT) in dialog.statusLabel.text()

        dialog.refresh()
        success_id = controller.refreshes[-1]
        controller.lots = (WARNING_LOT,)
        controller.cached_at = UPDATED_AT
        controller.inventoryLotsChanged.emit(controller.lots)
        assert dialog.model.lots == (WARNING_LOT,)
        assert not dialog.refreshButton.isEnabled()
        assert 'refreshed' not in dialog.statusLabel.text().lower()
        controller.inventoryRefreshFinished.emit('unrelated')
        assert not dialog.refreshButton.isEnabled()
        controller.inventoryRefreshFinished.emit(success_id)
        assert dialog.refreshButton.isEnabled()
        assert 'refreshed' in dialog.statusLabel.text().lower()
        assert rendered_timestamp(UPDATED_AT) in dialog.statusLabel.text()
        assert rendered_timestamp(CACHED_AT) not in dialog.statusLabel.text()
    finally:
        dialog.close()


@pytest.mark.parametrize('succeeds', [True, False])
def test_parent_refresh_then_modal_chooser_share_exact_completion(
    succeeds: bool,
) -> None:
    controller = FakeController((LOT,))
    parent, _qmc = _staging_dialog(controller)
    observed: dict[str, object] = {}
    try:
        parent.refreshInventoryLots()
        request = parent._inventory_refresh_request
        assert request is not None

        def complete_shared_refresh() -> None:
            chooser = parent.inventoryLotDialog
            assert chooser is not None
            chooser.refresh()
            observed['chooser_request'] = chooser._pending_refresh
            observed['before_parent'] = parent._inventory_refresh_request
            if succeeds:
                controller.inventoryRefreshFinished.emit(request)
            else:
                controller.operationFailed.emit(
                    request,
                    PublicFailure(
                        FailureKind.OFFLINE,
                        'offline',
                        'Shared refresh failed.',
                        True,
                    ),
                )
            observed['after_chooser'] = chooser._pending_refresh
            observed['after_parent'] = parent._inventory_refresh_request
            observed['chooser_enabled'] = chooser.refreshButton.isEnabled()
            chooser.reject()

        QTimer.singleShot(0, complete_shared_refresh)
        parent.openInventoryLotDialog()

        assert controller.refreshes == [request]
        assert observed == {
            'chooser_request': request,
            'before_parent': request,
            'after_chooser': None,
            'after_parent': None,
            'chooser_enabled': True,
        }
        assert parent.inventoryLotRefreshButton.isEnabled()
        expected = 'Inventory refreshed.' if succeeds else 'Shared refresh failed.'
        assert expected in parent.inventoryLotStatusLabel.text()
    finally:
        parent.cleanUpInventoryLotSelection()
        QDialog.reject(parent)


def test_widget_context_handlers_clear_pending_shared_refresh() -> None:
    controller = FakeController((LOT,))
    parent, _qmc = _staging_dialog(controller)
    receiver_baseline = _inventory_receiver_counts(controller)
    chooser = InventoryLotDialog(None, controller)
    try:
        parent.refreshInventoryLots()
        chooser.refresh()
        request = parent._inventory_refresh_request
        assert request is not None
        assert chooser._pending_refresh == request

        controller.settingsChanged.emit(object())

        assert parent._inventory_refresh_request is None
        assert chooser._pending_refresh is None
        assert parent.inventoryLotRefreshButton.isEnabled()
        assert chooser.refreshButton.isEnabled()
    finally:
        chooser.reject()
        assert _inventory_receiver_counts(controller) == receiver_baseline
        parent.cleanUpInventoryLotSelection()
        QDialog.reject(parent)


def test_dialog_choose_clear_keyboard_accessibility_and_plain_text() -> None:
    controller = FakeController((LOT,))
    dialog = InventoryLotDialog(None, controller)
    try:
        assert dialog.isModal()
        assert dialog.searchEdit.accessibleName()
        assert dialog.tableView.accessibleName()
        assert dialog.refreshButton.accessibleName()
        assert dialog.chooseButton.accessibleName()
        assert dialog.clearButton.accessibleName()

        dialog.tableView.selectRow(0)
        assert dialog.warningLabel.textFormat() is Qt.TextFormat.PlainText
        assert dialog.warningLabel.text() == ''
        QTest.mouseClick(dialog.chooseButton, Qt.MouseButton.LeftButton)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.selected_lot is LOT
    finally:
        dialog.close()

    clear_dialog = InventoryLotDialog(None, controller, selected_lot_id=LOT.lot_id)
    try:
        QTest.mouseClick(clear_dialog.clearButton, Qt.MouseButton.LeftButton)
        assert clear_dialog.result() == QDialog.DialogCode.Accepted
        assert clear_dialog.selected_lot is None
    finally:
        clear_dialog.close()

    keyboard_dialog = InventoryLotDialog(None, controller)
    try:
        keyboard_dialog.show()
        keyboard_dialog.tableView.selectRow(0)
        keyboard_dialog.tableView.setFocus()
        QTest.keyClick(keyboard_dialog.tableView, Qt.Key.Key_Return)
        assert keyboard_dialog.result() == QDialog.DialogCode.Accepted
        assert keyboard_dialog.selected_lot is LOT
    finally:
        keyboard_dialog.close()


def _full_roast_properties_dialog(
    monkeypatch: pytest.MonkeyPatch,
    controller: FakeController | None,
    *,
    charged: bool = False,
    link: InventoryProfileLink | None = None,
    roast_uuid: UUID | None = None,
) -> tuple[editGraphDlg, QWidget, MagicMock, MagicMock]:
    for name in ('init', 'update', 'clearStockCaches'):
        monkeypatch.setattr(roast_properties.plus.stock, name, MagicMock())
    for name in (
        'getStores',
        'getStoreLabels',
        'getCoffees',
        'getCoffeesLabels',
        'getBlends',
        'getBlendLabels',
    ):
        monkeypatch.setattr(
            roast_properties.plus.stock, name, MagicMock(return_value=[]))
    monkeypatch.setattr(
        roast_properties.plus.stock, 'getWorker', MagicMock(return_value=None))

    qmc = MagicMock()
    values: dict[str, object] = {
        'beans': 'Original beans',
        'mode': 'C',
        'roastingnotes': '',
        'cuppingnotes': '',
        'density': (650.0, 'g', 1.0, 'l'),
        'density_roasted': (550.0, 'g', 1.0, 'l'),
        'volume': (1.0, 1.0, 'l'),
        'beansize_min': 0,
        'beansize_max': 0,
        'moisture_greens': 0.0,
        'title': 'Title',
        'title_show_always': False,
        'weight': (1.0, 0.8, 'Kg'),
        'end_weight_est': 0.8,
        'roasted_defects_mode': False,
        'roasted_defects_weight': 0.0,
        'perKgRoastMode': False,
        'specialevents': [],
        'specialeventstype': [],
        'specialeventsStrings': [],
        'specialeventsvalue': [],
        'timeindex': [0 if charged else -1, 0, 0, 0, 0, 0, 0, 0],
        'timex': [],
        'phases': [0, 0, 0, 0],
        'ambientTemp': 0.0,
        'ambient_humidity': 0.0,
        'ambient_pressure': 0.0,
        'roastpropertiesAutoOpenFlag': False,
        'roastpropertiesAutoOpenDropFlag': False,
        'flagon': False,
        'flagstart': False,
        'safesaveflag': False,
        'roastdate': QDateTime.currentDateTime(),
        'roastbatchnr': 0,
        'roastbatchpos': 0,
        'roastbatchprefix': '',
        'batchcounter': 0,
        'palette': {'title': '#000000', 'canvas': '#ffffff'},
        'roastUUID': None if roast_uuid is None else roast_uuid.hex,
        'roastServerInventoryOrigin': (
            None if link is None else link.namespace.origin),
        'roastServerInventoryOrganizationUUID': (
            None if link is None else link.namespace.organization_id.hex),
        'roastServerBeanLotUUID': None if link is None else link.lot_id.hex,
        'roastServerBeanLotName': None if link is None else link.lot_name,
        'plus_default_store': None,
        'plus_custom_blend': None,
        'plus_store': 'plus-store',
        'plus_store_label': 'Plus Store',
        'plus_coffee': 'plus-coffee',
        'plus_coffee_label': 'Plus Coffee',
        'plus_blend_label': None,
        'plus_blend_spec': None,
        'plus_blend_spec_labels': None,
    }
    for name, value in values.items():
        setattr(qmc, name, value)
    qmc.device_name_subst.side_effect = lambda value: value

    aw = MagicMock()
    aw.qmc = qmc
    aw.ETname = 'ET'
    aw.BTname = 'BT'
    aw.roastserver_controller = controller
    aw.plus_account = 'plus-account'
    aw.app.darkmode = False
    aw.superusermode = False
    aw.ui_mode = UI_MODE.PRODUCTION
    aw.simulator = False
    aw.percent_decimals = 1
    aw.container1_idx = 0
    aw.QColorBrightness.return_value = 0
    aw.recentRoastsMenuList.return_value = []
    aw.weight_loss.return_value = 0.0
    aw.volume_increase.return_value = 0.0
    aw.scale_manager.is_scale1_configured.return_value = False
    aw.createCLocaleDoubleValidator.side_effect = (
        lambda *_args: QDoubleValidator(_args[-1]))
    aw.eNumberSpinBox = QSpinBox()
    aw.editGraphDlg_activeTab = 0
    aw.editgraphdialog = None

    parent = QWidget()
    return editGraphDlg(parent, aw), parent, aw, qmc


def _inventory_receiver_counts(controller: FakeController) -> tuple[int, ...]:
    return tuple(
        controller.receivers(signal)
        for signal in (
            controller.inventoryLotsChanged,
            controller.inventoryRefreshFinished,
            controller.operationFailed,
            controller.onlineChanged,
            controller.settingsChanged,
            controller.identityChanged,
        )
    )


def test_production_roast_properties_constructor_plus_and_close_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parity: list[tuple[object, ...]] = []
    for controller in (None, FakeController((LOT,))):
        baseline = None if controller is None else _inventory_receiver_counts(controller)
        dialog, _parent, _aw, qmc = _full_roast_properties_dialog(
            monkeypatch, controller)
        try:
            assert dialog.dialogbuttons is not None
            assert dialog.plus_coffees_combo.count() == 1
            assert dialog.plus_blends_combo.count() == 1
            if controller is None:
                assert not dialog.inventoryLotChooseButton.isEnabled()
            else:
                assert dialog.inventoryLotChooseButton.isEnabled()
                dialog.chooseInventoryLot(LOT)

            dialog.plus_coffees_combo.setCurrentIndex(-1)
            dialog.plus_blend_selected_label = 'Plus Blend'
            dialog.plus_blend_selected_spec = {'label': 'Plus Blend', 'ingredients': []}
            dialog.plus_blend_selected_spec_labels = []
            dialog.plus_blends_combo.setCurrentIndex(-1)
            callback_outcome = (
                dialog.plus_coffees_combo.currentIndex(),
                dialog.plus_blends_combo.currentIndex(),
                dialog.plus_store_selected,
                dialog.plus_coffee_selected,
                dialog.plus_blend_selected_spec,
                dialog.user_updated_coffee_or_blend,
            )

            ok = dialog.dialogbuttons.button(QDialogButtonBox.StandardButton.Ok)
            assert ok is not None
            QTest.mouseClick(ok, Qt.MouseButton.LeftButton)
            assert dialog.result() == QDialog.DialogCode.Accepted
            parity.append((
                callback_outcome,
                qmc.plus_store,
                qmc.plus_coffee,
                qmc.plus_blend_spec,
            ))
            if controller is None:
                assert qmc.roastServerBeanLotUUID is None
            else:
                assert qmc.roastServerBeanLotUUID == LOT.lot_id.hex
                assert _inventory_receiver_counts(controller) == baseline
        finally:
            if dialog.result() == 0:
                dialog.reject()
    assert parity[0] == parity[1]

    cancel_controller = FakeController((LOT,))
    cancel_baseline = _inventory_receiver_counts(cancel_controller)
    cancel_dialog, _cancel_parent, _aw, cancel_qmc = _full_roast_properties_dialog(
        monkeypatch, cancel_controller)
    cancel_dialog.chooseInventoryLot(LOT)
    cancel_dialog.plus_coffees_combo.setCurrentIndex(-1)
    cancel = cancel_dialog.dialogbuttons.button(QDialogButtonBox.StandardButton.Cancel)
    assert cancel is not None
    QTest.mouseClick(cancel, Qt.MouseButton.LeftButton)
    assert cancel_dialog.result() == QDialog.DialogCode.Rejected
    assert cancel_qmc.roastServerBeanLotUUID is None
    assert cancel_qmc.plus_coffee == 'plus-coffee'
    assert _inventory_receiver_counts(cancel_controller) == cancel_baseline

    charged_controller = FakeController((LOT,))
    charged_baseline = _inventory_receiver_counts(charged_controller)
    charged_dialog, _charged_parent, _aw, _qmc = _full_roast_properties_dialog(
        monkeypatch, charged_controller, charged=True)
    assert charged_controller.lock_calls == []
    assert not charged_dialog.inventoryLotChooseButton.isEnabled()
    assert not charged_dialog.inventoryLotClearButton.isEnabled()
    assert charged_dialog.inventoryLotRefreshButton.isEnabled()
    charged_cancel = charged_dialog.dialogbuttons.button(
        QDialogButtonBox.StandardButton.Cancel)
    assert charged_cancel is not None
    QTest.mouseClick(charged_cancel, Qt.MouseButton.LeftButton)
    assert _inventory_receiver_counts(charged_controller) == charged_baseline

    locked_controller = FakeController((LOT,))
    locked_controller.locked = True
    locked_link = InventoryProfileLink(NAMESPACE, LOT.lot_id, LOT.name)
    locked_roast_uuid = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
    locked_baseline = _inventory_receiver_counts(locked_controller)
    locked_dialog, _locked_parent, _aw, _qmc = _full_roast_properties_dialog(
        monkeypatch,
        locked_controller,
        link=locked_link,
        roast_uuid=locked_roast_uuid,
    )
    assert locked_controller.lock_calls == [
        (locked_link, locked_roast_uuid, False)
    ]
    assert not locked_dialog.inventoryLotChooseButton.isEnabled()
    assert not locked_dialog.inventoryLotClearButton.isEnabled()
    assert locked_dialog.inventoryLotRefreshButton.isEnabled()
    locked_cancel = locked_dialog.dialogbuttons.button(
        QDialogButtonBox.StandardButton.Cancel)
    assert locked_cancel is not None
    QTest.mouseClick(locked_cancel, Qt.MouseButton.LeftButton)
    assert _inventory_receiver_counts(locked_controller) == locked_baseline


def _staging_dialog(
    controller: FakeController | None,
    link: InventoryProfileLink | None = None,
) -> tuple[editGraphDlg, SimpleNamespace]:
    qmc = SimpleNamespace(
        roastServerInventoryOrigin=None if link is None else link.namespace.origin,
        roastServerInventoryOrganizationUUID=(
            None if link is None else link.namespace.organization_id.hex
        ),
        roastServerBeanLotUUID=None if link is None else link.lot_id.hex,
        roastServerBeanLotName=None if link is None else link.lot_name,
        roastUUID=None,
        timeindex=[-1, 0, 0, 0, 0, 0, 0, 0],
        title='Untouched title',
        weight=(750.0, 0.0, 'g'),
        plus_store='plus-store',
        plus_store_label='Plus Store',
        plus_coffee='plus-coffee',
        plus_coffee_label='Plus Coffee',
        plus_blend_label='plus-blend',
        plus_blend_spec={'label': 'Plus Blend', 'ingredients': []},
        plus_blend_spec_labels=[],
    )
    aw = SimpleNamespace(qmc=qmc, roastserver_controller=controller)
    dialog = editGraphDlg.__new__(editGraphDlg)
    QDialog.__init__(dialog)
    dialog.aw = cast('object', aw)
    dialog.beansedit = QTextEdit()
    dialog.initializeInventoryLotSelection()
    return dialog, qmc


def test_roast_properties_plus_widget_callbacks_match_without_or_with_controller() -> None:
    for controller in (None, FakeController()):
        dialog, qmc = _staging_dialog(controller)
        callbacks: list[str] = []
        try:
            dialog.plus_coffees = None
            dialog.plus_blends = None
            dialog.plus_coffees_combo = QComboBox()
            dialog.plus_blends_combo = QComboBox()
            dialog.plus_coffees_combo.addItems(['', 'Coffee'])
            dialog.plus_blends_combo.addItems(['', 'Blend'])
            dialog.plus_coffees_combo.setCurrentIndex(1)
            dialog.plus_blends_combo.setCurrentIndex(1)
            dialog.defaultCoffeeData = lambda target=callbacks: target.append('default')
            dialog.updateTitle = lambda *_args, target=callbacks: target.append('title')
            dialog.checkWeightIn = lambda target=callbacks: target.append('weight')
            dialog.updatePlusSelectedLine = lambda target=callbacks: target.append('line')
            dialog.plus_store_selected = qmc.plus_store
            dialog.plus_store_selected_label = qmc.plus_store_label
            dialog.plus_coffee_selected = qmc.plus_coffee
            dialog.plus_coffee_selected_label = qmc.plus_coffee_label
            dialog.plus_blend_selected_label = qmc.plus_blend_label
            dialog.plus_blend_selected_spec = qmc.plus_blend_spec
            dialog.plus_blend_selected_spec_labels = qmc.plus_blend_spec_labels
            dialog.plus_amount_selected = 1.0
            dialog.plus_amount_replace_selected = 2.0
            dialog.user_updated_coffee_or_blend = False

            dialog.plus_coffees_combo.currentIndexChanged.connect(
                dialog.coffeeSelectionChanged)
            dialog.plus_coffees_combo.setCurrentIndex(0)
            assert dialog.user_updated_coffee_or_blend
            assert dialog.plus_coffee_selected is None
            assert dialog.plus_store_selected is None
            assert callbacks == ['default', 'title', 'weight', 'line']

            callbacks.clear()
            dialog.plus_store_selected = qmc.plus_store
            dialog.plus_blend_selected_label = qmc.plus_blend_label
            dialog.plus_blend_selected_spec = qmc.plus_blend_spec
            dialog.plus_blend_selected_spec_labels = qmc.plus_blend_spec_labels
            dialog.plus_blends_combo.currentIndexChanged.connect(
                dialog.blendSelectionChanged)
            dialog.plus_blends_combo.setCurrentIndex(0)
            assert dialog.plus_blend_selected_label is None
            assert dialog.plus_blend_selected_spec is None
            assert callbacks == ['default', 'title', 'weight', 'line']

            if controller is None:
                assert not dialog.inventoryLotChooseButton.isEnabled()
                assert not dialog.inventoryLotRefreshButton.isEnabled()
            else:
                coffee_index = dialog.plus_coffees_combo.currentIndex()
                blend_index = dialog.plus_blends_combo.currentIndex()
                plus_state = (
                    dialog.plus_store_selected,
                    dialog.plus_coffee_selected,
                    dialog.plus_blend_selected_spec,
                )
                dialog.chooseInventoryLot(LOT)
                assert dialog.plus_coffees_combo.currentIndex() == coffee_index
                assert dialog.plus_blends_combo.currentIndex() == blend_index
                assert (
                    dialog.plus_store_selected,
                    dialog.plus_coffee_selected,
                    dialog.plus_blend_selected_spec,
                ) == plus_state
        finally:
            dialog.cleanUpInventoryLotSelection()
            QDialog.reject(dialog)


def test_roast_properties_real_ok_and_cancel_are_inventory_only() -> None:
    controller = FakeController()
    ok_dialog, ok_qmc = _staging_dialog(controller)
    ok_buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok_buttons.accepted.connect(ok_dialog.commitInventoryLotSelection)
    ok_buttons.accepted.connect(ok_dialog.accept)
    try:
        plus_before = (
            ok_qmc.plus_store,
            ok_qmc.plus_coffee,
            ok_qmc.plus_blend_label,
            ok_qmc.plus_blend_spec,
        )
        ok_dialog.chooseInventoryLot(LOT)
        ok_button = ok_buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok_button is not None
        QTest.mouseClick(ok_button, Qt.MouseButton.LeftButton)
        assert ok_dialog.result() == QDialog.DialogCode.Accepted
        assert ok_qmc.roastServerBeanLotUUID == LOT.lot_id.hex
        assert (
            ok_qmc.plus_store,
            ok_qmc.plus_coffee,
            ok_qmc.plus_blend_label,
            ok_qmc.plus_blend_spec,
        ) == plus_before
    finally:
        ok_dialog.cleanUpInventoryLotSelection()
        QDialog.reject(ok_dialog)

    cancel_dialog, cancel_qmc = _staging_dialog(controller)
    cancel_buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    cancel_buttons.rejected.connect(cancel_dialog.closeEvent)
    cancel_dialog.restoreAllEnergySettings = lambda: None
    cancel_dialog.org_beans = 'Original beans'
    cancel_dialog.org_density = (0.0, 'g', 0.0, 'l')
    cancel_dialog.org_density_roasted = (0.0, 'g', 0.0, 'l')
    cancel_dialog.org_beansize_min = 0
    cancel_dialog.org_beansize_max = 0
    cancel_dialog.org_moisture_greens = 0.0
    cancel_dialog.org_weight = cancel_qmc.weight
    cancel_dialog.org_end_weight_est = 0.0
    cancel_dialog.org_volume = (0.0, 0.0, 'l')
    cancel_dialog.org_roasted_defects_mode = False
    cancel_dialog.org_perKgRoastMode = False
    cancel_dialog.org_specialevents = []
    cancel_dialog.org_specialeventstype = []
    cancel_dialog.org_specialeventsStrings = []
    cancel_dialog.org_specialeventsvalue = []
    cancel_dialog.org_timeindex = list(cancel_qmc.timeindex)
    cancel_dialog.org_phases = []
    cancel_dialog.org_ambientTemp = 0.0
    cancel_dialog.org_ambient_humidity = 0.0
    cancel_dialog.org_ambient_pressure = 0.0
    cancel_dialog.org_roastpropertiesAutoOpenFlag = False
    cancel_dialog.org_roastpropertiesAutoOpenDropFlag = False
    cancel_qmc.beans = 'Original beans'
    cancel_qmc.density = cancel_dialog.org_density
    cancel_qmc.density_roasted = cancel_dialog.org_density_roasted
    cancel_qmc.beansize_min = 0
    cancel_qmc.beansize_max = 0
    cancel_qmc.moisture_greens = 0.0
    cancel_qmc.end_weight_est = 0.0
    cancel_qmc.volume = cancel_dialog.org_volume
    cancel_qmc.roasted_defects_mode = False
    cancel_qmc.perKgRoastMode = False
    cancel_qmc.specialevents = []
    cancel_qmc.specialeventstype = []
    cancel_qmc.specialeventsStrings = []
    cancel_qmc.specialeventsvalue = []
    cancel_qmc.phases = []
    cancel_qmc.ambientTemp = 0.0
    cancel_qmc.ambient_humidity = 0.0
    cancel_qmc.ambient_pressure = 0.0
    cancel_qmc.roastpropertiesAutoOpenFlag = False
    cancel_qmc.roastpropertiesAutoOpenDropFlag = False
    cancel_qmc.flagon = True
    cancel_qmc.clear_last_picked_event_selection = lambda: None
    cancel_dialog.aw.eNumberSpinBox = QSpinBox()
    cancel_dialog.clean_up = cancel_dialog.cleanUpInventoryLotSelection
    try:
        plus_before = (
            cancel_qmc.plus_store,
            cancel_qmc.plus_coffee,
            cancel_qmc.plus_blend_label,
            cancel_qmc.plus_blend_spec,
        )
        cancel_dialog.chooseInventoryLot(WARNING_LOT)
        cancel_button = cancel_buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert cancel_button is not None
        QTest.mouseClick(cancel_button, Qt.MouseButton.LeftButton)
        assert cancel_dialog.result() == QDialog.DialogCode.Rejected
        assert cancel_qmc.roastServerBeanLotUUID is None
        assert (
            cancel_qmc.plus_store,
            cancel_qmc.plus_coffee,
            cancel_qmc.plus_blend_label,
            cancel_qmc.plus_blend_spec,
        ) == plus_before
    finally:
        cancel_dialog.cleanUpInventoryLotSelection()
        QDialog.reject(cancel_dialog)


def test_roast_properties_selection_is_staged_fills_only_empty_and_commits_all_fields() -> None:
    controller = FakeController()
    dialog, qmc = _staging_dialog(controller)
    try:
        dialog.beansedit.setPlainText('')
        dialog.chooseInventoryLot(LOT)
        assert dialog.beansedit.toPlainText() == LOT.name
        assert qmc.roastServerBeanLotUUID is None
        dialog.commitInventoryLotSelection()
        assert qmc.roastServerInventoryOrigin == NAMESPACE.origin
        assert qmc.roastServerInventoryOrganizationUUID == NAMESPACE.organization_id.hex
        assert qmc.roastServerBeanLotUUID == LOT.lot_id.hex
        assert qmc.roastServerBeanLotName == LOT.name
        assert qmc.title == 'Untouched title'
        assert qmc.weight == (750.0, 0.0, 'g')
        assert qmc.plus_coffee == 'plus-coffee'
        assert qmc.plus_blend_label == 'plus-blend'

        dialog.beansedit.setPlainText('Manual beans')
        dialog.chooseInventoryLot(WARNING_LOT)
        assert dialog.beansedit.toPlainText() == 'Manual beans'
        dialog.clearInventoryLot()
        assert dialog.beansedit.toPlainText() == 'Manual beans'
    finally:
        dialog.cleanUpInventoryLotSelection()
        QDialog.reject(dialog)


def test_roast_properties_refresh_is_correlated_and_snapshot_failures_are_contained() -> None:
    controller = FakeController((LOT,))
    dialog, _qmc = _staging_dialog(controller)
    try:
        dialog.refreshInventoryLots()
        request_id = controller.refreshes[-1]
        controller.lots = (WARNING_LOT,)
        controller.cached_at = UPDATED_AT
        controller.inventoryLotsChanged.emit(controller.lots)
        assert dialog._inventory_refresh_request == request_id
        assert not dialog.inventoryLotRefreshButton.isEnabled()
        assert 'refreshed' not in dialog.inventoryLotStatusLabel.text().lower()
        controller.inventoryRefreshFinished.emit('unrelated')
        assert dialog._inventory_refresh_request == request_id
        controller.inventoryRefreshFinished.emit(request_id)
        assert dialog._inventory_refresh_request is None
        assert dialog.inventoryLotRefreshButton.isEnabled()
        assert 'refreshed' in dialog.inventoryLotStatusLabel.text().lower()

        dialog.refreshInventoryLots()
        failed_request = controller.refreshes[-1]
        controller.snapshot_error = True
        controller.inventoryLotsChanged.emit((LOT,))
        assert dialog._inventory_lots == (WARNING_LOT,)
        assert rendered_timestamp(UPDATED_AT) in dialog.inventoryLotStatusLabel.text()
        assert dialog._inventory_refresh_request == failed_request
        controller.operationFailed.emit(
            failed_request,
            PublicFailure(FailureKind.OFFLINE, 'offline', 'Connection failed.', True),
        )
        assert dialog._inventory_refresh_request is None
        assert 'retained' in dialog.inventoryLotStatusLabel.text().lower()
    finally:
        dialog.cleanUpInventoryLotSelection()
        QDialog.reject(dialog)


def test_repeated_roast_properties_chooser_open_deletes_closed_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = FakeController((LOT,))
    dialog, _qmc = _staging_dialog(controller)
    receiver_baseline = _inventory_receiver_counts(controller)
    destroyed: list[None] = []

    def reject_immediately(chooser: InventoryLotDialog) -> QDialog.DialogCode:
        chooser.destroyed.connect(lambda: destroyed.append(None))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(InventoryLotDialog, 'exec', reject_immediately)
    try:
        for _ in range(2):
            dialog.openInventoryLotDialog()
            QCoreApplication.sendPostedEvents(
                None, QEvent.Type.DeferredDelete
            )
            QCoreApplication.processEvents()
            assert dialog.inventoryLotDialog is None
            assert dialog.findChildren(InventoryLotDialog) == []
            assert _inventory_receiver_counts(controller) == receiver_baseline
        assert destroyed == [None, None]
    finally:
        dialog.cleanUpInventoryLotSelection()
        QDialog.reject(dialog)


def test_roast_properties_rejects_chooser_result_after_namespace_aba(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = FakeController((LOT,))
    dialog, qmc = _staging_dialog(controller)

    def exec_with_namespace_aba(chooser: InventoryLotDialog) -> QDialog.DialogCode:
        controller.namespace = OTHER_NAMESPACE
        controller.settingsChanged.emit(object())
        controller.lots = (WARNING_LOT,)
        controller.namespace = NAMESPACE
        controller.identityChanged.emit(object())
        chooser.selected_lot = WARNING_LOT
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(InventoryLotDialog, 'exec', exec_with_namespace_aba)
    try:
        dialog.openInventoryLotDialog()
        assert dialog._inventory_link is None
        assert qmc.roastServerBeanLotUUID is None
        assert WARNING_LOT.name not in dialog.beansedit.toPlainText()
        assert 'organization changed' in dialog.inventoryLotStatusLabel.text().lower()
    finally:
        dialog.cleanUpInventoryLotSelection()
        QDialog.reject(dialog)


def test_roast_properties_cancel_lock_and_context_change_rules() -> None:
    original = InventoryProfileLink(OTHER_NAMESPACE, LOT.lot_id, LOT.name)
    controller = FakeController()
    controller.locked = True
    receiver_baseline = _inventory_receiver_counts(controller)
    dialog, qmc = _staging_dialog(controller, original)
    try:
        assert dialog.inventoryLotNameLabel.text() == LOT.name
        assert 'unavailable' in dialog.inventoryLotStatusLabel.text().lower()
        assert rendered_timestamp(CACHED_AT) in dialog.inventoryLotStatusLabel.text()
        assert not dialog.inventoryLotChooseButton.isEnabled()
        assert not dialog.inventoryLotClearButton.isEnabled()
        assert dialog.inventoryLotRefreshButton.isEnabled()
        controller.cached_at = UPDATED_AT
        controller.inventoryLotsChanged.emit(controller.lots)
        assert rendered_timestamp(UPDATED_AT) in dialog.inventoryLotStatusLabel.text()
        inventory_signals = (
            controller.inventoryLotsChanged,
            controller.inventoryRefreshFinished,
            controller.operationFailed,
            controller.onlineChanged,
            controller.settingsChanged,
            controller.identityChanged,
        )
        assert all(controller.receivers(signal) > 0 for signal in inventory_signals)
        dialog.cleanUpInventoryLotSelection()
        assert _inventory_receiver_counts(controller) == receiver_baseline
        assert qmc.roastServerInventoryOrigin == OTHER_NAMESPACE.origin
        assert qmc.roastServerBeanLotUUID == LOT.lot_id.hex
    finally:
        QDialog.reject(dialog)

    charge_controller = FakeController()
    charged_link = InventoryProfileLink(NAMESPACE, LOT.lot_id, LOT.name)
    charged_dialog, charged_qmc = _staging_dialog(charge_controller, charged_link)
    try:
        charged_qmc.timeindex[0] = 0
        charged_dialog.updateInventoryLotRow()
        assert not charged_dialog.inventoryLotChooseButton.isEnabled()
        assert not charged_dialog.inventoryLotClearButton.isEnabled()
        assert charged_dialog.inventoryLotRefreshButton.isEnabled()
    finally:
        charged_dialog.cleanUpInventoryLotSelection()
        QDialog.reject(charged_dialog)

    controller = FakeController()
    staged_dialog, staged_qmc = _staging_dialog(controller)
    try:
        staged_dialog.chooseInventoryLot(LOT)
        controller.namespace = OTHER_NAMESPACE
        controller.settingsChanged.emit(object())
        assert staged_dialog.inventoryLotNameLabel.text() == 'None'
        staged_dialog.commitInventoryLotSelection()
        assert staged_qmc.roastServerBeanLotUUID is None
    finally:
        staged_dialog.cleanUpInventoryLotSelection()
        QDialog.reject(staged_dialog)
