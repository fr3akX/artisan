#
# ABOUT
# Artisan Roast Server verified namespaced profile cache
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

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    Final,
    NoReturn,
    Protocol,
    Self,
    cast,
    runtime_checkable,
)
from uuid import UUID, uuid4

from artisanlib.roastserver import _filesystem as secure_filesystem
from artisanlib.roastserver.contract import (
    ArchiveFilters,
    ContractError,
    FAILURE_MESSAGES,
    FailureKind,
    FrozenJsonArray,
    FrozenJsonObject,
    JsonValue,
    LabelSummary,
    MAX_JSON_BYTES,
    MAX_PROFILE_BYTES,
    Namespace,
    PublicFailure,
    Revision,
    RoastDetail,
    RoastSummary,
    ServerProfileSource,
    parse_revision_upload,
    parse_roast_page,
    validate_archive_filters,
)

if TYPE_CHECKING:
    from artisanlib.roastserver.api import DownloadReceipt

_SCHEMA_VERSION: Final[int] = 1
_COPY_CHUNK_BYTES: Final[int] = 1024 * 1024
_LOCK_NAME: Final[str] = '.cache.lock'
_NAMESPACE_KEY_RE: Final[re.Pattern[str]] = re.compile(r'^namespace-sha256:([0-9a-f]{64})$')
_NAMESPACE_DIRECTORY_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_UUID_HEX_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}$')
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{64}$')
_CACHE_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r'^([1-9][0-9]*)-([0-9a-f]{64})\.(alog|json)$'
)
_TEMP_FILE_RE: Final[re.Pattern[str]] = re.compile(r'^[0-9a-f]{32}\.part$')
_STAGE_FILE_RE: Final[re.Pattern[str]] = re.compile(r'^([0-9a-f]{32})\.(part|lock)$')
_SIDECAR_KEYS: Final[frozenset[str]] = frozenset(
    {
        'schema_version',
        'origin',
        'organization_uuid',
        'roast',
        'revision',
        'downloaded_at',
    }
)

CACHE_FAILURE: Final[PublicFailure] = PublicFailure(
    kind=FailureKind.CACHE_CORRUPT,
    code=FailureKind.CACHE_CORRUPT.value,
    message=FAILURE_MESSAGES[FailureKind.CACHE_CORRUPT],
    retryable=False,
)

# Keep narrow aliases as deterministic durability/publication test seams while
# the implementation itself is shared with the durable outbox.
_fsync_descriptor = secure_filesystem.fsync_descriptor
_replace_generated = secure_filesystem.replace_generated


class CacheError(RuntimeError):
    def __init__(self, failure: PublicFailure = CACHE_FAILURE) -> None:
        self.failure = failure
        super().__init__(failure.message)


@dataclass(frozen=True, slots=True)
class CachedRevision:
    namespace: Namespace
    roast: RoastSummary
    revision: Revision
    path: Path
    sidecar_path: Path
    downloaded_at: datetime

    @property
    def source(self) -> ServerProfileSource:
        return ServerProfileSource(
            namespace=self.namespace,
            roast_uuid=self.roast.roast_uuid,
            revision_number=self.revision.revision_number,
            sha256=self.revision.sha256,
            stale=True,
        )


@dataclass(frozen=True, slots=True)
class CacheStats:
    byte_count: int
    revision_count: int


@dataclass(frozen=True, slots=True)
class CachedPage:
    items: tuple[CachedRevision, ...]


@dataclass(slots=True)
class _OwnedStage:
    path: Path
    identity: tuple[int, int]
    output: BinaryIO
    lock_path: Path
    lock_identity: tuple[int, int]
    lock_descriptor: int


@dataclass(frozen=True, slots=True)
class _ProtectedPath:
    path: Path
    descriptor: int
    identity: tuple[int, int]


type _FileIdentity = tuple[int, int, int, int, int]


@runtime_checkable
class _DownloadReceiptLike(Protocol):
    roast_uuid: UUID
    revision_number: int
    sha256: str
    byte_count: int
    filename: str


