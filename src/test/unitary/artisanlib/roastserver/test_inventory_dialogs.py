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
from uuid import UUID

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QTextEdit

from artisanlib.roast_properties import editGraphDlg
from artisanlib.roastserver.contract import FailureKind, Namespace, PublicFailure
from artisanlib.roastserver.inventory_contract import BeanLot, InventoryProfileLink
from artisanlib.roastserver.inventory_dialogs import BeanLotTableModel, InventoryLotDialog
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
    operationFailed = pyqtSignal(str, object)
    onlineChanged = pyqtSignal(bool)
    settingsChanged = pyqtSignal(object)
    identityChanged = pyqtSignal(object)

    def __init__(self, lots: tuple[BeanLot, ...] = (LOT, WARNING_LOT)) -> None:
        super().__init__()
        self.lots = lots
        self.refreshes: list[str] = []
        self.namespace: Namespace | None = NAMESPACE
        self.locked = False

    def inventory_lots(self) -> tuple[BeanLot, ...]:
        return self.lots

    def refresh_inventory_lots(self) -> str:
        request_id = f'refresh-{len(self.refreshes)}'
        self.refreshes.append(request_id)
        return request_id

    def inventory_context(self) -> SimpleNamespace:
        return SimpleNamespace(namespace=self.namespace)

    def inventory_lot_locked(
        self,
        link: InventoryProfileLink | None,
        roast_uuid: UUID | None,
        profile_has_charge: bool,
    ) -> bool:
        del link, roast_uuid, profile_has_charge
        return self.locked


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
    dialog = InventoryLotDialog(None, controller, cached_at=datetime(2026, 8, 5, 12, 30, tzinfo=UTC))
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


def test_dialog_refresh_retains_cache_on_failure_and_updates_on_success() -> None:
    controller = FakeController((LOT,))
    dialog = InventoryLotDialog(
        None,
        controller,
        cached_at=datetime(2026, 8, 5, 12, 30, tzinfo=UTC),
        online=False,
    )
    try:
        assert '2026' in dialog.statusLabel.text()
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

        dialog.refresh()
        controller.lots = (WARNING_LOT,)
        controller.inventoryLotsChanged.emit(controller.lots)
        assert dialog.model.lots == (WARNING_LOT,)
        assert dialog.refreshButton.isEnabled()
        assert 'refreshed' in dialog.statusLabel.text().lower()
    finally:
        dialog.close()


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


def _staging_dialog(
    controller: FakeController,
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
        plus_coffee='plus-coffee',
        plus_blend_label='plus-blend',
    )
    aw = SimpleNamespace(qmc=qmc, roastserver_controller=controller)
    dialog = editGraphDlg.__new__(editGraphDlg)
    QDialog.__init__(dialog)
    dialog.aw = cast('object', aw)
    dialog.beansedit = QTextEdit()
    dialog.initializeInventoryLotSelection()
    return dialog, qmc


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


def test_roast_properties_cancel_lock_and_context_change_rules() -> None:
    original = InventoryProfileLink(OTHER_NAMESPACE, LOT.lot_id, LOT.name)
    controller = FakeController()
    controller.locked = True
    dialog, qmc = _staging_dialog(controller, original)
    try:
        assert dialog.inventoryLotNameLabel.text() == LOT.name
        assert 'unavailable' in dialog.inventoryLotStatusLabel.text().lower()
        assert not dialog.inventoryLotChooseButton.isEnabled()
        assert not dialog.inventoryLotClearButton.isEnabled()
        assert dialog.inventoryLotRefreshButton.isEnabled()
        dialog.cleanUpInventoryLotSelection()
        assert qmc.roastServerInventoryOrigin == OTHER_NAMESPACE.origin
        assert qmc.roastServerBeanLotUUID == LOT.lot_id.hex
    finally:
        QDialog.reject(dialog)

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
