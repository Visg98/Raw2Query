"""Coverage for the structured branch's scope ladder (app/query/routing.py).

No Postgres and no OpenAI key: every boundary the branch touches is stubbed,
so what's under test is the branch logic itself. The existing suite stubs
`app.routers.query.answer_query`, which means the routing module's own
decisions have never been exercised - these patch inside `app.query.routing`
instead.
"""

import uuid

import pytest

from app.query import routing
from app.query.sql_guard import UnsafeSqlError

TOPIC_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOC_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

CATALOG = [
    {
        "schema_id": uuid.uuid4(),
        "schema_name": "invoice",
        "view_name": "view_invoice",
        "columns": [
            {"column": "f_1a2b3c4d", "name": "Invoice Total", "type": "number", "description": ""}
        ],
    }
]


@pytest.fixture
def stubs(monkeypatch):
    """Records what the branch did: how many times SQL was generated, and
    which scope each execution attempt was given."""
    calls = {"generate": 0, "scopes": [], "composed": 0}

    def fake_generate_sql(question, catalog):
        calls["generate"] += 1
        return "SELECT sum(total) FROM view_invoice"

    def fake_compose(question, sql, rows):
        calls["composed"] += 1
        return f"composed from {len(rows)} row(s)"

    monkeypatch.setattr(routing, "generate_sql", fake_generate_sql)
    monkeypatch.setattr(routing, "compose_sql_answer", fake_compose)
    monkeypatch.setattr(routing, "_documents_in_topics", lambda session, topic_ids: [DOC_A] if topic_ids else [])
    return calls


def set_rows(monkeypatch, calls, rows_by_attempt):
    """Each execution attempt returns the next entry in `rows_by_attempt`."""
    attempts = iter(rows_by_attempt)

    def fake_run_safe_select(sql, allowed, *, scope=None):
        calls["scopes"].append(scope)
        return sql, next(attempts)

    monkeypatch.setattr(routing, "run_safe_select", fake_run_safe_select)


class TestScopeIsApplied:
    def test_scoped_candidate_gets_a_scope_predicate(self, monkeypatch, stubs):
        set_rows(monkeypatch, stubs, [[{"sum": 100}]])
        result = routing._answer_with_sql(None, "total?", CATALOG, [TOPIC_A], auto_detected=False)

        assert result["topic_ids_used"] == [TOPIC_A]
        assert len(stubs["scopes"]) == 1
        # a callable predicate, not None - the branch did not run unscoped
        assert callable(stubs["scopes"][0])

    def test_no_topics_means_no_predicate(self, monkeypatch, stubs):
        set_rows(monkeypatch, stubs, [[{"sum": 1000}]])
        result = routing._answer_with_sql(None, "total?", CATALOG, [], auto_detected=False)

        assert result["topic_ids_used"] == []
        assert stubs["scopes"] == [None]


class TestSqlIsGeneratedOnce:
    def test_one_generation_even_when_the_ladder_retries(self, monkeypatch, stubs):
        """The point of hoisting generation out of the loop: the retry is the
        same statement under a different scope, not a second LLM roll."""
        set_rows(monkeypatch, stubs, [[], [{"sum": 1000}]])
        result = routing._answer_with_sql(None, "total?", CATALOG, [TOPIC_A], auto_detected=True)

        assert stubs["generate"] == 1
        assert len(stubs["scopes"]) == 2
        assert callable(stubs["scopes"][0])   # scoped attempt
        assert stubs["scopes"][1] is None     # relaxed attempt
        assert result["topic_ids_used"] == []


class TestRelaxationSemantics:
    def test_auto_detected_empty_scope_relaxes(self, monkeypatch, stubs):
        set_rows(monkeypatch, stubs, [[], [{"sum": 1000}]])
        result = routing._answer_with_sql(None, "total?", CATALOG, [TOPIC_A], auto_detected=True)

        assert result["rows"] == [{"sum": 1000}]
        assert result["topic_ids_used"] == [], "a relaxed answer must report that it searched everything"

    def test_explicit_empty_scope_never_relaxes(self, monkeypatch, stubs):
        """An explicit selection is a deliberate narrowing. Overriding it
        would be lying about what was searched (decisions.md)."""
        set_rows(monkeypatch, stubs, [[]])
        result = routing._answer_with_sql(None, "total?", CATALOG, [TOPIC_A], auto_detected=False)

        assert len(stubs["scopes"]) == 1, "explicit scope must not be retried unscoped"
        assert result["rows"] == []
        assert result["topic_ids_used"] == [TOPIC_A]


class TestFallbackToRag:
    def test_rejected_sql_hands_off_to_rag(self, monkeypatch, stubs):
        def boom(sql, allowed, *, scope=None):
            raise UnsafeSqlError("nope")

        monkeypatch.setattr(routing, "run_safe_select", boom)
        assert routing._answer_with_sql(None, "total?", CATALOG, [], auto_detected=False) is None
        assert stubs["composed"] == 0, "a rejected query must never reach answer composition"

    def test_broken_sql_hands_off_to_rag(self, monkeypatch, stubs):
        def boom(sql, allowed, *, scope=None):
            raise RuntimeError("UndefinedColumn")

        monkeypatch.setattr(routing, "run_safe_select", boom)
        assert routing._answer_with_sql(None, "total?", CATALOG, [], auto_detected=False) is None
