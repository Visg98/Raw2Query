"""ORM -> Pydantic assembly for response shapes that need more than a plain
`from_attributes` mapping (computed fields, nested relationships)."""

import uuid

from sqlalchemy.orm import Session

from app.models import Chunk, Document, ExtractedRecord, Job, SchemaDef, SchemaVersion
from app.pipeline.views import view_name_for
from app.schemas_pydantic import (
    DocumentDetailOut,
    DocumentOut,
    JobOut,
    JobReviewOut,
    SchemaOut,
    SchemaVersionOut,
    TopicOut,
)


def document_out(document: Document) -> DocumentOut:
    topics = [TopicOut.model_validate(link.topic) for link in document.topic_links]
    latest_job = max(document.jobs, key=lambda j: j.created_at) if document.jobs else None
    return DocumentOut(
        id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        file_size=document.file_size,
        uploaded_at=document.uploaded_at,
        batch_id=document.batch_id,
        schema_id=document.schema_id,
        schema_version_id=document.schema_version_id,
        topics=topics,
        latest_job_status=latest_job.status if latest_job else None,
    )


def document_detail_out(document: Document, session: Session) -> DocumentDetailOut:
    base = document_out(document)
    latest_record = (
        session.query(ExtractedRecord)
        .filter(ExtractedRecord.document_id == document.id)
        .order_by(ExtractedRecord.created_at.desc())
        .first()
    )
    chunk_count = session.query(Chunk).filter(Chunk.document_id == document.id).count()
    return DocumentDetailOut(
        **base.model_dump(),
        extracted_record=latest_record.data if latest_record else None,
        chunk_count=chunk_count,
        jobs=[job_out(j) for j in document.jobs],
    )


def job_out(job: Job) -> JobOut:
    return JobOut.model_validate(job)


def _review_result_out(result: dict | None) -> dict | None:
    """`jobs.result` as the review screen should see it: chunks without their
    embedding vectors.

    A 384-float array per chunk, times dozens of chunks, is the bulk of this
    response, and nothing renders it - the review queue fetches one of these
    per row. `confirm_job` reads the embeddings server-side out of
    `jobs.result`, so the client never needs them back.

    COPY, never pop. `job.result` is a mutable JSONB attribute: mutating the
    stored chunk dicts in place would be flushed by the next `db.commit()` -
    and `patch_review` commits on the very same request - permanently
    destroying the embeddings that confirm depends on. The failure would be
    silent and only surface later as a document with no vectors.
    """
    if result is None:
        return None
    chunks = result.get("chunks")
    if not chunks:
        return result
    return {
        **result,
        "chunks": [{k: v for k, v in (chunk or {}).items() if k != "embedding"} for chunk in chunks],
    }


def job_review_out(job: Job) -> JobReviewOut:
    return JobReviewOut(
        id=job.id,
        status=job.status,
        result=_review_result_out(job.result),
        document=document_out(job.document),
    )


def schema_version_out(version: SchemaVersion) -> SchemaVersionOut:
    """`identity_fields` is stored as column keys but is exposed as field
    *names*, so no client has to know keys exist.

    Keys are what's persisted because names orphan: renaming a field used to
    leave `identity_fields` pointing at a name nothing declared any more, and
    `_find_dedup_match` then bailed on the first identity field - dedup
    silently became a no-op. Names remain the API's currency in both
    directions (see `_identity_keys` on the way in).
    """
    names_by_key = {f.get("column"): f.get("name") for f in (version.fields or [])}
    return SchemaVersionOut(
        id=version.id,
        schema_id=version.schema_id,
        version=version.version,
        fields=version.fields or [],
        identity_fields=(
            [names_by_key.get(key) or key for key in version.identity_fields]
            if version.identity_fields
            else None
        ),
        is_breaking_from_prev=version.is_breaking_from_prev,
        created_at=version.created_at,
    )


def schema_out(
    schema: SchemaDef,
    *,
    document_count: int | None = None,
    enqueued_backfill_job_ids: list[uuid.UUID] | None = None,
) -> SchemaOut:
    """The two keyword-only extras are supplied by the callers that have them
    to hand: `document_count` needs a query, and the enqueued job ids only
    exist on the one request that creates them. Neither is derived here, so
    listing schemas stays a single query rather than N+1."""
    current = schema.versions[-1] if schema.versions else None
    return SchemaOut(
        id=schema.id,
        name=schema.name,
        created_at=schema.created_at,
        view_name=view_name_for(schema.name),
        current_version=schema_version_out(current) if current else None,
        document_count=document_count,
        enqueued_backfill_job_ids=enqueued_backfill_job_ids,
    )
