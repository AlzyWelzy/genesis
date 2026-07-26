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
import hmac
import shutil
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.common.constants import STREAM_CHUNK_SIZE
from app.common.utils.crypto import sign_payload
from app.common.utils.datetime import utc_now
from app.common.utils.files import safe_join
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError
from app.core.logging import get_logger
from app.infrastructure.storage.client import StoredObject

logger = get_logger(__name__)


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
        handle = await asyncio.to_thread(path.open, "wb")
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

        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while block := await asyncio.to_thread(handle.read, STREAM_CHUNK_SIZE):
                yield block
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(self, key: str) -> None:
        """Unlink the file, ignoring a missing path."""
        path = self._path(key)
        await asyncio.to_thread(path.unlink, True)
        await asyncio.to_thread(path.with_suffix(path.suffix + ".meta").unlink, True)

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
        signature = sign_payload(
            f"{key}:{expires_at}".encode(), settings.redis.key_prefix
        )
        return f"/_storage/{key}?expires={expires_at}&signature={signature}"

    @staticmethod
    def verify_presigned(key: str, expires: int, signature: str) -> bool:
        """Verify a signature produced by :meth:`presigned_url`."""
        if expires < int(utc_now().timestamp()):
            return False
        expected = sign_payload(
            f"{key}:{expires}".encode(), settings.redis.key_prefix
        )
        return hmac.compare_digest(expected, signature)

    async def clear(self) -> None:
        """Remove everything beneath the root. For test teardown only."""
        await asyncio.to_thread(shutil.rmtree, self._root, True)


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
        import aioboto3

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
        """HEAD the object, interpreting a 404 as absence."""
        async with self._client() as client:
            try:
                await client.head_object(Bucket=self._bucket, Key=key)
            except Exception:  # noqa: BLE001 - botocore raises a dynamic 404 type
                return False
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
