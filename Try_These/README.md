# Try these

Two guided walkthroughs using the sample documents in this folder. Between them
they exercise both halves of the application: **structured extraction into SQL
tables**, and **retrieval over document text**.

Live app: **https://raw2query.netlify.app/upload**

| Folder | What's in it |
| --- | --- |
| `sample_invoices/` | 10 tax invoices from a fictional lighting supplier, as PDFs, plus `invoice.schema.json`. Three of them are deliberate byte-identical copies, to show duplicate handling. |
| `sample_policies/` | 5 compliance policies from a fictional financial firm, as `.docx`. |

Everything below has been run end to end against the live deployment — the
queries and the behaviour described are what actually happens, including the
places where a field comes back blank.

---

## Workflow 1 — Invoices, with a schema

**The point:** when you already know what shape the data has, you define it once
and every matching document lands in the same table, queryable with SQL.

### 1. Create the schema

Schemas → **New schema**, name it `invoice`, and add the fields.

> **Watch out:** `invoice.schema.json` is a complete *API payload* —
> `{"name": ..., "fields": [...]}`. If you paste the whole file into a field-list
> box you get a schema with two fields literally called `name` and `fields`. Paste
> only the contents of the `fields` array, or create it via the API:
>
> ```bash
> curl -X POST http://localhost:8000/schemas \
>   -H 'content-type: application/json' \
>   -d @Try_These/sample_invoices/invoice.schema.json
> ```

The schema has **35 fields in two scopes**, which is the interesting part:

- **29 document-scoped** — one value per invoice: `invoice_number`, `seller_name`,
  `total_amount`, `currency`, GST fields, and so on.
- **6 row-scoped** — one value *per line item*: `item_description`, `item_hsn`,
  `item_quantity`, `item_unit`, `item_rate`, `item_amount`.

A row-scoped field is what turns a repeating table inside a document into real
rows. An invoice with 3 line items produces 3 database rows, each carrying its own
item values plus a copy of the document-level ones. That is what makes "list the
products sold" answerable.

`identity_fields` is set to `["invoice_number", "seller_name"]`, so re-uploading
the same invoice is recognised rather than silently doubling your totals.

### 2. Upload

Upload → drag in **4 or 5** of the invoice PDFs → pick **invoice** as the schema →
upload.

Include all three `aster_workspace_solutions` files (`.pdf`, ` - Copy.pdf`,
` - Copy - Copy.pdf`) if you want to see duplicate detection flag them in review.

Each file becomes its own job. Progress streams live; a text-based PDF takes a few
seconds.

### 3. Review — this is the part worth looking at

Review queue → open a document. You get the original PDF beside the extracted
values, with a **confidence score per field**.

Two things you will see, and both are the point of this screen:

- **Some fields come back blank.** On these invoices `balance_due` and
  `payment_status` are usually empty, because the documents don't state them
  explicitly. The model left them out rather than inventing them.
- **Some fields are flagged low-confidence.** Anything under the 0.75 threshold is
  marked, and its document is *excluded* from "confirm all clean".

