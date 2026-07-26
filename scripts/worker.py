"""Background job worker entry point.

Why this file exists
--------------------
The API process serves requests; this process runs deferred work. Keeping them
separate matters operationally: they scale on different signals (request rate
versus queue depth), a runaway job cannot starve request handling, and a deploy
can drain one without dropping the other.

Usage::

    uv run python scripts/worker.py
    uv run python scripts/worker.py --concurrency 8 --name worker-1

Shuts down gracefully on SIGTERM — the signal an orchestrator sends before
stopping a container. In-flight jobs finish and are acknowledged rather than
being left pending for another worker to reclaim minutes later.
"""

import argparse
import asyncio
import contextlib
import signal
import socket
import sys
from pathlib import Path

# Allow running as a plain script (`python scripts/worker.py`) rather than only
# as a module, which is what a container CMD and a developer both reach for.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.discovery import import_side_effect_modules
from app.core.logging import configure_logging, get_logger
from app.infrastructure.queue.client import tasks
from app.infrastructure.queue.worker import Worker
from app.infrastructure.redis.client import close_redis, init_redis

logger = get_logger("worker")


def load_task_modules() -> None:
    """Import every module that registers task handlers.

    Handlers register via a decorator, which only runs on import. A worker that
    has not imported a task module rejects its jobs as unknown and dead-letters
    them — silently losing work that the producer believes was accepted.

    Discovery removes the hand-maintained list; see :mod:`app.core.discovery`.
    """
    loaded = import_side_effect_modules()
    logger.info(
        "Task modules loaded",
        extra={"features": {name: list(subs) for name, subs in loaded.items()}},
    )


async def run(name: str, concurrency: int) -> None:
    """Start the worker and run until interrupted."""
    configure_logging()
    load_task_modules()

    await init_redis()
    worker = Worker(tasks, name=name, concurrency=concurrency)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)

    logger.info(
        "Worker process starting",
        extra={"worker": name, "environment": settings.app.environment},
    )
    try:
        await worker.run()
    finally:
        await close_redis()
        logger.info("Worker process stopped")


def main() -> None:
    """Parse arguments and run the worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        default=f"worker-{socket.gethostname()}",
        help="Consumer name. Must be unique per process: Redis tracks pending "
        "messages per consumer, so duplicates break stalled-job reclamation.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Jobs processed simultaneously (default: 4).",
    )
    args = parser.parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.name, args.concurrency))


if __name__ == "__main__":
    main()
