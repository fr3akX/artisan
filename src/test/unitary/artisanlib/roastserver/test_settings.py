from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
import re
from uuid import UUID

from PyQt6.QtCore import QByteArray, QCoreApplication, QSettings
import pytest

from artisanlib.roastserver.contract import IdentityOrganization, IdentityUser, ServerIdentity
from artisanlib.roastserver.settings import (
    DEFAULT_CACHE_LIMIT_BYTES,
    DEFAULT_ORIGIN,
    KEYRING_FAILURE_MESSAGE,
    KEYRING_SERVICE,
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    CredentialStoreError,
    SettingsError,
    SettingsStore,
    SystemCredentialStore,
    canonical_origin,
    credential_account,
    namespace_for,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.get_error: Exception | None = None
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None

    def get_password(self, service_name: str, account_name: str) -> str | None:
        if self.get_error is not None:
            raise self.get_error
        return self.values.get((service_name, account_name))

    def set_password(self, service_name: str, account_name: str, credential: str) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.values[(service_name, account_name)] = credential

    def delete_password(self, service_name: str, account_name: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        if (service_name, account_name) not in self.values:
            raise RuntimeError('missing credential')
        del self.values[(service_name, account_name)]


@pytest.fixture(scope='session', autouse=True)
def qcoreapplication() -> Generator[QCoreApplication, None, None]:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


@pytest.fixture
def qsettings(tmp_path: Path) -> Generator[QSettings, None, None]:
    qsettings = QSettings(str(tmp_path / 'roastserver.ini'), QSettings.Format.IniFormat)
    qsettings.clear()
    qsettings.sync()
    yield qsettings
    qsettings.clear()
    qsettings.sync()


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def identity() -> ServerIdentity:
    return ServerIdentity(
        user=IdentityUser(
            id=UUID('11111111-1111-4111-8111-111111111111'),
            email='owner@example.test',
            nickname='Owner',
        ),
        organization=IdentityOrganization(
            id=UUID('22222222-2222-4222-8222-222222222222'),
            name='Roastery',
            slug='roastery',
        ),
        role='admin',
    )


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        (' HTTPS://Example.COM:443/ ', 'https://example.com'),
        ('https://example.com:8443', 'https://example.com:8443'),
        ('http://127.0.0.1:8000/', 'http://127.0.0.1:8000'),
        ('http://[::1]:8000', 'http://[::1]:8000'),
        ('https://BÜCHER.example', 'https://xn--bcher-kva.example'),
    ],
)
def test_canonical_origin(raw: str, expected: str) -> None:
    assert canonical_origin(raw) == expected


@pytest.mark.parametrize(
    'raw',
    [
        'http://example.com',
        'http://localhost:8000',
        'https://user@example.com',
        'https://example.com/api',
        'https://example.com/?query=1',
        'https://example.com/#fragment',
        'https://exa mple.com',
        'https://example\n.com',
    ],
)
def test_origin_policy_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(SettingsError, match='valid HTTPS origin'):
        canonical_origin(raw)


def test_namespace_and_account_hashes_are_scoped() -> None:
    organization_id = UUID('22222222-2222-4222-8222-222222222222')
    other_organization_id = UUID('33333333-3333-4333-8333-333333333333')

    namespace = namespace_for('https://Example.com:443', organization_id)
    other_origin_namespace = namespace_for('https://other.example', organization_id)
    other_organization_namespace = namespace_for('https://example.com', other_organization_id)

    assert namespace.origin == 'https://example.com'
    assert namespace.organization_id == organization_id
    assert re.fullmatch(r'namespace-sha256:[0-9a-f]{64}', namespace.key)
    assert namespace.key != other_origin_namespace.key
    assert namespace.key != other_organization_namespace.key
    assert re.fullmatch(r'origin-sha256:[0-9a-f]{64}', credential_account('https://Example.com'))


def test_settings_never_store_credential_and_auto_upload_defaults_false(qsettings: QSettings) -> None:
    loaded = SettingsStore(qsettings).load()

    assert loaded.origin == DEFAULT_ORIGIN
    assert not loaded.enabled
    assert not loaded.automatic_upload
    assert loaded.identity is None
    assert loaded.cache_limit_bytes == DEFAULT_CACHE_LIMIT_BYTES
    assert not any(
        'token' in key.casefold() or 'credential' in key.casefold()
        for key in qsettings.allKeys()
    )


