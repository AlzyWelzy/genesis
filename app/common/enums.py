"""Enumerations shared across features.

Why this file exists
--------------------
An enum is a contract with the database (stored values), with clients (API
strings) and with the code (comparisons). Defining shared ones once prevents
the classic failure where two modules spell the same status differently and a
filter silently matches nothing.

Rules
-----
* Inherit from ``StrEnum`` so members serialise as plain strings in JSON and
  compare equal to strings — no ``.value`` noise at call sites.
* **Member values are permanent.** They are persisted in the database and
  published in the API; renaming one is a data migration plus a breaking API
  change, not a refactor. Rename the *member* freely, never the value.
* Feature-specific enums (``SubscriptionTier``, ``TicketPriority``) belong in
  that module's ``enums.py`` or ``constants.py``, not here.
"""

from enum import StrEnum


class SortOrder(StrEnum):
    """Direction for an ordered query."""

    ASC = "asc"
    DESC = "desc"


class Status(StrEnum):
    """Generic lifecycle state for records that are enabled or withdrawn.

    Only use this where the three states are genuinely sufficient. A domain
    with its own lifecycle (draft → review → published) deserves its own enum;
    forcing it into this one loses meaning and invites invalid transitions.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class Environment(StrEnum):
    """Deployment environment names.

    Mirrors the literal type in :mod:`app.core.settings`; use this enum where a
    runtime comparison reads better than a string literal.
    """

    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class JobStatus(StrEnum):
    """Lifecycle of a background job.

    Shared because job state is reported through generic endpoints and admin
    tooling rather than owned by one feature.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# TODO: when an enum is persisted, decide between a native PostgreSQL ENUM type
# (validated by the database, but every change needs a migration) and a VARCHAR
# with a CHECK constraint (cheaper to evolve). Record the choice here.
