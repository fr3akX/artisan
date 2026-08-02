#
# ABOUT
# Artisan Roast Server private cross-platform filesystem primitives
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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
import importlib
import os
from pathlib import Path
import stat
import sys
import threading
from typing import Any, Final, NoReturn, Protocol, cast
from uuid import uuid4

_IS_WINDOWS: bool = os.name == 'nt'
_HAS_DIRECTORY_FDS: Final[bool] = os.name != 'nt' and os.open in os.supports_dir_fd
_QUARANTINE_PREFIX: Final[str] = '.artisan-quarantine-'
_RENAME_NOREPLACE: Final[int] = 1
_RENAME_EXCL_DARWIN: Final[int] = 0x4
_UNSUPPORTED_DIRECTORY_SYNC_ERRNOS: Final[frozenset[int]] = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, 'ENOTSUP', None),
        getattr(errno, 'EOPNOTSUPP', None),
    )
    if isinstance(value, int)
)

# ``ctypes.wintypes`` follows host C widths when imported off Windows. These
# fixed-width aliases keep portable native seams byte-for-byte compatible with
# the Win32 SDK without changing their ABI on Windows.
_WindowsByte = ctypes.c_uint8
_WindowsWord = ctypes.c_uint16
_WindowsDword = ctypes.c_uint32


class _WindowsAclSizeInformation(ctypes.Structure):
    _fields_ = [
        ('AceCount', _WindowsDword),
        ('AclBytesInUse', _WindowsDword),
        ('AclBytesFree', _WindowsDword),
    ]


class _WindowsAceHeader(ctypes.Structure):
    _fields_ = [
        ('AceType', _WindowsByte),
        ('AceFlags', _WindowsByte),
        ('AceSize', _WindowsWord),
    ]


class _WindowsAccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ('Header', _WindowsAceHeader),
        ('Mask', _WindowsDword),
        ('SidStart', _WindowsDword),
    ]


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [('DeleteFile', wintypes.BOOLEAN)]



