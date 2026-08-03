import os
import platform
from collections.abc import Generator
from typing import cast
from unittest.mock import Mock, call

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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


def test_compact_controls_match_top_button_layout_height(
    qapplication: QApplication,
) -> None:
    parent = QWidget()
    layout = QHBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    top_button = QPushButton('ON')
    native_height = top_button.sizeHint().height()
    height = int(round(native_height * (2 if platform.system() == 'Windows' else 1.3)))
    top_button.setFixedHeight(height)
    controls = SantokerWarmupControls()
    controls.setCompactHeight(height)
    layout.addWidget(top_button)
    layout.addWidget(controls)

    parent.show()
    qapplication.processEvents()

    controls_layout = controls.layout()
    assert controls.height() == top_button.height() == height
    assert controls_layout is not None
    assert (
        controls.button.height()
        + controls_layout.spacing()
        + controls.target.height()
        == height
    )
    assert parent.sizeHint().height() == height


def test_warmup_spacing_collapses_with_hidden_control(
    qapplication: QApplication,
) -> None:
    parent = QWidget()
    layout = QHBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    on_button = QPushButton('ON')
    on_button.setFixedSize(60, 34)
    controls = SantokerWarmupControls()
    controls.setFixedSize(90, 34)
    start_button = QPushButton('START')
    start_button.setFixedSize(60, 34)
    layout.addStretch()
    layout.addWidget(on_button)
    layout.addSpacing(10)
    layout.addWidget(controls)
    layout.addWidget(start_button)

    parent.show()
    qapplication.processEvents()

    controls_right = controls.button.mapTo(
        parent, QPoint(controls.button.width() - 1, 0)
    ).x()
    assert controls.x() - (on_button.x() + on_button.width()) == 10
    assert start_button.x() - controls_right - 1 == 10

    controls.hide()
    qapplication.processEvents()

    assert start_button.x() - (on_button.x() + on_button.width()) == 10


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


def test_typing_commits_once_on_focus_loss(qapplication: QApplication) -> None:
    parent = QWidget()
    layout = QVBoxLayout(parent)
    controls = SantokerWarmupControls()
    controls.configureTarget('C', 200.0)
    focus_recipient = QLineEdit()
    layout.addWidget(controls)
    layout.addWidget(focus_recipient)
    parent.show()
    parent.activateWindow()
    qapplication.processEvents()

    changed = Mock()
    controls.targetChanged.connect(changed)
    line_edit = controls.target.lineEdit()
    assert line_edit is not None
    line_edit.setFocus()
    line_edit.selectAll()
    QTest.keyClicks(line_edit, '205')  # type: ignore[call-arg,arg-type]
    qapplication.processEvents()
    changed.assert_not_called()

    focus_recipient.setFocus()
    qapplication.processEvents()

    assert focus_recipient.hasFocus()
    assert controls.target.value() == 205
    changed.assert_called_once_with(205)


def test_set_state_blocks_button_signal(qapplication: QApplication) -> None:  # noqa: ARG001
    controls = SantokerWarmupControls()
    changed = Mock()
    controls.enabledChanged.connect(changed)

    controls.setState(True)

    assert controls.button.isChecked()
    changed.assert_not_called()
