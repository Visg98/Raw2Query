"""Coverage for GET /schemas/{id}/records paging, and for the review PATCH's
handling of row confidence.

The paging tests need Postgres (they page over a real generated view) but
never an LLM. They exist because the two bugs they pin were both invisible to
a single-page test: a missing `total` and a non-total ORDER BY only misbehave
once there is more than one page.
"""

import uuid

import pytest
from sqlalchemy import text

from app.models import Batch, Document, ExtractedRecord, Job, SchemaDef, SchemaVersion
from app.pipeline.views import regenerate_schema_view
from tests.conftest import requires_db

K_LINE = "f_31313131"
K_INV = "f_32323232"


def f(name, type="string", column=None, scope="document"):
    return {
        "name": name,
        "type": type,
        "description": "",
        "required": False,
        "column": column,
        "scope": scope,
    }


FIELDS = [
    f("Invoice Number", "string", K_INV),
    f("Line Description", "string", K_LINE, scope="row"),
]


SEED_PREFIX = "r2qtest_pagination_"


def _purge_seeded(session, *, name: str | None = None) -> int:
    """Removes seeded schemas and everything hanging off them.

    Keyed on the `SEED_PREFIX` name rather than on ids captured by the
    fixture, so it works both as that fixture's teardown and as a sweep for
    anything a previously *failed* teardown left behind. That second job is
    why it exists at all: an earlier version of this fixture raised partway
    through cleanup and stranded 8 schemas, 40 documents and 200 records in
    the developer's database, where they showed up in the real schema list.
    A test that seeds committed data has to be able to clean up after its own
    crash, not just after its own success.

    Deletion order is child-to-parent with a flush between each step: bulk
    `query(...).delete()` bypasses the ORM's dependency ordering, so a parent
    deleted while its children are merely pending-deleted raises
    ForeignKeyViolation - which is precisely how the original leak happened.
    """
    schemas = [
        s
        for s in session.query(SchemaDef).all()
        if s.name.startswith(SEED_PREFIX) and (name is None or s.name == name)
    ]
    for schema in schemas:
        documents = session.query(Document).filter(Document.schema_id == schema.id).all()
        document_ids = [d.id for d in documents]
        batch_ids = {d.batch_id for d in documents if d.batch_id}

        session.query(ExtractedRecord).filter(ExtractedRecord.schema_id == schema.id).delete()
        session.flush()
        if document_ids:
            session.query(Job).filter(Job.document_id.in_(document_ids)).delete()
            session.flush()
            session.query(Document).filter(Document.id.in_(document_ids)).delete()
            session.flush()
        session.execute(text(f"DROP VIEW IF EXISTS view_{schema.name}"))
        session.query(SchemaVersion).filter(SchemaVersion.schema_id == schema.id).delete()
        session.flush()
        session.query(SchemaDef).filter(SchemaDef.id == schema.id).delete()
        session.flush()
        for batch_id in batch_ids:
            # Only if nothing else moved into it; a shared batch isn't ours.
            if session.query(Document).filter(Document.batch_id == batch_id).count() == 0:
                session.query(Batch).filter(Batch.id == batch_id).delete()
        session.flush()
    session.commit()
    return len(schemas)


@pytest.fixture
def seeded_schema(db_session):
    """A schema with 25 records spread over 5 documents - so paging has more
    than one page, and every record of a document shares a `created_at`.

    Committed rather than left in the transaction: the endpoint under test
    runs in its own session via the TestClient. That commit is what makes the
    teardown load-bearing, hence `_purge_seeded` and the sweep below.
    """
    session = db_session
    # Sweep first: if a previous run died before cleaning up, its rows are
    # still committed and would otherwise accumulate on every run.
    _purge_seeded(session)

    name = f"{SEED_PREFIX}{uuid.uuid4().hex[:8]}"
    schema = SchemaDef(name=name)
    session.add(schema)
    session.flush()
    version = SchemaVersion(schema_id=schema.id, version=1, fields=FIELDS, is_breaking_from_prev=False)
    session.add(version)
    session.flush()
    regenerate_schema_view(session, schema.id)

    batch = Batch()
    session.add(batch)
    session.flush()
    for d in range(5):
        document = Document(
            batch_id=batch.id,
            filename=f"inv-{d}.pdf",
            storage_path="/dev/null",
            mime_type="application/pdf",
            file_size=1,
            file_hash=uuid.uuid4().hex,
            schema_id=schema.id,
            schema_version_id=version.id,
        )
        session.add(document)
        session.flush()
        # Five line items per document, all inserted together - so all five
        # share a `created_at` to the microsecond, which is exactly the tie
        # that made `ORDER BY created_at DESC` alone unsound for paging.
        for row_index in range(5):
            session.add(
                ExtractedRecord(
                    document_id=document.id,
                    schema_id=schema.id,
                    schema_version_id=version.id,
                    row_index=row_index,
                    data={"Invoice Number": f"INV-{d}", "Line Description": f"line-{d}-{row_index}"},
                )
            )
    session.commit()

    try:
        yield schema
    finally:
        # `finally`, so a failing assertion still cleans up, and `rollback`
        # first, so a test that left the session in a failed transaction
        # doesn't make every delete below a no-op.
        session.rollback()
        _purge_seeded(session, name=name)


