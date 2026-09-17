"""Give every schema field a persisted, opaque column key.

A field's column in `view_<schema>` used to be `safe_ident(field.name)`,
recomputed at view-generation time and stored nowhere - so every other
consumer re-derived it, and two of them got it wrong: the NL-to-SQL catalog
and the records grid both advertised the raw field name, so a field called
"Invoice Total" was offered to the model as `Invoice Total` while the column
was `invoice_total`. Generated SQL died on `UndefinedColumn` and fell back to
RAG; the records grid rendered the column blank. The derived name also
collided three ways: with the view's own bookkeeping columns (a field named
`id` broke `CREATE VIEW` outright), after Postgres truncated identifiers at 63
characters, and via a disambiguation suffix that depended on dict iteration
order.

This backfills `schema_versions.fields[*].column` with a key matching
`^f_[0-9a-f]{8}$`, and converts `schema_versions.identity_fields` from field
names to those keys so a later rename cannot orphan them.

Two deliberate departures from the conventions in this directory:

* It is the project's first DATA migration. The carry-forward rule is a
  stateful fold over a lineage's versions in order, which in pure SQL means a
  recursive CTE rebuilding a JSONB array element-wise - unreviewable, and
  reviewability is a data migration's only safety net. So: `op.get_bind()`
  plus Python, in the spirit of 0003's loop-a-list-and-execute shape.
* It does NOT use `op.execute(f"...")`. `fields` holds user- and
  LLM-authored names and descriptions, so values go back through bound
  parameters. Please don't "fix" this back to an f-string.

It deliberately imports nothing from `app/`: this revision has to keep
meaning the same thing regardless of how `app/schema_fields.py` later
evolves. The duplicated key format is pinned against the app's by a test.

The generated views are NOT rebuilt here - the view body is derived from
application code, so an alembic revision would freeze a copy of it. Run
`python -m app.pipeline.views` after upgrading (the app also reconciles on
startup). In the window between the two, structured questions fall back to
RAG and the records grid shows blank columns - both of which are exactly the
pre-existing behaviour this change fixes, so nothing is lost and nothing is
wrong, it just isn't better yet.

Revision ID: 0005_field_column_keys
Revises: 0004_chat_sessions
Create Date: 2026-09-16
"""

import json
import logging
import re
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_field_column_keys"
down_revision: str | None = "0004_chat_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Kept in step with app.schema_fields.COLUMN_KEY_RE by a test, not by import.
KEY_RE = re.compile(r"^f_[0-9a-f]{8}$")

_SELECT = sa.text(
    "SELECT id, schema_id, version, fields, identity_fields "
    "FROM schema_versions ORDER BY schema_id, version"
)
_UPDATE = sa.text(
    "UPDATE schema_versions SET fields = CAST(:fields AS jsonb), identity_fields = :identity "
    "WHERE id = :id"
)


def _mint(taken: set[str]) -> str:
    while True:
        key = f"f_{uuid.uuid4().hex[:8]}"
        if key not in taken:
            return key


