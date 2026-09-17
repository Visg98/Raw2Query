"""Chat session history for the Ask page.

Sessions are created by the /query endpoint (app/routers/query.py), never
here: opening "New chat" in the UI is a purely local act until the user
actually asks something, so this router only lists, reads, renames and
deletes what a real question has already created.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import ChatMessage, ChatSession
from app.schemas_pydantic import (
    ChatMessageOut,
    ChatSessionDetailOut,
    ChatSessionOut,
    ChatSessionPatch,
)

router = APIRouter(tags=["chats"])

LIST_LIMIT = 200
TITLE_MAX_CHARS = 80
PREVIEW_MAX_CHARS = 120


def derive_title(question: str) -> str:
    """A session's title is its opening question, trimmed - the same
    convention the user already expects from every other chat UI. Renameable
    via PATCH."""
    flat = " ".join(question.split())
    if not flat:
        return "New chat"
    if len(flat) <= TITLE_MAX_CHARS:
        return flat
    return flat[: TITLE_MAX_CHARS - 1].rstrip() + "…"


def json_safe(value: Any) -> Any:
    """SQL result rows carry Decimal/date/UUID values straight from the
    `view_*` projections, none of which `json.dumps` can encode - and
    `metadata` is a JSONB column, so the insert would fail at commit time
    rather than anywhere near the query that produced them."""
    return json.loads(json.dumps(value, default=str))


def message_out(message: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=message.id,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        meta=message.message_metadata,
    )


def session_out(session_row: ChatSession, message_count: int, last_preview: str | None) -> ChatSessionOut:
    return ChatSessionOut(
        id=session_row.id,
        title=session_row.title,
        created_at=session_row.created_at,
        updated_at=session_row.updated_at,
        message_count=message_count,
        last_message_preview=last_preview,
    )


@router.get("/chats", response_model=list[ChatSessionOut])
def list_chats(db: Session = Depends(get_db)) -> list[ChatSessionOut]:
    """Most recently used first, which is the order the sidebar shows."""
    counts = dict(
        db.query(ChatMessage.session_id, func.count(ChatMessage.id)).group_by(ChatMessage.session_id).all()
    )
    sessions = (
        db.query(ChatSession).order_by(ChatSession.updated_at.desc()).limit(LIST_LIMIT).all()
    )
    if not sessions:
        return []

    # One extra query for the sidebar's subtitle instead of N. Ordered by
    # `position`, not `created_at`: a question and its answer share a
    # timestamp (see ChatMessage.position), so a timestamp sort picks one of
    # the pair at random and the preview would show the question about half
    # the time.
    latest_by_session: dict[uuid.UUID, str] = {}
    session_ids = [s.id for s in sessions]
    rows = (
        db.query(ChatMessage.session_id, ChatMessage.content)
        .filter(ChatMessage.session_id.in_(session_ids))
        .order_by(ChatMessage.session_id, ChatMessage.position.desc())
        .all()
    )
    for session_id, content in rows:
        latest_by_session.setdefault(session_id, " ".join((content or "").split())[:PREVIEW_MAX_CHARS])

    return [session_out(s, counts.get(s.id, 0), latest_by_session.get(s.id)) for s in sessions]


@router.get("/chats/{chat_id}", response_model=ChatSessionDetailOut)
def get_chat(chat_id: uuid.UUID, db: Session = Depends(get_db)) -> ChatSessionDetailOut:
    session_row = (
        db.query(ChatSession)
        .options(selectinload(ChatSession.messages))
        .filter(ChatSession.id == chat_id)
        .one_or_none()
    )
    if session_row is None:
        raise HTTPException(404, "chat not found")
    messages = [message_out(m) for m in session_row.messages]
    base = session_out(session_row, len(messages), messages[-1].content if messages else None)
    return ChatSessionDetailOut(**base.model_dump(), messages=messages)


@router.patch("/chats/{chat_id}", response_model=ChatSessionOut)
def rename_chat(chat_id: uuid.UUID, body: ChatSessionPatch, db: Session = Depends(get_db)) -> ChatSessionOut:
    session_row = db.get(ChatSession, chat_id)
    if session_row is None:
        raise HTTPException(404, "chat not found")
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(422, "title cannot be empty")
    session_row.title = title[:TITLE_MAX_CHARS]
    db.commit()
    db.refresh(session_row)
    count = db.query(func.count(ChatMessage.id)).filter(ChatMessage.session_id == chat_id).scalar() or 0
    return session_out(session_row, count, None)


@router.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    session_row = db.get(ChatSession, chat_id)
    if session_row is None:
        raise HTTPException(404, "chat not found")
    # Messages go with it (ondelete=CASCADE plus the ORM cascade).
    db.delete(session_row)
    db.commit()
    return Response(status_code=204)
