"""Background worker (plan section 4).

A separate long-running process, started N times for parallelism (decision
#9). Each iteration claims one `pending` job via `SELECT ... FOR UPDATE SKIP
LOCKED` - the standard Postgres pattern for a DB-backed queue with multiple
competing consumers, no separate broker.

Run with: `python -m app.worker`
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from openai import RateLimitError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job
from app.pipeline.extract import run_backfill_pipeline, run_pipeline, run_reextract_pipeline
from app.pipeline.ratelimit import RateLimitTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("worker")

settings = get_settings()
WORKER_ID = settings.worker_id or f"{os.uname().nodename}-{os.getpid()}"

# A rate-limited job is requeued rather than failed (see `process_job`), but
# kept deliberately low, because a requeue is not free: the pipeline restarts
# from partitioning, so every LLM call the run had already made is spent again.
# Against a per-minute request quota that is self-defeating - each retry
# consumes the same quota it is waiting for. Three is enough to ride out a
# transient limit without turning one document into a request sink. Attempts
# live in the existing `progress` JSONB, so this needs no migration.
MAX_RATE_LIMIT_ATTEMPTS = 3

# Wait before a rate-limited job becomes claimable again. Without it the worker
# re-claims the job within its 1s poll and re-sends the same calls immediately,
# against a quota window that has had no time to roll over.
RATE_LIMIT_REQUEUE_DELAY_SECONDS = 30.0

CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'extracting', locked_at = now(), locked_by = :worker_id
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'pending'
          -- A job requeued after a rate limit carries a not-before stamp (see
          -- RATE_LIMIT_REQUEUE_DELAY_SECONDS). Skipping it until then lets the
          -- request window roll over, and lets other documents through
          -- meanwhile, instead of the worker re-claiming it on the next 1s poll.
          AND (
            progress->>'retry_not_before' IS NULL
            OR (progress->>'retry_not_before')::timestamptz <= now()
          )
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def claim_job(session: Session) -> uuid.UUID | None:
    row = session.execute(CLAIM_SQL, {"worker_id": WORKER_ID}).first()
    session.commit()
    return row[0] if row else None


def process_job(session: Session, job_id: uuid.UUID) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return
    try:
        if job.kind == "backfill":
            run_backfill_pipeline(session, job)
        elif job.kind == "reextract":
            run_reextract_pipeline(session, job)
        else:
            run_pipeline(session, job)
        # The attempt counter below counts *consecutive* rate-limited runs, so
        # a run that got all the way through clears it.
        if job.progress and {"rate_limit_attempts", "retry_not_before"} & job.progress.keys():
            progress = dict(job.progress)
            progress.pop("rate_limit_attempts", None)
            progress.pop("retry_not_before", None)
            job.progress = progress
        session.commit()
        logger.info("job %s -> %s", job_id, job.status)
    except Exception as exc:
        session.rollback()
        job = session.get(Job, job_id)
        if job is None:
            # The batch was deleted from a queue while this job was
            # extracting (DELETE /batches/{id}). The commit above then
            # touched zero rows, which arrives here as a StaleDataError.
            # There is nothing left to mark failed and nothing went wrong
            # that anyone needs to see - the run was cancelled on purpose.
            logger.info("job %s was deleted mid-extraction; dropping the run", job_id)
            return
        if isinstance(exc, (RateLimitError, RateLimitTimeout)):
            _handle_rate_limited(session, job, exc)
            return
        job.status = "failed"
        job.error_message = str(exc)
        session.commit()
        logger.exception("job %s failed", job_id)


def _handle_rate_limited(session: Session, job: Job, exc: RateLimitError) -> None:
    """Requeue a job the provider rate-limited, with a message worth reading.

    The pacer in app/pipeline/ratelimit.py is meant to stop this happening at
    all, so arriving here means something outside this process is spending the
    same per-key request quota. That is transient, so the job goes back to
    `pending` rather than `failed` - it had already paid for OCR and
    partitioning, and asking the user to click Retry for a limit that clears
    in seconds is busywork.

    The raw provider text ("Error code: 429 - {'error': {'message': ...") is
    dropped on the way: it used to be rendered verbatim in the queue row.

    The reason goes in `progress.step` as well as `error_message`, because
    `QueueRow` only renders `error_message` for a *failed* job - a requeued one
    would otherwise sit at `pending` with a stale step and look stuck.
    """
    progress = dict(job.progress or {})
    attempts = int(progress.get("rate_limit_attempts", 0)) + 1
    progress["rate_limit_attempts"] = attempts

    if attempts >= MAX_RATE_LIMIT_ATTEMPTS:
        job.status = "failed"
        job.error_message = (
            f"The language model provider's rate limit was hit on {attempts} consecutive attempts. "
            "Check the account's requests-per-minute quota against LLM_REQUESTS_PER_MINUTE, "
            "or that only one worker is running."
        )
        logger.error("job %s failed after %d rate-limited attempts: %s", job.id, attempts, exc)
    else:
        job.status = "pending"
        job.locked_at = None
        job.locked_by = None
        job.error_message = (
            f"Waiting on the language model provider's rate limit - retrying (attempt {attempts})."
        )
        progress["step"] = "waiting on rate limit"
        progress["retry_not_before"] = (
            datetime.now(timezone.utc) + timedelta(seconds=RATE_LIMIT_REQUEUE_DELAY_SECONDS)
        ).isoformat()
        logger.warning("job %s rate limited (attempt %d); requeued", job.id, attempts)

    job.progress = progress
    session.commit()


def run_forever() -> None:
    logger.info("worker %s starting, poll interval %.1fs", WORKER_ID, settings.worker_poll_interval_seconds)
    while True:
        session = SessionLocal()
        try:
            job_id = claim_job(session)
        except Exception:
            logger.exception("claim failed")
            session.rollback()
            job_id = None
        if job_id is None:
            session.close()
            time.sleep(settings.worker_poll_interval_seconds)
            continue
        try:
            process_job(session, job_id)
        except Exception:
            # `process_job` handles its own pipeline failures; anything
            # escaping it is a failure of that handling (the job row
            # vanishing under it, the database going away mid-rollback).
            # One bad job must not take the whole worker down - it would
            # stop every *other* document extracting too, and the only
            # symptom would be a queue that quietly stops moving.
            logger.exception("worker loop error on job %s; continuing", job_id)
            session.rollback()
        finally:
            session.close()


if __name__ == "__main__":
    run_forever()
