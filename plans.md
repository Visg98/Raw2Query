# System design decisions

Decisions agreed on for the "messy documents → structured, queryable data" system.
Each entry: decision, the reasoning behind it, and what it implies for implementation.

## 1. Schema re-extraction on breaking edits: versioned schema, one logical store, backfill vs. forward-only

**Decision:** When a schema edit is *breaking* (field removed, retyped, or its meaning
changed), ask the user to choose between:
- **Background backfill** — re-extract all existing documents under the new schema
  version as an async job (not synchronous — surface a cost/time estimate before running).
- **Forward-only** — old documents keep their data under the old schema version; only
  new documents are extracted with the new version.

*Additive* edits (a new optional field) do **not** need to ask — they auto-apply going
forward, and existing records simply show `NULL`/absent for the new field until (if ever)
backfilled.

**What this replaces:** the earlier idea of "No → keep old records, new records go into
a separate table for the edited schema." That leads to schema-version table sprawl
(v1 table, v2 table, v3 table...) and breaks cross-version querying — a question like
"total spend across all invoices" would silently need to know to UNION every version's
table.

**How to implement it instead:**
- Schema definitions are versioned and immutable once published: editing a schema
  creates a new `(schema_id, version)`, it never mutates a version in place.
- Every extracted record stores which `(schema_id, version)` produced it.
- Records live in **one logical store per schema lineage**, not one physical table per
  version (this follows from the JSONB-per-record + generated SQL views design discussed
  for the storage layer). The generated view unions across versions and reconciles
  columns where types are compatible, so "all invoices regardless of schema version" stays
  a single, queryable thing without extra work at query time.
- The yes/no prompt on a breaking edit only decides *whether a backfill job runs*, not
  which table a record lands in.

## 2. Deduplication: logical/business dedup via schema-declared identity fields

**Decision:** Adopted as described:

> Two different scans of the same invoice won't hash the same, but will extract to the
> same `invoice_number` + `vendor` + `total`. Let each schema optionally declare
> "identity fields" (a natural key) — on extraction, check for an existing record with
> matching identity fields before inserting; if found, flag as a duplicate and let the
> user decide (skip / keep both / replace).

This is the **post-extraction** dedup tier — it catches duplicates that exact file-hash
comparison (the pre-extraction tier, for byte-identical re-uploads) misses, because two
scans/exports of the same real-world document differ at the byte level but extract to the
same business identity.

**Implementation implications:**
- Each schema can optionally define a set of `identity_fields` (e.g. `[invoice_number,
  vendor]` for an invoice schema) — a natural key, not enforced for every schema.
- After extraction, before insert, query for an existing record in the same schema
  lineage with matching identity-field values.
- On a match, don't silently overwrite or silently skip — surface it to the user with
  three explicit choices: **skip** (discard the new extraction), **keep both** (insert
  as a separate record, e.g. legitimately re-billed), or **replace** (overwrite the old
  record with the new extraction).
- Runs independently of, and in addition to, the exact-file-hash dedup done at upload
  time (which avoids re-running OCR/LLM extraction at all on a byte-identical re-upload).

## 3. Prototype scope: no multi-tenancy or authentication

**Decision:** No `tenant_id`, no auth, no user model for this prototype. Single shared
pool of documents, schemas, and topics.

**Why:** Adding tenant isolation (RLS, per-tenant vector namespaces, injected filters on
every generated query) is real complexity that isn't needed to prove out the core
pipeline. Revisit explicitly if/when this moves beyond a prototype — the topic model
below is designed so a `tenant_id` could be added to `documents`/`topics` later without
restructuring anything.

## 4. Topics: many-to-many, not single-topic-per-document

**Decision:** A document can carry zero, one, or many topics. Rejected the alternative
of one-topic-per-document — real documents don't fit a strict folder model (e.g. an
invoice reasonably belongs to both "Vendor X" and "Q1 2024 Invoices").

## 5. Topic selection UX

**Decision:** One reusable multi-select "chip" input component — type to search existing
topics, select any number, or choose "+ Create '<text>'" if nothing matches — used
identically on the document upload screen and the query page. No separate UI/interaction
model needed for the two contexts.

## 6. Topic filtering at query time: optional narrowing, not mandatory

**Decision:** Selecting topics on the query page narrows the search; selecting none
means search across everything. Never force a topic choice before a question can be
asked — that would break cross-topic questions and add friction for casual queries.

## 7. Topic storage model

```
topics
  id, name (UNIQUE, case-insensitive), description, created_at

document_topics        -- the many-to-many join, at the DOCUMENT level
  document_id FK, topic_id FK
  PRIMARY KEY (document_id, topic_id)
```

