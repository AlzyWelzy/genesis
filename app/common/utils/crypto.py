"""General-purpose cryptographic helpers.

Why this file exists — and how it differs from :mod:`app.core.security`
-----------------------------------------------------------------------
:mod:`app.core.security` owns *authentication material*: password hashes and
JWTs, configured by settings and tied to the application's key pair. This
module owns stateless primitives with no configuration: random token
generation, constant-time comparison, HMAC signing, content hashing.

The split matters because these helpers are needed by features that have
nothing to do with auth — signing a webhook payload, generating an invite code,
fingerprinting an upload — and they must not import the auth module to do it.

Rules
-----
* Randomness comes from :mod:`secrets`, never :mod:`random`. ``random`` is a
  Mersenne Twister: observe a few outputs and the rest are predictable.
* Comparing secrets uses :func:`constant_time_compare`. A plain ``==`` returns
  early on the first differing byte, and that timing difference is enough to
  recover a token byte by byte.
* Never invent a construction. Hashing a secret with SHA-256 is not a password
  hash and not a MAC.
"""

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator


def generate_token(length: int = 32) -> str:
    """Generate a cryptographically secure URL-safe token.

    For password-reset links, invitations, API keys and CSRF tokens.

    Args:
        length: Bytes of entropy. 32 bytes (256 bits) is the floor for anything
            that grants access. The returned string is longer than this number,
            since base64 expands by roughly 4/3.

    Returns:
        A URL-safe base64 token.
    """
    return secrets.token_urlsafe(length)


def generate_numeric_code(digits: int = 6) -> str:
    """Generate a zero-padded numeric code for one-time verification.

    Six digits is only ~20 bits of entropy, so a code like this is safe **only**
    with a short expiry and a strict attempt limit. Without both it is
    brute-forceable in seconds.

    Uses ``secrets.randbelow`` rather than sampling digits independently, which
    keeps the distribution uniform across the whole range including codes with
    leading zeros.

    Args:
        digits: Length of the code.

    Returns:
        The zero-padded code.

    Raises:
        ValueError: When ``digits`` is not positive.
    """
    if digits <= 0:
        raise ValueError("digits must be positive")
    upper = 10**digits
    return str(secrets.randbelow(upper)).zfill(digits)


def constant_time_compare(a: str | bytes, b: str | bytes) -> bool:
    """Compare two values without leaking their contents through timing.

    Use for every secret comparison: API keys, webhook signatures, one-time
    codes, session identifiers.

    Note that length is still leaked — ``compare_digest`` returns early when the
    inputs differ in length. That is unavoidable and rarely useful to an
    attacker for fixed-length secrets.
    """
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


def hash_token(token: str) -> str:
    """Hash a high-entropy token for storage.

    SHA-256 without a salt is correct **here** and wrong for passwords. The
    distinction is entropy: a 256-bit random token cannot be brute-forced or
    rainbow-tabled, so the slow salted hashing that passwords require would add
    latency and nothing else. Passwords go through
    :func:`app.core.security.hash_password`.

    Storing the hash means a database leak does not hand over usable tokens.

    Args:
        token: The token to hash.

    Returns:
        The hex-encoded digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def sign_payload(payload: bytes, secret: str) -> str:
    """Produce an HMAC-SHA256 signature for an outgoing payload.

    For webhooks the receiver must be able to verify.

    **Sign the exact bytes that are transmitted.** Re-serialising JSON before
    signing changes key order and whitespace, and the signature stops matching —
    the single most common cause of webhook verification failures.

    Args:
        payload: The exact request body bytes.
        secret: The shared signing secret.

    Returns:
        The hex-encoded signature.
    """
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify an HMAC-SHA256 signature in constant time.

    For inbound webhooks. Verify **before** parsing the body: parsing untrusted
    input is itself an attack surface, and an unverified payload has no more
    standing than any other internet traffic.

    Args:
        payload: The exact received body bytes.
        signature: The signature supplied by the sender.
        secret: The shared signing secret.

    Returns:
        ``True`` when the signature matches.
    """
    return constant_time_compare(sign_payload(payload, secret), signature)


async def hash_stream(source: AsyncIterator[bytes]) -> str:
    """Compute the SHA-256 digest of a byte stream.

    For content addressing and deduplication of uploads. Streaming so a large
    file never has to be held in memory.

    Args:
        source: The byte stream. Consumed entirely.

    Returns:
        The hex-encoded digest.
    """
    digest = hashlib.sha256()
    async for block in source:
        digest.update(block)
    return digest.hexdigest()


def hash_bytes(data: bytes) -> str:
    """Compute the SHA-256 digest of an in-memory buffer."""
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "constant_time_compare",
    "generate_numeric_code",
    "generate_token",
    "hash_bytes",
    "hash_stream",
    "hash_token",
    "sign_payload",
    "verify_signature",
]
