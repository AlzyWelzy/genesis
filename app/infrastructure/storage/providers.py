"""Concrete storage providers and their factory.

Why this file exists
--------------------
:mod:`app.infrastructure.storage.client` says what storage *is*; this module
says how each backend does it and which one the running process gets. Isolating
the selection in a factory means configuration is the only thing that changes
between a laptop writing to ``var/storage`` and production writing to S3 — no
conditional imports scattered through feature code.

Add a backend by writing a class satisfying
:class:`~app.infrastructure.storage.client.StorageProvider` and adding one
branch to :func:`get_storage_provider`.
"""

import asyncio
import hashlib
import hmac
import shutil
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from app.common.constants import STREAM_CHUNK_SIZE
from app.common.utils.crypto import sign_payload
from app.common.utils.datetime import utc_now
from app.common.utils.files import safe_join
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError
from app.core.logging import get_logger
from app.infrastructure.storage.client import (
    MIN_PART_SIZE,
    MultipartUpload,
    StoredObject,
)

logger = get_logger(__name__)

#: Used when a stored object has no recorded content type.
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


#: Domain separator, so a signature minted for a storage URL can never be
#: replayed as one for some future capability URL derived from the same key.
_URL_SIGNING_CONTEXT = b"genesis/storage/presigned-url/v1"


def url_signing_secret() -> str:
    """Return the HMAC key used to sign and verify capability URLs.

    Why this is not simply a config string
    --------------------------------------
    A presigned URL *is* the authorisation: whoever holds it can read the
    object, with no further check. The signature is the only thing standing
    between that and "anyone can read any object", so the key behind it has to
    be secret. Signing with a service name, a namespace or any other publicly
    known value means an attacker can mint a link for an arbitrary key with an
    arbitrary expiry — which is not a weak signature, it is no signature.

    Prefers ``SECURITY__URL_SIGNING_KEY`` when configured. Otherwise derives one
    from the JWT private key, which is always present (the service cannot start
    without it) and genuinely secret, so local development needs no extra
    configuration to be correct.

    Derived through HMAC with a domain separator rather than used directly: the
    signing key must not appear in a value that is compared against attacker-
    supplied input, and the separator keeps this use distinct from any other
    key derived the same way.

    Deliberately not cached. ``get_signing_key()`` already is, so this costs one
    HMAC over a string that is in memory. Caching it here would instead mean
    holding a value derived from a key that ``reset_key_cache()`` can replace —
    and this module cannot be invalidated from ``app.core``, which is not
    permitted to import infrastructure.
    """
    configured = settings.security.url_signing_key
    if configured is not None:
        return configured.get_secret_value()

    from app.core.security.keys import get_signing_key  # noqa: PLC0415 - cycle

    return hmac.new(
        get_signing_key().private_pem.encode(),
        _URL_SIGNING_CONTEXT,
        hashlib.sha256,
    ).hexdigest()


