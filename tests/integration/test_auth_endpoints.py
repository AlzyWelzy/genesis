"""Authorization through the real HTTP stack.

Exercised end to end because that is where the dependency chain actually runs.
Calling the guard functions directly proves they raise; only a request proves
the raise becomes the right status code with the right envelope.

The tokens here are real — signed with the application's own key and verified
by the genuine dependency. A test that overrides the auth dependency proves the
endpoint works when authentication is skipped, which is not the thing worth
proving.
"""

import pytest

from app.common.dependencies import (
    ClaimsDep,
    PrincipalDep,
    require_permission,
    require_superuser,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def guarded_app(app, registered_principal):
    """An app with endpoints exercising each rung of the dependency chain."""
    from fastapi import Depends

    @app.get("/_t/claims")
    async def claims_only(claims: ClaimsDep) -> dict:
        return {"subject": claims.subject}

    @app.get("/_t/principal")
    async def principal_only(principal: PrincipalDep) -> dict:
        return {"id": str(principal.id)}

    @app.get(
        "/_t/permitted",
        dependencies=[Depends(require_permission("invoices:read"))],
    )
    async def permitted() -> dict:
        return {"ok": True}

    @app.get("/_t/superuser", dependencies=[Depends(require_superuser())])
    async def superuser_only() -> dict:
        return {"ok": True}

    return app


class TestUnauthenticated:
    async def test_a_missing_token_is_401(self, guarded_app, client) -> None:
        response = await client.get("/_t/claims")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_a_401_carries_the_challenge_header(
        self, guarded_app, client
    ) -> None:
        """A 401 without WWW-Authenticate is not a valid HTTP challenge."""
        response = await client.get("/_t/claims")
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_a_malformed_token_is_401(self, guarded_app, client) -> None:
        response = await client.get(
            "/_t/claims", headers={"Authorization": "Bearer not-a-token"}
        )
        assert response.status_code == 401

    async def test_a_non_bearer_scheme_is_401(self, guarded_app, client) -> None:
        response = await client.get(
            "/_t/claims", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert response.status_code == 401

    async def test_a_refresh_token_is_rejected(self, guarded_app, client) -> None:
        """Access endpoints must not accept a refresh token."""
        from app.core.security import create_refresh_token

        token = create_refresh_token("someone")
        response = await client.get(
            "/_t/claims", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401


class TestAuthenticated:
    async def test_a_valid_token_reaches_the_endpoint(
        self, guarded_app, authenticated_client, registered_principal
    ) -> None:
        response = await authenticated_client.get("/_t/claims")
        assert response.status_code == 200
        assert response.json()["subject"] == str(registered_principal.id)

    async def test_the_principal_is_loaded(
        self, guarded_app, authenticated_client, registered_principal
    ) -> None:
        response = await authenticated_client.get("/_t/principal")
        assert response.status_code == 200
        assert response.json()["id"] == str(registered_principal.id)


class TestRevocation:
    async def test_a_stale_token_version_is_rejected(
        self, guarded_app, client, principal_factory
    ) -> None:
        """The mechanism behind logout-everywhere and password-change revocation."""
        from app.core.principal import (
            reset_principal_seams,
            set_principal_loader,
        )
        from app.core.security import create_access_token

        principal = principal_factory(token_version=5)

        async def load(_subject: str):
            return principal

        set_principal_loader(load)
        try:
            # Minted while the version was 4 — one bump ago.
            token = create_access_token(str(principal.id), token_version=4)
            response = await client.get(
                "/_t/principal", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
        finally:
            reset_principal_seams()

    async def test_a_deactivated_principal_is_rejected(
        self, guarded_app, client, principal_factory
    ) -> None:
        """Suspension must take effect before the token expires."""
        from app.core.principal import reset_principal_seams, set_principal_loader
        from app.core.security import create_access_token

        principal = principal_factory(is_active=False)

        async def load(_subject: str):
            return principal

        set_principal_loader(load)
        try:
            token = create_access_token(str(principal.id))
            response = await client.get(
                "/_t/principal", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
        finally:
            reset_principal_seams()

    async def test_a_deleted_principal_is_rejected(self, guarded_app, client) -> None:
        from app.core.principal import reset_principal_seams, set_principal_loader
        from app.core.security import create_access_token

        async def load(_subject: str):
            return None

        set_principal_loader(load)
        try:
            token = create_access_token("gone")
            response = await client.get(
                "/_t/principal", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
        finally:
            reset_principal_seams()


class TestPermissions:
    async def test_a_missing_permission_is_403_not_401(
        self, guarded_app, authenticated_client
    ) -> None:
        """403 means "I know who you are, and no" — retrying will not help."""
        response = await authenticated_client.get("/_t/permitted")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_the_403_names_the_requirement(
        self, guarded_app, authenticated_client
    ) -> None:
        response = await authenticated_client.get("/_t/permitted")
        assert response.json()["error"]["details"]["required"] == ["invoices:read"]

    async def test_a_holder_is_allowed(
        self, guarded_app, client, principal_factory
    ) -> None:
        from app.core.principal import reset_principal_seams, set_principal_loader
        from app.core.security import create_access_token

        principal = principal_factory(permissions=frozenset({"invoices:read"}))

        async def load(_subject: str):
            return principal

        set_principal_loader(load)
        try:
            token = create_access_token(str(principal.id))
            response = await client.get(
                "/_t/permitted", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
        finally:
            reset_principal_seams()

    async def test_a_superuser_bypasses_permission_checks(
        self, guarded_app, client, principal_factory
    ) -> None:
        from app.core.principal import reset_principal_seams, set_principal_loader
        from app.core.security import create_access_token

        principal = principal_factory(is_superuser=True)

        async def load(_subject: str):
            return principal

        set_principal_loader(load)
        try:
            token = create_access_token(str(principal.id))
            assert (
                await client.get(
                    "/_t/permitted", headers={"Authorization": f"Bearer {token}"}
                )
            ).status_code == 200
            assert (
                await client.get(
                    "/_t/superuser", headers={"Authorization": f"Bearer {token}"}
                )
            ).status_code == 200
        finally:
            reset_principal_seams()

    async def test_a_normal_principal_cannot_reach_a_superuser_route(
        self, guarded_app, authenticated_client
    ) -> None:
        assert (await authenticated_client.get("/_t/superuser")).status_code == 403


class TestUnregisteredSeam:
    async def test_a_missing_loader_fails_loudly_rather_than_allowing(
        self, guarded_app, permissive_client
    ) -> None:
        """The alternative — treating the caller as anonymous — is a bypass."""
        from app.core.principal import reset_principal_seams
        from app.core.security import create_access_token

        reset_principal_seams()
        token = create_access_token("someone")
        response = await permissive_client.get(
            "/_t/principal", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 500
