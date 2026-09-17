"""Job status, SSE progress, and the review/confirm/reject/retry flow
(decisions #12, #19; plan section 5)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import Job, SchemaDef
from app.pipeline.confirm import ConfirmError, confirm_job
from app.schemas_pydantic import (
    JobConfirmRequest,
    JobOut,
    JobReextractRequest,
    JobReviewOut,
    JobReviewPatch,
)
from app.serializers import job_out, job_review_out

router = APIRouter(tags=["jobs"])

TERMINAL_STATUSES = {"awaiting_review", "confirmed", "rejected", "failed"}


def _get_job_or_404(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobOut:
    return job_out(_get_job_or_404(db, job_id))


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: uuid.UUID) -> StreamingResponse:
    """SSE stream of status/progress (decision #12). Persisted job status in
    Postgres is the source of truth; this just polls the same row and
    streams deltas - a dropped connection never loses a completed
    extraction, the client can reload and read `GET /jobs/{id}` instead."""

    async def event_stream():
        last_payload = None
        while True:
            db = SessionLocal()
            try:
                job = db.get(Job, job_id)
                if job is None:
                    yield 'event: error\ndata: {"error": "job not found"}\n\n'
                    return
                payload = {"status": job.status, "progress": job.progress, "error_message": job.error_message}
            finally:
                db.close()

            if payload != last_payload:
                yield f"data: {json.dumps(payload)}\n\n"
                last_payload = payload
            if payload["status"] in TERMINAL_STATUSES:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _surviving_confidence(
    old_objects: list[dict], old_confidence: list[dict], new_objects: list[dict]
) -> list[dict]:
    """The model's scores that still describe the reviewer's edited table.

    A score is kept only where the value at that exact index AND key is
    unchanged. So a hand-typed value never inherits the confidence of whatever
    the model put there before, and an added, deleted or reordered row starts
    with no scores at all rather than silently borrowing its neighbour's.

    Scoring survives per cell rather than per table: editing one cell of a
    40-line invoice used to blank the flags on all 40 rows, which made the
    flags useless on exactly the documents that need them. The "Save draft and
    Confirm both resend the whole table, so an untouched draft must keep its
    flags" property falls out of the same rule - an identical resend changes
    no value, so it drops no score.
    """
    surviving = []
    for index, new_object in enumerate(new_objects):
        if index >= len(old_objects):
            surviving.append({})
            continue
        old_object = old_objects[index] or {}
        scores = old_confidence[index] if index < len(old_confidence) else None
        surviving.append(
            {
                key: value
                for key, value in (scores or {}).items()
                if key in (new_object or {}) and (new_object or {})[key] == old_object.get(key)
            }
        )
    return surviving


@router.get("/jobs/{job_id}/review", response_model=JobReviewOut)
def get_review(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobReviewOut:
    return job_review_out(_get_job_or_404(db, job_id))


@router.patch("/jobs/{job_id}/review", response_model=JobReviewOut)
def patch_review(job_id: uuid.UUID, patch: JobReviewPatch, db: Session = Depends(get_db)) -> JobReviewOut:
    """User edits from the review screen, merged into `jobs.result` - the
    pending/staging payload (decision #19) - before it is ever confirmed."""
    job = _get_job_or_404(db, job_id)
    if job.status != "awaiting_review":
        raise HTTPException(400, "job is not awaiting review")

    result = dict(job.result or {})
    if patch.extracted_data is not None:
        # Recomputed before the overwrite, since it is defined against what is
        # currently stored.
        result["data_confidence"] = _surviving_confidence(
            result.get("extracted_data") or [],
            result.get("data_confidence") or [],
            patch.extracted_data,
        )
        result["extracted_data"] = patch.extracted_data
    if patch.proposed_schema_fields is not None:
        result["proposed_schema_fields"] = [f.model_dump() for f in patch.proposed_schema_fields]
    if patch.matched_schema_id is not None:
        schema = db.get(SchemaDef, patch.matched_schema_id)
        if schema is None or not schema.versions:
            raise HTTPException(400, "unknown schema_id")
        result["matched_schema_id"] = str(patch.matched_schema_id)
        result["schema_version_id"] = str(schema.versions[-1].id)
    if patch.topic_ids is not None:
        result["suggested_topic_ids"] = [str(t) for t in patch.topic_ids]
    if patch.new_topic_names is not None:
        result["confirmed_new_topic_names"] = patch.new_topic_names

    job.result = result
    db.commit()
    return job_review_out(job)


@router.post("/jobs/{job_id}/confirm", response_model=JobOut)
def confirm_job_endpoint(job_id: uuid.UUID, body: JobConfirmRequest = JobConfirmRequest(), db: Session = Depends(get_db)) -> JobOut:
    job = _get_job_or_404(db, job_id)
    try:
        confirm_job(db, job, body)
    except ConfirmError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return job_out(job)


@router.post("/jobs/{job_id}/reextract", response_model=JobOut)
def reextract_job(
    job_id: uuid.UUID, body: JobReextractRequest, db: Session = Depends(get_db)
) -> JobOut:
    """Re-run extraction on an already-extracted job, with reviewer feedback.

    The third exit from the review screen, alongside confirm and reject: the
    reviewer can see the extraction is wrong, say why, and get another one -
    rather than hand-correcting a table the model filled in badly, or
    rejecting the document and re-uploading it.

    Re-runs on the SAME job rather than enqueueing a new one. A second job
    would list the same document twice in the review queue and strand this
    job's draft (the topic and schema choices the reviewer already made), and
    the review URL would go stale.

    Replacing the staged table means the reviewer's own cell edits are lost -
    which is why the UI confirms first. Deliberately not merged: the point of
    a re-extract is a fresh read of the document, and interleaving it with
    edits made against the previous read would produce a table that matches
    neither.
    """
    job = _get_job_or_404(db, job_id)
    if job.status != "awaiting_review":
        raise HTTPException(400, "only jobs awaiting review can be re-extracted")

    result = dict(job.result or {})
    # History is what the review screen shows back, so a reviewer can see what
    # they already asked for instead of repeating it. The singular key is what
    # the pipeline consumes and then clears, so feedback is applied exactly
    # once however many times the job is re-run.
    result["reextract_history"] = [
        *(result.get("reextract_history") or []),
        {"feedback": body.feedback, "at": datetime.now(timezone.utc).isoformat()},
    ]
    result["reextract_feedback"] = body.feedback
    job.result = result

    job.kind = "reextract"
    job.status = "pending"
    job.progress = None
    job.error_message = None
    job.locked_at = None
    job.locked_by = None
    db.commit()
    return job_out(job)


@router.post("/jobs/{job_id}/reject", response_model=JobOut)
def reject_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobOut:
    job = _get_job_or_404(db, job_id)
    job.status = "rejected"
    db.commit()
    return job_out(job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobOut:
    job = _get_job_or_404(db, job_id)
    if job.status != "failed":
        raise HTTPException(400, "only failed jobs can be retried")
    job.status = "pending"
    job.error_message = None
    job.locked_at = None
    job.locked_by = None
    db.commit()
    return job_out(job)
