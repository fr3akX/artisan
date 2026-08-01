from collections.abc import Generator

import pytest
import requests.sessions


@pytest.fixture(autouse=True)
def block_unmocked_requests(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    def raise_for_request(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('unexpected network request')

    monkeypatch.setattr(requests.sessions.Session, 'request', raise_for_request)
    yield
