"""Browser tests for the two surfaces that show a document.

This is the test for the bug as the user experiences it: opening a document
must *show* it, not hand it to the download manager. An HTTP-level test
can't see that, because the download is a decision the browser makes about
bytes it has already received - so this drives a real browser and asserts
that no download fires.

Covered for every supported file type:

  1. the review screen's preview pane  (/review/{job_id})
  2. the Ask page's source cards -> "View file"  (/query)

Running it:

    docker compose up -d db
    alembic upgrade head
    uvicorn app.api:app --port 8000 &
    cd frontend && npm run dev &            # VITE_API_BASE_URL must match
    R2Q_E2E=1 pytest tests/test_preview_ui_e2e.py

    # non-default ports:
    R2Q_E2E=1 R2Q_API_URL=http://localhost:8099 R2Q_APP_URL=http://localhost:5199 pytest ...

Skipped unless `R2Q_E2E=1` and both servers answer, so a normal `pytest`
run stays fast and hermetic.

Two deliberate choices:

* Chromium is launched with `channel="chromium"` (the full build) rather
  than Playwright's default headless shell. The shell ships no PDF viewer,
  so *every* PDF "downloads" there - a false failure that says nothing
  about the app.
* No LLM is involved. Extraction jobs and a chat session are seeded
  directly in the database, so the fixtures under test are the preview and
  the viewer, not the model.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
import uuid

import pytest

from tests.conftest import SAMPLE_BUILDERS, requires_db

API_URL = os.environ.get("R2Q_API_URL", "http://localhost:8000")
APP_URL = os.environ.get("R2Q_APP_URL", "http://localhost:5173")
HAS_OCR = shutil.which("tesseract") is not None


def _server_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = [
    requires_db,
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("R2Q_E2E") != "1", reason="set R2Q_E2E=1 to run browser tests"),
    pytest.mark.skipif(not _playwright_available(), reason="playwright is not installed"),
    pytest.mark.skipif(not _server_up(f"{API_URL}/health"), reason=f"no API at {API_URL}"),
    pytest.mark.skipif(not _server_up(APP_URL), reason=f"no frontend at {APP_URL}"),
]

# extension -> (expected preview kind, the element that proves it rendered)
#
# The element matters as much as the kind: `kind == "image"` with a broken
# `<img>` is still a preview the reviewer can't use.
EXPECTED_RENDER = {
    ".pdf": ("pdf", "iframe"),
    ".png": ("image", "img"),
    ".svg": ("image", "img"),
    ".html": ("html", "iframe"),
    ".wav": ("audio", "audio"),
    ".docx": ("elements", "p, h4, table"),
    ".xlsx": ("elements", "table"),
    ".pptx": ("elements", "p, h4"),
    ".eml": ("elements", "p, h4"),
    ".csv": ("elements", "table"),
    ".tsv": ("elements", "table"),
    ".tiff": ("elements", "p, h4"),
    ".txt": ("text", "pre"),
    ".md": ("text", "pre"),
    ".json": ("text", "pre"),
    ".xml": ("text", "pre"),
    ".yaml": ("text", "pre"),
    ".py": ("text", "pre"),
    # No preview exists for an archive. The requirement is still that it
    # doesn't download on its own - it offers a Download button instead.
    ".zip": ("unsupported", "[data-testid=preview-fallback]"),
}

OCR_EXTS = {".tiff"}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Upload one file per type, put each into review, and build a chat
    session whose answer cites every one of them.

    Job rows are written straight to `awaiting_review` rather than waiting
    on the worker: extraction calls an LLM, and the viewer doesn't care what
    it extracted.
    """
    from app.db import SessionLocal
    from app.models import ChatMessage, ChatSession, Job

    directory = tmp_path_factory.mktemp("e2e-samples")
    files = []
    for ext, builder in SAMPLE_BUILDERS.items():
        path = directory / f"sample{ext}"
        path.write_bytes(builder())
        files.append((ext, path))

    # Multipart upload, hand-rolled to keep the test dependency-free.
    boundary = "----r2qE2EBoundary"
    body = bytearray()
    for _ext, path in files:
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        f"{API_URL}/documents",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        uploaded = json.load(response)["documents"]

    doc_by_ext = {"." + d["filename"].rsplit(".", 1)[1]: d["document_id"] for d in uploaded}
    job_by_ext = {"." + d["filename"].rsplit(".", 1)[1]: d["job_id"] for d in uploaded}

    session = SessionLocal()
    chat_id = None
    try:
        for job_id in job_by_ext.values():
            job = session.get(Job, uuid.UUID(job_id))
            job.status = "awaiting_review"
            job.result = {
                "matched_schema_id": None,
                "schema_version_id": None,
                "proposed_schema_fields": None,
                "extracted_data": [{"reference": "INV-2026-0042"}],
                "data_confidence": [{"reference": 0.95}],
                "dedup_match": None,
                "suggested_topic_ids": [],
                "proposed_new_topics": [],
                "chunks": [],
            }

        chat = ChatSession(title="Every file type")
        session.add(chat)
        session.flush()
        chat_id = chat.id
        session.add(
            ChatMessage(session_id=chat.id, position=0, role="user", content="Show me one of each file type")
        )
        session.add(
            ChatMessage(
                session_id=chat.id,
                position=1,
                role="assistant",
                content="Here is one source per uploaded file type.",
                message_metadata={
                    "routing_used": "rag",
                    "sql": None,
                    "rows": None,
                    "topic_ids_used": [],
                    "sources": [
                        {
                            "document_id": document_id,
                            "chunk_id": str(uuid.uuid4()),
                            "snippet": f"A passage from sample{ext}",
                            "filename": f"sample{ext}",
                            "page_number": 1,
                        }
                        for ext, document_id in doc_by_ext.items()
                    ],
                },
            )
        )
        session.commit()
    finally:
        session.close()

    yield {"documents": doc_by_ext, "jobs": job_by_ext, "chat_id": str(chat_id)}

    session = SessionLocal()
    try:
        chat = session.get(ChatSession, chat_id)
        if chat is not None:
            session.delete(chat)
            session.commit()
    finally:
        session.close()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        # The full Chromium build, not the default headless shell: the shell
        # has no PDF viewer, so it downloads every PDF regardless of what
        # the app does.
        instance = playwright.chromium.launch(channel="chromium")
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def _tab(browser):
    """One page for the whole module.

    A fresh context per test meant 40-odd cold starts against the Vite dev
    server, which was both slow and flaky - unrelated file types would time
    out waiting for the viewer purely from contention. The per-test state
    that actually matters (recorded downloads and page errors) is cleared by
    the `page` fixture instead.
    """
    context = browser.new_context(viewport={"width": 1440, "height": 960}, accept_downloads=True)
    tab = context.new_page()
    tab.downloads = []
    tab.console_errors = []
    tab.on("download", lambda d: tab.downloads.append(d.suggested_filename))
    tab.on("pageerror", lambda e: tab.console_errors.append(str(e)))
    yield tab
    context.close()


