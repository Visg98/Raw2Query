"""Generates/regenerates the flattened `view_<schema_name>` SQL view for a
schema lineage (plan section 2, "Extracted records").

Not a migration - this is backend logic that runs right after a schema
version insert, so the view stays in sync as versions are added. That is also
why `reconcile_schema_views` exists: a shape change in this file leaves every
existing view stale, and an alembic revision calling into app code would
freeze a snapshot of it.

`render_view_sql` is pure - specs in, SQL text out - so the projection rules
can be tested as golden strings with no database, mirroring the split between
`app/query/sql_guard.py` and `app/query/nl_to_sql.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import SchemaDef, SchemaVersion
from app.schema_fields import COLUMN_KEY_RE, ColumnKeyError, FieldSpec, field_specs

logger = logging.getLogger(__name__)

# Safe cast helpers from migration 0003 rather than raw `::type` casts: a
# single unparseable value in a single row would otherwise raise and break
# the whole view for every row and every consumer (records page, NL-to-SQL,
# ad hoc SQL). These null out just that cell. Values are normalized on
# write too - see app/pipeline/coerce.py.
_SAFE_CAST_FN = {
    "number": "safe_numeric",
    "integer": "safe_integer",
    "boolean": "safe_boolean",
    "date": "safe_date",
    "datetime": "safe_timestamptz",
    "string": None,  # plain text, no cast needed
}

# Bookkeeping columns every generated view carries, before the schema's own
# fields. `topic_ids` is here so a topic filter can be enforced against the
# view itself (see app/query/sql_guard.py); it is deliberately not advertised
# to the SQL-generating model.
_BASE_COLUMNS = [
    "er.id",
    "er.document_id",
    # A document with row-scoped fields projects one row per repeating source
    # row, all sharing a document_id; this is what orders them. For a
    # document-scoped-only schema it is always 0.
    "er.row_index",
    "er.schema_version_id",
    "er.is_duplicate_of",
    "er.created_at",
]

# Pre-aggregated rather than joined and grouped in the outer query: grouping
# there would force every projected field into GROUP BY and break `SELECT *`.
# LEFT JOIN rather than INNER so a document with no topics keeps its records -
# a retrieval change must not silently delete rows. The COALESCE is
# load-bearing: `NULL && ARRAY[...]` is NULL rather than false, whereas
# `'{}' && anything` is a hard false, so a topic-less record is reachable only
# unscoped - the same semantics as `chunks.topic_ids`.
_TOPIC_JOIN = (
    "LEFT JOIN (SELECT document_id, array_agg(DISTINCT topic_id) AS topic_ids\n"
    "           FROM document_topics GROUP BY document_id) dt\n"
    "       ON dt.document_id = er.document_id"
)

COMMENT_PREFIX = "r2q:"


def safe_ident(name: str) -> str:
    """Only used for the *view* name now; field columns carry a stored,
    collision-proof key instead (see app/schema_fields.py)."""
    ident = re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()
    if not ident or ident[0].isdigit():
        ident = f"f_{ident}"
    return ident


def view_name_for(schema_name: str) -> str:
    return f"view_{safe_ident(schema_name)}"


def _escape_literal(value: str) -> str:
    return value.replace("'", "''")


def _json_read(name: str) -> str:
    return f"er.data->>'{_escape_literal(name)}'"


def _projection(spec: FieldSpec) -> str:
    """One column expression.

    A field whose name never changed, was never shadowed by another field, and
    was declared by every version reads as a bare `er.data->>'name'`. Anything
    else is scoped by the row's own `schema_version_id`.

    `COALESCE(data->>'new', data->>'old')` is NOT a valid alternative. The
    extraction contract writes every declared field, using an explicit JSON
    null when the value is absent (`app/pipeline/extract.py` puts every field
    in `required` as `["string","null"]`), and `->>` cannot distinguish a JSON
    null from a missing key - so COALESCE falls through and surfaces a
    *different* field's value under this column whenever a later version reuses
    an older name. Verified against Postgres.
    """
    if not COLUMN_KEY_RE.fullmatch(spec.column):
        # Defence in depth at the point of interpolation: this value becomes a
        # SQL identifier, and `confirm.py` writes proposed fields into JSONB
        # verbatim without passing through Pydantic.
        raise ColumnKeyError(f"refusing to build SQL for invalid column key {spec.column!r}")

    if spec.plain:
        expr = _json_read(spec.branches[0][0])
    else:
        whens = []
        for name, version_ids in spec.branches:
            ids = ", ".join(f"'{v}'" for v in version_ids)
            whens.append(f"WHEN er.schema_version_id IN ({ids}) THEN {_json_read(name)}")
        indented = "\n           ".join(whens)
        expr = f"CASE\n           {indented}\n       END"

    # The cast wraps the whole expression, not each branch: the safe_* helpers
    # are STRICT so NULL never reaches the body, every branch is text, and one
    # call is cheaper and far shorter than one per branch.
    cast_fn = _SAFE_CAST_FN.get(spec.type)
    if cast_fn:
        expr = f"{cast_fn}({expr})"
    return f"{expr} AS {spec.column}"


def spec_fingerprint(view_name: str, schema_id: uuid.UUID, specs: list[FieldSpec]) -> str:
    """A digest of everything `render_view_sql` depends on.

    Stored as a view comment so `reconcile_schema_views` can detect drift
    exactly. A column-set probe cannot: renaming a field keeps both the column
    name and its type and changes only the view body, which is precisely the
    case this projection logic exists for.

    `base` is in here because the bookkeeping columns are as much a part of
    the rendered view as the fields are, and leaving them out made the digest
    quietly untrue: adding `er.row_index` to `_BASE_COLUMNS` changed every
    view's shape without changing any fingerprint, so `reconcile_schema_views`
    would have considered every existing view up to date and no view would
    ever have gained the column. Any future base-column change now invalidates
    on its own.
    """
    canonical = json.dumps(
        {
            "view": view_name,
            "schema_id": str(schema_id),
            "base": _BASE_COLUMNS,
            "fields": [
                {
                    "column": s.column,
                    "type": s.type,
                    "plain": s.plain,
                    "branches": [[n, [str(v) for v in vids]] for n, vids in s.branches],
                }
                for s in specs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def render_view_sql(view_name: str, schema_id: uuid.UUID, specs: list[FieldSpec]) -> str:
    """The `CREATE VIEW` statement. Pure: no session, no I/O."""
    columns = [*_BASE_COLUMNS, "COALESCE(dt.topic_ids, '{}'::uuid[]) AS topic_ids"]
    columns.extend(_projection(s) for s in specs)
    return (
        f"CREATE VIEW {view_name} AS\n"
        f"SELECT {', '.join(columns)}\n"
        f"FROM extracted_records er\n"
        f"{_TOPIC_JOIN}\n"
        f"WHERE er.schema_id = '{schema_id}'"
    )


def _lineage(session: Session, schema_id: uuid.UUID) -> list[tuple[uuid.UUID, list[dict]]]:
    versions = (
        session.query(SchemaVersion)
        .filter(SchemaVersion.schema_id == schema_id)
        .order_by(SchemaVersion.version.asc())
        .all()
    )
    return [(v.id, v.fields or []) for v in versions]


def regenerate_schema_view(session: Session, schema_id: uuid.UUID) -> str:
    """(Re)builds the view for this lineage (decision #1: old records read
    NULL for columns introduced later)."""
    schema = session.get(SchemaDef, schema_id)
    if schema is None:
        raise ValueError(f"unknown schema_id {schema_id}")

    view = view_name_for(schema.name)
    specs = field_specs(_lineage(session, schema_id))
    sql = render_view_sql(view, schema_id, specs)

    # DROP + CREATE rather than CREATE OR REPLACE: a version bump that
    # retypes a field (one of decision #1's two "breaking" cases) changes a
    # column's output type, which Postgres refuses under REPLACE. The
    # read-only role's grant (migration 0001, ALTER DEFAULT PRIVILEGES) is
    # keyed to the creating role, not the object's creation time, so it
    # still applies after the view is recreated.
    session.execute(text(f"DROP VIEW IF EXISTS {view}"))
    session.execute(text(sql))
    fingerprint = spec_fingerprint(view, schema_id, specs)
    session.execute(text(f"COMMENT ON VIEW {view} IS '{COMMENT_PREFIX}{fingerprint}'"))
    return view


def reconcile_schema_views(session: Session, *, force: bool = False) -> list[str]:
    """Brings every `view_*` up to the current generated shape.

    Idempotent, so it is safe to run on every boot and doubles as a repair
    tool (`python -m app.pipeline.views`). Returns the views it rebuilt.

    Not an alembic revision: the view body is derived from this module, so a
    migration would freeze a copy of it and go stale at the next shape change.
    """
    # DROP VIEW takes an AccessExclusiveLock, so it queues behind a long
    # read-only SELECT - and every new reader then queues behind the DROP.
    # A blocking lock here would turn reconcile-on-startup into a
    # self-inflicted outage, so: never wait, and never hold up a second
    # process that is already doing this work. The xact-scoped variant
    # releases on commit/rollback, so a crash cannot wedge it.
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    acquired = session.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext('r2q_view_reconcile'))")
    ).scalar()
    if not acquired:
        logger.info("another process is reconciling schema views; skipping")
        return []

    existing = {
        row.view_name: (row.comment or "")
        for row in session.execute(
            text(
                "SELECT c.relname AS view_name, obj_description(c.oid, 'pg_class') AS comment "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'v' AND n.nspname = 'public'"
            )
        )
    }

    rebuilt: list[str] = []
    seen: dict[str, uuid.UUID] = {}
    for schema in session.query(SchemaDef).all():
        view = view_name_for(schema.name)
        if view in seen:
            # Two schema names collapsing to one view name means the second
            # CREATE's `DROP VIEW IF EXISTS` destroys the first one's view,
            # and reconcile would rebuild them alternately forever. Refuse
            # loudly rather than thrash.
            logger.error(
                "schema %s and %s both map to view %s - skipping; rename one",
                seen[view],
                schema.id,
                view,
            )
            continue
        seen[view] = schema.id

        try:
            specs = field_specs(_lineage(session, schema.id))
            wanted = f"{COMMENT_PREFIX}{spec_fingerprint(view, schema.id, specs)}"
            if not force and existing.get(view) == wanted:
                continue
            regenerate_schema_view(session, schema.id)
            rebuilt.append(view)
        except Exception:
            # One unmigrated lineage or one lock timeout must not abort the
            # whole reconcile.
            logger.exception("could not reconcile %s", view)

    return rebuilt


def main() -> None:  # pragma: no cover - operational entry point
    logging.basicConfig(level=logging.INFO)
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        rebuilt = reconcile_schema_views(session)
        session.commit()
        print(f"rebuilt {len(rebuilt)} view(s): {', '.join(rebuilt) or 'none'}")
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover
    main()
