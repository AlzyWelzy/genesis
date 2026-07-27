"""Tests for the principal seam and the authorization dependencies.

Weighted heavily towards refusal. An authorization layer that permits the right
callers is easy; one that *refuses* revoked tokens, deactivated accounts and
non-members is the part that matters, and each of those is a real bypass if it
regresses.
"""

from dataclasses import dataclass, field
from uuid import UUID, uuid7

import pytest

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.principal import (
    assert_active,
    assert_token_version,
    get_membership_checker,
    get_principal_loader,
    has_permissions,
    reset_principal_seams,
    set_principal_loader,
)


@dataclass(frozen=True, slots=True)
class StubPrincipal:
    """Satisfies the Principal protocol structurally, with no User model."""

    id: UUID = field(default_factory=uuid7)
    is_active: bool = True
    token_version: int = 0
    permissions: frozenset[str] = frozenset()
    is_superuser: bool = False


@pytest.fixture(autouse=True)
def _clean_seams():
    """Seams are process-wide globals; leaking one authenticates later tests."""
    reset_principal_seams()
    yield
    reset_principal_seams()


class TestSeamRegistration:
    async def test_registered_loader_is_returned(self) -> None:
        principal = StubPrincipal()

        async def load(_subject: str):
            return principal

        set_principal_loader(load)
        assert await get_principal_loader()("any") is principal

    def test_missing_loader_raises_rather_than_returning_none(self) -> None:
        """Treating an authenticated request as anonymous is a bypass."""
        with pytest.raises(RuntimeError, match="set_principal_loader"):
            get_principal_loader()

    def test_missing_membership_checker_raises(self) -> None:
        with pytest.raises(RuntimeError, match="set_membership_checker"):
            get_membership_checker()

    def test_replacing_a_loader_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """Two modules both registering means one silently loses."""

        async def load(_subject: str):
            return None

        set_principal_loader(load)
        with caplog.at_level("WARNING"):
            set_principal_loader(load)

        assert "replaced" in caplog.text

    def test_structural_typing_needs_no_inheritance(self) -> None:
        """This is what lets core stay ignorant of the auth module's User."""
        from app.core.principal import Principal

        assert isinstance(StubPrincipal(), Principal)


class TestTokenVersion:
    def test_matching_version_is_accepted(self) -> None:
        assert_token_version(StubPrincipal(token_version=3), 3)

    def test_stale_version_is_rejected(self) -> None:
        """The mechanism behind logout-everywhere and password-change revocation."""
        with pytest.raises(AuthenticationError):
            assert_token_version(StubPrincipal(token_version=4), 3)

    def test_a_token_from_the_future_is_also_rejected(self) -> None:
        """Not equal is not equal; a mismatch either way is untrustworthy."""
        with pytest.raises(AuthenticationError):
            assert_token_version(StubPrincipal(token_version=1), 9)


class TestActiveCheck:
    def test_active_principal_passes(self) -> None:
        assert_active(StubPrincipal(is_active=True))

    def test_inactive_principal_is_rejected(self) -> None:
        """Suspension must take effect before the token expires."""
        with pytest.raises(AuthenticationError):
            assert_active(StubPrincipal(is_active=False))

    def test_rejection_is_401_not_403(self) -> None:
        """The credentials are unusable; retrying with them will never help."""
        with pytest.raises(AuthenticationError) as exc:
            assert_active(StubPrincipal(is_active=False))
        assert exc.value.status_code == 401


class TestPermissions:
    def test_holding_every_permission_passes(self) -> None:
        principal = StubPrincipal(permissions=frozenset({"a", "b"}))
        assert has_permissions(principal, ["a", "b"]) is True

    def test_missing_one_permission_fails(self) -> None:
        """All, not any: an endpoint naming two requirements means both."""
        principal = StubPrincipal(permissions=frozenset({"a"}))
        assert has_permissions(principal, ["a", "b"]) is False

    def test_superuser_bypasses(self) -> None:
        assert has_permissions(StubPrincipal(is_superuser=True), ["anything"]) is True

    def test_no_requirements_passes(self) -> None:
        assert has_permissions(StubPrincipal(), []) is True

    def test_extra_permissions_do_not_matter(self) -> None:
        principal = StubPrincipal(permissions=frozenset({"a", "b", "c"}))
        assert has_permissions(principal, ["a"]) is True


