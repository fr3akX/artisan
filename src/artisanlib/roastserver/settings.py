#
# ABOUT
# Artisan Roast Server connector settings and credential isolation
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

from dataclasses import dataclass
import hashlib
from typing import Final, Literal, Protocol, cast
from uuid import UUID, uuid4

from PyQt6.QtCore import QByteArray, QSettings

from artisanlib.roastserver.contract import IdentityOrganization, IdentityUser, Namespace, ServerIdentity
from artisanlib.roastserver.origin import SettingsError, canonical_origin

KEYRING_SERVICE: Final[str] = 'org.artisan-scope.Artisan.RoastServer'
DEFAULT_ORIGIN: Final[str] = 'https://artisan.frxhome.chown.lv'
DEFAULT_CACHE_LIMIT_BYTES: Final[int] = 512 * 1024 * 1024
MIN_CACHE_LIMIT_BYTES: Final[int] = 64 * 1024 * 1024
MAX_CACHE_LIMIT_BYTES: Final[int] = 4 * 1024 * 1024 * 1024
KEYRING_FAILURE_MESSAGE: Final[str] = (
    'The Roast Server credential could not be stored in the operating-system keyring. '
    'Verify that your system keyring is available and try again.'
)

_GROUP_NAME: Final[str] = 'RoastServer'
_ALLOWED_GROUP_KEYS: Final[frozenset[str]] = frozenset(
    {
        'origin',
        'enabled',
        'automaticUpload',
        'clientInstanceUUID',
        'identityUserID',
        'identityUserEmail',
        'identityUserNickname',
        'identityOrganizationID',
        'identityOrganizationName',
        'identityOrganizationSlug',
        'identityRole',
        'cacheLimitBytes',
        'configurationGeometry',
        'browserGeometry',
    }
)
_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    'identityUserID',
    'identityUserEmail',
    'identityUserNickname',
    'identityOrganizationID',
    'identityOrganizationName',
    'identityOrganizationSlug',
    'identityRole',
)
_IDENTITY_ROLE_VALUES: Final[frozenset[str]] = frozenset({'admin', 'member'})
_VALID_BOOLEAN_STRINGS: Final[dict[str, bool]] = {'true': True, 'false': False}


class CredentialStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectorSettings:
    origin: str
    enabled: bool
    automatic_upload: bool
    client_instance_uuid: UUID
    identity: ServerIdentity | None
    cache_limit_bytes: int
    configuration_geometry: QByteArray | None
    browser_geometry: QByteArray | None


class CredentialStore(Protocol):
    def get(self, origin: str) -> str | None: ...
    def set(self, origin: str, credential: str) -> None: ...
    def delete(self, origin: str) -> None: ...


class _KeyringBackend(Protocol):
    def get_password(self, service_name: str, account_name: str) -> str | None: ...
    def set_password(self, service_name: str, account_name: str, credential: str) -> None: ...
    def delete_password(self, service_name: str, account_name: str) -> None: ...


class SystemCredentialStore:
    def __init__(self, backend: _KeyringBackend) -> None:
        self._backend = backend

    def get(self, origin: str) -> str | None:
        account_name = credential_account(origin)
        try:
            credential = self._backend.get_password(KEYRING_SERVICE, account_name)
        except Exception:
            raise CredentialStoreError(KEYRING_FAILURE_MESSAGE) from None
        return credential

    def set(self, origin: str, credential: str) -> None:
        account_name = credential_account(origin)
        try:
            self._backend.set_password(KEYRING_SERVICE, account_name, credential)
        except Exception:
            raise CredentialStoreError(KEYRING_FAILURE_MESSAGE) from None

    def delete(self, origin: str) -> None:
        account_name = credential_account(origin)
        try:
            self._backend.delete_password(KEYRING_SERVICE, account_name)
            return
        except Exception:
            pass
        try:
            if self._backend.get_password(KEYRING_SERVICE, account_name) is None:
                return
        except Exception:
            raise CredentialStoreError(KEYRING_FAILURE_MESSAGE) from None
        raise CredentialStoreError(KEYRING_FAILURE_MESSAGE)