Extracted structured records and RAG chunks do **not** get their own topic-tag rows —
they inherit topics through their existing `document_id` foreign key (already needed for
provenance). Exception: chunk metadata gets a **denormalized copy** of the parent
document's current `topic_ids`, re-synced whenever the document's topics change, so the
vector store can filter fast on its own metadata instead of requiring a relational join
at query time. `document_topics` stays the source of truth; the copy on chunks is a
fast-path cache.

## 8. Duplicate topic prevention

**Decision:** `UNIQUE` constraint on `LOWER(topics.name)`, and "create new topic" is
implemented as get-or-create (case-insensitive match returns the existing id instead of
erroring). No fuzzy-matching or topic-merge tooling for the prototype — acceptable risk
at this scale; revisit only if it becomes a real annoyance in practice.

## 9. Batch upload: independent per-document pipeline runs

**Decision:** A batch of N uploaded files is N independent extraction pipeline runs,
parallelizable, not one shared job. Each document gets its own schema match (or ad hoc
inference) and its own topic suggestions — a mixed batch (invoices + policy docs
together) is expected to produce different schemas/topics per row. Any schema/topic
picked at the batch upload screen acts only as a **default hint** applied to every file,
still overridable per file in review.

## 10. Topic auto-suggestion

**Decision:** Reuse the extraction LLM call (or a lightweight follow-up using the same
context) to suggest existing topics that clearly apply, passing the existing topic list
(name + description) as context. On cold start (no topics exist yet, or none fit), the
same step proposes a new topic name/description from the document's content instead of
returning nothing. Suggestions are surfaced as pre-filled, removable chips on the review
screen — never silently auto-applied.

## 11. Schema auto-matching

**Decision:** Mirrors #10 for schema: when no schema was picked at upload, the same
per-document step checks the document against existing defined schemas and matches one
if it clearly fits, before falling back to ad hoc schema inference.

## 12. Review/confirm flow and persisted job status

**Decision:** SSE pushes live extraction progress to the screen, but job status
(`pending` → `extracting` → `awaiting_review` → `confirmed`/`rejected`/`failed`) is
persisted server-side as the source of truth — not held only in the SSE stream — so a
dropped connection or page refresh doesn't lose a completed extraction; the client can
reload and pick up from `awaiting_review`. Nothing is written to the real schema tables,
topic joins, or vector index until the user explicitly confirms.

The review screen has **three** exits, not two. Alongside confirm and reject, a reviewer
can re-extract: describe what the extraction got wrong and the document is read again with
that feedback in the prompt, returning to `awaiting_review` on the same job. The extracted
values are shown as one editable table — the list of objects that will become the records —
with each document's chunks listed read-only beneath it.

## 13. Batch review UX

**Decision:** A table/list of all documents processed in a batch, each showing its own
status. A bulk "confirm all clean" action for documents that extracted without any
flagged issues; individual, per-document review is required only where a document or
specific field is flagged low-confidence.

## 14. Ad hoc schema handling

