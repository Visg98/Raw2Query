import { ConfidenceFlag } from "../../../components/common/ConfidenceFlag";
import styles from "./ExtractedDataTable.module.css";

/**
 * Editable table over `extracted_data` — the staged list of objects, one
 * table row per `extracted_records` row that Confirm will create.
 *
 * Every declared field is a column, including the per-document ones: the
 * staged payload carries them on every object (`extract.flatten_extraction`),
 * so the table shows exactly what Confirm will store rather than a view the
 * reviewer has to assemble from two widgets.
 *
 * That redundancy is why per-document columns fill down. A reviewer fixing a
 * misread vendor name means "the document says this", not "row 3 says this",
 * and they must not have to retype it once per line item. Editing any cell of
 * a `scope !== "row"` column writes the value into every object — which is
 * the property the old non-redundant payload got for free by keeping the
 * header out of the rows entirely.
 *
 * @param {{name: string, scope?: string}[]} columns field names in schema
 *   declaration order; `scope` is undefined for ad hoc / undeclared columns
 * @param {object[]} rows
 * @param {object[]} [confidence] per-object {field: score}, positionally aligned
 * @param {(rows: object[]) => void} onChange
 */
export function ExtractedDataTable({ columns, rows, confidence = [], onChange }) {
  const safeRows = rows || [];

  function updateCell(index, name, value, scope) {
    // Anything not explicitly row-scoped is treated as cell-local, matching
    // how the backend reads a missing scope (`schema_fields.field_scope`
    // defaults to document) only in reverse: filling a column down is the
    // destructive direction, so it needs an explicit declaration. An ad hoc
    // column the LLM proposed has no scope and stays cell-local.
    const fillDown = scope !== undefined && scope !== "row";
    onChange(
      safeRows.map((row, i) =>
        fillDown || i === index ? { ...row, [name]: value } : row,
      ),
    );
  }

  function removeRow(index) {
    onChange(safeRows.filter((_, i) => i !== index));
  }

  function addRow() {
    // Per-document values are copied from the first object rather than left
    // blank: they describe the document, so they are already known for a row
    // the reviewer is adding by hand, and a blank would confirm as a null.
    const first = safeRows[0] || {};
    onChange([
      ...safeRows,
      Object.fromEntries(
        columns.map(({ name, scope }) => [
          name,
          scope !== undefined && scope !== "row" ? (first[name] ?? "") : "",
        ]),
      ),
    ]);
  }

  if (!columns.length) {
    return <p className="muted">No structured fields were extracted for this document.</p>;
  }

  return (
    <div className={styles.wrap}>
      {safeRows.length === 0 ? (
        <p className="muted">
          The table is empty. Confirming still creates one blank record — add a row here if the
          document does hold values the model missed.
        </p>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th className={styles.indexHead} />
                {columns.map(({ name, scope }) => (
                  <th key={name}>
                    {name}
                    {scope !== undefined && scope !== "row" && (
                      <span className={styles.scopeBadge}>per document</span>
                    )}
                  </th>
                ))}
                <th />
              </tr>
            </thead>
            <tbody>
              {safeRows.map((row, index) => (
                <tr key={index}>
                  <td className={styles.index}>{index + 1}</td>
                  {columns.map(({ name, scope }) => {
                    const perDocument = scope !== undefined && scope !== "row";
                    return (
                      <td key={name} className={perDocument ? styles.perDocumentCell : undefined}>
                        {/* The flag sits above the input rather than in the
                            header: confidence is per cell, not per column. */}
                        <ConfidenceFlag value={confidence?.[index]?.[name]} />
                        <input
                          className="text-input"
                          value={row?.[name] ?? ""}
                          onChange={(e) => updateCell(index, name, e.target.value, scope)}
                        />
                      </td>
                    );
                  })}
                  <td className={styles.removeCell}>
                    {/* A glyph rather than the word "Remove": every declared
                        field is a column here, so the table is already wider
                        than the review column and a per-row text button costs
                        another ~70px of horizontal scroll. */}
                    <button
                      type="button"
                      className={`btn btn-sm btn-danger ${styles.remove}`}
                      disabled={safeRows.length === 1}
                      aria-label={`Remove row ${index + 1}`}
                      title={
                        safeRows.length === 1
                          ? "Confirming always creates at least one record, so an empty table would still store a blank one. Clear the cells instead."
                          : `Remove row ${index + 1}`
                      }
                      onClick={() => removeRow(index)}
                    >
                      ×
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className={styles.footer}>
        <button type="button" className="btn btn-sm" onClick={addRow}>
          + Add row
        </button>
        {safeRows.length > 0 && (
          <span className="muted" style={{ fontSize: 12 }}>
            {safeRows.length} record{safeRows.length === 1 ? "" : "s"} will be created
          </span>
        )}
      </div>
    </div>
  );
}
