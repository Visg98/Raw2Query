"""Query-page routing (decisions #18, #20): one layer per incoming question
decides scoping (explicit topics, else auto-detected) then NL-to-SQL vs RAG.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.models import DocumentTopic, Topic
from app.pipeline.llm import llm_classify, llm_extract
from app.query.nl_to_sql import (
    UnsafeSqlError,
    allowed_view_names,
    build_catalog,
    column_labels,
    compose_sql_answer,
    document_scope,
    generate_sql,
    relabel_rows,
    run_safe_select,
)
from app.query.rag import compose_answer, hybrid_search

logger = logging.getLogger(__name__)


def resolve_topic_ids(
    session: Session, question: str, explicit_topic_ids: list[uuid.UUID] | None
) -> tuple[list[uuid.UUID], bool]:
    """Decision #6: explicit selection always wins and narrows the search.
    Decision #20: with no explicit selection, ask the LLM which topic(s) (by
    name + description) the question is most likely about, rather than
    searching unscoped by default.

    Returns `(topic_ids, auto_detected)`. Callers need to know which of the
    two it was: an explicit selection is the user's deliberate narrowing and
    is honoured even when it matches nothing, but an auto-detected guess that
    matches nothing must not be allowed to turn an answerable question into
    "no sources found" (see `_relax_scope` use below).
    """
    if explicit_topic_ids:
        return list(explicit_topic_ids), False

    topics = session.query(Topic).all()
    if not topics:
        return [], False
    options = [{"id": str(t.id), "name": t.name, "description": t.description or ""} for t in topics]
    result = llm_classify(
        instructions="Which topic(s) does this question relate to most closely, if any?",
        text=question,
        options=options,
        allow_new=False,
    )
    return [uuid.UUID(i) for i in (result.get("selected_ids") or [])], True


def _documents_in_topics(session: Session, topic_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if not topic_ids:
        return []
    rows = (
        session.query(DocumentTopic.document_id)
        .filter(DocumentTopic.topic_id.in_(topic_ids))
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"routing": {"type": "string", "enum": ["sql", "rag"]}},
    "required": ["routing"],
    "additionalProperties": False,
}


def decide_routing(question: str, catalog: list[dict]) -> str:
    # Human field names, never the column keys. This call is choosing between
    # branches on the strength of what the schemas are *about*, so a list of
    # opaque `f_xxxxxxxx` tokens would describe nothing and push every
    # structured question to RAG - a worse failure than the mis-advertised
    # column names that motivated keying the catalog in the first place.
    catalog_block = (
        "\n".join(
            f"- {c['schema_name']}: {', '.join(col['name'] for col in c['columns'])}"
            for c in catalog
        )
        or "(no structured schemas defined yet)"
    )
    instructions = (
        "Decide whether this question is best answered by a structured SQL query over the "
        "tabular schemas below (routing=sql), or by searching document text (routing=rag). "
        "Prefer sql when the question asks for specific fields, aggregates, filters, or counts "
        "that the schemas below cover; prefer rag for open-ended or contextual questions.\n\n"
        f"Available structured schemas:\n{catalog_block}"
    )
    result = llm_extract(instructions=instructions, text=question, json_schema=ROUTE_SCHEMA, schema_name="route_decision")
    return result["routing"]


def answer_query(session: Session, question: str, explicit_topic_ids: list[uuid.UUID] | None) -> dict:
    topic_ids, auto_detected = resolve_topic_ids(session, question, explicit_topic_ids)
    catalog = build_catalog(session)
    routing = decide_routing(question, catalog) if catalog else "rag"

    if routing == "sql" and catalog:
        answered = _answer_with_sql(session, question, catalog, topic_ids, auto_detected)
        if answered is not None:
            return answered

    return _answer_with_rag(session, question, topic_ids, auto_detected)


def _answer_with_sql(
    session: Session,
    question: str,
    catalog: list[dict],
    topic_ids: list[uuid.UUID],
    auto_detected: bool,
) -> dict | None:
    """The structured branch. Returns None to hand the question to RAG - a
    rejected or broken query, or no catalog match at all."""
    allowed = allowed_view_names(catalog)
    scope_ids = _documents_in_topics(session, topic_ids)

    # Scope candidates, tried in order. The unscoped retry only exists for an
    # auto-detected scope: our own guess must never be the reason a question
    # comes back empty. An explicit selection is left exactly as the user set
    # it (decision #6).
    candidates: list[tuple[list[uuid.UUID], list[uuid.UUID] | None]] = []
    if topic_ids and scope_ids:
        candidates.append((topic_ids, scope_ids))
    if not topic_ids or auto_detected:
        candidates.append(([], None))

    # Generated once, outside the loop. Scope is injected into the parsed
    # statement per candidate rather than prompted for, so the ladder reuses
    # one statement under two scopes instead of spending a second LLM call
    # and getting an unrelated second draft.
    sql = generate_sql(question, catalog)
    labels = column_labels(catalog)

    for index, (used_topic_ids, scope_document_ids) in enumerate(candidates):
        is_last = index == len(candidates) - 1
        scope = document_scope(scope_document_ids) if scope_document_ids else None
        try:
            safe_sql, rows = run_safe_select(sql, allowed, scope=scope)
        except UnsafeSqlError as exc:
            # Never surface or execute a rejected query - hand it to RAG.
            logger.warning("rejected generated SQL (%s): %s", exc, sql)
            return None
        except Exception:
            # The SQL passed the safety rail (it's a genuine single SELECT)
            # but still doesn't run - a hallucinated column, a type mismatch.
            # That's a bad query, not an unsafe one; the user still gets an
            # answer via RAG instead of a 500. Logged (not swallowed
            # silently) since it usually means the catalog/prompt needs work.
            logger.exception("generated SQL failed to execute, falling back to RAG: %s", sql)
            return None

        if not rows and not is_last:
            logger.info("topic-scoped SQL matched no rows, retrying unscoped")
            continue

        # Relabelled once, at the boundary, so the composer and the UI both
        # see human column names instead of opaque keys.
        labelled = relabel_rows(rows, labels)
        return {
            "answer": compose_sql_answer(question, safe_sql, labelled),
            "routing_used": "sql",
            "sql": safe_sql,
            "rows": labelled,
            "sources": None,
            "topic_ids_used": used_topic_ids,
        }

    return None


def _answer_with_rag(
    session: Session, question: str, topic_ids: list[uuid.UUID], auto_detected: bool
) -> dict:
    chunks = hybrid_search(session, question, topic_ids)
    used_topic_ids = topic_ids

    # Same guard as the SQL branch. An auto-detected topic that happens to
    # hold none of the matching chunks used to answer every question with
    # "there are no sources available" while the documents sat right there
    # under a different topic.
    if not chunks and topic_ids and auto_detected:
        logger.info("auto-detected topic scope matched no chunks, retrying unscoped")
        chunks = hybrid_search(session, question, None)
        used_topic_ids = []

    answer = compose_answer(question, chunks)
    sources = [
        {
            "document_id": c["document_id"],
            "chunk_id": c["id"],
            "snippet": c["content"][:280],
            "filename": c.get("filename"),
            # `chunks.metadata->>'page_number'` comes back as text.
            "page_number": int(c["page_number"]) if (c.get("page_number") or "").isdigit() else None,
        }
        for c in chunks
    ]
    return {
        "answer": answer,
        "routing_used": "rag",
        "sql": None,
        "rows": None,
        "sources": sources,
        "topic_ids_used": used_topic_ids,
    }