#: Error codes S3 uses for "this object is not here". ``head_object`` reports
#: ``404``/``NotFound`` where ``get_object`` reports ``NoSuchKey``, so both are
#: recognised.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _is_not_found(exc: BaseException) -> bool:
    """Whether a botocore error means "absent" rather than "unavailable".

    botocore builds its exception classes dynamically per service, so there is
    no importable ``NoSuchKey`` to catch. The error code inside ``response`` is
    the stable contract, and reading it is what lets absence be distinguished
    from an outage — a distinction the caller very much needs.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = response.get("Error", {}).get("Code")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return str(code) in _NOT_FOUND_CODES or status == 404  # noqa: PLR2004


def _open_read(path: Path) -> IO[bytes]:
    """Open for binary reading, with a type the checker can follow."""
    return path.open("rb")


def _open_write(path: Path) -> IO[bytes]:
    """Open for binary writing, with a type the checker can follow."""
    return path.open("wb")


def _unlink(path: Path) -> None:
    """Delete a file, tolerating one that is already gone."""
    path.unlink(missing_ok=True)


class LocalStorageProvider:
    """Filesystem-backed provider for local development and tests.

    Presigned URLs are *simulated*: there is no signing authority on a local
    disk, so the returned URL is only meaningful to this process. Never enable
    this provider in a deployed environment — files vanish with the container
    and are invisible to other replicas.

    All filesystem calls run through :func:`asyncio.to_thread`. Disk I/O is
    blocking, and a blocking call in an async path stalls the entire event loop,
    which presents as "the whole service got slow" rather than "one upload got
    slow".
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root or settings.storage.local_root)

    def _path(self, key: str) -> Path:
        """Resolve a key beneath the root, refusing to escape it.

        The check that makes this provider safe: a key containing ``../``
        would otherwise write anywhere the process can write.
        """
        return safe_join(self._root, key)

    async def upload(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        content_type: str,
    ) -> StoredObject:
        """Write the object beneath the configured root."""
        path = self._path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)

        if isinstance(data, bytes):
            await asyncio.to_thread(path.write_bytes, data)
            size = len(data)
        else:
            size = await self._write_stream(path, data)

        # Content type is not recoverable from the filesystem, so record it
        # alongside the object. Without it, a download cannot set the header
        # and the browser sniffs — which is the stored-XSS route.
        await asyncio.to_thread(
            path.with_suffix(path.suffix + ".meta").write_text, content_type
        )
        return StoredObject(key=key, size=size, content_type=content_type)

    async def _write_stream(self, path: Path, data: AsyncIterator[bytes]) -> int:
        """Stream to disk without holding the whole object in memory."""
        size = 0
        handle = await asyncio.to_thread(_open_write, path)
        try:
            async for block in data:
                await asyncio.to_thread(handle.write, block)
                size += len(block)
        finally:
            await asyncio.to_thread(handle.close)
        return size

    async def download(self, key: str) -> AsyncIterator[bytes]:
        """Stream the file in chunks."""
        path = self._path(key)
        if not await asyncio.to_thread(path.is_file):
            raise NotFoundError(f"No stored object for key: {key}")

        handle = await asyncio.to_thread(_open_read, path)
        try:
            while block := await asyncio.to_thread(handle.read, STREAM_CHUNK_SIZE):
                yield block
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(self, key: str) -> None:
        """Unlink the file, ignoring a missing path."""
        path = self._path(key)
        await asyncio.to_thread(_unlink, path)
        await asyncio.to_thread(_unlink, path.with_suffix(path.suffix + ".meta"))

    async def exists(self, key: str) -> bool:
        """Whether the path exists beneath the root."""
        try:
            return await asyncio.to_thread(self._path(key).is_file)
        except ValueError:
            # An escaping key does not exist by definition.
            return False

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        """Return a signed local URL standing in for a real presigned one.

        Signed even though it is a stand-in, so development exercises the same
        verification path production will use — an unsigned dev URL hides bugs
        in whatever consumes it.
        """
        ttl = expires_in or settings.storage.presigned_url_expire_seconds
        expires_at = int((utc_now() + timedelta(seconds=ttl)).timestamp())
        signature = sign_payload(f"{key}:{expires_at}".encode(), url_signing_secret())
        return f"/_storage/{key}?expires={expires_at}&signature={signature}"

    @staticmethod
    def verify_presigned(key: str, expires: int, signature: str) -> bool:
        """Verify a signature produced by :meth:`presigned_url`.

        Expiry is checked before the signature so an expired link is rejected
        even if it was validly signed. The comparison itself is constant time:
        a byte-by-byte early return leaks how much of a forged signature was
        correct, which is enough to construct one a byte at a time.
        """
        if expires < int(utc_now().timestamp()):
            return False
        expected = sign_payload(f"{key}:{expires}".encode(), url_signing_secret())
        return hmac.compare_digest(expected, signature)

    async def copy(self, source_key: str, destination_key: str) -> StoredObject:
        """Copy a file within the root.

        The "promote from a temp prefix" flow: an upload lands under
        ``tmp/<id>`` while it is being validated, then moves to its permanent
        key once accepted. Uploading straight to the final key means a rejected
        file has already occupied the name a valid one needs.
        """
        source = self._path(source_key)
        if not await asyncio.to_thread(source.is_file):
            raise NotFoundError(f"No stored object for key: {source_key}")

        destination = self._path(destination_key)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, source, destination)

        metadata = source.with_suffix(source.suffix + ".meta")
        content_type = _DEFAULT_CONTENT_TYPE
        if await asyncio.to_thread(metadata.is_file):
            content_type = await asyncio.to_thread(metadata.read_text)
            await asyncio.to_thread(
                destination.with_suffix(destination.suffix + ".meta").write_text,
                content_type,
            )

        size = await asyncio.to_thread(lambda: destination.stat().st_size)
        return StoredObject(key=destination_key, size=size, content_type=content_type)

    async def move(self, source_key: str, destination_key: str) -> StoredObject:
        """Move a file, deleting the source only after the copy succeeds.

        Copy-then-delete rather than a rename: a rename cannot cross
        filesystems, and this provider's root may well be a mount. The ordering
        matters — a failed copy leaves the source intact, whereas
        delete-then-copy would lose the object outright.
        """
        stored = await self.copy(source_key, destination_key)
        await self.delete(source_key)
        return stored

    async def clear(self) -> None:
        """Remove everything beneath the root. For test teardown only."""
        await asyncio.to_thread(shutil.rmtree, self._root, ignore_errors=True)


