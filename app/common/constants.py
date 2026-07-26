"""Application-wide constants.

Why this file exists
--------------------
A magic number repeated in four modules will be changed in three of them. This
file holds values that are genuinely global and genuinely fixed — limits,
header names, formats — so a change is one edit and a review can see it.

What belongs here
-----------------
Constants used across *multiple* features and not configurable per environment.

What does not
-------------
* Anything that varies by environment → :mod:`app.core.settings`.
* Anything used by one feature → that module's ``constants.py``. A shared file
  that accumulates single-use constants becomes a dependency magnet linking
  every module to every other.
* Enumerated value sets → :mod:`app.common.enums`.
"""

from typing import Final

# --- Pagination ------------------------------------------------------------

#: Page size used when a request does not specify one.
DEFAULT_PAGE_SIZE: Final[int] = 20

#: Hard ceiling on page size. Unbounded pages are a denial-of-service vector: a
#: client asking for 1,000,000 rows exhausts memory and the connection pool.
MAX_PAGE_SIZE: Final[int] = 100

# --- Headers ---------------------------------------------------------------

#: Identifies a single HTTP request. Minted per request, echoed on the response.
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"

#: Shared across every hop of one logical operation. Propagated from an
#: upstream caller when present — this is what makes a user action traceable
#: across service boundaries.
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-ID"

#: Standard bearer-token header.
AUTHORIZATION_HEADER: Final[str] = "Authorization"

#: Scheme prefix expected in the authorization header.
BEARER_PREFIX: Final[str] = "Bearer "

#: Client-supplied key making an unsafe request safely retryable.
IDEMPOTENCY_KEY_HEADER: Final[str] = "Idempotency-Key"

# --- Limits ----------------------------------------------------------------

#: Largest accepted request body (10 MiB). Enforce at the proxy as well: by the
#: time the application sees an oversized body it has already been buffered.
MAX_REQUEST_BODY_BYTES: Final[int] = 10 * 1024 * 1024

#: Chunk size for streaming uploads and downloads (64 KiB).
STREAM_CHUNK_SIZE: Final[int] = 64 * 1024

#: Largest number of items accepted in a bulk request.
MAX_BULK_ITEMS: Final[int] = 500

# --- Formats ---------------------------------------------------------------

#: Canonical timestamp format for anything not serialised by Pydantic.
ISO_8601_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"

#: Placeholder substituted for secrets in logs and error payloads.
REDACTED: Final[str] = "[REDACTED]"

#: Field names scrubbed from logged request/response bodies and log extras.
#: Matched case-insensitively. Add to this list eagerly — the cost of redacting
#: something harmless is zero, and the cost of missing something is a credential
#: in a log aggregator that a dozen people can search.
SENSITIVE_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "secret",
        "client_secret",
        "api_key",
        "private_key",
        "authorization",
        "cookie",
        "set-cookie",
        "session",
        "credit_card",
        "card_number",
        "cvv",
        "ssn",
    }
)
