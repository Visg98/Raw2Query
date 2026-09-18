# raw2query

**Messy documents in, structured and queryable data out.**

Organisations sit on piles of documents that contain the data they need but not in
a form anything can use: invoices as scanned PDFs, policies as Word files,
statements as spreadsheets, correspondence as email exports. The information is
there, and the only way to get at it is for a person to open each file and retype
what they see into a spreadsheet or a system. That work is slow, it does not scale,
and it quietly introduces errors nobody catches.

raw2query removes that step. Drop a pile of documents in, and it:

- **Reads anything.** PDFs (including scanned ones, via OCR), Word, Excel,
  PowerPoint, CSV, email, images, plain text.
- **Works out what each document is.** It matches a document against the schemas
  you already have, and where nothing fits it proposes a new shape from the
  document itself, rather than making you define every format up front.
- **Pulls out the fields** into real database columns, with a **confidence score
  per field**.
- **Asks for help only where it is unsure.** Anything above the confidence
  threshold can be confirmed in bulk; anything below is flagged for a human to
  check. You review the extraction side by side with the original document, fix
  what is wrong, and confirm.
- **Makes it queryable in plain English.** Ask "what is the total due across all
  invoices from Aster?" and it writes and runs SQL against the extracted tables.
  Ask "what is the policy on sharing client information?" and it retrieves the
  relevant passages and answers from them. It decides which of the two approaches
  fits the question.

**What that changes:** the expensive part of document work stops being data entry
and becomes review — and only of the fields the system flagged. The data lands
somewhere you can query, so questions that used to mean opening twenty files
become one sentence.

## Live demo

**https://raw2query.netlify.app/upload**

Sample documents to try it with, and two guided walkthroughs, are in
[`Try_These/`](Try_These/).

## Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| **API** | FastAPI (Python 3.11) | Async HTTP, automatic OpenAPI docs at `/docs`, Pydantic validation on every boundary. |
| **Worker** | A plain Python poll loop | Separate process so heavy OCR never blocks a web request. |
| **Queue** | Postgres `SELECT … FOR UPDATE SKIP LOCKED` | The job queue is a table. No Redis, no Celery — one less service to run, and claims are transactional. |
| **Database** | Postgres 16 + `pgvector` | One store for structured records, documents, jobs, topics *and* embeddings. Vector search sits next to the relational data instead of in a separate system. |
| **ORM / migrations** | SQLAlchemy 2 + Alembic | |
| **Document parsing** | `unstructured[all-docs]` | One parser covering every supported format, plus OCR (`tesseract`, `poppler`) for scanned pages. |
| **Embeddings** | `sentence-transformers`, `BAAI/bge-small-en-v1.5` (384-dim) | Runs locally on CPU, so indexing costs nothing per document. |
| **LLM** | Groq, via the OpenAI-compatible SDK | Used for schema matching, field extraction, topic suggestion, query routing, SQL generation and answer composition. Swappable by changing a base URL. |
| **SQL safety** | `sqlglot` + a restricted DB role | Generated SQL is parsed and rejected unless it is a single read-only `SELECT`, then executed as a role that can only read the generated views. |
| **Frontend** | React 19 + Vite 8, React Router, TanStack Query | |
| **Deployment** | Docker Compose behind Caddy; frontend on Netlify | Caddy terminates TLS with automatic Let's Encrypt certificates. |

## Architecture

```
                 ┌──────────────────────────────────────────┐
  Browser ──────▶│  React SPA (Vite)                        │
                 │  upload · review · schemas · topics · ask │
                 └───────────────────┬──────────────────────┘
                                     │ HTTPS/JSON + SSE
                 ┌───────────────────▼──────────────────────┐
                 │  FastAPI                                 │
                 │  uploads, review/confirm, schema & topic │
                 │  CRUD, NL query, progress streaming      │
                 └───────┬──────────────────────┬───────────┘
                         │ writes job row       │ reads/writes
                         ▼                      ▼
        ┌────────────────────────┐   ┌──────────────────────────────┐
        │  Postgres + pgvector   │◀──│  Worker (1..N processes)     │
        │                        │   │  claims jobs FOR UPDATE      │
        │  documents  jobs       │   │        SKIP LOCKED           │
        │  schemas    versions   │   └──────────────┬───────────────┘
        │  extracted_records     │                  │
        │  chunks (vector 384)   │                  ▼
        │  topics     batches    │        8-step pipeline:
        │  view_<schema> (views) │        partition → chunk → embed
        └────────────────────────┘        → match schema → extract
                                          → score confidence
                                          → suggest topics → stage
```