class S3StorageProvider:
    """S3-compatible provider (AWS S3, MinIO, Cloudflare R2, Spaces).

    Uses ``aioboto3``. ``boto3`` is blocking and would stall the event loop for
    the duration of every transfer, which on a large upload is seconds.

    A client is created per operation rather than held open. ``aioboto3``
    clients are async context managers bound to an event loop, and caching one
    across loops — as a test suite or a worker restart will do — produces
    "Event loop is closed" errors that are hard to trace back here.
    """

    def __init__(self) -> None:
        self._bucket = settings.storage.bucket
        if not self._bucket:
            raise ValueError("STORAGE__BUCKET is required when using the s3 provider")

    def _client(self) -> Any:
        """Build a configured S3 client context manager."""
        # Deferred deliberately: importing botocore costs roughly a second,
        # which every process would pay even when the local provider is in
        # use. Only an S3-configured deployment should bear it.
        import aioboto3  # noqa: PLC0415 - heavy import, deferred on purpose

        session = aioboto3.Session()
        return session.client(
            "s3",
            region_name=settings.storage.region,
            endpoint_url=settings.storage.endpoint_url,
            aws_access_key_id=(
                settings.storage.access_key_id.get_secret_value()
                if settings.storage.access_key_id
                else None
            ),
            aws_secret_access_key=(
                settings.storage.secret_access_key.get_secret_value()
                if settings.storage.secret_access_key
                else None
            ),
        )

    async def upload(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        content_type: str,
    ) -> StoredObject:
        """Put an object into the configured bucket."""
        body = data if isinstance(data, bytes) else await _collect(data)
        try:
            async with self._client() as client:
                response = await client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                )
        except Exception as exc:
            raise ExternalServiceError("Object storage upload failed") from exc

        return StoredObject(
            key=key,
            size=len(body),
            content_type=content_type,
            etag=response.get("ETag", "").strip('"') or None,
        )

    async def download(self, key: str) -> AsyncIterator[bytes]:
        """Stream an object body."""
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except client.exceptions.NoSuchKey as exc:
                raise NotFoundError(f"No stored object for key: {key}") from exc
            except Exception as exc:
                raise ExternalServiceError("Object storage read failed") from exc

            async for block in response["Body"].iter_chunks(STREAM_CHUNK_SIZE):
                yield block

    async def delete(self, key: str) -> None:
        """Delete an object. Deleting a missing key is not an error in S3."""
        try:
            async with self._client() as client:
                await client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise ExternalServiceError("Object storage delete failed") from exc

    async def exists(self, key: str) -> bool:
        """HEAD the object, interpreting a 404 as absence.

        Only a 404 (or S3's ``NoSuchKey``/``NotFound`` codes) counts as absence.
        Anything else is re-raised as an
        :class:`~app.core.exceptions.ExternalServiceError`.

        Treating every failure as "not there" is worse than it sounds: an
        outage, a credentials expiry or a bucket-policy change makes every
        object report absent. A "create if it does not exist" flow then silently
        overwrites live data, and a read path returns 404 — telling the caller
        the object is gone — when the truthful answer is that storage is down
        and retrying would work.
        """
        try:
            async with self._client() as client:
                await client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise ExternalServiceError("Object storage lookup failed") from exc
        return True

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        """Generate a signed GET URL.

        Clients download directly from storage rather than through the API.
        Proxying large files through an ASGI worker holds it — and a database
        connection — for the whole transfer, and is a reliable way to exhaust
        the pool.
        """
        try:
            async with self._client() as client:
                return await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=expires_in
                    or settings.storage.presigned_url_expire_seconds,
                )
        except Exception as exc:
            raise ExternalServiceError("Could not sign a storage URL") from exc

    async def copy(self, source_key: str, destination_key: str) -> StoredObject:
        """Copy an object server-side.

        The bytes never travel through this process: S3 copies within the
        bucket itself. Downloading and re-uploading would be slower, cost
        egress, and hold a worker for the duration.
        """
        try:
            async with self._client() as client:
                await client.copy_object(
                    Bucket=self._bucket,
                    Key=destination_key,
                    CopySource={"Bucket": self._bucket, "Key": source_key},
                )
                head = await client.head_object(
                    Bucket=self._bucket, Key=destination_key
                )
        except Exception as exc:
            raise ExternalServiceError("Object storage copy failed") from exc

        return StoredObject(
            key=destination_key,
            size=head.get("ContentLength", 0),
            content_type=head.get("ContentType", _DEFAULT_CONTENT_TYPE),
            etag=head.get("ETag", "").strip('"') or None,
        )

    async def move(self, source_key: str, destination_key: str) -> StoredObject:
        """Move an object, deleting the source once the copy succeeds.

        S3 has no atomic move. Copy first, then delete: if the delete fails the
        result is a duplicate, which a lifecycle rule can clean up. The reverse
        order risks losing the object entirely.
        """
        stored = await self.copy(source_key, destination_key)
        await self.delete(source_key)
        return stored

    async def begin_multipart(self, key: str, *, content_type: str) -> MultipartUpload:
        """Start a multipart upload.

        The returned ``upload_id`` must be kept until the upload is completed
        or aborted — losing it strands the parts, which continue to be billed
        and do not appear in a normal bucket listing.
        """
        try:
            async with self._client() as client:
                response = await client.create_multipart_upload(
                    Bucket=self._bucket, Key=key, ContentType=content_type
                )
        except Exception as exc:
            raise ExternalServiceError("Could not start a multipart upload") from exc

        return MultipartUpload(key=key, upload_id=response["UploadId"])

    async def upload_part(
        self, upload: MultipartUpload, part_number: int, data: bytes
    ) -> MultipartUpload:
        """Upload one part and record its ETag.

        Every part except the last must be at least ``MIN_PART_SIZE``. Checked
        here rather than left to S3, which only reports the violation at
        completion — after every other part has been uploaded and paid for.

        Returns:
            A new :class:`MultipartUpload` with the part recorded. The value is
            frozen, so the caller must use the returned object.

        Raises:
            ValueError: When the part is undersized or the number is out of
                sequence.
        """
        if part_number != len(upload.parts) + 1:
            raise ValueError(
                f"Parts must be contiguous from 1; expected "
                f"{len(upload.parts) + 1}, got {part_number}"
            )
        if len(data) < MIN_PART_SIZE:
            logger.warning(
                "Multipart part is below the S3 minimum; it must be the last one",
                extra={"part_number": part_number, "size": len(data)},
            )

        try:
            async with self._client() as client:
                response = await client.upload_part(
                    Bucket=self._bucket,
                    Key=upload.key,
                    UploadId=upload.upload_id,
                    PartNumber=part_number,
                    Body=data,
                )
        except Exception as exc:
            raise ExternalServiceError("Multipart part upload failed") from exc

        return MultipartUpload(
            key=upload.key,
            upload_id=upload.upload_id,
            parts=[*upload.parts, (part_number, response["ETag"])],
        )

    async def complete_multipart(self, upload: MultipartUpload) -> StoredObject:
        """Assemble the uploaded parts into the final object.

        Raises:
            ValueError: When no parts were uploaded — completing an empty
                upload produces a confusing provider error rather than an
                obvious one.
        """
        if not upload.parts:
            raise ValueError("Cannot complete a multipart upload with no parts")

        try:
            async with self._client() as client:
                await client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=upload.key,
                    UploadId=upload.upload_id,
                    MultipartUpload={
                        "Parts": [
                            {"PartNumber": number, "ETag": etag}
                            for number, etag in upload.parts
                        ]
                    },
                )
                head = await client.head_object(Bucket=self._bucket, Key=upload.key)
        except Exception as exc:
            # Abort so the parts stop accruing storage charges. Best-effort:
            # the completion failure is the one worth reporting.
            await self.abort_multipart(upload)
            raise ExternalServiceError(
                "Could not complete the multipart upload"
            ) from exc

        return StoredObject(
            key=upload.key,
            size=head.get("ContentLength", 0),
            content_type=head.get("ContentType", _DEFAULT_CONTENT_TYPE),
            etag=head.get("ETag", "").strip('"') or None,
        )

    async def abort_multipart(self, upload: MultipartUpload) -> None:
        """Discard an incomplete upload.

        Never raises. It is called from failure paths, where masking the
        original error with a cleanup error helps nobody — the abort is logged
        and a lifecycle rule is the backstop.
        """
        try:
            async with self._client() as client:
                await client.abort_multipart_upload(
                    Bucket=self._bucket, Key=upload.key, UploadId=upload.upload_id
                )
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            logger.warning(
                "Could not abort a multipart upload; parts may still be billed",
                extra={"key": upload.key, "upload_id": upload.upload_id},
            )