class CacheStore:
    """Verified cache below a private connector-owned root.

    Mutation uses one advisory process lock. Under the documented private-root
    model this protects against connector process races; links, reparse points,
    stale permissions, malformed sidecars, and crash residue still fail closed.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._lock = threading.RLock()
        self._staging: dict[Path, _OwnedStage] = {}
        self._closed = False
        try:
            secure_filesystem.prepare_private_root(self.root)
            with self._filesystem_lock():
                self._startup_maintenance()
        except CacheError:
            raise
        except (OSError, ValueError, secure_filesystem.FilesystemError):
            raise CacheError from None

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def new_staging_file(self, namespace: Namespace) -> tuple[Path, BinaryIO]:
        try:
            self._require_open()
            namespace_key = _namespace_key(namespace)
            with self._filesystem_lock():
                return self._new_staging_file_locked(namespace_key)
        except CacheError:
            raise
        except (OSError, ValueError, secure_filesystem.FilesystemError):
            raise CacheError from None

    def _new_staging_file_locked(self, namespace_key: str) -> tuple[Path, BinaryIO]:
        lock_descriptor: int | None = None
        lock_identity: tuple[int, int] | None = None
        lock_acquired = False
        part_descriptor: int | None = None
        part_identity: tuple[int, int] | None = None
        output: BinaryIO | None = None
        path: Path | None = None
        lock_path: Path | None = None
        try:
            temporary_directory = self._temporary_directory(namespace_key)
            secure_filesystem.ensure_generated_directory(self.root, temporary_directory)
            token = uuid4().hex
            path = temporary_directory / f'{token}.part'
            lock_path = temporary_directory / f'{token}.lock'
            lock_descriptor = secure_filesystem.create_generated_file(
                self.root, lock_path, 0o600
            )
            lock_stat = os.fstat(lock_descriptor)
            lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
            secure_filesystem.acquire_file_lock(lock_descriptor)
            lock_acquired = True
            part_descriptor = secure_filesystem.create_generated_file(
                self.root, path, 0o600
            )
            part_stat = os.fstat(part_descriptor)
            part_identity = (part_stat.st_dev, part_stat.st_ino)
            _fsync_descriptor(part_descriptor)
            secure_filesystem.fsync_directory(temporary_directory)
            output = cast(BinaryIO, os.fdopen(part_descriptor, 'w+b'))
            part_descriptor = None
            stage = _OwnedStage(
                path=path,
                identity=part_identity,
                output=output,
                lock_path=lock_path,
                lock_identity=lock_identity,
                lock_descriptor=lock_descriptor,
            )
            self._staging[path] = stage
            lock_descriptor = None
            return path, output
        except BaseException as error:
            cleanup_failed = False
            if output is not None:
                try:
                    output.close()
                except BaseException:
                    cleanup_failed = True
            if part_descriptor is not None:
                try:
                    os.close(part_descriptor)
                except OSError:
                    cleanup_failed = True
            if path is not None and part_identity is not None:
                try:
                    self._discard_owned_path(path, part_identity)
                except BaseException:
                    cleanup_failed = True
            if lock_descriptor is not None and lock_acquired:
                try:
                    secure_filesystem.release_file_lock(lock_descriptor)
                except BaseException:
                    cleanup_failed = True
            if lock_descriptor is not None:
                try:
                    os.close(lock_descriptor)
                except OSError:
                    cleanup_failed = True
            if lock_path is not None and lock_identity is not None:
                try:
                    self._discard_owned_path(lock_path, lock_identity)
                except BaseException:
                    cleanup_failed = True
            if cleanup_failed:
                raise CacheError from None
            if isinstance(error, CacheError):
                raise
            if isinstance(error, (OSError, ValueError, secure_filesystem.FilesystemError)):
                raise CacheError from None
            raise

    def discard_staging(self, path: Path) -> None:
        staged = Path(path)
        try:
            self._require_open()
            with self._filesystem_lock():
                stage = self._staging.get(staged)
                if stage is None or self._cleanup_owned_stage(stage):
                    raise CacheError
        except CacheError:
            raise
        except (OSError, ValueError, secure_filesystem.FilesystemError):
            raise CacheError from None

    def close(self) -> None:
        if self._closed:
            return
        failed = False
        try:
            with self._filesystem_lock():
                for stage in tuple(self._staging.values()):
                    if self._cleanup_owned_stage(stage):
                        failed = True
        except CacheError:
            failed = True
            for stage in tuple(self._staging.values()):
                if self._cleanup_owned_stage(stage):
                    failed = True
        finally:
            self._closed = True
        if failed:
            raise CacheError

    def publish(
        self,
        namespace: Namespace,
        detail: RoastDetail,
        receipt: DownloadReceipt,
        staged_path: Path,
        validated_at: datetime,
    ) -> CachedRevision:
        staged = Path(staged_path)
        try:
            self._require_open()
            with self._filesystem_lock():
                try:
                    return self._publish_locked(
                        namespace, detail, receipt, staged, validated_at
                    )
                except BaseException:
                    stage = self._staging.get(staged)
                    if stage is not None and self._cleanup_owned_stage(stage):
                        raise CacheError from None
                    raise
        except CacheError:
            raise
        except (
            ContractError,
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            secure_filesystem.FilesystemError,
        ):
            raise CacheError from None

    def find_current(
        self,
        namespace: Namespace,
        roast_uuid: UUID,
        revision_number: int,
        sha256: str,
    ) -> CachedRevision | None:
        try:
            namespace_key = _namespace_key(namespace)
            roast_hex = _uuid_hex(roast_uuid)
            revision = _revision_number(revision_number)
            digest = _sha256(sha256)
            with self._filesystem_lock():
                path = self._profile_path(namespace_key, roast_hex, revision, digest)
                sidecar_path = path.with_suffix('.json')
                profile_exists = os.path.lexists(path)
                sidecar_exists = os.path.lexists(sidecar_path)
                if not profile_exists and not sidecar_exists:
                    return None
                if not profile_exists or not sidecar_exists:
                    raise CacheError
                cached = self._load_pair(namespace, path, sidecar_path)
                if (
                    cached.roast.roast_uuid != roast_uuid
                    or cached.revision.revision_number != revision
                    or cached.revision.sha256 != digest
                ):
                    raise CacheError
                return cached
        except CacheError:
            raise
        except (
            ContractError,
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            secure_filesystem.FilesystemError,
        ):
            raise CacheError from None

    def validate(self, cached: CachedRevision) -> CachedRevision:
        try:
            with self._filesystem_lock():
                namespace_key = _namespace_key(cached.namespace)
                expected_path = self._profile_path(
                    namespace_key,
                    _uuid_hex(cached.roast.roast_uuid),
                    _revision_number(cached.revision.revision_number),
                    _sha256(cached.revision.sha256),
                )
                if cached.path != expected_path or cached.sidecar_path != expected_path.with_suffix(
                    '.json'
                ):
                    raise CacheError
                loaded = self._load_pair(
                    cached.namespace, cached.path, cached.sidecar_path
                )
                if loaded != cached:
                    raise CacheError
                return loaded
        except CacheError:
            raise
        except (
            ContractError,
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            secure_filesystem.FilesystemError,
        ):
            raise CacheError from None

    def list_offline(self, namespace: Namespace, filters: ArchiveFilters) -> CachedPage:
        normalized = validate_archive_filters(filters)
        try:
            with self._filesystem_lock():
                revisions = self._scan_namespace(namespace)
                latest: dict[UUID, CachedRevision] = {}
                for cached in revisions:
                    current = latest.get(cached.roast.roast_uuid)
                    if current is None or _latest_key(cached) > _latest_key(current):
                        latest[cached.roast.roast_uuid] = cached
                items = [
                    cached
                    for cached in latest.values()
                    if _matches_filters(cached.roast, normalized)
                ]
                items.sort(key=lambda cached: cached.roast.roast_uuid.hex)
                items.sort(key=lambda cached: cached.roast.roast_at, reverse=True)
                return CachedPage(tuple(items))
        except CacheError:
            raise
        except (
            ContractError,
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            secure_filesystem.FilesystemError,
        ):
            raise CacheError from None

    def stats(self, namespace: Namespace) -> CacheStats:
        try:
            with self._filesystem_lock():
                return _stats(self._scan_namespace(namespace))
        except CacheError:
            raise
        except (
            ContractError,
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            secure_filesystem.FilesystemError,
        ):
            raise CacheError from None

    def prune(
        self,
        namespace: Namespace,
        limit_bytes: int,
        protected_paths: frozenset[Path],
    ) -> CacheStats:
        if type(limit_bytes) is not int or limit_bytes < 0:
            raise ValueError('cache limit is invalid')
        if not isinstance(protected_paths, frozenset):
            raise ValueError('protected paths are invalid')
        try:
            self._require_open()
            with self._filesystem_lock(), self._protected_paths(protected_paths) as protected:
                revisions = list(self._scan_namespace(namespace))
                protected_identities = frozenset(item.identity for item in protected)
                total = sum(item.revision.byte_size for item in revisions)
                candidates = sorted(revisions, key=_lru_key)
                for cached in candidates:
                    if total <= limit_bytes:
                        break
                    if self._path_identity(cached.path) in protected_identities:
                        continue
                    self._verify_protected_paths(protected)
                    self._remove_pair(cached)
                    revisions.remove(cached)
                    total -= cached.revision.byte_size
                return CacheStats(total, len(revisions))
        except CacheError:
            raise
        except (
            ContractError,
            OSError,
            RecursionError,
            UnicodeError,
            ValueError,
            secure_filesystem.FilesystemError,
        ):
            raise CacheError from None

    def clear_unused(
        self, namespace: Namespace, protected_paths: frozenset[Path]
    ) -> CacheStats:
        return self.prune(namespace, 0, protected_paths)

    def _publish_locked(
        self,
        namespace: Namespace,
        detail: RoastDetail,
        receipt: DownloadReceipt,
        staged_path: Path,
        validated_at: datetime,
    ) -> CachedRevision:
        namespace_key = _namespace_key(namespace)
        downloaded_at = _canonical_datetime(validated_at)
        roast = _summary_from_detail(detail)
        revision = detail.current_revision
        if revision is None:
            raise CacheError
        _validate_publication_identity(detail, revision, receipt)
        expected_staging_directory = self._temporary_directory(namespace_key)
        try:
            staged_path.relative_to(expected_staging_directory)
        except ValueError:
            raise CacheError from None
        if (
            staged_path.parent != expected_staging_directory
            or _TEMP_FILE_RE.fullmatch(staged_path.name) is None
        ):
            raise CacheError
        stage = self._staging.get(staged_path)
        if stage is None:
            raise CacheError

        destination = self._profile_path(
            namespace_key,
            detail.roast_uuid.hex,
            revision.revision_number,
            revision.sha256,
        )
        sidecar_path = destination.with_suffix('.json')
        secure_filesystem.ensure_generated_directory(self.root, destination.parent)
        copy_path = expected_staging_directory / f'{uuid4().hex}.part'
        sidecar_temporary_path = expected_staging_directory / f'{uuid4().hex}.part'
        copy_identity: tuple[int, int] | None = None
        sidecar_temporary_identity: tuple[int, int] | None = None
        published_profile_identity: tuple[int, int] | None = None
        published_sidecar_identity: tuple[int, int] | None = None
        result: CachedRevision | None = None
        operation_error: BaseException | None = None
        try:
            if not stage.output.closed:
                stage.output.flush()
                stage.output.close()
            copy_descriptor = secure_filesystem.create_generated_file(
                self.root, copy_path, 0o600
            )
            copy_stat = os.fstat(copy_descriptor)
            copy_identity = (copy_stat.st_dev, copy_stat.st_ino)
            try:
                self._copy_and_verify_staged(
                    staged_path,
                    stage.identity,
                    copy_descriptor,
                    copy_path,
                    revision.sha256,
                    revision.byte_size,
                )
            finally:
                os.close(copy_descriptor)
            profile_exists = os.path.lexists(destination)
            sidecar_exists = os.path.lexists(sidecar_path)
            if profile_exists or sidecar_exists:
                if not profile_exists or not sidecar_exists:
                    raise CacheError
                existing = self._load_pair(namespace, destination, sidecar_path)
                if existing.roast != roast or existing.revision != revision:
                    raise CacheError
                result = existing
            else:
                sidecar_bytes = _sidecar_bytes(namespace, roast, revision, downloaded_at)
                sidecar_descriptor = secure_filesystem.create_generated_file(
                    self.root, sidecar_temporary_path, 0o600
                )
                sidecar_temporary_stat = os.fstat(sidecar_descriptor)
                sidecar_temporary_identity = (
                    sidecar_temporary_stat.st_dev,
                    sidecar_temporary_stat.st_ino,
                )
                try:
                    self._write_temporary(
                        sidecar_temporary_path, sidecar_descriptor, sidecar_bytes
                    )
                finally:
                    os.close(sidecar_descriptor)
                published_profile_identity = copy_identity
                _replace_generated(self.root, copy_path, destination)
                secure_filesystem.set_private_permissions(destination, 0o600)
                published_sidecar_identity = sidecar_temporary_identity
                _replace_generated(self.root, sidecar_temporary_path, sidecar_path)
                secure_filesystem.set_private_permissions(sidecar_path, 0o600)
                expected = CachedRevision(
                    namespace=namespace,
                    roast=roast,
                    revision=revision,
                    path=destination,
                    sidecar_path=sidecar_path,
                    downloaded_at=downloaded_at,
                )
                result = self._load_pair(
                    namespace, destination, sidecar_path, expected=expected
                )
        except BaseException as error:
            operation_error = error

        cleanup_failed = self._cleanup_owned_paths(
            (
                (copy_path, copy_identity),
                (sidecar_temporary_path, sidecar_temporary_identity),
            )
        )
        if self._cleanup_owned_stage_data(stage):
            cleanup_failed = True
        if (
            operation_error is not None or cleanup_failed
        ) and self._cleanup_owned_paths(
            (
                (sidecar_path, published_sidecar_identity),
                (destination, published_profile_identity),
            )
        ):
            cleanup_failed = True
        release_failed = self._release_owned_stage(stage)
        if (
            release_failed
            and operation_error is None
            and not cleanup_failed
            and self._cleanup_owned_paths(
                (
                    (sidecar_path, published_sidecar_identity),
                    (destination, published_profile_identity),
                )
            )
        ):
            cleanup_failed = True
        if operation_error is not None or cleanup_failed or release_failed:
            if operation_error is not None and not isinstance(
                operation_error,
                (
                    CacheError,
                    ContractError,
                    OSError,
                    RecursionError,
                    UnicodeError,
                    ValueError,
                    secure_filesystem.FilesystemError,
                ),
            ):
                raise operation_error
            raise CacheError from None
        if result is None:
            raise CacheError
        return result

    def _copy_and_verify_staged(
        self,
        staged_path: Path,
        expected_identity: tuple[int, int],
        destination: int,
        copy_path: Path,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> None:
        source = secure_filesystem.open_generated_file(self.root, staged_path)
        try:
            before = os.fstat(source)
            if not stat.S_ISREG(before.st_mode):
                raise CacheError
            if (before.st_dev, before.st_ino) != expected_identity:
                raise CacheError
            secure_filesystem.verify_private_permissions(staged_path, 0o600)
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = _read_chunk(source)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > MAX_PROFILE_BYTES:
                    raise CacheError
                digest.update(chunk)
                secure_filesystem.write_all(destination, chunk)
            if byte_count < 1 or byte_count != expected_byte_count:
                raise CacheError
            if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                raise CacheError
            after = os.fstat(source)
            if _file_identity(before) != _file_identity(after):
                raise CacheError
            entry = secure_filesystem.generated_entry_stat(self.root, staged_path)
            if _file_identity(after) != _file_identity(entry):
                raise CacheError
            _fsync_descriptor(destination)
            secure_filesystem.set_private_permissions(copy_path, 0o600)
        finally:
            os.close(source)

    @staticmethod
    def _write_temporary(path: Path, descriptor: int, content: bytes) -> None:
        secure_filesystem.write_all(descriptor, content)
        _fsync_descriptor(descriptor)
        secure_filesystem.set_private_permissions(path, 0o600)

    def _load_pair(
        self,
        namespace: Namespace,
        path: Path,
        sidecar_path: Path,
        *,
        expected: CachedRevision | None = None,
    ) -> CachedRevision:
        cached = self._read_sidecar(namespace, sidecar_path)
        expected_path = self._profile_path(
            _namespace_key(namespace),
            cached.roast.roast_uuid.hex,
            cached.revision.revision_number,
            cached.revision.sha256,
        )
        if path != expected_path or sidecar_path != expected_path.with_suffix('.json'):
            raise CacheError
        if expected is not None and cached != expected:
            raise CacheError
        self._verify_profile(path, cached.revision)
        return cached

    def _read_sidecar(self, namespace: Namespace, sidecar_path: Path) -> CachedRevision:
        content = self._read_generated_bytes(sidecar_path, MAX_JSON_BYTES)
        try:
            text = content.decode('utf-8')
            decoded = cast(
                object,
                json.loads(
                    text,
                    object_pairs_hook=_reject_duplicate_pairs,
                    parse_constant=_reject_json_constant,
                ),
            )
        except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise CacheError from None
        if not isinstance(decoded, dict):
            raise CacheError
        value = cast(dict[str, object], decoded)
        if set(value) != set(_SIDECAR_KEYS):
            raise CacheError
        if type(value['schema_version']) is not int or value['schema_version'] != _SCHEMA_VERSION:
            raise CacheError
        if value['origin'] != namespace.origin:
            raise CacheError
        if value['organization_uuid'] != str(namespace.organization_id):
            raise CacheError
        roast_value: object = value['roast']
        revision_value = value['revision']
        roast = parse_roast_page({'items': [roast_value], 'next_cursor': None}).items[0]
        if not isinstance(revision_value, dict):
            raise CacheError
        revision_mapping = cast(dict[str, object], revision_value)
        state = 'parsed' if roast.state == 'parsed' else 'parse_failed'
        base = f'/api/v1/roasts/{roast.roast_uuid.hex}'
        revision_upload = parse_revision_upload(
            {
                'roast_uuid': roast.roast_uuid.hex,
                'state': state,
                'revision': revision_mapping,
                'links': {
                    'roast': base,
                    'chart': f'{base}/chart',
                    'revisions': f'{base}/revisions',
                    'download': (
                        f'{base}/revisions/{revision_mapping.get("revision_number")}/download'
                    ),
                },
            }
        )
        revision = revision_upload.revision
        if (
            roast.state == 'awaiting_profile'
            or revision.revision_number != roast.revision_count
        ):
            raise CacheError
        downloaded_at = _stored_datetime(value['downloaded_at'])
        cached = CachedRevision(
            namespace=namespace,
            roast=roast,
            revision=revision,
            path=sidecar_path.with_suffix('.alog'),
            sidecar_path=sidecar_path,
            downloaded_at=downloaded_at,
        )
        if content != _sidecar_bytes(namespace, roast, revision, downloaded_at):
            raise CacheError
        return cached

    def _read_generated_bytes(self, path: Path, maximum: int) -> bytes:
        descriptor = secure_filesystem.open_generated_file(self.root, path)
        chunks: list[bytes] = []
        byte_count = 0
        try:
            before = os.fstat(descriptor)
            secure_filesystem.verify_private_permissions(path, 0o600)
            while True:
                chunk = _read_chunk(descriptor)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > maximum:
                    raise CacheError
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if _file_identity(before) != _file_identity(after):
                raise CacheError
            entry = secure_filesystem.generated_entry_stat(self.root, path)
            if _file_identity(after) != _file_identity(entry):
                raise CacheError
        finally:
            os.close(descriptor)
        if byte_count < 1:
            raise CacheError
        return b''.join(chunks)

    def _verify_profile(self, path: Path, revision: Revision) -> None:
        descriptor = secure_filesystem.open_generated_file(self.root, path)
        digest = hashlib.sha256()
        byte_count = 0
        try:
            before = os.fstat(descriptor)
            secure_filesystem.verify_private_permissions(path, 0o600)
            while True:
                chunk = _read_chunk(descriptor)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > MAX_PROFILE_BYTES:
                    raise CacheError
                digest.update(chunk)
            after = os.fstat(descriptor)
            if _file_identity(before) != _file_identity(after):
                raise CacheError
            entry = secure_filesystem.generated_entry_stat(self.root, path)
            if _file_identity(after) != _file_identity(entry):
                raise CacheError
        finally:
            os.close(descriptor)
        if (
            byte_count < 1
            or byte_count != revision.byte_size
            or not hmac.compare_digest(digest.hexdigest(), revision.sha256)
        ):
            raise CacheError

    def _scan_namespace(self, namespace: Namespace) -> tuple[CachedRevision, ...]:
        namespace_key = _namespace_key(namespace)
        namespace_directory = self.root / namespace_key
        if not os.path.lexists(namespace_directory):
            return ()
        self._require_private_directory(namespace_directory)
        allowed_namespace_entries = {'tmp', 'roasts'}
        for entry in os.scandir(namespace_directory):
            if secure_filesystem.directory_entry_is_reparse(entry):
                raise CacheError
            if entry.name not in allowed_namespace_entries:
                raise CacheError
        self._maintain_temporary_directory(namespace_key)
        roasts_directory = namespace_directory / 'roasts'
        if not os.path.lexists(roasts_directory):
            return ()
        self._require_private_directory(roasts_directory)
        result: list[CachedRevision] = []
        for roast_entry in os.scandir(roasts_directory):
            if (
                secure_filesystem.directory_entry_is_reparse(roast_entry)
                or _UUID_HEX_RE.fullmatch(roast_entry.name) is None
                or not roast_entry.is_dir(follow_symlinks=False)
            ):
                raise CacheError
            roast_directory = Path(roast_entry.path)
            self._require_private_directory(roast_directory)
            files: dict[tuple[int, str], dict[str, Path]] = {}
            for file_entry in os.scandir(roast_directory):
                if secure_filesystem.directory_entry_is_reparse(file_entry):
                    raise CacheError
                match = _CACHE_FILE_RE.fullmatch(file_entry.name)
                if match is None or not file_entry.is_file(follow_symlinks=False):
                    raise CacheError
                revision_number = _revision_number(int(match.group(1)))
                sha256 = _sha256(match.group(2))
                extension = match.group(3)
                files.setdefault((revision_number, sha256), {})[extension] = Path(
                    file_entry.path
                )
            for (_revision, _sha), pair in sorted(files.items()):
                profile = pair.get('alog')
                sidecar = pair.get('json')
                if profile is not None and sidecar is None:
                    self._discard_generated(profile)
                    continue
                if profile is None or sidecar is None:
                    raise CacheError
                cached = self._load_pair(namespace, profile, sidecar)
                if cached.roast.roast_uuid.hex != roast_entry.name:
                    raise CacheError
                result.append(cached)
        return tuple(result)

    def _startup_maintenance(self) -> None:
        for entry in os.scandir(self.root):
            if entry.name == _LOCK_NAME:
                if secure_filesystem.directory_entry_is_reparse(entry) or not entry.is_file(
                    follow_symlinks=False
                ):
                    raise CacheError
                continue
            if (
                _NAMESPACE_DIRECTORY_RE.fullmatch(entry.name) is None
                or secure_filesystem.directory_entry_is_reparse(entry)
                or not entry.is_dir(follow_symlinks=False)
            ):
                raise CacheError
            directory = Path(entry.path)
            self._require_private_directory(directory)
            for namespace_entry in os.scandir(directory):
                if (
                    secure_filesystem.directory_entry_is_reparse(namespace_entry)
                    or namespace_entry.name not in {'tmp', 'roasts'}
                    or not namespace_entry.is_dir(follow_symlinks=False)
                ):
                    raise CacheError
            self._maintain_temporary_directory(entry.name)
            self._maintain_roast_tree(directory / 'roasts')

    def _maintain_roast_tree(self, roasts_directory: Path) -> None:
        if not os.path.lexists(roasts_directory):
            return
        self._require_private_directory(roasts_directory)
        for roast_entry in os.scandir(roasts_directory):
            if (
                secure_filesystem.directory_entry_is_reparse(roast_entry)
                or _UUID_HEX_RE.fullmatch(roast_entry.name) is None
                or not roast_entry.is_dir(follow_symlinks=False)
            ):
                raise CacheError
            roast_directory = Path(roast_entry.path)
            self._require_private_directory(roast_directory)
            files: dict[tuple[int, str], set[str]] = {}
            paths: dict[tuple[int, str, str], Path] = {}
            for file_entry in os.scandir(roast_directory):
                if secure_filesystem.directory_entry_is_reparse(file_entry):
                    raise CacheError
                match = _CACHE_FILE_RE.fullmatch(file_entry.name)
                if match is None or not file_entry.is_file(follow_symlinks=False):
                    raise CacheError
                revision = _revision_number(int(match.group(1)))
                sha256 = _sha256(match.group(2))
                extension = match.group(3)
                files.setdefault((revision, sha256), set()).add(extension)
                paths[(revision, sha256, extension)] = Path(file_entry.path)
            for (revision, sha256), extensions in files.items():
                if extensions == {'alog'}:
                    self._discard_generated(paths[(revision, sha256, 'alog')])
                elif extensions != {'alog', 'json'}:
                    raise CacheError

    def _maintain_temporary_directory(self, namespace_key: str) -> None:
        temporary_directory = self._temporary_directory(namespace_key)
        if not os.path.lexists(temporary_directory):
            return
        self._require_private_directory(temporary_directory)
        stages: dict[str, dict[str, Path]] = {}
        for entry in os.scandir(temporary_directory):
            if secure_filesystem.directory_entry_is_reparse(entry):
                raise CacheError
            match = _STAGE_FILE_RE.fullmatch(entry.name)
            if match is None or not entry.is_file(follow_symlinks=False):
                raise CacheError
            stages.setdefault(match.group(1), {})[match.group(2)] = Path(entry.path)
        for token in sorted(stages):
            pair = stages[token]
            part_path = pair.get('part')
            lock_path = pair.get('lock')
            if part_path is not None:
                owned = self._staging.get(part_path)
                if owned is not None:
                    if lock_path != owned.lock_path:
                        raise CacheError
                    continue
            if lock_path is None:
                if part_path is None:
                    raise CacheError
                self._discard_generated(part_path)
                continue
            lock_descriptor = secure_filesystem.open_generated_lock(
                self.root, lock_path
            )
            lock_stat = os.fstat(lock_descriptor)
            lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
            acquired = False
            try:
                acquired = secure_filesystem.try_acquire_file_lock(lock_descriptor)
                if not acquired:
                    if part_path is None:
                        raise CacheError
                    continue
                cleanup_failed = False
                if part_path is not None:
                    try:
                        part_identity = self._path_identity(part_path)
                        self._discard_owned_path(part_path, part_identity)
                    except BaseException:
                        cleanup_failed = True
                try:
                    secure_filesystem.release_file_lock(lock_descriptor)
                    acquired = False
                except BaseException:
                    cleanup_failed = True
                try:
                    os.close(lock_descriptor)
                    lock_descriptor = -1
                except OSError:
                    cleanup_failed = True
                try:
                    self._discard_owned_path(lock_path, lock_identity)
                except BaseException:
                    cleanup_failed = True
                if cleanup_failed:
                    raise CacheError
            finally:
                if acquired:
                    try:
                        secure_filesystem.release_file_lock(lock_descriptor)
                    except BaseException:
                        pass
                if lock_descriptor >= 0:
                    os.close(lock_descriptor)

    def _require_private_directory(self, path: Path) -> None:
        secure_filesystem.require_directory_path(path)
        secure_filesystem.set_private_permissions(path, 0o700)

    def _remove_pair(self, cached: CachedRevision) -> None:
        sidecar_identity = self._path_identity(cached.sidecar_path)
        profile_identity = self._path_identity(cached.path)
        # The sidecar is the index. Attempt it first, but a failure must not
        # prevent cleanup of the profile path owned by this exact pair.
        if self._cleanup_owned_paths(
            (
                (cached.sidecar_path, sidecar_identity),
                (cached.path, profile_identity),
            )
        ):
            raise CacheError

    @contextmanager
    def _protected_paths(
        self, protected_paths: frozenset[Path]
    ) -> Iterator[tuple[_ProtectedPath, ...]]:
        opened: list[_ProtectedPath] = []
        try:
            absolute_paths = sorted(
                (Path(os.path.abspath(os.fspath(path))) for path in protected_paths),
                key=os.fspath,
            )
            for path in absolute_paths:
                descriptor = secure_filesystem.open_path_readonly(path)
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    os.close(descriptor)
                    raise CacheError
                opened.append(
                    _ProtectedPath(
                        path,
                        descriptor,
                        (file_stat.st_dev, file_stat.st_ino),
                    )
                )
            protected = tuple(opened)
            self._verify_protected_paths(protected)
            yield protected
        except CacheError:
            raise
        except (OSError, ValueError, secure_filesystem.FilesystemError):
            raise CacheError from None
        finally:
            for opened_path in opened:
                try:
                    os.close(opened_path.descriptor)
                except OSError:
                    pass

    @staticmethod
    def _verify_protected_paths(protected_paths: tuple[_ProtectedPath, ...]) -> None:
        for protected in protected_paths:
            descriptor = secure_filesystem.open_path_readonly(protected.path)
            try:
                file_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or (file_stat.st_dev, file_stat.st_ino) != protected.identity
                ):
                    raise CacheError
            finally:
                os.close(descriptor)

    def _path_identity(self, path: Path) -> tuple[int, int]:
        descriptor = secure_filesystem.open_generated_file(self.root, path)
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise CacheError
            return file_stat.st_dev, file_stat.st_ino
        finally:
            os.close(descriptor)

    def _cleanup_owned_stage(self, stage: _OwnedStage) -> bool:
        failed = self._cleanup_owned_stage_data(stage)
        if self._release_owned_stage(stage):
            failed = True
        return failed

    def _cleanup_owned_stage_data(self, stage: _OwnedStage) -> bool:
        failed = False
        if not stage.output.closed:
            try:
                stage.output.close()
            except BaseException:
                failed = True
        try:
            self._discard_owned_path(stage.path, stage.identity)
        except BaseException:
            failed = True
        return failed

    def _release_owned_stage(self, stage: _OwnedStage) -> bool:
        self._staging.pop(stage.path, None)
        failed = False
        try:
            secure_filesystem.release_file_lock(stage.lock_descriptor)
        except BaseException:
            failed = True
        try:
            os.close(stage.lock_descriptor)
        except OSError:
            failed = True
        try:
            self._discard_owned_path(stage.lock_path, stage.lock_identity)
        except BaseException:
            failed = True
        return failed

    def _cleanup_owned_paths(
        self,
        paths: tuple[tuple[Path, tuple[int, int] | None], ...],
    ) -> bool:
        failed = False
        for path, identity in paths:
            if identity is None:
                continue
            try:
                self._discard_owned_path(path, identity)
            except BaseException:
                failed = True
        return failed

    def _discard_owned_path(self, path: Path, identity: tuple[int, int]) -> None:
        if not os.path.lexists(path):
            return
        if self._path_identity(path) != identity:
            raise CacheError
        secure_filesystem.unlink_generated_file(self.root, path, missing_ok=False)

    def _discard_generated(self, path: Path) -> None:
        try:
            secure_filesystem.unlink_generated_file(self.root, path, missing_ok=True)
        except (OSError, secure_filesystem.FilesystemError):
            if os.path.lexists(path):
                raise CacheError from None

    def _require_open(self) -> None:
        if self._closed:
            raise CacheError

    @contextmanager
    def _filesystem_lock(self) -> Iterator[None]:
        try:
            with secure_filesystem.process_lock(self.root, _LOCK_NAME, self._lock):
                yield
        except CacheError:
            raise
        except (OSError, secure_filesystem.FilesystemError):
            raise CacheError from None

    def _temporary_directory(self, namespace_key: str) -> Path:
        if _NAMESPACE_DIRECTORY_RE.fullmatch(namespace_key) is None:
            raise CacheError
        return self.root / namespace_key / 'tmp'

    def _profile_path(
        self, namespace_key: str, roast_uuid: str, revision_number: int, sha256: str
    ) -> Path:
        if (
            _NAMESPACE_DIRECTORY_RE.fullmatch(namespace_key) is None
            or _UUID_HEX_RE.fullmatch(roast_uuid) is None
            or _SHA256_RE.fullmatch(sha256) is None
        ):
            raise CacheError
        return (
            self.root
            / namespace_key
            / 'roasts'
            / roast_uuid
            / f'{revision_number}-{sha256}.alog'
        )


def _namespace_key(namespace: object) -> str:
    if not isinstance(namespace, Namespace):
        raise CacheError
    origin = _namespace_origin(namespace.origin)
    organization_hex = _uuid_hex(namespace.organization_id)
    match = _NAMESPACE_KEY_RE.fullmatch(namespace.key)
    if match is None:
        raise CacheError
    try:
        expected = hashlib.sha256(
            f'{origin}\n{UUID(hex=organization_hex)}'.encode()
        ).hexdigest()
    except UnicodeEncodeError:
        raise CacheError from None
    if not hmac.compare_digest(match.group(1), expected):
        raise CacheError
    return match.group(1)


def _validate_publication_identity(
    detail: object, revision: Revision, receipt: object
) -> None:
    if not isinstance(detail, RoastDetail):
        raise CacheError
    if not isinstance(receipt, _DownloadReceiptLike):
        raise CacheError
    roast_uuid = cast(object, receipt.roast_uuid)
    revision_number = cast(object, receipt.revision_number)
    sha256 = cast(object, receipt.sha256)
    byte_count = cast(object, receipt.byte_count)
    filename = cast(object, receipt.filename)
    expected_filename = f'{detail.roast_uuid.hex}-r{revision.revision_number}.alog'
    if (
        not isinstance(roast_uuid, UUID)
        or type(revision_number) is not int
        or not isinstance(sha256, str)
        or type(byte_count) is not int
        or not isinstance(filename, str)
        or roast_uuid != detail.roast_uuid
        or revision_number != revision.revision_number
        or not hmac.compare_digest(sha256, revision.sha256)
        or byte_count != revision.byte_size
        or filename != expected_filename
        or byte_count < 1
        or byte_count > MAX_PROFILE_BYTES
    ):
        raise CacheError


def _summary_from_detail(detail: RoastDetail) -> RoastSummary:
    return RoastSummary(
        roast_uuid=detail.roast_uuid,
        state=detail.state,
        roast_at=detail.roast_at,
        title=detail.title,
        batch_prefix=detail.batch_prefix,
        batch_number=detail.batch_number,
        batch_position=detail.batch_position,
        operator=detail.operator,
        machine=detail.machine,
        machine_setup=detail.machine_setup,
        temperature_unit=detail.temperature_unit,
        duration_seconds=detail.duration_seconds,
        green_weight_kg=detail.green_weight_kg,
        roasted_weight_kg=detail.roasted_weight_kg,
        revision_count=detail.revision_count,
        updated_at=detail.updated_at,
        labels=detail.labels,
    )


def _sidecar_bytes(
    namespace: Namespace,
    roast: RoastSummary,
    revision: Revision,
    downloaded_at: datetime,
) -> bytes:
    value = {
        'schema_version': _SCHEMA_VERSION,
        'origin': namespace.origin,
        'organization_uuid': str(namespace.organization_id),
        'roast': _roast_value(roast),
        'revision': _revision_value(revision),
        'downloaded_at': _datetime_text(downloaded_at),
    }
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode()
    except (RecursionError, TypeError, ValueError, UnicodeError):
        raise CacheError from None
    if not result or len(result) > MAX_JSON_BYTES:
        raise CacheError
    return result


def _roast_value(roast: RoastSummary) -> dict[str, object]:
    return {
        'roast_uuid': roast.roast_uuid.hex,
        'state': roast.state,
        'roast_at': _datetime_text(roast.roast_at),
        'title': roast.title,
        'batch_prefix': roast.batch_prefix,
        'batch_number': roast.batch_number,
        'batch_position': roast.batch_position,
        'operator': roast.operator,
        'machine': roast.machine,
        'machine_setup': roast.machine_setup,
        'temperature_unit': roast.temperature_unit,
        'duration_seconds': roast.duration_seconds,
        'green_weight_kg': roast.green_weight_kg,
        'roasted_weight_kg': roast.roasted_weight_kg,
        'revision_count': roast.revision_count,
        'updated_at': _datetime_text(roast.updated_at),
        'labels': [_label_value(label) for label in roast.labels],
    }


def _label_value(label: LabelSummary) -> dict[str, object]:
    return {
        'label_uuid': label.label_uuid.hex,
        'name': label.name,
        'color': label.color,
        'archived': label.archived,
    }


def _revision_value(revision: Revision) -> dict[str, object]:
    return {
        'revision_number': revision.revision_number,
        'sha256': revision.sha256,
        'byte_size': revision.byte_size,
        'parser_version': revision.parser_version,
        'parse_state': revision.parse_state,
        'parse_diagnostic_code': revision.parse_diagnostic_code,
        'parse_diagnostic_message': revision.parse_diagnostic_message,
        'uploaded_at': _datetime_text(revision.uploaded_at),
        'metadata': _thaw_json_object(revision.metadata),
        'reparse_recommended': revision.reparse_recommended,
    }


def _thaw_json_object(value: FrozenJsonObject) -> dict[str, object]:
    return {key: _thaw_json(item) for key, item in value}


def _thaw_json(value: JsonValue) -> object:
    if isinstance(value, FrozenJsonArray):
        return [_thaw_json(item) for item in value.items]
    if isinstance(value, tuple):
        return {key: _thaw_json(item) for key, item in value}
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError('non-finite JSON number')


def _canonical_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CacheError
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise CacheError from None


def _datetime_text(value: datetime) -> str:
    return _canonical_datetime(value).isoformat(timespec='microseconds')


def _stored_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise CacheError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CacheError from None
    canonical = _canonical_datetime(parsed)
    if _datetime_text(canonical) != value:
        raise CacheError
    return canonical


def _revision_number(value: object) -> int:
    if type(value) is not int or value < 1 or value > 2_147_483_647:
        raise CacheError
    return value


def _uuid_hex(value: object) -> str:
    if not isinstance(value, UUID):
        raise CacheError
    return value.hex


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CacheError
    return value


def _file_identity(value: os.stat_result) -> _FileIdentity:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_chunk(descriptor: int) -> bytes:
    return os.read(descriptor, _COPY_CHUNK_BYTES)


def _matches_filters(roast: RoastSummary, filters: ArchiveFilters) -> bool:
    if filters.state is not None and roast.state != filters.state:
        return False
    if filters.machine is not None and roast.machine != filters.machine:
        return False
    if filters.roast_at_from is not None and roast.roast_at < filters.roast_at_from:
        return False
    if filters.roast_at_to is not None and roast.roast_at > filters.roast_at_to:
        return False
    if filters.search is None:
        return True
    needle = filters.search.casefold()
    values = (
        roast.title,
        roast.batch_prefix,
        str(roast.batch_number) if roast.batch_number is not None else None,
        roast.operator,
        roast.machine,
        roast.machine_setup,
        *(label.name for label in roast.labels),
    )
    return any(value is not None and needle in value.casefold() for value in values)


def _latest_key(cached: CachedRevision) -> tuple[int, datetime, str]:
    return cached.revision.revision_number, cached.downloaded_at, cached.revision.sha256


def _lru_key(cached: CachedRevision) -> tuple[datetime, str]:
    return cached.downloaded_at, cached.path.as_posix()


def _stats(revisions: tuple[CachedRevision, ...] | list[CachedRevision]) -> CacheStats:
    return CacheStats(
        byte_count=sum(item.revision.byte_size for item in revisions),
        revision_count=len(revisions),
    )


def _namespace_origin(value: object) -> str:
    if not isinstance(value, str) or value == '' or _has_control(value):
        raise CacheError
    return value


def _has_control(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


__all__ = [
    'CACHE_FAILURE',
    'CacheError',
    'CachedPage',
    'CachedRevision',
    'CacheStats',
    'CacheStore',
]
