"""Concrete storage providers and their factory.

Why this file exists
--------------------
:mod:`app.infrastructure.storage.client` says what storage *is*; this module
says how each backend does it and which one the running process gets. Isolating
the selection in a factory means configuration is the only thing that changes
between a laptop writing to ``var/storage`` and production writing to S3 — no
conditional imports scattered through feature code.

Add a backend by writing a class that satisfies
:class:`~app.infrastructure.storage.client.StorageProvider` and adding one
branch to :func:`get_storage_provider`.
"""

from collections.abc import AsyncIterator

from app.core.config import settings
from app.infrastructure.storage.client import StoredObject


class LocalStorageProvider:
    """Filesystem-backed provider for local development and tests.

    Presigned URLs are simulated: there is no signing authority on a local
    disk, so the returned URL is only meaningful to this process. Never enable
    this provider in a deployed environment — files vanish with the container
    and are invisible to other replicas.
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = root or str(settings.storage.local_root)

    async def upload(
        self, key: str, data: bytes | AsyncIterator[bytes], *, content_type: str
    ) -> StoredObject:
        """Write the object beneath the configured root.

        The resolved path must be verified to stay inside the root; a key
        containing ``../`` would otherwise escape it.
        """
        raise NotImplementedError

    async def download(self, key: str) -> AsyncIterator[bytes]:
        """Stream the file in chunks."""
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        """Unlink the file, ignoring a missing path."""
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        """Whether the path exists beneath the root."""
        raise NotImplementedError

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        """Return a local URL standing in for a signed one."""
        raise NotImplementedError


class S3StorageProvider:
    """S3-compatible provider (AWS S3, MinIO, Cloudflare R2, Spaces).

    Requires an async-capable client; ``boto3`` is blocking and would stall the
    event loop, so use ``aioboto3``/``aiobotocore`` and add it to the project
    dependencies before implementing.
    """

    def __init__(self) -> None:
        self._bucket = settings.storage.bucket
        self._endpoint = settings.storage.endpoint_url

    async def upload(
        self, key: str, data: bytes | AsyncIterator[bytes], *, content_type: str
    ) -> StoredObject:
        """Put an object into the configured bucket."""
        raise NotImplementedError

    async def download(self, key: str) -> AsyncIterator[bytes]:
        """Stream an object body."""
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        """Delete an object."""
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        """HEAD the object and interpret a 404 as absence."""
        raise NotImplementedError

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        """Generate a signed GET URL."""
        raise NotImplementedError


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


# TODO: cache the provider in the lifespan; S3 clients hold connection pools
# and must not be rebuilt per request.