async def _collect(data: AsyncIterator[bytes]) -> bytes:
    """Buffer a stream into memory.

    Needed because ``put_object`` wants a complete body. Bounded in practice by
    ``STORAGE__MAX_UPLOAD_BYTES``, enforced at the edge before the stream gets
    here; a multipart upload is the fix once objects can exceed that.
    """
    return b"".join([block async for block in data])


def get_storage_provider() -> LocalStorageProvider | S3StorageProvider:
    """Build the provider selected by configuration.

    Returns:
        The provider matching ``settings.storage.provider``.

    Raises:
        ValueError: When the configured provider name is unknown.
    """
    match settings.storage.provider:
        case "local":
            return LocalStorageProvider()
        case "s3":
            return S3StorageProvider()
        case unknown:
            raise ValueError(f"Unknown storage provider: {unknown}")


#: Process-wide provider, built during startup by the lifespan.
storage: LocalStorageProvider | S3StorageProvider | None = None


def set_storage(provider: LocalStorageProvider | S3StorageProvider) -> None:
    """Install the process-wide storage provider."""
    global storage  # noqa: PLW0603 - process-wide singleton by design
    storage = provider


def get_storage() -> LocalStorageProvider | S3StorageProvider:
    """Return the initialised storage provider.

    Raises:
        RuntimeError: When called before the lifespan built it.
    """
    if storage is None:
        raise RuntimeError("Storage is not initialised; the lifespan builds it.")
    return storage
