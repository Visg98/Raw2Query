"""Schema CRUD, versioning, and backfill (decisions #1, #14; plan section 5)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Batch, Document, ExtractedRecord, Job, SchemaDef, SchemaVersion
from app.pipeline.views import regenerate_schema_view, view_name_for
from app.schema_fields import (
    COLUMN_KEY_RE,
    ROW_SCOPE,
    ColumnKeyError,
    assign_column_keys,
    field_scope,
)
from app.schemas_pydantic import (
    BackfillResponse,
    RecordDeleteResponse,
    RecordPage,
    SchemaCreate,
    SchemaDeleteResponse,
    SchemaFieldDef,
    SchemaOut,
    SchemaVersionCreate,
    SchemaVersionOut,
)
from app.serializers import schema_out, schema_version_out

logger = logging.getLogger(__name__)

router = APIRouter(tags=["schemas"])


def _document_count(db: Session, schema_id: uuid.UUID) -> int:
    return db.query(Document).filter(Document.schema_id == schema_id).count()


@router.get("/schemas", response_model=list[SchemaOut])
def list_schemas(db: Session = Depends(get_db)) -> list[SchemaOut]:
    # One grouped count for the whole list rather than a count per schema:
    # this endpoint is polled by every page that shows a schema name, and an
    # N+1 here would scale with the number of schemas on each of them.
    counts = dict(
        db.query(Document.schema_id, func.count(Document.id))
        .filter(Document.schema_id.isnot(None))
        .group_by(Document.schema_id)
        .all()
    )
    return [
        schema_out(s, document_count=counts.get(s.id, 0))
        for s in db.query(SchemaDef).order_by(SchemaDef.name).all()
    ]


@router.get("/schemas/{schema_id}/versions", response_model=list[SchemaVersionOut])
def list_versions(schema_id: uuid.UUID, db: Session = Depends(get_db)) -> list[SchemaVersionOut]:
    schema = db.get(SchemaDef, schema_id)
    if schema is None:
        raise HTTPException(404, "schema not found")
    # Via the serializer, not `from_attributes` on the ORM rows: it maps the
    # stored identity column keys back to field names.
    return [schema_version_out(v) for v in schema.versions]


@router.post("/schemas", response_model=SchemaOut)
def create_schema(body: SchemaCreate, db: Session = Depends(get_db)) -> SchemaOut:
    if db.query(SchemaDef).filter(SchemaDef.name == body.name).first() is not None:
        raise HTTPException(400, f"a schema named {body.name!r} already exists")
    schema = SchemaDef(name=body.name)
    db.add(schema)
    db.flush()
    # A brand-new lineage, so every column key is minted here and any key the
    # client happened to send is ignored rather than trusted.
    fields = _assign_keys(db, None, body.fields)
    version = SchemaVersion(
        schema_id=schema.id,
        version=1,
        fields=fields,
        identity_fields=_identity_keys(fields, body.identity_fields),
        is_breaking_from_prev=False,
    )
    db.add(version)
    db.flush()
    regenerate_schema_view(db, schema.id)
    db.commit()
    return schema_out(schema)


def _assign_keys(
    db: Session, schema_id: uuid.UUID | None, fields: list[SchemaFieldDef]
) -> list[dict]:
    """Mints/preserves each field's column key, 400-ing on bad input.

    `schema_id=None` means a brand-new lineage: nothing to preserve, so every
    key is minted.
    """
    lineage: list[list[dict]] = []
    if schema_id is not None:
        lineage = [
            v.fields or []
            for v in db.query(SchemaVersion)
            .filter(SchemaVersion.schema_id == schema_id)
            .order_by(SchemaVersion.version.asc())
            .all()
        ]
    try:
        return assign_column_keys([f.model_dump() for f in fields], lineage_versions=lineage)
    except ColumnKeyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _identity_keys(fields: list[dict], identity_names: list[str] | None) -> list[str] | None:
    """Translates the request's identity field *names* into column keys.

    Stored as keys so a later rename can't orphan them - with names, a rename
    made `_find_dedup_match` read a key that no longer exists, so it returned
    None on the first identity field and dedup silently became a no-op for
    every subsequent upload. Names stay the API's currency in both directions
    (`serializers.schema_version_out` maps them back), so no client has to
    know about keys.

    An unknown name is now a 400 rather than the silent no-op it used to be.

    A row-scoped field is also rejected. Identity means "this document is the
    same document", and `_find_dedup_match` runs once per document against the
    document-level values - so a row-scoped identity field would be matched
    against a value that is not in the dict it is given, returning None on the
    first identity field and turning dedup into the silent permanent no-op
    that storing keys instead of names was introduced to fix.
    """
    if not identity_names:
        return None
    by_name = {f["name"]: f for f in fields}
    keys = []
    for name in identity_names:
        field = by_name.get(name)
        if field is None:
            raise HTTPException(400, f"identity field {name!r} is not one of this schema's fields")
        if field_scope(field) == ROW_SCOPE:
            raise HTTPException(
                400,
                f"identity field {name!r} is row-scoped; identity fields must be "
                "document-scoped, since they identify the document rather than one of its rows",
            )
        keys.append(field["column"])
    return keys


def _is_breaking(prev_fields: list[dict], new_fields: list[dict]) -> bool:
    """Decision #1: a field removed or retyped is breaking; a new optional
    field is additive and auto-applies without asking.

    Keyed by column rather than by name, so a rename is no longer breaking:
    the column survives it and gains a version-scoped branch, and there is
    nothing to re-extract. A rename *plus* a retype still is. A delete-plus-add
    is distinguished from a rename exactly when the client distinguishes them,
    i.e. by whether it echoed the field's key back.

    A scope change is breaking too, and is the one case where that is not
    obvious from the column: the column keeps its name and its type, but
    moving a field between document and row scope changes how many records
    each document produces, and every already-stored record was materialized
    under the old answer. Only a re-extraction can produce the new row set -
    nothing in the existing `data` blobs can be rearranged into it.
    """
    prev_by_key = {f.get("column"): f for f in prev_fields}
    new_by_key = {f.get("column"): f for f in new_fields}
    return any(
        key not in new_by_key
        or new_by_key[key]["type"] != prev_field["type"]
        or field_scope(new_by_key[key]) != field_scope(prev_field)
        for key, prev_field in prev_by_key.items()
    )


def _warn_on_probable_lost_rename(prev_fields: list[dict], new_fields: list[dict]) -> None:
    """A client that renames a field without echoing its `column` gets a fresh
    key, which splits the data: old rows stay under the old key, new rows land
    under the new one. That is exactly the pre-existing behaviour, so it is not
    an error - but it cannot be undone without hand-editing JSONB, so it should
    not pass silently.
    """
    prev_keys = {f.get("column") for f in prev_fields}
    dropped = prev_keys - {f.get("column") for f in new_fields}
    minted = [f for f in new_fields if f.get("column") not in prev_keys]
    if dropped and minted:
        logger.warning(
            "schema edit dropped column key(s) %s while adding %s - if that was a rename, the "
            "client should echo the existing `column` to keep the data in one column",
            sorted(k for k in dropped if k),
            [f["name"] for f in minted],
        )


def _enqueue_backfill(db: Session, schema: SchemaDef, version: SchemaVersion) -> list[uuid.UUID]:
    documents = db.query(Document).filter(Document.schema_id == schema.id).all()
    job_ids = []
    for document in documents:
        document.schema_version_id = version.id
        job = Job(document_id=document.id, status="pending", kind="backfill")
        db.add(job)
        db.flush()
        job_ids.append(job.id)
    return job_ids


@router.put("/schemas/{schema_id}", response_model=SchemaOut)
def create_schema_version(schema_id: uuid.UUID, body: SchemaVersionCreate, db: Session = Depends(get_db)) -> SchemaOut:
    """Saves a new version, optionally re-extracting existing documents.

    `backfill` is honoured whatever kind of edit this is, not just a breaking
    one. It used to be read as `breaking and body.backfill`, so asking to
    re-extract after an *additive* edit was accepted and then silently
    ignored - and that is the case where a backfill is most obviously wanted:
    a newly added field reads NULL for every existing document until
    something re-extracts them, and nothing about "add a field" tells the user
    their existing rows will stay blank. A client that wanted it had to know
    to call POST /schemas/{id}/backfill separately afterwards.

    A breaking edit still *requires* an explicit choice (`backfill` may not be
    None), because there the cost of guessing wrong runs both ways.
    """
    schema = db.get(SchemaDef, schema_id)
    if schema is None:
        raise HTTPException(404, "schema not found")

    prev_version = schema.versions[-1] if schema.versions else None
    new_fields = _assign_keys(db, schema.id, body.fields)
    breaking = _is_breaking(prev_version.fields, new_fields) if prev_version else False
    if breaking and body.backfill is None:
        raise HTTPException(400, "this edit removes/retypes a field; body must include {\"backfill\": true|false}")

    if prev_version:
        _warn_on_probable_lost_rename(prev_version.fields, new_fields)

    version = SchemaVersion(
        schema_id=schema.id,
        version=(prev_version.version + 1) if prev_version else 1,
        fields=new_fields,
        identity_fields=_identity_keys(new_fields, body.identity_fields),
        is_breaking_from_prev=breaking,
    )
    db.add(version)
    db.flush()
    regenerate_schema_view(db, schema.id)  # never mutates a version in place - always a new row

    job_ids = _enqueue_backfill(db, schema, version) if body.backfill else []

    db.commit()
    # The job ids are returned so the caller can say what saving actually did.
    # Without them the edit page could only claim "Schema updated", giving no
    # signal that it had just queued N LLM re-extractions - or, just as bad,
    # implying it had when the schema had no documents to re-extract.
    return schema_out(
        schema,
        document_count=_document_count(db, schema.id),
        enqueued_backfill_job_ids=job_ids,
    )


@router.post("/schemas/{schema_id}/backfill", response_model=BackfillResponse)
def backfill_schema(schema_id: uuid.UUID, db: Session = Depends(get_db)) -> BackfillResponse:
    schema = db.get(SchemaDef, schema_id)
    if schema is None or not schema.versions:
        raise HTTPException(404, "schema not found")
    job_ids = _enqueue_backfill(db, schema, schema.versions[-1])
    db.commit()
    return BackfillResponse(enqueued_job_ids=job_ids)


def _clear_schema_from_staged_jobs(
    db: Session, schema_id: uuid.UUID, version_ids: list[uuid.UUID]
) -> int:
    """Drops a deleted schema's match out of every still-staged job result.

    `confirm_job` reads `result.matched_schema_id`/`schema_version_id` and
    inserts an `ExtractedRecord` against them, so a match left pointing at a
    deleted lineage would fail the confirm on a foreign key - on the review
    screen, with nothing to tell the reviewer why. Cleared, the job falls
    through to the same path as an unsaved ad hoc extraction: still
    reviewable, still chunked and searchable, just no longer bound to a table
    that does not exist.
    """
    wanted_versions = {str(v) for v in version_ids}
    wanted_schema = str(schema_id)
    cleared = 0
    jobs = (
        db.query(Job)
        .filter(Job.status.in_(("pending", "extracting", "awaiting_review", "failed")), Job.result.isnot(None))
        .all()
    )
    for job in jobs:
        result = job.result or {}
        # Either reference is enough to break a confirm, and a staged result
        # is a client-PATCHable blob that can carry one without the other.
        matches = (
            result.get("matched_schema_id") == wanted_schema
            or result.get("schema_version_id") in wanted_versions
        )
        if not matches:
            continue
        # Reassigned rather than mutated: SQLAlchemy only notices a JSONB
        # change on assignment.
        job.result = {**result, "matched_schema_id": None, "schema_version_id": None}
        cleared += 1
    return cleared


@router.delete("/schemas/{schema_id}", response_model=SchemaDeleteResponse)
def delete_schema(schema_id: uuid.UUID, db: Session = Depends(get_db)) -> SchemaDeleteResponse:
    """Deletes a schema lineage, its extracted table, and every row in it.

    This is the loud one. Unlike a queue tidy-up (`DELETE /batches/{id}`,
    which refuses to touch confirmed data), deleting a schema deletes live,
    queryable records on purpose: the table only exists as a projection of
    this lineage, so there is no version of "keep the rows" that leaves
    anything readable behind. The UI says so before it calls this.

    What survives: the documents themselves, their uploaded originals and
    their chunks. Extraction under a schema is only one of two things a
    document produces (decision #15) - the chunks are the other, and they
    keep answering questions on the Ask page after the table is gone. The
    documents are merely detached (`schema_id` and `schema_version_id` back
    to NULL), so they can be re-extracted under a different schema later.
    """
    schema = db.get(SchemaDef, schema_id)
    if schema is None:
        raise HTTPException(404, "schema not found")

    view = view_name_for(schema.name)
    version_ids = [version.id for version in schema.versions]

    deleted_records = db.query(ExtractedRecord).filter(ExtractedRecord.schema_id == schema_id).count()

    # `extracted_records.is_duplicate_of` is a self-referential FK, so any
    # pointer *into* the doomed set has to go first - including from records
    # of another schema. Dedup only ever matches within one lineage, so that
    # second case should not arise; it is one statement to rule out rather
    # than an integrity error discovered in production.
    doomed = select(ExtractedRecord.id).where(ExtractedRecord.schema_id == schema_id).scalar_subquery()
    db.query(ExtractedRecord).filter(ExtractedRecord.is_duplicate_of.in_(doomed)).update(
        {"is_duplicate_of": None}, synchronize_session=False
    )
    db.query(ExtractedRecord).filter(ExtractedRecord.schema_id == schema_id).delete(synchronize_session=False)

    detached = (
        db.query(Document)
        .filter(Document.schema_id == schema_id)
        .update({"schema_id": None, "schema_version_id": None}, synchronize_session=False)
    )
    # A document can point at one of this lineage's versions while its
    # `schema_id` was already cleared (a detached-then-re-detached document,
    # or a half-applied backfill). The version FK would still block the
    # delete, so clear it on its own terms too.
    if version_ids:
        db.query(Document).filter(Document.schema_version_id.in_(version_ids)).update(
            {"schema_version_id": None}, synchronize_session=False
        )

    # The per-batch upload hint is an FK as well.
    db.query(Batch).filter(Batch.default_schema_id == schema_id).update(
        {"default_schema_id": None}, synchronize_session=False
    )

    cleared_jobs = _clear_schema_from_staged_jobs(db, schema_id, version_ids)

    # Through the relationship, not a bulk DELETE: `schema.versions` is
    # already loaded (the version ids above came from it), and a bulk delete
    # with `synchronize_session=False` leaves those instances in the session.
    # `db.delete(schema)` then cascades onto them and SQLAlchemy raises
    # StaleDataError trying to null the FK on rows that are already gone.
    for version in list(schema.versions):
        db.delete(version)
    db.flush()

    # The view is derived from the lineage, so it goes with it. Dropped after
    # the rows rather than before: the drop takes an AccessExclusiveLock, and
    # holding it across the deletes would block every reader for the whole
    # transaction instead of just the end of it.
    db.execute(text(f"DROP VIEW IF EXISTS {view}"))

    db.delete(schema)
    db.commit()

    return SchemaDeleteResponse(
        deleted_record_count=deleted_records,
        deleted_version_count=len(version_ids),
        detached_document_count=detached or 0,
        cleared_job_count=cleared_jobs,
        dropped_view=view,
    )


@router.delete("/schemas/{schema_id}/records/{record_id}", response_model=RecordDeleteResponse)
def delete_record(
    schema_id: uuid.UUID, record_id: uuid.UUID, db: Session = Depends(get_db)
) -> RecordDeleteResponse:
    """Deletes one row out of a schema's table.

    Scoped by `schema_id` rather than taking the record id alone: the caller
    is looking at one schema's table, and a record id from another lineage
    reaching this path means the client has its wires crossed - better a 404
    than a silent cross-table delete.

    The document keeps its other rows. A row-scoped schema projects many
    records per document, and deleting one bad line item is exactly what this
    is for, so nothing here touches the document, its chunks or its job
    history.
    """
    record = db.get(ExtractedRecord, record_id)
    if record is None or record.schema_id != schema_id:
        raise HTTPException(404, "record not found")

    # Another row may be marked as a duplicate *of* this one. Dropping the
    # pointer keeps that row - it is real extracted data whose only crime was
    # resembling the row being deleted.
    db.query(ExtractedRecord).filter(ExtractedRecord.is_duplicate_of == record_id).update(
        {"is_duplicate_of": None}, synchronize_session=False
    )
    db.delete(record)
    db.commit()
    return RecordDeleteResponse(deleted_record_id=record_id)


@router.get("/schemas/{schema_id}/records", response_model=RecordPage)
def list_records(
    schema_id: uuid.UUID, request: Request, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)
) -> RecordPage:
    """Direct table browsing straight from `view_<schema_name>` (closes the
    gap noted in plan section 5) - filterable by any of the schema's own
    column keys via query params, independent of the NL query page.

    Filters are keyed by column key, not field name: a name is ambiguous
    across a lineage (two fields can have held the same name at different
    versions) whereas a key identifies exactly one column. It also closes a
    live hole - a field named `created_at` used to pass the name allowlist and
    then filter the view's *bookkeeping* column.

    Returns `{rows, total}` rather than a bare list. Without the total the
    table's pager could not know where the data ended: "Next" stayed enabled
    forever, and clicking it past the last page rendered the empty state *and
    hid the pager*, stranding the user on a blank page with no way back.
    """
    schema = db.get(SchemaDef, schema_id)
    if schema is None:
        raise HTTPException(404, "schema not found")

    known_columns = {
        f["column"]
        for version in schema.versions
        for f in (version.fields or [])
        if f.get("column")
    }
    filters = {
        k: v
        for k, v in request.query_params.items()
        if k not in ("limit", "offset") and k in known_columns
    }

    params: dict = {"limit": limit, "offset": offset}
    where_sql = ""
    if filters:
        clauses = []
        for i, (key, value) in enumerate(filters.items()):
            # Allowlisted above; the regex is defence in depth on a value that
            # is interpolated as an identifier.
            if not COLUMN_KEY_RE.fullmatch(key):
                raise HTTPException(400, f"{key!r} is not a valid column key")
            param_name = f"f{i}"
            clauses.append(f"{key} = :{param_name}")
            params[param_name] = value
        where_sql = "WHERE " + " AND ".join(clauses)

    view = view_name_for(schema.name)
    total = db.execute(text(f"SELECT count(*) FROM {view} {where_sql}"), params).scalar() or 0
    # `created_at` alone is not a total order, and a partial order makes
    # LIMIT/OFFSET paging genuinely unsound: Postgres may return tied rows in
    # any order per query, so a row can appear on two pages or on none. The
    # tie used to be rare (one record per document, inserted one confirm at a
    # time); a document that now confirms N line items in a single transaction
    # gives all N the *same* `created_at`, so it is the common case. Breaking
    # the tie on `(document_id, row_index)` also happens to be the order a
    # reader wants: line items grouped by document, in document order.
    rows = db.execute(
        text(
            f"SELECT * FROM {view} {where_sql} "
            "ORDER BY created_at DESC, document_id, row_index LIMIT :limit OFFSET :offset"
        ),
        params,
    ).mappings().all()
    # `topic_ids` is a scoping column for the query engine, not part of a
    # record - topic membership belongs to the document.
    return RecordPage(
        rows=[{k: v for k, v in r.items() if k != "topic_ids"} for r in rows],
        total=total,
    )
