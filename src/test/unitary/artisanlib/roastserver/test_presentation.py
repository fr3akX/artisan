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

from datetime import UTC, datetime
from uuid import UUID
from typing import Literal

import pytest
from PyQt6.QtWidgets import QApplication

from artisanlib.roastserver.contract import UploadProgress
from artisanlib.roastserver.presentation import UploadStatusWidget, server_page_url
from artisanlib.roastserver.settings import namespace_for

_APP = QApplication.instance() or QApplication([])


def test_browser_handoff_uses_only_origin_and_canonical_id() -> None:
    item = UUID('11111111-1111-4111-8111-111111111111')
    assert server_page_url('https://example.test', 'inventory', item) == (
        'https://example.test/inventory/' + item.hex)
    for origin in ['https://user:secret@example.test', 'https://example.test/?token=secret']:
        with pytest.raises(ValueError):
            server_page_url(origin, 'roasts', item)


def test_upload_status_opens_browser_only_after_confirmed_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    from artisanlib.roastserver import presentation
    calls: list[object] = []
    monkeypatch.setattr(presentation, 'open_server_page', lambda *args: calls.append(args) or True)
    item = UUID('11111111-1111-4111-8111-111111111111')
    namespace = namespace_for('https://example.test', item)
    widget = UploadStatusWidget()
    states: tuple[Literal['queued', 'uploading', 'retrying', 'paused', 'failed'], ...] = (
        'queued', 'uploading', 'retrying', 'paused', 'failed')
    for state in states:
        progress = UploadProgress(namespace, item, state, datetime.now(UTC))
        widget.show_progress(progress)
        assert not widget.open_button.isEnabled()
    widget.show_progress(UploadProgress(namespace, item, 'uploaded', datetime.now(UTC)))
    assert widget.open_button.isEnabled()
    widget.open_button.click()
    assert calls == [(namespace.origin, 'roasts', item)]
    widget.close()
