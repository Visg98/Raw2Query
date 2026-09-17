"""Safe cast helpers for the generated `view_<schema>` projections.

`view_<schema>` projects `extracted_records.data->>'field'` to the field's
declared type. A hard `::numeric` cast means one badly formatted value in
one row (an extraction reporting "83,880.00" or "9%" for a `number` field,
say) raises and takes down the *entire* view - every row, for every
consumer: the schema records page, NL-to-SQL, ad hoc SQL.

These return NULL for that one cell instead. Values are also normalized on
write now (app/pipeline/coerce.py), so this is the backstop for whatever
still slips through - legacy rows, hand edits, genuinely unparseable text.

Revision ID: 0003_safe_cast_functions
Revises: 0002_allow_duplicate_uploads
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_safe_cast_functions"
down_revision: str | None = "0002_allow_duplicate_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# STRICT: NULL in, NULL out, without entering the body at all.
# IMMUTABLE: text -> value mapping is deterministic, so the planner may
# fold/index it. (safe_timestamptz is only STABLE - parsing a bare
# timestamp depends on the session TimeZone setting.)
_FUNCTIONS = [
    ("safe_numeric", "numeric", "IMMUTABLE"),
    ("safe_integer", "integer", "IMMUTABLE"),
    ("safe_boolean", "boolean", "IMMUTABLE"),
    ("safe_date", "date", "IMMUTABLE"),
    ("safe_timestamptz", "timestamptz", "STABLE"),
]


def upgrade() -> None:
    for name, sql_type, volatility in _FUNCTIONS:
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {name}(txt text) RETURNS {sql_type} AS $$
            BEGIN
                RETURN txt::{sql_type};
            EXCEPTION WHEN others THEN
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql {volatility} STRICT PARALLEL SAFE
            """
        )


def downgrade() -> None:
    # Views built by app.pipeline.views reference these; they're recreated
    # on the next schema-version write, so drop with CASCADE.
    for name, _sql_type, _volatility in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {name}(text) CASCADE")
