from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
import re
from typing import cast
from uuid import UUID

from PyQt6.QtCore import QByteArray, QCoreApplication, QSettings
import pytest

from artisanlib.roastserver.contract import IdentityOrganization, IdentityUser, ServerIdentity
from artisanlib.roastserver.origin import canonical_origin as origin_canonical_origin
from artisanlib.roastserver.settings import (
    DEFAULT_CACHE_LIMIT_BYTES,
    DEFAULT_ORIGIN,
    KEYRING_FAILURE_MESSAGE,
    KEYRING_SERVICE,
    MAX_CACHE_LIMIT_BYTES,
    MIN_CACHE_LIMIT_BYTES,
    SETTINGS_FAILURE_MESSAGE,
    CredentialStoreError,
    PendingConnection,
    SettingsError,
    SettingsStore,
    SystemCredentialStore,
    canonical_origin,
    credential_account,
    namespace_for,
)


class FaultInjectingSettings:
    def __init__(
        self,
        backend: QSettings,
        *,
        status: QSettings.Status = QSettings.Status.NoError,
        mismatch_key: str | None = None,
    ) -> None:
        self.backend = backend
        self.status_value = status
        self.mismatch_key = mismatch_key

    def value(self, key: str) -> object:
        value = self.backend.value(key)
        if key == self.mismatch_key and isinstance(value, bool):
            return not value
        return value

    def setValue(self, key: str, value: object) -> None:  # noqa: N802
        self.backend.setValue(key, value)

    def remove(self, key: str) -> None:
        self.backend.remove(key)

    def sync(self) -> None:
        self.backend.sync()

    def status(self) -> QSettings.Status:
        return self.status_value


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


def _set_stored_identity(qsettings: QSettings, identity: ServerIdentity) -> None:
    qsettings.setValue('RoastServer/identityUserID', str(identity.user.id))
    qsettings.setValue('RoastServer/identityUserEmail', identity.user.email)
    qsettings.setValue('RoastServer/identityUserNickname', identity.user.nickname)
    qsettings.setValue('RoastServer/identityOrganizationID', str(identity.organization.id))
    qsettings.setValue('RoastServer/identityOrganizationName', identity.organization.name)
    qsettings.setValue('RoastServer/identityOrganizationSlug', identity.organization.slug)
    qsettings.setValue('RoastServer/identityRole', identity.role)


def _secret() -> str:
    return ''.join(chr(value) for value in (115, 101, 99, 114, 101, 116))


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        (' HTTPS://Example.COM:443/ ', 'https://example.com'),
        ('https://example.com:8443', 'https://example.com:8443'),
        ('http://localhost', 'http://localhost'),
        ('http://LOCALHOST:8000/', 'http://localhost:8000'),
        ('http://127.0.0.1:8000/', 'http://127.0.0.1:8000'),
        ('http://[::1]:8000', 'http://[::1]:8000'),
        ('https://[::1]', 'https://[::1]'),
        ('https://BÜCHER.example', 'https://xn--bcher-kva.example'),
        ('https://LOCALHOST:443/', 'https://localhost'),
    ],
)
def test_canonical_origin(raw: str, expected: str) -> None:
    assert canonical_origin is origin_canonical_origin
    assert canonical_origin(raw) == expected


@pytest.mark.parametrize(
    'raw',
    [
        'http://example.com',
        'https://user@example.com',
        'https://example.com/api',
        'https://example.com/?query=1',
        'https://example.com/#fragment',
        'https://exa mple.com',
        'https://example\n.com',
        'https://example.com:',
        'https://[::1]:',
        'https://example.com:abc',
        'https://_bad.example',
        'https://example..com',
        'https://example.com.',
        'https://.example.com',
        'https://example.com\\foo',
        'https://example.com%2f.evil',
        'https://127.1',
        'https://[0:0:0:0:0:0:0:1]',
        'https://[::1%25eth0]',
        'http://[0:0:0:0:0:0:0:1]',
    ],
)
def test_origin_policy_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(SettingsError, match='valid HTTPS origin'):
        canonical_origin(raw)


@pytest.mark.parametrize(
    'raw',
    [
        'https://[::1',
        'https://::1]',
        'https://[::1]extra',
        'https://[::1]:80]',
        'https://[::1]:abc',
    ],
)
def test_origin_policy_rejects_malformed_bracketed_authorities(raw: str) -> None:
    with pytest.raises(SettingsError) as raised:
        canonical_origin(raw)

    assert raised.value.args == ('Enter a valid HTTPS origin.',)
    assert raised.value.__cause__ is None
    assert raw not in str(raised.value)
    assert raw not in repr(raised.value)


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


