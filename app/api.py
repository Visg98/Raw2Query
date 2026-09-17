"""FastAPI entrypoint. Handles uploads, review/confirm, schema/topic CRUD,
the query endpoint, and SSE progress streaming - never does heavy extraction
itself (that's the worker's job; see app/worker.py).

Run with: `uvicorn app.api:app --reload`
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import SessionLocal
from app.pipeline.topics_util import get_or_create_uncategorized
from app.pipeline.views import reconcile_schema_views
from app.routers import batches, chats, documents, jobs, query, schemas, topics

logger = logging.getLogger(__name__)

app = FastAPI(title="raw2query", description="Messy documents -> structured, queryable data")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(jobs.router)
app.include_router(batches.router)
app.include_router(schemas.router)
app.include_router(topics.router)
app.include_router(query.router)
app.include_router(chats.router)


@app.on_event("startup")
def seed_uncategorized_topic() -> None:
    """Decision #16: the "Uncategorized" topic always exists. The initial
    migration seeds it too; this is just a safety net."""
    db = SessionLocal()
    try:
        get_or_create_uncategorized(db)
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def reconcile_views() -> None:
    """Brings every `view_<schema>` up to the shape `app/pipeline/views.py`
    currently generates.

    The view body is derived from application code, so a migration can't own
    it - migration 0005 backfills the column keys and leaves the views stale
    until this runs. Idempotent and advisory-locked, so it is a no-op on every
    boot after the first and never fights a second process.

    Failure is logged, never fatal: one unmigrated lineage must not stop the
    API from starting, and the degradation it leaves (structured questions
    falling back to RAG) is exactly the pre-existing behaviour.
    """
    db = SessionLocal()
    try:
        rebuilt = reconcile_schema_views(db)
        db.commit()
        if rebuilt:
            logger.info("rebuilt %s schema view(s): %s", len(rebuilt), ", ".join(rebuilt))
    except Exception:
        db.rollback()
        logger.exception("could not reconcile schema views; run `python -m app.pipeline.views`")
    finally:
        db.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
