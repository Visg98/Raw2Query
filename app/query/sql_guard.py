"""Safety rails and scope enforcement for generated SQL (plan section 3, call
site 11).

Everything in here is pure: sqlglot in, sqlglot out. No database, no LLM, no
settings. That is deliberate - this is the code that decides whether a
model-authored statement is allowed to run and what it is allowed to see, so
it has to be exhaustively testable without infrastructure. `nl_to_sql.py`
keeps the prompting and the execution.

The division of labour between the two rails is unchanged (decisions.md,
"Two independent safety rails on generated SQL"): this module is the parser
rail, and the read-only Postgres role is the database rail. Neither trusts
the prompt.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import sqlglot
from sqlglot import exp

# Node types that mean "this touches data or schema", checked anywhere in the
# parsed tree - not just at the top level, since a write can hide inside a
# CTE (`WITH x AS (DELETE ... RETURNING *) SELECT * FROM x` still parses as
# a top-level Select).
_UNSAFE_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Merge,
    exp.Command,  # catch-all for DDL/statements sqlglot doesn't model explicitly
)

# A predicate factory rather than a predicate. Each injection site needs its
# own expression object - sharing one node across several points in the tree
# is how you get an AST that renders correctly once and then mutates from
# under you.
ScopePredicate = Callable[[], exp.Expression]


class UnsafeSqlError(Exception):
    pass


def parse_and_guard(sql: str) -> exp.Select:
    """Safety rail (a): parse the generated SQL and reject anything that
    isn't a single, pure SELECT statement - blocks statement-stacking and
    writes hidden inside a CTE/subquery, regardless of what the LLM was told
    to do."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except Exception as exc:
        raise UnsafeSqlError(f"could not parse generated SQL: {exc}") from exc
    if len(statements) != 1:
        raise UnsafeSqlError("generated SQL must be exactly one statement")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise UnsafeSqlError("generated SQL must be a single SELECT statement")
    if statement.args.get("into"):
        raise UnsafeSqlError("SELECT INTO is not allowed")
    if statement.find(*_UNSAFE_NODE_TYPES) is not None:
        raise UnsafeSqlError("generated SQL contains a write/DDL statement")
    return statement


def allowed_view_names(catalog: list[dict]) -> set[str]:
    """The only relations generated SQL may read - every `view_<schema>` in
    the catalog. The read-only role's grants stop at these too, so this is
    the parser-level mirror of what the DB itself allows.

    Because the catalog may be narrowed to the selected topics before it gets
    here, a view holding no records under those topics is not even nameable.
    """
    return {c["view_name"] for c in catalog}


def cte_names(statement: exp.Select) -> set[str]:
    """CTE names are legitimate relation references that aren't views."""
    return {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name}


def strip_qualifiers(statement: exp.Select, allowed_views: set[str]) -> None:
    """Mutates `statement` in place: validates every relation reference
    against the allowlist and drops any schema/catalog qualifier.

    The repair exists because of the one mistake the model made every single
    time: qualifying the view with the schema's *logical* name
    (`invoice.view_invoice`). Nothing lives in a Postgres schema called
    `invoice`, so every generated query died on `UndefinedTable` and the
    query page silently fell back to RAG - structured questions never once
    got a structured answer. Prompt instructions are a request; this is a
    guarantee.
    """
    allowed_lower = {v.lower() for v in allowed_views} | cte_names(statement)

    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue  # not a plain relation reference (e.g. a table function)
        if name not in allowed_lower:
            raise UnsafeSqlError(
                f"generated SQL reads {table.sql(dialect='postgres')!r}, which is not one of the "
                f"allowed views ({', '.join(sorted(allowed_views)) or 'none'})"
            )
        table.set("db", None)
        table.set("catalog", None)