@pytest.fixture
def page(_tab):
    """The shared page, with its recorded downloads and errors reset.

    `page.downloads` staying empty is the assertion these tests exist for:
    a preview that downloads is the bug.
    """
    _tab.downloads.clear()
    _tab.console_errors.clear()
    yield _tab
    # Leave no modal or route handler behind for the next test.
    _tab.unroute_all()
    _tab.keyboard.press("Escape")


def _assert_rendered(page, ext, viewer):
    """Shared assertions for one file type in whichever surface it's in."""
    expected_kind, proof_selector = EXPECTED_RENDER[ext]

    if ext in OCR_EXTS and not HAS_OCR:
        # No OCR binary here: the viewer must still show the explained
        # fallback rather than download the file.
        assert viewer.get_attribute("data-preview-kind") == "unsupported"
        assert viewer.locator("[data-testid=preview-fallback]").count() == 1
    else:
        assert viewer.get_attribute("data-preview-kind") == expected_kind, (
            f"sample{ext} rendered as {viewer.get_attribute('data-preview-kind')!r}"
        )
        assert viewer.locator(proof_selector).first.is_visible(), (
            f"sample{ext} reported kind={expected_kind} but rendered no {proof_selector}"
        )

    # The whole point.
    assert page.downloads == [], f"sample{ext} triggered a download: {page.downloads}"
    assert page.console_errors == [], f"sample{ext} raised: {page.console_errors}"


@pytest.mark.parametrize("ext", sorted(EXPECTED_RENDER))
def test_review_page_previews_rather_than_downloads(ext, seeded, page):
    """Surface 1: the review screen's side-by-side preview pane."""
    page.goto(f"{APP_URL}/review/{seeded['jobs'][ext]}", wait_until="domcontentloaded")
    viewer = page.locator("[data-testid=document-viewer]")
    viewer.wait_for(timeout=60_000)
    # Wait for the preview fetch to settle out of its skeleton state.
    page.wait_for_function(
        "() => document.querySelector('[data-testid=document-viewer]')"
        "?.getAttribute('data-preview-kind') !== 'unknown'",
        timeout=60_000,
    )
    page.wait_for_timeout(600)
    _assert_rendered(page, ext, viewer)


