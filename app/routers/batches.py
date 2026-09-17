"""Batch review UX (decision #13)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Batch, Chunk, Document, DocumentTopic, ExtractedRecord, Job
from app.pipeline.confirm import ConfirmError, confirm_job, is_job_clean
from app.schemas_pydantic import (
    BatchDeleteResponse,
    BatchOut,
    ConfirmAllCleanResponse,
    JobConfirmRequest,
)
from app.serializers import document_out, job_out
from app.storage import delete_file

router = APIRouter(tags=["batches"])


def _get_batch_or_404(db: Session, batch_id: uuid.UUID) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    return batch


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: uuid.UUID, db: Session = Depends(get_db)) -> BatchOut:
    batch = _get_batch_or_404(db, batch_id)
    return BatchOut(
        id=batch.id,
        created_at=batch.created_at,
        documents=[document_out(d) for d in batch.documents],
        jobs=[job_out(j) for j in batch.jobs],
    )


@router.post("/batches/{batch_id}/confirm-all-clean", response_model=ConfirmAllCleanResponse)
def confirm_all_clean(batch_id: uuid.UUID, db: Session = Depends(get_db)) -> ConfirmAllCleanResponse:
    """Bulk-confirm every job with no low-confidence flag and no dedup
    conflict; anything else is left for individual review (decision #13)."""
    batch = _get_batch_or_404(db, batch_id)
    threshold = get_settings().confidence_threshold

    confirmed: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []
    for job in batch.jobs:
        if job.status != "awaiting_review":
            continue
        if not is_job_clean(job, threshold):
            skipped.append(job.id)
            continue
        try:
            confirm_job(db, job, JobConfirmRequest())
            confirmed.append(job.id)
        except ConfirmError:
            skipped.append(job.id)

    db.commit()
    return ConfirmAllCleanResponse(confirmed_job_ids=confirmed, skipped_job_ids=skipped)


def _has_committed_data(db: Session, document: Document) -> bool:
    """Whether this document's extraction has ever landed in the real
    tables.

    `confirm_job` is the only thing that writes `extracted_records`,
    `chunks` or `document_topics`, so a job's status is the rule: anything
    short of `confirmed` is pure staging inside `jobs.result`. The row
    counts below are belt and braces - a document carrying records or
    chunks is live queryable data whatever its jobs claim, and silently
    dropping that on a queue tidy-up is the one outcome worth two extra
    cheap queries to rule out.
    """
    if any(job.status == "confirmed" for job in document.jobs):
        return True
    if db.query(ExtractedRecord.id).filter(ExtractedRecord.document_id == document.id).first() is not None:
        return True
    return db.query(Chunk.id).filter(Chunk.document_id == document.id).first() is not None


@router.delete("/batches/{batch_id}", response_model=BatchDeleteResponse)
def delete_batch(batch_id: uuid.UUID, db: Session = Depends(get_db)) -> BatchDeleteResponse:
    """Discards a batch's unconfirmed work - the "delete" on the extraction
    and review queues.

    Only documents that have never been confirmed go: their uploads, jobs
    and staged `jobs.result` payloads are removed and the originals are
    unlinked from disk. A confirmed document is kept (and reported back in
    `kept_document_ids`), which also keeps the batch row alive for grouping;
    deleting one would mean deleting extracted records and chunks that the
    Ask page and the schema tables are already serving, and that is a
    different, much louder operation than clearing a queue.

    A job that is mid-extraction is deleted too, deliberately: a wedged
    `extracting` row is exactly what someone is trying to clear. The worker
    holding it finds its row gone at commit time and drops the run - see
    `process_job` in app/worker.py.
    """
    batch = _get_batch_or_404(db, batch_id)

    deleted_document_ids: list[uuid.UUID] = []
    deleted_job_ids: list[uuid.UUID] = []
    kept_document_ids: list[uuid.UUID] = []
    storage_paths: list[str] = []

    for document in list(batch.documents):
        if _has_committed_data(db, document):
            kept_document_ids.append(document.id)
            continue
        for job in list(document.jobs):
            deleted_job_ids.append(job.id)
            db.delete(job)
        # Nothing unconfirmed should have topic links, but `document_topics`
        # has no cascade at the DB level, so an unexpected row here would
        # surface as an FK violation rather than as a clean delete.
        db.query(DocumentTopic).filter(DocumentTopic.document_id == document.id).delete(synchronize_session=False)
        storage_paths.append(document.storage_path)
        deleted_document_ids.append(document.id)
        db.delete(document)

    db.flush()

    # The batch row goes only once nothing points at it any more.
    still_referenced = (
        db.query(Document.id).filter(Document.batch_id == batch.id).first() is not None
        or db.query(Job.id).filter(Job.batch_id == batch.id).first() is not None
    )
    if not still_referenced:
        db.delete(batch)

    db.commit()

    # After the commit, never before: an un-deleted row can be rolled back,
    # an unlinked file cannot.
    for path in storage_paths:
        delete_file(path)

    return BatchDeleteResponse(
        deleted_document_ids=deleted_document_ids,
        deleted_job_ids=deleted_job_ids,
        kept_document_ids=kept_document_ids,
        batch_deleted=not still_referenced,
    )
