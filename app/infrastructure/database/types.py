"""Shared column types and the Python-to-SQL annotation map.

Why this file exists
--------------------
SQLAlchemy's defaults are portable rather than optimal for PostgreSQL: ``str``
becomes an unbounded ``VARCHAR``, ``datetime`` becomes a *naive* ``TIMESTAMP``
and ``dict`` becomes ``JSON`` instead of ``JSONB``. Left alone, those defaults
produce a schema that loses timezone information and cannot be indexed well.

Declaring the mapping once here means models say what they *mean*
(``Mapped[datetime]``) and always get the right physical type. It also gives a
home for reusable annotated aliases so column definitions stay short and
consistent across hundreds of models.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final

from sqlalchemy import DateTime, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import mapped_column

#: Timezone-aware timestamp. Every datetime stored by the application must be
#: UTC and aware — naive timestamps are the root of most date bugs at scale.
type UTCDateTime = Annotated[datetime, mapped_column(DateTime(timezone=True))]

#: Unbounded text. Prefer this over a very large VARCHAR; in PostgreSQL they
#: have identical storage but TEXT states the intent.
type LongText = Annotated[str, mapped_column(Text)]

#: Short identifiers, slugs and enum-like values.
type ShortStr = Annotated[str, mapped_column(String(64))]

#: Human-facing names and titles.
type NameStr = Annotated[str, mapped_column(String(255))]

#: Email addresses. Length follows the practical RFC 5321 limit.
type EmailStr = Annotated[str, mapped_column(String(320))]

#: Monetary amounts. NEVER use float for money — binary floats cannot represent
#: decimal fractions exactly and rounding errors accumulate.
type Money = Annotated[Decimal, mapped_column(Numeric(18, 4))]

#: Schemaless payloads. JSONB (not JSON) so it can be indexed and queried.
type JSONDict = Annotated[dict[str, Any], mapped_column(JSONB)]

#: Consumed by :class:`app.infrastructure.database.base.Base`. Applies to bare
#: annotations that carry no explicit ``mapped_column`` type.
TYPE_ANNOTATION_MAP: Final[dict[Any, Any]] = {
    datetime: DateTime(timezone=True),
    dict[str, Any]: JSONB,
    str: String(255),
    Decimal: Numeric(18, 4),
}

# TODO: add a `CitextStr` alias once the citext extension is enabled, for
# case-insensitive unique columns such as email.
# TODO: add an encrypted-at-rest TypeDecorator for PII columns.
