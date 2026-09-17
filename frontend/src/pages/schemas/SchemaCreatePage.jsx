import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createSchema } from "../../api/schemas";
import { SchemaFieldEditor } from "../../components/SchemaFieldEditor";
import { JsonSchemaImporter } from "../upload/components/JsonSchemaImporter";
import { useToast } from "../../components/common/Toast";

/**
 * Standalone "create a schema" flow, reachable directly from the Schemas
 * screen. Previously the only way to define a schema was the upload
 * accordion, which forced you through picking files first and dropped you
 * on the upload/progress screen afterward — this creates the schema and
 * takes you straight back into the schemas section.
 */
export function SchemaCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [name, setName] = useState("");
  const [fields, setFields] = useState([]);
  const [identityFields, setIdentityFields] = useState([]);

  const createMutation = useMutation({
    mutationFn: () => createSchema({ name: name.trim(), fields, identityFields }),
    onSuccess: (schema) => {
      showToast(`Schema "${schema.name}" created.`, { variant: "success" });
      queryClient.invalidateQueries({ queryKey: ["schemas"] });
      navigate(`/schemas/${schema.id}`);
    },
    onError: (err) => showToast(err.message || "Could not create schema.", { variant: "error" }),
  });

  function handleImport(imported) {
    setName(imported.name || name);
    setFields(imported.fields);
    setIdentityFields(imported.identityFields || []);
  }

  const canSubmit = Boolean(name.trim()) && fields.length > 0;

  return (
    <div className="page">
      <div className="page-header">
        <h1>New schema</h1>
        <Link to="/schemas">← Back to schemas</Link>
      </div>

      <div className="card" style={{ padding: "var(--space-4)", marginBottom: "var(--space-4)" }}>
        <h3 style={{ marginTop: 0 }}>Paste or upload a definition (optional)</h3>
        <JsonSchemaImporter onImport={handleImport} />
      </div>

      <div className="card" style={{ padding: "var(--space-4)" }}>
        <label className="field-label">Schema name</label>
        <input
          className="text-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Invoices"
          style={{ marginBottom: "var(--space-4)" }}
        />
        <SchemaFieldEditor
          mode="create"
          fields={fields}
          identityFields={identityFields}
          onChange={setFields}
          onIdentityFieldsChange={setIdentityFields}
        />
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-4)" }}>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canSubmit || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? "Creating…" : "Create schema"}
          </button>
        </div>
      </div>
    </div>
  );
}
