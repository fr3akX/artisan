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

from typing import Literal

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFrame, QPushButton, QSpinBox, QVBoxLayout, QWidget


class SantokerWarmupControls(QFrame):
    enabledChanged = pyqtSignal(bool)
    targetChanged = pyqtSignal(int)

    trailing_spacing = 10

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.button: QPushButton = QPushButton(QApplication.translate('Button', 'WARM-UP'))
        self.button.setCheckable(True)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setToolTip(QApplication.translate('Tooltip', 'Santoker warm-up'))

        self.target: QSpinBox = QSpinBox()
        self.target.setKeyboardTracking(False)
        self.target.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.target.setToolTip(QApplication.translate('Tooltip', 'Warm-up target'))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, self.trailing_spacing, 0)
        layout.setSpacing(2)
        layout.addWidget(self.button)
        layout.addWidget(self.target)

        self.button.clicked.connect(self.enabledChanged.emit)
        self.target.valueChanged.connect(self.targetChanged.emit)

    def setCompactHeight(self, height: int) -> None:
        layout = self.layout()
        if layout is None:
            return
        fixed_height = max(2, height)
        spacing = min(layout.spacing(), fixed_height - 2)
        child_height = fixed_height - spacing
        button_height = (child_height + 1) // 2
        target_height = child_height - button_height
        layout.setSpacing(spacing)
        self.button.setFixedHeight(button_height)
        self.target.setFixedHeight(target_height)
        self.setFixedHeight(fixed_height)

    def configureTarget(self, unit: Literal['C', 'F'], value: float) -> None:
        was_blocked = self.target.blockSignals(True)
        try:
            minimum = 212 if unit == 'F' else 100
            maximum = 572 if unit == 'F' else 300
            suffix = ' °F' if unit == 'F' else ' °C'
            self.target.setRange(minimum, maximum)
            self.target.setSuffix(suffix)
            self.target.setValue(int(round(value)))
        finally:
            self.target.blockSignals(was_blocked)

    def setState(self, enabled: bool) -> None:
        was_blocked = self.button.blockSignals(True)
        try:
            self.button.setChecked(enabled)
        finally:
            self.button.blockSignals(was_blocked)
