"""Upload + browse-all endpoints (plan section 5)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Batch, Document, DocumentTopic, Job
from app.preview import (
    build_element_preview,
    delimiter_for,
    preview_kind,
    read_delimited,
    read_text,
    resolve_media_type,
)
from app.schemas_pydantic import (
    DocumentDetailOut,
    DocumentOut,
    DocumentPreviewOut,
    DocumentUploadResult,
    PreviewElement,
    UploadResponse,
)
from app.serializers import document_detail_out, document_out
from app.storage import compute_hash, write_file

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


@router.post("/documents", response_model=UploadResponse)
def upload_documents(
    files: list[UploadFile] = File(...),
    schema_id: uuid.UUID | None = Form(None),
    topic_ids: list[uuid.UUID] | None = Form(None),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Batch upload (decision #9): N independent pipeline runs. `schema_id`/
    `topic_ids` are only a default hint applied to every file - still
    overridable per file in review.

    Every uploaded file becomes its own document with its own extraction
    job, byte-identical re-uploads included. There is deliberately no
    pre-extraction exact-dedup tier: it used to silently fold a re-upload
    into the existing document, which created neither a document nor a job,
    so the upload appeared to vanish - nothing extracted, nothing in the
    extraction queue, nothing in review. Duplicates are accepted instead.
    """
    batch = Batch(default_schema_id=schema_id, default_topic_ids=topic_ids)
    db.add(batch)
    db.flush()

    results: list[DocumentUploadResult] = []
    for upload in files:
        content = upload.file.read()
        file_hash = compute_hash(content)
        storage_path = write_file(content, upload.filename or "")
        document = Document(
            filename=upload.filename or "unnamed",
            mime_type=upload.content_type,
            file_size=len(content),
            file_hash=file_hash,
            storage_path=storage_path,
            batch_id=batch.id,
            schema_id=schema_id,
        )
        db.add(document)
        db.flush()

        job = Job(document_id=document.id, batch_id=batch.id, status="pending", kind="extract")
        db.add(job)
        db.flush()

        results.append(DocumentUploadResult(document_id=document.id, job_id=job.id, filename=document.filename))

    db.commit()
    return UploadResponse(batch_id=batch.id, documents=results)


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(
    topic_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
    status: list[str] | None = Query(None),
    db: Session = Depends(get_db),
) -> list[DocumentOut]:
    """`status` may be repeated (`?status=pending&status=extracting`) so the
    review queue can show in-progress documents (not just
    `awaiting_review`) in one call."""
    query = db.query(Document)
    if schema_id is not None:
        query = query.filter(Document.schema_id == schema_id)
    if topic_id is not None:
        query = query.join(DocumentTopic, DocumentTopic.document_id == Document.id).filter(
            DocumentTopic.topic_id == topic_id
        )
    documents = query.order_by(Document.uploaded_at.desc()).all()

    out = [document_out(d) for d in documents]
    if status:
        status_set = set(status)
        out = [d for d in out if d.latest_job_status in status_set]
    return out


@router.get("/documents/{document_id}", response_model=DocumentDetailOut)
def get_document(document_id: uuid.UUID, db: Session = Depends(get_db)) -> DocumentDetailOut:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "document not found")
    return document_detail_out(document, db)


@router.get("/documents/{document_id}/file")
def get_document_file(
    document_id: uuid.UUID,
    download: bool = False,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Streams the original upload. `FileResponse` natively supports HTTP
    range requests, so a PDF viewer can seek.

    Only the browser-native preview kinds (pdf, image, html) are pointed
    here; everything else goes through `/preview`, which is what actually
    stopped office formats from downloading. Two things still had to change
    here:

    * The media type is derived from the *filename*, not from
      `documents.mime_type`. That column holds the browser's upload-time
      `content_type`, which is empty or `application/octet-stream` often
      enough that PDFs and images were being downloaded despite the browser
      having a viewer for them - octet-stream means "unknown bytes, save
      it".
    * `filename` is only sent when the caller actually asked to download.
      Starlette emits `Content-Disposition` solely because `filename` was
      set, and even `inline; filename=...` nudges some browsers toward the
      download manager. Previews get no disposition header at all.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "document not found")
    if not Path(document.storage_path).is_file():
        raise HTTPException(404, "original file is no longer on disk")

    media_type = resolve_media_type(document.filename, document.mime_type)
    if download:
        return FileResponse(
            document.storage_path,
            media_type=media_type,
            filename=document.filename,
            content_disposition_type="attachment",
        )
    return FileResponse(document.storage_path, media_type=media_type)


@router.get("/documents/{document_id}/preview", response_model=DocumentPreviewOut)
def get_document_preview(document_id: uuid.UUID, db: Session = Depends(get_db)) -> DocumentPreviewOut:
    """A renderable preview of the original, for any type the browser can't
    render itself.

    This is the fix for "the review page downloads the file instead of
    showing it". Handing raw .docx/.xlsx/.pptx/.csv/.eml bytes to an
    `<iframe>` always downloaded them - `Content-Disposition: inline` asks
    the browser to display the file, it cannot give the browser a renderer
    it doesn't have. So the file is converted server-side into structured
    elements (see app/preview.py) using the same `unstructured` parser the
    extraction pipeline runs, and the frontend renders those as real DOM.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "document not found")

    media_type = resolve_media_type(document.filename, document.mime_type)
    base = {
        "document_id": document.id,
        "filename": document.filename,
        "media_type": media_type,
        "file_size": document.file_size,
    }

    if not Path(document.storage_path).is_file():
        return DocumentPreviewOut(
            **base, kind="unsupported", message="The original file is no longer on disk."
        )

    kind = preview_kind(document.filename, media_type)

    if kind in ("pdf", "image", "audio", "video", "html"):
        # Browser-native: no payload, the frontend streams /file directly.
        return DocumentPreviewOut(**base, kind=kind)

    if kind == "unsupported":
        return DocumentPreviewOut(
            **base,
            kind="unsupported",
            message="This file type can't be previewed in the browser. Download it to open it locally.",
        )

    if kind == "text":
        text, truncated = read_text(document.storage_path)
        return DocumentPreviewOut(**base, kind="text", text=text, truncated=truncated)

    delimiter = delimiter_for(document.filename)
    try:
        if delimiter is not None:
            elements = read_delimited(document.storage_path, delimiter)
            truncated = any(e.get("truncated") for e in elements)
        else:
            elements, truncated = build_element_preview(document.storage_path)
    except Exception:
        # A format `unstructured` can't parse, a corrupt file, a missing
        # optional parser dependency. "Unsupported" is a preview outcome the
        # UI renders (with a download link), not a 500 - and definitely not
        # the surprise download this endpoint exists to remove.
        logger.exception("could not build a preview for document %s (%s)", document.id, document.filename)
        return DocumentPreviewOut(
            **base,
            kind="unsupported",
            message="This file type can't be previewed in the browser. Download it to open it locally.",
        )

    if not elements:
        return DocumentPreviewOut(
            **base, kind="unsupported", message="No readable content was found in this file."
        )
    return DocumentPreviewOut(
        **base,
        kind="elements",
        elements=[PreviewElement(**e) for e in elements],
        truncated=truncated,
    )
