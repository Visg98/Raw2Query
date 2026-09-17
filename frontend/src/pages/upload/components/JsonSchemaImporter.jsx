import { useRef, useState } from "react";

// Keep in sync with SchemaFieldEditor's FIELD_TYPES / the backend's
// `FieldType` literal (app/schemas_pydantic.py) - these are the only values
// POST /schemas will accept.
const VALID_TYPES = new Set(["string", "number", "integer", "boolean", "date", "datetime"]);

// A pasted/hand-written schema is likely to use a natural-language type name
// that isn't one of the app's literal values (e.g. "text" for "string",
// "float" for "number"). Rather than pass it through and let the field
// silently fail POST /schemas validation later with a cryptic error, map
// the common synonyms and otherwise fall back to "string".
const TYPE_ALIASES = {
  text: "string",
  str: "string",
  varchar: "string",
  float: "number",
  double: "number",
  decimal: "number",
  int: "integer",
  bool: "boolean",
};

function normalizeType(rawType) {
  const t = String(rawType || "string").trim().toLowerCase();
  if (VALID_TYPES.has(t)) return t;
  return TYPE_ALIASES[t] || "string";
}

/**
 * Paste or upload a JSON schema definition ({name, fields, identity_fields})
 * and parse it into the shape SchemaFieldEditor expects, for a visual check
 * before it's actually submitted to POST /schemas.
 */
export function JsonSchemaImporter({ onImport }) {
  const [text, setText] = useState("");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const fileInputRef = useRef(null);

  function parseAndImport(raw) {
    try {
      const parsed = JSON.parse(raw);
      const unrecognizedTypes = new Set();
      const fields = (parsed.fields || []).map((f) => {
        const normalized = normalizeType(f.type);
        if (f.type && normalized !== String(f.type).toLowerCase()) unrecognizedTypes.add(f.type);
        return {
          name: f.name || "",
          type: normalized,
          description: f.description || "",
          required: Boolean(f.required),
          // Carried through the whitelist so this importer stays safe to reuse
          // on the schema *edit* page, where dropping `column` would turn every
          // rename into a delete-plus-add and split the field's data across two
          // columns. Harmless here (imports are new fields, and the server
          // ignores a key it doesn't recognise on a new lineage).
          ...(f.column ? { column: f.column } : {}),
        };
      });
      if (!fields.length) throw new Error("JSON has no \"fields\" array");
      onImport({ name: parsed.name || "", fields, identityFields: parsed.identity_fields || [] });
      setError(null);
      setNotice(
        unrecognizedTypes.size
          ? `Mapped unrecognized type(s) ${[...unrecognizedTypes].map((t) => `"${t}"`).join(", ")} to a supported type below - double-check them.`
          : null,
      );
    } catch (err) {
      setNotice(null);
      setError(err.message || "Could not parse JSON");
    }
  }

  return (
    <div>
      <textarea
        className="text-input"
        rows={5}
        placeholder={'Paste a schema, e.g. {"name": "Invoices", "fields": [{"name":"invoice_number","type":"string"}]}'}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
        <button type="button" className="btn btn-sm" disabled={!text.trim()} onClick={() => parseAndImport(text)}>
          Load into editor
        </button>
        <button type="button" className="btn btn-sm" onClick={() => fileInputRef.current?.click()}>
          Upload .json file
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="application/json"
          hidden
          onChange={async (e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (!file) return;
            const raw = await file.text();
            setText(raw);
            parseAndImport(raw);
          }}
        />
      </div>
      {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}
      {notice && <p style={{ color: "var(--color-warning)" }}>{notice}</p>}
    </div>
  );
}
