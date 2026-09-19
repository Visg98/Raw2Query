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

---

## The hard part: the UI does not own its own state

Most CRUD frontends are easy because the user causes every state change. Click save, state
changes, render. This app is not shaped like that. **The thing the user is waiting for happens
in a separate worker process, minutes after they navigated away from the page that started
it.** A document moves `pending → extracting → awaiting_review → confirmed` on its own
schedule, across three different screens, and the user can refresh, navigate, open a second
tab, or delete the whole batch at any point in the middle.

So the interesting frontend problem here is not layout or component composition. It is
**telling the truth about work you don't control** — and the specific trap is that the honest
answer and the convenient answer look identical right up to the moment they diverge.

### The three-way ambiguity

Both queues are *filtered* lists: the extraction queue shows `pending`/`extracting`/`failed`,
the review queue shows `awaiting_review`. That filtering is what makes "did my batch finish?"
answerable at all — a batch whose work is done stops appearing. So the completion signal is
**"present, then absent."**

That signal is also produced by two other things that are not completion:

| Batch is absent because… | What the user should see |
| --- | --- |
| Every document finished | Completion banner, then move them forward |
| They deleted it | Nothing. The delete already reported itself |
| The query hasn't loaded yet | Nothing. An empty list and a drained list are byte-identical |

Reading "absent" as "finished" is the obvious implementation, and it gets two of three cases
wrong. Deleting a bad upload ended in **confetti and a redirect to review**, congratulating
the user for finishing work they had just thrown away. And on every page load, the
one-frame-empty list fired the celebration before any data arrived.

[`useBatchCompletion`](frontend/src/hooks/useBatchCompletion.js) exists to disambiguate those
three, and every part of it is load-bearing:

- **`ready`** — the caller passes `!isLoading`. A loading queue and a drained queue are
  indistinguishable from inside the hook, so it refuses to act until the distinction exists.
- **`markBatchDiscarded`** — the delete mutation is the *only* caller that knows a
  disappearance was deliberate, so it is the one that has to say so. It writes the batch id to
  a shared `sessionStorage` key **before** invalidating the queries that drop the rows,
  because the invalidation is what triggers the hook.
- **One discard key for both queues** — a delete in either is a delete everywhere, and both
  have to stay quiet. The *watched-batch* markers are per-queue (`storageKey`), because the
  two queues watch the same batches independently and must not clear each other's marker.
- **Watch the newest batch only** — that's the one the user just acted on. Celebrating an
  older batch that happened to drain first would navigate someone away mid-task.
- **Key on the watched batch being gone, not on the queue being empty** — a failed document
  *keeps* its row, so an older batch with a failure sits in the queue forever and would mask
  the newest batch draining. The upload the user is actually waiting on would finish in
  silence.
- **A failed extraction cannot celebrate, structurally.** Because a `failed` row keeps the
  batch in the list, "present, then absent" is *false* for the case that must never fire. That
  is why it's the right test rather than a convenient one — the safety property falls out of
  the signal instead of needing a separate guard.

### Why `sessionStorage`, not a ref

This is the part I'd want to be asked about. A `useRef` only remembers what *this component
instance* has seen — and **the review queue is remounted by the very action it needs to react
to.** Confirming the last document in a batch navigates from `/review/:jobId` back to
`/review`, so the page mounts fresh into an already-empty queue with nothing to compare
against. The signal is destroyed by the event that produces it.

The watched id therefore has to outlive the mount. `sessionStorage` rather than `localStorage`
because it should be per-tab and per-session: a batch that finished last week is not news on a
fresh visit. Every read and write is wrapped — blocked storage in private mode degrades the
hook to "only notices a batch finishing while you were looking at it," which is the common
case anyway, rather than throwing. The discard list is capped at 20 entries, since only
recent ids can still be in a marker slot.

*Tradeoff accepted:* the mechanism is inference over a filtered list, not a server event. A
`batch_completed` webhook or a status field on the batch would be unambiguous and is the right
answer at scale. I chose inference because it needed no new backend surface and no second
source of truth about what "done" means — the queue's own contents already define it — but it
means the hook is coupled to the queues staying filtered, which is a real constraint a future
change could break silently.

---

## Frontend decisions

### Architecture and the data layer

**Server state and client state are strictly separated. `react-query` owns everything
server-shaped; `useState`/`useReducer` owns the rest. No global store.**
*Alternatives:* Redux or Zustand for a single app-wide store; lifting server data into context.
*Reasoning:* almost every piece of state in this app is a cached copy of something the server
owns, and the hard parts are staleness, refetch timing and invalidation — exactly what
react-query is for and what a hand-rolled store makes you reimplement badly. What's left that
is genuinely client-owned is small and local: a review draft, a dropdown's open state, the
topic filter on the Ask page. None of it needs to be global, so a store would add a layer that
only forwards. The cache *is* the shared state, and the query key is the contract.
*Consequence:* invalidation is the thing to get right, and it's explicit at every mutation
site. `DeleteBatchButton` invalidates both `["documents"]` **and** `["batch", batchId]` —
missing the second left the group's rows rendering live status from a poll for a batch that no
longer existed.

