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

import hashlib
import hmac
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final

from cryptography.fernet import Fernet
from sqlalchemy import DateTime, Numeric, String, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import mapped_column

from app.core.config import settings

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


class CaseInsensitiveString(TypeDecorator[str]):
    """Case-insensitive text, for values whose case is not meaningful.

    The problem
    -----------
    ``alice@example.com`` and ``Alice@Example.com`` are the same mailbox, but a
    ``UNIQUE`` index on plain ``VARCHAR`` treats them as different rows — so two
    accounts get created for one human, and a login lookup finds neither
    reliably.

    Why not the ``citext`` extension
    --------------------------------
    ``citext`` does exactly this in the database, and would be the obvious
    answer, but it requires ``CREATE EXTENSION`` — a superuser operation that
    several managed PostgreSQL providers do not permit, and one more thing that
    must be true of every environment before a migration will apply.

    This decorator lower-cases on the way in instead. Storage stays plain
    ``TEXT``, a normal unique index works, and there is no extension to install.
    The trade is that the *original* casing is lost, so do not use it where the
    display form matters — a person's name, for instance.

    Comparison values are normalised too, so ``where(User.email == "A@B.com")``
    matches the stored ``a@b.com``.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Lower-case on the way into the database."""
        return value.lower() if value is not None else None

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Return the stored value unchanged; it is already normalised."""
        return value


class EncryptedString(TypeDecorator[str]):
    """Application-level encryption for a column holding sensitive data.

    What this defends against, and what it does not
    ----------------------------------------------
    It protects data **at rest outside the running application**: a stolen
    backup, a decommissioned disk, an over-broad read grant, a support engineer
    with psql access. Ciphertext without the key is useless in all of those.

    It does **not** protect against an attacker who has compromised the
    application process, because that process must hold the key to function.
    Encryption at this layer is one control among several, not a substitute for
    access control.

    Costs you are accepting
    -----------------------
    * **The column is not searchable.** ``WHERE ssn = ?`` cannot work: Fernet
      is randomised, so the same plaintext encrypts differently every time.
      Where a lookup is needed, store a separate blind index — an HMAC of the
      normalised plaintext — and query that.
    * **The column is not sortable or indexable** for the same reason.
    * **Key rotation is a data migration.** Re-encrypting means reading and
      rewriting every row, so decide the rotation story before adopting this.

    The key comes from configuration; a missing key raises at first use rather
    than silently storing plaintext.
    """

    impl = Text
    cache_ok = True

    def _cipher(self) -> Fernet:
        """Build the cipher, failing loudly when no key is configured."""
        key = settings.security.encryption_key
        if key is None:
            raise RuntimeError(
                "SECURITY__ENCRYPTION_KEY is required to read or write an "
                "encrypted column. Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )
        return Fernet(key.get_secret_value().encode())

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Encrypt on the way into the database."""
        if value is None:
            return None
        return self._cipher().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Decrypt on the way out.

        A decryption failure is raised rather than swallowed: returning ``None``
        for an undecryptable row would present data loss as an empty field, and
        the row would then be overwritten with a fresh encryption of nothing.
        """
        if value is None:
            return None
        return self._cipher().decrypt(value.encode()).decode()


def blind_index(value: str, *, secret: str) -> str:
    """Compute a deterministic, searchable fingerprint of a sensitive value.

    The companion to :class:`EncryptedString`. Because encrypted columns cannot
    be searched, store this alongside and query it::

        email_encrypted: Mapped[str] = mapped_column(EncryptedString)
        email_index: Mapped[str] = mapped_column(String(64), index=True)

    Keyed with an HMAC, never a bare hash: a plain ``sha256(email)`` is
    trivially reversible for any value drawn from a guessable set — every email
    address in a breach corpus, every possible national insurance number — so
    an unkeyed index would hand back exactly the data the encryption protects.

    Normalise before calling (lower-case, strip) or the same logical value
    produces different fingerprints.

    Args:
        value: The normalised plaintext.
        secret: The index key. Must differ from the encryption key, so leaking
            one does not compromise the other.

    Returns:
        A hex digest suitable for an indexed column.
    """
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


#: Case-insensitive email column, unique-safe without the citext extension.
type CIEmailStr = Annotated[str, mapped_column(CaseInsensitiveString(320))]

#: Encrypted free-text column for PII. Not searchable — pair with a blind index.
type EncryptedStr = Annotated[str, mapped_column(EncryptedString)]