@pytest.mark.parametrize(
    ('stored', 'expected'),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ('true', True),
        ('false', False),
    ],
)
def test_load_accepts_expected_enabled_representations(
    qsettings: QSettings,
    stored: bool | int | str,
    expected: bool,
) -> None:
    qsettings.setValue('RoastServer/enabled', stored)

    assert SettingsStore(qsettings).load().enabled is expected


@pytest.mark.parametrize('stored', ['TRUE', '1', 'yes', 'on', 2, -1, 1.0, QByteArray(b'true')])
def test_load_repairs_invalid_enabled_values(qsettings: QSettings, stored: object) -> None:
    qsettings.setValue('RoastServer/enabled', stored)

    loaded = SettingsStore(qsettings).load()

    assert not loaded.enabled
    assert qsettings.value('RoastServer/enabled') is False


def test_load_repairs_invalid_automatic_upload_values(qsettings: QSettings, identity: ServerIdentity) -> None:
    _set_stored_identity(qsettings, identity)
    qsettings.setValue('RoastServer/automaticUpload', 'yes')

    loaded = SettingsStore(qsettings).load()

    assert loaded.identity == identity
    assert not loaded.automatic_upload
    assert qsettings.value('RoastServer/automaticUpload') is False


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


@pytest.mark.parametrize('stored_origin', [123, 'https://example.com:'])
def test_invalid_origin_repair_clears_identity_and_disables_connection(
    qsettings: QSettings,
    identity: ServerIdentity,
    stored_origin: object,
) -> None:
    _set_stored_identity(qsettings, identity)
    qsettings.setValue('RoastServer/origin', stored_origin)
    qsettings.setValue('RoastServer/enabled', True)
    qsettings.setValue('RoastServer/automaticUpload', True)

    loaded = SettingsStore(qsettings).load()

    assert loaded.origin == DEFAULT_ORIGIN
    assert not loaded.enabled
    assert not loaded.automatic_upload
    assert loaded.identity is None
    assert qsettings.value('RoastServer/origin') == DEFAULT_ORIGIN
    assert qsettings.value('RoastServer/enabled') is False
    assert qsettings.value('RoastServer/automaticUpload') is False
    assert {key for key in qsettings.allKeys() if 'identity' in key} == set()


