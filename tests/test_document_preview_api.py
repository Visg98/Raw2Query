"""Upload a real file of every supported type and prove it previews.

The regression being guarded, end to end over HTTP:

* `GET /documents/{id}/preview` must return a renderable `kind` with the
  payload that kind needs - never a shrug that leaves the UI with no option
  but to hand the file to the download manager.
* `GET /documents/{id}/file` must send *no* `Content-Disposition` at all.
  Starlette emits the header only because `filename` was passed, and even
  `inline; filename=...` pushes some browsers toward saving the file. The
  media type must also be the real one, not the browser's upload-time
  guess.
* `GET /documents/{id}/file?download=1` is the one and only route that says
  `attachment` - an explicit "save a copy", never a surprise.
"""

from __future__ import annotations

import shutil

import pytest

from tests.conftest import requires_db

pytestmark = requires_db

# Scanned-image formats reach text only through OCR, which needs the
# `tesseract` binary (docker/ installs it; a bare checkout may not have it).
# Where it's missing the *graceful* outcome is still asserted below - an
# explained `unsupported` preview rather than a 500 or a surprise download -
# only the "and it produced elements" half is skipped.
OCR_EXTS = {".tiff"}
HAS_OCR = shutil.which("tesseract") is not None

# Extension -> the kind the endpoint must report, and where its content
# lives. `native` kinds carry no payload: the frontend streams /file and the
# browser renders it. See app/preview.py.
EXPECTED = {
    ".pdf": ("pdf", "native"),
    ".png": ("image", "native"),
    ".svg": ("image", "native"),
    ".html": ("html", "native"),
    ".wav": ("audio", "native"),
    ".docx": ("elements", "elements"),
    ".xlsx": ("elements", "elements"),
    ".pptx": ("elements", "elements"),
    ".eml": ("elements", "elements"),
    ".csv": ("elements", "elements"),
    ".tsv": ("elements", "elements"),
    ".tiff": ("elements", "elements"),
    ".txt": ("text", "text"),
    ".md": ("text", "text"),
    ".json": ("text", "text"),
    ".xml": ("text", "text"),
    ".yaml": ("text", "text"),
    ".py": ("text", "text"),
    # The honest case: no preview exists, and the API says so instead of
    # letting the UI start a download.
    ".zip": ("unsupported", "message"),
}


@pytest.fixture(scope="module")
def uploaded(request):
    """Upload one of every sample once and reuse the ids across the module.

    Module-scoped because partitioning a TIFF through OCR is slow and the
    same document is asserted about from several angles.
    """
    from fastapi.testclient import TestClient

    from app.api import app
    from tests.conftest import SAMPLE_BUILDERS

    directory = request.getfixturevalue("tmp_path_factory").mktemp("upload-samples")
    files = []
    for ext, builder in SAMPLE_BUILDERS.items():
        path = directory / f"sample{ext}"
        path.write_bytes(builder())
        files.append(("files", (path.name, path.read_bytes())))

    with TestClient(app) as client:
        response = client.post("/documents", files=files)
        assert response.status_code == 200, response.text
        documents = response.json()["documents"]

    by_ext = {}
    for document in documents:
        ext = "." + document["filename"].rsplit(".", 1)[1]
        by_ext[ext] = document["document_id"]
    return by_ext


@pytest.mark.parametrize("ext", sorted(EXPECTED))
def test_preview_returns_a_renderable_kind(ext, uploaded, client):
    """No supported type may leave the UI with nothing to render."""
    expected_kind, payload = EXPECTED[ext]
    response = client.get(f"/documents/{uploaded[ext]}/preview")
    # Whatever the file, the endpoint answers - it never 500s and never
    # leaves the UI with only a download to fall back on.
    assert response.status_code == 200, response.text
    preview = response.json()

    if ext in OCR_EXTS and not HAS_OCR:
        # The honest degraded path: still a 200, still explained.
        assert preview["kind"] == "unsupported" and preview["message"]
        pytest.skip("tesseract is not installed, so OCR-backed previews can't be produced here")

    assert preview["kind"] == expected_kind, f"{ext} previewed as {preview['kind']!r}"

    if payload == "elements":
        assert preview["elements"], f"{ext} reported no elements to render"
    elif payload == "text":
        assert preview["text"], f"{ext} reported no text to render"
    elif payload == "message":
        # An unpreviewable file must explain itself, so the UI can show a
        # download card instead of silently doing nothing.
        assert preview["message"]
    else:
        # Browser-native: no payload, by design.
        assert preview["elements"] is None and preview["text"] is None