class TestGuards:
    async def test_require_permission_allows_a_holder(self) -> None:
        from app.common.dependencies import require_permission

        principal = StubPrincipal(permissions=frozenset({"invoices:delete"}))
        guard = require_permission("invoices:delete")

        assert await guard(principal) is principal

    async def test_require_permission_refuses_a_non_holder(self) -> None:
        from app.common.dependencies import require_permission

        guard = require_permission("invoices:delete")

        with pytest.raises(AuthorizationError) as exc:
            await guard(StubPrincipal())
        assert exc.value.status_code == 403

    async def test_refusal_names_the_requirement(self) -> None:
        """A 403 that does not say what was needed is unactionable."""
        from app.common.dependencies import require_permission

        with pytest.raises(AuthorizationError) as exc:
            await require_permission("invoices:delete")(StubPrincipal())

        assert exc.value.details["required"] == ["invoices:delete"]

    async def test_require_superuser_refuses_a_normal_principal(self) -> None:
        from app.common.dependencies import require_superuser

        with pytest.raises(AuthorizationError):
            await require_superuser()(StubPrincipal())

    async def test_require_superuser_allows_a_superuser(self) -> None:
        from app.common.dependencies import require_superuser

        principal = StubPrincipal(is_superuser=True)
        assert await require_superuser()(principal) is principal


class TestMalformedClaims:
    """A verified signature says the payload is *ours*, not that it is well-formed.

    Every one of these previously escaped ``decode_token`` as a ``ValueError`` or
    ``TypeError``, becoming a 500 with a traceback rather than a 401 — which both
    reports a bad credential as a server fault and breaks the documented promise
    that no reason for rejection is exposed.
    """

    @staticmethod
    def _signed(**extra: object) -> str:
        """Mint a correctly signed token carrying deliberately odd claims."""
        import datetime as dt

        import jwt

        from app.core.config import settings
        from app.core.security.keys import get_signing_key

        key = get_signing_key()
        now = dt.datetime.now(dt.UTC)
        payload = {
            "sub": "u",
            "iss": settings.jwt.issuer,
            "aud": settings.jwt.audience,
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
            "jti": "j",
            "type": "access",
        }
        payload.update(extra)
        return jwt.encode(
            payload,
            key.private_pem,
            algorithm=settings.jwt.algorithm,
            headers={"kid": key.key_id},
        )

    @pytest.mark.parametrize(
        ("label", "claims"),
        [
            ("tenant id that is not a uuid", {"tid": "not-a-uuid"}),
            ("issued-at beyond datetime range", {"iat": 10**20}),
            ("scopes as an integer", {"scopes": 5}),
        ],
    )
    def test_a_malformed_claim_is_an_invalid_token_not_a_crash(
        self, label: str, claims: dict
    ) -> None:
        from app.core.security.tokens import InvalidTokenError, decode_token

        with pytest.raises(InvalidTokenError):
            decode_token(self._signed(**claims))

    def test_scopes_as_a_bare_string_is_rejected(self) -> None:
        """A bare string scope must be rejected, not split into characters.

        ``tuple("admin")`` is ``('a','d','m','i','n')`` — five scopes matching
        nothing, so the token grants silently less than intended instead of
        failing.
        """
        from app.core.security.tokens import InvalidTokenError, decode_token

        with pytest.raises(InvalidTokenError):
            decode_token(self._signed(scopes="admin"))

    def test_a_well_formed_token_still_decodes(self) -> None:
        from app.core.security.tokens import decode_token

        claims = decode_token(self._signed())
        assert claims.subject == "u"
        assert claims.scopes == ()
