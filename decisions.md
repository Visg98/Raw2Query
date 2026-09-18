# Decisions

A log of the real calls made while building raw2query, and the reasoning behind them.

[`plans.md`](plans.md) is the companion document: the 23 up-front design decisions, written
as specification. This file is the judgment layer over it — what I chose, what I rejected,
what I got wrong and changed, and what I deliberately left out. Where the two disagree,
this file is newer.

---

## Project interpretation

I interpreted this problem as building a system that lets users bring in documents, turn the
information they care about into a consistent structure, and query that information across
their collection.

The statement leaves the document types, domain, and expected structure open. An invoice
extractor or a resume parser would both fit. I chose to build a reusable platform because
the underlying need is shared: users have information spread across documents with different
layouts and wording, and they need a reliable way to organize and use it.

One question that shaped my approach was: **who decides what "structured" means?** For
invoices, a user might care about the supplier, amount, and due date. For contracts, they
might care about the parties, renewal date, and notice period. Even users working with the
same document type may need different fields. This led me to support reusable schemas that
users can define, along with a proposed structure inferred from the document when no suitable
schema exists. Users can review and edit that proposal before accepting it.

A practical use case is a small operations team working with supplier invoices, agreements,
and policy documents. Today, someone might open these files individually and copy information
into a spreadsheet whenever a question comes up. With this system, they could extract
consistent records, organize documents by topics such as supplier or project, and reuse that
information for future questions.

I also interpreted "searched and queried" as covering two needs. Users may want exact answers
from extracted fields, such as *"What is the total invoiced amount by supplier?"* They may
also want information from the surrounding text, such as *"What does this agreement say about
cancellation?"* I therefore chose to preserve both the structured records and the document
content, supporting database queries alongside document retrieval.

The usefulness depends on whether users can trust the resulting data. Review, correction, and
duplicate handling are therefore part of the core workflow. My goal is to make the extracted
information consistent, verifiable, and useful beyond the initial upload.

This broader interpretation adds challenges around schema consistency and query routing. For
the prototype, I chose to explore those challenges while keeping the application single-user
and making human confirmation part of the workflow.

## Decisions

### Interpretation and scope

**A reusable platform, not a single-document-type extractor.**
*Alternatives:* build an invoice extractor (much stronger accuracy, demoable in a day); build
a generic "PDF to JSON" endpoint with no schema concept at all.
*Reasoning:* a domain-specific extractor answers the prompt but dodges its interesting
question — who defines the structure. The generic endpoint dodges "queryable." The platform
forces both.
*Tradeoff accepted:* extraction accuracy on any *specific* document type is worse than a
purpose-built extractor's. I decided the user-defined-schema story was worth that.

**User-defined schemas, with inference as the fallback — not inference as the product.**
*Alternatives:* infer a schema for every document and skip user-defined schemas entirely.
*Reasoning:* pure inference guarantees inconsistency. Two invoices with different layouts
infer different field names, and now `SUM(total)` can't find half the rows. The schema is
what makes a *collection* queryable rather than a pile of individually-structured documents.
Inference exists for the cold start, and its output is an *editable proposal* that can be
promoted to a reusable named schema, which is how the cold start resolves itself.

**Single-user, no auth, no tenancy.**
*Reasoning:* tenant isolation means RLS, per-tenant vector scoping, and an injected filter on
every generated SQL query — that last one interacts badly with NL-to-SQL, which is already
the riskiest component. It is real work that proves nothing about the core pipeline.
*Kept cheap to add later:* `documents` and `topics` are shaped so a `tenant_id` slots in
without restructuring.

### Extraction and trust

**Nothing reaches the real tables until a human confirms.**
*Alternatives:* write on extract and let users edit afterwards.
*Reasoning:* this is the decision the whole trust story rests on. The pipeline writes its
output to `jobs.result` and stops at `awaiting_review`; `extracted_records`, `document_topics`
and `chunks` are only written by [`confirm.py`](app/pipeline/confirm.py), in one transaction.
An LLM extraction is a *proposal*. If bad data can enter the queryable set unattended, every
answer the system gives afterwards is suspect, and the user has no way to know which ones.
*Tradeoff accepted:* real friction — a 50-file batch needs a human before it's queryable.
Mitigated by "confirm all clean," which bulk-confirms only jobs where no field came back
below the confidence threshold, so review effort concentrates where the model was unsure.

