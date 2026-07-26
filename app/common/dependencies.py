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

The pattern
-----------
Dependencies compose in one direction, each layer adding exactly one thing::

    SessionDep                    database session, per request
      → CurrentClaimsDep          a verified token (identity)
        → CurrentUserDep          a loaded, active user (identity + state)
          → CurrentTenantDep      the tenant this request operates in
            → require_permission  authorization for this specific action

Each step is a separate dependency because each fails differently: no token is
a 401, a token for a deleted user is a 401, a valid user outside the tenant is
a 404, and a valid user without the permission is a 403.

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

Declaring authorization
-----------------------
Permissions are declared at the route, so the requirement stays visible next to
what it guards and appears in the OpenAPI schema::

    @router.delete(
        "/{invoice_id}",
        dependencies=[Depends(require_permission("invoices:delete"))],
    )

Prefer that over checking roles inside a service: the permission model can then
change without touching business logic.

What must never be here
-----------------------
Business logic. A dependency that decides *what to charge* is a service
wearing a dependency's clothes. Dependencies resolve, authenticate, authorise
and construct — nothing else.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.pagination import PaginationParams
from app.common.sorting import SortParams
from app.infrastructure.database.session import get_session

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
# Identity, authorization and tenancy — implemented in Stage 2
# ---------------------------------------------------------------------------
#
# Declared here as the contract every feature will import, so module authors
# have one obvious place to look and cannot invent a parallel scheme. The
# implementations live in the auth module because they need the user
# repository, and core/common must never import from app.modules.
#
# TODO: get_current_claims(request) -> TokenClaims
#   Extract the bearer token, verify it with app.core.security.decode_token,
#   require token_type == "access". Raise AuthenticationError on anything
#   missing, malformed or expired. Pure token verification: no database access,
#   so it stays cheap and usable for endpoints that need identity but not a
#   user record.
#
# TODO: get_current_user(claims, session) -> User
#   Load the user named by `claims.subject`. Reject when the record is missing,
#   inactive, locked, or when `user.token_version != claims.token_version` —
#   that comparison is what makes logout-everywhere and password-change
#   revocation take effect against an already-issued stateless token. Cache the
#   lookup per request; it is on the hot path of every authenticated endpoint.
#
# TODO: get_current_tenant(user, claims) -> Tenant
#   Resolve the tenant from the token's `tid` claim, verify the user is still a
#   member, and push it into app.core.context.tenant_id_var so the repository
#   layer scopes every query automatically. Membership must be re-checked here,
#   not trusted from the token: a user removed from a tenant still holds a
#   token naming it until that token expires.
#
# TODO: require_permission(*permissions) -> Callable
#   Dependency factory raising AuthorizationError unless the current user holds
#   every listed permission. See the module docstring for the call shape.
#
# TODO: require_superuser() — narrow escape hatch for internal tooling. Keep it
#   rare and audited; a broad admin bypass is how authorization models decay.