@pytest.mark.parametrize("ext", sorted(EXPECTED_RENDER))
def test_ask_page_view_file_previews_rather_than_downloads(ext, seeded, page):
    """Surface 2: the Ask page's sources modal -> "View file".

    That link used to be a plain `<a href>` to the raw file endpoint, so
    every type the browser couldn't render downloaded on click.
    """
    page.goto(f"{APP_URL}/query", wait_until="domcontentloaded")

    # Open the seeded conversation from the sidebar.
    page.locator("aside").get_by_text("Every file type").first.click()
    page.wait_for_selector("button:has-text(' source')", timeout=30_000)

    # Sources are behind a button now, not inlined under the answer.
    page.locator("section button:has-text(' source')").first.click()
    dialog = page.locator("[role=dialog]")
    dialog.wait_for(timeout=15_000)

    # One card per source, stacked; pick this file type's card by filename.
    card = dialog.locator("article").filter(has_text=f"sample{ext}").first
    card.get_by_role("button", name="View file").click()

    viewer = page.locator("[role=dialog] [data-testid=document-viewer]")
    viewer.wait_for(timeout=60_000)
    page.wait_for_function(
        "() => document.querySelector('[role=dialog] [data-testid=document-viewer]')"
        "?.getAttribute('data-preview-kind') !== 'unknown'",
        timeout=60_000,
    )
    page.wait_for_timeout(600)
    _assert_rendered(page, ext, viewer)


def test_sources_are_behind_a_button_not_inlined(seeded, page):
    """The answer should not be buried under a stack of snippet boxes."""
    page.goto(f"{APP_URL}/query", wait_until="domcontentloaded")
    page.locator("aside").get_by_text("Every file type").first.click()
    page.wait_for_selector("section button:has-text(' source')", timeout=30_000)

    # Nothing is open until the button is pressed.
    assert page.locator("[role=dialog]").count() == 0
    button = page.locator("section button:has-text(' source')").first
    assert f"{len(SAMPLE_BUILDERS)} sources" in button.inner_text()

    button.click()
    dialog = page.locator("[role=dialog]")
    dialog.wait_for()
    assert dialog.locator("article").count() == len(SAMPLE_BUILDERS)


def test_download_button_is_the_only_thing_that_downloads(seeded, page):
    """The explicit escape hatch still has to work."""
    page.goto(f"{APP_URL}/review/{seeded['jobs']['.docx']}", wait_until="domcontentloaded")
    viewer = page.locator("[data-testid=document-viewer]")
    viewer.wait_for(timeout=60_000)
    page.wait_for_timeout(1500)
    assert page.downloads == []

    with page.expect_download(timeout=30_000) as download:
        viewer.get_by_text("Download", exact=True).first.click()
    assert download.value.suggested_filename == "sample.docx"


def test_new_chat_creates_nothing_until_a_question_is_asked(seeded, page):
    """The draft-session promise, from the UI side."""

    def chat_count():
        with urllib.request.urlopen(f"{API_URL}/chats", timeout=30) as response:
            return len(json.load(response))

    page.goto(f"{APP_URL}/query", wait_until="domcontentloaded")

    # A fresh load already *is* a draft, so "New chat" starts disabled -
    # there is nothing for it to do. Open a saved conversation first so the
    # button becomes meaningful.
    new_chat = page.get_by_role("button", name="New chat")
    assert new_chat.is_disabled(), "a fresh page should already be an unsaved draft"
    assert page.get_by_text("Unsaved").count() == 1

    page.locator("aside").get_by_text("Every file type").first.click()
    page.wait_for_selector("section button:has-text(' source')", timeout=30_000)
    assert page.get_by_text("Unsaved").count() == 0

    before = chat_count()
    new_chat.click()
    page.wait_for_timeout(1000)
    assert page.get_by_text("Unsaved").count() == 1, "the draft chat isn't shown in the sidebar"
    assert page.locator("section button:has-text(' source')").count() == 0, "the transcript wasn't cleared"
    assert chat_count() == before, "opening a new chat wrote a row to the database"


def test_thinking_animation_is_shown_while_a_query_runs(seeded, page):
    """The Lottie indicator replaces the old static "Thinking…" line.

    `/query` is stubbed and deliberately held open, so the in-flight state
    is observable rather than raced against: the handler captures the route
    and doesn't resolve it until the assertions have run. No LLM involved -
    this is about the loading state, not the answer.
    """
    held = []
    # Scoped to the API origin on purpose: the app's own route is also
    # `/query`, so a bare `**/query` pattern intercepts the page navigation
    # and the test hangs on `goto` instead of on the request under test.
    page.route(f"{API_URL}/query", lambda route: held.append(route))

    page.goto(f"{APP_URL}/query", wait_until="domcontentloaded")
    page.fill("textarea", "How many invoices?")
    page.get_by_role("button", name="Ask", exact=True).click()

    status = page.locator("[role=status]")
    status.wait_for(timeout=20_000)
    assert "Thinking" in status.inner_text()
    # lottie-react draws the animation as an inline SVG, loaded on demand
    # (the engine is a lazy chunk), so give it a moment to arrive.
    page.wait_for_function("() => !!document.querySelector('[role=status] svg')", timeout=30_000)

    # Release the request and check the indicator goes away again.
    assert held, "the /query request was never intercepted"
    held[0].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "answer": "Two invoices.",
                "routing_used": "rag",
                "sql": None,
                "rows": None,
                "sources": [],
                "topic_ids_used": [],
                "chat_id": seeded["chat_id"],
            }
        ),
    )
    page.wait_for_selector("[role=status]", state="detached", timeout=20_000)
    assert page.get_by_text("Two invoices.").count() == 1
