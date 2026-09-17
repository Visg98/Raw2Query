import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { deleteSchema } from "../../../api/schemas";
import { Modal } from "../../../components/common/Modal";
import { TrashIcon } from "../../../components/common/Icon";
import { useToast } from "../../../components/common/Toast";

/**
 * Deletes a schema along with its extracted table.
 *
 * The dialog leads with the record count because that is the part a user
 * will not predict: "delete schema" sounds like deleting a definition, and
 * the definition is the cheap half — the expensive half is every row that
 * was ever extracted under it, which is live data the Ask page and the
 * records table are serving right now. The count is fetched with the schema
 * list (`document_count`) and the rows are reported back by the endpoint, so
 * the dialog warns with the number and the toast confirms with it.
 *
 * @param {object} props
 * @param {string} props.schemaId
 * @param {string} props.schemaName
 * @param {number} [props.documentCount] - documents extracted under this
 *   schema, for the warning. Those documents are kept; their rows are not.
 */
export function DeleteSchemaButton({ schemaId, schemaName, documentCount }) {
  const [confirming, setConfirming] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const deleteMutation = useMutation({
    mutationFn: () => deleteSchema(schemaId),
    onSuccess: (result) => {
      showToast(
        `Deleted “${schemaName}” and ${result.deleted_record_count} record${result.deleted_record_count === 1 ? "" : "s"}` +
          (result.detached_document_count
            ? ` · kept ${result.detached_document_count} document${result.detached_document_count === 1 ? "" : "s"}`
            : ""),
        { variant: "success" },
      );
      setConfirming(false);
      queryClient.invalidateQueries({ queryKey: ["schemas"] });
      // The documents' schema link is gone, so anything listing them by
      // schema is stale too.
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.removeQueries({ queryKey: ["records", schemaId] });
      queryClient.removeQueries({ queryKey: ["schemaVersions", schemaId] });
      navigate("/schemas");
    },
    onError: (err) => showToast(err.message || "Could not delete this schema.", { variant: "error" }),
  });

  return (
    <>
      <button
        type="button"
        className="btn btn-sm btn-danger"
        onClick={() => setConfirming(true)}
        title="Delete this schema and its extracted table"
      >
        <TrashIcon size={14} />
        Delete schema
      </button>

      {confirming && (
        <Modal
          title={`Delete “${schemaName}”?`}
          subtitle="This deletes the extracted table too"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={deleteMutation.isPending}
                onClick={() => deleteMutation.mutate()}
              >
                {deleteMutation.isPending ? "Deleting…" : "Delete schema and its data"}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            <strong>Every record extracted under this schema will be deleted along with it.</strong> The
            table this schema generated is dropped, so those rows stop appearing in the records view and
            stop being available to answer questions on the Ask page.
          </p>
          <p>
            {documentCount ? (
              <>
                The {documentCount} document{documentCount === 1 ? "" : "s"} extracted under this schema{" "}
                {documentCount === 1 ? "is" : "are"} <strong>kept</strong> —{" "}
                {documentCount === 1 ? "its" : "their"} uploaded file{documentCount === 1 ? "" : "s"} and
                searchable text stay exactly as {documentCount === 1 ? "it is" : "they are"}, so you can
                still ask questions about {documentCount === 1 ? "it" : "them"} and re-extract{" "}
                {documentCount === 1 ? "it" : "them"} under a different schema later.
              </>
            ) : (
              <>Your documents and their searchable text are kept — only this schema and its rows go.</>
            )}
          </p>
          <p className="muted" style={{ marginBottom: 0 }}>
            The schema definition, all of its versions and its field history go too. This cannot be
            undone; re-creating the schema would not bring the rows back, only a re-extraction would.
          </p>
        </Modal>
      )}
    </>
  );
}
