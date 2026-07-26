"""Signing key material and rotation.

Why this file exists
--------------------
A signing key is the single most sensitive thing this application holds:
whoever has it can mint a token for any user. Two operational realities follow,
and both must be designed for before the first token is issued.

**Keys must be rotatable.** Naively swapping the key file invalidates every
outstanding token the instant it happens — every user logged out, every mobile
client erroring, mid-deploy. The fix is to keep verifying with retired keys for
as long as tokens signed by them can still be alive, while signing only with
the active one. That requires tokens to say which key signed them, which is
what the ``kid`` header is for.

**Keys must be loaded once.** Reading a PEM and parsing it per request is pure
waste, and the key cannot change without a process restart.

This module owns both concerns and nothing else: no tokens, no passwords, no
authentication. See ``docs/architecture/security.md``.

Rotation procedure
------------------
1. Generate a new pair, e.g. ``keys/private.pem`` with kid ``2026-q1``.
2. Move the *previous public* key to ``keys/retired/<old-kid>.pem``.
3. Deploy. New tokens are signed with the new key; old tokens still verify
   against the retired one, and both are published in JWKS.
4. After the longest token lifetime has elapsed, delete the retired key.

Skipping step 2 is the flag-day failure described above.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SigningKey:
    """The active key pair used to sign tokens.

    Attributes:
        key_id: Published as the ``kid`` header so verifiers can select the
            matching public key without trial decryption.
        private_pem: PEM-encoded private key. Never log or serialise this.
        public_pem: PEM-encoded public key. Safe to publish.
    """

    key_id: str
    private_pem: str
    public_pem: str


@dataclass(frozen=True, slots=True)
class VerificationKey:
    """A public key accepted when verifying tokens.

    Includes the active key and every retired key still within its wind-down
    window.
    """

    key_id: str
    public_pem: str


def _read_pem(path: Path) -> str:
    """Read a PEM file, failing clearly when it is missing.

    Raises:
        FileNotFoundError: When the path does not exist. Fatal and intentional:
            a service that cannot sign or verify must not start.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"PEM key file not found: {path}. "
            "Generate one with: uv run python scripts/generate_keys.py"
        )
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_signing_key() -> SigningKey:
    """Return the active signing key, reading it from disk on first use.

    Cached for the process lifetime.
    """
    return SigningKey(
        key_id=settings.jwt.active_key_id,
        private_pem=_read_pem(settings.jwt.private_key_path),
        public_pem=_read_pem(settings.jwt.public_key_path),
    )


@lru_cache(maxsize=1)
def get_verification_keys() -> dict[str, VerificationKey]:
    """Return every public key accepted for verification, keyed by ``kid``.

    The active key plus every ``<kid>.pem`` in the retired directory. A missing
    retired directory is normal (no rotation has happened yet) and not an error.

    Returns:
        Mapping of key ID to verification key.
    """
    active = get_signing_key()
    keys: dict[str, VerificationKey] = {
        active.key_id: VerificationKey(active.key_id, active.public_pem)
    }

    retired_dir = settings.jwt.retired_keys_dir
    if retired_dir.is_dir():
        for path in sorted(retired_dir.glob("*.pem")):
            key_id = path.stem
            if key_id in keys:
                logger.warning("Retired key shadows the active kid: %s", key_id)
                continue
            keys[key_id] = VerificationKey(key_id, path.read_text(encoding="utf-8"))

    return keys


def get_verification_key(key_id: str | None) -> VerificationKey:
    """Select the public key for a given ``kid``.

    Args:
        key_id: The ``kid`` header from the token. ``None`` for tokens issued
            before rotation support existed, which fall back to the active key.

    Returns:
        The matching verification key.

    Raises:
        KeyError: When the ``kid`` is unknown. Treat as an invalid token —
            never fall back to trying every key, which would let an attacker
            probe for a key that validates.
    """
    keys = get_verification_keys()
    if key_id is None:
        return keys[settings.jwt.active_key_id]
    return keys[key_id]


def reset_key_cache() -> None:
    """Clear the cached keys.

    For tests that generate throwaway key pairs, and for a future SIGHUP-driven
    reload. Not safe to call while requests are in flight.
    """
    get_signing_key.cache_clear()
    get_verification_keys.cache_clear()
