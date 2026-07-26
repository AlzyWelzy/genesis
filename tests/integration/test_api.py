"""End-to-end tests of the HTTP surface.

Exercised through the real ASGI stack — middleware, exception handlers, routing —
because that is where these behaviours actually live. Testing the handler
functions directly would prove nothing about the envelope a client receives, the
headers the middleware attaches, or the order they run in.

No socket is opened: ``ASGITransport`` calls the app in-process.
"""

import pytest

from app.core.exceptions import (
    AuthorizationError,
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)

pytestmark = pytest.mark.integration


class TestHealthProbes:
    async def test_live_answers_without_touching_dependencies(self, client) -> None:
        """A liveness probe that checked the database would restart the fleet."""
        response = await client.get("/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    async def test_health_reports_identity(self, client) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["service"]
        assert body["version"]

    async def test_health_leaks_no_infrastructure_detail(self, client) -> None:
        """One of the most-scanned URLs on any public service."""
        body = (await client.get("/health")).json()
        serialised = str(body)
        assert "postgres" not in serialised.lower()
        assert "redis" not in serialised.lower()
        assert "://" not in serialised

    async def test_probes_are_hidden_from_the_schema(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/live" not in paths
        assert "/ready" not in paths


class TestJWKS:
    async def test_publishes_the_public_key(self, client) -> None:
        body = (await client.get("/.well-known/jwks.json")).json()
        assert body["keys"]
        assert body["keys"][0]["kty"] == "OKP"

    async def test_never_serves_private_material(self, client) -> None:
        text = (await client.get("/.well-known/jwks.json")).text
        assert "PRIVATE" not in text
        assert '"d"' not in text


class TestErrorEnvelope:
    async def test_unknown_route_uses_the_standard_envelope(self, client) -> None:
        response = await client.get("/api/v1/does-not-exist")
        assert response.status_code == 404

        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["message"]
        assert error["request_id"]

    async def test_every_error_carries_a_request_id(self, client) -> None:
        """Without it, a user report cannot be tied to a log line."""
        body = (await client.get("/api/v1/nope")).json()
        assert body["error"]["request_id"]

    @pytest.mark.parametrize(
        ("error", "expected_status", "expected_code"),
        [
            (NotFoundError, 404, "not_found"),
            (AuthorizationError, 403, "permission_denied"),
            (ValidationError, 422, "validation_error"),
            (BusinessRuleError, 409, "business_rule_violation"),
        ],
    )
    async def test_domain_errors_map_to_the_right_status(
        self, app, client, error, expected_status, expected_code
    ) -> None:
        """Services raise domain errors; the edge decides the HTTP status."""

        @app.get("/_test/raise")
        async def raising() -> None:
            raise error()

        response = await client.get("/_test/raise")
        assert response.status_code == expected_status
        assert response.json()["error"]["code"] == expected_code

    async def test_an_unhandled_exception_leaks_nothing(
        self, app, permissive_client
    ) -> None:
        """Exception text is how connection strings reach a customer's console."""

        @app.get("/_test/boom")
        async def boom() -> None:
            raise RuntimeError("secret connection string postgres://user:pw@host")

        response = await permissive_client.get("/_test/boom")
        assert response.status_code == 500

        error = response.json()["error"]
        assert error["code"] == "internal_error"
        assert "postgres://" not in response.text
        assert "secret" not in response.text
        # The request ID is the only thing a client gets, and it is enough.
        assert error["request_id"]

    async def test_validation_errors_name_the_offending_field(
        self, app, client
    ) -> None:
        """A 422 that does not say which field is wrong forces guesswork."""
        from pydantic import BaseModel

        class Body(BaseModel):
            count: int

        @app.post("/_test/validate")
        async def validate(body: Body) -> dict:
            return {"ok": True}

        response = await client.post("/_test/validate", json={"count": "not-an-int"})
        assert response.status_code == 422

        fields = response.json()["error"]["details"]["fields"]
        assert any(f["field"] == "count" for f in fields)


class TestRequestContext:
    async def test_request_id_is_echoed(self, client) -> None:
        response = await client.get("/health")
        assert response.headers["X-Request-ID"]

    async def test_each_request_gets_a_distinct_id(self, client) -> None:
        first = (await client.get("/health")).headers["X-Request-ID"]
        second = (await client.get("/health")).headers["X-Request-ID"]
        assert first != second

    async def test_correlation_id_is_propagated_from_upstream(self, client) -> None:
        """This is what ties one user action together across services."""
        response = await client.get(
            "/health", headers={"X-Correlation-ID": "trace-abc-123"}
        )
        assert response.headers["X-Correlation-ID"] == "trace-abc-123"

    async def test_correlation_id_defaults_to_the_request_id(self, client) -> None:
        response = await client.get("/health")
        assert response.headers["X-Correlation-ID"] == response.headers["X-Request-ID"]

    async def test_context_does_not_leak_between_requests(self, client) -> None:
        """A ContextVar set in a task outlives it unless explicitly reset."""
        await client.get("/health", headers={"X-Correlation-ID": "first"})
        response = await client.get("/health")
        assert response.headers["X-Correlation-ID"] != "first"


class TestOpenAPI:
    async def test_schema_is_served(self, client) -> None:
        assert (await client.get("/openapi.json")).status_code == 200

    async def test_operation_ids_are_stable_and_readable(self, client) -> None:
        """These become method names in every generated client."""
        schema = (await client.get("/openapi.json")).json()
        operation_ids = [
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
        ]
        assert "system_jwks" in operation_ids
        assert not any("_get" in oid and "api_v1" in oid for oid in operation_ids)

    async def test_security_scheme_is_declared(self, client) -> None:
        schema = (await client.get("/openapi.json")).json()
        assert "BearerAuth" in schema["components"]["securitySchemes"]

    async def test_error_envelope_example_is_published(self, client) -> None:
        schema = (await client.get("/openapi.json")).json()
        assert "ErrorEnvelope" in schema["components"]["examples"]


class TestMetricsEndpoint:
    async def test_returns_404_when_metrics_are_disabled(self, client) -> None:
        """An empty 200 would look like a healthy service reporting nothing."""
        response = await client.get("/metrics")
        assert response.status_code == 404

    async def test_exposes_prometheus_format_when_enabled(self, client) -> None:
        from app.infrastructure.observability.metrics import (
            HTTP_REQUESTS,
            PrometheusMetrics,
            set_metrics,
        )

        recorder = PrometheusMetrics()
        recorder.increment(HTTP_REQUESTS, method="GET", route="/x", status="200")
        set_metrics(recorder)

        response = await client.get("/metrics")
        assert response.status_code == 200
        assert HTTP_REQUESTS in response.text
