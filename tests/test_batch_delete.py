"""DELETE /batches/{id} — the "delete" on the extraction and review queues.

The whole risk in this endpoint is over-deleting. Clearing a queue is meant
to throw away staged work, and staged work lives entirely in `jobs.result`:
`confirm_job` is the only thing that ever writes `extracted_records`,
`chunks` or `document_topics`. So the rule under test is that a confirmed
document and everything it produced survives a batch delete, while its
unconfirmed siblings do not — including when the two share a batch, which
is the case a reviewer actually hits after confirming some of an upload.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def cleanup_documents(db_session):
    """Removes anything a test leaves behind.

    These tests run against the project Postgres, not a throwaway database,
    so a test that asserts "the confirmed document survived" has to take
    that document back out itself — otherwise it accumulates in the real
    schema tables and the Ask page starts answering from test fixtures.
    """
    document_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    yield document_ids, batch_ids

    from app.models import Batch, Chunk, Document, DocumentTopic, ExtractedRecord, Job

    for document_id in document_ids:
        document = db_session.get(Document, document_id)
        storage_path = document.storage_path if document else None
        for model in (Chunk, ExtractedRecord, DocumentTopic, Job):
            db_session.query(model).filter(model.document_id == document_id).delete(synchronize_session=False)
        if document is not None:
            db_session.delete(document)
        if storage_path:
            Path(storage_path).unlink(missing_ok=True)
    for batch_id in batch_ids:
        db_session.query(Job).filter(Job.batch_id == batch_id).delete(synchronize_session=False)
        batch = db_session.get(Batch, batch_id)
        if batch is not None:
            db_session.delete(batch)
    db_session.commit()


def _upload(client, names=("queue-a.txt", "queue-b.txt")):
    files = [("files", (name, b"Invoice INV-7001\nTotal: 120.00\n", "text/plain")) for name in names]
    response = client.post("/documents", files=files)
    assert response.status_code == 200, response.text
    return response.json()


def _storage_paths(db_session, document_ids):
    from app.models import Document

    paths = []
    for document_id in document_ids:
        document = db_session.get(Document, uuid.UUID(document_id))
        assert document is not None
        paths.append(Path(document.storage_path))
    return paths


def test_delete_batch_removes_unconfirmed_documents_jobs_and_files(client, db_session, cleanup_documents):
    from app.models import Batch, Document, Job

    uploaded = _upload(client)
    batch_id = uploaded["batch_id"]
    document_ids = [d["document_id"] for d in uploaded["documents"]]

    paths = _storage_paths(db_session, document_ids)
    assert all(path.is_file() for path in paths), "upload should have written the originals"

    response = client.delete(f"/batches/{batch_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert sorted(body["deleted_document_ids"]) == sorted(document_ids)
    assert len(body["deleted_job_ids"]) == 2
    assert body["kept_document_ids"] == []
    assert body["batch_deleted"] is True

    db_session.expire_all()
    for document_id in document_ids:
        assert db_session.get(Document, uuid.UUID(document_id)) is None
    assert db_session.get(Batch, uuid.UUID(batch_id)) is None
    assert db_session.query(Job).filter(Job.batch_id == uuid.UUID(batch_id)).count() == 0
    # The originals are unlinked too - this is what makes the delete
    # irreversible, and what the confirm dialog warns about.
    assert not any(path.exists() for path in paths)


def test_delete_batch_keeps_a_confirmed_document_and_its_data(client, db_session, cleanup_documents):
    """The mixed batch: confirm one document, delete the batch, and the
    confirmed one plus its record and chunk must still be there."""
    from app.models import Chunk, Document, ExtractedRecord, Job, SchemaDef, SchemaVersion

    tracked_documents, tracked_batches = cleanup_documents

    uploaded = _upload(client, names=("keep.txt", "drop.txt"))
    batch_id = uuid.UUID(uploaded["batch_id"])
    keep_id = uuid.UUID(uploaded["documents"][0]["document_id"])
    drop_id = uuid.UUID(uploaded["documents"][1]["document_id"])
    tracked_documents.append(keep_id)
    tracked_batches.append(batch_id)

    # Stand the confirmed state up directly rather than driving the worker
    # and the review screen: what this endpoint reads is the committed
    # shape, and going through a real extraction would make the test depend
    # on a running worker and an LLM.
    schema = SchemaDef(name=f"batch-delete-test-{uuid.uuid4().hex[:8]}")
    db_session.add(schema)
    db_session.flush()
    version = SchemaVersion(schema_id=schema.id, version=1, fields=[], is_breaking_from_prev=False)
    db_session.add(version)
    db_session.flush()

    keep_job = db_session.query(Job).filter(Job.document_id == keep_id).one()
    keep_job.status = "confirmed"
    db_session.add(
        ExtractedRecord(
            document_id=keep_id, schema_id=schema.id, schema_version_id=version.id, data={"total": 120.0}
        )
    )
    db_session.add(
        Chunk(document_id=keep_id, chunk_index=0, content="Invoice INV-7001", embedding=[0.0] * 384)
    )
    db_session.commit()

    keep_path = Path(db_session.get(Document, keep_id).storage_path)
    drop_path = Path(db_session.get(Document, drop_id).storage_path)

    response = client.delete(f"/batches/{batch_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["deleted_document_ids"] == [str(drop_id)]
    assert body["kept_document_ids"] == [str(keep_id)]
    # A kept document still needs its batch for grouping in the UI.
    assert body["batch_deleted"] is False

    db_session.expire_all()
    assert db_session.get(Document, drop_id) is None
    assert not drop_path.exists()

    assert db_session.get(Document, keep_id) is not None
    assert keep_path.is_file()
    assert db_session.query(ExtractedRecord).filter(ExtractedRecord.document_id == keep_id).count() == 1
    assert db_session.query(Chunk).filter(Chunk.document_id == keep_id).count() == 1
    assert db_session.query(Job).filter(Job.document_id == keep_id).count() == 1

    # Tidy up the schema lineage this test invented.
    db_session.query(ExtractedRecord).filter(ExtractedRecord.document_id == keep_id).delete(
        synchronize_session=False
    )
    db_session.commit()
    db_session.delete(db_session.get(SchemaVersion, version.id))
    db_session.delete(db_session.get(SchemaDef, schema.id))
    db_session.commit()


def test_delete_batch_keeps_a_document_that_has_chunks_despite_its_job_status(
    client, db_session, cleanup_documents
):
    """The belt-and-braces guard: job status is the rule, but a document
    carrying chunks is live data whatever its jobs say, and must not be
    swept up by a queue tidy-up."""
    from app.models import Chunk, Document

    tracked_documents, tracked_batches = cleanup_documents

    uploaded = _upload(client, names=("stray.txt",))
    batch_id = uuid.UUID(uploaded["batch_id"])
    document_id = uuid.UUID(uploaded["documents"][0]["document_id"])
    tracked_documents.append(document_id)
    tracked_batches.append(batch_id)

    db_session.add(
        Chunk(document_id=document_id, chunk_index=0, content="stray content", embedding=[0.0] * 384)
    )
    db_session.commit()

    response = client.delete(f"/batches/{batch_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["deleted_document_ids"] == []
    assert body["kept_document_ids"] == [str(document_id)]

    db_session.expire_all()
    assert db_session.get(Document, document_id) is not None


def test_delete_batch_is_a_no_op_when_everything_is_already_confirmed(client, db_session, cleanup_documents):
    """Deleting a batch twice must not claim a second delete - the UI toasts
    straight off this response."""
    from app.models import Job

    tracked_documents, tracked_batches = cleanup_documents

    uploaded = _upload(client, names=("only.txt",))
    batch_id = uuid.UUID(uploaded["batch_id"])
    document_id = uuid.UUID(uploaded["documents"][0]["document_id"])
    tracked_documents.append(document_id)
    tracked_batches.append(batch_id)

    job = db_session.query(Job).filter(Job.document_id == document_id).one()
    job.status = "confirmed"
    db_session.commit()

    first = client.delete(f"/batches/{batch_id}").json()
    second = client.delete(f"/batches/{batch_id}").json()

    for body in (first, second):
        assert body["deleted_document_ids"] == []
        assert body["kept_document_ids"] == [str(document_id)]
        assert body["batch_deleted"] is False


def test_delete_unknown_batch_is_404(client):
    response = client.delete(f"/batches/{uuid.uuid4()}")
    assert response.status_code == 404
