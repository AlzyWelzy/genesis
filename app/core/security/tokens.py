"""JWT creation and verification.

Why this file exists
--------------------
Signing a token is three lines of PyJWT. Signing it *correctly, the same way,
every time* is the part worth centralising: the registered claims, the audience
and issuer checks, the key ID header that makes rotation possible, and the type
discriminator that stops one kind of token being replayed as another.

The design problem this solves
------------------------------
Stateless tokens cannot be revoked. That is their entire performance advantage
and their entire security weakness. The standard answers, both implemented here
as primitives:

**Short access tokens.** The lifetime *is* the revocation window. Fifteen
minutes means a compromised token is useful for at most fifteen minutes.

**Token versioning.** Every access token carries the user's ``tv`` claim. The
user record holds the current value; bumping it invalidates every token already
issued to that user, instantly. This is what makes "log out everywhere",
"password changed", and "account suspended" actually take effect — at the cost
of one cheap lookup (cacheable) per request. The comparison itself is Stage 2's
job; this module guarantees the claim is present and returns it.

Refresh tokens are handled differently: they are long-lived, so they are backed
by a server-side session record and revoked by deleting it. The ``jti`` claim
is that record's identifier.

Scope
-----
Mechanism only. No user lookup, no credential validation, no session storage,
no FastAPI dependencies — this module has no imports from :mod:`app.modules`
and never will. See ``docs/architecture/security.md``.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import jwt

from app.core.config import settings
from app.core.security.keys import get_signing_key, get_verification_key

#: Discriminator stored in the ``type`` claim. Without it, an access token can
#: be presented at the refresh endpoint (or vice versa) and the signature check
#: alone will happily accept it.
ACCESS_TOKEN_TYPE: Final[str] = "access"  # noqa: S105 - a claim value, not a secret
REFRESH_TOKEN_TYPE: Final[str] = "refresh"  # noqa: S105 - a claim value

#: Single-purpose tokens. Short-lived and narrowly scoped, so a leaked
#: verification link cannot be used to authenticate.
EMAIL_VERIFICATION_TOKEN_TYPE: Final[str] = "email_verification"  # noqa: S105
PASSWORD_RESET_TOKEN_TYPE: Final[str] = "password_reset"  # noqa: S105


class InvalidTokenError(Exception):
    """A token failed verification.

    Deliberately opaque: it carries no reason. Callers translate it into a
    generic 401, because telling a client whether a token was *expired* versus
    *forged* versus *for the wrong audience* is free reconnaissance.
    """


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Decoded and verified token claims.

    A verified token is a statement about *identity*, never about
    *authorization*. That the signature is valid says nothing about whether the
    subject still exists, is still active, or may perform the action being
    attempted. Those checks belong upstream in a dependency.

    Attributes:
        subject: The ``sub`` claim — the principal's identifier.
        token_type: Which kind of token this is.
        token_id: The ``jti`` claim, unique per token. Used to revoke a refresh
            token and to detect replay.
        token_version: The subject's token version at issue time. Compare
            against the stored value to honour a global revocation.
        tenant_id: The tenant this token is scoped to, when applicable.
        scopes: Coarse permission scopes carried in the token. Fine-grained
            authorization must be checked server-side, not trusted from here.
        issued_at: When the token was signed.
        expires_at: When it stops being valid.
        raw: The complete claim set, for anything not modelled above.
    """

    subject: str
    token_type: str
    token_id: str
    token_version: int
    tenant_id: UUID | None
    scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    raw: dict[str, Any]


def create_token(  # noqa: PLR0913 - each claim is a distinct, keyword-only concern
    subject: str,
    *,
    token_type: str,
    expires_in: timedelta,
    token_version: int = 0,
    tenant_id: UUID | None = None,
    scopes: tuple[str, ...] = (),
    claims: dict[str, Any] | None = None,
) -> str:
    """Sign a JWT with the standard registered claims.

    The caller decides the subject and lifetime; this function guarantees that
    ``iss``, ``aud``, ``iat``, ``exp``, ``jti``, ``type`` and the token version
    are present and consistent, and that the ``kid`` header names the signing
    key. It performs no authorization checks.

    Args:
        subject: Value for the ``sub`` claim, typically a user identifier.
        token_type: Discriminator stored in the ``type`` claim.
        expires_in: Lifetime measured from now.
        token_version: The subject's current token version.
        tenant_id: Tenant the token is scoped to.
        scopes: Coarse permission scopes.
        claims: Extra application claims. Merged *first*, so registered claim
            names cannot be overridden by a caller and spoofed.

    Returns:
        The encoded, signed JWT.
    """
    now = datetime.now(UTC)
    key = get_signing_key()

    payload: dict[str, Any] = {
        **(claims or {}),
        "sub": subject,
        "iss": settings.jwt.issuer,
        "aud": settings.jwt.audience,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid4().hex,
        "type": token_type,
        settings.security.token_version_claim: token_version,
    }
    if tenant_id is not None:
        payload["tid"] = str(tenant_id)
    if scopes:
        payload["scopes"] = list(scopes)

    return jwt.encode(
        payload,
        key.private_pem,
        algorithm=settings.jwt.algorithm,
        headers={"kid": key.key_id},
    )


