"""Chat sessions: creation timing, turn ordering, listing, rename, delete.

`answer_query` is stubbed throughout - these tests are about the session
bookkeeping around the answer, and calling a real LLM would make them slow,
non-deterministic and dependent on an API key.

The two properties worth being strict about:

* A session is created by the *first answered question*, never before. The
  frontend can open any number of empty "New chat" panes without leaving
  rows behind, which is the behaviour the UI promises.
* A question and its answer keep their order. They're inserted in one
  transaction and Postgres' `now()` is transaction-scoped, so both rows
  share a `created_at` - ordering by it would let the answer sort above the
  question that produced it. Hence `chat_messages.position`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def stub_answer(monkeypatch):
    """Replace the LLM with a recorded answer, and hand back a knob for
    what the next call returns."""
    state = {
        "answer": "Two invoices, totalling 83,880.00.",
        "routing_used": "rag",
        "sql": None,
        "rows": None,
        "sources": [],
        "topic_ids_used": [],
    }

    def fake_answer_query(session, question, topic_ids):
        return dict(state)

    monkeypatch.setattr("app.routers.query.answer_query", fake_answer_query)
    return state


@pytest.fixture
def cleanup_chats(db_session):
    """Track created sessions and remove them, so a test run doesn't
    accumulate rows in the developer's own database."""
    created: list[uuid.UUID] = []
    yield created
    from app.models import ChatSession

    for chat_id in created:
        row = db_session.get(ChatSession, chat_id)
        if row is not None:
            db_session.delete(row)
    db_session.commit()


def ask(client, question, chat_id=None):
    body = {"question": question}
    if chat_id is not None:
        body["chat_id"] = str(chat_id)
    response = client.post("/query", json=body)
    assert response.status_code == 200, response.text
    return response.json()


