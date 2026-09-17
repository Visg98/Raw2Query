import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getBatch, confirmAllClean } from "../../../api/batches";
import { useToast } from "../../../components/common/Toast";
import { DeleteBatchButton } from "../../../components/DeleteBatchButton";
import { ReviewQueueRow } from "./ReviewQueueRow";

const IN_FLIGHT = new Set(["pending", "extracting"]);

/** One batch's awaiting-review documents, with the bulk "confirm all clean" shortcut. */
export function BatchGroup({ batchId, documents }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: batch } = useQuery({ queryKey: ["batch", batchId], queryFn: () => getBatch(batchId), enabled: Boolean(batchId) });

  const confirmAllMutation = useMutation({
    mutationFn: () => confirmAllClean(batchId),
    onSuccess: (result) => {
      const jobs = batch?.jobs || [];
      const stillInFlight = jobs.filter(
        (j) => !result.confirmed_job_ids.includes(j.id) && !result.skipped_job_ids.includes(j.id) && IN_FLIGHT.has(j.status),
      ).length;
      showToast(
        `${result.confirmed_job_ids.length} confirmed · ${result.skipped_job_ids.length} need individual review` +
          (stillInFlight ? ` · ${stillInFlight} still processing` : ""),
        { variant: "success" },
      );
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["batch", batchId] });
    },
    onError: (err) => showToast(err.message || "Could not bulk-confirm.", { variant: "error" }),
  });

  const jobByDocumentId = new Map((batch?.jobs || []).map((j) => [j.document_id, j]));

  return (
    <div className="card" style={{ padding: "var(--space-4)", marginBottom: "var(--space-4)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-3)" }}>
        <strong>{batchId ? `Batch ${batchId.slice(0, 8)}` : "Ungrouped"}</strong>
        {batchId && (
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            <DeleteBatchButton batchId={batchId} documentCount={documents.length} />
            <button type="button" className="btn btn-sm btn-primary" disabled={confirmAllMutation.isPending} onClick={() => confirmAllMutation.mutate()}>
              Confirm all clean
            </button>
          </div>
        )}
      </div>
      <table style={{ width: "100%", fontSize: 13 }}>
        <tbody>
          {documents.map((doc) => {
            const jobId = jobByDocumentId.get(doc.id)?.id;
            return jobId ? (
              <ReviewQueueRow key={doc.id} jobId={jobId} documentId={doc.id} filename={doc.filename} />
            ) : null;
          })}
        </tbody>
      </table>
    </div>
  );
}
