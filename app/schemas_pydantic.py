"""Pydantic request/response models for the API layer.

Named `schemas_pydantic` (not `schemas`) to keep a clean distinction from the
`schemas` / `SchemaDef` DB table, which is a first-class domain concept here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schema_fields import COLUMN_KEY_RE

FieldType = Literal["string", "number", "integer", "boolean", "date", "datetime"]
# Whether a field takes one value for the whole document ("Invoice Number")
# or one value per repeating row ("Line Amount"). A schema that declares no
# row-scoped field behaves exactly as it did before this existed: one
# document, one extraction, one `extracted_records` row.
FieldScope = Literal["document", "row"]
JobStatus = Literal["pending", "extracting", "awaiting_review", "confirmed", "rejected", "failed"]
DedupDecision = Literal["skip", "keep_both", "replace"]
RoutingChoice = Literal["sql", "rag"]


class SchemaFieldDef(BaseModel):
    """A field as sent BY a client.

    `column` is the field's stored, opaque column key (see
    `app/schema_fields.py`). It is optional on input and echoing it back is
    what distinguishes a rename from a delete-plus-add - so the schema edit
    page must round-trip it, which is why `SchemaFieldOut` carries it too.

    Deliberately NOT defaulted to a freshly minted key: minting here would
    happen on every request, including every PUT, and would make "the client
    sent nothing" indistinguishable from "the client sent a real key",
    destroying rename detection. Keys are minted at the three
    `SchemaVersion` creation sites via `assign_column_keys`.
    """

    name: str
    type: FieldType
    description: str = ""
    required: bool = False
    column: str | None = None
    # Defaults to "document", which is what every field stored before this
    # existed effectively was - so an un-upgraded client, and every row
    # already in `schema_versions.fields`, keeps its exact current meaning.
    scope: FieldScope = "document"

    @field_validator("column")
    @classmethod
    def _check_column(cls, value: str | None) -> str | None:
        # This value ends up interpolated as a SQL identifier. `views.py`
        # re-asserts the same pattern at that point, because the ad hoc
        # promote path writes JSONB without passing through this model.
        if value is not None and not COLUMN_KEY_RE.fullmatch(value):
            raise ValueError(f"{value!r} is not a valid column key")
        return value


class SchemaFieldOut(SchemaFieldDef):
    """A field as returned TO a client.

    Separate from the input model only so `column` can be documented as
    server-owned. It stays nullable rather than required: if the app is ever
    deployed ahead of migration 0005, a required field here would turn the
    whole schema list into a 500 instead of letting the UI report the
    problem.
    """

    column: str | None = None


# ---- Schemas -----------------------------------------------------------


class SchemaVersionOut(BaseModel):
    id: uuid.UUID
    schema_id: uuid.UUID
    version: int
    fields: list[SchemaFieldOut]
    identity_fields: list[str] | None
    is_breaking_from_prev: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SchemaOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    # Served rather than derived: the frontend used to rebuild it with an
    # inlined copy of `safe_ident` that didn't match the Python one.
    view_name: str | None = None
    current_version: SchemaVersionOut | None = None
    # How many documents are currently assigned to this schema. Lets the edit
    # page say how much work a backfill is *before* the user ticks the box,
    # rather than after they've committed to N LLM calls.
    document_count: int | None = None
    # Populated only by PUT /schemas/{id}, which is the one response where
    # "what did saving actually do?" is a question. Left None everywhere else
    # rather than split into a second response model, so the frontend's schema
    # shape stays one thing.
    enqueued_backfill_job_ids: list[uuid.UUID] | None = None

    class Config:
        from_attributes = True


class SchemaCreate(BaseModel):
    name: str
    fields: list[SchemaFieldDef]
    identity_fields: list[str] | None = None


class SchemaVersionCreate(BaseModel):
    fields: list[SchemaFieldDef]
    identity_fields: list[str] | None = None
    # Three-valued on purpose. `True` re-extracts every existing document
    # under this schema; `False` is an explicit forward-only save; `None` is
    # "the client hasn't decided", which is an error for a breaking edit and
    # means forward-only for an additive one. Collapsing None into False would
    # delete the one signal that lets the API insist on a choice where the
    # choice matters.
    backfill: bool | None = None


class BackfillResponse(BaseModel):
    enqueued_job_ids: list[uuid.UUID]


class RecordPage(BaseModel):
    """One page of `view_<schema>` rows plus the unpaginated match count.

    `total` counts rows matching the same filters, ignoring limit/offset - it
    is what lets the table's pager know it has reached the end.
    """

    rows: list[dict[str, Any]]
    total: int


class SchemaDeleteResponse(BaseModel):
    """What deleting a schema lineage actually destroyed.

    Every count is reported rather than inferred by the client: the records
    are live queryable data, so the UI has to be able to say what went
    instead of claiming a bare success.
    """

    deleted_record_count: int
    deleted_version_count: int
    # Documents that had been extracted under this schema. They survive - only
    # their schema link and their rows in the deleted table go - so their
    # chunks stay searchable on the Ask page (decision #15).
    detached_document_count: int
    # Staged, not-yet-confirmed jobs whose matched schema was this one. They
    # stay reviewable, minus the match.
    cleared_job_count: int
    dropped_view: str


class RecordDeleteResponse(BaseModel):
    deleted_record_id: uuid.UUID


class TopicDeleteResponse(BaseModel):
    """What deleting a topic detached, and what fell back to Uncategorized."""

    unlinked_document_count: int
    # Documents left with no topic at all by the delete, and so re-tagged
    # Uncategorized (decision #16).
    uncategorized_document_count: int
    resynced_chunk_count: int
    cleared_job_count: int


# ---- Topics --------------------------------------------------------------


class TopicOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class TopicCreate(BaseModel):
    name: str
    description: str | None = None


# ---- Documents / batches --------------------------------------------------


class DocumentUploadResult(BaseModel):
    # No `duplicate_of_document_id`: every upload is its own document with
    # its own extraction job now, byte-identical re-uploads included
    # (see upload_documents / migration 0002).
    document_id: uuid.UUID
    job_id: uuid.UUID | None = None
    filename: str


class UploadResponse(BaseModel):
    batch_id: uuid.UUID
    documents: list[DocumentUploadResult]


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str | None
    file_size: int
    uploaded_at: datetime
    batch_id: uuid.UUID | None
    schema_id: uuid.UUID | None
    schema_version_id: uuid.UUID | None
    topics: list[TopicOut] = []
    latest_job_status: JobStatus | None = None

    class Config:
        from_attributes = True


class DocumentDetailOut(DocumentOut):
    extracted_record: dict[str, Any] | None = None
    chunk_count: int = 0
    jobs: list[JobOut] = []


# ---- Document preview -----------------------------------------------------

PreviewKind = Literal["pdf", "image", "audio", "video", "text", "html", "elements", "unsupported"]
PreviewElementType = Literal["heading", "text", "list_item", "table", "page_break"]


class PreviewElement(BaseModel):
    """One renderable block. `rows` is set for `table`, `text` for the rest -
    deliberately cell text, not HTML, so the frontend never has to inject
    markup derived from an uploaded file into the DOM."""

    type: PreviewElementType
    text: str | None = None
    rows: list[list[str]] | None = None
    page_number: int | None = None
    truncated: bool = False


class DocumentPreviewOut(BaseModel):
    """What the frontend needs to render a document without downloading it.

    `kind` picks the renderer. `pdf`/`image`/`audio`/`video`/`html` are
    browser-native and carry no payload - the frontend points at
    `/documents/{id}/file`. `text` carries `text`; `elements` carries
    `elements` (see app/preview.py for why office formats have to be
    converted server-side).
    """

    document_id: uuid.UUID
    filename: str
    media_type: str
    file_size: int
    kind: PreviewKind
    text: str | None = None
    elements: list[PreviewElement] | None = None
    truncated: bool = False
    message: str | None = None  # why there is no preview, for kind="unsupported"


# ---- Jobs / review ---------------------------------------------------------


class JobOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    batch_id: uuid.UUID | None
    status: JobStatus
    progress: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class JobReviewOut(BaseModel):
    id: uuid.UUID
    status: JobStatus
    result: dict[str, Any] | None
    document: DocumentOut


class JobReviewPatch(BaseModel):
    """Partial edits from the review screen, merged into jobs.result."""

    # The whole extracted table, replaced wholesale rather than patched per
    # object: the review table can add and delete rows as well as edit cells,
    # and a per-index patch cannot express a deletion without the client and
    # server agreeing on indices they have no way to keep in sync. `[]` is a
    # meaningful value (the reviewer deleted every row), which is why the
    # "not sent" case has to stay None.
    extracted_data: list[dict[str, Any]] | None = None
    proposed_schema_fields: list[SchemaFieldDef] | None = None
    matched_schema_id: uuid.UUID | None = None
    topic_ids: list[uuid.UUID] | None = None
    new_topic_names: list[str] | None = None

    class Config:
        # Unknown keys are rejected rather than ignored. A client sending a
        # retired key (a browser tab left open across the `extracted_rows` ->
        # `extracted_data` change) would otherwise have its edits accepted
        # with a 200 and silently dropped.
        extra = "forbid"


class JobReextractRequest(BaseModel):
    """Reviewer feedback that re-runs extraction on an already-extracted job."""

    feedback: str = Field(min_length=1)


class JobConfirmRequest(BaseModel):
    dedup_decision: DedupDecision | None = None
    save_schema_as: str | None = None  # name to save an ad hoc proposed schema under


class ConfirmAllCleanResponse(BaseModel):
    confirmed_job_ids: list[uuid.UUID]
    skipped_job_ids: list[uuid.UUID]  # flagged low-confidence or dedup conflict, needs individual review


class BatchDeleteResponse(BaseModel):
    deleted_document_ids: list[uuid.UUID]
    deleted_job_ids: list[uuid.UUID]
    # Documents left behind because they have already been confirmed: their
    # records and chunks are live, queryable data, so a queue tidy-up must
    # not take them with it.
    kept_document_ids: list[uuid.UUID]
    # False when a confirmed document kept the batch row alive.
    batch_deleted: bool


class DocumentDeleteResponse(BaseModel):
    """What a single-document purge removed.

    Unlike `BatchDeleteResponse` there is no `kept_*` field: this endpoint
    deletes a document whatever its state, including confirmed records that
    the schema views and the Ask page are serving. The counts are returned so
    the caller can say what actually went.
    """

    deleted_document_id: uuid.UUID
    deleted_job_ids: list[uuid.UUID]
    deleted_record_count: int
    deleted_chunk_count: int
    # True when this was the batch's last document and the batch row went too.
    batch_deleted: bool
    # Set when the document's records were the last ones in their schema, so
    # the caller can point out that the schema is now empty.
    emptied_schema_id: uuid.UUID | None = None


class BatchOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    documents: list[DocumentOut]
    jobs: list[JobOut]

    class Config:
        from_attributes = True


# ---- Query ------------------------------------------------------------------


class QueryRequest(BaseModel):
    question: str
    topic_ids: list[uuid.UUID] | None = None
    # Which conversation this question belongs to. `None` means "the user is
    # in a brand-new chat": the session row is created by this request, so an
    # opened-but-never-used "New chat" leaves nothing in the database.
    chat_id: uuid.UUID | None = None


class QuerySource(BaseModel):
    document_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    snippet: str | None = None
    # Carried so a source card can be labelled with the document it came
    # from instead of a raw uuid.
    filename: str | None = None
    page_number: int | None = None


class QueryResponse(BaseModel):
    answer: str
    routing_used: RoutingChoice
    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    sources: list[QuerySource] | None = None
    topic_ids_used: list[uuid.UUID] = []
    chat_id: uuid.UUID | None = None


# ---- Chat sessions ----------------------------------------------------------

ChatRole = Literal["user", "assistant"]


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    role: ChatRole
    content: str
    created_at: datetime
    # The answer's supporting detail (routing_used, sql, rows, sources,
    # topic_ids_used, error) so a reloaded conversation renders identically
    # to the live one.
    meta: dict[str, Any] | None = None


class ChatSessionOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None


class ChatSessionDetailOut(ChatSessionOut):
    messages: list[ChatMessageOut] = []


class ChatSessionPatch(BaseModel):
    title: str
