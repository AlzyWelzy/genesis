"""Object storage contract.

Why this file exists
--------------------
A feature that stores an avatar or exports a report should depend on "somewhere
I can put bytes and get a URL back", not on ``boto3``. Defining that contract
here means the local filesystem, S3, MinIO, R2 and Spaces are interchangeable,
and tests never need network access or credentials.

This module declares *what* storage does. :mod:`app.infrastructure.storage.providers`
implements it and decides which implementation the configuration selects.

Contract notes
--------------
* Keys are opaque paths (``tenants/{id}/avatars/{uuid}.png``). Never derive a
  key from user-supplied text without sanitising it — path traversal in a key
  is as real as in a filename.
* Downloads should be served with presigned URLs, not proxied through the API;
  streaming large files through an ASGI worker is a reliable way to exhaust it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Metadata describing a stored object.

    Attributes:
        key: The storage key the object was written under.
        size: Size in bytes.
        content_type: MIME type recorded at upload time.
        etag: Provider-supplied integrity/version marker, when available.
    """

    key: str
    size: int
    content_type: str
    etag: str | None = None


class StorageProvider(Protocol):
    """Object storage operations every provider must support."""

    async def upload(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        content_type: str,
    ) -> StoredObject:
        """Write an object, overwriting any existing object at ``key``."""
        ...

    async def download(self, key: str) -> AsyncIterator[bytes]:
        """Stream an object's contents.

        Streaming rather than returning bytes so a large file never has to fit
        in the worker's memory.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove an object. Deleting a missing key is not an error."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether an object exists at ``key``."""
        ...

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        """Return a time-limited URL granting direct read access."""
        ...


# TODO: add `copy`/`move` for the "promote from a temp upload prefix" flow.
# TODO: add a multipart upload API once files can exceed a single request body.
