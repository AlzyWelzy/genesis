"""Shared FastAPI dependencies and the injection pattern every module follows.

Why this file exists
--------------------
Dependency injection is the seam between HTTP and business logic. If each
module invents its own way across it, four things go wrong: services get
constructed inside routers (untestable), authentication is re-implemented per
feature (and one copy will be wrong), the OpenAPI schema disagrees with what is
enforced, and nothing can be overridden cleanly in a test.

This module defines the *shared* dependencies and, just as importantly, the
pattern feature modules copy. See ``docs/architecture/dependency-rules.md``.

The chain
---------
Dependencies compose in one direction, each layer adding exactly one thing::

    SessionDep                  database session, per request
      → ClaimsDep               a verified token (identity, no I/O)
        → PrincipalDep          a loaded, active, current-version principal
          → TenantDep           the tenant this request operates in
            → require_permission(...)   authorization for this action

Each step is separate because each fails differently, and conflating them
produces the wrong status code:

============================  ======  =====================================
No or malformed token         401     credentials missing
Token for a deleted user      401     credentials no longer valid
Token predating a version bump 401    revoked
Valid user, foreign tenant    404     existence is privileged information
Valid user, missing permission 403    known, and refused
============================  ======  =====================================

Cost
----
``ClaimsDep`` performs no I/O — it verifies a signature and nothing else — so
endpoints needing only identity stay cheap. ``PrincipalDep`` costs one lookup,
cached per request, and is the first dependency that touches the database.

Annotated aliases
-----------------
Export ``Annotated[X, Depends(...)]`` aliases rather than raw functions. The
call site reads as a type::

    async def list_invoices(session: SessionDep, params: PaginationDep) -> ...:

and the wiring is stated once here instead of repeated in every signature.

The per-module service factory pattern
--------------------------------------
Every feature module defines its own ``dependencies.py`` following exactly this
shape. Copy it verbatim — the consistency is the point::

    from app.common.dependencies import SessionDep

    def get_invoice_service(session: SessionDep) -> InvoiceService:
        \"\"\"Build the invoice service for this request.\"\"\"
        return InvoiceService(InvoiceRepository(session))

    type InvoiceServiceDep = Annotated[
        InvoiceService, Depends(get_invoice_service)
    ]

The router depends on ``InvoiceServiceDep`` and never constructs anything.
Overriding one dependency in a test replaces the whole graph beneath it.

What must never be here
-----------------------
Business logic. A dependency that decides *what to charge* is a service wearing
a dependency's clothes. Dependencies resolve, authenticate, authorise and
construct — nothing else.
"""

from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.constants import BEARER_PREFIX
from app.common.pagination import PaginationParams
from app.common.sorting import SortParams
from app.core.context import get_user_id, tenant_id_var, user_id_var
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    RateLimitError,
)
from app.core.principal import (
    Principal,
    assert_active,
    assert_token_version,
    get_membership_checker,
    get_principal_loader,
    has_permissions,
)
from app.core.security import ACCESS_TOKEN_TYPE, InvalidTokenError, TokenClaims
from app.core.security import decode_token as _decode_token
from app.infrastructure.database.session import get_session
from app.infrastructure.redis.rate_limit import (
    check_rate_limit,
    check_token_bucket,
)

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

#: Request-scoped database session. One per request, rolled back if the request
#: raises. Repositories receive this; routers pass it straight to a factory and
#: never touch it themselves.
type SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------

#: Validated ``page``/``size`` parameters, capped at ``MAX_PAGE_SIZE``.
type PaginationDep = Annotated[PaginationParams, Depends()]

#: Validated ``sort_by``/``order`` parameters. The repository still checks the
#: field against its own allow-list — see :mod:`app.common.sorting`.
type SortDep = Annotated[SortParams, Depends()]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def extract_bearer_token(request: Request) -> str | None:
    """Pull the bearer token out of the ``Authorization`` header.

    Returns ``None`` rather than raising for a missing or non-bearer header, so
    the optional and required variants can share one parser and each decide what
    absence means.
    """
    header = request.headers.get("Authorization")
    if not header or not header.startswith(BEARER_PREFIX):
        return None
    return header.removeprefix(BEARER_PREFIX).strip() or None


async def get_optional_claims(request: Request) -> TokenClaims | None:
    """Verify the token if one is present, without requiring it.

    For endpoints that serve both anonymous and authenticated callers — a
    public listing that shows extra fields when signed in. A *malformed* token
    is still rejected: silently treating a broken token as anonymous hides real
    client bugs and would let an expired session look like a logged-out one.
    """
    token = extract_bearer_token(request)
    if token is None:
        return None

    try:
        claims = _decode_token(token, expected_type=ACCESS_TOKEN_TYPE)
    except InvalidTokenError as exc:
        raise AuthenticationError("Invalid or expired credentials.") from exc

    _bind_identity(claims)
    return claims


