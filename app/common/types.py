"""Shared type aliases and base schema classes.

Why this file exists
--------------------
Two problems this solves. First, repeated annotations: ``Annotated[str,
Field(min_length=1)]`` written a hundred times will be written slightly
differently a hundred times. Second, schema configuration: every Pydantic model
in the application needs the same behaviour (reject unknown fields on input,
serialise from ORM objects on output), and that policy belongs in a base class,
not copied into every ``model_config``.

Feature schemas inherit from :class:`BaseSchema` and use these aliases; nothing
here knows about any specific feature.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --- Primitive aliases -----------------------------------------------------

#: Primary key type used across the application.
type ID = UUID

#: Timezone-aware timestamp. Naive datetimes must never cross a layer boundary.
type Timestamp = datetime

#: Arbitrary JSON object payload.
type JSONObject = dict[str, Any]

#: Non-empty, whitespace-trimmed string.
type NonEmptyStr = Annotated[str, Field(min_length=1, strip_whitespace=True)]

#: Slug-safe identifier.
type Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]

#: Zero or greater.
type NonNegativeInt = Annotated[int, Field(ge=0)]

#: One or greater.
type PositiveInt = Annotated[int, Field(ge=1)]


# --- Base schemas ----------------------------------------------------------


class BaseSchema(BaseModel):
    """Base for every Pydantic schema in the application.

    ``extra="forbid"`` is the important setting: silently ignoring an unknown
    field means a client typo (``emial``) is accepted and quietly dropped. A
    422 telling them the field is unknown is far kinder than data that never
    arrives.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class ORMSchema(BaseSchema):
    """Base for response schemas built from ORM instances.

    ``from_attributes=True`` allows ``Model.model_validate(orm_object)``, which
    is how a router converts what a service returned into a response. Extra
    fields are permitted here because the source is a trusted ORM object, not
    client input.
    """

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class TimestampedSchema(ORMSchema):
    """Response mixin exposing the audit columns from ``TimestampMixin``."""

    created_at: Timestamp
    updated_at: Timestamp


# TODO: add a `SecretStr`-aware serialiser so a schema can never accidentally
# emit a credential in a response body.
