"""Get-or-create topic helper (decision #8: case-insensitive uniqueness via
`citext`, so a plain equality comparison is already case-insensitive) plus
the default "Uncategorized" topic (decision #16)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Topic

UNCATEGORIZED_TOPIC_NAME = "Uncategorized"


def get_or_create_topic(session: Session, name: str, description: str | None = None) -> Topic:
    name = name.strip()
    existing = session.query(Topic).filter(Topic.name == name).first()
    if existing is not None:
        return existing
    topic = Topic(name=name, description=description)
    session.add(topic)
    session.flush()
    return topic


def get_or_create_uncategorized(session: Session) -> Topic:
    return get_or_create_topic(session, UNCATEGORIZED_TOPIC_NAME, "Documents with no applicable topic.")


def resync_chunk_topic_ids(session: Session, document_ids: Iterable[uuid.UUID]) -> int:
    """Rewrites `chunks.topic_ids` from `document_topics` for these documents.

    `chunks.topic_ids` is a denormalized copy written once, at confirm time
    (`app/pipeline/confirm.py`), so anything that changes topic membership
    afterwards has to push the change down or retrieval keeps scoping chunks
    by a topic that no longer applies - and after a topic delete, by a topic
    id that no longer resolves to a row at all.

    The generated `view_<schema>` tables need no equivalent: they read
    `document_topics` live through a LEFT JOIN, so they follow the source of
    truth on their own.

    `COALESCE(..., '{}')` rather than leaving NULL: `NULL && ARRAY[...]` is
    NULL, not false, so a NULL here would make the chunk unreachable through
    a scoped search *and* through an unscoped one. `'{}'` is the empty-topic
    encoding the rest of the retrieval path already assumes (see
    `app/pipeline/views.py`).

    Returns the number of chunk rows rewritten.
    """
    ids = list(document_ids)
    if not ids:
        return 0
    result = session.execute(
        text(
            "UPDATE chunks c SET topic_ids = COALESCE("
            "  (SELECT array_agg(DISTINCT dt.topic_id) FROM document_topics dt"
            "   WHERE dt.document_id = c.document_id), '{}'::uuid[]) "
            "WHERE c.document_id = ANY(:document_ids)"
        ),
        {"document_ids": ids},
    )
    return result.rowcount or 0
