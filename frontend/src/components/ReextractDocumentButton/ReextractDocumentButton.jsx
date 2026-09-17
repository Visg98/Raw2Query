import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { reextractDocument } from "../../api/documents";
import { Modal } from "../common/Modal";
import { QueueIcon } from "../common/Icon";
import { useToast } from "../common/Toast";

/**
 * Re-runs extraction on an already-confirmed document, from the places that
 * list finished work (Topics, Schemas).
 *
 * The job-level `reextractJob` only accepts a job still awaiting review, so
 * this posts to the document-level endpoint instead, which enqueues a
 * `backfill`. Only structured extraction re-runs, against the document's
 * existing schema — so this cannot re-classify the document or duplicate its
 * chunks — and confirming the resulting review replaces the old record rather
 * than adding a second one.
 *
 * Behind a dialog, but a gentle one: unlike deleting, nothing is lost here
 * until the new extraction is reviewed and confirmed.
 *
 * @param {object} props
 * @param {string} props.documentId
 * @param {string} [props.filename]
 * @param {boolean} [props.iconOnly]
 * @param {string} [props.label]
 */
export function ReextractDocumentButton({ documentId, filename, iconOnly = false, label }) {
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const navigate = useNavigate();

  const reextractMutation = useMutation({
    mutationFn: () => reextractDocument(documentId),
    onSuccess: (result) => {
      setConfirming(false);
      showToast(`Re-extracting ${filename || "document"} — it will appear in the review queue.`, {
        variant: "success",
      });
      // The document's status becomes pending, so anything showing a status
      // badge is now stale. Records are untouched until the new job is
      // confirmed, so ["records"] deliberately is not invalidated here.
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["document", documentId] });
      // Straight to the review page for the new job: the whole point of
      // re-extracting is to look at the new values.
      if (result?.job_id) navigate(`/review/${result.job_id}`);
    },
    onError: (err) => showToast(err.message || "Could not re-extract this document.", { variant: "error" }),
  });

  return (
    <>
      <button
        type="button"
        className="btn btn-sm"
        onClick={() => setConfirming(true)}
        title={`Re-extract ${filename || "this document"}`}
        aria-label={`Re-extract ${filename || "this document"}`}
      >
        <QueueIcon size={14} />
        {iconOnly ? null : label || "Re-extract"}
      </button>

      {confirming && (
        <Modal
          title="Re-extract this document?"
          subtitle={filename}
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={reextractMutation.isPending}
                onClick={() => reextractMutation.mutate()}
              >
                {reextractMutation.isPending ? "Queueing…" : "Re-extract"}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            This runs extraction again against this document’s existing schema and puts it back in the
            review queue, so you can check the new values before they replace the current ones.
          </p>
          <p className="muted" style={{ marginBottom: 0 }}>
            Nothing is lost until you confirm the new extraction. The document’s topics and its indexed
            text are left as they are — only the extracted fields are re-read.
          </p>
        </Modal>
      )}
    </>
  );
}
