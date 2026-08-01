from collections.abc import Generator

import pytest
import requests.adapters


@pytest.fixture(autouse=True)
def block_unmocked_requests(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    attempts: list[None] = []

    def raise_for_request(*_args: object, **_kwargs: object) -> object:
        attempts.append(None)
        raise AssertionError('unexpected network request')

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', raise_for_request)
    yield
    assert attempts == []