def test_load_clears_partial_identity_and_invalid_automatic_upload(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    _set_stored_identity(qsettings, identity)
    qsettings.remove('RoastServer/identityRole')
    qsettings.setValue('RoastServer/automaticUpload', True)

    loaded = SettingsStore(qsettings).load()

    assert loaded.identity is None
    assert not loaded.automatic_upload
    assert qsettings.value('RoastServer/automaticUpload') is False
    assert {key for key in qsettings.allKeys() if 'identity' in key} == set()


def test_load_repairs_invalid_client_instance_uuid(qsettings: QSettings) -> None:
    qsettings.setValue('RoastServer/clientInstanceUUID', 7)

    loaded = SettingsStore(qsettings).load()

    assert loaded.client_instance_uuid == UUID(str(qsettings.value('RoastServer/clientInstanceUUID')))


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


def test_pending_connection_is_public_only_and_promotes_in_two_durable_phases(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    store = SettingsStore(qsettings)
    previous = store.load()

    pending = store.save_pending_connection('https://example.test', identity)

    assert pending.origin == previous.origin
    assert pending.identity is None
    assert pending.pending_connection == PendingConnection(
        'https://example.test', identity
    )
    assert not pending.automatic_upload
    assert not any(
        'token' in key.casefold() or 'credential' in key.casefold()
        for key in qsettings.allKeys()
    )

    active = store.activate_pending_connection('https://example.test', identity)

    assert active.origin == 'https://example.test'
    assert active.identity == identity
    assert active.pending_connection is None
    assert not active.enabled
    assert not active.automatic_upload


def test_clear_pending_connection_durably_retains_prior_active_but_disables_it(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    store = SettingsStore(qsettings)
    store.save_connection(DEFAULT_ORIGIN, identity)
    store.save_options(True, True, DEFAULT_CACHE_LIMIT_BYTES)
    store.save_pending_connection('https://example.test', identity)

    cleared = store.clear_pending_connection()
    fresh = SettingsStore(
        QSettings(qsettings.fileName(), qsettings.format())
    ).load()

    assert cleared == fresh
    assert fresh.origin == DEFAULT_ORIGIN
    assert fresh.identity == identity
    assert fresh.pending_connection is None
    assert not fresh.enabled
    assert not fresh.automatic_upload


@pytest.mark.parametrize(
    'status',
    [QSettings.Status.AccessError, QSettings.Status.FormatError],
)
def test_security_save_status_failure_is_fixed_and_fail_closed(
    qsettings: QSettings,
    identity: ServerIdentity,
    status: QSettings.Status,
) -> None:
    SettingsStore(qsettings).save_connection(DEFAULT_ORIGIN, identity)
    faulty = FaultInjectingSettings(qsettings, status=status)

    with pytest.raises(SettingsError) as raised:
        SettingsStore(cast(QSettings, faulty)).save_options(
            enabled=True,
            automatic_upload=True,
            cache_limit_bytes=DEFAULT_CACHE_LIMIT_BYTES,
        )

    assert raised.value.args == (SETTINGS_FAILURE_MESSAGE,)
    assert raised.value.__cause__ is None
    assert qsettings.value('RoastServer/automaticUpload') is False


def test_security_save_exact_fresh_readback_mismatch_is_fixed_and_fail_closed(
    qsettings: QSettings,
    identity: ServerIdentity,
) -> None:
    SettingsStore(qsettings).save_connection(DEFAULT_ORIGIN, identity)
    def mismatched_fresh_readback() -> QSettings:
        fresh = QSettings(qsettings.fileName(), qsettings.format())
        return cast(
            QSettings,
            FaultInjectingSettings(
                fresh, mismatch_key='RoastServer/automaticUpload'
            ),
        )

    with pytest.raises(SettingsError) as raised:
        SettingsStore(
            qsettings,
            readback_factory=mismatched_fresh_readback,
        ).save_options(
            enabled=True,
            automatic_upload=True,
            cache_limit_bytes=DEFAULT_CACHE_LIMIT_BYTES,
        )

    assert raised.value.args == (SETTINGS_FAILURE_MESSAGE,)
    assert qsettings.value('RoastServer/automaticUpload') is False


def test_geometry_sync_failure_reports_only_fixed_settings_error(
    qsettings: QSettings,
) -> None:
    faulty = FaultInjectingSettings(
        qsettings,
        status=QSettings.Status.AccessError,
    )

    with pytest.raises(SettingsError) as raised:
        SettingsStore(cast(QSettings, faulty)).save_geometry(
            QByteArray(b'configuration'),
            None,
        )

    assert raised.value.args == (SETTINGS_FAILURE_MESSAGE,)
    assert raised.value.__cause__ is None


def test_save_geometry_removes_absent_values(qsettings: QSettings) -> None:
    store = SettingsStore(qsettings)

    store.save_geometry(QByteArray(b'configuration'), QByteArray(b'browser'))
    store.save_geometry(None, None)

    loaded = store.load()
    assert loaded.configuration_geometry is None
    assert loaded.browser_geometry is None


def test_loaded_geometry_is_copied_from_qsettings(qsettings: QSettings) -> None:
    original = QByteArray(b'configuration')
    qsettings.setValue('RoastServer/configurationGeometry', original)

    loaded = SettingsStore(qsettings).load()
    assert loaded.configuration_geometry == original
    assert loaded.configuration_geometry is not None

    loaded.configuration_geometry.append(b'!')

    reloaded = SettingsStore(qsettings).load()
    assert reloaded.configuration_geometry == original


def test_keyring_get_set_delete_and_missing_delete_are_isolated(fake_keyring: FakeKeyring) -> None:
    store = SystemCredentialStore(fake_keyring)
    secret = _secret()

    store.set(' HTTPS://Example.COM:443/ ', secret)

    assert fake_keyring.values == {
        (KEYRING_SERVICE, credential_account('https://example.com')): secret,
    }
    assert store.get('https://example.com') == secret

    store.delete('https://example.com')
    store.delete('https://missing.example')

    assert store.get('https://example.com') is None


@pytest.mark.parametrize('operation', ['get', 'set', 'delete'])
def test_keyring_failures_have_fixed_public_errors(
    fake_keyring: FakeKeyring,
    operation: str,
) -> None:
    store = SystemCredentialStore(fake_keyring)
    secret = _secret()
    backend_error = RuntimeError('backend echoed ' + secret)
    action: Callable[[], object]

    match operation:
        case 'get':
            fake_keyring.get_error = backend_error

            def action() -> object:
                return store.get('https://example.test')

        case 'set':
            fake_keyring.set_error = backend_error

            def action() -> object:
                store.set('https://example.test', secret)
                return None

        case 'delete':
            fake_keyring.values[(KEYRING_SERVICE, credential_account('https://example.test'))] = secret
            fake_keyring.delete_error = backend_error

            def action() -> object:
                store.delete('https://example.test')
                return None

        case _:
            raise AssertionError('unexpected operation')

    with pytest.raises(CredentialStoreError) as raised:
        action()

    assert raised.value.args == (KEYRING_FAILURE_MESSAGE,)
    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_keyring_delete_tolerates_missing_entry_after_backend_delete_error(fake_keyring: FakeKeyring) -> None:
    fake_keyring.delete_error = RuntimeError('delete failed')

    SystemCredentialStore(fake_keyring).delete('https://missing.example')