def fetch(client, schema, page, size=10):
    response = client.get(
        f"/schemas/{schema.id}/records", params={"limit": size, "offset": page * size}
    )
    assert response.status_code == 200, response.text
    return response.json()


@requires_db
class TestRecordPaging:
    def test_response_carries_rows_and_total(self, client, seeded_schema):
        """The pager cannot know where the data ends without `total`: 'Next'
        stayed enabled forever, and one click past the last page hid the pager
        along with the table, stranding the user on a blank page."""
        body = fetch(client, seeded_schema, 0)
        assert set(body) == {"rows", "total"}
        assert body["total"] == 25
        assert len(body["rows"]) == 10

    def test_total_ignores_limit_and_offset(self, client, seeded_schema):
        assert fetch(client, seeded_schema, 2)["total"] == 25

    def test_last_page_is_short_and_not_empty(self, client, seeded_schema):
        assert len(fetch(client, seeded_schema, 2)["rows"]) == 5

    def test_paging_past_the_end_is_empty_rather_than_an_error(self, client, seeded_schema):
        body = fetch(client, seeded_schema, 9)
        assert body["rows"] == []
        # Still reported, so the UI can send the user back to a real page.
        assert body["total"] == 25

    def test_every_record_appears_exactly_once_across_pages(self, client, seeded_schema):
        """The ORDER BY regression. `created_at DESC` alone is a partial order
        once a document confirms several line items in one transaction, and
        Postgres may order tied rows differently per query - so a row could
        come back on two pages, or on none, and no single-page test would
        notice."""
        seen = []
        for page in range(3):
            seen.extend(row["f_31313131"] for row in fetch(client, seeded_schema, page)["rows"])
        assert len(seen) == 25
        assert len(set(seen)) == 25

    def test_line_items_stay_grouped_and_ordered_within_a_document(self, client, seeded_schema):
        """The tiebreak is `(document_id, row_index)`, which is also the order
        a reader wants - not an arbitrary one that happens to be stable."""
        rows = fetch(client, seeded_schema, 0)["rows"]
        by_document: dict[str, list[int]] = {}
        for row in rows:
            by_document.setdefault(str(row["document_id"]), []).append(row["row_index"])
        for indices in by_document.values():
            assert indices == sorted(indices)

    def test_filters_narrow_the_total_too(self, client, seeded_schema):
        response = client.get(
            f"/schemas/{seeded_schema.id}/records", params={"limit": 10, "offset": 0, K_INV: "INV-2"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 5
        assert {row[K_INV] for row in body["rows"]} == {"INV-2"}

    def test_topic_ids_is_still_withheld(self, client, seeded_schema):
        """A scoping column for the query engine, not part of a record."""
        assert all("topic_ids" not in row for row in fetch(client, seeded_schema, 0)["rows"])


@requires_db
class TestReviewPatchDataConfidence:
    """The review screen sends the whole table on every save, so the server
    has to decide for itself which cells actually changed."""

    @pytest.fixture
    def job(self, db_session):
        session = db_session
        batch = Batch()
        session.add(batch)
        session.flush()
        document = Document(
            batch_id=batch.id,
            filename="inv.pdf",
            storage_path="/dev/null",
            mime_type="application/pdf",
            file_size=1,
            file_hash=uuid.uuid4().hex,
        )
        session.add(document)
        session.flush()
        row_job = Job(document_id=document.id, batch_id=batch.id, status="awaiting_review", kind="extract")
        row_job.result = {
            "extracted_data": [
                {"Vendor": "Acme", "Line Amount": "1"},
                {"Vendor": "Acme", "Line Amount": "2"},
            ],
            "data_confidence": [
                {"Vendor": 0.99, "Line Amount": 0.9},
                {"Vendor": 0.99, "Line Amount": 0.8},
            ],
            "chunks": [{"content": "hello", "embedding": [0.5] * 384, "metadata": {"page_number": 1}}],
        }
        session.add(row_job)
        session.commit()

        # Same reasoning as `seeded_schema`: this fixture commits, so its
        # cleanup has to survive a failing test body.
        try:
            yield row_job
        finally:
            session.rollback()
            session.query(Job).filter(Job.id == row_job.id).delete()
            session.flush()
            session.query(Document).filter(Document.id == document.id).delete()
            session.flush()
            session.query(Batch).filter(Batch.id == batch.id).delete()
            session.commit()

    def test_resending_the_identical_table_keeps_the_confidence_scores(self, client, job):
        """Otherwise the flags vanish off a table the reviewer only looked
        at - one 'Save draft' and the low-confidence cell stops being
        highlighted."""
        response = client.patch(
            f"/jobs/{job.id}/review",
            json={
                "extracted_data": [
                    {"Vendor": "Acme", "Line Amount": "1"},
                    {"Vendor": "Acme", "Line Amount": "2"},
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["data_confidence"] == [
            {"Vendor": 0.99, "Line Amount": 0.9},
            {"Vendor": 0.99, "Line Amount": 0.8},
        ]

    def test_editing_one_cell_only_drops_that_cell_score(self, client, job):
        """Per cell, not per table: dropping every score would blank the flags
        on all 40 rows of an invoice because one was touched, which makes them
        useless on exactly the documents that need them. The edited cell keeps
        no model confidence - a hand-typed value was never scored."""
        response = client.patch(
            f"/jobs/{job.id}/review",
            json={
                "extracted_data": [
                    {"Vendor": "Acme", "Line Amount": "1"},
                    {"Vendor": "Acme", "Line Amount": "999"},
                ]
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()["result"]
        assert body["data_confidence"][0] == {"Vendor": 0.99, "Line Amount": 0.9}
        assert body["data_confidence"][1] == {"Vendor": 0.99}
        assert body["extracted_data"][1]["Line Amount"] == "999"

    def test_an_added_row_starts_with_no_scores(self, client, job):
        """It must not inherit the scores of whatever sat at that index."""
        response = client.patch(
            f"/jobs/{job.id}/review",
            json={
                "extracted_data": [
                    {"Vendor": "Acme", "Line Amount": "1"},
                    {"Vendor": "Acme", "Line Amount": "2"},
                    {"Vendor": "Acme", "Line Amount": "3"},
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["data_confidence"][2] == {}

    def test_deleting_every_row_is_honoured(self, client, job):
        """`[]` is a meaningful edit, not 'not sent' - the reviewer decided
        this document has no rows."""
        response = client.patch(f"/jobs/{job.id}/review", json={"extracted_data": []})
        assert response.status_code == 200, response.text
        body = response.json()["result"]
        assert body["extracted_data"] == []
        assert body["data_confidence"] == []

    def test_not_sending_the_table_leaves_it_alone(self, client, job):
        response = client.patch(f"/jobs/{job.id}/review", json={"topic_ids": []})
        assert response.status_code == 200, response.text
        body = response.json()["result"]
        assert len(body["extracted_data"]) == 2
        assert body["data_confidence"] == [
            {"Vendor": 0.99, "Line Amount": 0.9},
            {"Vendor": 0.99, "Line Amount": 0.8},
        ]

    def test_a_retired_key_is_rejected_rather_than_ignored(self, client, job):
        """A tab left open across the `extracted_rows` -> `extracted_data`
        change must not get a 200 for edits the server silently discards."""
        response = client.patch(f"/jobs/{job.id}/review", json={"extracted_rows": []})
        assert response.status_code == 422

    def test_the_review_response_omits_chunk_embeddings(self, client, job, db_session):
        """A 384-float vector per chunk is the bulk of this response and
        nothing renders it. Critically, stripping it must not reach the stored
        payload - confirm reads those embeddings server-side."""
        response = client.get(f"/jobs/{job.id}/review")
        assert response.status_code == 200, response.text
        chunks = response.json()["result"]["chunks"]
        assert chunks[0]["content"] == "hello"
        assert "embedding" not in chunks[0]

        # The PATCH path commits, so an in-place strip would be flushed here.
        assert client.patch(f"/jobs/{job.id}/review", json={"topic_ids": []}).status_code == 200
        db_session.expire_all()
        stored = db_session.get(Job, job.id)
        assert len(stored.result["chunks"][0]["embedding"]) == 384
