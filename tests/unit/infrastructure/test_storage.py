"""Tests for the local storage provider.

The traversal tests are the important ones. A storage key is attacker-supplied
in every upload flow, and a provider that resolves ``../`` writes wherever the
process can write.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.exceptions import NotFoundError
from app.infrastructure.storage.providers import LocalStorageProvider


@pytest.fixture
def provider(storage_root: Path) -> LocalStorageProvider:
    """A provider rooted at a throwaway directory."""
    return LocalStorageProvider(storage_root)


async def _stream(*blocks: bytes) -> AsyncIterator[bytes]:
    """Yield the given blocks as an async stream."""
    for block in blocks:
        yield block


class TestUploadDownload:
    async def test_round_trip_bytes(self, provider: LocalStorageProvider) -> None:
        stored = await provider.upload(
            "docs/a.txt", b"hello world", content_type="text/plain"
        )
        assert stored.key == "docs/a.txt"
        assert stored.size == 11
        assert stored.content_type == "text/plain"

        chunks = [c async for c in provider.download("docs/a.txt")]
        assert b"".join(chunks) == b"hello world"

    async def test_round_trip_stream(self, provider: LocalStorageProvider) -> None:
        stored = await provider.upload(
            "docs/b.bin",
            _stream(b"abc", b"def"),
            content_type="application/octet-stream",
        )
        assert stored.size == 6

        chunks = [c async for c in provider.download("docs/b.bin")]
        assert b"".join(chunks) == b"abcdef"

    async def test_nested_directories_are_created(
        self, provider: LocalStorageProvider, storage_root: Path
    ) -> None:
        await provider.upload("a/b/c/d.txt", b"x", content_type="text/plain")
        assert (storage_root / "a/b/c/d.txt").is_file()

    async def test_upload_overwrites(self, provider: LocalStorageProvider) -> None:
        await provider.upload("k.txt", b"first", content_type="text/plain")
        await provider.upload("k.txt", b"second", content_type="text/plain")

        chunks = [c async for c in provider.download("k.txt")]
        assert b"".join(chunks) == b"second"

    async def test_downloading_a_missing_key_raises(
        self, provider: LocalStorageProvider
    ) -> None:
        with pytest.raises(NotFoundError):
            [c async for c in provider.download("nope.txt")]


class TestExistsDelete:
    async def test_exists(self, provider: LocalStorageProvider) -> None:
        assert await provider.exists("k.txt") is False
        await provider.upload("k.txt", b"x", content_type="text/plain")
        assert await provider.exists("k.txt") is True

    async def test_delete(self, provider: LocalStorageProvider) -> None:
        await provider.upload("k.txt", b"x", content_type="text/plain")
        await provider.delete("k.txt")
        assert await provider.exists("k.txt") is False

    async def test_deleting_a_missing_key_is_not_an_error(
        self, provider: LocalStorageProvider
    ) -> None:
        await provider.delete("never-existed.txt")


class TestPathTraversal:
    @pytest.mark.parametrize(
        "key",
        [
            "../escape.txt",
            "../../etc/passwd",
            "a/../../escape.txt",
            "a/b/../../../escape.txt",
        ],
    )
    async def test_upload_refuses_to_escape_the_root(
        self, provider: LocalStorageProvider, key: str
    ) -> None:
        """A key is attacker-supplied; resolving `../` writes anywhere."""
        with pytest.raises(ValueError, match="escapes"):
            await provider.upload(key, b"pwned", content_type="text/plain")

    async def test_exists_reports_false_for_an_escaping_key(
        self, provider: LocalStorageProvider
    ) -> None:
        """An escaping key does not exist by definition, rather than raising."""
        assert await provider.exists("../../etc/passwd") is False

    async def test_nothing_is_written_outside_the_root(
        self, provider: LocalStorageProvider, storage_root: Path
    ) -> None:
        with pytest.raises(ValueError, match="escapes"):
            await provider.upload("../outside.txt", b"x", content_type="text/plain")
        assert not (storage_root.parent / "outside.txt").exists()


class TestPresignedUrls:
    async def test_signature_round_trip(self, provider: LocalStorageProvider) -> None:
        url = await provider.presigned_url("k.txt", expires_in=300)
        expires = int(url.split("expires=")[1].split("&")[0])
        signature = url.split("signature=")[1]

        assert provider.verify_presigned("k.txt", expires, signature) is True

    async def test_a_tampered_key_fails_verification(
        self, provider: LocalStorageProvider
    ) -> None:
        url = await provider.presigned_url("k.txt", expires_in=300)
        expires = int(url.split("expires=")[1].split("&")[0])
        signature = url.split("signature=")[1]

        assert provider.verify_presigned("other.txt", expires, signature) is False

    async def test_an_expired_url_fails_verification(
        self, provider: LocalStorageProvider
    ) -> None:
        url = await provider.presigned_url("k.txt", expires_in=300)
        signature = url.split("signature=")[1]

        assert provider.verify_presigned("k.txt", 0, signature) is False
