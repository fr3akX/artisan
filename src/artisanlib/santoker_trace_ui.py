#
# ABOUT
# Nonmodal, explicit Santoker diagnostic trace presentation.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from collections.abc import Callable
import logging
import time

from PyQt6.QtCore import QObject, QTimer, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from artisanlib.santoker_trace_runtime import Selection, TraceRuntime

_log = logging.getLogger(__name__)


def trace_message(code: str) -> str:
    messages = {
        'capture': QApplication.translate('Message', 'Diagnostic capture is incomplete or unavailable. Retained logs were not deleted.'),
        'capacity': QApplication.translate('Message', 'Diagnostic log capacity is exhausted or a diagnostic operation is busy. Capture may be incomplete.'),
        'transfer': QApplication.translate('Message', 'Diagnostic log operation failed. Retained logs require an explicit retry.'),
        'uploaded': QApplication.translate('Message', 'Diagnostic logs uploaded and local deletion recorded.'),
        'deleted': QApplication.translate('Message', 'Diagnostic log deletion recorded.'),
        'identity': QApplication.translate('Message', 'Verify the Roast Server connection before uploading diagnostic logs.'),
        'empty': QApplication.translate('Message', 'No previous diagnostic logs are available.'),
        'shutdown': QApplication.translate('Message', 'Diagnostic cleanup is incomplete. Shutdown will continue; upload outcome may be unknown.'),
    }
    return messages.get(code, messages['capture'])


class TracePresentation(QObject):
    def __init__(self, parent: QWidget, runtime: TraceRuntime, notify: Callable[[str], None]) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._window = parent
        self._notify = notify
        self._dialog: QDialog | None = None
        self._upload: QPushButton | None = None
        self._delete: QPushButton | None = None
        self._all_uploadable = False
        self._requested: int | None = None
        self._manual = False
        self._excluded_session: str | None = None
        self._page = 0
        self._deadline: float | None = None
        self._finish: Callable[[bool], None] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    def previous_sessions(self, *, manual: bool = False, page: int = 0,
                          current_session: str | None = None) -> None:
        if self.runtime.closing:
            return
        self._manual = manual
        self._excluded_session = current_session
        self._page = page
        self._requested = self.runtime.refresh(page)

    def shutdown(self, finish: Callable[[bool], None], *, seconds: float = 15.0) -> None:
        if self._deadline is not None:
            return
        if self._dialog is not None:
            self._dialog.close()
        self._requested = None
        self.runtime.shutdown()
        self._finish = finish
        self._deadline = time.monotonic() + seconds

    def poll(self) -> None:
        revision, selections, more, busy, authorized, notices = self.runtime.snapshot()
        for code in notices:
            self._notify(trace_message(code))
        if self._upload is not None:
            self._upload.setEnabled(authorized and not busy and self._all_uploadable)
        if self._delete is not None:
            self._delete.setEnabled(not busy and not self.runtime.closing)
        if self._requested is not None and revision > self._requested:
            selections = tuple(s for s in selections if s.session_id != self._excluded_session)
            if selections:
                self._requested = None
                self._show(selections, more, busy=busy, authorized=authorized)
            elif self._manual:
                self._requested = None
                self._notify(trace_message('empty'))
            else:
                self._requested = revision # wait for an older cleanup transition, not IO on ON
        if self._deadline is not None and (self.runtime.settled or time.monotonic() >= self._deadline):
            complete = self.runtime.settled
            if not complete:
                self._notify(trace_message('shutdown'))
                _log.warning('Diagnostic cleanup incomplete at application shutdown')
            finish, self._finish = self._finish, None
            self._timer.stop()
            if finish is not None:
                finish(self.runtime.cleanup_complete)

    def _show(self, selections: tuple[Selection, ...], more: bool, *, busy: bool, authorized: bool) -> None:
        if self._dialog is not None:
            self._dialog.close()
        dialog = QDialog(self._window)
        dialog.setWindowTitle(QApplication.translate('Message', 'Previous diagnostic logs'))
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(QApplication.translate('Message',
            'Raw Santoker BLE logs are private diagnostic data. Upload only to the verified Roast Server account. Closing this window keeps the logs.'), dialog))
        listing = QListWidget(dialog)
        for selection in selections:
            item = QListWidgetItem(selection.session_id, listing)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
        layout.addWidget(listing)
        upload = QPushButton(QApplication.translate('Button', 'Upload and delete'), dialog)
        delete = QPushButton(QApplication.translate('Button', 'Just delete'), dialog)
        def selected_uploadable() -> None:
            selected = tuple(s for index, s in enumerate(selections)
                             if (item := listing.item(index)) is not None and item.checkState() == Qt.CheckState.Checked)
            self._all_uploadable = bool(selected) and all(s.uploadable for s in selected)

        selected_uploadable()
        listing.itemChanged.connect(selected_uploadable)
        self._upload, self._delete = upload, delete
        upload.setEnabled(authorized and not busy and self._all_uploadable)
        delete.setEnabled(not busy)

        def submit(uploading: bool) -> None:
            # This dialog owns immutable IDs, never a later ON/current selection.
            ids = tuple(s.session_id for index, s in enumerate(selections)
                        if (item := listing.item(index)) is not None and item.checkState() == Qt.CheckState.Checked)
            if self.runtime.submit(ids, upload=uploading):
                dialog.close()

        upload.clicked.connect(lambda: submit(True))
        delete.clicked.connect(lambda: submit(False))
        layout.addWidget(upload)
        layout.addWidget(delete)
        if self._page > 0 or more:
            for title, page, enabled in (
                (QApplication.translate('Button', 'Previous'), self._page - 1, self._page > 0),
                (QApplication.translate('Button', 'Next'), self._page + 1, more),
            ):
                button = QPushButton(title, dialog)
                button.setEnabled(enabled)
                button.clicked.connect(lambda _checked=False, p=page: self.previous_sessions(manual=True, page=p))
                layout.addWidget(button)
        dialog.finished.connect(self._dismissed)
        self._dialog = dialog
        dialog.show()  # deliberately nonmodal; capture/control never wait for consent

    def _dismissed(self) -> None:
        dialog, self._dialog = self._dialog, None
        self._upload = self._delete = None
        if dialog is not None:
            dialog.deleteLater()
