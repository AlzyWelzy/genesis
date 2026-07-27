"""Tests for the local storage provider.

The traversal tests are the important ones. A storage key is attacker-supplied
in every upload flow, and a provider that resolves ``../`` writes wherever the
process can write.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

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

    async def test_a_tampered_expiry_fails_verification(
        self, provider: LocalStorageProvider
    ) -> None:
        """Otherwise a short-lived link is trivially extended forever."""
        url = await provider.presigned_url("k.txt", expires_in=300)
        expires = int(url.split("expires=")[1].split("&")[0])
        signature = url.split("signature=")[1]

        assert provider.verify_presigned("k.txt", expires + 86_400, signature) is False


class TestUrlSigningSecret:
    """A presigned URL *is* the authorisation; the key behind it must be secret.

    Signing with a service name or namespace is not a weak signature — it is no
    signature, because anyone can compute it and mint a link for any object with
    any expiry.
    """

    def test_the_secret_is_not_a_publicly_known_value(self) -> None:
        from app.core.config import settings
        from app.infrastructure.storage.providers import url_signing_secret

        secret = url_signing_secret()
        assert secret not in {
            settings.redis.key_prefix,
            settings.app.name,
            "genesis",
        }

    def test_an_explicit_key_is_used_when_configured(self) -> None:
        from app.core.config import settings
        from app.infrastructure.storage.providers import url_signing_secret

        original = settings.security.url_signing_key
        object.__setattr__(settings.security, "url_signing_key", SecretStr("chosen"))
        try:
            assert url_signing_secret() == "chosen"
        finally:
            object.__setattr__(settings.security, "url_signing_key", original)

    def test_the_derived_secret_is_not_the_private_key_itself(self) -> None:
        """A signing key must not appear in a value compared against user input."""
        from app.core.security.keys import get_signing_key
        from app.infrastructure.storage.providers import url_signing_secret

        assert url_signing_secret() != get_signing_key().private_pem

    def test_the_derivation_is_stable_across_calls(self) -> None:
        """A secret that changed per call would invalidate every live URL."""
        from app.infrastructure.storage.providers import url_signing_secret

        assert url_signing_secret() == url_signing_secret()

    async def test_a_forged_signature_is_rejected(
        self, provider: LocalStorageProvider
    ) -> None:
        """The concrete attack: signing with the namespace anyone can read."""
        from app.common.utils.crypto import sign_payload
        from app.core.config import settings

        expires = 4_102_444_800  # far future
        forged = sign_payload(
            f"secret-doc.pdf:{expires}".encode(), settings.redis.key_prefix
        )
        assert provider.verify_presigned("secret-doc.pdf", expires, forged) is False


class TestDegenerateKeys:
    """Keys that name the root rather than an object.

    ``""``, ``"."`` and ``"a/.."`` all resolve to the storage root. That is
    *inside* the root, so the traversal check passes — and the provider then
    tries to write to, or unlink, a directory. The result was an uncaught
    ``IsADirectoryError`` and a 500 for what is really a malformed key.

    Reachable as soon as a feature builds a key from user input and any
    component of it comes back empty.
    """

    @pytest.mark.parametrize("key", ["", ".", "a/..", "./", "a/b/../.."])
    async def test_a_key_naming_the_root_is_refused(
        self, provider: LocalStorageProvider, key: str
    ) -> None:
        with pytest.raises(ValueError, match="does not name an object"):
            await provider.upload(key, b"x", content_type="text/plain")

    async def test_deleting_with_a_root_key_does_not_touch_the_root(
        self, provider: LocalStorageProvider, storage_root: Path
    ) -> None:
        """The dangerous direction: unlinking the store itself."""
        with pytest.raises(ValueError, match="does not name an object"):
            await provider.delete("")

        # Checked off the event loop: the lint rule that flags blocking pathlib
        # calls in async functions is right, and it applies to tests too.
        assert await asyncio.to_thread(storage_root.is_dir)

    async def test_exists_reports_false_rather_than_raising(
        self, provider: LocalStorageProvider
    ) -> None:
        """``exists`` already treats an unusable key as absence; keep that."""
        assert await provider.exists("") is False

    async def test_an_ordinary_key_still_works(
        self, provider: LocalStorageProvider
    ) -> None:
        stored = await provider.upload("a/b.txt", b"x", content_type="text/plain")
        assert stored.key == "a/b.txt"
