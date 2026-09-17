# raw2query frontend

React + JSX (Vite, no TypeScript) UI implementing the plan at
`/home/amaddi/.claude/plans/let-us-plan-the-distributed-puddle.md`.

## Run it

```bash
npm install
cp .env.example .env   # then set VITE_API_BASE_URL if the API isn't on localhost:8000
npm run dev
```

Requires the FastAPI backend (`uvicorn app.api:app --reload` from the repo root, with
Postgres + `GROQ_API_KEY` configured per the root `.env.example`) running and reachable at
`VITE_API_BASE_URL`.

## Structure

- `src/api/` — one module per backend router, thin fetch wrappers (`src/api/client.js`).
- `src/hooks/` — `useJobEvents` (SSE + poll fallback), `useBatchPolling`, `useChatHistory`
  (localStorage-backed, since the API has no chat persistence endpoint).
- `src/components/` — cross-page reusable pieces: `TopicChipInput`, `SchemaFieldEditor`,
  `FileDropzone`, and `common/` (StatusBadge, ConfidenceFlag, DataTable, SqlPreview, JsonViewer,
  Toast, Accordion).
- `src/pages/` — one folder per screen area (`upload`, `review`, `schemas`, `topics`, `query`),
  each with a `components/` subfolder for page-local pieces.
- `src/layout/` — `AppShell` + `NavBar`.

## Known gap

`AdHocSqlPanel` (on a schema's records page) is a real, built UI with no live backend behind
it yet — `GET /schemas/{id}/records` only supports field=value filters, there's no raw-SQL
execution endpoint. See the plan doc §8; `api/schemas.js#runAdHocSql` is a clearly-labeled stub
until something like `POST /schemas/{id}/query` exists.

## Verification done so far

- `npm run build` and `npx oxlint src` both pass clean (build succeeds, lint has only
  fast-refresh/style warnings, no errors).
- `npm run dev` serves and transforms every page without a runtime import error.
- **Not yet done**: a real end-to-end run against the live backend (upload → SSE progress →
  review → confirm → query) — the backend needs `GROQ_API_KEY` set and
  `docker compose up db` running, neither of which is configured in this environment. Once
  those are in place, follow §10 of the plan doc to verify end-to-end.