**Job status is persisted server-side; SSE is a view onto it, not the source of truth.**
*Alternatives:* hold progress in the SSE stream only.
*Reasoning:* a dropped connection or a page refresh would otherwise discard a completed
extraction — the most expensive thing in the system. The client reloads and picks up from
`awaiting_review`.

**Identity-field dedup, because file-hash dedup solves the wrong problem.**
*Reasoning:* two scans of the same invoice never hash the same, but they extract to the same
`invoice_number` + `vendor`. Schemas optionally declare `identity_fields` as a natural key,
and a match is surfaced on review with skip / keep both / replace. Keeping both cross-links
via `is_duplicate_of`, so "we legitimately got billed twice" stays representable — a dedup
design that can't express that is worse than none.

**Confidence is per-field, not per-document.**
*Reasoning:* a document where 11 of 12 fields are certain and the total is a guess is not "low
confidence," it is one field that needs eyes. Per-field scores drive both the review
highlighting and `is_job_clean`. Mechanically this is `data_confidence`: one score map per
extracted object, positionally parallel to `extracted_data`, so a score is addressed per
*cell* of the review table. There is deliberately one confidence source rather than a document
dict plus a row list — `is_job_clean` reading only one of two was the bug that reported a
40-line invoice as clean on the strength of its header.

**Long documents reuse the RAG chunker rather than a second splitter.**
*Reasoning:* over `MAX_EXTRACT_CHARS`, extraction falls back to running per-chunk with
`chunk_by_title` — the same section-aware splitter used for embedding — then merges
field-by-field. Where two chunks disagree on a field, it keeps the later value but **records
the lower of the two confidences**, so a genuine conflict surfaces as something to look at
rather than a silent last-write-wins.
*Do not flatten before this merge.* It runs on the unflattened document/rows shape and the two
halves merge by opposite rules — document fields reconcile, rows concatenate. A flat list
cannot tell "this chunk's copy of the vendor" from "a row", so concatenating would add one
junk object per prose chunk and reconciling would collapse a 40-line invoice into its last
line. `flatten_extraction` runs once, afterwards.

**One document can produce many rows, and the header is denormalized onto every one of
them.**
*Alternatives:* a normalized parent/child pair (header view + line-item view joined on
`document_id`); one record per document holding a JSONB array of rows, unnested in the view
body.
*Reasoning:* a schema field now declares a `scope` — `document` for "Invoice Number",
`row` for "Line Amount" — and a document with row-scoped fields confirms to N
`extracted_records` rows, ordered by `row_index`. `extract.flatten_extraction` copies the
document-level values onto each row, so `view_<schema>` stays **one flat relation**. That is
the load-bearing part: `build_catalog` advertises no relationships between views, so generated
SQL has no join key available, and the normalized design would have made
`SUM(line_amount) GROUP BY vendor` — the exact question this feature exists to answer —
depend on the model inventing a join. Trading a solved problem for an unsolved one is the
wrong trade; repeating a vendor name across twelve rows costs nothing. The JSONB-array variant
keeps the record count at one but puts a second way to read a field into the view generator,
and `_find_dedup_match` has to agree with that generator about which JSON key holds a value or
the two disagree about what a record *is*.
*Tradeoff accepted:* a document-level value summed across rows multiply-counts; the SQL prompt
says so explicitly for multi-row views only, which is a request, not the guarantee that the
`sql_guard` rails are. Identity fields are constrained to document scope (a 400 otherwise),
since identity means "same document" and dedup runs once per document. And a schema with no
row-scoped field keeps its exact prior behaviour — same response schema, same prompt, one
record — so nothing that never opts in can regress.

