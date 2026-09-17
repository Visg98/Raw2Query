const OPTIONS = [
  { value: "skip", label: "Skip", hint: "Discard this extraction, keep the existing record." },
  { value: "keep_both", label: "Keep both", hint: "Insert as a separate record (e.g. legitimately re-billed)." },
  { value: "replace", label: "Replace", hint: "Overwrite the existing record with this extraction." },
];

/**
 * Shown iff result.dedup_match is set — a matching identity-field record
 * already exists. Informational: duplicates are acceptable, so confirming
 * without picking anything keeps both records. Skip/replace are there for
 * when you'd rather not.
 */
export function DedupBanner({ dedupMatch, decision, onChange }) {
  if (!dedupMatch) return null;

  return (
    <div className="card" style={{ padding: "var(--space-4)", background: "var(--color-warning-bg)", borderColor: "var(--color-warning)" }}>
      <strong>Possible duplicate</strong>
      <p className="muted" style={{ marginTop: 4 }}>
        A record with matching identity fields already exists (record {dedupMatch.extracted_record_id.slice(0, 8)}):
      </p>
      <ul style={{ margin: "0 0 var(--space-3)" }}>
        {Object.entries(dedupMatch.matched_fields || {}).map(([field, value]) => (
          <li key={field}>
            <strong>{field}:</strong> {String(value)}
          </li>
        ))}
      </ul>
      <div style={{ display: "flex", gap: "var(--space-4)", flexWrap: "wrap" }}>
        {OPTIONS.map((opt) => (
          <label key={opt.value} style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
            <input type="radio" name="dedup-decision" checked={decision === opt.value} onChange={() => onChange(opt.value)} />
            <span>
              <strong>{opt.label}</strong>
              <br />
              <span className="muted">{opt.hint}</span>
            </span>
          </label>
        ))}
      </div>
      {!decision && (
        <p className="muted" style={{ marginTop: "var(--space-2)" }}>
          Confirming without choosing keeps both records, cross-linked to the existing one.
        </p>
      )}
    </div>
  );
}
