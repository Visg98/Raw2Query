"""Renderable previews of original uploads.

Why this module exists
----------------------
The review screen and the Ask page's source cards both need to *show* an
original file. The obvious implementation - point an `<iframe>` at the raw
bytes - only works for the handful of types a browser ships a viewer for:
PDF, common web images, plain text, HTML. For every other type the
extraction pipeline happily accepts (.docx, .xlsx, .pptx, .csv, .eml, a
multi-page .tiff scan) the iframe navigation turns into a *download*.

`Content-Disposition: inline` does not change that. The header expresses
attachment-vs-inline intent; it cannot hand the browser a renderer it does
not have. So those types are converted here, server-side, into a structured
element list the frontend renders as real DOM.

The converter is `unstructured.partition` - already the extraction
pipeline's parser (app/pipeline/extract.py), so the preview shows a reviewer
exactly the text the extraction saw rather than a second, differently-lossy
rendering of the same file.

Two smaller fixes live here too:

* `resolve_media_type` derives the type from the filename instead of
  trusting `documents.mime_type`. That column holds the browser-supplied
  `UploadFile.content_type`, which is routinely empty or
  `application/octet-stream` for drag-and-drop uploads - and an
  octet-stream PDF downloads instead of rendering even though the browser
  has a perfectly good PDF viewer.
* `preview_kind` decides the renderer from the extension as well, so the
  frontend never has to guess from an unreliable type either.
"""

from __future__ import annotations

import csv
import io
import mimetypes
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

PreviewKind = Literal["pdf", "image", "audio", "video", "text", "html", "elements", "unsupported"]

# Types the browser renders natively and faithfully, so the frontend streams
# the untouched original from /documents/{id}/file for these.
_PDF_EXTS = {".pdf"}
# Only formats with a browser decoder. `.tiff`/`.heic` are images too but
# belong in _ELEMENT_EXTS - see there.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".apng", ".bmp", ".ico"}
_HTML_EXTS = {".html", ".htm", ".xhtml"}

# Media, played with <audio>/<video>. `unstructured` accepts these and
# transcribes them, but a transcript is not a preview of a sound file - the
# reviewer wants to hear it, and every browser can already play these.
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".oga", ".ogg", ".opus", ".aac"}
_VIDEO_EXTS = {".webm", ".mp4", ".mov", ".m4v", ".ogv"}

# Decodable as characters: served as text and rendered in a <pre>. Kept
# separate from _IMAGE_EXTS/_PDF_EXTS because the browser would *download*
# several of these (.json and .csv in Chrome, anything with an unknown
# extension everywhere) rather than display them.
#
# The source-code extensions are here because `unstructured` maps them all
# to its TXT file type, so the pipeline accepts them and the review screen
# has to show them.
_TEXT_EXTS = {
    ".txt",
    ".text",
    ".md",
    ".markdown",
    ".rst",
    ".org",
    ".log",
    ".json",
    ".jsonl",
    ".ndjson",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".xml",
    ".sql",
    ".vtt",
    ".srt",
    # Source code, all of it TXT to `unstructured`.
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".php",
    ".py",
    ".rb",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
}

# Tabular text: parsed server-side rather than dumped into a <pre>, because
# quoted, embedded-comma CSV is unreadable as raw text and trivially correct
# to parse with the stdlib. `.tab` is `unstructured`'s other TSV spelling.
_DELIMITED_EXTS = {".csv": ",", ".tsv": "\t", ".tab": "\t", ".psv": "|"}

# Everything `unstructured` can parse but no browser can render. These go
# through partition() into an element list.
_ELEMENT_EXTS = {
    ".docx",
    ".doc",
    ".odt",
    ".rtf",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".ods",
    ".eml",
    ".p7s",  # a signed email; `unstructured` reads it as EML
    ".msg",
    ".epub",
    # Real image formats with no browser decoder - partition() OCRs them.
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

# Accepted by upload but not previewable by anyone, so say so immediately
# rather than spending a partition() attempt to find out.
_UNSUPPORTED_EXTS = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".exe", ".bin", ".dll", ".so"}

# The media types a browser will actually decode, for files that arrive with
# a usable type but no extension. `image/tiff` and `image/heic` are
# deliberately absent: no browser decodes them.
_BROWSER_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/svg+xml",
    "image/apng",
    "image/bmp",
    "image/x-icon",
    "image/vnd.microsoft.icon",
}

# Extension -> media type for the types `mimetypes` doesn't know on every
# platform. Without these an .xlsx or .msg comes back as octet-stream.
_EXTRA_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".org": "text/org",
    ".log": "text/plain",
    ".text": "text/plain",
    ".tab": "text/tab-separated-values",
    ".p7s": "message/rfc822",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".webm": "video/webm",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".msg": "application/vnd.ms-outlook",
    ".eml": "message/rfc822",
    ".epub": "application/epub+zip",
}

# Preview caps. A preview is a human sanity-check next to the extracted
# fields, not a document viewer of record - a 900-page PDF's OCR text does
# not need to reach the browser in one JSON response.
MAX_ELEMENTS = 400
MAX_ELEMENT_CHARS = 4_000
MAX_TEXT_CHARS = 200_000
MAX_TABLE_ROWS = 300
MAX_TABLE_COLS = 40