async def get_current_claims(request: Request) -> TokenClaims:
    """Verify the presented access token and return its claims.

    Pure token verification: signature, expiry, issuer, audience and type. No
    database access, so this stays cheap for endpoints that need only an
    identifier.

    A verified token is a statement about *identity*, never *authorization*.
    That the signature is valid says nothing about whether the subject still
    exists, is active, or may perform the action — use :data:`PrincipalDep` for
    that.

    Raises:
        AuthenticationError: When the token is absent, malformed, expired, or
            not an access token.
    """
    claims = await get_optional_claims(request)
    if claims is None:
        raise AuthenticationError
    return claims


def _bind_identity(claims: TokenClaims) -> None:
    """Publish the caller's identity into the ambient request context.

    Makes the user and tenant available to logging, audit and the repository
    layer without threading them through every signature. Bound here, at the
    edge, and nowhere else — a service that sets its own tenant has made the
    isolation boundary un-auditable.
    """
    # A non-UUID subject is legitimate for machine principals; context
    # attribution simply does not apply to them.
    with suppress(ValueError):
        user_id_var.set(UUID(claims.subject))
    if claims.tenant_id is not None:
        tenant_id_var.set(claims.tenant_id)


async def get_current_principal(
    claims: Annotated[TokenClaims, Depends(get_current_claims)],
) -> Principal:
    """Load the caller and confirm their credentials are still usable.

    Three checks, each catching a token that is cryptographically valid but no
    longer meaningful:

    1. **The principal still exists.** A deleted user's token is otherwise
       valid until it expires.
    2. **The account is active.** Suspension must take effect immediately.
    3. **The token version matches.** This is what makes "log out everywhere"
       and password-change revocation work against an already-issued token.

    The loader is supplied by the auth module through
    :mod:`app.core.principal`, so core never imports a feature.

    Raises:
        AuthenticationError: When the principal is missing, inactive, or the
            token predates a version bump. All three produce the same message:
            distinguishing them tells an attacker which subjects exist.
    """
    principal = await get_principal_loader()(claims.subject)
    if principal is None:
        raise AuthenticationError("Credentials are no longer valid.")

    assert_active(principal)
    assert_token_version(principal, claims.token_version)
    return principal


