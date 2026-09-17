"""The rename lookup and view reconciliation, against real Postgres.

No LLM. `TestRenameLookup` is the test that justifies the version-scoped CASE:
it asserts the exact row values that the naive COALESCE form gets wrong.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from app.models import Document, ExtractedRecord, SchemaDef, SchemaVersion
from app.pipeline.views import (
    reconcile_schema_views,
    regenerate_schema_view,
    view_name_for,
)
from app.schema_fields import assign_column_keys
from tests.conftest import requires_db

pytestmark = requires_db


def f(name, type="string", column=None):
    out = {"name": name, "type": type, "description": "", "required": False}
    if column:
        out["column"] = column
    return out


@pytest.fixture
def lineage(db_session):
    """A 3-version lineage: v1 `Total`, v2 renames it to `Net` (key kept),
    v3 adds a brand-new `Total` reusing the retired name.

    One record per version, so every branch of the generated CASE is covered.
    """
    name = f"rename_test_{uuid.uuid4().hex[:8]}"
    schema = SchemaDef(name=name)
    db_session.add(schema)
    db_session.flush()

    v1_fields = assign_column_keys([f("Total", "number"), f("Vendor")])
    k_total = next(x["column"] for x in v1_fields if x["name"] == "Total")
    k_vendor = next(x["column"] for x in v1_fields if x["name"] == "Vendor")

    v2_fields = assign_column_keys(
        [f("Net", "number", k_total), f("Vendor", "string", k_vendor)],
        lineage_versions=[v1_fields],
    )
    v3_fields = assign_column_keys(
        [f("Net", "number", k_total), f("Vendor", "string", k_vendor), f("Total", "integer")],
        lineage_versions=[v1_fields, v2_fields],
    )
    k_new = next(x["column"] for x in v3_fields if x["name"] == "Total")

    versions = []
    for i, fields in enumerate([v1_fields, v2_fields, v3_fields], start=1):
        v = SchemaVersion(
            schema_id=schema.id, version=i, fields=fields, is_breaking_from_prev=False
        )
        db_session.add(v)
        db_session.flush()
        versions.append(v)

    document = Document(
        filename="rename-test.pdf", file_size=1, file_hash="0" * 64, storage_path="/dev/null"
    )
    db_session.add(document)
    db_session.flush()

    payloads = [
        {"Total": "100", "Vendor": "Acme"},
        {"Net": "200", "Vendor": "Acme"},
        # v3: this field has no value, and the row also carries the *reused*
        # name belonging to the new field. This is the row COALESCE gets wrong.
        {"Net": None, "Total": "300", "Vendor": "Acme"},
    ]
    for version, data in zip(versions, payloads, strict=True):
        db_session.add(
            ExtractedRecord(
                document_id=document.id,
                schema_id=schema.id,
                schema_version_id=version.id,
                data=data,
            )
        )
    db_session.flush()
    regenerate_schema_view(db_session, schema.id)
    db_session.commit()

    yield {
        "schema": schema,
        "view": view_name_for(name),
        "versions": versions,
        "document": document,
        "k_total": k_total,
        "k_new": k_new,
        "k_vendor": k_vendor,
    }

    db_session.execute(text(f"DROP VIEW IF EXISTS {view_name_for(name)}"))
    db_session.query(ExtractedRecord).filter(ExtractedRecord.schema_id == schema.id).delete()
    db_session.query(SchemaVersion).filter(SchemaVersion.schema_id == schema.id).delete()
    db_session.query(Document).filter(Document.id == document.id).delete()
    db_session.query(SchemaDef).filter(SchemaDef.id == schema.id).delete()
    db_session.commit()


class TestRenameLookup:
    def test_each_row_reads_under_its_own_versions_name(self, db_session, lineage):
        """v1's `Total` and v2/v3's `Net` are one column; the v3 row has no
        value for it and must read NULL - not the 300 that belongs to the new
        field which reused the name `Total`."""
        rows = db_session.execute(
            text(
                f"SELECT {lineage['k_total']} AS renamed, {lineage['k_new']} AS reused "
                f"FROM {lineage['view']} "
                "ORDER BY (SELECT version FROM schema_versions WHERE id = schema_version_id)"
            )
        ).all()
        assert [(r.renamed, r.reused) for r in rows] == [(100, None), (200, None), (None, 300)]

    def test_the_naive_coalesce_form_would_get_this_wrong(self, db_session, lineage):
        """Pins *why* the CASE exists. `->>` cannot distinguish an explicit
        JSON null from a missing key, and extraction writes absent fields as
        explicit nulls, so COALESCE falls through to the reused name."""
        leaked = db_session.execute(
            text(
                "SELECT COALESCE(data->>'Net', data->>'Total') AS bad "
                "FROM extracted_records er "
                "WHERE er.schema_id = :s "
                "  AND er.schema_version_id = :v"
            ),
            {"s": lineage["schema"].id, "v": lineage["versions"][2].id},
        ).scalar()
        assert leaked == "300", "if this stops leaking, the CASE form may be simplifiable"

    def test_an_untouched_field_still_reads_everywhere(self, db_session, lineage):
        values = db_session.execute(
            text(f"SELECT {lineage['k_vendor']} FROM {lineage['view']}")
        ).scalars().all()
        assert values == ["Acme", "Acme", "Acme"]

    def test_row_count_matches_the_base_table(self, db_session, lineage):
        """The topic LEFT JOIN must not drop records for a document with no
        topics - this document has none."""
        in_view = db_session.execute(text(f"SELECT count(*) FROM {lineage['view']}")).scalar()
        in_base = db_session.execute(
            text("SELECT count(*) FROM extracted_records WHERE schema_id = :s"),
            {"s": lineage["schema"].id},
        ).scalar()
        assert in_view == in_base == 3

    def test_topic_less_records_get_an_empty_array_not_null(self, db_session, lineage):
        """`NULL && ARRAY[...]` is NULL rather than false, so the COALESCE in
        the view is what makes topic scoping a hard exclusion."""
        values = db_session.execute(
            text(f"SELECT topic_ids FROM {lineage['view']}")
        ).scalars().all()
        assert all(v == [] for v in values)

    def test_the_readonly_role_can_read_the_view(self, lineage):
        """The view body joins `document_topics`, which the read-only role has
        no grant on - it works because a view runs with its owner's rights."""
        from sqlalchemy import create_engine

        from app.config import get_settings

        engine = create_engine(get_settings().readonly_database_url)
        with engine.connect() as conn:
            got = conn.execute(text(f"SELECT count(*) FROM {lineage['view']}")).scalar()
            assert got == 3
            with pytest.raises(Exception):
                conn.execute(text("SELECT 1 FROM document_topics LIMIT 1"))


