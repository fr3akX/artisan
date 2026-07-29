import os
from collections.abc import Generator
from typing import cast
from unittest.mock import Mock, call

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QVBoxLayout

from artisanlib.santoker_warmup_ui import SantokerWarmupControls


@pytest.fixture(scope='module')
def qapplication() -> Generator[QApplication, None, None]:
    app = QApplication.instance()
    if app is None:
        created = QApplication([])
        yield created
        created.quit()
        return
    yield cast(QApplication, app)


def test_compact_controls_layout_and_defaults(qapplication: QApplication) -> None:  # noqa: ARG001
    controls = SantokerWarmupControls()

    layout = controls.layout()
    assert isinstance(layout, QVBoxLayout)
    button_item = layout.itemAt(0)
    target_item = layout.itemAt(1)
    assert button_item is not None
    assert target_item is not None
    assert button_item.widget() is controls.button
    assert target_item.widget() is controls.target
    assert controls.button.isCheckable()
    assert not controls.button.isChecked()
    assert controls.button.focusPolicy() is Qt.FocusPolicy.NoFocus
    assert not controls.target.keyboardTracking()


def test_button_click_emits_enabled_state(qapplication: QApplication) -> None:  # noqa: ARG001
    controls = SantokerWarmupControls()
    changed = Mock()
    controls.enabledChanged.connect(changed)

    controls.button.click()
    controls.button.click()

    assert changed.call_args_list == [call(True), call(False)]


def test_configure_target_blocks_user_signal(qapplication: QApplication) -> None:  # noqa: ARG001
    controls = SantokerWarmupControls()
    changed = Mock()
    controls.targetChanged.connect(changed)

    controls.configureTarget('F', 374.0)

    assert (controls.target.minimum(), controls.target.maximum()) == (212, 572)
    assert controls.target.value() == 374
    assert controls.target.suffix() == ' °F'
    changed.assert_not_called()


def test_spinbox_step_up_emits_immediately(qapplication: QApplication) -> None:  # noqa: ARG001
    controls = SantokerWarmupControls()
    controls.configureTarget('C', 200.0)
    changed = Mock()
    controls.targetChanged.connect(changed)

    controls.target.stepUp()

    assert (controls.target.minimum(), controls.target.maximum()) == (100, 300)
    assert controls.target.suffix() == ' °C'
    changed.assert_called_once_with(201)


def test_typing_emits_only_after_commit(qapplication: QApplication) -> None:
    controls = SantokerWarmupControls()
    controls.configureTarget('C', 200.0)
    controls.show()
    qapplication.processEvents()

    changed = Mock()
    controls.targetChanged.connect(changed)

    line_edit = controls.target.lineEdit()
    assert line_edit is not None
    line_edit.setFocus()
    qapplication.processEvents()
    line_edit.setText('205')

    changed.assert_not_called()

    QTest.keyClick(line_edit, Qt.Key.Key_Return)  # type: ignore[call-overload]
    qapplication.processEvents()

    assert controls.target.value() == 205
    changed.assert_called_once_with(205)


def test_set_state_blocks_button_signal(qapplication: QApplication) -> None:  # noqa: ARG001
    controls = SantokerWarmupControls()
    changed = Mock()
    controls.enabledChanged.connect(changed)

    controls.setState(True)

    assert controls.button.isChecked()
    changed.assert_not_called()