**Decision:** When no schema was picked and none matched (#11), the inferred structure
is shown as an **editable** proposed schema on the review screen (field names/types),
not a flat JSON dump — with an option to save it as a reusable named schema rather than a
disposable one-off shape, so future similar documents can match against it.

## 15. RAG chunking is unconditional, independent of schema extraction

**Decision:** Every document is always chunked and embedded for RAG, regardless of
whether a schema was picked, matched, or applies at all. Schema-extracted field values
are never excluded or deduplicated out of the chunk stream — deliberate redundancy: the
structured field serves exact-value/SQL-style queries, the chunk serves contextual/
natural-language questions the schema didn't anticipate.

The chunks are shown on the review screen, one card each, so that redundancy is visible to
the reviewer rather than implicit — they can see the passage a field was read out of, and
what will still be answerable where extraction found nothing. The review response omits each
chunk's embedding vector; confirm reads those server-side.

## 16. Default "Uncategorized" topic

**Decision:** Documents (and their derived records/chunks) that end up with no topic —
none selected, none suggested — are tagged with an implicit "Uncategorized" topic, so
nothing in the system is topic-less and every downstream filter/query can assume at
least one topic is present.

## 17. Topic metadata shape: flat, no hierarchy

**Decision:** Topics are a flat set for the prototype — no topic/subtopic nesting.

## 18. Query-page routing

**Decision:** One routing layer per incoming question decides NL-to-SQL vs. RAG.
Priority order for scoping: if the user has explicitly selected topic(s) on the query
page, scope directly to those (per #6). If the user hasn't selected any, the router
should attempt to detect which topic(s)/clusters the question is likely about and narrow
to those before retrieving, rather than searching unscoped across all data.
**Open/not yet designed:** the concrete mechanism for that auto-detection step (e.g.
classify the question against topic names/descriptions vs. embedding-similarity against
topic clusters) — needs its own follow-up design pass.

## 19. Pending/staging area for unconfirmed extraction results

**Decision:** Reinforces #12 — results of an extraction (and any clustering/topic
suggestions) sit in a pending/staging store (a queue or a pending-status table) after
processing completes, not in the final production tables. Only an explicit user
confirm/save moves them into the real schema tables, `document_topics` rows, and vector
index.

## 20. Query-router topic auto-detection mechanism

**Decision:** When the user hasn't pre-selected any topics (#6/#18), the router detects
relevant topic(s) for a question via an LLM call given the topic list (name +
description), asking which topic(s) most closely represent the query — the same
"list + LLM classifies" pattern already used for ingestion-time topic suggestion (#10)
and schema auto-matching (#11). Reusing one pattern across all three call sites rather
than introducing embedding-similarity-against-topic-centroids as a second mechanism;
that stays a possible future optimization for scale, not needed now.

## 21. Batch schema-consistency risk: deferred

**Decision:** Explicitly accepting the risk from the "parallel batch processing" edge
case (similar documents in one batch independently landing on different suggested
schemas) for now. No bulk-apply-schema-to-selected review action, no pre-clustering step
before per-document schema matching. Human review before confirm (#12) remains the only
safety net. Revisit only if this causes real friction in practice.

## 22. RAG storage stack (free, no added infra cost)

**Decision:**
- **Vector store:** `pgvector` extension on the same Postgres database already used for
  the structured tables (#7's JSONB + generated-views design) — no separate vector DB
  service to run. Topic-scoping a search (#7) becomes a plain SQL
  `WHERE topic_id = ANY(...)` against the denormalized `topic_ids` column, rather than a
  vector-DB-specific metadata filter.
- **Embedding model:** `BAAI/bge-small-en-v1.5` — open-weight, runs locally via
  `sentence-transformers`, no per-call API cost, strong retrieval accuracy for its size.
  Fallback to the lighter/faster `all-MiniLM-L6-v2` if compute is constrained.
- **Chunking:** `unstructured`'s `chunk_by_title` (section/heading-aware splitting) —
  already decided earlier in the design discussion.
- **Hybrid retrieval:** combine `pgvector` cosine-similarity search with Postgres's
  native full-text search (`tsvector`/`ts_rank`) for exact-term queries (invoice
  numbers, names, IDs) that embedding similarity alone handles poorly. Both live in the
  same database — no extra service.

## 23. Field columns are persisted keys, not names derived at view-build time

**Decision:** Every schema field carries an opaque `column` key (`f_` + 8 hex), minted
server-side and stored in `schema_versions.fields[i]`. The generated `view_<schema>`
projects each field under that key, and every other consumer *reads* the key rather than
re-deriving it.

**What this replaces:** `safe_ident(field.name)`, computed inside the view generator and
persisted nowhere. Because the mapping was not recorded, each consumer re-derived it and two
of them got it wrong — the NL-to-SQL catalog and the records grid both advertised the raw
field name, so a field called "Invoice Total" was offered to the model as `Invoice Total`
while the column was `invoice_total`. Generated SQL died on `UndefinedColumn` and fell back
to RAG; the records grid rendered the column blank. The derived name also collided three
ways: with the view's own bookkeeping columns (a field named `id` broke `CREATE VIEW`
outright), after Postgres truncated identifiers at 63 characters, and through a
disambiguation suffix that depended on dict iteration order.

**Implementation implications:**
- `extracted_records.data` stays keyed by field *name* — no row data is migrated. Only the
  view's projected alias becomes opaque, which confines the change to the
  view/catalog/label boundary plus dedup.
- A key is *preserved across a rename*, so one column holds data from before and after.
  Renaming is therefore no longer a breaking edit (cf. #1). The client signals a rename by
  echoing the field's existing `column`; a client that drops it gets a new key and the
  pre-existing split column, which is logged.
- Because the field name can change while the key cannot, the view must read each row under
  the name *that row's own version* used: a `CASE` on `er.schema_version_id`, not a
  `COALESCE` over historical names.
- `identity_fields` stores keys for the same reason. Names are still the API's currency in
  both directions.
- Column keys are never shown to a user: the catalog gives the model `key (type) - "label":
  description`, result rows are relabelled at the boundary, and the records grid labels by
  name.
