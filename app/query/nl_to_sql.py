"""NL-to-SQL generation + execution (plan section 3, call site 11).

The parser rail and scope enforcement live in `app/query/sql_guard.py` - it
is pure sqlglot, so it can be tested without a database or an API key. This
module keeps the prompting and the execution, and re-exports the guard's
public names so existing callers and imports are unaffected.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SchemaDef
from app.pipeline.llm import llm_extract
from app.pipeline.views import view_name_for
from app.schema_fields import ROW_SCOPE, field_specs
from app.query.sql_guard import (  # noqa: F401  (re-exported for callers)
    ScopePredicate,
    UnsafeSqlError,
    allowed_view_names,
    document_scope,
    normalize_sql,
    topic_scope,
)

logger = logging.getLogger(__name__)


@lru_cache
def _readonly_engine():
    """Safety rail (b): a Postgres role holding SELECT on the `view_*` views
    and nothing else."""
    settings = get_settings()
    url = getattr(settings, "readonly_database_url", None)
    if not url:
        # Falling through to the read-write URL would run model-authored SQL
        # as the owning role, deleting the second rail with no signal at all.
        # The configured default is a real URL, so this only fires when
        # someone has explicitly blanked it - precisely the dangerous case,
        # which is why it has to be opted into rather than assumed.
        if not getattr(settings, "allow_readwrite_query_fallback", False):
            raise UnsafeSqlError(
                "no readonly_database_url configured - refusing to run generated SQL as the "
                "read-write role (set ALLOW_READWRITE_QUERY_FALLBACK=true to override)"
            )
        logger.warning(
            "running generated SQL as the READ-WRITE role: readonly_database_url is unset and "
            "allow_readwrite_query_fallback is on. The database safety rail is disabled."
        )
        url = settings.database_url
    return create_engine(url, pool_pre_ping=True, future=True)


def build_catalog(session: Session) -> list[dict]:
    """Column catalog for every defined schema - both the LLM prompt context
    and the set of views the generated SQL is allowed to touch.

    Each column now reports the *real* column key alongside its human name.
    This used to advertise the raw field name as if it were the column, so a
    field called "Invoice Total" was offered to the model as `Invoice Total`
    while the column was `invoice_total`: the generated SQL died on
    `UndefinedColumn` and the question fell back to RAG. The description is
    carried too - it was already stored and simply discarded here, and it is
    the only thing that distinguishes `due_date` from `paid_date` for the
    model.
    """
    catalog = []
    for schema in session.query(SchemaDef).all():
        if not schema.versions:
            continue
        versions = sorted(schema.versions, key=lambda v: v.version)
        specs = field_specs([(v.id, v.fields or []) for v in versions])
        catalog.append(
            {
                "schema_id": schema.id,
                "schema_name": schema.name,
                "view_name": view_name_for(schema.name),
                # Whether this view can hold several rows per document. Carried
                # on the catalog entry rather than recomputed at prompt time so
                # `decide_routing` and `generate_sql` cannot disagree about it.
                "multirow": any(s.scope == ROW_SCOPE for s in specs),
                "columns": [
                    {
                        "column": s.column,
                        "name": s.name,
                        "type": s.type,
                        "description": s.description,
                        "scope": s.scope,
                    }
                    for s in specs
                ],
            }
        )
    return catalog


def column_labels(catalog: list[dict]) -> dict[str, str]:
    """`column key -> human label`, for relabelling result rows.

    Labels are de-duplicated: two fields can legitimately share a display name
    (one renamed, one new field reusing the old name), and result rows are
    collected into a dict, so two columns relabelled identically would collapse
    to one with the last value winning.
    """
    labels: dict[str, str] = {}
    seen: set[str] = set()
    for entry in catalog:
        for col in entry["columns"]:
            label = col["name"]
            if label in seen:
                n = 2
                while f"{label} ({n})" in seen:
                    n += 1
                label = f"{label} ({n})"
            seen.add(label)
            labels[col["column"]] = label
    return labels


def relabel_rows(rows: list[dict], labels: dict[str, str]) -> list[dict]:
    """Swaps opaque column keys for human labels in result rows.

    Done at the result boundary rather than by rewriting the SQL: it covers
    `SELECT *`, which an AST rewrite cannot, and it keeps `sql_guard` free of
    both a database dependency and a second non-idempotent pass. It also means
    the answer composer summarizes `"Invoice Total": "4200"` rather than
    `"f_1a2b3c4d": "4200"`.
    """
    if not labels:
        return rows
    return [{labels.get(k, k): v for k, v in row.items()} for row in rows]


GENERATE_SQL_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
    "additionalProperties": False,
}


# Bookkeeping columns every generated view carries on top of the schema's own
# fields (see pipeline/views.py). Listing them lets the model filter/join on
# document_id without having to guess that they exist.
VIEW_BASE_COLUMNS = {
    "id": "uuid",
    "document_id": "uuid",
    "row_index": "integer",
    "schema_version_id": "uuid",
    "is_duplicate_of": "uuid",
    "created_at": "timestamptz",
}

# One sentence, and only for the schemas it is true of. A schema whose fields
# are all document-scoped still has exactly one row per document, and telling
# the model otherwise invites a pointless DISTINCT or GROUP BY document_id
# around every aggregate.
_MULTIROW_NOTE = (
    "    NOTE: one document can contribute several rows to this view (one per line item), "
    "sharing a document_id and distinguished by row_index. Document-level fields repeat "
    "identically on every row of the same document, so summing one of them across rows "
    "would multiply-count it - aggregate those with MAX/MIN per document_id, or count "
    "documents with COUNT(DISTINCT document_id)."
)


def generate_sql(question: str, catalog: list[dict]) -> str:
    """Generation is deliberately scope-free.

    Topic scoping used to be a sentence in this prompt ("add `document_id IN
    (...)` to the WHERE clause") carrying a literal id list. That was
    advisory - the model could and did omit it, and when it did, an aggregate
    was computed over the whole corpus while the answer stated the figure as
    fact and the UI reported "searched within 1 topic(s)" beside it. It also
    grew the prompt linearly with the corpus.

    `sql_guard.inject_scope` now rewrites the scope into the parsed statement
    instead, so the model cannot leave it out. Same reasoning as the
    qualifier repair: prompt instructions are a request, the normalizer is a
    guarantee. Being scope-free also means one generated statement can be
    reused across the scoped/unscoped retry ladder rather than costing a
    second LLM call.
    """
    blocks = []
    for c in catalog:
        lines = [f"- {c['view_name']}: one row per {c['schema_name']} record. Columns:"]
        if c.get("multirow"):
            lines.append(_MULTIROW_NOTE)
        for name, t in VIEW_BASE_COLUMNS.items():
            lines.append(f"    {name} ({t})")
        for col in c["columns"]:
            described = f' - "{col["name"]}"'
            if col["description"]:
                described += f": {col['description']}"
            # Only worth saying on a multi-row view; on a single-row one every
            # column is document-scoped and the label is noise.
            if c.get("multirow"):
                described += f" [{col.get('scope', 'document')}-level]"
            lines.append(f"    {col['column']} ({col['type']}){described}")
        blocks.append("\n".join(lines))
    catalog_block = "\n".join(blocks)

    instructions = (
        "Write a single read-only Postgres SELECT statement that answers the question.\n"
        "Rules:\n"
        "- Read only from the views listed below. Never reference any other table or view, and "
        "never emit DDL or DML.\n"
        "- Write each view name exactly as listed, unqualified. Do not prefix it with anything: "
        "write `view_foo`, never `public.view_foo` and never `foo.view_foo`. The record type "
        "named after each view is a label, not a Postgres schema.\n"
        # The columns are deliberately opaque identifiers. The quoted label
        # after each one is what the field is *called*, and is how the question
        # will refer to it - but it is documentation, not a column name.
        "- Each column is listed as `identifier (type) - \"label\": description`. Use the "
        "identifier verbatim in the SQL; the label and description are only there to tell you "
        "which column the question is about. Never write the label as a column name.\n"
        "- Aggregate in SQL when the question asks for a total, count or average.\n"
        "- Return only the SQL text.\n\nAvailable views:\n"
        f"{catalog_block or '(none defined yet)'}"
    )
    result = llm_extract(
        instructions=instructions, text=question, json_schema=GENERATE_SQL_SCHEMA, schema_name="generated_sql"
    )
    return result["sql"]


