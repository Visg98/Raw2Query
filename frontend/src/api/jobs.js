import { API_BASE_URL, apiFetch } from "./client";

export function getJob(jobId) {
  return apiFetch(`/jobs/${jobId}`);
}

export function getJobReview(jobId) {
  return apiFetch(`/jobs/${jobId}/review`);
}

/**
 * @param {string} jobId
 * `extractedData` is the whole table, sent whole rather than per object: the
 * table can add and delete rows as well as edit cells, and a per-index patch
 * can't express a deletion without both sides agreeing on indices they have
 * no way to keep in sync. The server keeps each confidence score only where
 * that exact cell's value is unchanged, so an untouched draft resent on every
 * save keeps its flags while an edited cell loses its own.
 *
 * Unknown keys are rejected with a 422, so a retired field name fails loudly
 * instead of being accepted and dropped.
 *
 * @param {{
 *   extractedData?: object[],
 *   proposedSchemaFields?: object[],
 *   matchedSchemaId?: string,
 *   topicIds?: string[],
 *   newTopicNames?: string[],
 * }} patch
 */
export function patchJobReview(jobId, patch) {
  const body = {};
  if (patch.extractedData !== undefined) body.extracted_data = patch.extractedData;
  if (patch.proposedSchemaFields !== undefined) body.proposed_schema_fields = patch.proposedSchemaFields;
  if (patch.matchedSchemaId !== undefined) body.matched_schema_id = patch.matchedSchemaId;
  if (patch.topicIds !== undefined) body.topic_ids = patch.topicIds;
  if (patch.newTopicNames !== undefined) body.new_topic_names = patch.newTopicNames;
  return apiFetch(`/jobs/${jobId}/review`, { method: "PATCH", json: body });
}

/** @param {{ dedupDecision?: "skip"|"keep_both"|"replace", saveSchemaAs?: string }} [body] */
export function confirmJob(jobId, body = {}) {
  const json = {};
  if (body.dedupDecision !== undefined) json.dedup_decision = body.dedupDecision;
  if (body.saveSchemaAs !== undefined) json.save_schema_as = body.saveSchemaAs;
  return apiFetch(`/jobs/${jobId}/confirm`, { method: "POST", json });
}

export function rejectJob(jobId) {
  return apiFetch(`/jobs/${jobId}/reject`, { method: "POST" });
}

/**
 * Re-runs extraction on the same job with the reviewer's correction folded
 * into the prompt. Replaces the staged table, so any unsaved or saved cell
 * edits are lost — the caller confirms first.
 */
export function reextractJob(jobId, feedback) {
  return apiFetch(`/jobs/${jobId}/reextract`, { method: "POST", json: { feedback } });
}

export function retryJob(jobId) {
  return apiFetch(`/jobs/${jobId}/retry`, { method: "POST" });
}

export function jobEventsUrl(jobId) {
  return new URL(`/jobs/${jobId}/events`, API_BASE_URL).toString();
}
