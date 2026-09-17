"""SQLAlchemy ORM models for the schema in plans/stateless-booping-melody.md section 2."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import BIGINT, CITEXT, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import Computed

from app.db import Base


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = uuid_pk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    default_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schemas.id"), nullable=True
    )
    default_topic_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="batch")
    jobs: Mapped[list["Job"]] = relationship(back_populates="batch")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int] = mapped_column(BIGINT)
    # sha256 hex digest. Deliberately NOT unique: a byte-identical re-upload
    # is its own document with its own extraction run (migration 0002).
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=True)
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("schemas.id"), nullable=True)
    schema_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schema_versions.id"), nullable=True
    )

    batch: Mapped[Batch | None] = relationship(back_populates="documents")
    schema_def: Mapped["SchemaDef | None"] = relationship(foreign_keys=[schema_id])
    schema_version: Mapped["SchemaVersion | None"] = relationship(foreign_keys=[schema_version_id])
    jobs: Mapped[list["Job"]] = relationship(back_populates="document")
    extracted_records: Mapped[list["ExtractedRecord"]] = relationship(back_populates="document")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document")
    topic_links: Mapped[list["DocumentTopic"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class SchemaDef(Base):
    """A named schema lineage. Table name stays `schemas` per the plan; the
    Python class is `SchemaDef` to avoid colliding with sqlalchemy's own
    notion of a DB schema."""

    __tablename__ = "schemas"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    versions: Mapped[list["SchemaVersion"]] = relationship(back_populates="schema", order_by="SchemaVersion.version")


class SchemaVersion(Base):
    __tablename__ = "schema_versions"
    __table_args__ = (UniqueConstraint("schema_id", "version", name="uq_schema_versions_schema_id_version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    schema_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schemas.id"))
    version: Mapped[int] = mapped_column(Integer)
    fields: Mapped[list] = mapped_column(JSONB)  # [{name, type, description, required}, ...]
    identity_fields: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    is_breaking_from_prev: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    schema: Mapped[SchemaDef] = relationship(back_populates="versions")


class ExtractedRecord(Base):
    __tablename__ = "extracted_records"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"))
    schema_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schemas.id"))
    schema_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schema_versions.id"))
    # This record's position in its document's record set (migration 0006). A
    # schema with no row-scoped fields yields one record per document, always
    # at index 0 - which is what every pre-existing row was backfilled to.
    row_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    data: Mapped[dict] = mapped_column(JSONB)
    is_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extracted_records.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="extracted_records")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(CITEXT, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document_links: Mapped[list["DocumentTopic"]] = relationship(back_populates="topic")


class DocumentTopic(Base):
    """The document<->topic many-to-many join (decision #7). Source of truth
    for topics; `chunks.topic_ids` is a denormalized, re-synced copy."""

    __tablename__ = "document_topics"

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), primary_key=True)
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"), primary_key=True)

    document: Mapped[Document] = relationship(back_populates="topic_links")
    topic: Mapped[Topic] = relationship(back_populates="document_links")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(384))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True), nullable=True
    )
    topic_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    chunk_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    # 'extract' | 'backfill' | 'reextract' - picks the worker entry point.
    # Deliberately unconstrained text: a new kind is a new pipeline function,
    # not a migration.
    kind: Mapped[str] = mapped_column(Text, default="extract")
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="jobs")
    batch: Mapped[Batch | None] = relationship(back_populates="jobs")


class ChatSession(Base):
    """One conversation on the Ask page.

    Chat history used to live in `localStorage` as a single flat message
    list, so there was exactly one conversation per browser and it never
    survived a different device. Sessions are server-side now.

    A row is created by the /query endpoint on the first answered question,
    never by the frontend opening a new chat - so clicking "New chat" and
    walking away leaves nothing behind.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Bumped explicitly whenever a message is appended: adding a child row
    # doesn't dirty the parent, so `onupdate` alone would never fire and the
    # sidebar's most-recent-first ordering would freeze at creation time.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        order_by="ChatMessage.position",
        cascade="all, delete-orphan",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    # Turn order within the conversation, 0-based. `created_at` cannot do
    # this job: a question and its answer are inserted in one transaction
    # and `now()` is transaction-scoped in Postgres, so both rows get the
    # *same* timestamp and ordering by it is a coin flip - the answer could
    # render above the question that produced it.
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(Text)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text)
    # An assistant turn's supporting detail: routing_used, sql, rows,
    # sources, topic_ids_used - or `error` when the query failed. Kept as one
    # JSONB blob because it's replayed verbatim into the same UI that
    # rendered it live, never queried field by field.
    message_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[ChatSession] = relationship(back_populates="messages")


# Valid job status values (decisions #12, #19).
JOB_STATUSES = ("pending", "extracting", "awaiting_review", "confirmed", "rejected", "failed")

CHAT_ROLES = ("user", "assistant")
