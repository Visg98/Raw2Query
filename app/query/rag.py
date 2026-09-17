"""Hybrid retrieval + RAG answer composition (plan section 3, call site 12;
decision #22's hybrid retrieval)."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.pipeline.embeddings import embed_text
from app.pipeline.llm import llm_extract

#  `d.filename` / `c.metadata->>'page_number'` are joined in so a source
#  card can name the document (and page) it came from - a card labelled with
#  a bare uuid tells the reader nothing about what they're being cited.
_HYBRID_SQL = text(
    """
    SELECT c.id, c.document_id, c.content,
           d.filename,
           (c.metadata->>'page_number') AS page_number,
           1 - (c.embedding <=> CAST(:vec AS vector)) AS vector_score,
           ts_rank(c.tsv, plainto_tsquery('english', :question)) AS text_score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE (CAST(:topic_array AS uuid[]) IS NULL OR c.topic_ids && CAST(:topic_array AS uuid[]))
    ORDER BY (1 - (c.embedding <=> CAST(:vec AS vector))) * 0.6
             + ts_rank(c.tsv, plainto_tsquery('english', :question)) * 0.4 DESC
    LIMIT :limit
    """
)


def hybrid_search(
    session: Session, question: str, topic_ids: list[uuid.UUID] | None, limit: int = 8
) -> list[dict]:
    """Combines pgvector cosine similarity with Postgres full-text search,
    scoped by the denormalized `chunks.topic_ids` (decision #7's fast path).
    """
    vec = embed_text(question)
    vec_literal = "[" + ",".join(f"{x:.8f}" for x in vec) + "]"
    topic_array = "{" + ",".join(str(t) for t in topic_ids) + "}" if topic_ids else None
    rows = session.execute(
        _HYBRID_SQL, {"vec": vec_literal, "question": question, "topic_array": topic_array, "limit": limit}
    ).mappings().all()
    return [dict(r) for r in rows]


COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def compose_answer(question: str, chunks: list[dict]) -> str:
    context = "\n\n".join(
        f"[source {i + 1} | document {c['document_id']} | chunk {c['id']}]\n{c['content']}"
        for i, c in enumerate(chunks)
    )
    instructions = (
        "Answer the question using only the sources below, citing them inline as [source N]. "
        "If the sources don't contain the answer, say so plainly rather than guessing.\n\n"
        f"Sources:\n{context or '(no matching sources found)'}"
    )
    result = llm_extract(instructions=instructions, text=question, json_schema=COMPOSE_SCHEMA, schema_name="rag_answer")
    return result["answer"]
