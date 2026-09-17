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

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job
from app.pipeline.extract import run_backfill_pipeline, run_pipeline, run_reextract_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("worker")

settings = get_settings()
WORKER_ID = settings.worker_id or f"{os.uname().nodename}-{os.getpid()}"

CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'extracting', locked_at = now(), locked_by = :worker_id
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'pending'
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
        job.status = "failed"
        job.error_message = str(exc)
        session.commit()
        logger.exception("job %s failed", job_id)


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