class SettingsStore:
    def __init__(self, qsettings: QSettings) -> None:
        self._qsettings = qsettings

    def load(self) -> ConnectorSettings:
        origin = self._load_origin()
        client_instance_uuid = self._load_client_instance_uuid()
        loaded = ConnectorSettings(
            origin=origin,
            enabled=self._load_security_bool('enabled'),
            automatic_upload=self._load_security_bool('automaticUpload'),
            client_instance_uuid=client_instance_uuid,
            identity=self._load_identity(),
            cache_limit_bytes=_bounded_cache_limit(self._value('cacheLimitBytes')),
            configuration_geometry=_coerce_qbytearray(self._value('configurationGeometry')),
            browser_geometry=_coerce_qbytearray(self._value('browserGeometry')),
        )
        if loaded.automatic_upload and loaded.identity is None:
            self._set_value('automaticUpload', False)
            self._sync()
            return ConnectorSettings(
                origin=loaded.origin,
                enabled=loaded.enabled,
                automatic_upload=False,
                client_instance_uuid=loaded.client_instance_uuid,
                identity=None,
                cache_limit_bytes=loaded.cache_limit_bytes,
                configuration_geometry=loaded.configuration_geometry,
                browser_geometry=loaded.browser_geometry,
            )
        return loaded

    def set_origin(self, origin: str) -> ConnectorSettings:
        canonical = canonical_origin(origin)
        current = self.load()
        if canonical == current.origin:
            return current
        self._set_value('origin', canonical)
        self._set_value('enabled', False)
        self._set_value('automaticUpload', False)
        self._clear_identity()
        self._sync()
        return ConnectorSettings(
            origin=canonical,
            enabled=False,
            automatic_upload=False,
            client_instance_uuid=current.client_instance_uuid,
            identity=None,
            cache_limit_bytes=current.cache_limit_bytes,
            configuration_geometry=current.configuration_geometry,
            browser_geometry=current.browser_geometry,
        )

    def save_connection(self, origin: str, identity: ServerIdentity) -> ConnectorSettings:
        current = self.set_origin(origin)
        self._set_identity(identity)
        self._sync()
        return ConnectorSettings(
            origin=current.origin,
            enabled=current.enabled,
            automatic_upload=current.automatic_upload,
            client_instance_uuid=current.client_instance_uuid,
            identity=identity,
            cache_limit_bytes=current.cache_limit_bytes,
            configuration_geometry=current.configuration_geometry,
            browser_geometry=current.browser_geometry,
        )

    def save_options(self, enabled: bool, automatic_upload: bool, cache_limit_bytes: int) -> ConnectorSettings:
        current = self.load()
        if automatic_upload and current.identity is None:
            raise SettingsError('Automatic upload requires a confirmed identity for the current origin.')
        bounded_cache_limit_bytes = _bounded_cache_limit(cache_limit_bytes)
        self._set_value('enabled', enabled)
        self._set_value('automaticUpload', automatic_upload)
        self._set_value('cacheLimitBytes', bounded_cache_limit_bytes)
        self._sync()
        return ConnectorSettings(
            origin=current.origin,
            enabled=enabled,
            automatic_upload=automatic_upload,
            client_instance_uuid=current.client_instance_uuid,
            identity=current.identity,
            cache_limit_bytes=bounded_cache_limit_bytes,
            configuration_geometry=current.configuration_geometry,
            browser_geometry=current.browser_geometry,
        )

    def save_geometry(self, configuration: QByteArray | None, browser: QByteArray | None) -> None:
        if configuration is None:
            self._remove('configurationGeometry')
        else:
            self._set_value('configurationGeometry', configuration)
        if browser is None:
            self._remove('browserGeometry')
        else:
            self._set_value('browserGeometry', browser)
        self._sync()

    def _load_origin(self) -> str:
        raw_origin = self._value('origin')
        if raw_origin is None:
            return DEFAULT_ORIGIN
        if not isinstance(raw_origin, str):
            return self._repair_origin(DEFAULT_ORIGIN)
        try:
            canonical = canonical_origin(raw_origin)
        except SettingsError:
            return self._repair_origin(DEFAULT_ORIGIN)
        if canonical != raw_origin:
            self._set_value('origin', canonical)
            self._sync()
        return canonical

    def _load_client_instance_uuid(self) -> UUID:
        raw_value = self._value('clientInstanceUUID')
        try:
            if isinstance(raw_value, UUID):
                client_instance_uuid = raw_value
            else:
                client_instance_uuid = UUID(_coerce_text(raw_value, default=''))
        except (TypeError, ValueError):
            client_instance_uuid = uuid4()
            self._set_value('clientInstanceUUID', str(client_instance_uuid))
            self._sync()
        return client_instance_uuid

    def _load_security_bool(self, key: Literal['enabled', 'automaticUpload']) -> bool:
        value, valid = _coerce_bool(self._value(key))
        if valid:
            return value
        self._set_value(key, False)
        self._sync()
        return False

    def _load_identity(self) -> ServerIdentity | None:
        values = {key: self._value(key) for key in _IDENTITY_KEYS}
        if all(value is None for value in values.values()):
            return None
        try:
            user_id = UUID(_coerce_text(values['identityUserID'], default=''))
            user_email = _required_text(values['identityUserEmail'])
            user_nickname = _required_text(values['identityUserNickname'])
            organization_id = UUID(_coerce_text(values['identityOrganizationID'], default=''))
            organization_name = _required_text(values['identityOrganizationName'])
            organization_slug = _required_text(values['identityOrganizationSlug'])
            role = _required_text(values['identityRole'])
        except (SettingsError, TypeError, ValueError):
            self._clear_identity()
            self._sync()
            return None
        if role not in _IDENTITY_ROLE_VALUES:
            self._clear_identity()
            self._sync()
            return None
        return ServerIdentity(
            user=IdentityUser(id=user_id, email=user_email, nickname=user_nickname),
            organization=IdentityOrganization(id=organization_id, name=organization_name, slug=organization_slug),
            role=cast(Literal['admin', 'member'], role),
        )

    def _set_identity(self, identity: ServerIdentity) -> None:
        self._set_value('identityUserID', str(identity.user.id))
        self._set_value('identityUserEmail', identity.user.email)
        self._set_value('identityUserNickname', identity.user.nickname)
        self._set_value('identityOrganizationID', str(identity.organization.id))
        self._set_value('identityOrganizationName', identity.organization.name)
        self._set_value('identityOrganizationSlug', identity.organization.slug)
        self._set_value('identityRole', identity.role)

    def _repair_origin(self, origin: str) -> str:
        self._set_value('origin', origin)
        self._set_value('enabled', False)
        self._set_value('automaticUpload', False)
        self._clear_identity()
        self._sync()
        return origin

    def _clear_identity(self) -> None:
        for key in _IDENTITY_KEYS:
            self._remove(key)

    def _value(self, key: str) -> object:
        return self._qsettings.value(_full_key(key))

    def _set_value(self, key: str, value: object) -> None:
        self._qsettings.setValue(_full_key(key), value)

    def _remove(self, key: str) -> None:
        self._qsettings.remove(_full_key(key))

    def _sync(self) -> None:
        self._qsettings.sync()


