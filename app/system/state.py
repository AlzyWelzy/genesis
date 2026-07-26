"""Process lifecycle state.

Why this file exists
--------------------
The startup probe has to answer "has initialisation finished?", and that fact
lives in the lifespan. Reaching into the lifespan from a route handler would
couple the two; a module-level flag is the smallest thing that decouples them.

It is a plain boolean rather than anything richer on purpose. Startup either
completed or it did not, and a probe endpoint must not itself be able to fail.
"""

_started = False


def mark_started() -> None:
    """Record that initialisation finished. Called at the end of the lifespan."""
    global _started  # noqa: PLW0603 - process-wide flag by design
    _started = True


def mark_stopped() -> None:
    """Record that shutdown has begun.

    Flipping this first means the startup probe reports "not started" while the
    process drains, so an orchestrator does not route new traffic to an
    instance that is on its way out.
    """
    global _started  # noqa: PLW0603 - process-wide flag by design
    _started = False


def is_started() -> bool:
    """Whether the application has finished initialising."""
    return _started
