import { apiFetch } from "./client";

export function searchTopics(q = "") {
  return apiFetch("/topics", { params: { q } });
}

/** Resolves specific topics by id, bypassing the typeahead search window —
 * used to label already-selected chips whose topic isn't in the current
 * search results. */
export function getTopicsByIds(ids = []) {
  if (!ids.length) return Promise.resolve([]);
  return apiFetch("/topics", { params: { ids } });
}

/** @param {{ name: string, description?: string }} body */
export function createTopic(body) {
  return apiFetch("/topics", { method: "POST", json: { name: body.name, description: body.description ?? null } });
}

/**
 * Deletes a topic and unlinks it everywhere — document links, the
 * denormalized copies on chunks, per-batch upload hints, staged job
 * suggestions. Documents and their data survive: a topic is a label, not a
 * container.
 *
 * A document the delete leaves with no topics at all comes back tagged
 * "Uncategorized" (counted in `uncategorized_document_count`). Deleting
 * "Uncategorized" itself is a 400 — it's the fallback everything untagged
 * lands on.
 *
 * @returns {Promise<{unlinked_document_count: number, uncategorized_document_count: number, resynced_chunk_count: number, cleared_job_count: number}>}
 */
export function deleteTopic(topicId) {
  return apiFetch(`/topics/${topicId}`, { method: "DELETE" });
}
