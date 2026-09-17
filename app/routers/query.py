"""The NL query endpoint (decisions #6, #18, #20; plan section 3 call sites 9-12)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ChatMessage, ChatSession
from app.query.routing import answer_query
from app.routers.chats import derive_title, json_safe
from app.schemas_pydantic import QueryRequest, QueryResponse

router = APIRouter(tags=["query"])

# Fields of the answer that are replayed into the UI when a saved
# conversation is reopened.
_ANSWER_META_KEYS = ("routing_used", "sql", "rows", "sources", "topic_ids_used")


@router.post("/query", response_model=QueryResponse)
def run_query(body: QueryRequest, db: Session = Depends(get_db)) -> QueryResponse:
    """Answers the question and appends both turns to a chat session.

    `chat_id` omitted means the user is in a new, not-yet-saved chat: the
    session row is created *here*, after the question is answered. That's
    deliberate - the frontend can open as many empty "New chat" panes as the
    user clicks without any of them turning into a stray row, and a question
    that fails outright doesn't leave a titled conversation with nothing in
    it either.
    """
    session_row: ChatSession | None = None
    if body.chat_id is not None:
        session_row = db.get(ChatSession, body.chat_id)
        if session_row is None:
            raise HTTPException(404, "chat not found")

    result = answer_query(db, body.question, body.topic_ids)

    if session_row is None:
        session_row = ChatSession(title=derive_title(body.question))
        db.add(session_row)
        db.flush()
        next_position = 0
    else:
        # Explicit turn ordering: both rows below land in the same
        # transaction, and `created_at` defaults to Postgres' `now()`, which
        # is transaction-scoped - so they'd share a timestamp and the answer
        # could sort above its own question.
        highest = (
            db.query(func.max(ChatMessage.position)).filter(ChatMessage.session_id == session_row.id).scalar()
        )
        next_position = 0 if highest is None else highest + 1

    db.add(
        ChatMessage(
            session_id=session_row.id, position=next_position, role="user", content=body.question
        )
    )
    db.add(
        ChatMessage(
            session_id=session_row.id,
            position=next_position + 1,
            role="assistant",
            content=result["answer"],
            # json_safe: SQL rows arrive as Decimal/date/UUID, which JSONB
            # encoding can't take.
            message_metadata=json_safe({k: result.get(k) for k in _ANSWER_META_KEYS}),
        )
    )
    # Appending children doesn't mark the session dirty, so `onupdate` never
    # fires - bump it by hand or the sidebar's ordering freezes at creation.
    session_row.updated_at = func.now()
    db.commit()

    return QueryResponse(**result, chat_id=session_row.id)
