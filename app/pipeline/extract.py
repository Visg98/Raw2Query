"""Per-document extraction pipeline (plan section 3, steps 1-8).

Run by a worker after it claims a `pending` job. Writes everything into
`job.result` / `job.progress` and leaves `job.status = "awaiting_review"` -
nothing touches the real tables here (decisions #12, #19); the caller
commits the session.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from itertools import zip_longest
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from unstructured.chunking.title import chunk_by_title
from unstructured.documents.elements import Table
from unstructured.partition.auto import partition

from app.config import get_settings
from app.models import Document, ExtractedRecord, Job, SchemaDef, SchemaVersion, Topic
from app.pipeline.coerce import coerce_extracted_values
from app.pipeline.embeddings import embed_texts
from app.pipeline.llm import llm_classify, llm_classify_multi, llm_extract
from app.schema_fields import FieldSpec, field_specs, name_at, split_by_scope

logger = logging.getLogger(__name__)

# Heuristic context-limit threshold for one-shot extraction (step 4's fallback).
# Configurable, and set high on purpose: every chunk below this threshold
# becomes its own *request*, and requests are what the provider meters (see
# app/pipeline/ratelimit.py), so a lower value costs throughput and buys
# nothing. Read through a function rather than bound at import time so a
# changed setting does not need a module reload.
def _max_extract_chars() -> int:
    return get_settings().max_extract_chars


# There is deliberately no separate, smaller cap for the classify calls. One
# existed while the provider metered tokens; now that requests are what is
# metered, trimming their prompts buys nothing and costs match quality, so they
# get the whole document.


def _elements_text(elements: list) -> str:
    parts = []
    for el in elements:
        if isinstance(el, Table):
            html = getattr(el.metadata, "text_as_html", None)
            parts.append(html or str(el))
        else:
            parts.append(str(el))
    return "\n\n".join(parts)


def _progress(job: Job, step: str, pct: int) -> None:
    # Merge rather than replace: `progress` also carries the worker's
    # rate-limit attempt counter (app/worker.py), and replacing the dict here
    # reset it on every re-run - which would have made a requeued job retry
    # forever instead of giving up after MAX_RATE_LIMIT_ATTEMPTS.
    progress = dict(job.progress or {})
    progress.update({"step": step, "pct": pct})
    job.progress = progress


def _values_and_confidence(names: list[str]) -> dict[str, Any]:
    """The `{values, confidence}` pair, which both the document-level object
    and each repeating row use."""
    return {
        "type": "object",
        "properties": {
            "values": {
                "type": "object",
                "properties": {name: {"type": ["string", "null"]} for name in names},
                "required": names,
                "additionalProperties": False,
            },
            "confidence": {
                "type": "object",
                "properties": {name: {"type": "number"} for name in names},
                "required": names,
                "additionalProperties": False,
            },
        },
        "required": ["values", "confidence"],
        "additionalProperties": False,
    }


def _build_extraction_json_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """The response schema for step 4.

    A schema declaring no row-scoped field gets *exactly* the shape this
    returned before scopes existed - a bare top-level `{values, confidence}`.
    That is not cosmetic: it keeps the prompt, the response shape and hence
    the extraction quality of every existing schema bit-for-bit unchanged, so
    this feature cannot regress documents that never opt into it.

    Only when row-scoped fields exist does the shape grow a `rows` array, and
    then the document-level fields move under `document` so the model is not
    asked to decide whether "Invoice Number" belongs to the header or to each
    line.
    """
    document_fields, row_fields = split_by_scope(fields)
    if not row_fields:
        return _values_and_confidence([f["name"] for f in fields])
    return {
        "type": "object",
        "properties": {
            "document": _values_and_confidence([f["name"] for f in document_fields]),
            "rows": {
                "type": "array",
                "items": _values_and_confidence([f["name"] for f in row_fields]),
            },
        },
        "required": ["document", "rows"],
        "additionalProperties": False,
    }


@dataclass
class Extraction:
    """The shape of ONE LLM extraction response - internal to this module.

    Nothing outside `extract.py` sees it: `flatten_extraction` denormalizes it
    into the staged payload (`job.result["extracted_data"]`, a flat list of
    objects) before it is written, so no consumer has to know that the model
    was asked for the document half and the row half separately.

    `values`/`confidence` are the document-level fields. `rows`/
    `row_confidence` are the repeating rows, empty for a schema with no
    row-scoped fields, and each row holds ONLY its row-scoped fields.

    Keeping the two halves separate *here* is load-bearing, not vestigial: the
    long-document fallback in `_extract_fields` merges them by deliberately
    opposite rules, which a flat list cannot express (see the comment there).
    Flattening happens once, afterwards.
    """

    values: dict[str, Any]
    confidence: dict[str, float]
    rows: list[dict[str, Any]] = dataclass_field(default_factory=list)
    row_confidence: list[dict[str, float]] = dataclass_field(default_factory=list)


def _with_feedback(instructions: str, feedback: str | None) -> str:
    """Reviewer feedback from a re-extract, appended to the instructions.

    Appended, never prepended: the structural rules above it ("do not repeat
    the document-level values inside the rows", "do not invent rows") are what
    keep the response parseable, and a correction phrased as an instruction is
    exactly the kind of text that would talk the model out of them. Last-word
    position is for the schema's own rules, so the feedback sits between the
    preamble and them.

    This is reviewer-supplied text going into a prompt, and it is deliberately
    not sanitized beyond the delimiting. Its whole blast radius is one
    extraction whose output the same person then reviews field-by-field before
    it can be confirmed - the feedback cannot reach another document, another
    job, or the database.
    """
    if not feedback:
        return instructions
    return (
        f"{instructions}\n\n"
        "--- REVIEWER FEEDBACK ---\n"
        "A reviewer read a previous extraction of this same document and found it wrong. "
        "Apply their correction, while still obeying every rule above:\n"
        f"{feedback}\n"
        "--- END REVIEWER FEEDBACK ---"
    )


def _instructions_for(fields: list[dict[str, Any]], feedback: str | None = None) -> str:
    document_fields, row_fields = split_by_scope(fields)

    def describe(group: list[dict[str, Any]]) -> str:
        return "\n".join(f"- {f['name']} ({f['type']}): {f.get('description', '')}" for f in group)

    if not row_fields:
        return _with_feedback(
            f"Extract the following fields from the document. For each field, also give a "
            f"confidence score between 0 and 1 reflecting how sure you are. Use null for fields "
            f"not present.\nFields:\n{describe(fields)}",
            feedback,
        )
    return _with_feedback(
        "This document contains both document-level values and a repeating set of rows (line "
        "items). Extract both. For every field, also give a confidence score between 0 and 1 "
        "reflecting how sure you are, and use null for fields not present.\n\n"
        "Return one `rows` entry per repeating row in the document, in the order the document "
        "lists them. Do not invent rows, do not merge two rows into one, and do not repeat the "
        "document-level values inside the rows - return an empty `rows` list if the document "
        "genuinely has no line items.\n\n"
        f"Document-level fields:\n{describe(document_fields)}\n\n"
        f"Per-row fields:\n{describe(row_fields)}",
        feedback,
    )


def _parse_extraction(fields: list[dict[str, Any]], raw: dict[str, Any]) -> Extraction:
    """One LLM response -> `Extraction`, normalizing every value on the way."""
    document_fields, row_fields = split_by_scope(fields)
    if not row_fields:
        return Extraction(
            values=coerce_extracted_values(fields, raw.get("values", {})),
            confidence=raw.get("confidence", {}) or {},
        )

    document = raw.get("document") or {}
    rows, row_confidence = [], []
    for row in raw.get("rows") or []:
        rows.append(coerce_extracted_values(row_fields, (row or {}).get("values", {})))
        row_confidence.append((row or {}).get("confidence", {}) or {})
    return Extraction(
        values=coerce_extracted_values(document_fields, document.get("values", {})),
        confidence=document.get("confidence", {}) or {},
        rows=rows,
        row_confidence=row_confidence,
    )


def _extract_fields(
    elements: list, full_text: str, fields: list[dict[str, Any]], feedback: str | None = None
) -> Extraction:
    """Structured extraction over the resolved schema's fields (step 4).
    `.metadata.text_as_html` is used for Table elements (via `_elements_text`)
    so column alignment survives instead of flattened `.text`.

    Values come back normalized toward their declared type
    (`coerce_extracted_values`): the model reports what the document says -
    "83,880.00", "9%" - which reads fine on review but is not castable to
    the `numeric`/`integer`/`boolean` column `view_<schema>` projects.
    """
    if not fields:
        return Extraction(values={}, confidence={})

    json_schema = _build_extraction_json_schema(fields)
    # One set of instructions for both paths below, so a re-extract's feedback
    # reaches the chunked fallback as well as the single-call case.
    base_instructions = _instructions_for(fields, feedback)

    if len(full_text) <= _max_extract_chars():
        result = llm_extract(instructions=base_instructions, text=full_text, json_schema=json_schema)
        return _parse_extraction(fields, result)

    # Context-limit fallback: reuse the same chunk_by_title splitter used for
    # RAG chunking (step 7) rather than inventing a second splitter, run the
    # same extraction call per chunk, then merge.
    #
    # The two halves merge by opposite rules, and using either rule for the
    # other half is silently wrong. Document-level fields are ONE value seen
    # from several angles, so they reconcile (last non-null wins, confidence
    # pessimistic). Rows are DISJOINT values that happen to be split across
    # chunks, so they concatenate - reconciling them field-by-field the way
    # this loop used to would have collapsed a 40-line invoice into a single
    # row holding whichever line happened to come last.
    #
    # `max_characters` is passed explicitly so no single chunk can exceed the
    # per-call size this function just decided was too big for one shot -
    # otherwise the fallback hands the model a chunk as large as the document
    # it was meant to split.
    chunks = chunk_by_title(elements, max_characters=_max_extract_chars())
    merged = Extraction(values={}, confidence={})
    for chunk in chunks:
        result = llm_extract(instructions=base_instructions, text=chunk.text, json_schema=json_schema)
        part = _parse_extraction(fields, result)
        for name, value in part.values.items():
            if value is None:
                continue
            new_conf = part.confidence.get(name, 0.0)
            if name in merged.values and merged.values[name] != value:
                # two chunks disagree - keep the last-non-null value but flag
                # the lower of the two confidences.
                new_conf = min(new_conf, merged.confidence.get(name, new_conf))
            merged.values[name] = value
            merged.confidence[name] = new_conf
        # A row whose every field came back null carries no information and is
        # almost always the model answering "no line items here" on a chunk
        # that is pure prose. Keeping it would add blank rows to the record set
        # in proportion to the document's length.
        for row, confidence in zip(part.rows, part.row_confidence, strict=False):
            if all(v is None for v in row.values()):
                continue
            merged.rows.append(row)
            merged.row_confidence.append(confidence)
    return merged


def flatten_extraction(
    extraction: Extraction, fields: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """One LLM response -> the staged payload: `(extracted_data, data_confidence)`.

    `extracted_data` is a flat list of objects, one per value-set the document
    yielded, each carrying EVERY field the schema declares - the document-level
    values denormalized onto each one. That is the shape the review screen
    renders as a table and the shape `extracted_records` stores, so it is the
    shape staged, rather than two half-payloads a reader has to recombine.

    The denormalization is the same one `confirm.materialize_record_data` used
    to do, moved one layer earlier. It costs redundancy: a reviewer editing a
    document-level value now has to change it in every object. The review
    table restores that with a document-scoped column fill, so the property
    lives where the reviewer's intent lives rather than in the payload's shape.

    `data_confidence` is positionally parallel - one score map per object,
    always the same length as `extracted_data`, so a reader never has to
    reconcile two differently-shaped confidence sources.

    A schema with no row-scoped fields, or one whose document genuinely had no
    line items, yields exactly ONE object. "No rows found" must not become "no
    object", or the document silently stops existing on review and in
    `view_<schema>`. Hence the branch keys off `fields`, never off `values`: a
    malformed response with empty `values` still has to produce its one object.
    """
    if not fields:
        return [], []
    if not extraction.rows:
        return [dict(extraction.values)], [dict(extraction.confidence)]
    # zip_longest, not strict zip: a model that returns 5 rows and 4
    # confidence maps is a cosmetic mismatch, and failing the whole job over
    # it would lose a good extraction. Every object still gets a confidence
    # entry, which is what makes the parallel-length contract hold.
    #
    # Row keys win over document keys, so a field the schema declares at row
    # scope is never shadowed by a same-named document-level value.
    return (
        [{**extraction.values, **(row or {})} for row in extraction.rows],
        [
            {**extraction.confidence, **(confidence or {})}
            for _, confidence in zip_longest(extraction.rows, extraction.row_confidence, fillvalue={})
        ],
    )


def _identity_values(extracted_data: list[dict[str, Any]], names: list[str]) -> dict[str, Any]:
    """The document-level identity values, read off a flat payload.

    Identity fields are always document-scoped (the schema endpoint rejects
    row-scoped ones), so they are denormalized identically onto every object
    and any object is representative. First non-null wins, which degenerates
    to `extracted_data[0][name]` in every pipeline-produced payload.

    Objects *can* disagree once a reviewer has edited them, or once a client
    has PATCHed the payload directly - a case the old non-redundant shape made
    structurally impossible. It is logged rather than enforced: rejecting it
    would mean teaching the flat payload about scopes again, and dedup runs
    before review, so in practice this only fires on a hand-built payload.
    """
    identity: dict[str, Any] = {}
    for name in names:
        for obj in extracted_data:
            value = (obj or {}).get(name)
            if value is None:
                continue
            if name in identity and identity[name] != value:
                logger.warning(
                    "extracted objects disagree on identity field %s (%r vs %r); using the first",
                    name,
                    identity[name],
                    value,
                )
                continue
            identity.setdefault(name, value)
    return identity


def _find_dedup_match(
    session: Session,
    schema_id: uuid.UUID,
    identity_fields: list[str],
    extracted_data: list[dict[str, Any]],
    specs_by_column: dict[str, FieldSpec] | None = None,
) -> ExtractedRecord | None:
    """Decision #2: post-extraction business dedup via schema-declared identity fields.

    `identity_fields` holds column keys. `extracted_records.data` is still
    keyed by field *name*, and a renamed field means older rows store the value
    under the old name - so a key whose name has changed has to be compared
    against every name it ever had, scoped to the versions that used each one.
    The unambiguous case (never renamed, never shadowed, always declared)
    degenerates to exactly the query this used to run, which is what keeps
    behaviour identical for schemas nobody has renamed.
    """
    specs_by_column = specs_by_column or {}
    identity = _identity_values(
        extracted_data,
        [specs_by_column[key].name for key in identity_fields if key in specs_by_column],
    )
    query = session.query(ExtractedRecord).filter(ExtractedRecord.schema_id == schema_id)
    for key in identity_fields:
        spec = specs_by_column.get(key)
        if spec is None:
            # An identity key no field declares any more. Refusing to match is
            # right (a partial identity isn't an identity), but it used to
            # happen silently on every rename, turning dedup into a permanent
            # no-op that looked like "no duplicates found".
            logger.warning("identity column %s is not declared by any version; skipping dedup", key)
            return None
        value = identity.get(spec.name)
        if value is None:
            return None  # can't do an identity match without all identity fields present
        if spec.plain:
            query = query.filter(ExtractedRecord.data[spec.name].astext == str(value))
        else:
            query = query.filter(
                or_(
                    *[
                        and_(
                            ExtractedRecord.schema_version_id.in_(version_ids),
                            ExtractedRecord.data[name].astext == str(value),
                        )
                        for name, version_ids in spec.branches
                    ]
                )
            )
    return query.first()


def run_pipeline(session: Session, job: Job) -> None:
    document: Document = job.document

    _progress(job, "partitioning", 5)
    elements = partition(filename=document.storage_path)
    full_text = _elements_text(elements)

    result: dict[str, Any] = {
        "matched_schema_id": None,
        "schema_version_id": None,
        "proposed_schema_fields": None,
        # The table this document extracts to: one object per value-set, each
        # carrying every declared field (see `flatten_extraction`). Stays `[]`
        # only when no schema resolved and nothing was inferred - a resolved
        # schema always produces at least one object.
        "extracted_data": [],
        # Positionally parallel to `extracted_data`, one score map per object.
        "data_confidence": [],
        "dedup_match": None,
        "suggested_topic_ids": [],
        "proposed_new_topics": [],
        "confirmed_new_topic_names": [],
        "chunks": [],
    }

    schema_def: SchemaDef | None = None
    schema_version: SchemaVersion | None = None

    # Steps 2 and 6, classified together (decisions #10/#11).
    #
    # Schema match and topic suggestion are independent judgements, but they ask
    # about the *same* text, and the provider meters requests rather than
    # tokens (see app/pipeline/ratelimit.py). Sent separately they cost two
    # requests and transmit the document twice; merged they cost one. That is
    # the difference between ~5 and ~7.5 documents a minute, so the topic
    # classification happens here rather than at step 6 even though its result
    # is not consumed until then.
    #
    # Either half can be unnecessary - a schema picked at upload, or a batch
    # that already hinted its topics - so the task list is assembled first and
    # the call is skipped entirely when nothing is left to ask.
    _progress(job, "schema_match", 15)

    hinted_topic_ids: set[str] = set()
    if document.batch is not None and document.batch.default_topic_ids:
        hinted_topic_ids = {str(t) for t in document.batch.default_topic_ids}

    tasks: list[dict[str, Any]] = []

    if document.schema_id is not None:
        schema_def = session.get(SchemaDef, document.schema_id)
        schema_version = document.schema_version or (
            schema_def.versions[-1] if schema_def and schema_def.versions else None
        )
    else:
        schemas = session.query(SchemaDef).all()
        if schemas:
            tasks.append(
                {
                    "key": "schema",
                    "instructions": "Which existing schema (document type) does this document match, if any?",
                    "options": [
                        {
                            "id": str(s.id),
                            "name": s.name,
                            "description": ", ".join(
                                f.get("name", "") for f in (s.versions[-1].fields if s.versions else [])
                            ),
                        }
                        for s in schemas
                    ],
                    "allow_new": False,
                }
            )

    # A batch-level topic hint (decision #9) is already an answer, so asking the
    # model as well spends a request to be told what we know.
    topics = session.query(Topic).all() if not hinted_topic_ids else []
    if topics:
        tasks.append(
            {
                "key": "topics",
                "instructions": (
                    "Which existing topics clearly apply to this document? "
                    "Propose a new topic only if none fit."
                ),
                "options": [
                    {"id": str(t.id), "name": t.name, "description": t.description or ""} for t in topics
                ],
                "allow_new": True,
            }
        )

    classified: dict[str, dict[str, Any]] = {}
    if len(tasks) == 1:
        # One task does not need the multi-task prompt scaffolding, and the
        # single-task prompt is the one with years of behaviour behind it.
        only = tasks[0]
        classified[only["key"]] = llm_classify(
            instructions=only["instructions"],
            text=full_text,
            options=only["options"],
            allow_new=only["allow_new"],
        )
    elif tasks:
        classified = llm_classify_multi(tasks=tasks, text=full_text)

    selected = (classified.get("schema") or {}).get("selected_ids") or []
    if selected:
        schema_def = session.get(SchemaDef, uuid.UUID(selected[0]))
        schema_version = schema_def.versions[-1] if schema_def and schema_def.versions else None

    # Step 3: ad hoc schema inference (decision #14) - only if step 2 found no match.
    if schema_version is None:
        _progress(job, "ad_hoc_schema_inference", 30)
        inference_schema = {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": ["string", "number", "integer", "boolean", "date", "datetime"],
                            },
                            "description": {"type": "string"},
                            "required": {"type": "boolean"},
                        },
                        "required": ["name", "type", "description", "required"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["fields"],
            "additionalProperties": False,
        }
        inferred = llm_extract(
            instructions=(
                "Propose a structured schema (field names, types, short descriptions) that "
                "captures the key structured data in this document."
            ),
            # Capped at the same threshold as extraction: proposing a schema
            # from scratch needs the document's breadth, and this call only
            # runs when no existing schema matched.
            text=full_text[:_max_extract_chars()],
            json_schema=inference_schema,
            schema_name="proposed_schema",
        )
        result["proposed_schema_fields"] = inferred.get("fields", [])

    # Step 4: structured extraction against whichever schema resolved.
    _progress(job, "extraction", 50)
    fields_for_extraction = schema_version.fields if schema_version else (result["proposed_schema_fields"] or [])
    extraction = _extract_fields(elements, full_text, fields_for_extraction)
    extracted, confidence = flatten_extraction(extraction, fields_for_extraction)
    result["extracted_data"] = extracted
    result["data_confidence"] = confidence

    if schema_def is not None and schema_version is not None:
        result["matched_schema_id"] = str(schema_def.id)
        result["schema_version_id"] = str(schema_version.id)

    # Step 5: dedup check (decision #2) - only if the schema declares identity_fields.
    if schema_version is not None and schema_version.identity_fields:
        _progress(job, "dedup_check", 60)
        lineage = (
            session.query(SchemaVersion)
            .filter(SchemaVersion.schema_id == schema_version.schema_id)
            .order_by(SchemaVersion.version.asc())
            .all()
        )
        specs_by_column = {
            s.column: s for s in field_specs([(v.id, v.fields or []) for v in lineage])
        }
        match = _find_dedup_match(
            session,
            schema_version.schema_id,
            schema_version.identity_fields,
            extracted,
            specs_by_column,
        )
        if match is not None:
            # Read under the name the *matched* row's own version used, not
            # the current one - otherwise every value on the review screen's
            # dedup banner reads as None after a rename.
            result["dedup_match"] = {
                "extracted_record_id": str(match.id),
                "matched_fields": {
                    specs_by_column[key].name: match.data.get(
                        name_at(specs_by_column[key], match.schema_version_id) or ""
                    )
                    for key in schema_version.identity_fields
                    if key in specs_by_column
                },
            }

    # Step 6: topic suggestion (decision #10). A batch-level topic hint
    # (decision #9) seeds the suggestion; it's still just a pre-filled,
    # removable chip on review, never silently final.
    #
    # The classification itself already happened, merged into step 2's call -
    # see the comment there for why. This step only resolves the result, and
    # `hinted_topic_ids` still unions in so a batch hint survives a document
    # the model found nothing for.
    _progress(job, "topic_suggestion", 70)
    topic_classification = classified.get("topics") or {}
    suggested_ids = hinted_topic_ids | set(topic_classification.get("selected_ids") or [])
    result["suggested_topic_ids"] = sorted(suggested_ids)
    if topic_classification.get("new_proposal"):
        result["proposed_new_topics"] = [topic_classification["new_proposal"]]

    # Step 7: chunk + embed - unconditional, independent of steps 2-6 (decision #15).
    _progress(job, "chunking", 85)
    chunks = chunk_by_title(elements)
    chunk_texts = [c.text for c in chunks]
    embeddings = embed_texts(chunk_texts)
    result["chunks"] = [
        {
            "content": chunk_text,
            "embedding": embedding,
            "metadata": {
                "page_number": getattr(chunk.metadata, "page_number", None),
                "element_types": sorted(
                    {type(el).__name__ for el in (getattr(chunk.metadata, "orig_elements", None) or [])}
                ),
            },
        }
        for chunk, chunk_text, embedding in zip(chunks, chunk_texts, embeddings, strict=True)
    ]

    # Step 8: hand the staged result back; caller commits.
    _progress(job, "done", 100)
    job.result = result
    job.status = "awaiting_review"


def run_backfill_pipeline(session: Session, job: Job) -> None:
    """Backfill entry point (section 4): a breaking schema edit re-extracts
    existing documents under the new version. Skips schema-match/ad-hoc
    inference/topic suggestion/chunking - only step 4 (structured extraction)
    re-runs, against `document.schema_version_id`, which the backfill enqueue
    endpoint already points at the new version.
    """
    document: Document = job.document
    schema_version = document.schema_version
    if schema_version is None:
        raise ValueError("backfill job's document has no target schema_version_id set")

    _progress(job, "partitioning", 20)
    elements = partition(filename=document.storage_path)
    full_text = _elements_text(elements)

    _progress(job, "extraction", 70)
    extraction = _extract_fields(elements, full_text, schema_version.fields)
    extracted, confidence = flatten_extraction(extraction, schema_version.fields)

    _progress(job, "done", 100)
    job.result = {
        "matched_schema_id": str(schema_version.schema_id),
        "schema_version_id": str(schema_version.id),
        "proposed_schema_fields": None,
        "extracted_data": extracted,
        "data_confidence": confidence,
        "dedup_match": None,
        "suggested_topic_ids": [],
        "proposed_new_topics": [],
        "chunks": [],
    }
    job.status = "awaiting_review"


def _reextract_fields(session: Session, result: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields a re-extract should run against.

    Read out of `job.result`, not off `document.schema_version` the way
    backfill does. The reviewer may have re-pointed this job at a different
    schema via the review screen's schema switcher since it was extracted, and
    that choice only exists in the staged payload until confirm writes it to
    the document - so reading the document would silently re-extract against
    the schema they just rejected.
    """
    version_id = result.get("schema_version_id")
    if version_id:
        version = session.get(SchemaVersion, uuid.UUID(version_id))
        if version is not None:
            return version.fields or []
    return result.get("proposed_schema_fields") or []


def run_reextract_pipeline(session: Session, job: Job) -> None:
    """Re-extract entry point: step 4 again, with the reviewer's feedback.

    Like backfill, only structured extraction re-runs - but this one MERGES
    into the existing `job.result` instead of replacing it. Everything the
    first run established about this document still holds: the chunks are
    deterministic from the same file (re-chunking would spend the embedding
    cost to produce identical rows - the one cost that scales with corpus
    size), and the suggested topics, dedup match and schema choice are either
    unchanged or were chosen by the reviewer since. Only the table is wrong,
    so only the table is rebuilt.
    """
    document: Document = job.document
    result: dict[str, Any] = dict(job.result or {})
    feedback = result.get("reextract_feedback")
    fields = _reextract_fields(session, result)

    _progress(job, "partitioning", 20)
    elements = partition(filename=document.storage_path)
    full_text = _elements_text(elements)

    _progress(job, "extraction", 70)
    extraction = _extract_fields(elements, full_text, fields, feedback)
    extracted, confidence = flatten_extraction(extraction, fields)
    result["extracted_data"] = extracted
    result["data_confidence"] = confidence
    # Cleared so it is applied exactly once. `reextract_history` keeps the
    # record of what was asked for; this key only means "pending".
    result.pop("reextract_feedback", None)

    _progress(job, "done", 100)
    job.result = result
    job.status = "awaiting_review"
