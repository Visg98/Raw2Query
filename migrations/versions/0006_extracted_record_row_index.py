"""Give every extracted record its position within its document.

Until now one document produced at most one `extracted_records` row, so a
record's identity was `(document_id, schema_id)` and nothing needed to say
*which* row it was. A schema may now declare row-scoped fields (see
`SchemaFieldDef.scope`), in which case one document produces N records - one
per repeating row in the source table - and they need a stable, meaningful
order:

* the review grid has to redisplay rows in the order the document lists them,
  not in whatever order Postgres hands back;
* a backfill has to be able to replace a document's record set deterministically
  (see `_apply_backfill_extracted_record`), which means the rows it writes must
  be addressable rather than interchangeable.

`server_default '0'` is what makes this a non-event for existing data: every
row already in the table is its document's only row, and 0 is exactly the
index it would be assigned today. The default is left on the column rather
than dropped afterwards so that any INSERT path not yet updated keeps
working instead of raising NotNullViolation.

There is deliberately NO unique constraint on `(document_id, schema_id,
row_index)`. It looks like the obvious invariant, but two confirm paths
legitimately break it: `keep_both` inserts a second record set alongside a
matched one, and `replace` re-points an existing record's `document_id` at a
different document. Both predate this change and neither is being altered
here, so adding the constraint would turn a working flow into a 500. The
ordering guarantee this column provides does not depend on uniqueness.

The generated `view_<schema>` views project this column, but they are NOT
rebuilt here - their body is derived from `app/pipeline/views.py`, so an
alembic revision would freeze a copy of it. The app reconciles views on
startup; `python -m app.pipeline.views` does it on demand.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_extracted_record_row_index"
down_revision: str | None = "0005_field_column_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extracted_records",
        sa.Column("row_index", sa.Integer(), nullable=False, server_default="0"),
    )
    # Every read of a document's record set orders by this, and the backfill
    # replace path deletes by the `(document_id, schema_id)` prefix.
    op.create_index(
        "ix_extracted_records_document_schema_row",
        "extracted_records",
        ["document_id", "schema_id", "row_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_extracted_records_document_schema_row", table_name="extracted_records")
    op.drop_column("extracted_records", "row_index")