def plan_lineage(versions: list[tuple]) -> dict:
    """The fold. Pure, module-level and side-effect free so it can be tested
    without a database.

    `versions` is `[(version_id, fields, identity_fields)]` for ONE schema
    lineage, ordered by version ascending. Returns
    `{version_id: (fields, identity_fields)}` for the rows that need writing.

    The name->key map accumulates across the WHOLE lineage and is never reset
    per version. For a field that went `Total` -> `Net` -> `Total`, resetting
    would mint a second key for the third version and leave two half-empty
    columns. This migration has no rename information at all, so "the same
    name in one lineage is the same field" is the only defensible reading, and
    it also makes the common drop-then-re-add reclaim its original column.
    """
    carried: dict[str, str] = {}
    taken: set[str] = set()
    updates: dict = {}

    for version_id, fields, identity_fields in versions:
        new_fields = []
        changed = False

        for f in fields or []:
            field = dict(f)
            key = field.get("column")
            if not (isinstance(key, str) and KEY_RE.fullmatch(key)):
                key = carried.get(field.get("name")) or _mint(taken)
                field["column"] = key
                changed = True
            carried[field.get("name")] = key
            taken.add(key)
            new_fields.append(field)

        # identity_fields: names -> keys. An entry matching no field of this
        # version was already a dead no-op (`_find_dedup_match` looked it up
        # in `data` and found nothing), so dropping it loses no behaviour and
        # keeps the stored value a pure key list.
        new_identity = None
        if identity_fields:
            by_name = {f.get("name"): f["column"] for f in new_fields}
            new_identity = []
            for entry in identity_fields:
                if isinstance(entry, str) and KEY_RE.fullmatch(entry):
                    new_identity.append(entry)  # already migrated
                    continue
                key = by_name.get(entry)
                if key is None:
                    logger.warning(
                        "dropping identity field %r on schema_version %s: no field declares it",
                        entry,
                        version_id,
                    )
                    changed = True
                    continue
                new_identity.append(key)
                changed = True

        if changed:
            updates[version_id] = (new_fields, new_identity)

    return updates


def upgrade() -> None:
    bind = op.get_bind()

    # A live API inserting version n+1 mid-fold would leave a keyless row
    # behind, and a keyless row is a hard failure for that lineage once
    # `views.py` starts refusing to build SQL without a key. Cheap insurance;
    # SHARE ROW EXCLUSIVE still allows concurrent reads.
    bind.execute(sa.text("LOCK TABLE schema_versions IN SHARE ROW EXCLUSIVE MODE"))

    by_lineage: dict = {}
    for row in bind.execute(_SELECT):
        by_lineage.setdefault(row.schema_id, []).append(
            (row.id, row.fields, row.identity_fields)
        )

    written = 0
    for versions in by_lineage.values():
        for version_id, (fields, identity) in plan_lineage(versions).items():
            bind.execute(
                _UPDATE,
                {"id": version_id, "fields": json.dumps(fields), "identity": identity},
            )
            written += 1

    # Re-running is a no-op: a well-formed key is never replaced, so nothing
    # is marked changed. That makes this usable as a repair tool too.
    logger.info("0005: assigned column keys on %s schema_version row(s)", written)


def downgrade() -> None:
    """Strips the keys and restores identity fields to names.

    LOSSY: the keys are gone, so re-upgrading mints different ones. Anything
    that captured a key - a stored `chat_messages.metadata` SQL string, a
    bookmark, an external dashboard - will not line up afterwards.
    """
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE schema_versions IN SHARE ROW EXCLUSIVE MODE"))

    # identity_fields first, while the keys are still there to translate.
    for row in bind.execute(_SELECT):
        if not row.identity_fields:
            continue
        names_by_key = {f.get("column"): f.get("name") for f in (row.fields or [])}
        restored = [names_by_key.get(key) or key for key in row.identity_fields]
        bind.execute(
            sa.text("UPDATE schema_versions SET identity_fields = :identity WHERE id = :id"),
            {"id": row.id, "identity": restored},
        )

    # `jsonb_agg` over zero rows returns NULL, and `fields` is `jsonb NOT
    # NULL` - so without the COALESCE a version row storing `[]` aborts the
    # whole downgrade. `WITH ORDINALITY` + `ORDER BY ord` because field order
    # is user-visible and `jsonb_agg` has no inherent input order. The `@?`
    # predicate makes this idempotent.
    op.execute(
        """
        UPDATE schema_versions
           SET fields = COALESCE(
               (SELECT jsonb_agg(elem - 'column' ORDER BY ord)
                  FROM jsonb_array_elements(fields) WITH ORDINALITY AS t(elem, ord)),
               '[]'::jsonb)
         WHERE fields @? '$[*].column'
        """
    )
