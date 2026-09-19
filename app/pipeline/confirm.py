"""Confirm flow (decisions #12, #19): commits jobs.result - the pending/
staging payload - into the real tables in one transaction. Nothing before
this point ever touches `extracted_records`, `document_topics`, or `chunks`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import (
    Chunk,
    Document,
    DocumentTopic,
    ExtractedRecord,
    Job,
    SchemaDef,
    SchemaVersion,
    Topic,
)
from app.pipeline.coerce import coerce_extracted_values
from app.pipeline.topics_util import get_or_create_topic, get_or_create_uncategorized
from app.pipeline.views import regenerate_schema_view
from app.schema_fields import ColumnKeyError, assign_column_keys
from app.schemas_pydantic import JobConfirmRequest


class ConfirmError(ValueError):
    """Raised for any confirm-time validation failure; routers translate this
    into a 400 response."""


def is_job_clean(job: Job, confidence_threshold: float) -> bool:
    """Decision #13: eligible for bulk "confirm all clean" only if no field
    came back low-confidence.

    An identity-field dedup match no longer disqualifies a job: duplicates
    are acceptable now, so a match is information shown on review (the
    reviewer can still explicitly skip/replace), not a blocker.

    Every extracted object's scores are read, not just the first. A row-scoped
    schema puts most of its numbers in the later objects, so scoring only
    object 0 would report a 40-line invoice as clean on the strength of its
    header - which is the one case bulk-confirm most needs to catch. That is
    also why there is exactly one confidence source: `data_confidence` runs
    parallel to `extracted_data`, so there is no second dict a reader can
    forget to check.

    A re-extract (`POST /jobs/{id}/reextract`) writes fresh scores, so a job
    the reviewer sent back becomes bulk-confirmable again on the strength of
    the new extraction rather than the one they rejected. That is intended.
    """
    result = job.result or {}
    groups = result.get("data_confidence") or []
    return all(
        value >= confidence_threshold
        for group in groups
        # Defensive: `job.result` is an untyped blob a client can PATCH, and a
        # non-numeric confidence here would otherwise raise TypeError inside
        # the bulk-confirm loop and fail the whole batch.
        for value in (group or {}).values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def materialize_record_data(fields: list[dict], extracted_data: list[dict] | None) -> list[dict]:
    """The staged payload -> one `data` dict per `extracted_records` row.

    The staged objects already carry the document-level values on every one of
    them (`extract.flatten_extraction` denormalizes them). That denormalization
    is the whole design: it keeps `view_<schema>` a single flat relation, which
    is the only shape the NL-to-SQL layer can query, since `build_catalog`
    advertises no relationships between views and the generated SQL therefore
    has no join key to use. Repeating a vendor name across twelve rows costs
    nothing next to teaching a model to join.

    So this is now a coercion pass, not a merge - but it still has to be one.
    Every value is normalized *here*, after the review screen, so a value
    hand-typed into the table ("1,250.00") is coerced exactly like an
    extracted one rather than reaching a `numeric` column as text.

    `or [{}]` is not defensive padding: a confirmed document must contribute
    at least one row to its view. "No objects" must not silently become "no
    record", or the document disappears from the view and only shows up later
    as a wrong SUM - and `_apply_extracted_record`'s replace branch indexes
    `record_data[0]` outright.
    """
    records = [coerce_extracted_values(fields, obj or {}) for obj in (extracted_data or [])]
    return records or [{}]


def confirm_job(session: Session, job: Job, body: JobConfirmRequest) -> None:
    if job.status != "awaiting_review":
        raise ConfirmError(f"job {job.id} is not awaiting review (status={job.status!r})")

    result = job.result or {}
    document: Document = job.document
    extracted_data = result.get("extracted_data") or []

    schema_id: uuid.UUID | None = None
    schema_version_id: uuid.UUID | None = None

    if result.get("proposed_schema_fields") and body.save_schema_as:
        # Ad hoc inference (decision #14), promoted to a reusable named schema.
        if session.query(SchemaDef).filter(SchemaDef.name == body.save_schema_as).first() is not None:
            raise ConfirmError(f"a schema named {body.save_schema_as!r} already exists")
        schema = SchemaDef(name=body.save_schema_as)
        session.add(schema)
        session.flush()
        # The only path that writes `fields` without passing through Pydantic,
        # so the column keys must be minted here explicitly. The lineage is
        # brand new, so any `column` the review screen echoed back is ignored
        # rather than trusted - `jobs.result` is an untyped blob a client can
        # PATCH freely.
        try:
            fields = assign_column_keys(result["proposed_schema_fields"])
        except ColumnKeyError as exc:
            raise ConfirmError(str(exc)) from exc
        version = SchemaVersion(
            schema_id=schema.id, version=1, fields=fields, is_breaking_from_prev=False
        )
        session.add(version)
        session.flush()
        regenerate_schema_view(session, schema.id)
        schema_id, schema_version_id = schema.id, version.id
    elif result.get("matched_schema_id"):
        schema_id = uuid.UUID(result["matched_schema_id"])
        # `.get`, not `[...]`: `jobs.result` is an untyped blob a client can
        # PATCH, so a match without a version is a 400, not a KeyError.
        raw_version_id = result.get("schema_version_id")
        if not raw_version_id:
            raise ConfirmError(
                "this document is matched to a schema but not to a schema version - "
                "re-pick the schema on the review screen"
            )
        schema_version_id = uuid.UUID(raw_version_id)
        # Checked here rather than left to the insert. Deleting a schema
        # clears the match out of every staged job (see
        # `_clear_schema_from_staged_jobs`), but the review screen's own
        # schema list is seeded at page load, so a schema deleted after that
        # can still be PATCHed back by Confirm. Without this, the stale id
        # reaches Postgres as a `documents_schema_id_fkey` violation - an
        # unhandled 500 the browser reports as a CORS error, rather than
        # something the reviewer can read and act on.
        if session.get(SchemaDef, schema_id) is None or session.get(SchemaVersion, schema_version_id) is None:
            raise ConfirmError(
                "the schema this document was matched to no longer exists - "
                "pick another schema on the review screen, or confirm without one"
            )
    # else: an ad hoc shape that wasn't saved as reusable, or no schema at
    # all - no ExtractedRecord is created. The document's content is still
    # fully captured through its chunks regardless (decision #15).

    if schema_id is not None and schema_version_id is not None:
        # Last chance to normalize before these become rows: values may have
        # been hand-edited on the review screen since extraction, and those
        # edits carry the same "1,250.00" problem as the model's output.
        target_version = session.get(SchemaVersion, schema_version_id)
        fields = target_version.fields if target_version is not None else []
        record_data = materialize_record_data(fields, extracted_data)

        if job.kind == "backfill":
            # A backfill re-extracts the *same* document under a new schema
            # version - it must update that document's own ExtractedRecord
            # in place, never insert a second one. Leaving the old row
            # behind would keep stale, old-type-formatted data around, and
            # `regenerate_schema_view` casts every row to the newest field
            # type - so a stale row's value that only parsed under the old
            # type (e.g. "1250.00" once retyped from number to integer)
            # breaks the view for everyone, not just this row.
            _apply_backfill_extracted_record(session, document, schema_id, schema_version_id, record_data)
        else:
            _apply_extracted_record(session, document, schema_id, schema_version_id, record_data, result.get("dedup_match"), body)
        document.schema_id = schema_id
        document.schema_version_id = schema_version_id

    # A backfill job only re-runs step 4 (structured extraction) - it never
    # touches topics or chunks, which already exist from the document's
    # original confirm.
    if job.kind != "backfill":
        topic_ids = _resolve_confirmed_topic_ids(session, result)
        existing_topic_ids = {link.topic_id for link in document.topic_links}
        for topic_id in topic_ids - existing_topic_ids:
            session.add(DocumentTopic(document_id=document.id, topic_id=topic_id))

        for index, chunk in enumerate(result.get("chunks") or []):
            session.add(
                Chunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=chunk["content"],
                    embedding=chunk["embedding"],
                    topic_ids=list(topic_ids),
                    chunk_metadata=chunk.get("metadata"),
                )
            )

    job.status = "confirmed"


def _resolve_confirmed_topic_ids(session: Session, result: dict) -> set[uuid.UUID]:
    """The topics a confirm should link, filtered to the ones that still exist.

    Deleting a topic clears it out of every staged job (see
    `_clear_topic_from_staged_jobs`), but the review screen's topic list is
    seeded at page load, so a topic deleted afterwards can still be PATCHed
    back by Confirm. An id that no longer resolves would reach Postgres as a
    `document_topics_topic_id_fkey` violation - an unhandled 500 that the
    browser reports as a CORS error on the review screen. Dropped instead:
    deleting the topic already meant the label no longer applies, and if that
    empties the set the document falls back to `Uncategorized` below, exactly
    as an untagged one does.
    """
    staged = {uuid.UUID(t) for t in (result.get("suggested_topic_ids") or [])}
    topic_ids = (
        {row[0] for row in session.query(Topic.id).filter(Topic.id.in_(staged))}
        if staged
        else set()
    )
    for name in result.get("confirmed_new_topic_names") or []:
        topic_ids.add(get_or_create_topic(session, name).id)
    if not topic_ids:
        topic_ids.add(get_or_create_uncategorized(session).id)  # decision #16
    return topic_ids


def _records_for(
    session: Session, document_id: uuid.UUID, schema_id: uuid.UUID
) -> list[ExtractedRecord]:
    """One document's record set for one schema, in document order."""
    return (
        session.query(ExtractedRecord)
        .filter(
            ExtractedRecord.document_id == document_id,
            ExtractedRecord.schema_id == schema_id,
        )
        .order_by(ExtractedRecord.row_index.asc())
        .all()
    )


