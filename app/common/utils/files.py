"""File and upload helpers.

Why this file exists
--------------------
Handling uploads safely requires the same three checks every time — sanitise
the name, verify the type from the *content* rather than the extension, and cap
the size while streaming. Any feature that reimplements them will skip one, and
each omission is a real vulnerability: path traversal, stored XSS via a
mislabelled SVG, or memory exhaustion from an unbounded body.

These helpers are storage-agnostic; writing bytes is
:mod:`app.infrastructure.storage`'s job.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from app.common.constants import STREAM_CHUNK_SIZE


def sanitize_filename(filename: str) -> str:
    """Strip a client-supplied filename down to a safe basename.

    Removes directory components, null bytes, control characters and leading
    dots. An uploaded name containing ``../../etc/passwd`` must never reach the
    filesystem — and note that even the sanitised result should not be used as
    a storage key on its own; generate a UUID key and keep this only as display
    metadata.

    Args:
        filename: The name supplied by the client. Untrusted.

    Returns:
        A safe basename, never empty.
    """
    raise NotImplementedError


def get_extension(filename: str) -> str:
    """Return the lowercase extension including the dot, or an empty string.

    The extension is a *hint* from the client, not evidence of content type.
    Validate with :func:`detect_content_type` before trusting it.
    """
    return Path(filename).suffix.lower()


def detect_content_type(header: bytes) -> str | None:
    """Infer a MIME type from a file's leading bytes (magic numbers).

    Content sniffing, not extension trust: renaming ``payload.html`` to
    ``avatar.png`` is the standard route to a stored-XSS bug when the file is
    later served back.

    Args:
        header: The first few hundred bytes of the file.

    Returns:
        The detected MIME type, or ``None`` when unrecognised.
    """
    raise NotImplementedError


def is_allowed_content_type(content_type: str, allowed: frozenset[str]) -> bool:
    """Whether a MIME type is in an explicit allow-list.

    Allow-list, never deny-list: a deny-list must anticipate every dangerous
    type forever, and it only takes one gap.
    """
    return content_type.lower() in allowed


async def stream_chunks(
    source: AsyncIterator[bytes],
    *,
    chunk_size: int = STREAM_CHUNK_SIZE,
    max_bytes: int | None = None,
) -> AsyncIterator[bytes]:
    """Re-chunk a byte stream, optionally enforcing a size ceiling.

    Enforcing the limit *while streaming* is the point: checking
    ``Content-Length`` is not enough, since a client can lie about it or send a
    chunked body with no length at all.

    Args:
        source: The incoming byte stream.
        chunk_size: Size of the emitted chunks.
        max_bytes: Abort once this many bytes have been read.

    Yields:
        Byte chunks.

    Raises:
        ValueError: When ``max_bytes`` is exceeded.
    """
    raise NotImplementedError


def human_readable_size(size_bytes: int) -> str:
    """Format a byte count for humans ("2.4 MB"). For logs and admin UIs."""
    raise NotImplementedError