def namespace_for(origin: str, organization_id: UUID) -> Namespace:
    canonical = canonical_origin(origin)
    digest = hashlib.sha256(f'{canonical}\n{organization_id}'.encode()).hexdigest()
    return Namespace(origin=canonical, organization_id=organization_id, key=f'namespace-sha256:{digest}')


def credential_account(origin: str) -> str:
    canonical = canonical_origin(origin)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return f'origin-sha256:{digest}'


def _bounded_cache_limit(value: object) -> int:
    cache_limit = _coerce_int(value, default=DEFAULT_CACHE_LIMIT_BYTES)
    if cache_limit < MIN_CACHE_LIMIT_BYTES:
        return MIN_CACHE_LIMIT_BYTES
    if cache_limit > MAX_CACHE_LIMIT_BYTES:
        return MAX_CACHE_LIMIT_BYTES
    return cache_limit


def _coerce_bool(value: object) -> tuple[bool, bool]:
    if value is None:
        return False, True
    if isinstance(value, bool):
        return value, True
    if isinstance(value, int):
        if value in {0, 1}:
            return bool(value), True
        return False, False
    if isinstance(value, str) and value in _VALID_BOOLEAN_STRINGS:
        return _VALID_BOOLEAN_STRINGS[value], True
    return False, False


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return default
    return default


def _coerce_qbytearray(value: object) -> QByteArray | None:
    if isinstance(value, QByteArray):
        return QByteArray(value)
    if isinstance(value, bytes):
        return QByteArray(value)
    return None


def _coerce_text(value: object, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return default


def _required_text(value: object) -> str:
    text = _coerce_text(value, default='')
    if text == '':
        raise SettingsError('Missing stored identity value.')
    return text


def _full_key(key: str) -> str:
    if key not in _ALLOWED_GROUP_KEYS:
        raise ValueError(f'unexpected settings key: {key}')
    return f'{_GROUP_NAME}/{key}'


__all__ = [
    'ConnectorSettings',
    'CredentialStore',
    'CredentialStoreError',
    'DEFAULT_CACHE_LIMIT_BYTES',
    'DEFAULT_ORIGIN',
    'KEYRING_FAILURE_MESSAGE',
    'KEYRING_SERVICE',
    'MAX_CACHE_LIMIT_BYTES',
    'MIN_CACHE_LIMIT_BYTES',
    'SettingsError',
    'SettingsStore',
    'SystemCredentialStore',
    'canonical_origin',
    'credential_account',
    'namespace_for',
]
