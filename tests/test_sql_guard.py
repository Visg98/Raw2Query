"""Coverage for the parser rail and scope injection (app/query/sql_guard.py).

Deliberately pure: no Postgres, no OpenAI key, no fixtures. These run with
the whole stack down, which matters because this is the code that decides
whether a model-authored statement executes at all.

Assertions are made on the re-parsed output rather than on substrings - the
question is always "what does this statement now mean", and string matching
answers a different question.
"""

import uuid

import pytest
from sqlglot import exp

from app.query.sql_guard import (
    UnsafeSqlError,
    allowed_view_names,
    cte_names,
    document_scope,
    inject_scope,
    normalize_sql,
    parse_and_guard,
    strip_qualifiers,
    topic_scope,
)

ALLOWED = {"view_invoice", "view_po"}
TOPIC_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TOPIC_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def scoped(sql: str, *, views=ALLOWED, topics=(TOPIC_A,)) -> str:
    return normalize_sql(sql, views, scope=topic_scope(list(topics)))


def reparse(sql: str) -> exp.Select:
    return parse_and_guard(sql)


def overlaps_count(sql: str) -> int:
    return len(list(reparse(sql).find_all(exp.ArrayOverlaps)))


def view_refs(sql: str) -> list[str]:
    stmt = reparse(sql)
    reserved = cte_names(stmt)
    return sorted(t.name.lower() for t in stmt.find_all(exp.Table) if t.name.lower() not in reserved)


class TestRejections:
    """Safety rail (a). Every one of these used to be untested."""

    def test_rejects_statement_stacking(self):
        with pytest.raises(UnsafeSqlError, match="exactly one statement"):
            normalize_sql("SELECT 1 FROM view_invoice; DROP TABLE documents", ALLOWED)

    def test_rejects_non_select(self):
        with pytest.raises(UnsafeSqlError, match="single SELECT"):
            normalize_sql("DELETE FROM view_invoice", ALLOWED)

    def test_rejects_write_hidden_in_a_cte(self):
        """`WITH x AS (DELETE ... RETURNING *) SELECT * FROM x` parses as a
        top-level Select, which is why the unsafe-node check walks the whole
        tree instead of inspecting only the root."""
        with pytest.raises(UnsafeSqlError, match="write/DDL"):
            normalize_sql(
                "WITH x AS (DELETE FROM view_invoice RETURNING *) SELECT * FROM x", ALLOWED
            )

    def test_rejects_select_into(self):
        with pytest.raises(UnsafeSqlError, match="SELECT INTO"):
            normalize_sql("SELECT * INTO evil FROM view_invoice", ALLOWED)

    @pytest.mark.parametrize(
        "relation",
        ["extracted_records", "documents", "document_topics", "chunks", "pg_catalog.pg_class"],
    )
    def test_rejects_base_tables(self, relation):
        """The read-only role has no grant on these, but the parser must not
        rely on that - it is the first of two independent rails."""
        with pytest.raises(UnsafeSqlError, match="not one of the allowed views"):
            normalize_sql(f"SELECT * FROM {relation}", ALLOWED)

    def test_rejects_unparseable_sql(self):
        with pytest.raises(UnsafeSqlError, match="could not parse|exactly one statement"):
            normalize_sql("SELECT FROM WHERE )(", ALLOWED)

    def test_empty_allowlist_rejects_everything(self):
        with pytest.raises(UnsafeSqlError, match="allowed views \\(none\\)"):
            normalize_sql("SELECT * FROM view_invoice", set())


class TestQualifierRepair:
    def test_strips_the_logical_schema_qualifier(self):
        """The mistake the model made every single time: `invoice.view_invoice`.
        Nothing lives in a Postgres schema called `invoice`."""
        out = normalize_sql("SELECT total FROM invoice.view_invoice", ALLOWED)
        assert "invoice.view_invoice" not in out
        assert view_refs(out) == ["view_invoice"]

    def test_strips_public_qualifier(self):
        out = normalize_sql("SELECT total FROM public.view_invoice", ALLOWED)
        assert view_refs(out) == ["view_invoice"]

    def test_cte_reference_is_allowed_without_being_a_view(self):
        out = normalize_sql(
            "WITH t AS (SELECT * FROM view_invoice) SELECT count(*) FROM t", ALLOWED
        )
        assert "t" in cte_names(reparse(out))