class TestSessionCreationTiming:
    def test_first_question_creates_the_session(self, client, stub_answer, cleanup_chats):
        result = ask(client, "What invoices do we have?")
        assert result["chat_id"]
        cleanup_chats.append(uuid.UUID(result["chat_id"]))

        detail = client.get(f"/chats/{result['chat_id']}").json()
        assert detail["message_count"] == 2
        # Titled from the opening question, as every chat UI does.
        assert detail["title"] == "What invoices do we have?"

    def test_listing_chats_does_not_create_one(self, client, stub_answer):
        """The frontend opens "New chat" as local state only. Nothing it can
        do before asking should produce a row."""
        before = len(client.get("/chats").json())
        client.get("/chats")
        assert len(client.get("/chats").json()) == before

    def test_second_question_reuses_the_session(self, client, stub_answer, cleanup_chats):
        first = ask(client, "What invoices do we have?")
        chat_id = first["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))

        second = ask(client, "Which is the largest?", chat_id=chat_id)
        assert second["chat_id"] == chat_id
        assert client.get(f"/chats/{chat_id}").json()["message_count"] == 4

    def test_questions_without_a_chat_id_are_separate_conversations(
        self, client, stub_answer, cleanup_chats
    ):
        first = ask(client, "First topic?")
        second = ask(client, "Unrelated topic?")
        cleanup_chats.extend([uuid.UUID(first["chat_id"]), uuid.UUID(second["chat_id"])])
        assert first["chat_id"] != second["chat_id"]

    def test_unknown_chat_id_is_rejected(self, client, stub_answer):
        response = client.post(
            "/query",
            json={"question": "hi", "chat_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert response.status_code == 404

    def test_a_failed_question_leaves_no_session_behind(self, client, monkeypatch):
        """Nothing is persisted for a question that couldn't be answered, so
        the sidebar doesn't fill up with empty titled conversations."""

        def exploding_answer_query(session, question, topic_ids):
            raise RuntimeError("the model is down")

        monkeypatch.setattr("app.routers.query.answer_query", exploding_answer_query)
        before = len(client.get("/chats").json())
        with pytest.raises(RuntimeError):
            client.post("/query", json={"question": "What invoices do we have?"})
        assert len(client.get("/chats").json()) == before


class TestTurnOrdering:
    def test_question_precedes_its_answer(self, client, stub_answer, cleanup_chats):
        chat_id = ask(client, "Question one?")["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))
        ask(client, "Question two?", chat_id=chat_id)

        messages = client.get(f"/chats/{chat_id}").json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[0]["content"] == "Question one?"
        assert messages[2]["content"] == "Question two?"

    def test_both_turns_of_a_pair_share_a_timestamp(self, client, stub_answer, cleanup_chats, db_session):
        """The reason `position` exists: ordering by `created_at` alone would
        be a coin flip. If this ever stops being true the column is still
        correct, but the comments explaining it are not."""
        from app.models import ChatMessage

        chat_id = uuid.UUID(ask(client, "Question one?")["chat_id"])
        cleanup_chats.append(chat_id)
        rows = (
            db_session.query(ChatMessage)
            .filter(ChatMessage.session_id == chat_id)
            .order_by(ChatMessage.position)
            .all()
        )
        assert [r.position for r in rows] == [0, 1]
        assert rows[0].created_at == rows[1].created_at

    def test_positions_keep_increasing_across_turns(self, client, stub_answer, cleanup_chats, db_session):
        from app.models import ChatMessage

        chat_id = uuid.UUID(ask(client, "One?")["chat_id"])
        cleanup_chats.append(chat_id)
        ask(client, "Two?", chat_id=chat_id)
        ask(client, "Three?", chat_id=chat_id)
        positions = [
            r.position
            for r in db_session.query(ChatMessage)
            .filter(ChatMessage.session_id == chat_id)
            .order_by(ChatMessage.position)
            .all()
        ]
        assert positions == [0, 1, 2, 3, 4, 5]


class TestAnswerMetadata:
    def test_answer_detail_is_replayed_on_reload(self, client, stub_answer, cleanup_chats):
        """A reopened conversation must render identically to the live one,
        so routing/sql/rows/sources ride along with the message."""
        stub_answer.update(
            {
                "routing_used": "sql",
                "sql": "SELECT count(*) FROM view_invoice",
                "rows": [{"count": 2}],
                "sources": [
                    {
                        "document_id": str(uuid.uuid4()),
                        "chunk_id": str(uuid.uuid4()),
                        "snippet": "TAX INVOICE",
                        "filename": "invoice.pdf",
                        "page_number": 1,
                    }
                ],
            }
        )
        chat_id = ask(client, "How many invoices?")["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))

        answer = client.get(f"/chats/{chat_id}").json()["messages"][1]
        assert answer["meta"]["routing_used"] == "sql"
        assert answer["meta"]["sql"] == "SELECT count(*) FROM view_invoice"
        assert answer["meta"]["rows"] == [{"count": 2}]
        assert answer["meta"]["sources"][0]["filename"] == "invoice.pdf"
        assert answer["meta"]["sources"][0]["page_number"] == 1

    def test_sql_value_types_survive_persistence(self, client, stub_answer, cleanup_chats):
        """`view_*` projections hand back Decimal/date/UUID values, none of
        which JSONB encoding accepts - without normalising them the whole
        request would fail at commit, well away from the query that made
        them."""
        import datetime

        stub_answer.update(
            {
                "routing_used": "sql",
                "sql": "SELECT * FROM view_invoice",
                "rows": [
                    {
                        "total": Decimal("83880.00"),
                        "issued": datetime.date(2026, 3, 1),
                        "document_id": uuid.uuid4(),
                        "missing": None,
                    }
                ],
            }
        )
        chat_id = ask(client, "Show me the invoices")["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))

        row = client.get(f"/chats/{chat_id}").json()["messages"][1]["meta"]["rows"][0]
        assert row["total"] == "83880.00"
        assert row["issued"] == "2026-03-01"
        assert row["missing"] is None


class TestListingRenameDelete:
    def test_listing_shows_newest_first_with_the_latest_answer(
        self, client, stub_answer, cleanup_chats
    ):
        older = ask(client, "Older conversation?")["chat_id"]
        stub_answer["answer"] = "The newest answer."
        newer = ask(client, "Newer conversation?")["chat_id"]
        cleanup_chats.extend([uuid.UUID(older), uuid.UUID(newer)])

        listed = client.get("/chats").json()
        ids = [c["id"] for c in listed]
        assert ids.index(newer) < ids.index(older), "most recently used must come first"

        newest = next(c for c in listed if c["id"] == newer)
        assert newest["message_count"] == 2
        # The preview is the newest message, i.e. the answer - not the
        # question, which is what a `created_at` sort would have picked
        # about half the time.
        assert newest["last_message_preview"] == "The newest answer."

    def test_a_new_turn_moves_a_conversation_back_to_the_top(
        self, client, stub_answer, cleanup_chats
    ):
        first = ask(client, "Conversation A?")["chat_id"]
        second = ask(client, "Conversation B?")["chat_id"]
        cleanup_chats.extend([uuid.UUID(first), uuid.UUID(second)])
        assert client.get("/chats").json()[0]["id"] == second

        ask(client, "Following up on A", chat_id=first)
        assert client.get("/chats").json()[0]["id"] == first

    def test_rename(self, client, stub_answer, cleanup_chats):
        chat_id = ask(client, "A question with a long and unhelpful title")["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))
        response = client.patch(f"/chats/{chat_id}", json={"title": "  Invoice   digging  "})
        assert response.status_code == 200
        # Whitespace is collapsed, since the title is rendered on one line.
        assert response.json()["title"] == "Invoice digging"
        assert client.get(f"/chats/{chat_id}").json()["title"] == "Invoice digging"

    def test_rename_rejects_an_empty_title(self, client, stub_answer, cleanup_chats):
        chat_id = ask(client, "A question")["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))
        assert client.patch(f"/chats/{chat_id}", json={"title": "   "}).status_code == 422

    def test_long_questions_make_a_trimmed_title(self, client, stub_answer, cleanup_chats):
        question = "Tell me everything about " + "invoices " * 40
        chat_id = ask(client, question)["chat_id"]
        cleanup_chats.append(uuid.UUID(chat_id))
        title = client.get(f"/chats/{chat_id}").json()["title"]
        assert len(title) <= 80
        assert title.endswith("…")

    def test_delete_removes_the_conversation_and_its_messages(self, client, stub_answer, db_session):
        from app.models import ChatMessage

        chat_id = ask(client, "Disposable question?")["chat_id"]
        assert client.delete(f"/chats/{chat_id}").status_code == 204
        assert client.get(f"/chats/{chat_id}").status_code == 404
        orphans = (
            db_session.query(ChatMessage).filter(ChatMessage.session_id == uuid.UUID(chat_id)).count()
        )
        assert orphans == 0, "messages outlived their conversation"

    def test_deleting_twice_404s(self, client, stub_answer):
        chat_id = ask(client, "Disposable question?")["chat_id"]
        assert client.delete(f"/chats/{chat_id}").status_code == 204
        assert client.delete(f"/chats/{chat_id}").status_code == 404
