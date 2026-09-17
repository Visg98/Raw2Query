"""Shared fixtures.

Sample documents are *generated* rather than checked in or borrowed from the
vendored `unstructured/example-docs`: the point of these tests is "a real
.docx/.xlsx/.pptx must preview rather than download", and building one with
python-docx/openpyxl/python-pptx (already dependencies via
`unstructured[all-docs]`) keeps the suite self-contained and the fixtures
readable.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest

# --- Sample file builders -------------------------------------------------
#
# Each returns bytes for one file type. Keyed by the extension the upload
# will carry, since the extension is what drives preview routing.


def _docx_bytes() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Vendor Invoice", level=1)
    document.add_paragraph("Invoice number: INV-2026-0042")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Description"
    table.cell(0, 1).text = "Amount"
    table.cell(1, 0).text = "Consulting, Q1"
    table.cell(1, 1).text = "83,880.00"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Summary"
    sheet.append(["Region", "Sales", "Margin"])
    sheet.append(["EMEA", 9467, 1570])
    sheet.append(["APAC", 9060, 2068])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pptx_bytes() -> bytes:
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Quarterly Review"
    slide.placeholders[1].text = "Revenue up 4%"
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 120), "white")
    ImageDraw.Draw(image).text((10, 50), "TAX INVOICE 4221", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _tiff_bytes() -> bytes:
    """A TIFF is a real image that no browser can decode - it has to come
    back as OCR'd elements, not a broken `<img>`."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (120, 60), "white").save(buffer, format="TIFF")
    return buffer.getvalue()


def _csv_bytes() -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["vendor", "amount", "note"])
    # A quoted cell containing the delimiter: unreadable if the preview
    # were to dump the raw text instead of parsing it.
    writer.writerow(["Acme Ltd", "1200.50", "Delivered, late"])
    return buffer.getvalue().encode()


def _eml_bytes() -> bytes:
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "Invoice attached"
    message["From"] = "billing@acme.test"
    message["To"] = "ap@example.test"
    message.set_content("Please find invoice INV-2026-0042 attached.")
    return message.as_bytes()


def _zip_bytes() -> bytes:
    """Genuinely unpreviewable - the endpoint must say so rather than
    starting a download."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("note.txt", "hello")
    return buffer.getvalue()


def _wav_bytes() -> bytes:
    """A tiny silent WAV - previews as a native <audio> player."""
    import struct
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<800h", *([0] * 800)))
    return buffer.getvalue()


SAMPLE_BUILDERS = {
    ".pdf": None,  # filled in below - needs a real PDF, see _pdf_bytes
    ".docx": _docx_bytes,
    ".xlsx": _xlsx_bytes,
    ".pptx": _pptx_bytes,
    ".png": _png_bytes,
    ".tiff": _tiff_bytes,
    ".csv": _csv_bytes,
    ".tsv": lambda: b"vendor\tamount\nAcme Ltd\t1200.50\n",
    ".eml": _eml_bytes,
    ".txt": lambda: b"Invoice INV-2026-0042\nTotal: 83,880.00\n",
    ".md": lambda: b"# Invoice\n\n- number: INV-2026-0042\n- total: 83,880.00\n",
    ".json": lambda: json.dumps({"invoice": "INV-2026-0042", "total": 83880.0}).encode(),
    ".xml": lambda: b"<invoice><number>INV-2026-0042</number></invoice>",
    ".yaml": lambda: b"invoice: INV-2026-0042\ntotal: 83880.0\n",
    ".py": lambda: b"TOTAL = 83880.0  # invoice INV-2026-0042\n",
    ".html": lambda: b"<html><body><h1>Invoice</h1><p>INV-2026-0042</p></body></html>",
    ".svg": lambda: b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"></svg>',
    ".wav": _wav_bytes,
    ".zip": _zip_bytes,
}


def _pdf_bytes() -> bytes:
    """A minimal one-page PDF, written by hand.

    `reportlab` isn't a dependency and the vendored example-docs may not be
    present, so the bytes are assembled directly. It only has to be a PDF a
    parser will open and a browser will render.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 62 >>\nstream\nBT /F1 12 Tf 20 50 Td (Invoice INV-2026-0042) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return bytes(out)


SAMPLE_BUILDERS[".pdf"] = _pdf_bytes


# --- Database-backed fixtures --------------------------------------------


def database_available() -> bool:
    """These tests talk to the real Postgres from docker-compose. Skip
    cleanly rather than fail when it isn't running."""
    try:
        from sqlalchemy import text

        from app.db import engine

        with engine.connect() as connection:
            connection.execute(text("select 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not database_available(),
    reason="needs the project Postgres (docker compose up -d db && alembic upgrade head)",
)


@pytest.fixture
def client():
    """A TestClient over the real app.

    `upload_dir` is left as configured: the upload endpoint writes originals
    there and the preview endpoint reads them back off disk, which is
    exactly the path under test.
    """
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: drives the real UI in a browser; needs dev servers running")
