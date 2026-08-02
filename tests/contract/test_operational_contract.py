"""Promises made to the orchestrator and to operators, locked.

Why this exists
---------------
``/live``, ``/ready`` and ``/startup`` are a contract with Kubernetes, and the
three mean different things: readiness false *deregisters* a pod, liveness false
*kills* it, startup gates the other two while a slow boot completes. Renaming a
path or changing which one consults dependencies is not a refactor — it changes
what the orchestrator does to a running deployment, and it does so on the next
rollout rather than at the point of the edit.

``/metrics`` is the same kind of promise to a scrape config that lives in another
repository.
"""

import pytest

pytestmark = pytest.mark.integration

#: Paths an orchestrator or scraper is configured to call. Renaming one is a
#: deployment-manifest change in another repository, not a local refactor.
OPERATIONAL_PATHS = ("/live", "/ready", "/startup", "/health")


class TestProbeEndpoints:
    @pytest.mark.parametrize("path", OPERATIONAL_PATHS)
    async def test_the_probe_path_is_routed(self, client, path: str) -> None:
        """A renamed probe fails a deployment, not a test — unless this runs.

        Asserted by *calling* the path rather than by inspecting ``app.routes``.
        FastAPI does not flatten ``include_router``, so the top-level list holds
        an opaque wrapper and none of the real paths — inspecting it would have
        reported every probe as missing while all four work perfectly.

        Any status other than 404 proves the path is routed; the probes'
        behaviour is covered in the dependency-failure suite.
        """
        response = await client.get(path)

        assert response.status_code != 404

    async def test_liveness_answers_without_touching_a_dependency(self, client) -> None:
        """Liveness must never consult PostgreSQL or Redis.

        Reporting a dependency outage as liveness gets every healthy replica
        killed during a database blip, converting a partial outage into a total
        one. This passes with no services running, which is the assertion.
        """
        assert (await client.get("/live")).status_code == 200

    async def test_probes_are_exempt_from_rate_limiting(self) -> None:
        """A throttled probe gets healthy pods killed by the orchestrator."""
        from app.core.config import settings

        for path in ("/health", "/live", "/ready"):
            assert path in settings.rate_limit.exempt_paths

    async def test_probes_are_excluded_from_the_access_log(self) -> None:
        """They fire every few seconds per replica and would drown the log."""
        from app.core.config import settings

        for path in ("/health", "/live", "/ready"):
            assert path in settings.logging.exclude_paths


class TestCorrelationHeaders:
    """The headers a support query starts from.

    A user quotes an ID from an error response; support greps for it. Renaming
    either header breaks that path silently — every log line still has *a*
    correlation ID, just not the one the user was shown.
    """

    async def test_the_request_id_header_is_returned(self, client) -> None:
        from app.common.constants import REQUEST_ID_HEADER

        response = await client.get("/live")

        assert response.headers[REQUEST_ID_HEADER]

    async def test_the_correlation_id_header_is_returned(self, client) -> None:
        from app.common.constants import CORRELATION_ID_HEADER

        response = await client.get("/live")

        assert response.headers[CORRELATION_ID_HEADER]

    async def test_an_inbound_correlation_id_is_honoured(self, client) -> None:
        """A user action fanning out across services must share one ID."""
        from app.common.constants import CORRELATION_ID_HEADER

        response = await client.get(
            "/live", headers={CORRELATION_ID_HEADER: "from-upstream"}
        )

        assert response.headers[CORRELATION_ID_HEADER] == "from-upstream"

    async def test_the_header_names_have_not_changed(self) -> None:
        """Locked: another service's client library sends these by name."""
        from app.common.constants import CORRELATION_ID_HEADER, REQUEST_ID_HEADER

        assert REQUEST_ID_HEADER == "X-Request-ID"
        assert CORRELATION_ID_HEADER == "X-Correlation-ID"


class TestRateLimitHeaders:
    """Published on every response so a client can slow down before a 429."""

    async def test_the_header_names_are_the_conventional_ones(self) -> None:
        from app.infrastructure.redis.rate_limit import RateLimitResult

        headers = RateLimitResult(
            allowed=True, limit=10, remaining=9, reset_after=60
        ).headers()

        assert set(headers) == {
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        }

    async def test_the_values_are_plain_integers(self) -> None:
        """A client parses these; a unit suffix would break it."""
        from app.infrastructure.redis.rate_limit import RateLimitResult

        headers = RateLimitResult(
            allowed=False, limit=10, remaining=0, reset_after=42
        ).headers()

        assert all(value.isdigit() for value in headers.values())


class TestApiVersioning:
    async def test_routes_are_mounted_under_a_version(self) -> None:
        """An unversioned public route cannot be changed without breaking someone."""
        from app.core.config import settings

        assert settings.app.version_prefix.startswith("/api/")
        assert settings.app.version_prefix.count("/") == 2

    async def test_the_current_prefix_is_v1(self) -> None:
        """Locked: every client's base URL contains this."""
        from app.core.config import settings

        assert settings.app.version_prefix == "/api/v1"