def _delete_records(session: Session, records: list[ExtractedRecord]) -> None:
    """Deletes records, first detaching anything that points at them.

    `is_duplicate_of` is a self-FK, so deleting a record another one is
    cross-linked to raises ForeignKeyViolation. Nulling the pointer loses the
    "these two are duplicates" note, which is strictly better than refusing
    the confirm - and the record the pointer described is being deleted, so
    the note has nothing left to mean.
    """
    doomed = [r.id for r in records]
    if not doomed:
        return
    session.query(ExtractedRecord).filter(
        ExtractedRecord.is_duplicate_of.in_(doomed),
        ExtractedRecord.id.notin_(doomed),
    ).update({ExtractedRecord.is_duplicate_of: None}, synchronize_session="fetch")
    for record in records:
        session.delete(record)
    # The inserts that follow reuse `row_index` values these rows hold, and
    # the delete has to reach the database first if a unique index is ever
    # added over that prefix.
    session.flush()


def _insert_records(
    session: Session,
    document: Document,
    schema_id: uuid.UUID,
    schema_version_id: uuid.UUID,
    record_data: list[dict],
    *,
    is_duplicate_of: uuid.UUID | None = None,
    start_index: int = 0,
) -> None:
    """One `ExtractedRecord` per entry, indexed in document order."""
    for offset, data in enumerate(record_data):
        session.add(
            ExtractedRecord(
                document_id=document.id,
                schema_id=schema_id,
                schema_version_id=schema_version_id,
                row_index=start_index + offset,
                data=data,
                is_duplicate_of=is_duplicate_of,
            )
        )


