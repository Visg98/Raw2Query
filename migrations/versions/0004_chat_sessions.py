"""Server-side chat sessions for the Ask page.

The query page kept its history in `localStorage` as one flat message list:
a single conversation per browser, lost on a different device or a cleared
profile, with no way to keep two lines of enquiry apart. This adds the
conversation as a first-class record - many sessions, each with its own
ordered messages.

An assistant turn's supporting detail (routing, sql, rows, sources, topics)
goes into `chat_messages.metadata` so a reloaded conversation renders
exactly like the live one did.

Revision ID: 0004_chat_sessions
Revises: 0003_safe_cast_functions
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_chat_sessions"
down_revision: str | None = "0003_safe_cast_functions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # The sidebar's only read pattern: most recently used first.
    op.create_index("ix_chat_sessions_updated_at", "chat_sessions", [sa.text("updated_at DESC")])

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            # Deleting a conversation takes its messages with it; there is
            # nothing meaningful about an orphaned turn.
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Turn order. Not derivable from `created_at`: a question and its
        # answer are written in one transaction, and Postgres' `now()` is
        # transaction-scoped, so both rows carry an identical timestamp.
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        # Doubles as the read index for "this conversation, in order".
        sa.UniqueConstraint("session_id", "position", name="uq_chat_messages_session_id_position"),
    )


def downgrade() -> None:
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_updated_at", table_name="chat_sessions")
    op.drop_table("chat_sessions")
