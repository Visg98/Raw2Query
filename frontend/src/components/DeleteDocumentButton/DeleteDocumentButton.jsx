import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { deleteDocument } from "../../api/documents";
import { Modal } from "../common/Modal";
import { TrashIcon } from "../common/Icon";
import { useToast } from "../common/Toast";

/**
 * Erases a single document and everything derived from it.
 *
 * The sibling `DeleteBatchButton` clears a queue and deliberately spares
 * already-confirmed documents, since their records are live data. That left
 * no way to remove one specific unwanted document once it had been
 * confirmed, which is what this covers — so it deletes confirmed extracted
 * records too, and the dialog has to say so plainly.
 *
 * Behind a confirm dialog for the same reason as the batch version: the
 * original is unlinked from disk, so nothing here can be undone by
 * re-running a job.
 *
 * @param {object} props
 * @param {string} props.documentId
 * @param {string} [props.filename] - shown in the dialog so the user can see
 *   which document they picked.
 * @param {boolean} [props.iconOnly] - render just the icon, for dense rows.
 * @param {() => void} [props.onDeleted] - called after a successful delete,
 *   e.g. to navigate away from a detail page that no longer has a subject.
 */
export function DeleteDocumentButton({ documentId, filename, iconOnly = false, onDeleted }) {
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const deleteMutation = useMutation({
    mutationFn: () => deleteDocument(documentId),
    onSuccess: (result) => {
      const parts = [];
      if (result.deleted_record_count) {
        parts.push(`${result.deleted_record_count} record${result.deleted_record_count === 1 ? "" : "s"}`);
      }
      if (result.deleted_chunk_count) {
        parts.push(`${result.deleted_chunk_count} chunk${result.deleted_chunk_count === 1 ? "" : "s"}`);
      }
      showToast(`Deleted ${filename || "document"}${parts.length ? ` · ${parts.join(", ")}` : ""}`, {
        variant: "success",
      });

      // The schema keeps its (now empty) view, so say so rather than leaving
      // the user to wonder why a schema lists nothing.
      if (result.emptied_schema_id) {
        showToast("That was the last record in its schema — the schema is now empty.", { variant: "info" });
      }

      setConfirming(false);

      // Every surface that could still be rendering this document: the
      // queues and lists read ["documents"], the review detail and viewer
      // read ["document", id], and the schema record tables and topic counts
      // change whenever extracted records go.
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["document", documentId] });
      queryClient.invalidateQueries({ queryKey: ["schemas"] });
      queryClient.invalidateQueries({ queryKey: ["topics"] });
      onDeleted?.();
    },
    onError: (err) => showToast(err.message || "Could not delete this document.", { variant: "error" }),
  });

  return (
    <>
      <button
        type="button"
        className="btn btn-sm btn-danger"
        onClick={() => setConfirming(true)}
        title={`Delete ${filename || "this document"} and all its data`}
        aria-label={`Delete ${filename || "this document"}`}
      >
        <TrashIcon size={14} />
        {iconOnly ? null : "Delete"}
      </button>

      {confirming && (
        <Modal
          title="Delete this document?"
          subtitle={filename}
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
                {deleteMutation.isPending ? "Deleting…" : "Delete document"}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            This erases everything belonging to this document: the uploaded file, its extraction job, its
            text chunks, and any extracted records — <strong>including records you have already confirmed</strong>.
          </p>
          <p className="muted" style={{ marginBottom: 0 }}>
            Confirmed records are live, queryable data, so they will disappear from their schema and from
            answers on the Ask page. This cannot be undone — you would have to upload the file again.
          </p>
        </Modal>
      )}
    </>
  );
}
