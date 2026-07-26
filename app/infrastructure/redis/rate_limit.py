"""Distributed rate limiting.

Why this file exists
--------------------
An in-process rate limiter is close to useless behind a load balancer: with
four replicas the effective limit is four times the configured one, and it
resets on every deploy. The counter has to live somewhere shared, which in this
stack means Redis.

Why a sliding window
--------------------
A fixed window ("100 requests per minute", counter reset on the minute) permits
a burst of 200 spanning the boundary — 100 at 11:59:59 and 100 at 12:00:00 —
which is exactly the traffic shape the limit exists to prevent.

A sliding window log stores each request's timestamp in a sorted set and counts
what falls inside the trailing window. Precise, and cheap enough at these
volumes. The Redis operations run in one pipeline so two concurrent requests
cannot both read a stale count and both proceed.

Failure policy
--------------
Configurable, defaulting to fail-open. A limiter that rejects all traffic when
Redis blinks has converted a cache outage into a full outage. For a limit
protecting against accidents and runaway clients that trade is wrong; for one
protecting a genuinely expensive operation, invert it deliberately.
"""

import time
from dataclasses import dataclass

from app.common.utils.crypto import generate_token
from app.core.config import settings
from app.core.logging import get_logger
from app.infrastructure.redis.client import build_key, get_redis

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of an allowance check.

    Carries the numbers needed for the ``X-RateLimit-*`` response headers.
    Publishing them lets a well-behaved client slow down before being rejected,
    which is worth far more than rejecting it politely afterwards.

    Attributes:
        allowed: Whether the request may proceed.
        limit: Requests permitted per window.
        remaining: Requests left in the current window.
        reset_after: Seconds until the window frees up.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_after: int

    def headers(self) -> dict[str, str]:
        """Render the standard rate-limit response headers."""
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_after),
        }


async def check_rate_limit(
    identity: str, *, limit: int, window_seconds: int
) -> RateLimitResult:
    """Check and consume one unit of a caller's allowance.

    Args:
        identity: What the limit is keyed by — an authenticated user ID where
            available, otherwise the client IP taken from the proxy's forwarded
            header. Never the raw socket address: behind a proxy every request
            appears to come from the proxy, so one caller's limit would throttle
            everyone.
        limit: Requests permitted per window.
        window_seconds: Length of the sliding window.

    Returns:
        The result, including the values for the response headers.
    """
    key = build_key("ratelimit", identity)
    now = time.time()
    window_start = now - window_seconds

    try:
        client = get_redis()
        async with client.pipeline(transaction=True) as pipe:
            # Drop entries that have aged out of the window.
            pipe.zremrangebyscore(key, 0, window_start)
            # Count what remains — this is the usage *before* this request.
            pipe.zcard(key)
            # Record this request. The member must be unique: two requests in
            # the same millisecond would otherwise collide into one sorted-set
            # entry and be undercounted.
            pipe.zadd(key, {f"{now}:{generate_token(8)}": now})
            # Let an idle key disappear rather than leaking one per caller
            # forever. Padded by a second so it cannot expire mid-window.
            pipe.expire(key, window_seconds + 1)
            _, used, _, _ = await pipe.execute()
    except Exception:  # noqa: BLE001 - policy decides what an outage means
        if settings.rate_limit.fail_open:
            return fail_open(identity, limit)
        logger.warning(
            "Rate limiter unavailable; failing closed", extra={"identity": identity}
        )
        return RateLimitResult(
            allowed=False, limit=limit, remaining=0, reset_after=window_seconds
        )

    # `used` excludes the request just recorded, so allow when it is below the
    # limit — the Nth request is permitted, the N+1th is not.
    allowed = used < limit
    remaining = max(0, limit - used - 1)
    return RateLimitResult(
        allowed=allowed,
        limit=limit,
        remaining=remaining,
        reset_after=window_seconds,
    )


def resolve_limit(*, authenticated: bool) -> int:
    """Return the configured allowance for a caller.

    Authenticated callers get a much higher limit: they are attributable and
    limitable per account, whereas an anonymous caller is identified only by an
    IP that may be shared by an entire office behind one NAT.
    """
    return (
        settings.rate_limit.authenticated_per_minute
        if authenticated
        else settings.rate_limit.anonymous_per_minute
    )


def fail_open(identity: str, limit: int) -> RateLimitResult:
    """Build the permissive result used when Redis is unreachable.

    Logged at WARNING rather than swallowed: an unenforced rate limit is a
    condition someone should know about, even though the request proceeds.
    """
    logger.warning(
        "Rate limiter unavailable; failing open", extra={"identity": identity}
    )
    return RateLimitResult(allowed=True, limit=limit, remaining=limit, reset_after=0)


async def reset_rate_limit(identity: str) -> None:
    """Clear a caller's recorded history.

    For support ("unblock this customer") and for tests. Not exposed through
    the API — a rate limit a caller can reset is not a rate limit.
    """
    try:
        await get_redis().delete(build_key("ratelimit", identity))
    except Exception:  # noqa: BLE001 - best effort
        logger.warning("Rate limit reset failed", extra={"identity": identity})