class _WindowsNativeApi(Protocol):
    def open_readonly(self, path: Path, *, directory: bool = False) -> int: ...

    def canonical_path(self, descriptor: int) -> Path: ...

    def open_lock(self, path: Path) -> int: ...

    def set_private_permissions(self, path: Path, mode: int) -> None: ...

    def verify_private_permissions(self, path: Path, mode: int) -> None: ...

    def flush(self, descriptor: int, *, directory: bool) -> None: ...

    def flush_directory(self, path: Path) -> None: ...

    def publish(self, source: Path, destination: Path) -> None: ...

    def replace(self, source: Path, destination: Path) -> None: ...

    def move_no_replace(self, source: Path, destination: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...

    def unlink_if_identity(
        self, path: Path, expected_identity: tuple[int, int]
    ) -> bool: ...


class _WindowsNativeLayer:
    """Small Win32 handle boundary, loaded only on Windows.

    Directory components are opened with FILE_FLAG_OPEN_REPARSE_POINT and held
    without FILE_SHARE_DELETE while the next component is opened. All reparse
    points are rejected. DACLs contain one full-control ACE for the current
    process user and are protected from inheritance.
    """

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _SYNCHRONIZE = 0x00100000
    _READ_CONTROL = 0x00020000
    _WRITE_DAC = 0x00040000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x80
    _FILE_WRITE_ATTRIBUTES = 0x100
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_READONLY = 0x1
    _ACCESS_ALLOWED_ACE_TYPE = 0x0
    _ACCESS_DENIED_ACE_TYPE = 0x1
    _OBJECT_INHERIT_ACE = 0x1
    _CONTAINER_INHERIT_ACE = 0x2
    _INHERITED_ACE = 0x10
    _FILE_ALL_ACCESS = 0x001F01FF
    _ACL_SIZE_INFORMATION_CLASS = 2
    _DACL_SECURITY_INFORMATION = 0x4
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_FILE_OBJECT = 1
    _SDDL_REVISION_1 = 1
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _FILE_BASIC_INFO_CLASS = 0
    _FILE_DISPOSITION_INFO_CLASS = 4
    _MOVEFILE_REPLACE_EXISTING = 0x1
    _MOVEFILE_WRITE_THROUGH = 0x8
    _FILE_NAME_NORMALIZED = 0x0
    _VOLUME_NAME_GUID = 0x1

    def __init__(self) -> None:
        self._ctypes: Any = ctypes
        self._wintypes: Any = wintypes
        win_dll = cast(Any, ctypes.__dict__['WinDLL'])
        self._kernel32 = win_dll('kernel32', use_last_error=True)
        self._advapi32 = win_dll('advapi32', use_last_error=True)
        self._invalid_handle = ctypes.c_void_p(-1).value

        self._set_prototype(
            self._kernel32.CreateFileW,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ],
            wintypes.HANDLE,
        )
        self._set_prototype(
            self._kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL
        )
        self._set_prototype(
            self._kernel32.GetFileInformationByHandle,
            [wintypes.HANDLE, wintypes.LPVOID],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._kernel32.GetFinalPathNameByHandleW,
            [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD],
            wintypes.DWORD,
        )
        self._set_prototype(self._kernel32.GetCurrentProcess, [], wintypes.HANDLE)
        self._set_prototype(
            self._kernel32.LocalFree, [wintypes.HLOCAL], wintypes.HLOCAL
        )
        self._set_prototype(
            self._kernel32.GetFileInformationByHandleEx,
            [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._kernel32.SetFileInformationByHandle,
            [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._kernel32.FlushFileBuffers, [wintypes.HANDLE], wintypes.BOOL
        )
        self._set_prototype(
            self._kernel32.MoveFileExW,
            [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.OpenProcessToken,
            [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.GetTokenInformation,
            [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.ConvertSidToStringSidW,
            [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.GetSecurityDescriptorDacl,
            [
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.BOOL),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.BOOL),
            ],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.ConvertStringSidToSidW,
            [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.GetAclInformation,
            [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.GetAce,
            [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.EqualSid,
            [wintypes.LPVOID, wintypes.LPVOID],
            wintypes.BOOL,
        )
        self._set_prototype(
            self._advapi32.GetLengthSid, [wintypes.LPVOID], wintypes.DWORD
        )
        self._set_prototype(
            self._advapi32.IsValidSid, [wintypes.LPVOID], wintypes.BOOL
        )
        security_info_arguments: list[Any] = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        self._set_prototype(
            self._advapi32.SetSecurityInfo,
            security_info_arguments,
            wintypes.DWORD,
        )
        self._set_prototype(
            self._advapi32.GetSecurityInfo,
            [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
            ],
            wintypes.DWORD,
        )
        self._set_prototype(
            self._advapi32.GetSecurityDescriptorControl,
            [
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.WORD),
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        )

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ('dwFileAttributes', wintypes.DWORD),
                ('ftCreationTimeLow', wintypes.DWORD),
                ('ftCreationTimeHigh', wintypes.DWORD),
                ('ftLastAccessTimeLow', wintypes.DWORD),
                ('ftLastAccessTimeHigh', wintypes.DWORD),
                ('ftLastWriteTimeLow', wintypes.DWORD),
                ('ftLastWriteTimeHigh', wintypes.DWORD),
                ('dwVolumeSerialNumber', wintypes.DWORD),
                ('nFileSizeHigh', wintypes.DWORD),
                ('nFileSizeLow', wintypes.DWORD),
                ('nNumberOfLinks', wintypes.DWORD),
                ('nFileIndexHigh', wintypes.DWORD),
                ('nFileIndexLow', wintypes.DWORD),
            ]

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ('CreationTime', ctypes.c_longlong),
                ('LastAccessTime', ctypes.c_longlong),
                ('LastWriteTime', ctypes.c_longlong),
                ('ChangeTime', ctypes.c_longlong),
                ('FileAttributes', wintypes.DWORD),
            ]

        self._by_handle_information = ByHandleFileInformation
        self._file_basic_info = FileBasicInfo
        self._file_disposition_info = _WindowsFileDispositionInfo

    @staticmethod
    def _set_prototype(function: Any, arguments: list[Any], result: Any) -> None:
        function.argtypes = arguments
        function.restype = result

    def _error(self) -> OSError:
        return OSError(self._ctypes.get_last_error(), 'Windows filesystem operation failed')

    def _close(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise self._error()

    def _attributes(self, handle: int) -> int:
        information = self._by_handle_information()
        if not self._kernel32.GetFileInformationByHandle(handle, self._ctypes.byref(information)):
            raise self._error()
        return cast(int, information.dwFileAttributes)

    def _open_one(self, path: Path, access: int, disposition: int) -> int:
        handle = self._kernel32.CreateFileW(
            os.fspath(path),
            access,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            None,
            disposition,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == self._invalid_handle:
            raise self._error()
        try:
            if self._attributes(handle) & self._FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(errno.ELOOP, 'reparse point rejected')
        except BaseException:
            self._close(handle)
            raise
        return cast(int, handle)

    def _open_chain(
        self,
        path: Path,
        *,
        final_access: int,
        final_disposition: int = _OPEN_EXISTING,
    ) -> list[int]:
        absolute = Path(os.path.abspath(os.fspath(path)))
        if not absolute.anchor:
            raise OSError(errno.EINVAL, 'absolute path required')
        current = Path(absolute.anchor)
        handles: list[int] = []
        try:
            handles.append(self._open_one(current, self._GENERIC_READ, self._OPEN_EXISTING))
            for index, component in enumerate(absolute.parts[1:]):
                if component in {'', '.', '..'}:
                    raise OSError(errno.EINVAL, 'invalid path component')
                current /= component
                final = index == len(absolute.parts[1:]) - 1
                handles.append(
                    self._open_one(
                        current,
                        final_access if final else self._GENERIC_READ,
                        final_disposition if final else self._OPEN_EXISTING,
                    )
                )
            return handles
        except BaseException:
            for handle in reversed(handles):
                try:
                    self._close(handle)
                except OSError:
                    pass
            raise

    def open_readonly(self, path: Path, *, directory: bool = False) -> int:
        msvcrt = cast(Any, importlib.import_module('msvcrt'))

        handles = self._open_chain(path, final_access=self._GENERIC_READ)
        final = handles.pop()
        try:
            attributes = self._attributes(final)
            is_directory = bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
            if is_directory != directory:
                raise OSError(errno.EISDIR if is_directory else errno.ENOTDIR, 'wrong file type')
            descriptor = cast(int, msvcrt.open_osfhandle(final, os.O_RDONLY))
            final = 0
            return descriptor
        finally:
            if final:
                self._close(final)
            for handle in reversed(handles):
                self._close(handle)

    def canonical_path(self, descriptor: int) -> Path:
        msvcrt = cast(Any, importlib.import_module('msvcrt'))
        handle = msvcrt.get_osfhandle(descriptor)
        if self._attributes(handle) & self._FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, 'reparse point rejected')
        flags = self._FILE_NAME_NORMALIZED | self._VOLUME_NAME_GUID
        required = self._kernel32.GetFinalPathNameByHandleW(
            handle, None, 0, flags)
        if required == 0:
            raise self._error()
        buffer = self._ctypes.create_unicode_buffer(required + 1)
        written = self._kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), flags)
        if written == 0 or written >= len(buffer):
            raise self._error()
        value = cast(str, buffer.value)
        if not value.startswith('\\\\?\\Volume{'):
            raise OSError(errno.EINVAL, 'Windows canonical volume path is invalid')
        return Path(value)

    def open_lock(self, path: Path) -> int:
        msvcrt = cast(Any, importlib.import_module('msvcrt'))

        handles = self._open_chain(
            path,
            final_access=(
                self._GENERIC_READ | self._GENERIC_WRITE | self._FILE_WRITE_ATTRIBUTES
            ),
            final_disposition=self._OPEN_ALWAYS,
        )
        final = handles.pop()
        try:
            descriptor = cast(int, msvcrt.open_osfhandle(final, os.O_RDWR))
            final = 0
            return descriptor
        finally:
            if final:
                self._close(final)
            for handle in reversed(handles):
                self._close(handle)

    def _current_user_sid_string(self) -> str:
        ctypes = self._ctypes
        token = self._wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(), self._TOKEN_QUERY, ctypes.byref(token)
        ):
            raise self._error()
        try:
            required = self._wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token, self._TOKEN_USER, None, 0, ctypes.byref(required)
            )
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                buffer,
                required,
                ctypes.byref(required),
            ):
                raise self._error()
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_text = self._wintypes.LPWSTR()
            if not self._advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
                raise self._error()
            try:
                return cast(str, sid_text.value)
            finally:
                self._kernel32.LocalFree(sid_text)
        finally:
            self._close(cast(int, token.value))

    def _security_descriptor(self, sid: str) -> tuple[Any, Any]:
        ctypes = self._ctypes
        descriptor = ctypes.c_void_p()
        size = self._wintypes.DWORD()
        sddl = f'D:P(A;OICI;FA;;;{sid})'
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            self._SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(size),
        ):
            raise self._error()
        dacl_present = self._wintypes.BOOL()
        dacl_defaulted = self._wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not self._advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value or not dacl.value:
            self._kernel32.LocalFree(descriptor)
            raise self._error()
        return descriptor, dacl

    def _set_readonly(self, handle: int, readonly: bool) -> None:
        information = self._file_basic_info()
        if not self._kernel32.GetFileInformationByHandleEx(
            handle,
            self._FILE_BASIC_INFO_CLASS,
            self._ctypes.byref(information),
            self._ctypes.sizeof(information),
        ):
            raise self._error()
        if readonly:
            information.FileAttributes |= self._FILE_ATTRIBUTE_READONLY
        else:
            information.FileAttributes &= ~self._FILE_ATTRIBUTE_READONLY
        if not self._kernel32.SetFileInformationByHandle(
            handle,
            self._FILE_BASIC_INFO_CLASS,
            self._ctypes.byref(information),
            self._ctypes.sizeof(information),
        ):
            raise self._error()

    def set_private_permissions(self, path: Path, mode: int) -> None:
        handles = self._open_chain(
            path,
            final_access=(
                self._GENERIC_READ
                | self._READ_CONTROL
                | self._WRITE_DAC
                | self._FILE_READ_ATTRIBUTES
                | self._FILE_WRITE_ATTRIBUTES
            ),
        )
        final = handles[-1]
        descriptor: Any = None
        try:
            sid = self._current_user_sid_string()
            descriptor, dacl = self._security_descriptor(sid)
            result = self._advapi32.SetSecurityInfo(
                final,
                self._SE_FILE_OBJECT,
                self._DACL_SECURITY_INFORMATION | self._PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if result != 0:
                raise OSError(result, 'Windows ACL operation failed')
            self._set_readonly(final, mode == 0o400)
        finally:
            if descriptor is not None:
                self._kernel32.LocalFree(descriptor)
            for handle in reversed(handles):
                self._close(handle)
        self.verify_private_permissions(path, mode)

    def _verify_private_dacl(
        self, dacl: Any, expected_sid: Any, *, protected: bool
    ) -> None:
        if not protected:
            raise OSError(errno.EACCES, 'Windows ACL is not protected')
        information = _WindowsAclSizeInformation()
        if not self._advapi32.GetAclInformation(
            dacl,
            self._ctypes.byref(information),
            self._ctypes.sizeof(information),
            self._ACL_SIZE_INFORMATION_CLASS,
        ):
            raise self._error()
        if information.AceCount != 1:
            raise OSError(errno.EACCES, 'Windows ACL has unexpected entries')
        ace_pointer = self._ctypes.c_void_p()
        if not self._advapi32.GetAce(dacl, 0, self._ctypes.byref(ace_pointer)):
            raise self._error()
        if not ace_pointer.value:
            raise OSError(errno.EACCES, 'Windows ACL entry is invalid')
        header = self._ctypes.cast(
            ace_pointer, self._ctypes.POINTER(_WindowsAceHeader)
        ).contents
        intended_flags = self._OBJECT_INHERIT_ACE | self._CONTAINER_INHERIT_ACE
        sid_offset = _WindowsAccessAllowedAce.SidStart.offset
        if (
            header.AceType != self._ACCESS_ALLOWED_ACE_TYPE
            or header.AceFlags != intended_flags
            or header.AceSize < sid_offset + 8
            or header.AceSize % self._ctypes.sizeof(_WindowsDword) != 0
        ):
            raise OSError(errno.EACCES, 'Windows ACL entry is not an exact allow ACE')
        allow = self._ctypes.cast(
            ace_pointer, self._ctypes.POINTER(_WindowsAccessAllowedAce)
        ).contents
        ace_sid = self._ctypes.c_void_p(ace_pointer.value + sid_offset)
        if not self._advapi32.IsValidSid(ace_sid):
            raise OSError(errno.EACCES, 'Windows ACL entry contains an invalid SID')
        sid_length = self._advapi32.GetLengthSid(ace_sid)
        if header.AceSize != sid_offset + sid_length:
            raise OSError(errno.EACCES, 'Windows ACL entry has trailing or missing bytes')
        if allow.Mask != self._FILE_ALL_ACCESS or not self._advapi32.EqualSid(
            ace_sid, expected_sid
        ):
            raise OSError(errno.EACCES, 'Windows ACL entry has the wrong SID or rights')

    def verify_private_permissions(self, path: Path, mode: int) -> None:
        handles = self._open_chain(
            path,
            final_access=self._GENERIC_READ | self._READ_CONTROL | self._FILE_READ_ATTRIBUTES,
        )
        final = handles[-1]
        security_descriptor = self._ctypes.c_void_p()
        expected_sid = self._ctypes.c_void_p()
        try:
            dacl = self._ctypes.c_void_p()
            result = self._advapi32.GetSecurityInfo(
                final,
                self._SE_FILE_OBJECT,
                self._DACL_SECURITY_INFORMATION,
                None,
                None,
                self._ctypes.byref(dacl),
                None,
                self._ctypes.byref(security_descriptor),
            )
            if result != 0 or not dacl.value:
                raise OSError(result, 'Windows ACL verification failed')
            if not self._advapi32.ConvertStringSidToSidW(
                self._current_user_sid_string(), self._ctypes.byref(expected_sid)
            ):
                raise self._error()
            control = self._wintypes.WORD()
            revision = self._wintypes.DWORD()
            if not self._advapi32.GetSecurityDescriptorControl(
                security_descriptor,
                self._ctypes.byref(control),
                self._ctypes.byref(revision),
            ):
                raise self._error()
            self._verify_private_dacl(
                dacl, expected_sid, protected=bool(control.value & 0x1000)
            )
            readonly = bool(self._attributes(final) & self._FILE_ATTRIBUTE_READONLY)
            if readonly != (mode == 0o400):
                raise OSError(errno.EACCES, 'Windows readonly state is invalid')
        finally:
            if expected_sid.value:
                self._kernel32.LocalFree(expected_sid)
            if security_descriptor.value:
                self._kernel32.LocalFree(security_descriptor)
            for handle in reversed(handles):
                self._close(handle)

    def flush(self, descriptor: int, *, directory: bool) -> None:
        del directory
        msvcrt = cast(Any, importlib.import_module('msvcrt'))

        handle = msvcrt.get_osfhandle(descriptor)
        if not self._kernel32.FlushFileBuffers(handle):
            raise OSError(self._ctypes.get_last_error(), 'Windows flush failed')

    def flush_directory(self, path: Path) -> None:
        handles = self._open_chain(
            path,
            final_access=self._GENERIC_WRITE | self._SYNCHRONIZE,
        )
        try:
            final = handles[-1]
            if not self._attributes(final) & stat.FILE_ATTRIBUTE_DIRECTORY:
                raise OSError(errno.ENOTDIR, 'Windows directory flush target is not a directory')
            if not self._kernel32.FlushFileBuffers(final):
                raise OSError(self._ctypes.get_last_error(), 'Windows directory flush failed')
        finally:
            for handle in reversed(handles):
                self._close(handle)

    def publish(self, source: Path, destination: Path) -> None:
        self.move_no_replace(source, destination)

    def move_no_replace(self, source: Path, destination: Path) -> None:
        if not self._kernel32.MoveFileExW(
            os.fspath(source), os.fspath(destination), self._MOVEFILE_WRITE_THROUGH
        ):
            error = self._ctypes.get_last_error()
            if error in {80, 183}:
                raise FileExistsError(errno.EEXIST, 'destination already exists')
            raise OSError(error, 'Windows write-through move failed')

    def replace(self, source: Path, destination: Path) -> None:
        if not self._kernel32.MoveFileExW(
            os.fspath(source),
            os.fspath(destination),
            self._MOVEFILE_REPLACE_EXISTING | self._MOVEFILE_WRITE_THROUGH,
        ):
            raise OSError(
                self._ctypes.get_last_error(), 'Windows write-through replacement failed'
            )

    def unlink(self, path: Path) -> None:
        handles = self._open_chain(
            path,
            final_access=(
                self._DELETE | self._FILE_READ_ATTRIBUTES | self._FILE_WRITE_ATTRIBUTES
            ),
        )
        try:
            self._unlink_handle(handles[-1])
        finally:
            for handle in reversed(handles):
                self._close(handle)

    def unlink_if_identity(
        self, path: Path, expected_identity: tuple[int, int]
    ) -> bool:
        msvcrt = cast(Any, importlib.import_module('msvcrt'))
        handles = self._open_chain(
            path,
            final_access=(
                self._DELETE
                | self._GENERIC_READ
                | self._FILE_READ_ATTRIBUTES
                | self._FILE_WRITE_ATTRIBUTES
            ),
        )
        final = handles.pop()
        descriptor: int | None = None
        try:
            descriptor = cast(int, msvcrt.open_osfhandle(final, os.O_RDONLY))
            final = 0
            file_stat = os.fstat(descriptor)
            if (file_stat.st_dev, file_stat.st_ino) != expected_identity:
                return False
            self._unlink_handle(cast(int, msvcrt.get_osfhandle(descriptor)))
            return True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif final:
                self._close(final)
            for handle in reversed(handles):
                self._close(handle)

    def _unlink_handle(self, handle: int) -> None:
        self._set_readonly(handle, False)
        disposition = self._file_disposition_info(True)
        if not self._kernel32.SetFileInformationByHandle(
            handle,
            self._FILE_DISPOSITION_INFO_CLASS,
            self._ctypes.byref(disposition),
            self._ctypes.sizeof(disposition),
        ):
            raise self._error()


def _load_windows_native() -> _WindowsNativeApi | None:
    if not _IS_WINDOWS:
        return None
    try:
        return _WindowsNativeLayer()
    except Exception as exc:
        raise RuntimeError('Windows secure filesystem APIs are unavailable') from exc


_WINDOWS_NATIVE: _WindowsNativeApi | None = _load_windows_native()



class FilesystemError(RuntimeError):
    """Fixed internal error raised by the private filesystem boundary."""


def _fail(message: str) -> NoReturn:
    raise FilesystemError(message)


def require_windows_native(
    *, native: _WindowsNativeApi | None = None,
) -> _WindowsNativeApi:
    result = _WINDOWS_NATIVE if native is None else native
    if result is None:
        _fail('secure filesystem APIs are unavailable')
    return result


def set_private_permissions(
    path: Path,
    mode: int,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        try:
            require_windows_native(native=native).set_private_permissions(path, mode)
        except (FilesystemError, OSError):
            _fail('private permissions could not be applied')
        return
    try:
        os.chmod(path, mode, follow_symlinks=False)
        actual_mode = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        _fail('private permissions could not be applied')
    if actual_mode != mode:
        _fail('private permissions could not be applied')


def verify_private_permissions(
    path: Path,
    mode: int,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        try:
            require_windows_native(native=native).verify_private_permissions(path, mode)
        except (FilesystemError, OSError):
            _fail('private permissions are invalid')
        return
    try:
        actual_mode = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        _fail('private permissions are invalid')
    if actual_mode != mode:
        _fail('private permissions are invalid')


def fsync_descriptor(
    descriptor: int,
    *,
    directory: bool = False,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        require_windows_native(native=native).flush(descriptor, directory=directory)
        return
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if directory and exc.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
            return
        raise


def fsync_directory(
    path: Path,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        require_windows_native(native=native).flush_directory(path)
        return
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        fsync_descriptor(descriptor, directory=True, is_windows=False)
    finally:
        os.close(descriptor)


def secure_unlink(
    path: Path,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        require_windows_native(native=native).unlink(path)
    else:
        path.unlink()


def path_is_junction(path: Path) -> bool:
    method = getattr(path, 'is_junction', None)
    if not callable(method):
        return False
    try:
        return bool(method())
    except OSError:
        return True


def directory_entry_is_reparse(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    method = getattr(entry, 'is_junction', None)
    if callable(method):
        try:
            return bool(method())
        except OSError:
            return True
    return path_is_junction(Path(entry.path))


def prepare_private_root(root: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(root)))
    creation_observed = not os.path.lexists(absolute)
    if creation_observed:
        try:
            absolute.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
    try:
        root_stat = os.lstat(absolute)
    except OSError:
        _fail('private root is unavailable')
    if stat.S_ISLNK(root_stat.st_mode) or path_is_junction(absolute):
        _fail('private root must not be a link or reparse point')
    if not stat.S_ISDIR(root_stat.st_mode):
        _fail('private root must be a directory')
    set_private_permissions(absolute, 0o700)
    if creation_observed:
        try:
            fsync_directory(absolute.parent)
        except OSError:
            _fail('private root could not be synchronized')
    return absolute


def _relative_parts(root: Path, path: Path) -> tuple[str, ...]:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        _fail('generated path escapes private root')
    for component in parts:
        if component in {'', '.', '..'} or '/' in component or '\\' in component:
            _fail('generated path is invalid')
    return parts


def require_directory_path(
    path: Path,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        try:
            descriptor = require_windows_native(native=native).open_readonly(
                path, directory=True
            )
        except (FilesystemError, OSError):
            _fail('generated directory contains a reparse point')
        os.close(descriptor)
        return
    try:
        path_stat = os.lstat(path)
    except OSError:
        _fail('generated directory is unavailable')
    if stat.S_ISLNK(path_stat.st_mode):
        _fail('generated directory must not be a symlink')
    if not stat.S_ISDIR(path_stat.st_mode):
        _fail('generated path must be a directory')


def open_generated_directory(
    root: Path,
    directory: Path,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> int:
    relative_parts = _relative_parts(root, directory)
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        try:
            return require_windows_native(native=native).open_readonly(
                directory, directory=True
            )
        except (FilesystemError, OSError):
            _fail('generated directory contains a reparse point')
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if not _HAS_DIRECTORY_FDS:
        require_directory_path(directory, is_windows=False)
        try:
            return os.open(directory, flags)
        except OSError:
            _fail('generated directory is unavailable')
    try:
        directory_fd = os.open(root, flags)
    except OSError:
        _fail('private root is unavailable')
    try:
        for component in relative_parts:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except OSError:
        os.close(directory_fd)
        _fail('generated directory contains a symlink or is unavailable')
    except BaseException:
        os.close(directory_fd)
        raise


def ensure_generated_directory(root: Path, directory: Path) -> None:
    parts = _relative_parts(root, directory)
    current = root
    for component in parts:
        parent = current
        current /= component
        created = False
        try:
            os.mkdir(current, 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError:
            _fail('generated directory could not be created')
        require_directory_path(current)
        set_private_permissions(current, 0o700)
        if created:
            try:
                fsync_directory(parent)
            except OSError:
                _fail('generated directory could not be synchronized')


def create_generated_file(root: Path, path: Path, mode: int = 0o600) -> int:
    _relative_parts(root, path)
    ensure_generated_directory(root, path.parent)
    directory_fd = open_generated_directory(root, path.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        if _HAS_DIRECTORY_FDS:
            descriptor = os.open(path.name, flags, mode, dir_fd=directory_fd)
        else:
            descriptor = os.open(path, flags, mode)
    except OSError:
        _fail('generated file could not be created')
    finally:
        os.close(directory_fd)
    try:
        set_private_permissions(path, mode)
    except BaseException:
        os.close(descriptor)
        try:
            secure_unlink(path)
        except OSError:
            pass
        raise
    return descriptor


def open_generated_file(
    root: Path,
    path: Path,
    *,
    writable: bool = False,
) -> int:
    _relative_parts(root, path)
    directory_fd = open_generated_directory(root, path.parent)
    try:
        if _IS_WINDOWS:
            if writable:
                _fail('writable generated opens are unsupported on Windows')
            descriptor = require_windows_native().open_readonly(path)
        else:
            flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, 'O_CLOEXEC', 0)
            flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            if _HAS_DIRECTORY_FDS:
                descriptor = os.open(path.name, flags, dir_fd=directory_fd)
            else:
                descriptor = os.open(path, flags)
    except (FilesystemError, OSError):
        _fail('generated file contains a link or is unavailable')
    finally:
        os.close(directory_fd)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            _fail('generated file must be regular')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def generated_entry_stat(root: Path, path: Path) -> os.stat_result:
    _relative_parts(root, path)
    directory_fd = open_generated_directory(root, path.parent)
    try:
        if _HAS_DIRECTORY_FDS:
            return os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        return os.lstat(path)
    except OSError:
        _fail('generated entry is unavailable')
    finally:
        os.close(directory_fd)


def open_generated_lock(root: Path, path: Path) -> int:
    _relative_parts(root, path)
    directory_fd = open_generated_directory(root, path.parent)
    try:
        if _IS_WINDOWS:
            if not os.path.lexists(path):
                _fail('generated lock is unavailable')
            descriptor = require_windows_native().open_lock(path)
        else:
            flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0)
            flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            if _HAS_DIRECTORY_FDS:
                descriptor = os.open(path.name, flags, dir_fd=directory_fd)
            else:
                descriptor = os.open(path, flags)
    except (FilesystemError, OSError):
        _fail('generated lock contains a link or is unavailable')
    finally:
        os.close(directory_fd)
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            _fail('generated lock must be regular')
        set_private_permissions(path, 0o600)
        verify_private_permissions(path, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def replace_generated(root: Path, source: Path, destination: Path) -> None:
    _relative_parts(root, source)
    _relative_parts(root, destination)
    source_directory_fd = open_generated_directory(root, source.parent)
    try:
        destination_directory_fd = open_generated_directory(root, destination.parent)
    except BaseException:
        os.close(source_directory_fd)
        raise
    try:
        source_stat = (
            os.stat(source.name, dir_fd=source_directory_fd, follow_symlinks=False)
            if _HAS_DIRECTORY_FDS
            else os.lstat(source)
        )
        if not stat.S_ISREG(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
            _fail('publication source is invalid')
        try:
            destination_stat = (
                os.stat(destination.name, dir_fd=destination_directory_fd, follow_symlinks=False)
                if _HAS_DIRECTORY_FDS
                else os.lstat(destination)
            )
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and (
            stat.S_ISLNK(destination_stat.st_mode)
            or not stat.S_ISREG(destination_stat.st_mode)
        ):
            _fail('publication destination is invalid')
        if _HAS_DIRECTORY_FDS:
            os.replace(
                source.name,
                destination.name,
                src_dir_fd=source_directory_fd,
                dst_dir_fd=destination_directory_fd,
            )
        elif _IS_WINDOWS:
            require_windows_native().replace(source, destination)
        else:
            os.replace(source, destination)
        fsync_directory(destination.parent)
        if source.parent != destination.parent:
            fsync_directory(source.parent)
    except FilesystemError:
        raise
    except OSError:
        _fail('atomic publication failed')
    finally:
        os.close(source_directory_fd)
        os.close(destination_directory_fd)


def generated_quarantine_identity(name: str) -> tuple[int, int] | None:
    if not name.startswith(_QUARANTINE_PREFIX):
        return None
    components = name.removeprefix(_QUARANTINE_PREFIX).split('-')
    if len(components) != 3:
        return None
    device_text, file_text, token = components
    if (
        not device_text
        or not file_text
        or len(token) != 32
        or any(character not in '0123456789abcdef' for character in token)
    ):
        return None
    try:
        identity = int(device_text, 16), int(file_text, 16)
    except ValueError:
        return None
    if device_text != f'{identity[0]:x}' or file_text != f'{identity[1]:x}':
        return None
    return identity


def is_generated_quarantine_name(name: str) -> bool:
    return generated_quarantine_identity(name) is not None


def _posix_move_no_replace(
    source_name: str, destination_name: str, directory_descriptor: int
) -> None:
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                directory_descriptor,
                source_bytes,
                directory_descriptor,
                destination_bytes,
                _RENAME_NOREPLACE,
            )
            == 0
        ):
            return
        error = ctypes.get_errno()
        if error not in {
            errno.EINVAL,
            errno.ENOSYS,
            getattr(errno, 'ENOTSUP', errno.EINVAL),
            getattr(errno, 'EOPNOTSUPP', errno.EINVAL),
        }:
            raise OSError(error, 'atomic no-replace move failed')
    if sys.platform == 'darwin':
        renameatx_np = getattr(libc, 'renameatx_np', None)
        if renameatx_np is not None:
            renameatx_np.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx_np.restype = ctypes.c_int
            if (
                renameatx_np(
                    directory_descriptor,
                    source_bytes,
                    directory_descriptor,
                    destination_bytes,
                    _RENAME_EXCL_DARWIN,
                )
                == 0
            ):
                return
            error = ctypes.get_errno()
            if error not in {errno.EINVAL, errno.ENOSYS}:
                raise OSError(error, 'atomic no-replace move failed')
    try:
        os.stat(destination_name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, 'destination already exists')
    os.rename(
        source_name,
        destination_name,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
    )


def _move_generated_no_replace(
    source: Path, destination: Path, directory_descriptor: int
) -> None:
    if source.parent != destination.parent:
        raise OSError(errno.EXDEV, 'quarantine move must stay in one directory')
    if _IS_WINDOWS:
        require_windows_native().move_no_replace(source, destination)
    elif _HAS_DIRECTORY_FDS:
        _posix_move_no_replace(source.name, destination.name, directory_descriptor)
    else:
        if os.path.lexists(destination):
            raise FileExistsError(errno.EEXIST, 'destination already exists')
        os.rename(source, destination)


def _unlink_quarantined_generated_file(
    root: Path,
    quarantine_path: Path,
    expected_identity: tuple[int, int],
    directory_descriptor: int,
) -> bool:
    _relative_parts(root, quarantine_path)
    if _IS_WINDOWS:
        return require_windows_native().unlink_if_identity(
            quarantine_path, expected_identity
        )
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor: int | None = None
    try:
        if _HAS_DIRECTORY_FDS:
            descriptor = os.open(
                quarantine_path.name, flags, dir_fd=directory_descriptor
            )
        else:
            descriptor = os.open(quarantine_path, flags)
        descriptor_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino) != expected_identity
        ):
            return False
        entry_stat = (
            os.stat(
                quarantine_path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _HAS_DIRECTORY_FDS
            else os.lstat(quarantine_path)
        )
        if (
            not stat.S_ISREG(entry_stat.st_mode)
            or stat.S_ISLNK(entry_stat.st_mode)
            or (entry_stat.st_dev, entry_stat.st_ino) != expected_identity
        ):
            return False
        if _HAS_DIRECTORY_FDS:
            os.unlink(quarantine_path.name, dir_fd=directory_descriptor)
        else:
            secure_unlink(quarantine_path)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)


def unlink_generated_file(
    root: Path,
    path: Path,
    expected_identity: tuple[int, int],
    *,
    missing_ok: bool = False,
) -> bool:
    """Remove one generated identity without unlinking through its original name.

    Callers serialize connector mutations with their process lock. The random
    quarantine name closes accidental connector races; mutation by an
    adversarial process running as the same user remains outside this boundary.
    """

    _relative_parts(root, path)
    if any(type(value) is not int or value < 0 for value in expected_identity):
        _fail('generated removal identity is invalid')
    directory_descriptor = open_generated_directory(root, path.parent)
    quarantine_path: Path | None = None
    moved = False
    try:
        for _attempt in range(32):
            candidate = path.parent / (
                f'{_QUARANTINE_PREFIX}{expected_identity[0]:x}-'
                f'{expected_identity[1]:x}-{uuid4().hex}'
            )
            try:
                _move_generated_no_replace(path, candidate, directory_descriptor)
            except FileExistsError:
                continue
            except FileNotFoundError:
                if missing_ok:
                    return False
                _fail('generated removal target is unavailable')
            quarantine_path = candidate
            moved = True
            break
        if quarantine_path is None:
            _fail('generated quarantine name is unavailable')
        if not _unlink_quarantined_generated_file(
            root, quarantine_path, expected_identity, directory_descriptor
        ):
            _fail('generated removal identity changed')
        moved = False
        fsync_directory(path.parent)
        return True
    except BaseException as error:
        if moved and quarantine_path is not None:
            try:
                _move_generated_no_replace(
                    quarantine_path, path, directory_descriptor
                )
                moved = False
                fsync_directory(path.parent)
            except (OSError, FilesystemError):
                pass
        if isinstance(error, FilesystemError):
            raise
        if isinstance(error, OSError):
            _fail('generated removal failed')
        raise
    finally:
        os.close(directory_descriptor)


def open_path_readonly(
    path: Path,
    *,
    is_windows: bool | None = None,
    native: _WindowsNativeApi | None = None,
) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        try:
            return require_windows_native(native=native).open_readonly(absolute)
        except (FilesystemError, OSError):
            _fail('source path contains a reparse point or is unavailable')
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    directory_flags = flags | getattr(os, 'O_DIRECTORY', 0)
    if os.open in os.supports_dir_fd:
        components = absolute.parts[1:]
        if not components:
            _fail('source path is invalid')
        directory_fd = os.open(os.sep, directory_flags)
        try:
            for component in components[:-1]:
                if component in {'', '.', '..'}:
                    _fail('source path is invalid')
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return os.open(components[-1], flags, dir_fd=directory_fd)
        except FilesystemError:
            raise
        except OSError:
            _fail('source path contains a symlink or is unavailable')
        finally:
            os.close(directory_fd)
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except OSError:
            _fail('source path is unavailable')
        if stat.S_ISLNK(current_stat.st_mode) or path_is_junction(current):
            _fail('source path contains a link or reparse point')
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute, flags)
        path_stat = os.lstat(absolute)
        descriptor_stat = os.fstat(descriptor)
        if (path_stat.st_dev, path_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            _fail('source path changed while it was opened')
        return descriptor
    except FilesystemError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        _fail('source path is unavailable')


def write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError('filesystem write made no progress')
        view = view[written:]


def acquire_file_lock(
    descriptor: int,
    *,
    is_windows: bool | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'0')
            fsync_descriptor(descriptor, is_windows=True)
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        locking(descriptor, cast(int, module.__dict__['LK_LOCK']), 1)
    else:
        module = importlib.import_module('fcntl')
        flock = cast(Callable[[int, int], None], module.__dict__['flock'])
        flock(descriptor, cast(int, module.__dict__['LOCK_EX']))


def try_acquire_file_lock(
    descriptor: int,
    *,
    is_windows: bool | None = None,
) -> bool:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'0')
            fsync_descriptor(descriptor, is_windows=True)
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        try:
            locking(descriptor, cast(int, module.__dict__['LK_NBLCK']), 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    module = importlib.import_module('fcntl')
    flock = cast(Callable[[int, int], None], module.__dict__['flock'])
    try:
        flock(
            descriptor,
            cast(int, module.__dict__['LOCK_EX']) | cast(int, module.__dict__['LOCK_NB']),
        )
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def release_file_lock(
    descriptor: int,
    *,
    is_windows: bool | None = None,
) -> None:
    windows = _IS_WINDOWS if is_windows is None else is_windows
    if windows:
        os.lseek(descriptor, 0, os.SEEK_SET)
        module = importlib.import_module('msvcrt')
        locking = cast(Callable[[int, int, int], None], module.__dict__['locking'])
        locking(descriptor, cast(int, module.__dict__['LK_UNLCK']), 1)
    else:
        module = importlib.import_module('fcntl')
        flock = cast(Callable[[int, int], None], module.__dict__['flock'])
        flock(descriptor, cast(int, module.__dict__['LOCK_UN']))


@contextmanager
def process_lock(
    root: Path,
    lock_name: str,
    thread_lock: threading.RLock,
) -> Iterator[None]:
    if '/' in lock_name or '\\' in lock_name or lock_name in {'', '.', '..'}:
        _fail('process lock name is invalid')
    lock_path = root / lock_name
    with thread_lock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            if _IS_WINDOWS:
                descriptor = require_windows_native().open_lock(lock_path)
            else:
                root_fd = open_generated_directory(root, root)
                try:
                    descriptor = os.open(lock_name, flags, 0o600, dir_fd=root_fd)
                finally:
                    os.close(root_fd)
        except (FilesystemError, OSError):
            _fail('process lock is unavailable')
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                _fail('process lock must be a regular file')
            set_private_permissions(lock_path, 0o600)
            acquire_file_lock(descriptor)
            try:
                yield
            finally:
                release_file_lock(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    'FilesystemError',
    '_HAS_DIRECTORY_FDS',
    '_IS_WINDOWS',
    '_WINDOWS_NATIVE',
    '_WindowsAccessAllowedAce',
    '_WindowsAclSizeInformation',
    '_WindowsAceHeader',
    '_WindowsFileDispositionInfo',
    '_WindowsNativeApi',
    '_WindowsNativeLayer',
    'acquire_file_lock',
    'create_generated_file',
    'directory_entry_is_reparse',
    'ensure_generated_directory',
    'fsync_descriptor',
    'fsync_directory',
    'generated_entry_stat',
    'generated_quarantine_identity',
    'is_generated_quarantine_name',
    'open_generated_directory',
    'open_generated_file',
    'open_generated_lock',
    'open_path_readonly',
    'path_is_junction',
    'prepare_private_root',
    'process_lock',
    'release_file_lock',
    'replace_generated',
    'require_directory_path',
    'require_windows_native',
    'secure_unlink',
    'set_private_permissions',
    'try_acquire_file_lock',
    'unlink_generated_file',
    'verify_private_permissions',
    'write_all',
]
