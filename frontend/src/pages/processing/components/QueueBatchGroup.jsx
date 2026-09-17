import { useBatchPolling } from "../../../hooks/useBatchPolling";
import { DeleteBatchButton } from "../../../components/DeleteBatchButton";
import { QueueRow } from "./QueueRow";

/** One batch's worth of in-flight/failed documents, backed by the same
 * GET /batches/{id} polling the old upload-progress screen used - one
 * request covers every job in the batch, and it keeps polling on its own
 * for as long as anything in it is still pending/extracting. */
export function QueueBatchGroup({ batchId, documents }) {
  const { data: batch } = useBatchPolling(batchId, { enabled: Boolean(batchId) });
  const jobByDocumentId = new Map((batch?.jobs || []).map((j) => [j.document_id, j]));

  return (
    <div className="card" style={{ padding: "var(--space-4)", marginBottom: "var(--space-4)" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "var(--space-2)",
          marginBottom: "var(--space-3)",
        }}
      >
        <strong>{batchId ? `Batch ${batchId.slice(0, 8)}` : "Ungrouped"}</strong>
        {/* An "Ungrouped" document predates batching and has no batch to
            delete — there's nothing for the endpoint to address. */}
        {batchId && <DeleteBatchButton batchId={batchId} documentCount={documents.length} />}
      </div>
      {documents.map((doc) => (
        <QueueRow key={doc.id} filename={doc.filename} job={jobByDocumentId.get(doc.id)} />
      ))}
    </div>
  );
}