@dataclass(frozen=True, slots=True)
class EndpointLimit:
    """A rate limit that applies to one route rather than globally.

    The global limit protects the service from a runaway client. It is the
    wrong instrument for a single expensive endpoint: setting it low enough to
    protect a report generator would throttle ordinary reads, and setting it
    high enough for ordinary reads leaves the report generator unprotected.

    Attributes:
        limit: Requests permitted per window.
        window_seconds: Sliding window length.
        burst: When set, uses a token bucket permitting this many requests
            back-to-back while holding the long-run average to ``limit``.
    """

    limit: int
    window_seconds: int = 60
    burst: int | None = None


#: Per-route overrides, keyed by the route *template* — never the resolved
#: path, or every distinct ID would get its own allowance.
ENDPOINT_LIMITS: dict[str, EndpointLimit] = {}


def register_endpoint_limit(route: str, limit: EndpointLimit) -> None:
    """Declare a tighter limit for one route.

    Called at import time by the feature that owns the route, so the limit
    lives next to the expensive endpoint rather than in a distant config file
    that nobody updates when the endpoint changes.
    """
    ENDPOINT_LIMITS[route] = limit


def limit_for_route(route: str, *, authenticated: bool) -> EndpointLimit:
    """Return the limit that applies to a route, falling back to the global one."""
    if override := ENDPOINT_LIMITS.get(route):
        return override
    return EndpointLimit(
        limit=resolve_limit(authenticated=authenticated),
        window_seconds=settings.rate_limit.window_seconds,
    )


async def check_token_bucket(
    identity: str, *, rate: float, burst: int, window_seconds: int = 60
) -> RateLimitResult:
    """Check an allowance using a token bucket rather than a sliding window.

    When to prefer this
    -------------------
    A sliding window is the right default: it is precise and easy to reason
    about. A token bucket is better where a *short burst is legitimate but a
    high sustained rate is not* — a client syncing fifty records on startup and
    then going quiet, or a bulk import that arrives in waves.

    Under a sliding window, that client must either be given a limit high
    enough for its burst (leaving it unthrottled the rest of the time) or be
    rejected for behaviour that is perfectly reasonable.

    How it works
    ------------
    The bucket holds at most ``burst`` tokens and refills at ``rate`` tokens
    per second. Each request costs one token. A caller can spend the whole
    bucket at once, then proceeds at the refill rate.

    State is two fields — token count and last-refill time — updated in one
    atomic Lua script, so two concurrent requests cannot both read a stale
    count and both take the last token.

    Args:
        identity: What the limit is keyed by.
        rate: Tokens added per second — the sustained rate.
        burst: Bucket capacity — the largest permissible burst.
        window_seconds: How long an idle bucket is retained.

    Returns:
        The result, including values for the response headers.
    """
    key = build_key("bucket", identity)
    now = time.time()

    try:
        # Lua so read-modify-write is atomic. Splitting it into GET/SET would
        # let concurrent requests both observe the same token count.
        tokens_remaining = await get_redis().eval(
            _BUCKET_LUA,
            1,
            key,
            str(rate),
            str(burst),
            str(now),
            str(window_seconds),
        )
    except Exception:  # noqa: BLE001 - policy decides what an outage means
        if settings.rate_limit.fail_open:
            return fail_open(identity, burst)
        return RateLimitResult(
            allowed=False, limit=burst, remaining=0, reset_after=window_seconds
        )

    remaining = int(tokens_remaining)
    allowed = remaining >= 0
    return RateLimitResult(
        allowed=allowed,
        limit=burst,
        remaining=max(0, remaining),
        # Time until one token is available again, which is what a rejected
        # caller actually needs to know.
        reset_after=max(1, int(1 / rate)) if not allowed else 0,
    )


#: Atomic token-bucket update, as Lua so read-modify-write cannot interleave.
#: (Named ``_BUCKET_LUA`` rather than ``..._TOKEN_...`` so the secret scanner
#: does not mistake a Lua script for a credential.)
#:
#: Returns the token count remaining after this request, or -1 when the bucket
#: was empty. Refill is computed from elapsed time rather than a timer, so an
#: idle bucket costs nothing and needs no background process.
_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'updated')
local tokens = tonumber(state[1])
local updated = tonumber(state[2])

if tokens == nil then
  tokens = burst
  updated = now
end

-- Refill for the time that has passed, capped at the bucket size.
local elapsed = math.max(0, now - updated)
tokens = math.min(burst, tokens + elapsed * rate)

if tokens < 1 then
  redis.call('HMSET', key, 'tokens', tokens, 'updated', now)
  redis.call('EXPIRE', key, ttl)
  return -1
end

tokens = tokens - 1
redis.call('HMSET', key, 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', key, ttl)
return math.floor(tokens)
"""