**The staged extraction payload is a flat list of objects.**
*Alternatives:* keep `job.result` split into an `extracted_data` dict plus a sibling
`extracted_rows` list, and flatten at confirm time; flatten only in the review API's
serializer, leaving storage split.
*Reasoning:* the review screen is a table and the confirm target is N flat records, so the
split shape served neither end — it existed only to give one editing property (below). Having
two shapes for one concept meant every consumer had to know both: `is_job_clean` read two
differently-shaped confidence sources, the PATCH merge had two branches with different
invalidation rules, and the review screen rendered two unrelated widgets over what is one
table. `extract.flatten_extraction` now produces `extracted_data: list[dict]` — one object per
value-set, each carrying every declared field — and `materialize_record_data` reduces to a
coercion pass. Flattening in the serializer only would have left the same two shapes in the
database with a third view on top.
*Tradeoff accepted:* the payload is now redundant, so "edit a document-level value once and it
applies to every row" is no longer structural. The review table restores it by filling a
`scope != "row"` column down on edit, which puts the property where the reviewer's intent
is — but a client PATCHing the API directly can now store objects that disagree on a
document-level field, which the split shape made impossible. `_identity_values` logs such a
disagreement rather than rejecting it; enforcing it would mean teaching the flat payload about
scopes again. Two guards keep the row count honest: `flatten_extraction` returns `[]` only for
an empty field list (never for "no rows found"), and `materialize_record_data` turns `[]` back
into `[{}]`, because a document that silently stops producing a record disappears from its view
and resurfaces only as a wrong `SUM`.

