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

import re
import unicodedata
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

from app.common.constants import STREAM_CHUNK_SIZE

#: Decimal units (1 kB = 1000 B), matching what operating systems and cloud
#: consoles report. Binary units would show a different number for the same
#: file than the customer sees in their own storage browser.
_SIZE_STEP = 1000

_UNSAFE_FILENAME_CHARS: Final[re.Pattern[str]] = re.compile(r"[^\w.\- ]+")
_COLLAPSE_DOTS: Final[re.Pattern[str]] = re.compile(r"\.{2,}")

#: Leading bytes identifying common formats. Ordered longest-first so a longer
#: signature is not shadowed by a shorter prefix.
_MAGIC_NUMBERS: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"BM", "image/bmp"),
)

#: Fallback used when nothing else matches and the content is not text.
_DEFAULT_CONTENT_TYPE: Final[str] = "application/octet-stream"


def sanitize_filename(filename: str) -> str:
    """Strip a client-supplied filename down to a safe basename.

    Removes directory components, null bytes, control characters and leading
    dots. An uploaded name containing ``../../etc/passwd`` must never reach the
    filesystem.

    Note that even the sanitised result should not be used as a storage key on
    its own — generate a UUID key and keep this only as display metadata.

    Args:
        filename: The name supplied by the client. Untrusted.

    Returns:
        A safe basename, never empty.
    """
    # PurePath handles both separators; take the final component only. Windows
    # clients send backslashes, which POSIX path handling would not split.
    basename = filename.replace("\\", "/").split("/")[-1]

    # Drop control characters and anything Unicode classes as a "format" or
    # "other" character. This includes U+202E RIGHT-TO-LEFT OVERRIDE, which
    # reverses the display of everything after it: a file named
    # "photo\\u202egnp.exe" renders as "photo.png" in a file listing while
    # still executing as .exe.
    cleaned = "".join(
        char for char in basename if unicodedata.category(char)[0] not in ("C", "Z")
    )
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    cleaned = _COLLAPSE_DOTS.sub(".", cleaned).strip(". _")

    return cleaned or "unnamed"


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
        The detected MIME type, or ``None`` when unrecognised. ``None`` means
        "unknown", so the caller must reject rather than assume.
    """
    for signature, content_type in _MAGIC_NUMBERS:
        if header.startswith(signature):
            return content_type

    # SVG and other XML formats have no fixed magic number. Detect them
    # explicitly because an SVG is executable in a browser — it can carry
    # <script> — and must never be served from the application's own origin.
    prefix = header[:512].lstrip()
    if prefix.startswith((b"<?xml", b"<svg")) and b"<svg" in header[:512].lower():
        return "image/svg+xml"

    return None


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

    Enforcing the limit *while streaming* is the point. Checking
    ``Content-Length`` is not enough: a client can lie about it, or send a
    chunked body with no length at all.

    Args:
        source: The incoming byte stream.
        chunk_size: Size of the emitted chunks.
        max_bytes: Abort once this many bytes have been read.

    Yields:
        Byte chunks of at most ``chunk_size``.

    Raises:
        ValueError: When ``max_bytes`` is exceeded. Raised as soon as the limit
            is crossed, so an oversized upload is cut off rather than buffered.
    """
    buffer = bytearray()
    total = 0

    async for block in source:
        total += len(block)
        if max_bytes is not None and total > max_bytes:
            raise ValueError(f"Stream exceeded the maximum of {max_bytes} bytes")

        buffer.extend(block)
        while len(buffer) >= chunk_size:
            yield bytes(buffer[:chunk_size])
            del buffer[:chunk_size]

    if buffer:
        yield bytes(buffer)


def human_readable_size(size_bytes: int) -> str:
    """Format a byte count for humans, e.g. ``"2.4 MB"``.

    For logs and admin UIs. Uses decimal units (1 kB = 1000 B) to match what
    operating systems and cloud consoles report.

    Args:
        size_bytes: The size.

    Returns:
        The formatted size.
    """
    if size_bytes < _SIZE_STEP:
        return f"{size_bytes} B"

    size = float(size_bytes)
    for unit in ("kB", "MB", "GB", "TB"):
        size /= _SIZE_STEP
        if size < _SIZE_STEP:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def safe_join(root: Path, key: str) -> Path:
    """Resolve a storage key beneath ``root``, refusing to escape it.

    The check that makes a filesystem-backed store safe. A key containing
    ``../`` resolves outside the root, which turns "upload a file" into "write
    anywhere the process can write".

    Args:
        root: The directory everything must stay inside.
        key: The relative storage key.

    Returns:
        The resolved absolute path.

    Raises:
        ValueError: When the resolved path escapes ``root``.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / key).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"Path escapes the storage root: {key}")
    # `""`, `"."` and `"a/.."` all resolve to the root itself. That is *inside*
    # the root, so the check above passes — and the caller then tries to write
    # to, or unlink, a directory. The result is an uncaught `IsADirectoryError`
    # and a 500 for what is really a malformed key. A storage key names an
    # object; it can never name the root.
    if candidate == root_resolved:
        raise ValueError(f"Storage key does not name an object: {key!r}")
    return candidate
