"""Container health probe.

Why this file exists
--------------------
A ``HEALTHCHECK`` in a Dockerfile needs a command that exits 0 or 1. Reaching
for ``curl`` means installing it into the image purely for this — extra bytes
and extra CVE surface in a container that otherwise has no HTTP client.

This uses the interpreter that is already there.

Usage::

    python scripts/healthcheck.py             # checks /ready
    python scripts/healthcheck.py --path /live

Exit codes: ``0`` healthy, ``1`` unhealthy. Nothing else — an orchestrator
treats every non-zero code identically, so there is no value in more.
"""

import argparse
import sys
import urllib.error
import urllib.request

#: The only status treated as healthy. A 503 from /ready is a deliberate
#: "not ready" signal, not an error to be tolerated.
HTTP_OK = 200

#: Deliberately short. A probe that hangs is indistinguishable from a hung
#: process, and the orchestrator's own timeout is the only thing that saves it.
DEFAULT_TIMEOUT_SECONDS = 3


def check(url: str, timeout: int) -> bool:
    """Request a probe endpoint and report whether it answered 200.

    Args:
        url: Full probe URL.
        timeout: Seconds to wait before giving up.

    Returns:
        ``True`` when the endpoint returned 200.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status == HTTP_OK
    except urllib.error.URLError, TimeoutError, OSError:
        # Any failure to reach our own port means unhealthy. The reason is not
        # worth distinguishing: the orchestrator's only lever is restart.
        return False


def main() -> int:
    """Run the probe and return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--path",
        default="/ready",
        help="Probe path. /ready checks dependencies; /live checks only that "
        "the process responds.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}{args.path}"
    healthy = check(url, args.timeout)
    if not healthy:
        print(f"unhealthy: {url}", file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