**The flow.** An upload writes one `documents` row and one `pending` job per file,
then returns immediately — the browser follows progress over Server-Sent Events. A
worker claims a job, runs the pipeline, and leaves the result *staged* on the job
rather than in the live tables. Confirming a job is what commits it: the record is
written to `extracted_records`, and a `view_<schema_name>` view is generated so the
schema's fields appear as ordinary SQL columns.

**Asking a question** routes one of two ways. A question about values that live in
columns becomes SQL: the model sees only a catalogue of column names, its output is
parsed by `sqlglot` and rejected unless it is a single read-only `SELECT`, and it
runs as a restricted role with `SELECT` on the generated views and nothing else —
so a bad generation fails rather than reaching a base table. A question about
wording or meaning goes to retrieval instead: hybrid vector plus full-text search
over `chunks`, with the answer composed from the passages retrieved.

**Why the worker is separate.** OCR on a scanned PDF is CPU-bound and takes
seconds to minutes. Run it in the request and uploads time out; run it in a
separate process and the API stays responsive while extraction proceeds. Because
the queue is a Postgres table using `SKIP LOCKED`, scaling out is just starting
more worker processes — they cannot claim the same job.

## Setup

### Prerequisites

- Docker and Docker Compose
- Python 3.11+
- Node.js 20.19+ or 22.12+ (for the frontend)
- A Groq API key — free at [console.groq.com](https://console.groq.com)

### For a human

```bash
git clone https://github.com/Visg98/Raw2Query.git
cd Raw2Query
cp .env.example .env
```

Put your key in `.env`:

```
GROQ_API_KEY=gsk_...
```

Then start everything with one command:

```bash
./run.sh
```

That brings up Postgres in Docker, runs the migrations, starts the API, two worker
processes and the Vite dev server. `Ctrl+C` stops everything it started.

- Frontend: **http://localhost:5173**
- API docs: **http://localhost:8000/docs**

Useful flags: `./run.sh --help`, `--workers N`, `--skip-install`, `--no-db`,
`--keep-db`.

Everything except uploads and questions works without a key — you can browse the
UI, define schemas and create topics before adding one.

### For an agent

Non-interactive, with a verification step after each stage. Fail on the first
non-zero exit rather than continuing.

```bash
# 1. Configure. GROQ_API_KEY must be set in the environment already.
cp -n .env.example .env
sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=${GROQ_API_KEY}|" .env
grep -q '^GROQ_API_KEY=gsk_' .env || { echo "GROQ_API_KEY not set in .env"; exit 1; }

# 2. Database. Wait for the healthcheck - do not assume it is ready.
docker compose up -d db
for i in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' raw2query-db-1)" = healthy ] && break
  sleep 3
done

# 3. Dependencies. Large: unstructured[all-docs] plus torch.
python -m venv .venv && . .venv/bin/activate
pip install -q -e .

# 4. Migrations. Must run BEFORE the API starts: the startup hook queries the
#    topics table with no error handling, so an unmigrated database crash-loops
#    the API.
alembic upgrade head
docker compose exec -T db psql -U raw2query -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
# expect 12

# 5. Processes.
uvicorn app.api:app --host 0.0.0.0 --port 8000 &
python -m app.worker &

# 6. Verify. /health does NOT check the database, so check a real endpoint too.
curl -sf http://localhost:8000/health
curl -sf http://localhost:8000/topics   # expect the seeded "Uncategorized" topic

# 7. Frontend.
cd frontend && npm ci && npm run dev
```

Notes that matter when automating this:

- `VITE_API_BASE_URL` and `VITE_DEMO_TOKEN` are inlined by Vite **at build time**.
  Changing either requires a rebuild, not a restart.
- The API and every worker must share the `UPLOAD_DIR` filesystem — the worker
  reads back the exact file the API wrote.
- `QUERY_RUNNER_DB_PASSWORD` must be set **before the first `alembic upgrade head`**.
  Migration `0001` bakes it into the restricted role it creates, and it has to
  match the password inside `READONLY_DATABASE_URL`.
- The embedding model (~130 MB) downloads on first use. Persist the HuggingFace
  cache or it re-downloads on every cold start.

### Deploying

Running this on a public server (Docker Compose, Caddy, TLS, the firewall
specifics) is covered in [`DEPLOY.md`](DEPLOY.md).
