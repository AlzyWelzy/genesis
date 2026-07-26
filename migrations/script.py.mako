"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Review checklist before merging:

* Does ``downgrade()`` actually reverse ``upgrade()``? A migration you cannot
  roll back is a migration you cannot deploy safely.
* Does this lock a large table? ``ALTER TABLE ... ADD COLUMN NOT NULL`` without
  a default rewrites the whole table and blocks writes for its duration.
* Are new indexes created ``CONCURRENTLY``? A plain ``CREATE INDEX`` blocks
  writes; note that concurrent creation cannot run inside a transaction.
* Is this backwards compatible with the currently deployed code? During a
  rolling deploy both versions run at once, so destructive changes need two
  releases: stop using the column, then drop it.
* Does a data migration handle an empty table, and is it batched for a large one?
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the schema change."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Reverse the schema change."""
    ${downgrades if downgrades else "pass"}
