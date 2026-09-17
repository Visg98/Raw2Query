import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { deleteBatch } from "../../api/batches";
import { Modal } from "../common/Modal";
import { TrashIcon } from "../common/Icon";
import { useToast } from "../common/Toast";
import { markBatchDiscarded } from "../../hooks/useBatchCompletion";

/**
 * Discards a whole batch from a queue — shared by the extraction queue and
 * the review queue, which both group by batch and both need the same way
 * out of a bad upload.
 *
 * Deliberately behind a confirm dialog rather than an inline button: the
 * originals are unlinked from disk, so this is the one action on either
 * queue that cannot be undone by re-running anything.
 *
 * @param {object} props
 * @param {string} props.batchId
 * @param {number} props.documentCount - how many of this batch's documents the
 *   calling queue can see. The batch may hold more (already-confirmed ones the
 *   queue filters out), which is why the dialog hedges the count and the toast
 *   reports what the server actually did.
 */
export function DeleteBatchButton({ batchId, documentCount }) {
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const deleteMutation = useMutation({
    mutationFn: () => deleteBatch(batchId),
    onSuccess: (result) => {
      // Before the invalidation below drops the rows: the queues read a
      // batch leaving the list as "its work finished" and would otherwise
      // answer a delete with the completion banner and a redirect.
      markBatchDiscarded(batchId);

      const deleted = result.deleted_document_ids.length;
      const kept = result.kept_document_ids.length;

      if (deleted === 0 && kept > 0) {
        // Everything in the batch is already confirmed, so there was
        // nothing to throw away. Saying "deleted" here would be a lie the
        // user would only catch by reloading.
        showToast(
          `Nothing deleted — all ${kept} document${kept === 1 ? "" : "s"} in this batch are already confirmed.`,
          { variant: "info" },
        );
      } else {
        showToast(
          `Deleted ${deleted} document${deleted === 1 ? "" : "s"}` +
            (kept ? ` · kept ${kept} already confirmed` : ""),
          { variant: "success" },
        );
      }

      setConfirming(false);
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      // The group's live status comes from the ["batch", batchId] poll, not
      // from the documents list — without this the rows keep rendering from
      // cache after the batch is gone.
      queryClient.invalidateQueries({ queryKey: ["batch", batchId] });
    },
    onError: (err) => showToast(err.message || "Could not delete this batch.", { variant: "error" }),
  });

  return (
    <>
      <button
        type="button"
        className="btn btn-sm btn-danger"
        onClick={() => setConfirming(true)}
        title="Delete this batch"
      >
        <TrashIcon size={14} />
        Delete
      </button>

      {confirming && (
        <Modal
          title="Delete this batch?"
          subtitle={`Batch ${batchId.slice(0, 8)}`}
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
                {deleteMutation.isPending ? "Deleting…" : "Delete batch"}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            This removes {documentCount} document{documentCount === 1 ? "" : "s"} from the queue along with
            the uploaded file{documentCount === 1 ? "" : "s"} and anything extracted from{" "}
            {documentCount === 1 ? "it" : "them"} that hasn't been confirmed yet.
          </p>
          <p className="muted" style={{ marginBottom: 0 }}>
            Documents in this batch that you've already confirmed are kept — their records are live and
            queryable. Everything else is gone for good; you'd have to upload the files again.
          </p>
        </Modal>
      )}
    </>
  );
}