def _apply_backfill_extracted_record(
    session: Session,
    document: Document,
    schema_id: uuid.UUID,
    schema_version_id: uuid.UUID,
    record_data: list[dict],
) -> None:
    """Replaces this document's whole record set for this schema.

    Delete-then-insert rather than the update-in-place this used to do. The
    in-place version located the document's record with `.first()`, which is
    structurally blind to row count: re-extracting a 12-line invoice as 9
    lines would have updated one row and left 11 stale ones behind, and
    `regenerate_schema_view` casts every row to the newest field type - so one
    stale row whose value only parsed under the old type nulls out a cell for
    everyone, which is exactly the breakage the in-place update existed to
    prevent. Replacing the set keeps that guarantee at any row count.

    The cost is that record ids are not preserved across a backfill. Nothing
    references them except `is_duplicate_of`, which `_delete_records` detaches.
    """
    _delete_records(session, _records_for(session, document.id, schema_id))
    _insert_records(session, document, schema_id, schema_version_id, record_data)


def _apply_extracted_record(
    session: Session,
    document: Document,
    schema_id: uuid.UUID,
    schema_version_id: uuid.UUID,
    record_data: list[dict],
    dedup_match: dict | None,
    body: JobConfirmRequest,
) -> None:
    if dedup_match is None:
        _insert_records(session, document, schema_id, schema_version_id, record_data)
        return

    # An identity-field match is surfaced on the review screen so the
    # reviewer *can* choose to skip or replace, but it no longer blocks the
    # confirm: with duplicates acceptable, the default is to keep both
    # (cross-linked via is_duplicate_of) rather than refuse to save.
    decision = body.dedup_decision or "keep_both"
    matched_id = uuid.UUID(dedup_match["extracted_record_id"])

    if decision == "skip":
        return  # discard the new extraction
    if decision == "keep_both":
        # Every row of the new set points at the matched record. The match is
        # on document-level identity fields (enforced when the schema is
        # saved), so what is duplicated is the document, not any one line -
        # so every line of it carries the same cross-link.
        _insert_records(
            session,
            document,
            schema_id,
            schema_version_id,
            record_data,
            is_duplicate_of=matched_id,
        )
        return
    if decision == "replace":
        existing = session.get(ExtractedRecord, matched_id)
        if existing is None:
            raise ConfirmError("matched record no longer exists")

        # Row 0 is updated in place so the matched record keeps its id, which
        # is what "replace" has always meant and what keeps this path
        # byte-identical for a single-row schema. The matched document's
        # remaining rows are deleted rather than left behind: they describe a
        # record set whose row 0 now belongs to a different document, so
        # keeping them would strand a partial set under the old document.
        stranded = [
            r
            for r in _records_for(session, existing.document_id, schema_id)
            if r.id != existing.id
        ]
        _delete_records(session, stranded)

        existing.document_id = document.id
        existing.schema_version_id = schema_version_id
        existing.row_index = 0
        existing.data = record_data[0]
        _insert_records(
            session,
            document,
            schema_id,
            schema_version_id,
            record_data[1:],
            start_index=1,
        )
        return
    raise ConfirmError(f"unknown dedup_decision {decision!r}")
