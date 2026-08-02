from collections.abc import Generator

from PyQt6.QtWidgets import QApplication
import pytest
import requests.adapters


@pytest.fixture(scope='session', autouse=True)
def roastserver_qapplication() -> Generator[QApplication, None, None]:
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    if not isinstance(application, QApplication):
        pytest.fail('Roast Server Qt tests require QApplication, not QCoreApplication')
    yield application


@pytest.fixture(autouse=True)
def block_unmocked_requests(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    attempts: list[None] = []

    def raise_for_request(*_args: object, **_kwargs: object) -> object:
        attempts.append(None)
        raise AssertionError('unexpected network request')

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', raise_for_request)
    yield
    assert attempts == []