def test_settings_store_persists_only_roastserver_keys_and_stable_client_uuid(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    store = SettingsStore(qsettings)
    configuration_geometry = QByteArray(b'configuration')
    browser_geometry = QByteArray(b'browser')

    store.save_connection(' HTTPS://Example.COM:443/ ', identity)
    store.save_options(enabled=True, automatic_upload=False, cache_limit_bytes=DEFAULT_CACHE_LIMIT_BYTES)
    store.save_geometry(configuration_geometry, browser_geometry)

    loaded = store.load()
    qsettings.sync()

    assert loaded.origin == 'https://example.com'
    assert loaded.enabled
    assert not loaded.automatic_upload
    assert loaded.identity == identity
    assert loaded.configuration_geometry == configuration_geometry
    assert loaded.browser_geometry == browser_geometry
    assert store.load().client_instance_uuid == loaded.client_instance_uuid
    assert set(qsettings.allKeys()) == {
        'RoastServer/origin',
        'RoastServer/enabled',
        'RoastServer/automaticUpload',
        'RoastServer/clientInstanceUUID',
        'RoastServer/identityUserID',
        'RoastServer/identityUserEmail',
        'RoastServer/identityUserNickname',
        'RoastServer/identityOrganizationID',
        'RoastServer/identityOrganizationName',
        'RoastServer/identityOrganizationSlug',
        'RoastServer/identityRole',
        'RoastServer/cacheLimitBytes',
        'RoastServer/configurationGeometry',
        'RoastServer/browserGeometry',
    }


def test_settings_store_requires_confirmed_identity_for_automatic_upload_and_bounds_cache(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    store = SettingsStore(qsettings)

    with pytest.raises(SettingsError, match='confirmed identity'):
        store.save_options(enabled=True, automatic_upload=True, cache_limit_bytes=DEFAULT_CACHE_LIMIT_BYTES)

    store.save_connection(DEFAULT_ORIGIN, identity)

    minimum = store.save_options(enabled=True, automatic_upload=True, cache_limit_bytes=MIN_CACHE_LIMIT_BYTES - 1)
    maximum = store.save_options(enabled=True, automatic_upload=True, cache_limit_bytes=MAX_CACHE_LIMIT_BYTES + 1)

    assert minimum.automatic_upload
    assert minimum.cache_limit_bytes == MIN_CACHE_LIMIT_BYTES
    assert maximum.cache_limit_bytes == MAX_CACHE_LIMIT_BYTES


def test_set_origin_clears_identity_and_disables_when_origin_changes(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    store = SettingsStore(qsettings)

    store.save_connection(DEFAULT_ORIGIN, identity)
    store.save_options(enabled=True, automatic_upload=True, cache_limit_bytes=DEFAULT_CACHE_LIMIT_BYTES)

    unchanged = store.set_origin(' HTTPS://ARTISAN.FRXHOME.CHOWN.LV:443/ ')
    changed = store.set_origin('https://other.example:443')
    qsettings.sync()

    assert unchanged.identity == identity
    assert unchanged.enabled
    assert unchanged.automatic_upload
    assert changed.origin == 'https://other.example'
    assert changed.identity is None
    assert not changed.enabled
    assert not changed.automatic_upload
    assert {key for key in qsettings.allKeys() if 'identity' in key} == set()


def test_save_geometry_removes_absent_values(qsettings: QSettings) -> None:
    store = SettingsStore(qsettings)

    store.save_geometry(QByteArray(b'configuration'), QByteArray(b'browser'))
    store.save_geometry(None, None)

    loaded = store.load()
    assert loaded.configuration_geometry is None
    assert loaded.browser_geometry is None


def test_keyring_get_set_delete_and_missing_delete_are_isolated(fake_keyring: FakeKeyring) -> None:
    store = SystemCredentialStore(fake_keyring)
    secret = ''.join(chr(value) for value in (115, 101, 99, 114, 101, 116))

    store.set(' HTTPS://Example.COM:443/ ', secret)

    assert fake_keyring.values == {
        (KEYRING_SERVICE, credential_account('https://example.com')): secret,
    }
    assert store.get('https://example.com') == secret

    store.delete('https://example.com')
    store.delete('https://missing.example')

    assert store.get('https://example.com') is None


def test_keyring_failure_has_fixed_message_and_no_secret(fake_keyring: FakeKeyring) -> None:
    secret = ''.join(chr(value) for value in (115, 101, 99, 114, 101, 116))
    fake_keyring.set_error = RuntimeError('backend echoed ' + secret)

    with pytest.raises(CredentialStoreError) as raised:
        SystemCredentialStore(fake_keyring).set('https://example.test', secret)

    assert raised.value.args == (KEYRING_FAILURE_MESSAGE,)
    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)
