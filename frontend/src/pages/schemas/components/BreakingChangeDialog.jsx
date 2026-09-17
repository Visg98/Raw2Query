import styles from "./BreakingChangeDialog.module.css";

/**
 * Shown when PUT /schemas/{id} 400s because the edit removes, retypes or
 * rescopes a field — i.e. only when the reviewer has NOT already ticked
 * "re-extract existing documents" on the edit page, since that sends an
 * explicit `backfill: true` and the request never 400s.
 *
 * The backend has no time estimate for a backfill, so don't promise one —
 * but the document count is known, and "42 documents" is the difference
 * between an informed choice and a guess.
 */
export function BreakingChangeDialog({ onChoose, onCancel, pending, documentCount = 0 }) {
  return (
    <div className={styles.overlay}>
      <div className={`card ${styles.dialog}`}>
        <h3 style={{ marginTop: 0 }}>This is a breaking change</h3>
        <p className="muted">
          A field was removed, retyped or moved between per-document and per-row. Existing documents
          extracted under the previous version won't automatically pick up this change unless you
          backfill them.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", margin: "var(--space-4) 0" }}>
          <button type="button" className="btn btn-primary" disabled={pending} onClick={() => onChoose(true)}>
            Backfill now — re-extract
            {documentCount ? ` all ${documentCount} document${documentCount === 1 ? "" : "s"}` : " existing documents"}
            {" "}under this schema
          </button>
          <button type="button" className="btn" disabled={pending} onClick={() => onChoose(false)}>
            Forward-only — keep old documents on the previous version, only new uploads use this one
          </button>
        </div>
        <button type="button" className="btn btn-sm" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
      </div>
    </div>
  );
}
