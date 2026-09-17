# raw2query

Messy documents in, structured & queryable data out.

- [`decisions.md`](decisions.md) - how the problem was interpreted, the calls made along the
  way, what was reversed, and what was deliberately cut. **Start here.**
- [`plans.md`](plans.md) - the up-front design, as 23 numbered architecture decisions the
  code refers back to.
- [`DEPLOY.md`](DEPLOY.md) - running this on a public VM: why the backend can't go anywhere
  serverless, and the Compose/Caddy/Netlify setup that hosts it.

- **API**: FastAPI (uploads, review/confirm, schema/topic CRUD, NL query, SSE progress)
- **Worker**: a poll-loop process running the extraction pipeline (`unstructured` + `sentence-transformers` + Groq)
- **DB**: Postgres + `pgvector` - one database for structured records, topics, jobs, and vectors; the job queue is a Postgres table (`SELECT ... FOR UPDATE SKIP LOCKED`), no Redis/Celery
- **Frontend**: React + JSX (Vite) in [`frontend/`](frontend/)

## Quick start

```bash
./run.sh
```

Brings up Postgres (docker), runs migrations, starts the API, 2 worker processes, and the
frontend dev server, all in one shot - then `Ctrl+C` stops everything it started. Fill in
`GROQ_API_KEY` in `.env` first for uploads/queries that call the LLM (everything else - the
UI, schemas, topics - works without it). See `./run.sh --help` for options (`--workers N`,
`--skip-install`, `--no-db`, `--keep-db`). The manual steps below are what it automates, useful
if you want to run pieces individually or debug a step.

## Layout

```
app/
  api.py              FastAPI app (routers only, no heavy work)
  worker.py           poll loop: python -m app.worker
  config.py           Settings, loaded from .env
  models.py           SQLAlchemy ORM (plan section 2)
  schemas_pydantic.py API request/response models
  serializers.py       ORM -> Pydantic assembly
  storage.py           local file storage (./data/uploads)
  preview.py           turns an original upload into something a browser can
                        render - the types with no native viewer (.docx,
                        .xlsx, .pptx, .csv, .eml) are converted here
  routers/            documents, jobs, batches, schemas, topics, query, chats
  pipeline/
    llm.py             the two shared LLM helpers: llm_classify / llm_extract
    embeddings.py       sentence-transformers wrapper
    extract.py          the 8-step per-document pipeline + backfill entry point
    confirm.py          commits jobs.result into the real tables (decision #19)
    views.py            generates/regenerates view_<schema_name>; reconciles
                        every view on startup (also `python -m app.pipeline.views`)
    topics_util.py       get-or-create topic + "Uncategorized" seed
  schema_fields.py    column-key assignment + historical-name analysis, shared
                        by view generation and dedup (decision #23)
  query/
    routing.py          topic auto-detect + sql-vs-rag routing (decisions #18/#20)
    nl_to_sql.py         SQL generation, the column catalog, execution
    sql_guard.py         the parser rail + scope injection (pure sqlglot)
    rag.py               hybrid retrieval + answer composition
migrations/            Alembic; 0001_initial creates everything, incl. the
                        read-only query-runner role and the seeded topic.
                        0005 backfills field column keys, 0006 adds
                        extracted_records.row_index - run
                        `python -m app.pipeline.views` after upgrading either
docker/                Dockerfile.api (no OCR deps) / Dockerfile.worker (has them)
```

## Setup

1. Copy `.env.example` to `.env` (already done) and fill in `GROQ_API_KEY`.
   Everything else has a working default that matches `docker-compose.yml`.

2. Start Postgres:

   ```bash
   docker compose up -d db
   ```

3. Install the package (creates `app` as an importable package plus every
   dependency, including `unstructured[all-docs]` and `sentence-transformers`
   which are large - the worker's OCR path also needs `poppler-utils` and
   `tesseract-ocr` as system packages, see `docker/Dockerfile.worker`):

   ```bash
   pip install -e .
   ```

4. Run migrations (creates all tables, the `vector`/`citext` extensions, the
   restricted `raw2query_query_runner` role used by NL-to-SQL, and the seeded
   "Uncategorized" topic):

   ```bash
   alembic upgrade head
   ```

5. Run the API and at least one worker, in separate terminals:

   ```bash
   uvicorn app.api:app --reload
   python -m app.worker
   ```

   For real parallelism (decision #9), run multiple workers, or
   `docker compose up --scale worker=4`.

Full stack via Docker instead of steps 3-5: `docker compose up --build`.

## Trying it out

```bash
# create a schema
curl -s localhost:8000/schemas -X POST -H 'content-type: application/json' -d '{
  "name": "invoice",
  "fields": [
    {"name": "invoice_number", "type": "string", "required": true},
    {"name": "vendor", "type": "string", "required": true},
    {"name": "total", "type": "number", "required": true}
  ],
  "identity_fields": ["invoice_number", "vendor"]
}'

# upload a document
curl -s localhost:8000/documents -F files=@/path/to/invoice.pdf

# poll status, then review, then confirm
curl -s localhost:8000/jobs/<job_id>
curl -s localhost:8000/jobs/<job_id>/review
curl -s localhost:8000/jobs/<job_id>/confirm -X POST -H 'content-type: application/json' -d '{}'

# ask a question
curl -s localhost:8000/query -X POST -H 'content-type: application/json' \
  -d '{"question": "what is the total across all invoices from Acme?"}'
```

Interactive API docs: `localhost:8000/docs`.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

| File | Needs | What it covers |
| --- | --- | --- |
| `tests/test_preview_kinds.py` | nothing | Every extension `unstructured` can partition resolves to a renderable preview kind, and to the *right* one. The list is read out of the library, so gaining a new supported type fails the suite instead of shipping a file the review screen would download. |
| `tests/test_document_preview_api.py` | Postgres | Uploads a real file of each type and checks `/documents/{id}/preview` over HTTP, plus the response headers on `/file`: no `Content-Disposition` for a preview, `attachment` only for `?download=1`, and never `application/octet-stream`. |
| `tests/test_chats_api.py` | Postgres | Chat sessions: created by the first *answered* question and not before, turn ordering, listing/rename/delete. The LLM is stubbed. |
| `tests/test_preview_ui_e2e.py` | Postgres, API, frontend, Playwright | Drives a real browser over both surfaces that show a document - the review pane and the Ask page's "View file" - and asserts no download fires for any supported type. Skipped unless `R2Q_E2E=1`. |

Everything that needs a service it can't find skips with a reason rather than
failing, so a bare `pytest` on a fresh checkout runs the type-resolution suite
and stays green.

The browser tests need both servers up and `R2Q_E2E=1`:

```bash
./run.sh &                      # or start the API and frontend by hand
R2Q_E2E=1 pytest tests/test_preview_ui_e2e.py

# non-default ports
R2Q_E2E=1 R2Q_API_URL=http://localhost:8099 R2Q_APP_URL=http://localhost:5199 \
  pytest tests/test_preview_ui_e2e.py
```

They launch the full Chromium build (`channel="chromium"`) rather than
Playwright's default headless shell, which ships no PDF viewer and would
"download" every PDF regardless of what the app does. Install it once with
`playwright install chromium`.

A few file types need system packages to preview at all: OCR for scanned
images (`tesseract-ocr`) and `poppler-utils` for some PDFs - the same ones
`docker/Dockerfile.worker` installs for extraction. Without them the preview
endpoint returns an explained "can't preview this, download it" response
rather than an error, and the tests for those types skip.

## Known gap (deferred on purpose)

No edit/delete on a confirmed record, document, topic, or schema - append-only
for the prototype (plan section 6).
