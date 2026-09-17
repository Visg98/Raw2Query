"""Initial schema: extensions, all tables/indexes, the read-only query-runner
role + grants, and the seeded "Uncategorized" topic.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-13
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match app.config.Settings.embedding_dim / EMBEDDING_MODEL_NAME
# (BAAI/bge-small-en-v1.5 -> 384 dims, decision #22).
EMBEDDING_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.execute(
        """
        CREATE TABLE batches (
            id uuid PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            default_schema_id uuid,
            default_topic_ids uuid[]
        )
        """
    )

    op.execute(
        """
        CREATE TABLE schemas (
            id uuid PRIMARY KEY,
            name text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE schema_versions (
            id uuid PRIMARY KEY,
            schema_id uuid NOT NULL REFERENCES schemas(id),
            version integer NOT NULL,
            fields jsonb NOT NULL,
            identity_fields text[],
            is_breaking_from_prev boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (schema_id, version)
        )
        """
    )

    op.execute(
        "ALTER TABLE batches ADD CONSTRAINT fk_batches_default_schema_id "
        "FOREIGN KEY (default_schema_id) REFERENCES schemas(id)"
    )

    op.execute(
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY,
            filename text NOT NULL,
            mime_type text,
            file_size bigint NOT NULL,
            file_hash varchar(64) NOT NULL UNIQUE,
            storage_path text NOT NULL,
            uploaded_at timestamptz NOT NULL DEFAULT now(),
            batch_id uuid REFERENCES batches(id),
            schema_id uuid REFERENCES schemas(id),
            schema_version_id uuid REFERENCES schema_versions(id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE extracted_records (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(id),
            schema_id uuid NOT NULL REFERENCES schemas(id),
            schema_version_id uuid NOT NULL REFERENCES schema_versions(id),
            data jsonb NOT NULL,
            is_duplicate_of uuid REFERENCES extracted_records(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE topics (
            id uuid PRIMARY KEY,
            name citext NOT NULL UNIQUE,
            description text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE document_topics (
            document_id uuid NOT NULL REFERENCES documents(id),
            topic_id uuid NOT NULL REFERENCES topics(id),
            PRIMARY KEY (document_id, topic_id)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE chunks (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(id),
            chunk_index integer NOT NULL,
            content text NOT NULL,
            embedding vector({EMBEDDING_DIM}) NOT NULL,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
            topic_ids uuid[],
            metadata jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # HNSW cosine index (pgvector) + full-text + topic-scoping indexes (decision #22).
    op.execute("CREATE INDEX idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX idx_chunks_tsv ON chunks USING gin (tsv)")
    op.execute("CREATE INDEX idx_chunks_topic_ids ON chunks USING gin (topic_ids)")

    op.execute(
        """
        CREATE TABLE jobs (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(id),
            batch_id uuid REFERENCES batches(id),
            status text NOT NULL DEFAULT 'pending',
            kind text NOT NULL DEFAULT 'extract',
            progress jsonb,
            error_message text,
            result jsonb,
            locked_at timestamptz,
            locked_by text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Supports both the SKIP LOCKED claim query and the batch review listing.
    op.execute("CREATE INDEX idx_jobs_status_created_at ON jobs (status, created_at)")

    # Decision #16: nothing in the system is topic-less.
    op.execute(
        "INSERT INTO topics (id, name, description) VALUES "
        "(gen_random_uuid(), 'Uncategorized', 'Documents with no applicable topic.') "
        "ON CONFLICT DO NOTHING"
    )

    # Restricted role for NL-to-SQL's DB-enforced safety rail (plan section 3,
    # call site 11): SELECT only, and only on views the app's own role
    # creates from here on (i.e. the view_* views from regenerate_schema_view,
    # never the base tables above, which already exist before this grant).
    query_runner_password = os.environ.get("QUERY_RUNNER_DB_PASSWORD", "raw2query_query_runner")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'raw2query_query_runner') THEN
                CREATE ROLE raw2query_query_runner LOGIN PASSWORD '{query_runner_password}';
            END IF;
        END
        $$;
        """
    )
    db_name = os.environ.get("POSTGRES_DB", "raw2query")
    op.execute(f"GRANT CONNECT ON DATABASE {db_name} TO raw2query_query_runner")
    op.execute("GRANT USAGE ON SCHEMA public TO raw2query_query_runner")
    app_role = op.get_bind().execute(sa.text("SELECT current_user")).scalar()
    op.execute(
        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{app_role}" IN SCHEMA public '
        "GRANT SELECT ON TABLES TO raw2query_query_runner"
    )


def downgrade() -> None:
    app_role = op.get_bind().execute(sa.text("SELECT current_user")).scalar()
    op.execute(
        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{app_role}" IN SCHEMA public '
        "REVOKE SELECT ON TABLES FROM raw2query_query_runner"
    )
    op.execute("REVOKE ALL ON SCHEMA public FROM raw2query_query_runner")
    op.execute("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'raw2query_query_runner') THEN DROP ROLE raw2query_query_runner; END IF; END $$;")

    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS idx_chunks_tsv")
    op.execute("DROP INDEX IF EXISTS idx_chunks_topic_ids")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS document_topics")
    op.execute("DROP TABLE IF EXISTS topics")
    op.execute("DROP TABLE IF EXISTS extracted_records")
    op.execute("ALTER TABLE batches DROP CONSTRAINT IF EXISTS fk_batches_default_schema_id")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS schema_versions")
    op.execute("DROP TABLE IF EXISTS schemas")
    op.execute("DROP TABLE IF EXISTS batches")