**Do this:** fill in `balance_due` (use the invoice's total) and set
`payment_status` to `unpaid`. Correct anything else that looks wrong. This is what
makes the "total due" query in step 4 return a real number instead of null — worth
doing precisely so you can see the difference.

Then **Confirm**. Because the schema already matched, you don't need to name a
schema here.

> If you use **Confirm all clean** on the batch instead, documents with a
> low-confidence field are **skipped** — the response says so, and they stay in the
> queue for individual review. A batch where nothing seems to happen usually means
> every document in it needs a look.

### 4. Ask questions

Ask page. Both of these route to **SQL** — the question is about values in columns,
so the system writes a query against the generated `view_invoice`:

**"List the products sold with quantities"**

Returns each distinct item with its total quantity, e.g.

```
20W Adjustable Track Light - Black (TRK-20W-BLK)  — 36
1 metre track rail - Black (RAIL-1M-BLK)          — 18
L-joint track connector (CONN-L-TRK)              — 12
```

This only works because the line items are row-scoped. As a single blob field
there would be nothing to `SUM` or `GROUP BY`.

**"What is the total due amount across all invoices?"**

If you filled in `balance_due` during review, you get the sum. If you skipped that
step, the honest answer is `null` — the column is empty, and the system says so
rather than guessing. Try **"What is the total invoice amount across all
invoices?"** to query `total_amount`, which extraction always populates.

Other things worth asking: *"Which customer has the highest invoice total?"*,
*"Show invoices due before 20 September 2026"*, *"What is the total CGST charged?"*

You can see the generated SQL with each answer. It is parsed before it runs and
rejected unless it is a single read-only `SELECT`, then executed by a role that can
only read the generated views.

---

## Workflow 2 — Policies, with no schema

**The point:** not every document has a shape worth tabulating. A policy is prose —
what you want is to *ask it questions*, not put it in columns. Same pipeline, no
schema, and the answers come from the document text.

### 1. Upload without a schema

Upload → drag in **2 or 3** `.docx` files from `sample_policies/` → leave the
schema picker **empty** → upload.

These take longer than the invoices — around **60–90 seconds each**. They are
42 KB Word files, and the whole document is parsed, split and embedded.

### 2. Look at what it produced

Open the document in the review queue.

- **Chunks.** One policy produces around **28 chunks**. Each is a passage kept
  whole with its heading context, and each has a 384-dimension embedding. These are
  what make the document searchable.
- **No schema match.** `matched_schema_id` is empty — the `invoice` schema quite
  correctly doesn't fit a policy document.
- **A proposed shape anyway.** The model infers about **11 fields** it *could*
  extract — `document_title`, `policy_code`, `policy_owner`, `effective_date`,
  `review_cycle`, `classification`, `version` and so on. You are not obliged to use
  them.
- **A proposed topic**, e.g. `Confidential Information Policy`, inferred from the
  content.

### 3. Edit, and add a topic

**Do this:**

1. Correct one of the inferred values — `policy_owner` or `effective_date` are good
   candidates.
2. **Add a new topic.** Type something like `Compliance` in the topic field and
   create it. Proposed topics are *suggestions*: they are not created unless you
   accept them, so if you skip this the document stays `Uncategorized`. Tagging
   matters because topics scope retrieval — you can later ask a question against
   compliance documents only.
3. **Confirm without giving a schema name.**

> **The one thing to understand here:** confirming without naming a schema keeps
> the chunks and embeddings — so the document is fully searchable — but does **not**
> create a table, and the inferred field values are discarded. That is deliberate:
> an ad hoc shape is only promoted to a reusable schema if you name it. If you
> *did* want these policies in a table, type a name like `Policy Document` before
> confirming, and the schema, its view and its rows are created.

### 4. Ask questions

These route to **retrieval**, not SQL — they are about wording and meaning, so the
system finds the relevant passages (hybrid vector plus full-text search) and
answers from them, citing sources.

**"What is the policy on sharing confidential client information?"**

Returns a real synthesis across ~8 retrieved passages — that sharing is permitted
only for a legitimate authorised business purpose, to a verified recipient with a
need to know, subject to contractual, privacy and regulatory obligations, with
classification and approved transfer channels required beforehand.

**"What are the approved channels for client communication?"**

Answered from the Electronic Communications policy, if you uploaded it.

More to try: *"What must an employee do if they receive material non-public
information?"*, *"How long are communications retained?"*, *"When must a conflict of
interest be escalated?"*

Each answer shows its sources, so you can open the passage it came from and check
it. Ask something the documents don't cover and it says so rather than inventing an
answer.

---

## What the two workflows show together

| | Invoices | Policies |
| --- | --- | --- |
| Schema | Defined up front | None |
| Output | Rows in `view_invoice` | Chunks + embeddings |
| Query route | SQL | Retrieval |
| Good for | Totals, filters, grouping, comparison | "What does it say about…", "Am I allowed to…" |

The routing is automatic — the same Ask box handles both, and picks based on the
question. Upload both sets, then ask *"What is the total invoice amount?"* followed
by *"What is the policy on sharing client information?"* and watch it switch.
