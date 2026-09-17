import { apiFetch, ApiError } from "./client";

export function listSchemas() {
  return apiFetch("/schemas");
}

export function listSchemaVersions(schemaId) {
  return apiFetch(`/schemas/${schemaId}/versions`);
}

/** @param {{ name: string, fields: object[], identityFields?: string[] }} body */
export function createSchema(body) {
  return apiFetch("/schemas", {
    method: "POST",
    json: { name: body.name, fields: body.fields, identity_fields: body.identityFields ?? null },
  });
}

/**
 * `backfill` is three-valued and the distinction matters: `true` re-extracts
 * every existing document under the schema, `false` is an explicit
 * forward-only save, and `undefined` means "not decided" — which the backend
 * rejects with a 400 for a breaking edit, and treats as forward-only
 * otherwise. Don't collapse it to a plain boolean: `undefined` is what makes
 * the breaking-change dialog fire instead of silently guessing.
 *
 * Resolves to the schema, with `enqueued_backfill_job_ids` populated so the
 * caller can report how many re-extractions were actually queued.
 *
 * @param {{ fields: object[], identityFields?: string[], backfill?: boolean }} body
 */
export function updateSchema(schemaId, body) {
  return apiFetch(`/schemas/${schemaId}`, {
    method: "PUT",
    json: {
      fields: body.fields,
      identity_fields: body.identityFields ?? null,
      backfill: body.backfill ?? null,
    },
  });
}

/** Thrown by updateSchema when the backend needs an explicit backfill choice. */
export function isBreakingChangeError(err) {
  return err instanceof ApiError && err.status === 400 && /breaking/i.test(err.message || "");
}

export function backfillSchema(schemaId) {
  return apiFetch(`/schemas/${schemaId}/backfill`, { method: "POST" });
}

/** Resolves to `{rows, total}` — `total` ignores limit/offset, so the
 * table's pager can tell where the data ends. */
export function getSchemaRecords(schemaId, { limit = 10, offset = 0, filters = {} } = {}) {
  return apiFetch(`/schemas/${schemaId}/records`, { params: { limit, offset, ...filters } });
}

/**
 * Deletes a schema, its versions, its generated table and every record in
 * it. Unlike `deleteBatch`, this deletes confirmed, live, queryable data on
 * purpose — the table is only a projection of the schema, so there is no
 * "keep the rows" that leaves anything readable behind. Always confirm first.
 *
 * The documents survive, detached: their chunks keep answering questions on
 * the Ask page, and they can be re-extracted under another schema.
 *
 * @returns {Promise<{deleted_record_count: number, deleted_version_count: number, detached_document_count: number, cleared_job_count: number, dropped_view: string}>}
 */
export function deleteSchema(schemaId) {
  return apiFetch(`/schemas/${schemaId}`, { method: "DELETE" });
}

/** Deletes one row from a schema's table. The document and its other rows
 * are untouched. */
export function deleteSchemaRecord(schemaId, recordId) {
  return apiFetch(`/schemas/${schemaId}/records/${recordId}`, { method: "DELETE" });
}
