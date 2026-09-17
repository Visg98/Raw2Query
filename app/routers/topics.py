"""Topic CRUD + typeahead (decisions #5, #8)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DocumentTopic, Job, Topic
from app.pipeline.topics_util import (
    UNCATEGORIZED_TOPIC_NAME,
    get_or_create_topic,
    get_or_create_uncategorized,
    resync_chunk_topic_ids,
)
from app.schemas_pydantic import TopicCreate, TopicDeleteResponse, TopicOut

router = APIRouter(tags=["topics"])

TYPEAHEAD_LIMIT = 50

# Job statuses whose `result` blob is still staging - i.e. still carries a
# `suggested_topic_ids` list that a later confirm will turn into real
# `document_topics` rows. A confirmed/rejected job's result is history.
_STAGED_JOB_STATUSES = ("pending", "extracting", "awaiting_review", "failed")


@router.get("/topics", response_model=list[TopicOut])
def search_topics(
    q: str = "",
    ids: list[uuid.UUID] | None = Query(None),
    db: Session = Depends(get_db),
) -> list[Topic]:
    """Typeahead for the chip input (decision #5). `citext` makes this
    case-insensitive without any extra work.

    `ids` may be repeated (`?ids=<uuid>&ids=<uuid>`) to resolve specific
    topics by id, ignoring `q` and the typeahead limit. The chip input needs
    that to label an already-selected topic: a selected/suggested id is not
    necessarily inside the current search page, and a chip showing a raw
    uuid instead of its name reads as the topic having vanished.
    """
    query = db.query(Topic)
    if ids:
        return query.filter(Topic.id.in_(ids)).order_by(Topic.name).all()
    if q:
        query = query.filter(Topic.name.contains(q))
    return query.order_by(Topic.name).limit(TYPEAHEAD_LIMIT).all()


@router.post("/topics", response_model=TopicOut)
def create_topic(body: TopicCreate, db: Session = Depends(get_db)) -> Topic:
    """Get-or-create, case-insensitive (decision #8) - "+ Create '<text>'" in
    the chip input never errors on an existing name, it just returns it."""
    topic = get_or_create_topic(db, body.name, body.description)
    db.commit()
    return topic


def _clear_topic_from_staged_jobs(db: Session, topic_id: uuid.UUID) -> int:
    """Drops the topic id out of every still-staged job's suggested topics.

    Not cosmetic: `jobs.result.suggested_topic_ids` is what `confirm_job`
    turns into `document_topics` rows, so an id left behind here would make
    the next confirm insert a link to a topic that no longer exists and fail
    the whole confirm on a foreign key - long after the delete, and on a
    screen that has nothing to do with topics.
    """
    needle = str(topic_id)
    cleared = 0
    jobs = (
        db.query(Job)
        .filter(Job.status.in_(_STAGED_JOB_STATUSES), Job.result.isnot(None))
        .all()
    )
    for job in jobs:
        suggested = (job.result or {}).get("suggested_topic_ids")
        if not isinstance(suggested, list) or needle not in suggested:
            continue
        # A new dict, not an in-place mutation: SQLAlchemy tracks JSONB
        # columns by assignment, so mutating the nested list would not mark
        # the row dirty and the UPDATE would never be emitted.
        job.result = {**job.result, "suggested_topic_ids": [t for t in suggested if t != needle]}
        cleared += 1
    return cleared


@router.delete("/topics/{topic_id}", response_model=TopicDeleteResponse)
def delete_topic(topic_id: uuid.UUID, db: Session = Depends(get_db)) -> TopicDeleteResponse:
    """Deletes a topic and unlinks it from everything that referenced it.

    Documents, chunks and extracted records all survive - a topic is a label
    on data, not a container for it, so deleting one must never delete what
    was filed under it. What goes is the label: the `document_topics` links,
    the denormalized copies in `chunks.topic_ids`, the per-batch upload hint
    in `batches.default_topic_ids`, and the staged suggestions in
    `jobs.result`.

    A document the delete leaves with no topics at all is re-tagged
    `Uncategorized` (decision #16) rather than left bare, so "no topic" stays
    one addressable thing on the Ask page instead of an unreachable gap.
    That is also why `Uncategorized` itself cannot be deleted: it is the
    fallback every untagged document lands on.
    """
    topic = db.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(404, "topic not found")
    if topic.name == UNCATEGORIZED_TOPIC_NAME:
        raise HTTPException(
            400,
            f"{UNCATEGORIZED_TOPIC_NAME!r} cannot be deleted - it is the fallback topic every "
            "document with no other topic is filed under",
        )

    document_ids = [
        row[0] for row in db.query(DocumentTopic.document_id).filter(DocumentTopic.topic_id == topic_id).all()
    ]

    db.query(DocumentTopic).filter(DocumentTopic.topic_id == topic_id).delete(synchronize_session=False)
    db.flush()

    # Whatever is left of those documents' topic links after the delete. The
    # difference is what has just become untagged.
    still_linked = {
        row[0]
        for row in db.query(DocumentTopic.document_id)
        .filter(DocumentTopic.document_id.in_(document_ids))
        .all()
    } if document_ids else set()
    orphaned = [document_id for document_id in document_ids if document_id not in still_linked]
    if orphaned:
        uncategorized = get_or_create_uncategorized(db)
        for document_id in orphaned:
            db.add(DocumentTopic(document_id=document_id, topic_id=uncategorized.id))
        db.flush()

    # After the re-tagging, so an orphaned document's chunks pick up
    # Uncategorized in the same pass rather than being left with '{}'.
    resynced = resync_chunk_topic_ids(db, document_ids)

    # `batches.default_topic_ids` is a plain uuid[] with no foreign key, so
    # nothing at the database level would have caught a dangling id here - it
    # would just quietly re-apply a dead topic to the next upload in that
    # batch.
    db.execute(
        text(
            "UPDATE batches SET default_topic_ids = array_remove(default_topic_ids, :topic_id) "
            "WHERE :topic_id = ANY(default_topic_ids)"
        ),
        {"topic_id": str(topic_id)},
    )

    cleared_jobs = _clear_topic_from_staged_jobs(db, topic_id)

    db.delete(topic)
    db.commit()

    return TopicDeleteResponse(
        unlinked_document_count=len(document_ids),
        uncategorized_document_count=len(orphaned),
        resynced_chunk_count=resynced,
        cleared_job_count=cleared_jobs,
    )
