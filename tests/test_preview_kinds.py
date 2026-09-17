"""Every file type the pipeline accepts must resolve to a renderable preview.

This is the regression net for the original bug: the review screen pointed
an `<iframe>` at the raw bytes and let the browser decide, so every type the
browser has no viewer for (.docx, .xlsx, .pptx, .csv, anything uploaded as
`application/octet-stream`) *downloaded* instead of appearing on screen.

`preview_kind` is now the single place that decision is made, so the
important property to pin down is exhaustive: for every extension
`unstructured` will partition - i.e. everything the upload endpoint can
usefully accept - the answer must be a kind the UI can actually render, and
it must be the *right* one.

No database, no network, no LLM: these run anywhere.
"""

from __future__ import annotations

import pytest

from app.preview import preview_kind, resolve_media_type

# Kinds the frontend has a real renderer for (see DocumentViewer.jsx).
RENDERABLE_KINDS = {"pdf", "image", "audio", "video", "text", "html", "elements"}

# The expected renderer for every extension in `unstructured`'s partitionable
# registry, spelled out rather than derived - a table that has to be edited
# by hand is the point, because silently re-classifying a type is exactly
# the regression this guards.
EXPECTED_KIND_BY_EXT = {
    # Browser-native documents and images
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".bmp": "image",
    ".html": "html",
    ".htm": "html",
    # Images with no browser decoder -> OCR'd server-side
    ".tiff": "elements",
    ".heic": "elements",
    # Office / mail / ebook formats -> converted server-side
    ".docx": "elements",
    ".doc": "elements",
    ".odt": "elements",
    ".rtf": "elements",
    ".pptx": "elements",
    ".ppt": "elements",
    ".xlsx": "elements",
    ".xls": "elements",
    ".eml": "elements",
    ".p7s": "elements",
    ".msg": "elements",
    ".epub": "elements",
    # Delimited text -> parsed into a table server-side
    ".csv": "elements",
    ".tsv": "elements",
    ".tab": "elements",
    # Plain text and markup
    ".txt": "text",
    ".text": "text",
    ".md": "text",
    ".rst": "text",
    ".org": "text",
    ".log": "text",
    ".json": "text",
    ".ndjson": "text",
    ".xml": "text",
    ".yaml": "text",
    ".yml": "text",
    # Source code: all TXT to `unstructured`, all text to us
    ".c": "text",
    ".cc": "text",
    ".cpp": "text",
    ".cxx": "text",
    ".cs": "text",
    ".go": "text",
    ".java": "text",
    ".js": "text",
    ".php": "text",
    ".py": "text",
    ".rb": "text",
    ".swift": "text",
    ".ts": "text",
    # Media -> a native player, not a transcript
    ".mp3": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".m4a": "audio",
    ".oga": "audio",
    ".ogg": "audio",
    ".opus": "audio",
    ".webm": "video",
}


def partitionable_extensions() -> set[str]:
    """Every extension `unstructured` claims it can partition.

    Read from the library rather than copied, so upgrading `unstructured`
    and gaining a new type fails this suite instead of silently shipping a
    file type the review screen would download.
    """
    from unstructured.file_utils.model import FileType

    return {
        ext
        for file_type in FileType
        for ext in file_type._extensions  # noqa: SLF001 - no public accessor exists
        if file_type.is_partitionable
    }


def test_expected_kind_table_covers_every_partitionable_extension():
    """The table above must not drift behind the library."""
    missing = partitionable_extensions() - set(EXPECTED_KIND_BY_EXT)
    assert not missing, (
        f"`unstructured` can partition {sorted(missing)}, but EXPECTED_KIND_BY_EXT "
        "doesn't say how to preview them - add them here and to app/preview.py"
    )


@pytest.mark.parametrize("ext", sorted(partitionable_extensions()))
def test_every_supported_type_previews_rather_than_downloads(ext):
    """The core guarantee: no supported type falls through to a download."""
    filename = f"sample{ext}"
    kind = preview_kind(filename, resolve_media_type(filename, None))
    assert kind in RENDERABLE_KINDS, f"{ext} would have no renderer (kind={kind!r})"
    assert kind == EXPECTED_KIND_BY_EXT[ext], f"{ext} should preview as {EXPECTED_KIND_BY_EXT[ext]}"


@pytest.mark.parametrize("ext", sorted(partitionable_extensions()))
def test_uppercase_extensions_resolve_identically(ext):
    """Windows uploads arrive as INVOICE.PDF; case must not change anything."""
    assert preview_kind(f"SAMPLE{ext.upper()}") == preview_kind(f"sample{ext}")


class TestMediaTypeResolution:
    """`documents.mime_type` holds the browser's upload-time guess, which is
    routinely wrong. Resolving from the filename instead is half the fix for
    the download bug - `application/octet-stream` means "unknown bytes, save
    me" to every browser, so an octet-stream PDF downloaded even though the
    browser had a perfectly good viewer for it."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("invoice.pdf", "application/pdf"),
            ("scan.png", "image/png"),
            ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ("mail.msg", "application/vnd.ms-outlook"),
            ("mail.eml", "message/rfc822"),
            ("notes.md", "text/markdown"),
        ],
    )
    def test_resolved_from_filename(self, filename, expected):
        assert resolve_media_type(filename, None) == expected

    @pytest.mark.parametrize("filename", ["invoice.pdf", "scan.jpg", "report.docx"])
    def test_filename_overrides_a_useless_stored_type(self, filename):
        """The exact case that made PDFs download."""
        resolved = resolve_media_type(filename, "application/octet-stream")
        assert resolved != "application/octet-stream"
        assert preview_kind(filename, resolved) in RENDERABLE_KINDS

    def test_stored_type_used_when_the_filename_says_nothing(self):
        assert resolve_media_type("blob", "application/pdf") == "application/pdf"

    def test_falls_back_to_octet_stream_when_nothing_is_known(self):
        assert resolve_media_type("blob", None) == "application/octet-stream"


class TestKindFromMediaTypeAlone:
    """Files uploaded with no extension still have to be routed somewhere."""

    @pytest.mark.parametrize(
        ("media_type", "expected"),
        [
            ("application/pdf", "pdf"),
            ("image/png", "image"),
            ("image/svg+xml", "image"),
            ("text/html", "html"),
            ("text/plain", "text"),
            ("application/json", "text"),
            ("audio/mpeg", "audio"),
            ("video/mp4", "video"),
            # No browser decoder: OCR instead of a broken <img>.
            ("image/tiff", "elements"),
            ("image/heic", "elements"),
            # Unknown bytes: let partition() sniff them.
            ("application/octet-stream", "elements"),
            ("", "elements"),
        ],
    )
    def test_kind_from_media_type(self, media_type, expected):
        assert preview_kind("blob", media_type) == expected

    def test_charset_parameter_is_ignored(self):
        assert preview_kind("blob", "text/plain; charset=utf-8") == "text"


class TestExplicitlyUnpreviewable:
    """An archive genuinely has no preview. Saying so up front is the point:
    the old behaviour was to start a download nobody asked for."""

    @pytest.mark.parametrize("ext", [".zip", ".tar", ".7z", ".exe"])
    def test_archives_and_binaries_are_declared_unsupported(self, ext):
        assert preview_kind(f"bundle{ext}") == "unsupported"
