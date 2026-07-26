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

from sqlalchemy import Enum as SAEnum


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


def enum_column(enum_type: type[StrEnum], *, length: int = 32) -> SAEnum:
    """Build the column type for a persisted enum.

    The decision, and why
    ---------------------
    PostgreSQL offers a native ``ENUM`` type. It is rejected here in favour of
    ``VARCHAR`` plus a ``CHECK`` constraint, which is what this helper builds
    (SQLAlchemy's ``Enum(..., native_enum=False)``).

    Native ``ENUM`` validates in the database and is compact, but adding a
    value requires ``ALTER TYPE``, which **cannot run inside a transaction**
    before PostgreSQL 12 and still cannot be rolled back afterwards. *Removing*
    or reordering a value means creating a new type, rewriting every column
    that uses it, and dropping the old one — on a large table, a long exclusive
    lock. Enum values change far more often than anyone predicts.

    ``VARCHAR`` + ``CHECK`` keeps database-side validation — an invalid value
    is still rejected — while making an added value a cheap constraint swap.
    The storage difference is irrelevant next to that.

    ``length`` is validated against the members so a value longer than the
    column silently truncating is impossible.

    Args:
        enum_type: The ``StrEnum`` being persisted.
        length: VARCHAR length. Must fit every member.

    Returns:
        The SQLAlchemy column type.

    Raises:
        ValueError: When a member does not fit in ``length``.
    """
    longest = max(len(member.value) for member in enum_type)
    if longest > length:
        raise ValueError(
            f"{enum_type.__name__} has a {longest}-character member but "
            f"length={length}; widen the column or shorten the value."
        )

    return SAEnum(
        enum_type,
        native_enum=False,
        length=length,
        # Store the *value*, not the Python member name. The value is the
        # public contract — it appears in APIs and exports — while the member
        # name is an implementation detail that should stay renameable.
        values_callable=lambda enum: [member.value for member in enum],
        # Named so the CHECK constraint is deterministic and Alembic can drop it.
        create_constraint=True,
        name=f"ck_{enum_type.__name__.lower()}",
    )
