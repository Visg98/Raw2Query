"""Local filesystem storage for uploaded originals (decision: no object storage
service for the prototype, `./data/uploads/`)."""

import hashlib
import uuid
from pathlib import Path

from app.config import get_settings

settings = get_settings()


def _upload_root() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def compute_hash(content: bytes) -> str:
    """sha256 hex digest, recorded on every document. No longer a dedup key -
    byte-identical re-uploads are accepted as separate documents (migration
    0002) - it's kept as a cheap "have I seen these bytes before?" marker."""
    return hashlib.sha256(content).hexdigest()


def write_file(content: bytes, filename: str) -> str:
    """Writes the file to disk and returns its storage_path. Every upload gets
    its own uuid-named copy, so re-uploading the same bytes writes a second
    file rather than being folded into the first."""
    root = _upload_root()
    suffix = Path(filename).suffix
    dest = root / f"{uuid.uuid4()}{suffix}"
    dest.write_bytes(content)
    return str(dest)


def read_file(storage_path: str) -> bytes:
    return Path(storage_path).read_bytes()


def delete_file(storage_path: str) -> None:
    """Removes an upload's original from disk. Best effort: the row is
    already gone by the time this runs, so a missing or unremovable file is
    nothing the caller can act on - it just leaves an orphan on disk rather
    than failing a delete the user already saw succeed."""
    try:
        Path(storage_path).unlink(missing_ok=True)
    except OSError:
        pass