class TestScopeInjection:
    @pytest.mark.parametrize(
        "sql,expected_refs",
        [
            ("SELECT sum(total) FROM view_invoice", 1),
            ("SELECT sum(i.total) FROM view_invoice i", 1),
            ("SELECT view_invoice.total FROM view_invoice", 1),
            ("SELECT total FROM invoice.view_invoice", 1),
            ("SELECT * FROM view_invoice a JOIN view_po b ON b.ref = a.ref", 2),
            ("SELECT (SELECT sum(total) FROM view_invoice) AS t", 1),
            ("SELECT * FROM view_po WHERE ref IN (SELECT ref FROM view_invoice)", 2),
            ("WITH t AS (SELECT * FROM view_invoice) SELECT count(*) FROM t", 1),
        ],
    )
    def test_every_view_reference_is_scoped(self, sql, expected_refs):
        """One predicate per view reference, wherever in the tree it sits -
        including CTE bodies and correlated/scalar subqueries, which an
        "append to the top-level WHERE" shortcut would silently miss."""
        out = scoped(sql)
        assert overlaps_count(out) == expected_refs

    def test_the_cte_reference_itself_is_not_scoped(self):
        """`t` is a CTE name, not a view. Wrapping it would be both wrong and
        a reference to a relation that doesn't exist."""
        out = scoped("WITH t AS (SELECT * FROM view_invoice) SELECT count(*) FROM t")
        assert overlaps_count(out) == 1  # the body only
        assert "FROM t" in out.replace("\n", " ") or "FROM t)" in out.replace("\n", " ")

    def test_same_view_twice_gets_two_independent_predicates(self):
        out = scoped(
            "SELECT (SELECT count(*) FROM view_invoice) AS n, sum(total) FROM view_invoice"
        )
        assert overlaps_count(out) == 2

    def test_alias_is_preserved(self):
        out = scoped("SELECT i.total FROM view_invoice i WHERE i.total > 100")
        stmt = reparse(out)
        # the derived table must still be addressable as `i`
        aliases = {s.alias_or_name for s in stmt.find_all(exp.Subquery)}
        assert "i" in aliases
        assert "i.total" in out.replace("\n", " ").replace("  ", " ")

    def test_unaliased_reference_is_aliased_with_its_own_name(self):
        """So a pre-existing qualified reference keeps resolving."""
        out = scoped("SELECT view_invoice.total FROM view_invoice")
        aliases = {s.alias_or_name for s in reparse(out).find_all(exp.Subquery)}
        assert "view_invoice" in aliases

    def test_existing_where_is_preserved(self):
        out = scoped("SELECT sum(total) FROM view_invoice WHERE status = 'open'")
        assert "status" in out
        assert overlaps_count(out) == 1

    def test_predicate_lands_before_aggregation(self):
        """In the inner derived table, so it filters rows rather than groups."""
        out = scoped("SELECT supplier, sum(total) FROM view_invoice GROUP BY supplier")
        inner = next(s for s in reparse(out).find_all(exp.Subquery))
        assert inner.this.find(exp.ArrayOverlaps) is not None
        assert inner.this.find(exp.Group) is None

    def test_all_topic_ids_reach_the_predicate(self):
        out = scoped("SELECT * FROM view_invoice", topics=(TOPIC_A, TOPIC_B))
        assert str(TOPIC_A) in out
        assert str(TOPIC_B) in out

    def test_output_still_only_reads_allowed_views(self):
        out = scoped("SELECT * FROM view_invoice a JOIN view_po b ON b.ref = a.ref")
        assert set(view_refs(out)) <= ALLOWED

    def test_document_scope_variant(self):
        """The no-DDL fallback: same enforcement via `document_id IN (...)`."""
        doc = uuid.uuid4()
        out = normalize_sql(
            "SELECT sum(total) FROM view_invoice", ALLOWED, scope=document_scope([doc])
        )
        assert len(list(reparse(out).find_all(exp.In))) == 1
        assert str(doc) in out


class TestUnscoped:
    def test_no_scope_injects_nothing(self):
        sql = "SELECT sum(total) FROM view_invoice"
        out = normalize_sql(sql, ALLOWED)
        assert overlaps_count(out) == 0
        assert not list(reparse(out).find_all(exp.Subquery))

    def test_unscoped_output_matches_qualifier_repair_alone(self):
        """`scope=None` must be byte-identical to the pre-injection
        behaviour, so turning scoping on can never be blamed for a change on
        an unscoped question."""
        sql = "SELECT total FROM invoice.view_invoice WHERE total > 10"
        stmt = parse_and_guard(sql)
        strip_qualifiers(stmt, ALLOWED)
        assert normalize_sql(sql, ALLOWED) == stmt.sql(dialect="postgres")


class TestInjectionContract:
    def test_injection_is_single_pass_by_construction(self):
        """A second call wraps what the first created - which is exactly why
        normalize_sql must never be handed its own output. Pinning the
        behaviour so the constraint is discovered by a failing test rather
        than by a doubled predicate in production."""
        stmt = parse_and_guard("SELECT sum(total) FROM view_invoice")
        assert inject_scope(stmt, ALLOWED, topic_scope([TOPIC_A])) == 1
        assert len(list(stmt.find_all(exp.ArrayOverlaps))) == 1
        assert inject_scope(stmt, ALLOWED, topic_scope([TOPIC_A])) == 1
        assert len(list(stmt.find_all(exp.ArrayOverlaps))) == 2

    def test_returns_the_number_of_rewritten_references(self):
        stmt = parse_and_guard("SELECT * FROM view_invoice a JOIN view_po b ON b.ref = a.ref")
        assert inject_scope(stmt, ALLOWED, topic_scope([TOPIC_A])) == 2

    def test_unknown_relations_are_left_alone_by_injection(self):
        """`inject_scope` is not a rail - `strip_qualifiers` rejects unknown
        relations. Injection simply must not invent scoping for them."""
        stmt = parse_and_guard("SELECT * FROM something_else")
        assert inject_scope(stmt, ALLOWED, topic_scope([TOPIC_A])) == 0


class TestAllowedViewNames:
    def test_derived_from_the_catalog(self):
        catalog = [
            {"schema_name": "invoice", "view_name": "view_invoice", "columns": {}},
            {"schema_name": "po", "view_name": "view_po", "columns": {}},
        ]
        assert allowed_view_names(catalog) == ALLOWED

    def test_a_narrowed_catalog_narrows_the_allowlist(self):
        """Topic filtering the catalog makes an out-of-scope view unnameable -
        the third independent scoping rail."""
        catalog = [{"schema_name": "invoice", "view_name": "view_invoice", "columns": {}}]
        with pytest.raises(UnsafeSqlError):
            normalize_sql("SELECT * FROM view_po", allowed_view_names(catalog))