def create_access_token(
    subject: str,
    *,
    token_version: int = 0,
    tenant_id: UUID | None = None,
    scopes: tuple[str, ...] = (),
    claims: dict[str, Any] | None = None,
) -> str:
    """Sign a short-lived access token using the configured lifetime."""
    return create_token(
        subject,
        token_type=ACCESS_TOKEN_TYPE,
        expires_in=timedelta(minutes=settings.jwt.access_token_expire_minutes),
        token_version=token_version,
        tenant_id=tenant_id,
        scopes=scopes,
        claims=claims,
    )


def create_refresh_token(
    subject: str,
    *,
    token_version: int = 0,
    tenant_id: UUID | None = None,
    claims: dict[str, Any] | None = None,
) -> str:
    """Sign a long-lived refresh token using the configured lifetime.

    Carries no scopes: a refresh token's only power is to obtain an access
    token, and granting it more would make a stolen one far more valuable.
    """
    return create_token(
        subject,
        token_type=REFRESH_TOKEN_TYPE,
        expires_in=timedelta(days=settings.jwt.refresh_token_expire_days),
        token_version=token_version,
        tenant_id=tenant_id,
        claims=claims,
    )


def _reject_string_scopes(scopes: object) -> None:
    """Refuse a ``scopes`` claim that is a bare string rather than a sequence.

    Raises:
        InvalidTokenError: When ``scopes`` is a string.
    """
    if isinstance(scopes, str):
        raise InvalidTokenError


def decode_token(token: str, *, expected_type: str | None = None) -> TokenClaims:
    """Verify a token's signature and registered claims.

    Signature, expiry, issuer and audience are always validated, and the
    signing key is selected by the token's ``kid`` header so rotated keys keep
    working.

    Args:
        token: The encoded JWT.
        expected_type: When given, the ``type`` claim must match exactly.

    Returns:
        The verified claims.

    Raises:
        InvalidTokenError: On any signature, expiry, audience or type failure.
            The specific reason is intentionally not exposed.
    """
    try:
        header = jwt.get_unverified_header(token)
        key = get_verification_key(header.get("kid"))
        payload: dict[str, Any] = jwt.decode(
            token,
            key.public_pem,
            algorithms=[settings.jwt.algorithm],
            issuer=settings.jwt.issuer,
            audience=settings.jwt.audience,
            leeway=settings.jwt.leeway_seconds,
            options={"require": ["exp", "iat", "sub", "jti", "type"]},
        )
    except (jwt.InvalidTokenError, KeyError) as exc:
        raise InvalidTokenError from exc

    if expected_type is not None and payload.get("type") != expected_type:
        raise InvalidTokenError

    # Building the claims is inside the guard too, not just the signature check.
    # A verified signature says the payload is *ours*, not that every field is
    # well-formed — a `tid` that is not a UUID, an `iat` outside the range of a
    # datetime, or `scopes` that is a string rather than a list all raise here.
    # Uncaught, each becomes a 500 with a traceback instead of a 401, which both
    # breaks the "no reason is exposed" contract above and reports a bad
    # credential as a server fault.
    try:
        tenant_raw = payload.get("tid")
        scopes = payload.get("scopes", ())
        # A single scope serialised as a bare string would otherwise become one
        # entry per character, silently granting nothing that matches.
        _reject_string_scopes(scopes)
        return TokenClaims(
            subject=payload["sub"],
            token_type=payload["type"],
            token_id=payload["jti"],
            token_version=payload.get(settings.security.token_version_claim, 0),
            tenant_id=UUID(tenant_raw) if tenant_raw else None,
            scopes=tuple(scopes),
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
            raw=payload,
        )
    except InvalidTokenError:
        raise
    except (ValueError, TypeError, OverflowError, OSError, AttributeError) as exc:
        raise InvalidTokenError from exc
