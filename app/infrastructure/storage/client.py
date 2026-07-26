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
from dataclasses import dataclass, field
from typing import Final, Protocol


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

    async def copy(self, source_key: str, destination_key: str) -> StoredObject:
        """Copy an object within the same store."""
        ...

    async def move(self, source_key: str, destination_key: str) -> StoredObject:
        """Move an object, deleting the source once the copy succeeds."""
        ...


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    """A multipart upload in progress.

    Why multipart exists
    --------------------
    A single ``PUT`` has to complete in one connection. For a large object that
    means a failure at 95% starts again from zero, and the whole body has to be
    held somewhere while it transfers. Multipart splits it into independently
    retryable chunks that can also upload concurrently.

    S3 requires parts of at least 5 MiB (except the last). Below that, a plain
    upload is simpler and cheaper — there is no reason to reach for this until
    objects genuinely exceed a comfortable single request.

    **An abandoned multipart upload still costs money.** Parts are stored and
    billed until the upload is completed or aborted, and they are invisible in a
    normal bucket listing. Always :meth:`abort` on failure, and set a lifecycle
    rule to clean up incomplete uploads after a few days as a backstop.

    Attributes:
        key: Storage key the finished object will occupy.
        upload_id: Provider-assigned identifier for this upload.
        parts: Completed parts, in order, as ``(part_number, etag)``.
    """

    key: str
    upload_id: str
    parts: list[tuple[int, str]] = field(default_factory=list)


#: Minimum size for a non-final part, imposed by S3. Enforced locally so an
#: undersized part fails immediately rather than at completion, by which point
#: every other part has already been uploaded and paid for.
MIN_PART_SIZE: Final[int] = 5 * 1024 * 1024


class MultipartCapable(Protocol):
    """Optional multipart interface, implemented by providers that support it.

    Separate from :class:`StorageProvider` so the local filesystem provider is
    not forced to fake an API it has no need for. Callers check with
    ``isinstance(provider, MultipartCapable)``.
    """

    async def begin_multipart(self, key: str, *, content_type: str) -> MultipartUpload:
        """Start a multipart upload."""
        ...

    async def upload_part(
        self, upload: MultipartUpload, part_number: int, data: bytes
    ) -> MultipartUpload:
        """Upload one part. Part numbers start at 1 and must be contiguous."""
        ...

    async def complete_multipart(self, upload: MultipartUpload) -> StoredObject:
        """Assemble the uploaded parts into the final object."""
        ...

    async def abort_multipart(self, upload: MultipartUpload) -> None:
        """Discard an incomplete upload and stop paying for its parts."""
        ...
