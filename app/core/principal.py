"""The authenticated caller, and the seam that loads them.

Why this file exists
--------------------
Authorization needs to know *who is calling* and *what they may do*. Both facts
live on a ``User`` model, which is a Stage 2 feature — and :mod:`app.core` must
never import from :mod:`app.modules`, so core cannot reach for it.

The usual workarounds are both bad. Putting the dependencies in the auth module
means every feature imports that module directly, so there is no seam left to
swap in a test. Deferring the whole authorization layer to Stage 2 means the
dependency contract is invented per feature, and the first three features each
invent it differently.

The seam
--------
This module defines what core *needs* from a principal as a
:class:`~typing.Protocol`, plus a registration hook for supplying the loader.
Core depends on the protocol; the auth module registers a concrete loader at
startup; nothing imports inward. Structural typing means the auth module's
``User`` satisfies :class:`Principal` without importing or inheriting anything
from here — the coupling is nominal-free in both directions.

Registration happens once, in the auth module's import side effects. In
``app/modules/auth/handlers.py``, import ``set_principal_loader`` from this
module together with the feature's own ``load_principal``, then::

    set_principal_loader(load_principal)

Until that happens, any endpoint depending on a principal fails loudly with a
message naming the missing registration — never by treating the caller as
anonymous, which would turn a wiring mistake into an authorization bypass.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class Principal(Protocol):
    """The authenticated caller, as core needs to understand them.

    Deliberately minimal: five members, each required by a check core actually
    performs. A ``User`` model will carry far more — name, email, timestamps —
    and none of it belongs in this contract.

    Attributes:
        id: Stable identifier. Used for audit attribution and rate-limit keying.
        is_active: Whether the account may currently authenticate. A deactivated
            user holding a valid unexpired token must still be refused.
        token_version: Incremented to invalidate every token already issued to
            this principal. The mechanism behind "log out everywhere", password
            change and suspension — without it, a stateless token cannot be
            revoked before it expires.
        permissions: Fine-grained permissions, resolved server-side. Never read
            from the token: a token minted before a permission was revoked would
            still carry it.
        is_superuser: Bypasses permission checks. Kept narrow and audited.
    """

    @property
    def id(self) -> UUID:
        """Stable identifier for the principal."""
        ...

    @property
    def is_active(self) -> bool:
        """Whether the account may currently authenticate."""
        ...

    @property
    def token_version(self) -> int:
        """Counter whose increment invalidates previously issued tokens."""
        ...

    @property
    def permissions(self) -> frozenset[str]:
        """Permissions held, resolved server-side."""
        ...

    @property
    def is_superuser(self) -> bool:
        """Whether permission checks are bypassed for this principal."""
        ...


#: Loads a principal by its subject claim. Returns ``None`` when no such
#: principal exists — the dependency turns that into a 401, so the loader
#: itself never needs to know about HTTP.
type PrincipalLoader = Callable[[str], Awaitable[Principal | None]]

#: Resolves a tenant membership check. Returns ``True`` when the principal is
#: still a member of the tenant. Re-checked per request rather than trusted
#: from the token, because a user removed from a tenant keeps a token naming it
#: until that token expires.
type MembershipChecker = Callable[[Principal, UUID], Awaitable[bool]]

_principal_loader: PrincipalLoader | None = None
_membership_checker: MembershipChecker | None = None


def set_principal_loader(loader: PrincipalLoader) -> None:
    """Register the function that loads a principal by subject.

    Called once during startup by the auth module. Registering twice replaces
    the previous loader and logs, because a silent second registration means
    two modules both believe they own authentication.
    """
    global _principal_loader  # noqa: PLW0603 - process-wide seam by design

    if _principal_loader is not None:
        logger.warning("Principal loader replaced; two modules may be registering it")
    _principal_loader = loader


def get_principal_loader() -> PrincipalLoader:
    """Return the registered loader.

    Raises:
        RuntimeError: When no loader has been registered. Deliberately fatal:
            the alternative — treating an authenticated request as anonymous —
            converts a wiring mistake into an authorization bypass.
    """
    if _principal_loader is None:
        raise RuntimeError(
            "No principal loader registered. The auth module must call "
            "app.core.principal.set_principal_loader() during startup; see "
            "docs/architecture/security.md."
        )
    return _principal_loader


def set_membership_checker(checker: MembershipChecker) -> None:
    """Register the tenant-membership check."""
    global _membership_checker  # noqa: PLW0603 - process-wide seam by design
    _membership_checker = checker


def get_membership_checker() -> MembershipChecker:
    """Return the registered membership check.

    Raises:
        RuntimeError: When none is registered, for the same reason as
            :func:`get_principal_loader` — an unchecked membership is a
            cross-tenant read.
    """
    if _membership_checker is None:
        raise RuntimeError(
            "No membership checker registered. The tenancy module must call "
            "app.core.principal.set_membership_checker() during startup."
        )
    return _membership_checker


def reset_principal_seams() -> None:
    """Clear both registrations. For tests."""
    global _principal_loader, _membership_checker  # noqa: PLW0603 - test seam
    _principal_loader = None
    _membership_checker = None


def assert_token_version(principal: Principal, token_version: int) -> None:
    """Reject a token issued before the principal's version was bumped.

    This single comparison is what makes a stateless token revocable. Without
    it, "log out everywhere" and "suspend this account" do nothing until every
    outstanding access token expires on its own.

    Args:
        principal: The loaded principal.
        token_version: The ``tv`` claim from the presented token.

    Raises:
        AuthenticationError: When the token predates the current version.
    """
    if principal.token_version != token_version:
        logger.info(
            "Rejected a token from a previous version",
            extra={"principal_id": str(principal.id)},
        )
        raise AuthenticationError("Credentials are no longer valid.")


def assert_active(principal: Principal) -> None:
    """Reject a deactivated principal.

    Raises:
        AuthenticationError: When the account is not active. A 401 rather than
            a 403: the credentials themselves are no longer usable, and
            retrying with them will never help.
    """
    if not principal.is_active:
        raise AuthenticationError("This account is not active.")


def has_permissions(principal: Principal, required: Sequence[str]) -> bool:
    """Whether a principal holds every listed permission.

    Superusers bypass the check. Requiring *all* rather than *any* is the safe
    default: an endpoint declaring two permissions means both, and anyone
    wanting "either" should say so explicitly with a single composite
    permission rather than relying on a looser default.
    """
    if principal.is_superuser:
        return True
    return set(required).issubset(principal.permissions)