def test_office_tables_come_back_as_cell_text_not_markup(uploaded, client):
    """Table cells are rendered as a real `<table>` in the UI, so the API
    hands over rows of text - nothing derived from an uploaded file is ever
    injected as HTML."""
    preview = client.get(f"/documents/{uploaded['.xlsx']}/preview").json()
    tables = [e for e in preview["elements"] if e["type"] == "table"]
    assert tables, "the spreadsheet preview has no table element"
    header = tables[0]["rows"][0]
    assert "Region" in header and "Sales" in header
    assert all("<" not in cell for row in tables[0]["rows"] for cell in row)


def test_quoted_csv_cells_survive_parsing(uploaded, client):
    """A cell containing the delimiter is the reason CSV is parsed
    server-side rather than shown as raw text."""
    preview = client.get(f"/documents/{uploaded['.csv']}/preview").json()
    rows = preview["elements"][0]["rows"]
    assert rows[0] == ["vendor", "amount", "note"]
    assert rows[1] == ["Acme Ltd", "1200.50", "Delivered, late"]


@pytest.mark.parametrize("ext", sorted(EXPECTED))
def test_file_endpoint_sends_no_disposition_for_a_preview(ext, uploaded, client):
    """The header that used to force the download."""
    response = client.get(f"/documents/{uploaded[ext]}/file")
    assert response.status_code == 200
    assert "content-disposition" not in response.headers, (
        f"{ext} is served with Content-Disposition: {response.headers.get('content-disposition')!r}"
    )


@pytest.mark.parametrize("ext", sorted(EXPECTED))
def test_download_flag_is_the_only_attachment_route(ext, uploaded, client):
    response = client.get(f"/documents/{uploaded[ext]}/file", params={"download": 1})
    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    assert disposition.startswith("attachment"), disposition
    assert f"sample{ext}" in disposition


@pytest.mark.parametrize(
    ("ext", "expected_media_type"),
    [
        (".pdf", "application/pdf"),
        (".png", "image/png"),
        (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (".wav", "audio/x-wav"),
    ],
)
def test_file_is_served_with_a_real_media_type(ext, expected_media_type, uploaded, client):
    response = client.get(f"/documents/{uploaded[ext]}/file")
    content_type = response.headers["content-type"].split(";")[0]
    # `mimetypes` disagrees with itself across platforms on a couple of
    # these (audio/wav vs audio/x-wav), so accept either spelling - what
    # matters is that it is never octet-stream.
    assert content_type != "application/octet-stream"
    assert content_type.replace("x-", "") == expected_media_type.replace("x-", "")


def test_a_useless_stored_mime_type_does_not_cause_a_download(uploaded, client, db_session):
    """The single most common trigger of the original bug: a drag-and-drop
    upload arrives as `application/octet-stream`, which means "unknown
    bytes, save me" to every browser - so the PDF downloaded even though the
    browser had a viewer for it."""
    import uuid

    from app.models import Document

    document_id = uuid.UUID(uploaded[".pdf"])
    document = db_session.get(Document, document_id)
    original = document.mime_type
    document.mime_type = "application/octet-stream"
    db_session.commit()
    try:
        preview = client.get(f"/documents/{document_id}/preview").json()
        assert preview["kind"] == "pdf"
        assert preview["media_type"] == "application/pdf"
        served = client.get(f"/documents/{document_id}/file")
        assert served.headers["content-type"].split(";")[0] == "application/pdf"
    finally:
        document.mime_type = original
        db_session.commit()


def test_preview_of_a_missing_original_is_reported_not_raised(uploaded, client, db_session):
    """A stale `storage_path` must produce an explained `unsupported`
    preview, not a 500 and not a broken viewer."""
    import uuid

    from app.models import Document

    document_id = uuid.UUID(uploaded[".txt"])
    document = db_session.get(Document, document_id)
    original = document.storage_path
    document.storage_path = original + ".gone"
    db_session.commit()
    try:
        response = client.get(f"/documents/{document_id}/preview")
        assert response.status_code == 200
        assert response.json()["kind"] == "unsupported"
        assert "no longer on disk" in response.json()["message"]
        assert client.get(f"/documents/{document_id}/file").status_code == 404
    finally:
        document.storage_path = original
        db_session.commit()


def test_unknown_document_404s(client):
    assert client.get("/documents/00000000-0000-0000-0000-000000000000/preview").status_code == 404
    assert client.get("/documents/00000000-0000-0000-0000-000000000000/file").status_code == 404
