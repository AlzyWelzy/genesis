"""JSON Web Key Set publication.

Why this file exists
--------------------
Once more than one thing verifies these tokens — an API gateway, a second
service, a mobile SDK, an external partner — distributing the public key by
copying a PEM into each of them does not scale and, worse, makes rotation a
coordinated manual operation across systems you may not control.

JWKS (RFC 7517) is the standard answer: publish the public keys at a well-known
URL, let verifiers fetch and cache them, and key rotation becomes something
they pick up on their own. Publishing *both* the active and retired keys is
what allows tokens signed before a rotation to keep verifying afterwards.

Safety
------
This endpoint exposes public keys only. That is by design and is not a leak —
a public key is what verifiers need and cannot be used to sign anything. The
private key is never loaded by this module.

Served by :mod:`app.system.router` at ``/.well-known/jwks.json``.
"""

import base64
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from app.core.config import settings
from app.core.security.keys import get_verification_keys


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding, as JWK requires."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwk_for_pem(key_id: str, public_pem: str) -> dict[str, Any]:
    """Convert one PEM public key into a JWK.

    Args:
        key_id: Value published as ``kid``; must match the ``kid`` header that
            tokens signed with this key carry, or verifiers cannot pair them.
        public_pem: The PEM-encoded public key.

    Returns:
        The JWK object.

    Raises:
        ValueError: When the key type is not supported.
    """
    key = serialization.load_pem_public_key(public_pem.encode())

    common = {"kid": key_id, "use": "sig", "alg": settings.jwt.algorithm}

    if isinstance(key, ed25519.Ed25519PublicKey):
        raw = key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return {**common, "kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}

    if isinstance(key, rsa.RSAPublicKey):
        numbers = key.public_numbers()
        return {
            **common,
            "kty": "RSA",
            "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
            "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8)),
        }

    if isinstance(key, ec.EllipticCurvePublicKey):
        numbers = key.public_numbers()
        size = (key.curve.key_size + 7) // 8
        return {
            **common,
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url(numbers.x.to_bytes(size)),
            "y": _b64url(numbers.y.to_bytes(size)),
        }

    raise ValueError(f"Unsupported key type for JWKS: {type(key).__name__}")


def build_jwks() -> dict[str, Any]:
    """Build the JWKS document containing every public verification key.

    Includes retired keys: a verifier that has cached this document must still
    be able to validate tokens signed before the most recent rotation.

    Returns:
        The JWKS document, ready to serialise as JSON.
    """
    return {
        "keys": [
            _jwk_for_pem(key.key_id, key.public_pem)
            for key in get_verification_keys().values()
        ]
    }
