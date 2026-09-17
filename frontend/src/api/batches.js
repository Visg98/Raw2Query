import { apiFetch } from "./client";

export function getBatch(batchId) {
  return apiFetch(`/batches/${batchId}`);
}

export function confirmAllClean(batchId) {
  return apiFetch(`/batches/${batchId}/confirm-all-clean`, { method: "POST" });
}

/**
 * Discards a batch's unconfirmed documents, jobs and uploaded originals.
 * Already-confirmed documents are kept and come back in
 * `kept_document_ids` — their records and chunks are live data.
 * @returns {Promise<{deleted_document_ids: string[], deleted_job_ids: string[], kept_document_ids: string[], batch_deleted: boolean}>}
 */
export function deleteBatch(batchId) {
  return apiFetch(`/batches/${batchId}`, { method: "DELETE" });
}