def run_safe_select(
    sql: str, allowed_views: set[str], *, scope: ScopePredicate | None = None
) -> tuple[str, list[dict]]:
    """Safety rail (b): execute against a Postgres role that only has SELECT
    grants on the `view_*` views (see migrations) - a second, DB-enforced
    backstop if the parser check is ever wrong.

    `scope` is applied here rather than asked for in the prompt, so the
    emitted SQL is scoped whatever the model wrote. `scope=None` is unscoped
    and injects nothing.

    Returns the SQL that actually ran (post-`normalize_sql`, so including the
    repair and the injected scope) alongside the rows, so the query page
    shows the reviewer the real statement rather than the model's draft.
    """
    safe_sql = normalize_sql(sql, allowed_views, scope=scope)
    engine = _readonly_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(safe_sql)).mappings().all()
    return safe_sql, [dict(r) for r in rows]


SQL_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

# Enough rows for the model to summarize or spot a pattern without blowing
# the context on a wide result set; the full set still goes back to the UI.
MAX_SUMMARIZED_ROWS = 50


def compose_sql_answer(question: str, sql: str, rows: list[dict]) -> str:
    """Answer composition for the SQL branch, mirroring `rag.compose_answer`.

    This used to be a hardcoded "Ran a SQL query and found N row(s)." - so
    the query page reported that something had happened without ever
    answering the question. The rows are the evidence; the model turns them
    into prose.
    """
    shown = rows[:MAX_SUMMARIZED_ROWS]
    # default=str so Decimal/date/UUID values from the view survive encoding.
    payload = json.dumps(shown, default=str, indent=2)
    truncation = f"\n(showing the first {len(shown)} of {len(rows)} rows)" if len(rows) > len(shown) else ""
    instructions = (
        "A SQL query was run against the user's own document database to answer their question. "
        "Answer the question directly, in prose, treating the result rows below as the only "
        "source of truth. Quote the actual figures. Attach a unit or currency symbol only when "
        "a result column actually supplies it - never assume one, and leave a bare number bare "
        "if the rows don't say. If the result set is empty, say plainly that no matching "
        "records were found. Don't mention SQL, views or column names unless the question "
        "asked about them.\n\n"
        f"Query that was run:\n{sql}\n\nResult rows (JSON):\n{payload}{truncation}"
    )
    result = llm_extract(
        instructions=instructions, text=question, json_schema=SQL_ANSWER_SCHEMA, schema_name="sql_answer"
    )
    return result["answer"]
