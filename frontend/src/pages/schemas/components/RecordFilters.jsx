/**
 * One input per schema field, generated at runtime from SchemaVersionOut.fields
 * (records are untyped over the wire — columns are only known from the
 * version's field list, never hardcoded). Feeds GET /schemas/{id}/records'
 * column=value query params.
 *
 * Keyed by `field.column` (the stored column key), labelled with `field.name`.
 * The param has to be the key: a field *name* is ambiguous across a lineage,
 * since two different fields can have held the same name at different
 * versions, and the endpoint filters real view columns.
 */
export function RecordFilters({ fields, filters, onChange, onClear }) {
  const hasFilters = Object.values(filters).some((v) => v);

  return (
    <div className="card" style={{ padding: "var(--space-4)", marginBottom: "var(--space-4)" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: "var(--space-3)" }}>
        {fields.filter((field) => field.column).map((field) => (
          <div key={field.column}>
            <label className="field-label">{field.name}</label>
            <input
              className="text-input"
              value={filters[field.column] || ""}
              onChange={(e) => onChange({ ...filters, [field.column]: e.target.value })}
              placeholder={field.type}
            />
          </div>
        ))}
      </div>
      {hasFilters && (
        <button type="button" className="btn btn-sm" style={{ marginTop: "var(--space-3)" }} onClick={onClear}>
          Clear filters
        </button>
      )}
    </div>
  );
}