**A bad extraction is re-run with feedback, in place, on the same job.**
*Alternatives:* enqueue a second job for the document; make the reviewer hand-correct the
table; reject and re-upload.
*Reasoning:* a misread column is one sentence to describe and expensive to fix cell by cell,
so `POST /jobs/{id}/reextract` takes the reviewer's correction, appends it to the extraction
prompt (after the structural rules, never before — a correction phrased as an instruction is
exactly the text that would talk the model out of "do not repeat the document-level values
inside the rows") and re-runs step 4. A second job would list the same document twice in the
review queue, strand this job's draft, and stale the review URL. Only the table is rebuilt:
chunks are deterministic from the same file, so re-chunking would spend the embedding cost —
the one cost that scales with corpus size — to produce identical rows, and the topics, dedup
match and schema choice are either unchanged or were made by the reviewer since. Fields are
resolved from `job.result`, **not** `document.schema_version` as backfill does, because the
reviewer may have re-pointed the job at another schema via the review screen and that choice
lives only in the staged payload until confirm.
*Tradeoff accepted:* re-extracting discards the reviewer's own cell edits, including a saved
draft. Interleaving a fresh read with edits made against the previous one would produce a
table matching neither, so the UI confirms first rather than merging. The feedback is
untrusted text in a prompt, left unsanitized beyond delimiting: its blast radius is one
extraction that the same person then reviews field by field before it can be confirmed.

**Backfill replaces a document's record set rather than updating it in place.**
*Reasoning:* the in-place version located the row with `.first()`, which is structurally blind
to row count — re-extracting a 12-line invoice as 9 lines would update one row and leave 11
stale ones behind. `regenerate_schema_view` casts every row to the *newest* field type, so a
single stale row whose value only parsed under the old type nulls out a cell for every reader,
which is exactly the breakage in-place updating existed to prevent. Delete-then-insert keeps
that guarantee at any row count.
*Tradeoff accepted:* record ids don't survive a backfill. Nothing references them except
`is_duplicate_of`, which is detached first — losing a "these two are duplicates" note about a
row being deleted is better than a `ForeignKeyViolation` that fails the confirm.

### Query

**A routing layer, not one query mechanism.**
*Alternatives:* RAG only (simplest, and what most implementations ship); SQL only.
*Reasoning:* RAG-only cannot reliably answer *"total invoiced amount by supplier"* — summing
across documents is exactly what retrieval-and-summarize is worst at, and it will confidently
return a wrong number. SQL-only cannot answer *"what does this agreement say about
cancellation?"* The two question types are both native to this product, so the router is not
optional. Structured questions get SQL over the typed views; contextual ones get hybrid
retrieval.

**Two independent safety rails on generated SQL.**
*Reasoning:* I assumed from the start that the model would eventually emit something
destructive, so neither rail trusts the prompt.
- *Parser rail:* `sqlglot` parses the SQL and rejects anything that isn't exactly one pure
  `SELECT`. Write and DDL node types are checked **anywhere in the tree**, not just at the
  top level, because `WITH x AS (DELETE ... RETURNING *) SELECT * FROM x` parses as a
  top-level `Select`. Table references are checked against the allowed view list.
- *Database rail:* execution goes through a separate Postgres role holding `SELECT` on the
  `view_*` views and nothing else. If the parser check is ever wrong, the database still
  refuses.

*Redundant on purpose, same reasoning as the cast layers.* A rejected query is never shown or
executed — it logs and hands the question to RAG.

**Chunk and embed every document unconditionally.**
*Reasoning:* even fully-structured documents get chunked, and schema-extracted values are
*not* excluded from the chunk stream. Deliberate redundancy: the structured field answers the
question the schema anticipated, the chunk answers the one it didn't.

**Hybrid retrieval, vector plus full-text.**
*Reasoning:* embeddings are poor at exact tokens — invoice numbers, reference codes, proper
nouns — which is a large share of what someone actually searches a document collection for.
`pgvector` cosine similarity is blended 0.6/0.4 with Postgres `ts_rank` in one SQL statement.
*Tradeoff accepted:* the weights are a judgment call, not a tuned result.

**Source cards name the document and page.**
*Reasoning:* a citation labelled with a bare UUID tells the reader nothing about what they are
being shown, which defeats the purpose of citing. `filename` and `page_number` are joined into
the retrieval query itself.

**The scope predicate is injected, not requested.**
*Reasoning:* topic scoping used to be a sentence in the SQL-generation prompt carrying a literal
`document_id IN (...)` list. The model could omit it, and when it did the aggregate was computed
over the whole corpus while the answer stated the figure as fact and the page reported "searched
within 1 topic(s)" beside it — the only failure mode in the system where the UI actively
corroborates a wrong number. `sql_guard.inject_scope` now rewrites the scope into the parsed
statement. Same argument as the qualifier repair one section up: *prompt instructions are a
request; the normalizer is a guarantee.*

**A view reference becomes a scoped derived table, not an extra WHERE clause.**
*Reasoning:* appending `AND alias.topic_ids && …` to the containing `SELECT` is the obvious move
and it is wrong in the cases that matter. An unaliased reference cannot be qualified without
inventing an alias, which breaks any `view_invoice.total` the model already wrote; and a
reference inside a `JOIN ... ON` or a correlated subquery makes "which WHERE clause" ambiguous.
Rewriting the table node into `(SELECT * FROM view_x WHERE …) AS <original alias>` is
unambiguous everywhere, and an unaliased reference keeps its own name so qualified references
still resolve. *Consequence:* the rewrite is not idempotent — it must be a single pass, and
`normalize_sql` must never see its own output.

**Generated SQL is scope-free, so the retry ladder is free.**
*Reasoning:* once scope left the prompt, `generate_sql` no longer depends on it, so it hoists out
of the scoped→unscoped candidate loop. The relaxation retry is now the same statement under a
different scope instead of a second, unrelated LLM roll — one fewer call on that path, and a
deterministic retry.

**A field's column is persisted, never derived.**
*Reasoning:* the field-name→column mapping was computed inside the view generator and stored
nowhere, so every other consumer re-derived it and two of them derived it wrong: both the
NL-to-SQL catalog and the records grid advertised the raw field name while the column was
`safe_ident(name)`. A field called "Invoice Total" was offered to the model as a column that did
not exist, so the query failed and fell back to RAG — silently, because the fallback was
working as designed. Field names with spaces and capitals are the normal case, so this was
likely a large share of why structured questions underperformed.
*What I hadn't appreciated:* it was three collision bugs, not one. A field named `id` collided
with the view's own `er.id` and made `CREATE VIEW` fail outright. `safe_ident` never truncated
but Postgres truncates identifiers at 63 characters, so two long names sharing a prefix collided
*after* truncation — which the dedup loop structurally could not see, because it deduped the
untruncated strings. And the `_` disambiguation suffix depended on dict iteration order across
versions, so it wasn't reproducible outside the one loop that produced it. A 10-character
`f_[0-9a-f]{8}` key removes all three by construction rather than by care.
*Tradeoff accepted:* opaque columns strip the semantic signal a model uses to pick a column. Paid
for by giving the catalog the human label *and* the description (which was already stored and
previously discarded), and by keeping the routing call on human names — handing that one a list
of hex tokens would push every structured question to RAG, a worse failure than the one being
fixed.

**The column lookup is scoped by the row's own version.**
*Reasoning:* a stable key plus a changing name means the view has to know which JSON key holds a
given field's value for a given row. `COALESCE(data->>'new', data->>'old')` looks like the
answer and leaks: extraction writes every declared field, using an explicit JSON null when the
value is absent, and `->>` cannot distinguish a JSON null from a missing key — so COALESCE falls
through and surfaces a *different* field's value whenever a later version reuses a retired name.
Branching on `er.schema_version_id` reads a persisted fact instead of inferring one from the
payload shape. The obvious optimisation ("only one historical name, so read it directly") is
unsound in the mirror-image way, so the plain form is only used for a field whose name is
unique to it in both directions and declared by every version.

### Infrastructure

**One Postgres instance does four jobs: relational store, job queue, vector index, full-text
index.**
*Alternatives:* Redis + Celery for the queue; Pinecone/Qdrant/Chroma for vectors;
Elasticsearch for text.
*Reasoning:* the honest version is that a separate broker and a separate vector DB would each
buy me something I don't need at this scale, and cost me something I do need — a setup a
stranger can run in one shot, and one transaction boundary. The queue is a `jobs` table
claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, the standard pattern for multiple competing
consumers. Vectors are `pgvector` in the same database, which turns topic-scoped search into a
plain `WHERE topic_ids && ...` instead of a vector-DB metadata filter plus a relational join.
*Tradeoff accepted:* polling costs a little latency versus a push broker, and `pgvector` will
be outperformed by a dedicated store at millions of vectors. Neither binds here.

**One physical store per schema lineage, with a generated view — not a table per version.**
*Alternatives:* a new physical table per schema version (the original plan, rejected before
implementation).
*Reasoning:* table-per-version means *"total spend across all invoices"* has to know to
`UNION` every historical version's table. That is a query-time tax that grows every time
someone edits a schema. Instead, records are JSONB with a `(schema_id, version)` stamp, and
`view_<schema>` flattens the union of every version's field set — old records simply read
`NULL` for columns added later. Cross-version queryability becomes free rather than
conditional.
*Consequence I had to handle:* the view is `DROP`/`CREATE`, not `CREATE OR REPLACE`, because
retyping a field changes a column's output type and Postgres refuses that under `REPLACE`.
The read-only role's grant survives, since `ALTER DEFAULT PRIVILEGES` keys to the creating
role rather than the object's creation time.

**Breaking schema edits ask; additive edits don't.**
*Reasoning:* adding an optional field can't invalidate existing data, so prompting for it is
noise. Removing, retyping, or redefining a field can, so the user chooses: background backfill
(re-extract everything under the new version) or forward-only (old records keep their data,
new documents use the new version). The prompt decides *whether a job runs* — never which
table a record lands in.

**Local embeddings (`bge-small-en-v1.5`) over an embedding API.**
*Reasoning:* embedding runs on every chunk of every document, so it is the one LLM-adjacent
cost that scales with corpus size rather than with user activity. Local means no per-chunk
cost, no rate limit on ingest, and the app still works with no network. Retrieval quality at
that size is strong enough that the tradeoff isn't close.
*Cost accepted:* a heavy install, and the worker image carries OCR system packages the API
image doesn't need — which is why they are separate Dockerfiles.

**Two shared LLM helpers, not per-call-site prompting.**
*Reasoning:* six call sites needed an LLM. Five of them are the same shape — *here is a list,
which entries apply?* — used for schema matching, topic suggestion, and query-time topic
detection. Routing them all through `llm_classify`/`llm_extract` with strict JSON-schema
responses means the pattern is implemented once, and a prompt improvement lands everywhere.
*Alternative rejected:* embedding-similarity-against-topic-centroids for query routing. It
would have been a *second* mechanism for a problem one mechanism already solved. Held as a
scale optimization, not built.

---

## Decisions I reversed

These are the ones I'd want a teammate to read, because the first answer was wrong in a way
that wasn't visible from the design.

**File-hash dedup: removed after building it.**
The original design had two dedup tiers, exact-hash at upload and identity-field
post-extraction. Hash dedup shipped as a `UNIQUE` constraint, and it was wrong in a way the
design couldn't show: re-uploading the same bytes was *silently folded into the existing
document*. No new document, no new job — so nothing extracted, and nothing appeared in the
extraction or review queues. The user uploaded a file and the product did nothing, with no
explanation. [Migration 0002](migrations/versions/0002_allow_duplicate_uploads.py) drops the
constraint and keeps `file_hash` as a plain index. Every upload is now its own document with
its own extraction run. The deeper mistake was treating "these bytes are identical" as
equivalent to "the user didn't mean this" — the byte-level tier was answering a question
nobody asked, while the identity-field tier answers the real one.

**Dedup went from blocking a confirm to informing one.**
Following from the above: an identity match used to disqualify a job from bulk confirm and
refuse the save. Now duplicates are explicitly acceptable, the default is keep-both, and the
match is information on the review screen. Refusing to save data the user can see is correct
is a bad default.

**Graceful fallback hid three real bugs — and that's the lesson.**
The router falls back to RAG when the SQL branch can't answer. That is the right behaviour and
I'd keep it, but it is *silently* the right behaviour, and it turned three hard failures into
plausible-looking answers:

1. The model qualified every view with the schema's *logical* name — `invoice.view_invoice`.
   Nothing lives in a Postgres schema called `invoice`, so every generated query died on
   `UndefinedTable`. **Structured questions never once got a structured answer**, and the
   product looked like it was working.
2. Topic scoping was expressed as a subquery against `document_topics` — a table the
   read-only role deliberately has no grant on. Every topic-scoped query failed with
   `InsufficientPrivilege` and fell back. Fixed by resolving document IDs up front and passing
   them in, which keeps the role's "views only" boundary intact rather than widening it.
3. `compose_sql_answer` was a hardcoded `"Ran a SQL query and found N row(s)."` — the query
   page reported that something had happened without answering the question.

The fix for (1) is a repair pass in `normalize_sql` that strips any qualifier from an
otherwise-valid view name, rather than another attempt at prompting the model out of it.
Prompt instructions are a request; the normalizer is a guarantee.

The durable lesson: **a fallback path needs to be loud.** Every rejection and every fallback
now logs with the offending SQL, and the query response reports `routing_used` and the SQL
that actually ran, post-repair — so both a developer reading logs and a user reading the page
can tell which branch answered. Designing for graceful degradation without observability
means building a system that hides its own breakage, and I'd caught none of these without
going looking.

**Auto-detected topic scope now relaxes itself; explicit scope never does.**
Query-time topic detection narrows the search, which is right when it's right and silently
destructive when it's wrong: an auto-detected topic holding none of the matching chunks
answered *"there are no sources available"* while the documents sat in plain sight under
another topic. Both branches now retry unscoped when a scope produced nothing — **but only
when the scope was our own guess.** An explicit user selection is honoured even when it
matches nothing, because that is a deliberate narrowing and overriding it would be lying
about what was searched. The distinction is threaded through as an `auto_detected` flag rather
than inferred, since the two cases are indistinguishable by the time you have the topic IDs.

**Previews: `Content-Disposition: inline` does not do what I assumed.**
The review screen needs to *show* the original file. Pointing an `<iframe>` at the raw bytes
works for the few types a browser ships a viewer for — PDF, web images, text, HTML — and
turns into a **download** for every other type the pipeline accepts: `.docx`, `.xlsx`,
`.pptx`, `.csv`, `.eml`, a multi-page `.tiff` scan. The header expresses intent; it cannot
hand the browser a renderer it doesn't have. Those types are now converted server-side into a
structured element list the frontend renders as real DOM — using `unstructured.partition`,
the same parser the extraction pipeline uses, **so the reviewer sees exactly the text the
extraction saw** rather than a second, differently-lossy rendering. Reviewing against a
different view of the file than the model read is a subtle way to make review useless.

Two smaller things fell out of this. `documents.mime_type` holds the browser-supplied
`content_type`, which is routinely empty or `application/octet-stream` for drag-and-drop —
and an octet-stream PDF downloads even though the browser has a perfectly good PDF viewer.
Media types and renderer choice are both derived from the filename instead. And audio/video
are played, not transcribed: `unstructured` will happily transcribe them, but a transcript is
not a preview of a sound file.

---

## What I deliberately cut

| Cut | Why it was right for this build | What it would take |
| --- | --- | --- |
| Auth, users, multi-tenancy | Proves nothing about the pipeline; tenant filters interact badly with NL-to-SQL, the riskiest component | `tenant_id` on `documents`/`topics`, RLS, an injected predicate in `generate_sql` |
| Edit/delete on confirmed records, documents, topics, schemas | Append-only. Mutation means cascade rules across records, chunks, and vectors, and the review gate already catches errors before they land | Soft-delete plus chunk/vector reconciliation |
| Topic hierarchy | Flat topics answer the scoping question. Nesting adds tree UI and recursive scoping for no gain at this size | Parent FK, recursive CTE for scope resolution |
| Fuzzy topic matching and topic merge | Case-insensitive get-or-create prevents the common duplicate. Fuzzy matching risks silently merging two genuinely distinct topics | Similarity threshold, plus a merge tool with provenance rewriting |
| Batch schema consistency | Similar documents in one batch can independently land on different inferred schemas. Real risk, accepted knowingly — human review is the safety net | Pre-clustering before per-document matching, or bulk-apply-schema on review |
| LLM retry and backoff | A failed job is recorded with its error and is re-runnable. Retry logic is easy to add and hard to *tune* blind | Bounded exponential backoff, distinguishing rate-limit from malformed-response |
| Stuck-job recovery | `jobs.locked_at`/`locked_by` are written, but nothing sweeps them — a worker killed mid-job leaves the job `extracting` forever | A reaper returning jobs locked beyond a timeout to `pending`, with an attempt counter |
| Streaming answers | The query page shows a thinking state and returns a composed answer. Token streaming is polish, not capability | SSE on the query endpoint |

The last two are the ones I'd be least comfortable shipping to real users, and they are listed
here rather than buried because a 5-day prototype that pretends to be production-ready is
less trustworthy than one that says where the edges are.

---

## What I don't trust yet

Honest read on where this would break first.

**Test coverage is lopsided, and it's lopsided in the wrong direction.** The suite is
genuinely good where it exists — [`test_preview_kinds.py`](tests/test_preview_kinds.py) reads
the supported-extension list *out of `unstructured` itself*, so gaining a new supported file
type **fails the suite** instead of silently shipping a file the review screen would download;
the Playwright tests drive a real Chromium (not the headless shell, which ships no PDF viewer
and would "download" every PDF regardless of what the app does) across both surfaces that
show a document. But that is preview and chat coverage. The two components most likely to
cause real damage — value coercion and the NL-to-SQL safety rails — have **no direct tests**,
and coercion in particular is pure, table-driven, and the easiest thing in the codebase to
test well. That's the first gap I'd close.

**The routing decision is an LLM call with no ground truth.** `decide_routing` picks SQL or
RAG per question and nothing measures whether it picks correctly. The fallback means a wrong
"sql" choice degrades to RAG, but a wrong "rag" choice on an aggregate question produces a
confidently-worded, possibly-wrong number — the failure mode with no safety net. What this
needs is an eval set of questions with known-correct routes, not more prompt engineering.

**Extraction quality is unmeasured.** There is no labelled set, so "accuracy" is an
impression from manual testing. The confidence scores are the model's self-report, which is
not the same thing as calibration — a score of 0.9 does not mean nine out of ten are right,
and `is_job_clean` thresholds against it as though it did.

**Single-document context limits are handled; single-*field* ambiguity isn't.** The chunked
extraction path merges field-by-field and keeps the lower confidence on disagreement, which
flags a conflict but doesn't resolve it — the reviewer sees a value and a low score, not the
two candidate values and where each came from. Showing both would be the better answer.

**Row extraction over the chunked path is the weakest link in the multi-row feature.** The
storage side is pinned by tests and behaves at any row count, but *finding* the rows is not
measured. Document-level fields and rows merge across chunks by deliberately opposite rules —
document fields reconcile, rows concatenate — and concatenation is only correct if the chunk
boundaries don't cut a table. `chunk_by_title` makes no such promise, so a long table split
mid-way can yield a duplicated or a partial row, and an all-null row is dropped on the
assumption it means "no line items here" rather than "the model lost the table." Both are
judgement calls standing in for a labelled set. A one-shot extraction under
`MAX_EXTRACT_CHARS` — the common case — doesn't have this problem at all, which is why it
wasn't worth solving speculatively; splitting on element boundaries so a `Table` element is
never cut is the obvious next move if real documents show it.

**Scale is untested past prototype volumes.** `pgvector` without a tuned index, hybrid-search
weights picked by judgement, a poll-based queue: all reasonable at hundreds of documents, all
worth re-measuring at hundreds of thousands.
