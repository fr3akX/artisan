#
# ABOUT
# Artisan Roast Server connector response contracts
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

from typing import Literal
from uuid import UUID

from PyQt6.QtCore import QUrl, Qt, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from artisanlib.roastserver.contract import FailureKind, PublicFailure, UploadProgress
from artisanlib.roastserver.origin import canonical_origin
from artisanlib.roastserver.settings import ConnectorSettings


def server_page_url(origin: str, page: Literal['roasts', 'inventory'], item_id: UUID) -> str:
    if page not in {'roasts', 'inventory'} or not isinstance(item_id, UUID):
        raise ValueError('invalid server page')
    return f'{canonical_origin(origin)}/{page}/{item_id.hex}'


def open_server_page(origin: str, page: Literal['roasts', 'inventory'], item_id: UUID) -> bool:
    # Browser authentication remains independent; URLs contain no bearer credential.
    return QDesktopServices.openUrl(QUrl(server_page_url(origin, page, item_id)))


class UploadStatusWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._progress: UploadProgress | None = None
        self.label = QLabel(QApplication.translate('RoastServer', 'No uploads this session.'), self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAccessibleName(QApplication.translate('RoastServer', 'Roast Server upload status'))
        self.last_success = QLabel('', self)
        self.last_success.setTextFormat(Qt.TextFormat.PlainText)
        self.open_button = QPushButton(QApplication.translate('RoastServer', 'Open on server'), self)
        self.open_button.setEnabled(False)
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self._open)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        layout.addWidget(self.last_success)
        layout.addWidget(self.open_button)

    @pyqtSlot(object)
    def show_progress(self, value: object) -> None:
        if not isinstance(value, UploadProgress):
            return
        self._progress = value
        labels = {
            'queued': QApplication.translate('RoastServer', 'Upload queued'),
            'uploading': QApplication.translate('RoastServer', 'Uploading'),
            'uploaded': QApplication.translate('RoastServer', 'Uploaded'),
            'retrying': QApplication.translate('RoastServer', 'Upload will retry'),
            'paused': QApplication.translate('RoastServer', 'Upload paused: replace the token in Roast Server settings'),
            'failed': QApplication.translate('RoastServer', 'Upload failed: review the upload queue'),
        }
        caption = labels[value.state]
        if value.state == 'uploaded':
            self.last_success.setText(QApplication.translate('RoastServer', 'Last successful upload: {time}').format(
                time=value.occurred_at.astimezone().strftime('%Y-%m-%d %H:%M')))
        self.label.setText(caption)
        self.label.setToolTip(QApplication.translate('RoastServer', 'Roast {roast}').format(roast=str(value.roast_uuid)))
        self.open_button.setEnabled(value.state == 'uploaded')

    @pyqtSlot(object)
    def settings_changed(self, value: object) -> None:
        if not isinstance(value, ConnectorSettings) or self._progress is None:
            return
        if (value.origin != self._progress.namespace.origin or value.identity is None
                or value.identity.organization.id != self._progress.namespace.organization_id):
            self._progress = None
            self.label.setText(QApplication.translate('RoastServer', 'No uploads this session.'))
            self.last_success.clear()
            self.open_button.setEnabled(False)

    @pyqtSlot(str, object)
    def operation_failed(self, _operation: str, value: object) -> None:
        if isinstance(value, PublicFailure) and value.kind is FailureKind.CREDENTIAL_REJECTED:
            self.label.setText(QApplication.translate('RoastServer', 'Authentication paused: replace the token in Roast Server settings.'))
            self.open_button.setEnabled(False)

    @pyqtSlot()
    def _open(self) -> None:
        value = self._progress
        if (value is not None and value.state == 'uploaded'
                and not open_server_page(value.namespace.origin, 'roasts', value.roast_uuid)):
            self.label.setText(QApplication.translate('RoastServer', 'Could not open the browser.'))