async def get_current_tenant_id(
    claims: Annotated[TokenClaims, Depends(get_current_claims)],
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> UUID:
    """Resolve the tenant this request operates in, re-checking membership.

    Membership is verified against the database rather than trusted from the
    token. A user removed from a tenant keeps a token naming it until that
    token expires, and honouring that claim would let them keep reading the
    tenant's data for the remainder of its lifetime.

    A non-member gets a **404, not a 403**: whether a given tenant exists is
    itself privileged information, and a 403 confirms it.

    Raises:
        AuthenticationError: When the token carries no tenant.
        NotFoundError: When the principal is not a member.
    """
    if claims.tenant_id is None:
        raise AuthenticationError("This token is not scoped to a tenant.")

    if not await get_membership_checker()(principal, claims.tenant_id):
        raise NotFoundError("Tenant not found.")

    tenant_id_var.set(claims.tenant_id)
    return claims.tenant_id


#: A verified access token. No I/O — signature verification only.
type ClaimsDep = Annotated[TokenClaims, Depends(get_current_claims)]

#: A verified token, or ``None`` for an anonymous caller.
type OptionalClaimsDep = Annotated[TokenClaims | None, Depends(get_optional_claims)]

#: The loaded, active, current-version caller. Costs one lookup.
type PrincipalDep = Annotated[Principal, Depends(get_current_principal)]

#: The tenant this request operates in, with membership re-checked.
type TenantDep = Annotated[UUID, Depends(get_current_tenant_id)]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

type _Guard = Callable[..., Coroutine[Any, Any, Principal]]


def require_permission(*permissions: str) -> _Guard:
    """Build a dependency requiring every listed permission.

    A factory, so the requirement is declared at the route and therefore
    visible in the OpenAPI schema and next to the thing it guards::

        @router.delete(
            "/{invoice_id}",
            dependencies=[Depends(require_permission("invoices:delete"))],
        )

    Prefer this over checking roles inside a service. The requirement stays
    next to the route, and the permission model can change without touching
    business logic.

    Requires *all* listed permissions, not any. An endpoint naming two means
    both; "either" should be modelled as a single composite permission rather
    than by loosening the default.

    Args:
        *permissions: Permission identifiers, e.g. ``"invoices:delete"``.

    Returns:
        A dependency returning the principal when permitted.
    """

    async def guard(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        """Raise unless the principal holds every required permission."""
        if not has_permissions(principal, permissions):
            raise AuthorizationError(
                "You do not have permission to perform this action.",
                details={"required": sorted(permissions)},
            )
        return principal

    return guard


def require_scopes(*scopes: str) -> _Guard:
    """Build a dependency requiring every listed token scope.

    Scopes are carried *in the token* and are therefore coarse and slightly
    stale — a scope revoked a minute ago is still present until the token
    expires. That makes them right for gating whole API surfaces from a machine
    client, and wrong for anything a user can have taken away mid-session.

    For fine-grained, immediately-revocable checks use
    :func:`require_permission`, which resolves server-side.

    Args:
        *scopes: Scope identifiers the token must carry.

    Returns:
        A dependency returning the principal when permitted.
    """

    async def guard(
        claims: Annotated[TokenClaims, Depends(get_current_claims)],
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        """Raise unless the token carries every required scope."""
        if not set(scopes).issubset(claims.scopes):
            raise AuthorizationError(
                "This token does not carry the required scope.",
                details={"required": sorted(scopes)},
            )
        return principal

    return guard


def require_superuser() -> _Guard:
    """Build a dependency permitting only superusers.

    A narrow escape hatch for internal tooling. Keep its use rare and audited:
    a broad admin bypass is how a permission model decays into "is_admin"
    checks scattered through the codebase, at which point the model is
    decorative.

    Returns:
        A dependency returning the principal when they are a superuser.
    """

    async def guard(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        """Raise unless the principal is a superuser."""
        if not principal.is_superuser:
            raise AuthorizationError("This action requires elevated privileges.")
        return principal

    return guard


def rate_limit(
    limit: int, *, window_seconds: int = 60, burst: int | None = None
) -> Callable[[Request], Awaitable[None]]:
    """Apply a tighter rate limit to one route.

    Why this is a dependency and not middleware
    -------------------------------------------
    The global limit protects the service from a runaway client. It is the wrong
    instrument for a single expensive endpoint: set it low enough to protect a
    report generator and ordinary reads are throttled; set it high enough for
    ordinary reads and the report generator is unprotected.

    A per-route limit has to know *which* route it is limiting, and that is
    precisely what middleware cannot know — middleware runs above the router, so
    ``scope["route"]`` is still empty. Every attempt to guess the template from
    ``app.routes`` depends on FastAPI internals that do not hold: an
    ``include_router`` is not flattened but wrapped in an opaque object with no
    ``path``, and a nested route's ``path`` is relative to its immediate router's
    prefix rather than to the full mount point. A version of this that resolved
    routes registered with ``@app.get`` returned "unmatched" for every route a
    feature mounts — which is every real endpoint — and was therefore inert while
    appearing to work.

    As a dependency it runs *after* routing, so the template is simply
    ``scope["route"].path``, with no internals involved. It also becomes visible
    in the OpenAPI schema, testable on its own, and costs nothing on the routes
    that do not use it.

    Usage::

        @router.post(
            "/reports",
            dependencies=[Depends(rate_limit(5, window_seconds=60))],
        )
        async def generate_report(...): ...

    Args:
        limit: Requests permitted per window.
        window_seconds: Length of the window.
        burst: When set, uses a token bucket permitting this many requests
            back to back while holding the long-run average to ``limit``. For
            clients that legitimately arrive in waves — a sync on startup —
            where a sliding window would have to be set implausibly high.

    Returns:
        A dependency that raises :class:`~app.core.exceptions.RateLimitError`
        when the caller is over their allowance.
    """

    async def guard(request: Request) -> None:
        # Keyed by identity *and* route. Keying by identity alone would make a
        # tight limit on one expensive endpoint share a counter with every
        # ordinary read the same caller makes, so unrelated traffic would
        # exhaust it and its own allowance would eat everyone else's.
        route = getattr(request.scope.get("route"), "path", request.url.path)
        user_id = get_user_id()
        identity = f"user:{user_id}" if user_id else _client_identity(request)
        key = f"{identity}:{route}"

        if burst is None:
            result = await check_rate_limit(
                key, limit=limit, window_seconds=window_seconds
            )
        else:
            result = await check_token_bucket(
                key,
                rate=limit / window_seconds,
                burst=burst,
                window_seconds=window_seconds,
            )

        if not result.allowed:
            # Raised, not returned. Unlike the middleware, a dependency runs
            # *below* `ExceptionMiddleware`, so the handler turns this into the
            # standard 429 envelope.
            raise RateLimitError(retry_after=result.reset_after)

    return guard


def _client_identity(request: Request) -> str:
    """Identify an unauthenticated caller for rate-limiting purposes.

    The left-most ``X-Forwarded-For`` entry is the original client; the rest are
    proxy hops. Trusted only because a proxy and ``TrustedHostMiddleware`` sit in
    front — the header is client-controlled and must not be believed by a
    directly exposed app.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    return f"ip:{client_ip}"
