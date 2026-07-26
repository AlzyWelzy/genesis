"""Redis-backed caching, pub/sub and shared ephemeral state.

One connection pool (``client``) serves three distinct concerns — caching
(``cache``), broadcast messaging (``pubsub``) and, later, rate limiting and
distributed locks. Keeping them behind separate abstractions prevents a feature
from reaching for raw Redis commands and quietly inventing a fourth key scheme.
"""
