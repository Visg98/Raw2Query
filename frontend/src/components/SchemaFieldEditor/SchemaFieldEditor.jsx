import styles from "./SchemaFieldEditor.module.css";

const FIELD_TYPES = ["string", "number", "integer", "boolean", "date", "datetime"];

const FIELD_SCOPES = [
  { value: "document", label: "Per document" },
  { value: "row", label: "Per row" },
];

function emptyField() {
  return { name: "", type: "string", description: "", required: false, scope: "document" };
}

/**
 * Editable rows of {name, type, description, required, scope}, shared across
 * three contexts: defining a new schema at upload time, editing an ad hoc
 * inferred schema on the review screen, and editing an existing schema's
 * fields (which may trigger the breaking-change flow on submit elsewhere).
 *
 * `scope` decides how many rows one document becomes: a schema with any
 * per-row field extracts one record per repeating line item, with the
 * per-document values copied onto each. Every field defaults to per-document,
 * which is one record per document - the behaviour before scopes existed.
 *
 * Note that `updateField` spreads the existing field, so `column` (the
 * server-owned rename signal) and `scope` both survive an edit to any other
 * cell. Dropping either on the way through would silently split a column's
 * data or reset a schema to single-row on the next save.
 *
 * @param {object[]} fields
 * @param {string[]} [identityFields]
 * @param {(fields: object[]) => void} onChange
 * @param {(identityFields: string[]) => void} [onIdentityFieldsChange]
 * @param {"create"|"edit"|"proposed"} mode
 */
export function SchemaFieldEditor({ fields, identityFields = [], onChange, onIdentityFieldsChange, mode = "create" }) {
  function updateField(index, patch) {
    const next = fields.map((f, i) => (i === index ? { ...f, ...patch } : f));
    onChange(next);
    // Switching a field to per-row drops it as an identity key. The server
    // rejects a row-scoped identity field outright (identity means "the same
    // document", and dedup runs once per document), so leaving the box ticked
    // would turn the next save into a 400 the user can't see the cause of.
    const name = next[index]?.name;
    if (patch.scope === "row" && name && onIdentityFieldsChange && identityFields.includes(name)) {
      onIdentityFieldsChange(identityFields.filter((n) => n !== name));
    }
  }

  function removeField(index) {
    const removedName = fields[index]?.name;
    onChange(fields.filter((_, i) => i !== index));
    if (removedName && onIdentityFieldsChange) {
      onIdentityFieldsChange(identityFields.filter((name) => name !== removedName));
    }
  }

  function addField() {
    onChange([...fields, emptyField()]);
  }

  function toggleIdentityField(name, checked) {
    if (!onIdentityFieldsChange) return;
    onIdentityFieldsChange(checked ? [...identityFields, name] : identityFields.filter((n) => n !== name));
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.headerRow}>
        <span>Name</span>
        <span>Type</span>
        <span title="Per document = one value for the whole file. Per row = one value per line item; any per-row field makes the document extract to several rows.">
          Scope
        </span>
        <span>Description</span>
        <span>Required</span>
        {onIdentityFieldsChange && <span title="Used for dedup matching">Identity key</span>}
        <span />
      </div>
      {fields.map((field, index) => (
        <div className={styles.row} key={index}>
          <input
            className="text-input"
            value={field.name}
            placeholder="field_name"
            onChange={(e) => updateField(index, { name: e.target.value })}
          />
          <select className="text-input" value={field.type} onChange={(e) => updateField(index, { type: e.target.value })}>
            {FIELD_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <select
            className="text-input"
            // Stored fields predating scopes have no `scope` key at all, and
            // the server reads that as document scope - so the control has to
            // show the same thing rather than an empty select.
            value={field.scope || "document"}
            onChange={(e) => updateField(index, { scope: e.target.value })}
          >
            {FIELD_SCOPES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <input
            className="text-input"
            value={field.description}
            placeholder="What this field captures"
            onChange={(e) => updateField(index, { description: e.target.value })}
          />
          <input
            type="checkbox"
            checked={field.required}
            onChange={(e) => updateField(index, { required: e.target.checked })}
          />
          {onIdentityFieldsChange && (
            <input
              type="checkbox"
              disabled={!field.name || field.scope === "row"}
              title={field.scope === "row" ? "Identity keys must be per-document fields" : undefined}
              checked={identityFields.includes(field.name)}
              onChange={(e) => toggleIdentityField(field.name, e.target.checked)}
            />
          )}
          <button type="button" className="btn btn-sm btn-danger" onClick={() => removeField(index)}>
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="btn btn-sm" onClick={addField}>
        + Add field
      </button>
      {mode === "proposed" && (
        <p className="muted" style={{ marginTop: "var(--space-2)" }}>
          This structure was inferred by the extraction model — check names/types before confirming.
        </p>
      )}
    </div>
  );
}

export { emptyField };