**Six runtime dependencies, no component library, no CSS framework.**
`react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`, `lottie-react`,
`react-syntax-highlighter`.
*Reasoning:* the components this app needs most — an editable extraction table with per-cell
confidence, a chip input with inline create, a preview pane that dispatches on server-decided
renderer — are not in any component library. I'd have imported MUI for the generic 20% and
fought it for the specific 80%. Styling is CSS Modules over design tokens in
[`tokens.css`](frontend/src/styles/tokens.css), which gives scoped class names with no runtime
and no build plugin.
*Tradeoff accepted:* I wrote my own `Modal`, `Toast`, `Accordion`, `DataTable` and
`EmptyState`. They're small, but they're also less accessible than a mature library's — the
modal doesn't trap focus, which is a real gap I'd close before shipping this to users.

**One API module per resource, and the fetch wrapper is the only place that knows about HTTP.**
*Reasoning:* [`api/client.js`](frontend/src/api/client.js) handles base URL, params, JSON vs.
multipart, and error normalization; `api/jobs.js`, `api/topics.js` and the rest expose plain
async functions. No component constructs a URL or reads `res.status`. That boundary is also
where the camelCase↔snake_case translation lives: `patchJobReview` maps the UI's
`topicIds`/`newTopicNames` onto the API's read shape, so the rest of the frontend never has to
know that the review payload's read and write field names differ.

**Normalized API errors, because FastAPI's error body has two shapes.**
*Reasoning:* `detail` is a string for a hand-raised `HTTPException` and a **list** of
`{loc, msg, type}` objects for a 422 validation failure. Passing that list to `new Error()`
stringifies it as `"[object Object]"` — so every validation error in the app rendered as
`[object Object]` in a toast. `formatErrorDetail` renders both shapes as readable text and
flattens `loc` into a field path, so a 422 now says `name: field required` in the place the
user is looking. Error *messages* are a UI surface; they deserve the same attention as a
component.

**Feature-folder colocation, with a shared `components/common`.**
`pages/review/ReviewDetailPage.jsx` sits next to `pages/review/components/*`. A component is
promoted to `components/` only when a second feature actually needs it — which is how
`TopicChipInput`, `DocumentViewer`, `FileDropzone` and `DeleteBatchButton` got there, each
with a genuine second call site.
*Reasoning:* the alternative — one flat `components/` directory — makes it impossible to tell
what is shared from what is incidentally reused, and every shared component becomes something
you have to check all call sites before touching. Promotion-on-second-use keeps that cost
proportional.

### Representing work in flight

**SSE below four documents per batch, polling above it.**
*Alternatives:* SSE for everything; polling for everything.
*Reasoning:* SSE gives a genuinely live progress bar per document, and one `EventSource` per
row is fine for the small batch a user is watching. It stops being fine quickly — browsers cap
concurrent connections per origin at around six, so a 20-file batch opening 20 streams
exhausts the pool and starves *the app's own API calls*, including the ones that would tell the
user anything is wrong. Above the threshold, [`useBatchPolling`](frontend/src/hooks/useBatchPolling.js)
polls `GET /batches/{id}` — **one request covering every job in the batch** — and stops
polling entirely once no job is in flight (`refetchInterval` returning `false`).
*Consequence:* [`UploadProgressPage`](frontend/src/pages/upload/UploadProgressPage.jsx) renders
two row components, `JobProgressRowLive` and `JobProgressRowStatic`, against one visual design.
The duplication is deliberate: the alternative is one component with a mode flag branching on
its own data source, which hides the connection-count decision inside a leaf.

**`useJobEvents` seeds from a fetch, then streams — and keeps a fallback poll.**
*Reasoning:* the SSE stream carries deltas, not history, so a page refreshed mid-extraction
would show nothing until the next progress tick. Seeding from `GET /jobs/{id}` first makes
refresh work. A 2s fallback poll runs alongside, because an `EventSource` that dies without a
clean close otherwise leaves the UI frozen on a stale status forever. Whichever source reports
a terminal status first tears down both. `awaiting_review` counts as terminal for the stream:
past that point, state only changes by explicit user action, so there is nothing to stream.

**A standing `/queue` page, not just a per-batch progress URL.**
*Reasoning:* `/upload/:batchId/progress` only exists for the batch you just uploaded. Close
that tab and the work is invisible — still running, but unreachable. The
[extraction queue](frontend/src/pages/processing/ProcessingQueuePage.jsx) re-derives every
in-flight and failed document from scratch on each load, batch and all, so there is nothing
to lose track of. The review queue then carries an
[`InProgressList`](frontend/src/pages/review/components/InProgressList.jsx) pointer, because
the review queue lists *only* `awaiting_review` — landing there while something is still
extracting would otherwise look like the document vanished.

**A bookmarked progress URL still polls.**
When the router-state handoff from `POST /documents` isn't available — a reopened or bookmarked
progress link — the page falls back to `GET /batches/{id}`. That query fetched **once** and
then sat frozen on whatever status the jobs happened to have, forever, looking like extraction
was stuck. It also fed `allTerminal`, so the same freeze removed the link forward to review.
The fallback query now carries the same `refetchInterval` as the primary path.

**Router state as a handoff, with the fetch as the fallback.**
*Reasoning:* `POST /documents` already returns the batch id and every document's job id, so
passing that through `navigate(..., { state })` renders the progress rows with no round trip —
the user sees their filenames the instant the upload completes. But router state is gone on
reload, so the same screen has to work without it. Handoff for speed, fetch for correctness.

### The review screen

