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

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    SecretStr,
    field_serializer,
)
from pydantic.functional_serializers import SerializerFunctionWrapHandler
from pydantic_core.core_schema import FieldSerializationInfo

from app.common.constants import REDACTED, SENSITIVE_FIELD_NAMES

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


class SafeResponseSchema(ORMSchema):
    """Response base that refuses to serialise secrets.

    The failure this prevents is mundane and severe: someone adds
    ``api_key: SecretStr`` to a model, a response schema is built from that
    model with ``from_attributes``, and the key ships in a JSON body. Pydantic
    renders a bare ``SecretStr`` as ``"**********"`` in ``model_dump()`` but
    **not** in ``model_dump_json()`` unless told to, and the JSON path is the
    one an API uses.

    Inheriting from this makes the safe behaviour the default. A field that
    genuinely must be returned — a token the client just asked for — should be
    a plain ``str`` on a schema that says so in its name, so the decision is
    visible in review rather than implied by a type.
    """

    model_config = ConfigDict(
        from_attributes=True,
        extra="ignore",
        # Applies to JSON serialisation too, which is the gap that leaks.
        ser_json_bytes="base64",
    )

    @field_serializer("*", mode="wrap", check_fields=False)
    def _mask_secrets(
        self,
        value: Any,
        handler: SerializerFunctionWrapHandler,
        _info: FieldSerializationInfo,
    ) -> Any:
        """Replace any ``SecretStr``/``SecretBytes`` with a fixed placeholder."""
        if isinstance(value, SecretStr | SecretBytes):
            return REDACTED
        return handler(value)


def assert_no_secrets(payload: Mapping[str, Any], *, where: str = "response") -> None:
    """Raise if a payload contains a value that looks like a live credential.

    A belt-and-braces check for the highest-risk endpoints, and a useful
    assertion in tests. Deliberately *not* wired into every response: scanning
    every payload on every request costs more than it saves, and the schema
    layer is the right place to be correct.

    Args:
        payload: The serialised body.
        where: Included in the error, to say which response was at fault.

    Raises:
        RuntimeError: When a sensitive key carries a non-redacted value.
    """
    for key, value in payload.items():
        if key.lower() in SENSITIVE_FIELD_NAMES and value not in (None, REDACTED):
            raise RuntimeError(
                f"Refusing to emit {where}: field {key!r} looks like a credential."
            )