def _ext(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def resolve_media_type(filename: str, stored_mime_type: str | None) -> str:
    """The media type to actually serve the file as.

    Filename first, `documents.mime_type` (the browser's upload-time guess)
    only as a fallback: that column is regularly empty or
    `application/octet-stream`, and octet-stream forces a download even for
    a PDF the browser could have rendered.
    """
    ext = _ext(filename)
    if ext in _EXTRA_MEDIA_TYPES:
        return _EXTRA_MEDIA_TYPES[ext]
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed
    if stored_mime_type and stored_mime_type != "application/octet-stream":
        return stored_mime_type
    return "application/octet-stream"


def preview_kind(filename: str, media_type: str | None = None) -> PreviewKind:
    """Which renderer the frontend should use.

    Extension-driven, with the media type as a secondary signal for files
    uploaded without a useful extension.
    """
    ext = _ext(filename)
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _HTML_EXTS:
        return "html"
    if ext in _DELIMITED_EXTS or ext in _ELEMENT_EXTS:
        return "elements"
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _UNSUPPORTED_EXTS:
        return "unsupported"

    mt = (media_type or "").split(";")[0].strip().lower()
    if mt == "application/pdf":
        return "pdf"
    if mt in {"text/html", "application/xhtml+xml"}:
        return "html"
    if mt.startswith("image/"):
        # An image type with no browser decoder still has to go through OCR.
        return "image" if mt in _BROWSER_IMAGE_MIME_TYPES else "elements"
    if mt.startswith("audio/"):
        return "audio"
    if mt.startswith("video/"):
        return "video"
    if mt.startswith("text/") or mt in {"application/json", "application/xml", "application/x-ndjson"}:
        return "text"
    # No extension, an unknown extension, or octet-stream: partition()
    # sniffs the bytes, and `unsupported` is the answer only once it has
    # actually failed to read them.
    return "elements"


class _TableHtmlParser(HTMLParser):
    """`unstructured` exposes a parsed table only as an HTML string
    (`metadata.text_as_html`). Reduce it back to rows of cell text so the
    frontend can build a real <table> - nothing derived from an uploaded
    file is ever handed to `dangerouslySetInnerHTML`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:  # pragma: no cover - flush an unterminated <tr>
        super().close()
        if self._row:
            self.rows.append(self._row)
            self._row = None


def _clip_table(rows: list[list[str]]) -> tuple[list[list[str]], bool]:
    clipped = [r[:MAX_TABLE_COLS] for r in rows[:MAX_TABLE_ROWS]]
    truncated = len(rows) > MAX_TABLE_ROWS or any(len(r) > MAX_TABLE_COLS for r in rows)
    return clipped, truncated


def _table_element(html: str | None, fallback_text: str) -> dict[str, Any]:
    if html:
        parser = _TableHtmlParser()
        parser.feed(html)
        parser.close()
        if parser.rows:
            rows, truncated = _clip_table(parser.rows)
            return {"type": "table", "rows": rows, "truncated": truncated}
    return {"type": "text", "text": fallback_text[:MAX_ELEMENT_CHARS]}


# `unstructured` element class name -> preview element type. Anything not
# listed renders as ordinary text, which is the right default for a new or
# exotic element class.
_ELEMENT_TYPE_MAP = {
    "Title": "heading",
    "Header": "heading",
    "Section": "heading",
    "ListItem": "list_item",
    "PageBreak": "page_break",
}


def read_delimited(path: str, delimiter: str) -> list[dict[str, Any]]:
    """CSV/TSV preview: one table element, parsed with the stdlib so quoted
    cells containing the delimiter survive."""
    raw = Path(path).read_bytes()[: MAX_TEXT_CHARS * 2]
    text_body = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text_body), delimiter=delimiter)
    try:
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    except csv.Error:
        # A stray NUL or an unbalanced quote - fall back to showing the text.
        return [{"type": "text", "text": text_body[:MAX_ELEMENT_CHARS]}]
    if not rows:
        return []
    clipped, truncated = _clip_table(rows)
    return [{"type": "table", "rows": clipped, "truncated": truncated}]


def read_text(path: str) -> tuple[str, bool]:
    """Decode a text file for display. `errors="replace"` rather than a
    failure: a mostly-readable file with a couple of bad bytes is still worth
    showing a reviewer."""
    raw = Path(path).read_bytes()
    body = raw.decode("utf-8", errors="replace")
    if len(body) > MAX_TEXT_CHARS:
        return body[:MAX_TEXT_CHARS], True
    return body, False


def build_element_preview(path: str) -> tuple[list[dict[str, Any]], bool]:
    """Partition the file and reduce it to renderable preview elements.

    Returns `(elements, truncated)`. Raises whatever `partition` raises for a
    format it cannot handle - the caller turns that into an `unsupported`
    preview rather than a 500.
    """
    # Imported lazily: `unstructured.partition.auto` pulls in the whole
    # parser stack (and its model downloads), which a text or PDF preview
    # never needs.
    from unstructured.documents.elements import Table
    from unstructured.partition.auto import partition

    elements = partition(filename=path)
    out: list[dict[str, Any]] = []
    truncated = len(elements) > MAX_ELEMENTS

    for element in elements[:MAX_ELEMENTS]:
        text_body = str(element).strip()
        if isinstance(element, Table):
            out.append(_table_element(getattr(element.metadata, "text_as_html", None), text_body))
            continue
        if not text_body:
            continue
        kind = _ELEMENT_TYPE_MAP.get(type(element).__name__, "text")
        item: dict[str, Any] = {"type": kind, "text": text_body[:MAX_ELEMENT_CHARS]}
        page = getattr(element.metadata, "page_number", None)
        if page is not None:
            item["page_number"] = page
        out.append(item)

    truncated = truncated or any(e.get("truncated") for e in out)
    return out, truncated


def delimiter_for(filename: str) -> str | None:
    return _DELIMITED_EXTS.get(_ext(filename))
