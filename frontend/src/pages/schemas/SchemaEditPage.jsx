import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isBreakingChangeError, listSchemas, updateSchema } from "../../api/schemas";
import { SchemaFieldEditor } from "../../components/SchemaFieldEditor";
import { useToast } from "../../components/common/Toast";
import { BreakingChangeDialog } from "./components/BreakingChangeDialog";

export function SchemaEditPage() {
  const { schemaId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: schemas = [] } = useQuery({ queryKey: ["schemas"], queryFn: listSchemas });
  const schema = schemas.find((s) => s.id === schemaId);

  const [fields, setFields] = useState([]);
  const [identityFields, setIdentityFields] = useState([]);
  const [backfill, setBackfill] = useState(false);
  const [showBreakingDialog, setShowBreakingDialog] = useState(false);

  useEffect(() => {
    if (schema?.current_version) {
      setFields(schema.current_version.fields);
      setIdentityFields(schema.current_version.identity_fields || []);
    }
  }, [schema]);

  const documentCount = schema?.document_count ?? 0;

  const updateMutation = useMutation({
    mutationFn: (backfillChoice) => updateSchema(schemaId, { fields, identityFields, backfill: backfillChoice }),
    onSuccess: (updated) => {
      // Reported from the response rather than from the checkbox: a schema
      // with no documents queues nothing however the box was left, and
      // claiming otherwise would have the user waiting for work that will
      // never appear on the processing page.
      const queued = updated?.enqueued_backfill_job_ids?.length ?? 0;
      showToast(
        queued
          ? `Schema updated — re-extracting ${queued} document${queued === 1 ? "" : "s"}.`
          : "Schema updated.",
        { variant: "success" },
      );
      queryClient.invalidateQueries({ queryKey: ["schemas"] });
      queryClient.invalidateQueries({ queryKey: ["schemaVersions", schemaId] });
      // The backfill jobs land on the processing queue as `pending`, so send
      // the user where the work actually is instead of to a records table
      // that won't change until those jobs are reviewed and confirmed.
      navigate(queued ? "/queue" : `/schemas/${schemaId}`);
    },
    onError: (err) => {
      if (isBreakingChangeError(err)) {
        setShowBreakingDialog(true);
      } else {
        showToast(err.message || "Could not update schema.", { variant: "error" });
      }
    },
  });

  if (!schema) return <div className="page skeleton" style={{ height: 300 }} />;

  return (
    <div className="page">
      <div className="page-header">
        <h1>Edit {schema.name}</h1>
      </div>

      <div className="card" style={{ padding: "var(--space-4)" }}>
        <SchemaFieldEditor
          mode="edit"
          fields={fields}
          identityFields={identityFields}
          onChange={setFields}
          onIdentityFieldsChange={setIdentityFields}
        />

        <div
          style={{
            marginTop: "var(--space-4)",
            paddingTop: "var(--space-4)",
            borderTop: "1px solid var(--color-border)",
          }}
        >
          <label style={{ display: "flex", gap: "var(--space-3)", alignItems: "flex-start", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={backfill}
              disabled={documentCount === 0}
              onChange={(e) => setBackfill(e.target.checked)}
              style={{ marginTop: 3 }}
            />
            <span>
              <strong>Re-extract existing documents after saving</strong>
              <span className="muted" style={{ display: "block", marginTop: 2 }}>
                {documentCount === 0
                  ? "No documents are assigned to this schema yet, so there is nothing to re-extract."
                  : `Queues a fresh extraction for all ${documentCount} document${
                      documentCount === 1 ? "" : "s"
                    } under this schema — one LLM call each. Leave unchecked to apply the new version to new uploads only; existing documents keep the values they were extracted with, and any field you just added stays blank for them.`}
              </span>
            </span>
          </label>
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-4)" }}>
          <button
            type="button"
            className="btn btn-primary"
            disabled={updateMutation.isPending}
            // `true` when ticked, `undefined` when not — never `false`.
            // Undefined is what makes a breaking edit 400 and raise the
            // dialog; sending `false` would suppress that warning and save
            // forward-only without ever telling the user their edit dropped
            // or retyped a field.
            onClick={() => updateMutation.mutate(backfill ? true : undefined)}
          >
            {backfill && documentCount > 0 ? "Save and re-extract" : "Save"}
          </button>
        </div>
      </div>

      {showBreakingDialog && (
        <BreakingChangeDialog
          documentCount={documentCount}
          pending={updateMutation.isPending}
          onCancel={() => setShowBreakingDialog(false)}
          onChoose={(choice) => {
            setShowBreakingDialog(false);
            // Keeps the checkbox in step with what the dialog decided, so the
            // toast and the button label don't contradict the choice just made.
            setBackfill(choice);
            updateMutation.mutate(choice);
          }}
        />
      )}
    </div>
  );
}
