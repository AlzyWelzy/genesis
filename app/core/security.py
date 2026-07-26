"""Cryptographic primitives: password hashing and JWT signing/verification.

Why this file exists
--------------------
Authentication *policy* (who may log in, what a refresh token means, how
lockouts work) is business logic and belongs in a feature module. The
*mechanics* of hashing a password and signing a token are neither business
logic nor infrastructure — they are pure, stateless helpers that many modules
need. Centralising them here means:

* one place decides the hashing algorithm, so a future migration is one edit;
* signing keys are loaded and cached once instead of read per request;
* no feature module ever imports ``pyjwt`` or ``pwdlib`` directly.

This module deliberately contains **no** authentication flow: no user lookup,
no credential validation, no session handling, no FastAPI dependencies. It has
no imports from :mod:`app.modules` and never will.

Key material
------------
Tokens are signed with Ed25519 (EdDSA) by default. The private key signs; the
public key verifies. Only services that *issue* tokens need the private key —
keep it out of the repository and out of verify-only deployments.
"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

#: Argon2id via pwdlib's recommended construction. Upgrading the parameters
#: later only requires changing this object; `needs_rehash` handles migration.
_password_hash: Final[PasswordHash] = PasswordHash.recommended()

#: Claim value distinguishing token kinds so an access token can never be
#: replayed where a refresh token is expected.
ACCESS_TOKEN_TYPE: Final[str] = "access"
REFRESH_TOKEN_TYPE: Final[str] = "refresh"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage.

    Args:
        password: The plaintext password. Never log or persist this value.

    Returns:
        An encoded Argon2id hash including algorithm parameters and salt.
    """
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash.

    Runs in constant time with respect to the hash contents. Returns ``False``
    rather than raising when the stored hash is malformed or uses an unknown
    algorithm, so callers cannot distinguish "no such user" from "bad hash".

    Args:
        password: The plaintext password supplied by the client.
        password_hash: The previously stored hash.

    Returns:
        ``True`` when the password matches.
    """
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hashes must not leak details
        return False


def verify_and_update_password(
    password: str, password_hash: str
) -> tuple[bool, str | None]:
    """Verify a password and, if its hash is outdated, return an upgraded one.

    A successful verification is the only moment the plaintext is available, so
    it is the only moment a stored hash can be migrated to stronger parameters.
    When the second element is not ``None``, the caller must persist it.

    Args:
        password: The plaintext password supplied by the client.
        password_hash: The previously stored hash.

    Returns:
        A ``(verified, updated_hash)`` pair. ``updated_hash`` is ``None`` when
        the stored hash is already current.
    """
    try:
        return _password_hash.verify_and_update(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hashes must not leak details
        return False, None


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_private_key() -> str:
    """Read and cache the PEM private key used to sign tokens.

    Cached because reading from disk on every token issuance is pure waste and
    the key cannot change without a process restart.

    Raises:
        FileNotFoundError: When the configured path does not exist. This is
            intentional and fatal — a service that cannot sign must not start.
    """
    return _read_pem(settings.jwt.private_key_path)


@lru_cache(maxsize=1)
def load_public_key() -> str:
    """Read and cache the PEM public key used to verify tokens."""
    return _read_pem(settings.jwt.public_key_path)


def _read_pem(path: Path) -> str:
    """Read a PEM file, raising a clear error when it is missing."""
    if not path.is_file():
        raise FileNotFoundError(f"PEM key file not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Token creation and decoding
# ---------------------------------------------------------------------------


def create_token(
    subject: str,
    *,
    token_type: str,
    expires_in: timedelta,
    claims: dict[str, Any] | None = None,
) -> str:
    """Sign a JWT with the standard registered claims.

    The caller decides the subject and lifetime; this function only guarantees
    that ``iss``, ``aud``, ``iat``, ``exp``, ``jti`` and ``type`` are present
    and consistent. It performs no authorization checks.

    Args:
        subject: Value for the ``sub`` claim, typically a user identifier.
        token_type: Discriminator stored in the ``type`` claim.
        expires_in: Lifetime measured from now.
        claims: Extra application claims merged into the payload. Registered
            claim names are overwritten by this function and cannot be spoofed.

    Returns:
        The encoded, signed JWT.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        **(claims or {}),
        "sub": subject,
        "iss": settings.jwt.issuer,
        "aud": settings.jwt.audience,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid4().hex,
        "type": token_type,
    }
    return jwt.encode(payload, load_private_key(), algorithm=settings.jwt.algorithm)


def create_access_token(subject: str, claims: dict[str, Any] | None = None) -> str:
    """Sign a short-lived access token using the configured lifetime."""
    return create_token(
        subject,
        token_type=ACCESS_TOKEN_TYPE,
        expires_in=timedelta(minutes=settings.jwt.access_token_expire_minutes),
        claims=claims,
    )


def create_refresh_token(subject: str, claims: dict[str, Any] | None = None) -> str:
    """Sign a long-lived refresh token using the configured lifetime."""
    return create_token(
        subject,
        token_type=REFRESH_TOKEN_TYPE,
        expires_in=timedelta(days=settings.jwt.refresh_token_expire_days),
        claims=claims,
    )


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    """Verify a token's signature and registered claims, returning its payload.

    Signature, expiry, issuer and audience are always validated. Callers get a
    payload they can trust structurally — but *authorization* (does this
    subject still exist, is the session revoked) remains the caller's job.

    Args:
        token: The encoded JWT.
        expected_type: When given, the ``type`` claim must match exactly.

    Returns:
        The decoded claim set.

    Raises:
        jwt.InvalidTokenError: On any signature, expiry or claim mismatch.
            Callers should translate this into a domain-level error rather
            than leaking the reason to clients.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        load_public_key(),
        algorithms=[settings.jwt.algorithm],
        issuer=settings.jwt.issuer,
        audience=settings.jwt.audience,
        leeway=settings.jwt.leeway_seconds,
    )
    if expected_type is not None and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("Unexpected token type")
    return payload


# TODO: add `kid` header emission and a key-set aware verifier for rotation.
# TODO: add helpers for constant-time comparison of API keys / webhook HMACs.