class TestReconcile:
    def test_recreates_a_dropped_view(self, db_session, lineage):
        db_session.execute(text(f"DROP VIEW {lineage['view']}"))
        db_session.commit()
        rebuilt = reconcile_schema_views(db_session)
        db_session.commit()
        assert lineage["view"] in rebuilt
        assert db_session.execute(text(f"SELECT count(*) FROM {lineage['view']}")).scalar() == 3

    def test_is_a_noop_when_nothing_changed(self, db_session, lineage):
        reconcile_schema_views(db_session)
        db_session.commit()
        assert reconcile_schema_views(db_session) == []
        db_session.commit()

    def test_detects_a_rename_that_leaves_the_columns_identical(self, db_session, lineage):
        """The case a column-set probe cannot see: renaming a field changes
        neither the column name nor its type, only the view body. This is why
        reconcile compares a spec fingerprint."""
        version = lineage["versions"][2]
        renamed = [
            {**x, "name": "Net Amount"} if x["column"] == lineage["k_total"] else x
            for x in version.fields
        ]
        db_session.execute(
            text("UPDATE schema_versions SET fields = CAST(:f AS jsonb) WHERE id = :i"),
            {"f": json.dumps(renamed), "i": version.id},
        )
        db_session.commit()

        before = set(
            db_session.execute(
                text(
                    "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:v AS regclass) "
                    "AND attnum > 0 AND NOT attisdropped"
                ),
                {"v": lineage["view"]},
            ).scalars()
        )
        rebuilt = reconcile_schema_views(db_session)
        db_session.commit()
        after = set(
            db_session.execute(
                text(
                    "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:v AS regclass) "
                    "AND attnum > 0 AND NOT attisdropped"
                ),
                {"v": lineage["view"]},
            ).scalars()
        )
        assert lineage["view"] in rebuilt, "a body-only change must still be detected"
        assert before == after, "and the column set is genuinely unchanged by it"

    def test_skips_when_another_process_holds_the_lock(self, db_session, lineage):
        """Reconcile must never queue: DROP VIEW takes an AccessExclusiveLock,
        and waiting for it on startup would block every new reader behind it."""
        from app.db import SessionLocal

        other = SessionLocal()
        try:
            other.execute(text("SELECT pg_advisory_xact_lock(hashtext('r2q_view_reconcile'))"))
            db_session.execute(text(f"DROP VIEW {lineage['view']}"))
            assert reconcile_schema_views(db_session) == []
            db_session.rollback()
        finally:
            other.rollback()
            other.close()
