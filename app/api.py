"""FastAPI entrypoint. Handles uploads, review/confirm, schema/topic CRUD,
the query endpoint, and SSE progress streaming - never does heavy extraction
itself (that's the worker's job; see app/worker.py).

Run with: `uvicorn app.api:app --reload`
"""

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import RateLimitError

from app.db import SessionLocal
from app.pipeline.topics_util import get_or_create_uncategorized
from app.pipeline.views import reconcile_schema_views
from app.routers import batches, chats, documents, jobs, query, schemas, topics

logger = logging.getLogger(__name__)

app = FastAPI(title="raw2query", description="Messy documents -> structured, queryable data")


class UnhandledErrorMiddleware:
    """Turns an unhandled exception into a real 500 response, inside the CORS
    layer.

    Starlette's own `ServerErrorMiddleware` sits *above* everything added
    here, so an exception that reaches it re-raises and the 500 uvicorn
    finally writes never passes back through `CORSMiddleware`. That response
    therefore carries no `Access-Control-Allow-Origin`, and the browser
    reports a CORS failure instead of the server error that actually
    happened - which is why a failing `POST /jobs/{id}/confirm` looked like a
    CORS misconfiguration on the review screen rather than the foreign key
    violation it was. Every hand-raised `HTTPException` was always fine; only
    the unhandled ones lost their headers, which is what made it look
    intermittent.

    Pure ASGI rather than `BaseHTTPMiddleware`: `/jobs/{id}/events` is a
    long-lived SSE stream, and `BaseHTTPMiddleware` wraps responses in an
    extra task and queue that interferes with streaming ones.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "unhandled error serving %s %s", scope.get("method"), scope.get("path")
            )
            if started:
                # The status line and headers are already on the wire (an
                # exception part-way through a streamed body). There is no
                # response left to replace, so let it propagate and be logged
                # as the transport error it now is.
                raise
            # Deliberately opaque: the client gets a status it can act on and
            # the traceback stays in the server log, not in a toast.
            await JSONResponse({"detail": "internal server error"}, status_code=500)(
                scope, receive, send
            )


# Added before CORSMiddleware on purpose. Starlette inserts each new
# middleware at the *front* of the stack, so the last one added is the
# outermost - and CORS has to be outermost for its headers to reach the
# error responses the middleware above produces.
app.add_middleware(UnhandledErrorMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RateLimitError)
async def provider_rate_limited(request: Request, exc: RateLimitError) -> JSONResponse:
    """The query path running out of the provider's request quota.

    Extraction requeues a rate-limited job (see app/worker.py), but a question
    on the Ask page has someone waiting on it, so it gets an answer it can act
    on instead. Without this the exception reached `UnhandledErrorMiddleware`
    above and came back as an opaque 500 "internal server error", which told
    the user nothing and looked like a bug in the app.

    429 rather than 503 so the status itself carries the meaning, and the raw
    provider payload stays in the log.
    """
    logger.warning("provider rate limit serving %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        {"detail": "The language model provider is rate limiting us. Please try that again in a moment."},
        status_code=429,
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