This is the product's centre — the one screen where a human either catches a bad extraction or
lets it into the dataset — and [it](frontend/src/pages/review/ReviewDetailPage.jsx) carries
most of the frontend's difficulty.

**Draft state in a `useReducer`, seeded once per *extraction run*, not once per mount.**
*Alternatives:* seed on mount; treat the server payload as the single source and PATCH on every
keystroke.
*Reasoning:* the reviewer is editing a draft that the server also rewrites underneath them —
every PATCH writes its response back into the cache, and a re-extract replaces the whole table.
Seeding on "payload changed" would discard their in-progress edits on every save. Seeding on
mount would never pick up a re-extract. So the seed is keyed on
`review.result.reextract_history.length` — the count of extraction runs. An ordinary refetch
leaves edits alone; a completed re-extract bumps the count past `seededRun` and re-seeds the
table. A reducer rather than eight `useState`s because the fields interact: choosing a matched
schema has to clear `proposedSchemaFields` and `saveSchemaAs` in the same transition, and that
rule belongs in one place.

**Per-document columns fill down when edited.**
*Reasoning:* the staged payload is redundant by design — a document's header values are copied
onto every row, so the table shows exactly what Confirm will store. But a reviewer fixing a
misread vendor name means *"the document says this"*, not *"row 3 says this"*, and must not
retype it once per line item. Editing any cell of a `scope !== "row"` column writes it to every
object. Adding a row copies the per-document values from the first row rather than leaving them
blank, since a blank would confirm as a null.
*The direction of the default matters:* filling a column down is the destructive move, so it
requires an *explicit* `scope` declaration. An ad hoc column the model proposed has no scope
and stays cell-local — the backend defaults a missing scope to `document`, and the UI
deliberately defaults it the other way, because guessing wrong in the backend costs a label and
guessing wrong here overwrites twelve cells the reviewer didn't touch.

**Columns are the declared field list *plus* anything the payload actually carries.**
*Reasoning:* a field the model found nothing for has no key in `extracted_data`, so
data-derived columns would hide it — and the reviewer would never get the chance to fill it in.
Conversely, a schema edited between extraction and review has fields the payload doesn't match,
and dropping those columns would **silently confirm values the reviewer never saw**. So
declared columns come first, in schema order, and undeclared-but-present keys are appended
rather than discarded. Showing a column that shouldn't be there is a visible oddity; hiding one
is invisible data loss.

**Seeding is guarded on type, not on shape.**
`job.result` is an untyped JSON blob any client can PATCH. `Array.isArray(result.extracted_data)
? … : []` — because a non-array there takes the column derivation (`.flatMap` over rows)
straight to a `TypeError` and blanks the entire page. The review screen is the one place a
malformed payload must not become a white screen, since it's where you'd go to fix it.

**Confidence flags sit above the input, per cell.**
*Reasoning:* confidence is per cell, not per column — a header-column flag would either
aggregate away the one bad row or mark the whole column suspect. Putting the flag in the cell
puts the reviewer's attention exactly where the model was unsure.