def topic_scope(topic_ids: list[uuid.UUID]) -> ScopePredicate:
    """`topic_ids && ARRAY[...]::uuid[]` against the generated views' own
    `topic_ids` column.

    Note `'{}' && anything` is a hard false, so a record whose document has
    no topics is reachable only unscoped - the same semantics as
    `chunks.topic_ids` on the retrieval side.
    """

    def build() -> exp.Expression:
        return exp.ArrayOverlaps(
            this=exp.column("topic_ids"),
            expression=exp.Cast(
                this=exp.Array(expressions=[exp.Literal.string(str(t)) for t in topic_ids]),
                to=exp.DataType.build("uuid[]", dialect="postgres"),
            ),
        )

    return build


def document_scope(document_ids: list[uuid.UUID]) -> ScopePredicate:
    """`document_id IN (...)` - the fallback for as long as the generated
    views have no `topic_ids` column. Same enforcement, no DDL; it just
    carries the whole id list into the emitted SQL, which grows with the
    corpus.
    """

    def build() -> exp.Expression:
        return exp.column("document_id").isin(*[exp.Literal.string(str(d)) for d in document_ids])

    return build


def inject_scope(
    statement: exp.Select, allowed_views: set[str], predicate: ScopePredicate
) -> int:
    """Replaces every allowed-view reference with a pre-filtered derived
    table, returning how many were rewritten.

    Appending `AND <predicate>` to the containing SELECT's WHERE looks
    simpler and is wrong in the cases that matter: an unaliased reference
    can't be qualified without inventing an alias (which breaks any
    `view_invoice.total` the model already wrote), and a reference inside a
    `JOIN ... ON` or a correlated subquery makes "which WHERE clause"
    ambiguous. Rewriting the table node itself is unambiguous everywhere:

        FROM view_invoice i
     -> FROM (SELECT * FROM view_invoice WHERE <predicate>) AS i

    An unaliased reference is aliased with its own name, so `SELECT
    view_invoice.total` keeps resolving.

    NOT IDEMPOTENT. Each call wraps every view reference it can see, so a
    second call would wrap the ones this call just created. The table list is
    snapshotted before any mutation precisely so that this call doesn't do
    that to itself - but it means `normalize_sql` must never be handed its
    own output.
    """
    # Snapshot before mutating: find_all is a generator over a tree we are
    # about to rewrite, and the fresh `exp.Table` inside each subquery we
    # create must not be visited.
    tables = list(statement.find_all(exp.Table))
    reserved = cte_names(statement)
    allowed_lower = {v.lower() for v in allowed_views}
    injected = 0

    for table in tables:
        name = (table.name or "").lower()
        if not name or name in reserved or name not in allowed_lower:
            continue
        alias = table.args.get("alias")
        # Preserve the alias the model chose; fall back to the view's own
        # name so pre-existing qualified references still resolve.
        ident = alias.this if alias is not None else exp.to_identifier(table.name)
        scoped = exp.select(exp.Star()).from_(exp.to_table(table.name)).where(predicate())
        table.replace(exp.Subquery(this=scoped, alias=exp.TableAlias(this=ident)))
        injected += 1

    return injected


def render_sql(statement: exp.Select) -> str:
    return statement.sql(dialect="postgres")


def normalize_sql(
    sql: str, allowed_views: set[str], *, scope: ScopePredicate | None = None
) -> str:
    """Guard, repair, optionally scope, and re-check.

    `scope=None` means unscoped, and injects nothing. There is deliberately
    no `IS NULL OR ...` escape hatch: "unscoped" is the *absence* of the
    predicate, so there is exactly one way to be unscoped and it is visible
    in the SQL the query page shows the user.
    """
    statement = parse_and_guard(sql)
    strip_qualifiers(statement, allowed_views)
    if scope is not None:
        inject_scope(statement, allowed_views, scope)
    rendered = render_sql(statement)

    # Belt and braces. Re-parsing our own output turns any future sqlglot
    # codegen surprise into a rejection rather than a query: whatever we are
    # about to execute must still be one pure SELECT over allowed relations.
    verified = parse_and_guard(rendered)
    strip_qualifiers(verified, allowed_views)
    return rendered
