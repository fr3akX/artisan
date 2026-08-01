#
# ABOUT
# Artisan Roast Server canonical origin policy
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

from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Final
from urllib.parse import urlsplit

import idna

_DEFAULT_HTTPS_PORT: Final[int] = 443
_DEFAULT_HTTP_PORT: Final[int] = 80
_ALLOWED_HTTP_HOSTS: Final[frozenset[str]] = frozenset({'localhost', '127.0.0.1', '[::1]'})


class SettingsError(ValueError):
    pass


def canonical_origin(value: str) -> str:
    raw_value = value.strip()
    if raw_value == '' or _has_disallowed_inner_code_points(raw_value) or '\\' in raw_value:
        raise SettingsError('Enter a valid HTTPS origin.')
    try:
        parts = urlsplit(raw_value)
    except (ValueError, UnicodeError):
        raise SettingsError('Enter a valid HTTPS origin.') from None
    if parts.scheme not in {'https', 'http'}:
        raise SettingsError('Enter a valid HTTPS origin.')
    if parts.netloc == '' or parts.query != '' or parts.fragment != '' or parts.path not in {'', '/'}:
        raise SettingsError('Enter a valid HTTPS origin.')
    hostname, port, bracketed = _split_authority(parts.netloc)
    normalized_host = _normalize_host(hostname, bracketed=bracketed)
    if parts.scheme == 'http' and normalized_host not in _ALLOWED_HTTP_HOSTS:
        raise SettingsError('Enter a valid HTTPS origin.')
    if port is None or (
        (parts.scheme == 'https' and port == _DEFAULT_HTTPS_PORT)
        or (parts.scheme == 'http' and port == _DEFAULT_HTTP_PORT)
    ):
        port_suffix = ''
    else:
        port_suffix = f':{port}'
    return f'{parts.scheme}://{normalized_host}{port_suffix}'


def _has_disallowed_inner_code_points(value: str) -> bool:
    for char in value:
        code_point = ord(char)
        if char.isspace() or code_point < 0x20 or 0x7F <= code_point <= 0x9F:
            return True
    return False


def _split_authority(netloc: str) -> tuple[str, int | None, bool]:
    if netloc == '' or '@' in netloc:
        raise SettingsError('Enter a valid HTTPS origin.')
    if netloc.startswith('['):
        closing_index = netloc.find(']')
        if closing_index <= 1:
            raise SettingsError('Enter a valid HTTPS origin.')
        hostname = netloc[1:closing_index]
        remainder = netloc[closing_index + 1 :]
        if remainder == '':
            return hostname, None, True
        if not remainder.startswith(':') or remainder == ':':
            raise SettingsError('Enter a valid HTTPS origin.')
        return hostname, _parse_port(remainder[1:]), True
    if '[' in netloc or ']' in netloc or netloc.count(':') > 1:
        raise SettingsError('Enter a valid HTTPS origin.')
    if ':' not in netloc:
        return netloc, None, False
    hostname, port_text = netloc.rsplit(':', 1)
    if hostname == '' or port_text == '':
        raise SettingsError('Enter a valid HTTPS origin.')
    return hostname, _parse_port(port_text), False


def _parse_port(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise SettingsError('Enter a valid HTTPS origin.')
    port = int(value)
    if not 0 <= port <= 65535:
        raise SettingsError('Enter a valid HTTPS origin.')
    return port


def _looks_like_ipv4_candidate(hostname: str) -> bool:
    return '.' in hostname and all(char.isdigit() or char == '.' for char in hostname)


def _normalize_host(hostname: str, *, bracketed: bool) -> str:
    if hostname == '' or '%' in hostname or hostname.endswith('.'):
        raise SettingsError('Enter a valid HTTPS origin.')
    if bracketed:
        try:
            parsed_ip = ip_address(hostname)
        except ValueError as exc:
            raise SettingsError('Enter a valid HTTPS origin.') from exc
        if not isinstance(parsed_ip, IPv6Address) or hostname != parsed_ip.compressed:
            raise SettingsError('Enter a valid HTTPS origin.')
        return f'[{parsed_ip.compressed}]'
    if ':' in hostname:
        raise SettingsError('Enter a valid HTTPS origin.')
    try:
        parsed_ip = ip_address(hostname)
    except ValueError:
        if _looks_like_ipv4_candidate(hostname):
            raise SettingsError('Enter a valid HTTPS origin.') from None
        try:
            ascii_hostname = idna.encode(hostname, uts46=True, std3_rules=True).decode('ascii')
        except (idna.IDNAError, UnicodeError) as exc:
            raise SettingsError('Enter a valid HTTPS origin.') from exc
        if ascii_hostname == '' or ascii_hostname.endswith('.'):
            raise SettingsError('Enter a valid HTTPS origin.') from None
        return ascii_hostname.lower()
    if not isinstance(parsed_ip, IPv4Address) or hostname != str(parsed_ip):
        raise SettingsError('Enter a valid HTTPS origin.')
    return str(parsed_ip)


__all__ = ['SettingsError', 'canonical_origin']
