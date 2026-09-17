import { useQuery } from "@tanstack/react-query";
import { listSchemas } from "../../../api/schemas";
import { SchemaFieldEditor } from "../../../components/SchemaFieldEditor";
import { JsonSchemaImporter } from "./JsonSchemaImporter";
import styles from "./SchemaPicker.module.css";

/**
 * The three ways to answer "what shape is this data?", as cards rather than
 * a row of radios.
 *
 * The choice is the most consequential one on the upload screen — it decides
 * whether every document lands in an existing table, a new one, or whatever
 * extraction infers per document — and three bare radio labels gave no way
 * to tell those apart without already knowing. Each card carries the
 * sentence that distinguishes it.
 */
const MODES = [
  {
    value: "existing",
    name: "Use an existing schema",
    description:
      "Extract every file in this batch against one of your saved schemas. Best when you know what these documents are — the rows join your existing table.",
  },
  {
    value: "new",
    name: "Define a new schema",
    description:
      "Describe the fields yourself, or import them from a JSON schema. Creates a reusable schema and its own table.",
  },
  {
    value: "none",
    name: "Let extraction decide",
    description:
      "Match each document against your schemas automatically, and propose a structure where nothing fits. Best for a mixed pile you haven't sorted yet.",
  },
];

/**
 * Upload accordion B: pick an existing saved schema as a per-batch default
 * hint (decision #9 — still overridable per document later), or define a
 * brand-new one inline (paste/upload JSON, or build it field-by-field).
 */
export function SchemaPicker({ mode, onModeChange, schemaId, onSchemaIdChange, newSchema, onNewSchemaChange }) {
  const { data: schemas = [], isLoading } = useQuery({ queryKey: ["schemas"], queryFn: listSchemas });

  return (
    <div>
      {/* The cards are buttons, so the radio semantics have to be stated:
          without this a screen reader would announce three unrelated
          buttons rather than one choice of three. */}
      <div className={styles.options} role="radiogroup" aria-label="How to structure this batch">
        {MODES.map((option) => {
          const selected = mode === option.value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={selected}
              className={`${styles.option} ${selected ? styles.selected : ""}`}
              onClick={() => onModeChange(option.value)}
            >
              <span className={styles.name}>
                <span className={styles.marker} aria-hidden="true" />
                {option.name}
              </span>
              <p className={styles.description}>{option.description}</p>
            </button>
          );
        })}
      </div>

      {mode === "existing" && (
        <div>
          {isLoading && <div className="skeleton" style={{ height: 38 }} />}
          {!isLoading && !schemas.length && <p className="muted">No saved schemas yet — define a new one instead.</p>}
          <select className="text-input" value={schemaId || ""} onChange={(e) => onSchemaIdChange(e.target.value || null)}>
            <option value="">— Select a schema —</option>
            {schemas.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} (v{s.current_version?.version ?? 1})
              </option>
            ))}
          </select>
        </div>
      )}

      {mode === "new" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
          <JsonSchemaImporter onImport={onNewSchemaChange} />
          <div>
            <label className="field-label">Schema name</label>
            <input
              className="text-input"
              value={newSchema.name}
              onChange={(e) => onNewSchemaChange({ ...newSchema, name: e.target.value })}
              placeholder="e.g. Invoices"
            />
          </div>
          <SchemaFieldEditor
            mode="create"
            fields={newSchema.fields}
            identityFields={newSchema.identityFields}
            onChange={(fields) => onNewSchemaChange({ ...newSchema, fields })}
            onIdentityFieldsChange={(identityFields) => onNewSchemaChange({ ...newSchema, identityFields })}
          />
        </div>
      )}

      {/* No panel for `none`: the card's own description is the whole
          explanation, and repeating it underneath was the one thing on this
          screen that said the same sentence twice. */}
    </div>
  );
}
