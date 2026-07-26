"""Populate a database with development data.

Why this file exists
--------------------
"Clone the repo and start building" only works if there is data to build
against. Without a seed script every developer hand-crafts their own fixtures,
those fixtures differ, and a bug that only reproduces on one machine costs an
afternoon to explain.

Safety
------
This script **refuses to run in production**. That check is not paranoia: a
seed script writes fabricated rows, and the most common way it reaches
production is a misconfigured ``DATABASE__URL`` in a terminal someone forgot
they had open. The guard costs one line and removes the failure mode.

It is also idempotent — running it twice must not double the data — and it
reuses the application's own services rather than writing SQL, so seeded rows
go through the same validation as real ones. A seed script with its own INSERT
statements drifts from the application and produces data the application would
have rejected.

Usage::

    uv run python scripts/seed.py
    uv run python scripts/seed.py --reset      # truncate first
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.database.session import session_scope

logger = get_logger("seed")


def guard_environment() -> None:
    """Refuse to run anywhere that could be real.

    Raises:
        SystemExit: When the target environment is production.
    """
    if settings.app.is_production:
        raise SystemExit(
            "Refusing to seed a production environment. "
            f"APP__ENVIRONMENT={settings.app.environment}"
        )


async def reset(_session: object) -> None:
    """Remove existing seed data.

    Deletes in reverse dependency order so foreign keys are never violated
    mid-way. Truncating tables in an arbitrary order is how a reset half-runs
    and leaves the database in a state neither the script nor the app expects.
    """
    logger.warning("Reset requested")
    # Stage 2: delete seeded rows in reverse dependency order, so a foreign
    # key is never violated mid-way. Truncating in arbitrary order is how a
    # reset half-runs and leaves a state neither the script nor the app expects.


async def seed() -> None:
    """Create the development dataset.

    Every step must be idempotent — check before insert, or upsert — so this
    can be re-run after adding a new fixture without duplicating the old ones.
    """
    # Stage 2: seed through the feature services in dependency order —
    # tenants -> users -> memberships -> feature records. Use the service layer,
    # never raw SQL: seeded rows must satisfy the same invariants the
    # application enforces, or they exercise code paths real data never reaches.
    logger.info("Nothing to seed yet: no feature modules exist")


async def run(*, do_reset: bool) -> None:
    """Run the seed, wrapped in a single transaction."""
    configure_logging()
    guard_environment()

    logger.info("Seeding database", extra={"environment": settings.app.environment})
    async with session_scope() as session:
        if do_reset:
            await reset(session)
        await seed()
    logger.info("Seed complete")


def main() -> None:
    """Parse arguments and run the seed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing seed data before inserting. Destructive.",
    )
    args = parser.parse_args()
    asyncio.run(run(do_reset=args.reset))


if __name__ == "__main__":
    main()
