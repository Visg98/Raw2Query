"""Allow byte-identical re-uploads as independent documents.

Architecture change: the pre-extraction exact-dedup tier (originally
decision #2's first tier) is gone. Re-uploading the same bytes used to be
silently folded into the existing document - no new document, no new job,
so nothing extracted and nothing showed up in the extraction/review
queues. Duplicates are now explicitly acceptable: every upload is its own
document with its own extraction run.

`file_hash` is kept (still cheap to compute, and useful for "have I seen
these bytes before?" reporting) but is no longer unique - only indexed.

Revision ID: 0002_allow_duplicate_uploads
Revises: 0001_initial
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_allow_duplicate_uploads"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Created implicitly by the `file_hash varchar(64) NOT NULL UNIQUE`
    # column definition in 0001, hence the Postgres-default constraint name.
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_file_hash_key")
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_file_hash ON documents (file_hash)")


def downgrade() -> None:
    # Only reversible while no duplicate hashes exist - re-adding the
    # constraint fails if any were uploaded since the upgrade, which is the
    # point of the change.
    op.execute("DROP INDEX IF EXISTS ix_documents_file_hash")
    op.execute("ALTER TABLE documents ADD CONSTRAINT documents_file_hash_key UNIQUE (file_hash)")
