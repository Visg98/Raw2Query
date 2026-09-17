import { apiFetch, fileUrl } from "./client";

/**
 * @param {File[]} files
 * @param {{ schemaId?: string, topicIds?: string[] }} [hints]
 */
export function uploadDocuments(files, hints = {}) {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  if (hints.schemaId) form.append("schema_id", hints.schemaId);
  (hints.topicIds || []).forEach((id) => form.append("topic_ids", id));
  return apiFetch("/documents", { method: "POST", form });
}

export function listDocuments({ topicId, schemaId, status } = {}) {
  return apiFetch("/documents", { params: { topic_id: topicId, schema_id: schemaId, status } });
}

export function getDocument(documentId) {
  return apiFetch(`/documents/${documentId}`);
}

/**
 * The raw original, served inline. Only point browser-native renderers at
 * this (`<iframe>` for PDF/HTML, `<img>` for images) — see
 * `getDocumentPreview` for why every other type has to go through the
 * preview endpoint instead.
 */
export function getDocumentFileUrl(documentId) {
  return fileUrl(`/documents/${documentId}/file`);
}

/** Explicit "save a copy" URL — the only place that sends
 * `Content-Disposition: attachment`. */
export function getDocumentDownloadUrl(documentId) {
  return fileUrl(`/documents/${documentId}/file?download=1`);
}

/**
 * A renderable preview: `{ kind, media_type, text?, elements?, ... }`.
 *
 * `kind` tells the viewer what to do. `pdf`/`image`/`html` are rendered
 * from `getDocumentFileUrl` by the browser itself; `text` and `elements`
 * carry their content here because handing raw .docx/.xlsx/.csv bytes to an
 * `<iframe>` downloads the file instead of showing it, whatever
 * `Content-Disposition` says.
 */
export function getDocumentPreview(documentId) {
  return apiFetch(`/documents/${documentId}/preview`);
}

/**
 * Erases one document and everything derived from it — its jobs, chunks and
 * extracted records, confirmed or not — plus the uploaded original.
 *
 * Distinct from `deleteBatch`, which spares already-confirmed documents
 * because their records are live queryable data. This one does not, so the
 * caller must confirm with the user first.
 *
 * Resolves to `{ deleted_document_id, deleted_job_ids, deleted_record_count,
 * deleted_chunk_count, batch_deleted, emptied_schema_id }`.
 */
export function deleteDocument(documentId) {
  return apiFetch(`/documents/${documentId}`, { method: "DELETE" });
}