**Re-extract is in-place, with its own live status.**
*Reasoning:* a misread column is one sentence to describe and expensive to fix cell by cell, so
the reviewer can send feedback and re-run. That puts the job back through the worker, so the
page has to watch it the way the queue does — but `useJobEvents` is enabled *only* for the
duration of a run, since the stream closes at terminal status and flipping the flag back on is
what re-arms it for the next one. A failed re-extract says so and leaves the previous table
untouched; the destructive part (discarding the reviewer's cell edits) is confirmed before the
run rather than merged after, because interleaving a fresh read with edits made against the
previous one produces a table matching neither.

**`minmax(0, 1fr)`, not `1fr`.**
A grid track's implicit `min-width: auto` lets wide content force its column past its share.
A spreadsheet preview's table did exactly that, pushing the extracted-data column off the right
edge of the screen. Worth writing down because it looks like a typo and is the difference
between a usable review screen and an unusable one.

### Telling the user the truth

A theme rather than a single decision, and the frontend counterpart to the backend's "a
fallback path needs to be loud."

**The toast reports what the server did, not what was asked.** Deleting a batch whose documents
are all already confirmed deletes nothing — the backend keeps confirmed records. The UI reads
`deleted_document_ids` and `kept_document_ids` off the response and says *"Nothing deleted — all
3 documents in this batch are already confirmed"* rather than a blanket "Deleted." Reporting
success for a no-op is a lie the user only catches by reloading.

**The confirm dialog hedges the count it isn't sure about.** The queue can only see its own
filtered subset of a batch, so the dialog says what it knows ("removes N documents from the
queue") and states the rule separately ("documents you've already confirmed are kept"), instead
of asserting a total it can't compute.

**Destructive actions are behind a dialog when they're genuinely irreversible.** Batch delete
unlinks the originals from disk, so it's the one queue action nothing can undo — dialog. Confirm
and reject are not, so they aren't gated.

**The answer shows which branch produced it.** The Ask page renders `routing_used` and, for the
SQL branch, the statement that actually ran post-repair. The user can tell whether a number came
from a query over their records or from summarized prose — which is the difference between a
figure they can act on and one they should check.

**Empty states and full-queue guidance are the same component.**
[`QueueGuidance`](frontend/src/components/common/QueueGuidance.jsx) renders one line at the top
of a busy queue and the full copy inside the empty state.
*Reasoning:* the question a user has when the queue is empty (*"where did everything go?"*) is
the same one they have when it's full (*"what is this waiting for?"*). Writing it twice is how
those two answers drift apart. And both variants point *forward* — an empty extraction queue is
usually not an error, it means the work moved to review, so the empty state's job is to say
where rather than to apologize.

**The completion banner renders where the batch's card was.** The user was watching that spot,
so that is where the answer belongs — and the "nothing is extracting right now" empty state is
suppressed while the banner is up, because by definition the queue is empty at that moment and
*"nothing is extracting"* directly under *"extraction complete"* reads as the page contradicting
itself.

### Input components

**`TopicChipInput` caches labels instead of deriving them.**
*Reasoning:* chip labels were derived from the current search results, which only hold what
matches what's typed *right now*. So a selected topic's name turned back into a raw UUID the
moment you typed something that didn't match it — and a freshly created topic became a UUID as
soon as you cleared the box. It looked like the selection had been lost. Labels now accumulate
into a ref-held `Map`, and any id still unlabelled (a suggested topic seeded from a review
payload, or one outside the typeahead window) is resolved by a `getTopicsByIds` fetch, so a chip
is never a bare UUID.

**One flat option list, so mouse and keyboard commit through the same path.**
The typeahead results and the synthetic `+ Create "…"` row are one `options` array; `Enter` and
click both call `commitOption`. Two lists would be two chances for the create row to behave
differently depending on how you reached it. The highlight index is *clamped* on read rather
than reset from an effect, because the list shrinks whenever a search settles and a stale index
must never point past it.

**The blur-vs-click race, fixed properly.**
The dropdown originally closed on the input's `onBlur` after a 100ms `setTimeout`. That raced
every click: `mousedown` on an option blurs the input, 100ms later the dropdown unmounts, and a
deliberate click — mouseup more than 100ms after mousedown, which is most of them — landed on a
button that no longer existed and **silently selected nothing.** Now the wrapper's `onBlur`
checks `relatedTarget` containment so it closes only when focus genuinely leaves the widget,
*and* options `preventDefault` on mousedown so the input never loses focus to them at all.
Two mechanisms because the first makes it correct and the second makes it not depend on
event ordering.

**Enter used to do nothing.** Typing a topic name and pressing Enter left the text sitting in the
box, uncommitted — so the upload carried no topic hint even though it looked like one had been
picked. Silent no-ops on the primary key of a text input are the worst class of UI bug: nothing
is wrong on screen.

**A failed create is surfaced.** Without it, a rejected `createTopic` was completely silent and
the typed name just sat there looking accepted.

**Async callbacks read the selection from a ref.** `createTopic`'s `onSuccess` appends to
`selectedIdsRef.current`, not to the `selectedIds` captured when the request went out, so a
selection made while the create was in flight isn't clobbered. And because topic creation is
get-or-create server-side, the success handler checks membership before appending — otherwise
creating a topic that already existed duplicated its chip.

### Performance

**Lottie is code-split, and it's the light build.**
*Reasoning:* `lottie-react` re-exports every engine build from one barrel and `lottie-web`'s
builds aren't side-effect-free, so a static import pulls ~200 kB of player into the entry chunk
for animations that appear on three screens. The animation JSON is heavy too.
[`createLazyLottie`](frontend/src/components/common/lazyLottie.jsx) defers both behind
`React.lazy`, so they arrive while the user is already waiting on work.
`LottieLight` specifically, because the light build drops the expression engine — which none of
these animations use and which is the part carrying a direct `eval`.

**Previews are cached forever; the queues poll adaptively.**
A stored original never changes and partitioning a large scan isn't cheap, so
`["documentPreview", id]` uses `staleTime: Infinity` — remounting the review pane doesn't re-run
it. The queues go the other way: `refetchInterval` returns 2s when there are rows and 5s when
there aren't, and the batch poll returns `false` once nothing is in flight, so an idle queue
stops asking.

**`refetchOnWindowFocus: false` globally.** With adaptive polling already covering the live
surfaces, focus-refetching every query on every tab switch was redundant load that also caused
visible reshuffles on the review screen.

---

## Decisions I reversed

The ones where the first answer was wrong in a way the design couldn't show.

**Deleting a batch celebrated it.** Covered above — the single best example in the project of
two conditions producing one indistinguishable signal. The fix isn't clever; it's recognizing
that the delete is the only party with the missing information and making it responsible for
saying so.

**Chat history moved from `localStorage` to the server.** `useChatHistory` kept one flat message
list in `localStorage`: exactly **one conversation per browser**, gone on a cleared profile,
invisible from any other device. Conversations are now server-side records, which is what made
a session list, rename and delete possible at all.
*And then a second bug inside the fix:* reopening a conversation seeded messages from "any
payload for this chat id," which replayed whatever was in the query cache — and that snapshot
goes stale the moment another turn is appended. Reopening a two-question conversation showed
only the first question. Seeding now waits for a *settled* fetch of the chat that was actually
asked for (`chat.id === pendingChatId && !isFetching`); the `!isFetching` half is the important
one, because react-query hands back cached data immediately and refetches behind it, and that
cached copy is precisely the stale snapshot that truncated the transcript. Turns otherwise live
in local state, so a freshly answered question can't flicker out while a refetch lands.

**A frozen progress page.** Covered above: the bookmarked-URL fallback query had no
`refetchInterval`, so it showed a permanent snapshot of whatever was true when the page loaded,
including "still extracting" for work that had long finished — *and* withheld the link forward,
because `allTerminal` read the same frozen data.

**The dropdown's 100ms blur timeout.** Covered above. The lesson I'd carry: a timing constant
standing in for "has the click finished" is always a race, and the fix is to ask the DOM what
it actually knows (`relatedTarget`) rather than to tune the number.

**`Content-Disposition: inline` does not do what I assumed.** The review pane pointed an
`<iframe>` at the raw bytes and hoped. Browsers ship viewers for PDF, common web images, plain
text and HTML — everything else (`.docx`, `.xlsx`, `.pptx`, `.csv`, `.eml`, a multi-page
`.tiff`) went to the **download manager** instead. The header expresses intent; it cannot hand
the browser a renderer it doesn't have. The server now decides which renderer applies and
converts the rest into a structured element list, and
[`DocumentViewer`](frontend/src/components/DocumentViewer/DocumentViewer.jsx) dispatches on
`kind`. Three things I'd call out:
- The converter is `unstructured.partition` — **the same parser the extraction pipeline uses**,
  so the reviewer sees the text the extraction saw rather than a second, differently-lossy
  rendering. Reviewing against a different view of the file than the model read is a subtle way
  to make review useless.
- `unsupported` is an explicit case, not a fallthrough. An unpreviewable file says so instead of
  silently starting a download nobody asked for.
- An uploaded HTML file renders in an iframe with `sandbox=""` — no scripts, no forms, no
  navigation. It's untrusted content the user uploaded, and it's the one preview path that
  renders foreign markup natively.

**Graceful fallback hid three real bugs.** The query router falls back to RAG when the SQL branch
can't answer, which is right, and *silently* right — so three hard failures rendered as
plausible answers. A schema-qualified view name (`invoice.view_invoice`) meant **structured
questions never once got a structured answer**; topic scoping hit `InsufficientPrivilege` on a
table the read-only role deliberately can't see; and the SQL answer was a hardcoded `"Ran a SQL
query and found N row(s)."` The durable lesson: **a fallback path needs to be loud** — every
rejection logs, and the response reports `routing_used` and the SQL that ran, so the UI can show
which branch answered. The structural fix for the first was a repair pass in the SQL normalizer
rather than another attempt at prompting: *prompt instructions are a request; the normalizer is
a guarantee.*

**File-hash dedup: removed after building it.** A `UNIQUE` constraint on `file_hash` silently
folded a byte-identical re-upload into the existing document — no new job, so nothing extracted
and nothing appeared in either queue. **The user uploaded a file and the product did nothing,
with no explanation.** The deeper mistake was treating "these bytes are identical" as "the user
didn't mean this." Identity-field dedup answers the real question; the byte tier answered one
nobody asked. Following from that, a dedup match went from *blocking* a confirm to *informing*
one — refusing to save data the user can see is correct is a bad default.

**Auto-detected query scope now relaxes itself; explicit scope never does.** An auto-detected
topic holding none of the matching chunks answered *"there are no sources available"* while the
documents sat in plain sight under another topic. Both branches retry unscoped when a scope
produced nothing — **but only when the scope was our own guess.** An explicit user selection is
honoured even when it matches nothing, because overriding it would be lying about what was
searched.

**Append-only gave way to a per-document delete.** The original stance was that confirmed data is
immutable: `DELETE /batches/{id}` clears a queue but deliberately *spares* confirmed documents,
because their records and chunks are live data the schema views and the Ask page serve. That is
defensible right up to the moment someone uploads the wrong file and confirms it — and then there
is no route to removing it at all, short of dropping the whole schema. `DELETE /documents/{id}`
is the loud counterpart: one document, and everything derived from it, confirmed records
included. Two things made it more than a `db.delete()`:

- **Order is load-bearing.** All four tables referencing `documents` are `ON DELETE NO ACTION`, so
  children must go before the parent or Postgres refuses the whole statement.
- **Bulk-deleting children then deleting the parent cannot work.** `Document.jobs`,
  `.extracted_records` and `.chunks` are relationships with no cascade, so deleting the parent
  makes SQLAlchemy load each collection and null the children's FK to de-associate them. Those
  rows are already gone, the UPDATE matches nothing, and the flush raises `StaleDataError` — which
  surfaced as a 500 that *claimed the `db` hostname was unresolvable*. Expiring the instance first
  makes the collections reload empty, so there is nothing to de-associate. `schemas.py` had
  already hit the identical trap on its own delete path; I re-derived the fix instead of reading
  the comment that was sitting there.

The button belongs in Topics and Schemas, not the queues. The queues are for work still being
triaged, where batch delete already applies; the thing that was missing was a way to remove a
document *after* it was finished, from the places that list finished work.

**One document did not fit in one minute's token budget, so the provider changed.** Extraction
failed on Groq with a 429 on tokens-per-minute, and the instinct — add retries — was the wrong fix.
The arithmetic said the pipeline could not succeed at any retry count. Measured, not estimated:

| Call | Text sent | Real tokens |
|---|---|---|
| Schema match | 4,000 chars | 1,994 |
| Field extraction | 12,000 chars | 5,166 |
| Topic suggestion | 4,000 chars | 2,038 |
| | | **9,198** |

**One document cost ~9,200 tokens against a total budget of 8,000 per minute.** A retry waits for a
budget the same document is about to overspend again. Only about a third of that was document size;
the rest was sending the document three times, ~2,100 tokens of invisible reasoning tokens
(`gpt-oss-120b` is a reasoning model and `max_tokens` does not bound them), and the JSON schema
echoed into every request.

Cutting tokens to fit was possible but meant permanently degrading the prompts. Gemini's free tier
turned out to be metered differently, and that was the better lever:

| | Groq `gpt-oss-120b` | Gemini `3.5-flash-lite` |
|---|---|---|
| Binding free-tier limit | 8,000 **tokens**/min | 15 **requests**/min |
| Tokens per document | 9,198 | ~2,500–6,000 |
| Documents per minute | 0.87 | **~4.3 measured end to end** |
| Throughput vs document size | degrades | flat |

**Gemini does not use fewer tokens — it uses more** (10,648 vs 9,198 on identical input; its
tokenizer runs ~2 chars/token against Groq's ~4.2). It wins because tokens stop being the rationed
resource. Everything below follows from that one fact:

- **Requests became the currency, so calls got merged, not prompts trimmed.** Schema match and
  topic suggestion were two "list + LLM classifies" calls over the *same* text, run back to back.
  `llm_classify_multi` asks both in one request, taking the pipeline from three requests per
  document to two. The tasks stay explicitly separate in prompt and schema rather than being
  blended into one question, because one answer contaminating the other is exactly what decision
  #20's one-focused-call-per-decision rule exists to prevent — verified with an invoice against an
  employment-contract schema and an HR topic, neither of which leaked.
- **`MAX_EXTRACT_CHARS` went up, from 12k to 40k.** The opposite of what the token limit wanted.
  Each chunk of the context-limit fallback is a separate *request*, so smaller chunks cost
  throughput and buy nothing.
- **`MAX_CLASSIFY_CHARS` was deleted.** It existed only to cut tokens, and it cost match quality.
- **Work already known is no longer asked.** A batch that hinted its topics, or a schema picked at
  upload, removes that task from the merged call; when nothing is left the call is skipped
  entirely. Verified: a topic-hinted upload with no schemas made **zero** classify calls.
- **The pacer meters requests, from the provider's own numbers.** The 429 body carries a
  `QuotaFailure` with the real `quotaValue` — which is how 15/min and 5/min were established — so
  `app/pipeline/ratelimit.py` adopts the true quota instead of trusting config.

Two bugs were worth the scar tissue, both of them mine, and both recorded in the module:

- **Never advance a rate limiter's clock into the future.** Honouring `retry-after` by pushing the
  window's start time ahead of now interacts fatally with a projection that clamps elapsed time
  with `max(0.0, now - at)`: the window read empty *forever*, every iteration recomputing the same
  wait. The symptom was 14 identical 28-second waits, a worker pinned for six and a half minutes,
  and five queued documents that never started. A 429 now marks the window full *as of now* and
  nothing more; per-request `retry-after` is the SDK's job.
- **A truncated JSON-schema reply still parses, and that is the danger.** Capping `max_tokens` at
  1024 made a 25-row invoice come back as 21 rows — valid JSON, schema-conforming, four rows
  silently gone, on data a human was about to confirm. `max_tokens` is now a generous runaway guard
  (8192) and `finish_reason == "length"` is a hard failure. This one is provider-independent and is
  the single most dangerous thing found in this work.

*Cost accepted:* the pacer's window is process-local, so its accounting is only truthful while one
process is calling the provider — which is why the worker default dropped from 2 to 1. That is a
rate-limit decision wearing the clothes of a concurrency setting, worth knowing before someone
raises it back for throughput. And the free tier is only defensible while the corpus is synthetic:
Google's unpaid-tier terms say human reviewers may read submitted content and to keep confidential
information out of it, which invoices plainly are. That precondition is in `DEPLOY.md`, because
nothing in the app can notice that a real invoice has been uploaded.

---

## Deployment decisions

Getting this onto a public URL surfaced problems that only exist outside a dev loop.

**One image, two commands — the split was fictional.** `Dockerfile.api` claimed to skip "the
OCR/torch stack that the worker image needs". It did not: `pyproject.toml` has a single flat
dependency list, both Dockerfiles ran a bare `pip install .`, and so both built the same ~5 GB
tree. The API needs torch regardless — `app/api.py` reaches `sentence_transformers` through the
query router — and needs `unstructured` for previews. Collapsing to one image and selecting the
entrypoint with compose's `command` halves the build.

**Nothing could migrate a fresh database.** Neither Dockerfile copied `migrations/` or
`alembic.ini`, so `docker compose run api alembic upgrade head` could never have worked; the only
path that ever existed was `run.sh` invoking Alembic from a host venv. A compose-only deployment
was therefore impossible, and nothing said so until a real one was attempted. Ordering matters
too: `depends_on` waits for the database to be *healthy*, not *migrated*, and the API's
`seed_uncategorized_topic` startup hook queries `topics` with no error handling — so on an
unmigrated database the API crash-loops rather than reporting anything useful.

**Postgres was published to the internet.** `docker-compose.yml` published `5432:5432` with
password `raw2query`. Fine on a laptop, indefensible on a public VM — and worse than it looks,
because Docker publishes ports by DNAT in `PREROUTING`, which **bypasses the iptables INPUT
chain**. A host firewall would not have contained it. The API's own port was published on
`0.0.0.0` too, reachable past the reverse proxy that is supposed to be the only way in. Both are
now `expose`/loopback.

**Compose merges `ports` by appending, not overriding.** Publishing `8000:8000` in the base file
and narrowing it to `127.0.0.1:8000:8000` in the production overlay produced *two* bindings.
Docker bound `0.0.0.0` first, failed the second with "address already in use", and left a
half-created container with **no network attached** — which then presented as the API being unable
to resolve the `db` hostname. Two misleading symptoms from one cause. Development publishing now
lives in `docker-compose.override.yml`, which compose loads automatically for a bare
`docker compose up` and skips whenever explicit `-f` flags are passed.

**A default `pip install` ships a GPU stack to a CPU box.** No torch variant is pinned, so pip
resolves the CUDA build plus ~15 `nvidia-*` packages — several GB of dead weight, and it
*succeeds*, so nothing looks wrong until the image is built. Installing torch from the CPU
channel first, with `--index-url` (not `--extra-index-url`, which lets pip prefer the PyPI CUDA
wheel anyway) and a pinned `torchvision`, fixes it. `triton` still arrives as a torch dependency:
897 MB of GPU kernel compiler that only `torch.compile`'s GPU codegen uses. Removing it in the
same layer that installs it — layers are additive, so a later `pip uninstall` saves nothing — took
the image from 4.54 GB to 3.61 GB with embeddings, `unstructured` and the API all still working.

**`--only-binary=:all:` is too blunt.** It was meant to turn a missing wheel into a fast, named
failure instead of a 30–60 minute cmake build that would be OOM-killed. It instead made resolution
outright impossible: `unstructured` depends on `langdetect`, which publishes no wheel at all — a
1 MB pure-Python sdist that builds in seconds. Named packages only.

**The demo gate had to be shaped around what the browser can't do.** The API has no auth, so a
shared header token is checked at the proxy. Cookie schemes were ruled out by the code, not by
preference: `useJobEvents` builds an `EventSource` with no `withCredentials` and the fetch wrapper
sets no `credentials`, so anything cookie-based breaks every cross-origin call. The token gate
needs two exemptions to work at all — `OPTIONS`, because a custom header triggers a CORS preflight
that does not carry the header, and `GET` on `/documents/*` and `/jobs/*`, because `fileUrl()`
feeds `<img>`/`<a>` attributes and `EventSource` has no headers API. Those paths are read-only,
keyed by unguessable UUIDs, and cost no LLM calls. It stops crawlers, not people; the token ships
in a public bundle. The provider's rate limit is the real backstop.

---

## Backend decisions, in brief

Fuller treatment in [`plans.md`](plans.md); these are the calls that shaped the product.

**Nothing reaches the real tables until a human confirms.** The pipeline stages its output in
`jobs.result` and stops at `awaiting_review`; `confirm.py` is the only writer of
`extracted_records`, `document_topics` and `chunks`, in one transaction. An LLM extraction is a
*proposal*. This is the decision the whole trust story — and the entire review screen — rests
on. *Cost accepted:* a 50-file batch needs a human before it's queryable, mitigated by bulk
"confirm all clean."

**The hardest systems problem was making extracted values genuinely queryable.** The model is
instructed to report what the document says, and does: `"83,880.00"`, `"9%"`, `"₹1,250.50"`,
`"(1,200.00)"`. Every one is a correct reading; not one survives `::numeric`. Worse, the failure
isn't local — a hard cast in the generated view means **one unparseable cell takes down the whole
view for every row and every consumer.** Four layers: normalize on write; *refuse to guess*
(dates are deliberately untouched — `03/04/2026` is genuinely ambiguous and a wrong guess is
worse than a null); `safe_*` cast functions that null out one cell instead of raising;
re-normalize at confirm time, because a reviewer typing `1,250.00` carries the same problem as
the model. Layers 1 and 3 are redundant on purpose.

**One Postgres instance does four jobs:** relational store, job queue (`FOR UPDATE SKIP LOCKED`,
no broker), vector index (`pgvector`), full-text index. One datastore means one transaction
boundary and a setup a stranger can run in one shot. *Accepted:* polling latency, and `pgvector`
will lose to a dedicated store at millions of vectors.

**One physical store per schema lineage with a generated view, not a table per version.**
Table-per-version makes *"total spend across all invoices"* `UNION` every historical table — a
query-time tax that grows with every schema edit. Records are JSONB stamped with
`(schema_id, version)`; the view flattens the union of every version's fields.

**Two independent rails on generated SQL:** a `sqlglot` parse that rejects anything but a single
pure `SELECT` (checking write/DDL nodes *anywhere* in the tree, since a write hides inside a
CTE), and execution through a Postgres role with `SELECT` on the views and nothing else.
Scope is *injected* into the parsed statement, not requested in the prompt — the model could omit
a requested filter, and when it did the aggregate ran over the whole corpus while the UI
reported "searched within 1 topic(s)" beside it.

**A routing layer, not one query mechanism.** RAG can't reliably total invoices across
documents; SQL can't answer *"what does this agreement say about cancellation?"* Both question
types are native here.

**Local embeddings (`bge-small-en-v1.5`).** Embedding is the one cost that scales with corpus
size rather than user activity — local means no per-chunk cost, no ingest rate limit, and the app
works offline.

---

## What I deliberately cut

| Cut | Why it was right for this build | What it would take |
| --- | --- | --- |
| Optimistic updates on mutations | Every mutation here is either cheap or irreversible. Optimistically showing a confirm that then failed would claim data is live when it isn't | Per-mutation rollback, and a rule for which are safe to fake |
| Focus trapping / full a11y audit | Custom modal and dropdown carry ARIA roles and keyboard nav, but not focus containment | `inert` or a focus-trap on `Modal`, then a real audit |
| Mobile layout | The review screen is a side-by-side document/table comparison — the one job the product exists for, and not a phone task. Grid tracks and tables assume desktop width | A stacked review layout with a preview/table toggle |
| Token-streamed answers | The Ask page shows a thinking state and returns a composed answer. Streaming is polish, not capability | SSE on `/query`, incremental `ChatMessage` render |
| Undo | Nothing is undoable, so destructive actions get dialogs instead. A fake undo is worse than none | Soft-delete server-side first |
| Auth, multi-tenancy | Proves nothing about the pipeline; tenant filters interact badly with NL-to-SQL | `tenant_id`, RLS, injected predicate |
| Edit/delete on confirmed records | *Partly reversed — see below.* Deleting a whole document is now possible; editing a confirmed record's values still isn't | Soft-delete plus chunk/vector reconciliation |
| Batch schema consistency | Similar documents in one batch can independently infer different schemas. Real risk, accepted — human review is the net | Pre-clustering, or bulk-apply-schema on review |
| Stuck-job recovery | `jobs.locked_at`/`locked_by` are written but nothing sweeps them — a worker killed mid-job leaves the job `extracting` forever | A reaper returning timed-out jobs to `pending`, with an attempt counter |

The last one is what I'd be least comfortable shipping, which is why it's listed rather than
buried.

---

## What I don't trust yet

**The completion mechanism is inference, not an event.** `useBatchCompletion` is careful and I
believe it's correct for the cases enumerated above, but it infers a state change from a filtered
list's contents. It's coupled to both queues staying filtered the way they are today — a future
change that shows confirmed documents in the review queue would break completion detection
silently, because "present, then absent" would simply stop happening. A `batch_completed` event
or a status field on the batch would make it a fact rather than a deduction.

**Frontend test coverage is thin and lopsided.** The Playwright suite drives a real Chromium
(not the headless shell, which ships no PDF viewer and would "download" every PDF regardless of
what the app does) across both surfaces that show a document, and the preview type-resolution
test reads the supported-extension list *out of `unstructured` itself*, so gaining a new file
type fails the suite rather than silently shipping a download. But that's preview coverage. The
logic most likely to regress — `useBatchCompletion`'s three-way disambiguation, the review
draft's seed-once-per-run rule, the chip input's label cache — is verified by hand. Those are
pure-ish functions over enumerable states, and they're where I'd start.

**A dropped SSE connection is covered; a slow one isn't.** The fallback poll catches an
`EventSource` that dies, but a stream that stays open and simply stops delivering looks
identical to a job that's genuinely still working. There's no staleness timeout on progress.

**No error boundary.** A render-time exception anywhere below `AppShell` blanks the app. The
review screen's type guard exists precisely because that page had the most likely path to one,
but that's a point fix where a boundary is the general answer.

**Concurrency across tabs is unhandled.** Two tabs open on the same job can both PATCH a review
draft, last write wins, and neither is told. Single-user was a deliberate scope call, but "one
user, two tabs" is a real situation the design doesn't address.

**The routing decision has no ground truth.** `decide_routing` picks SQL or RAG per question and
nothing measures whether it picks correctly. A wrong "sql" degrades to RAG; a wrong "rag" on an
aggregate question produces a confidently-worded, possibly-wrong number — the failure mode with
no safety net. That needs an eval set, not more prompt engineering.

**Extraction quality is unmeasured.** No labelled set, so "accuracy" is an impression from manual
testing. Confidence scores are the model's self-report, which is not calibration — 0.9 does not
mean nine of ten are right, and `is_job_clean` thresholds against it as though it did.

**Confirming without naming a schema silently throws the extraction away.** This is the one I'd
fix first, because it cost real data on the live deployment before I noticed. `confirm.py` has
three branches: a named ad hoc shape creates a schema and records, a matched schema writes
records, and *everything else falls through storing nothing*. The job is marked `confirmed`
either way. So confirming a first-of-its-kind document without typing a schema name marks it
done and discards every extracted field — 13 correctly extracted invoice fields, in the case that
caught my attention, with the UI reporting success. It is defensible as a rule (an ad hoc shape is
only promoted if you name it) and indefensible as an interaction, because nothing asks and
nothing warns.

It compounds: **`confirm-all-clean` cannot pass a schema name at all**, so the first document of
any new type is *guaranteed* to hit the discarding branch. The batch action is unusable for
exactly the case where it looks most useful. And when it skips a low-confidence document it
returns `skipped_job_ids` faithfully — which the UI does not surface, so "nothing appeared to
happen" is indistinguishable from a failure. Three separate silent outcomes in one flow.

**Repeating data has one right representation and nothing enforces it.** Line items belong in
`scope: "row"` fields, which project one `extracted_records` row per source row; that is what
makes `SUM`/`GROUP BY` over line items possible. A hand-written schema that models them as one
blob field instead is rejected by the API only incidentally — there is no `list` field type — and
the error names a type, not the modelling mistake. Nothing guides an author toward row scope, and
a schema that omits it produces a table that quietly cannot answer the questions it exists for.
