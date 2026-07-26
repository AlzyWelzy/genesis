"""Development server entry point.

Why this file exists
--------------------
A convenience wrapper so ``uv run python main.py`` starts a reloading server
without anyone memorising uvicorn flags. It is **development only**.

In deployment, uvicorn is invoked directly by the container command with
``--workers`` and no reloader — see ``Dockerfile``. The reloader spawns a
watcher process and reimports on every file change, which is wrong for
production in three separate ways: it doubles memory, it breaks the lifespan's
resource ownership, and it makes shutdown non-deterministic.

The application itself is built in :mod:`app.main`. Nothing here belongs there.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        # Binds all interfaces so the server is reachable from a container or
        # another device on the LAN. Safe here because this path is never used
        # in deployment; the production command binds explicitly.
        host="0.0.0.0",  # noqa: S104 - development server only
        port=8000,
        reload=True,
    )
