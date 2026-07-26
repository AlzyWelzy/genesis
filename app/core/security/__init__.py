"""Security primitives: password hashing, token signing, key management.

Why this is a package
---------------------
Authentication *policy* — who may log in, what a session means, when to lock an
account — is business logic and belongs in a Stage 2 feature module. The
*mechanics* are neither business logic nor infrastructure: they are pure,
stateless helpers that many features need and that must behave identically
everywhere.

Four concerns, four modules, because each has genuinely different reasons to
change:

* :mod:`~app.core.security.keys` — loading, caching and rotating key material.
* :mod:`~app.core.security.passwords` — Argon2id hashing and password policy.
* :mod:`~app.core.security.tokens` — JWT creation and verification.
* :mod:`~app.core.security.jwks` — publishing public keys to other verifiers.

This package contains no authentication flow: no user lookup, no credential
validation, no session storage, no FastAPI dependencies. It has no imports from
:mod:`app.modules` and never will.

Import from here rather than reaching into the submodules::

    from app.core.security import create_access_token, hash_password
"""

from app.core.security.jwks import build_jwks
from app.core.security.keys import (
    SigningKey,
    VerificationKey,
    get_signing_key,
    get_verification_key,
    get_verification_keys,
    reset_key_cache,
)
from app.core.security.passwords import (
    hash_password,
    validate_password_policy,
    verify_and_update_password,
    verify_password,
)
from app.core.security.tokens import (
    ACCESS_TOKEN_TYPE,
    EMAIL_VERIFICATION_TOKEN_TYPE,
    PASSWORD_RESET_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    InvalidTokenError,
    TokenClaims,
    create_access_token,
    create_refresh_token,
    create_token,
    decode_token,
)

__all__ = [
    "ACCESS_TOKEN_TYPE",
    "EMAIL_VERIFICATION_TOKEN_TYPE",
    "PASSWORD_RESET_TOKEN_TYPE",
    "REFRESH_TOKEN_TYPE",
    "InvalidTokenError",
    "SigningKey",
    "TokenClaims",
    "VerificationKey",
    "build_jwks",
    "create_access_token",
    "create_refresh_token",
    "create_token",
    "decode_token",
    "get_signing_key",
    "get_verification_key",
    "get_verification_keys",
    "hash_password",
    "reset_key_cache",
    "validate_password_policy",
    "verify_and_update_password",
    "verify_password",
]
