"""Tests for the operational endpoints beyond the basic probes.

The startup-probe tests encode a distinction that causes real outages when it
is missed: an orchestrator kills a container whose *liveness* probe fails, but
merely waits on a *startup* probe. Conflating them turns a slow boot into a
crash loop.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def _clean_state():
    """Startup state is a process-wide flag; restore it after each test."""
    from app.system.state import is_started, mark_started, mark_stopped

    original = is_started()
    yield
    (mark_started if original else mark_stopped)()


@pytest.mark.usefixtures("_clean_state")
class TestStartupProbe:
    async def test_reports_starting_before_the_lifespan_completes(self, client) -> None:
        from app.system.state import mark_stopped

        mark_stopped()
        response = await client.get("/startup")

        assert response.status_code == 503
        assert response.json()["status"] == "starting"

    async def test_reports_started_afterwards(self, client) -> None:
        from app.system.state import mark_started

        mark_started()
        response = await client.get("/startup")

        assert response.status_code == 200
        assert response.json()["status"] == "started"

    async def test_shutdown_flips_it_back(self, client) -> None:
        """So an orchestrator stops routing to an instance that is draining."""
        from app.system.state import mark_started, mark_stopped

        mark_started()
        mark_stopped()

        assert (await client.get("/startup")).status_code == 503

    async def test_liveness_is_unaffected_by_startup_state(self, client) -> None:
        """Liveness must not fail during boot, or the container is killed."""
        from app.system.state import mark_stopped

        mark_stopped()
        assert (await client.get("/live")).status_code == 200

    async def test_the_probe_is_hidden_from_the_schema(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/startup" not in paths
