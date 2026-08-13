#
# ABOUT
# Santoker diagnostics dialog.
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
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

import logging
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QMessageBox,
    QWidget,
)
from typing import override, final

from artisanlib.santoker_diagnostics import (
    SantokerDiagnosticEvent,
    SantokerDiagnosticsSession,
    SantokerDiagnosticsView,
)


_LOG = logging.getLogger(__name__)
_REFRESH_INTERVAL_MS = 250
_UNKNOWN_TEXT = QApplication.translate('Label', 'unknown')


def _normalize_report(report: str) -> str:
    return report.replace('\r\n', '\n').replace('\r', '\n')


def create_santoker_diagnostics_button(
    callback: Callable[[], None],
    parent: QWidget | None = None,
) -> QPushButton:
    button = QPushButton(QApplication.translate('Button', 'Diagnostics…'), parent)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.clicked.connect(lambda _checked=False: callback())
    return button


@final
class SantokerDiagnosticsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        session_provider: Callable[[], SantokerDiagnosticsSession | None],
    ) -> None:
        super().__init__(parent)

        self._session_provider = session_provider
        self._session: SantokerDiagnosticsSession | None = None
        self._last_sequence = 0
        self._first_retained_sequence: int | None = None
        self._last_refresh_failed = False

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setModal(False)
        self.setWindowTitle(QApplication.translate('Form Caption', 'Santoker Diagnostics'))

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self.refresh)

        self._build_ui()

    @property
    def history(self) -> QPlainTextEdit:
        return self._history

    def _build_ui(self) -> None:
        session_group = QGroupBox(QApplication.translate('GroupBox', 'Session and Connection'))
        session_layout = QGridLayout(session_group)
        session_layout.setColumnStretch(1, 1)

        self._monitoring_value = self._add_value_row(
            session_layout,
            0,
            QApplication.translate('Label', 'Monitoring'),
            'valueMonitoring',
        )
        self._transport_value = self._add_value_row(
            session_layout,
            1,
            QApplication.translate('Label', 'Transport'),
            'valueTransport',
        )
        self._connected_value = self._add_value_row(
            session_layout,
            2,
            QApplication.translate('Label', 'Connected'),
            'valueConnected',
        )
        self._ready_value = self._add_value_row(
            session_layout,
            3,
            QApplication.translate('Label', 'Ready'),
            'valueReady',
        )
        self._header_value = self._add_value_row(
            session_layout,
            4,
            QApplication.translate('Label', 'Header'),
            'valueHeader',
        )
        self._reconnect_count_value = self._add_value_row(
            session_layout,
            5,
            QApplication.translate('Label', 'Reconnect Count'),
            'valueReconnectCount',
        )
        self._last_packet_value = self._add_value_row(
            session_layout,
            6,
            QApplication.translate('Label', 'Last Packet'),
            'valueLastPacket',
        )

        machine_group = QGroupBox(QApplication.translate('GroupBox', 'Machine State'))
        machine_layout = QGridLayout(machine_group)
        machine_layout.setColumnStretch(1, 1)

        self._board_value = self._add_value_row(
            machine_layout,
            0,
            QApplication.translate('Label', 'Board'),
            'valueBoard',
        )
        self._bean_value = self._add_value_row(
            machine_layout,
            1,
            QApplication.translate('Label', 'Bean'),
            'valueBean',
        )
        self._environment_value = self._add_value_row(
            machine_layout,
            2,
            QApplication.translate('Label', 'Environment'),
            'valueEnvironment',
        )
        self._infrared_value = self._add_value_row(
            machine_layout,
            3,
            QApplication.translate('Label', 'Infrared'),
            'valueInfrared',
        )
        self._bean_ror_value = self._add_value_row(
            machine_layout,
            4,
            QApplication.translate('Label', 'Bean ROR'),
            'valueBeanRor',
        )
        self._environment_ror_value = self._add_value_row(
            machine_layout,
            5,
            QApplication.translate('Label', 'Environment ROR'),
            'valueEnvironmentRor',
        )
        self._power_value = self._add_value_row(
            machine_layout,
            6,
            QApplication.translate('Label', 'Power'),
            'valuePower',
        )
        self._fan_value = self._add_value_row(
            machine_layout,
            7,
            QApplication.translate('Label', 'Fan'),
            'valueFan',
        )
        self._drum_value = self._add_value_row(
            machine_layout,
            8,
            QApplication.translate('Label', 'Drum'),
            'valueDrum',
        )

        warmup_group = QGroupBox(QApplication.translate('GroupBox', 'Warm-up State'))
        warmup_layout = QGridLayout(warmup_group)
        warmup_layout.setColumnStretch(1, 1)

        self._desired_warmup_value = self._add_value_row(
            warmup_layout,
            0,
            QApplication.translate('Label', 'Desired warm-up'),
            'valueDesiredWarmup',
        )
        self._desired_warmup_target_value = self._add_value_row(
            warmup_layout,
            1,
            QApplication.translate('Label', 'Desired target'),
            'valueDesiredWarmupTarget',
        )
        self._reported_warmup_value = self._add_value_row(
            warmup_layout,
            2,
            QApplication.translate('Label', 'Reported warm-up'),
            'valueReportedWarmup',
        )
        self._reported_warmup_target_value = self._add_value_row(
            warmup_layout,
            3,
            QApplication.translate('Label', 'Reported target'),
            'valueReportedWarmupTarget',
        )
        self._restoration_value = self._add_value_row(
            warmup_layout,
            4,
            QApplication.translate('Label', 'Restoration'),
            'valueRestoration',
        )
        self._charge_latch_value = self._add_value_row(
            warmup_layout,
            5,
            QApplication.translate('Label', 'CHARGE latch'),
            'valueChargeLatch',
        )

        self._history_discarded = QLabel()
        self._history_discarded.setObjectName('valueDiscarded')

        history_label = QLabel(QApplication.translate('Label', 'History'))

        self._history = QPlainTextEdit()
        self._history.setReadOnly(True)

        button_container = QVBoxLayout()

        copy_button = QPushButton(QApplication.translate('Button', 'Copy All'))
        copy_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        copy_button.clicked.connect(self._copy_all)

        save_button = QPushButton(QApplication.translate('Button', 'Save as Text…'))
        save_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        save_button.clicked.connect(self._save_as_text)

        close_button = QPushButton(QApplication.translate('Button', 'Close'))
        close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_button.clicked.connect(self.close)

        button_container.addWidget(copy_button)
        button_container.addWidget(save_button)
        button_container.addWidget(close_button)

        root_layout = QVBoxLayout(self)
        root_layout.addWidget(session_group)
        root_layout.addWidget(machine_group)
        root_layout.addWidget(warmup_group)
        root_layout.addWidget(history_label)
        root_layout.addWidget(self._history_discarded)
        root_layout.addWidget(self._history)
        root_layout.addLayout(button_container)

        self._set_no_session_state()

    def _add_value_row(
        self,
        layout: QGridLayout,
        row: int,
        label_text: str,
        object_name: str,
    ) -> QLabel:
        label = QLabel(label_text)
        value = QLabel(_UNKNOWN_TEXT)
        value.setObjectName(object_name)
        layout.addWidget(label, row, 0)
        layout.addWidget(value, row, 1)
        return value

    def _set_no_session_state(self) -> None:
        self._session = None
        self._last_sequence = 0
        self._first_retained_sequence = None
        self._last_refresh_failed = False
        for value_label in (
            self._monitoring_value,
            self._transport_value,
            self._connected_value,
            self._ready_value,
            self._header_value,
            self._reconnect_count_value,
            self._last_packet_value,
            self._board_value,
            self._bean_value,
            self._environment_value,
            self._infrared_value,
            self._bean_ror_value,
            self._environment_ror_value,
            self._power_value,
            self._fan_value,
            self._drum_value,
            self._desired_warmup_value,
            self._desired_warmup_target_value,
            self._reported_warmup_value,
            self._reported_warmup_target_value,
            self._restoration_value,
            self._charge_latch_value,
        ):
            value_label.setText(_UNKNOWN_TEXT)
        self._history_discarded.setText('')
        self._history.setPlainText(QApplication.translate('Message', 'No Santoker monitoring session captured'))

    @override
    def showEvent(self, event: QShowEvent | None = None) -> None:
        super().showEvent(event)
        self.refresh()
        self._refresh_timer.start()

    @override
    def closeEvent(self, event: QCloseEvent | None = None) -> None:
        self._refresh_timer.stop()
        self.hide()
        if event is not None:
            event.ignore()

    def refresh(self) -> None:
        session = self._resolve_session()
        if session is None:
            if self._last_refresh_failed:
                return
            self._set_no_session_state()
            return

        try:
            if session is self._session and self._last_sequence > 0:
                view = session.view(self._last_sequence)
                rebuild = False

                if self._first_retained_sequence is not None:
                    if view.first_retained_sequence is not None:
                        if view.first_retained_sequence > self._first_retained_sequence:
                            rebuild = True
                    else:
                        rebuild = True
                else:
                    rebuild = True

                if rebuild:
                    view = session.view()
                self._load_view(view, replace=rebuild)
            else:
                view = session.view()
                self._load_view(view, replace=True)
        except Exception:
            self._last_refresh_failed = True
            _LOG.exception('failed to refresh Santoker diagnostics')
            self._history.setPlainText(QApplication.translate('Message', 'Diagnostics unavailable'))
            return

        self._last_refresh_failed = False
        self._session = session
        self._last_sequence = view.last_sequence
        self._first_retained_sequence = view.first_retained_sequence
        self._populate_summary(view)

    def _load_view(self, view: SantokerDiagnosticsView, replace: bool) -> None:
        lines = [
            self._format_event_line(event)
            for event in view.events
        ]
        text = '\n'.join(lines)
        if replace:
            self._history.setPlainText(text)
        else:
            current = self._history.toPlainText()
            if text:
                if current:
                    self._history.setPlainText(current + '\n' + text)
                else:
                    self._history.setPlainText(text)

    @staticmethod
    def _format_event_line(event: SantokerDiagnosticEvent) -> str:
        line = f'{event.sequence:>6} {event.timestamp_utc.isoformat()} {event.category}'
        if event.direction is not None:
            line += f' {event.direction}'
        line += f': {event.description}'
        if event.packet is not None:
            packet_text = ' '.join(f'{value:02X}' for value in event.packet)
            if packet_text:
                line += f' {packet_text}'
        return line

    def _populate_summary(self, view: SantokerDiagnosticsView) -> None:
        state = view.state
        self._monitoring_value.setText(self._render_value(state.monitoring_active))
        self._transport_value.setText(self._render_value(state.transport))
        self._connected_value.setText(self._render_value(state.connected))
        self._ready_value.setText(self._render_value(state.protocol_ready))
        self._header_value.setText(self._render_value(state.active_header))
        self._reconnect_count_value.setText(self._render_value(state.reconnect_count))
        self._last_packet_value.setText(self._render_value(state.last_packet_utc))

        self._board_value.setText(self._render_value(state.board_c))
        self._bean_value.setText(self._render_value(state.bt_c))
        self._environment_value.setText(self._render_value(state.et_c))
        self._infrared_value.setText(self._render_value(state.ir_c))
        self._bean_ror_value.setText(self._render_value(state.bt_ror_c))
        self._environment_ror_value.setText(self._render_value(state.et_ror_c))
        self._power_value.setText(self._render_value(state.power))
        self._fan_value.setText(self._render_value(state.fan))
        self._drum_value.setText(self._render_value(state.drum))

        self._desired_warmup_value.setText(self._render_value(state.desired_warmup))
        self._desired_warmup_target_value.setText(self._render_value(state.desired_target_c))
        self._reported_warmup_value.setText(self._render_value(state.reported_warmup))
        self._reported_warmup_target_value.setText(self._render_value(state.reported_target_c))
        self._restoration_value.setText(self._render_value(state.restoration_state))
        self._charge_latch_value.setText(self._render_value(state.charge_latched))

        if view.first_retained_sequence is None:
            self._history_discarded.setText('')
        else:
            self._history_discarded.setText(
                QApplication.translate('Message', '[older entries discarded: %1]').replace(
                    '%1', str(view.state.discarded_event_count)
                )
            )

    @staticmethod
    def _render_value(value: object) -> str:
        if value is None:
            return _UNKNOWN_TEXT
        if isinstance(value, Enum):
            return str(value.value)
        if hasattr(value, 'isoformat'):
            return value.isoformat()  # type: ignore[no-any-return]
        return str(value)

    def _resolve_session(self) -> SantokerDiagnosticsSession | None:
        try:
            session = self._session_provider()
            self._last_refresh_failed = False
            return session
        except Exception:
            self._last_refresh_failed = True
            _LOG.exception('failed to retrieve Santoker diagnostics session')
            self._history.setPlainText(QApplication.translate('Message', 'Diagnostics unavailable'))
            return None

    def _resolve_report(self) -> str:
        session = self._session_provider()
        if session is None:
            raise RuntimeError('No Santoker diagnostics session available')
        return session.format_report()

    def _copy_all(self) -> None:
        try:
            report = self._resolve_report()
            clipboard = QApplication.clipboard()
            if clipboard is None:
                raise RuntimeError('No clipboard available')
            clipboard.setText(_normalize_report(report))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception('failed to copy Santoker diagnostics')
            self._show_warning(
                QApplication.translate('Error Message', 'Failed to copy Santoker diagnostics'),
                exc,
            )

    def _save_as_text(self) -> None:
        path_text, _ = QFileDialog.getSaveFileName(self, QApplication.translate('Button', 'Save as Text…'))
        if not path_text:
            return

        try:
            report = self._resolve_report()
            Path(path_text).write_text(
                _normalize_report(report),
                encoding='utf-8',
                newline='\n',
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, OSError):
                _LOG.exception('failed to save Santoker diagnostics as text')
            else:
                _LOG.exception('failed to format Santoker diagnostics report')
            self._show_warning(
                QApplication.translate('Error Message', 'Failed to save Santoker diagnostics'),
                exc,
            )

    def _show_warning(self, message: str, exception: BaseException) -> None:
        QMessageBox.warning(
            self,
            QApplication.translate('Message', 'Error'),
            f'{message}: {exception}',
        )
