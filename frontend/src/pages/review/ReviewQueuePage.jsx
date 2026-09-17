import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "../../api/documents";
import { CompletionBanner } from "../../components/common/CompletionBanner";
import { EmptyState } from "../../components/common/EmptyState";
import { ReviewQueueGuidance } from "../../components/common/QueueGuidance";
import { useBatchCompletion } from "../../hooks/useBatchCompletion";
import { BatchGroup } from "./components/BatchGroup";
import { InProgressList } from "./components/InProgressList";

export function ReviewQueuePage() {
  const { data: documents, isLoading } = useQuery({
    queryKey: ["documents", { status: "awaiting_review" }],
    queryFn: () => listDocuments({ status: "awaiting_review" }),
  });

  const groups = useMemo(() => {
    const byBatch = new Map();
    (documents || []).forEach((doc) => {
      const key = doc.batch_id || "ungrouped";
      if (!byBatch.has(key)) byBatch.set(key, []);
      byBatch.get(key).push(doc);
    });
    return Array.from(byBatch.entries());
  }, [documents]);

  const batchIds = useMemo(() => groups.map(([key]) => key).filter((key) => key !== "ungrouped"), [groups]);

  // Confirming the last document in a batch navigates back here from
  // /review/:jobId, so this page is mounted fresh into an already-drained
  // queue — which is exactly why the hook keeps its marker in
  // sessionStorage rather than in a ref.
  const completedBatchId = useBatchCompletion({
    storageKey: "raw2query.review.watchedBatch",
    batchIds,
    ready: !isLoading,
    navigateTo: "/query",
  });

  return (
    <div className="page">
      <div className="page-header">
        <h1>Review queue</h1>
        <p className="muted">Nothing here is saved yet — confirm each document (or a whole clean batch) to make it queryable.</p>
      </div>

      {completedBatchId && (
        <CompletionBanner title="Review complete — your data is live">
          Every document in batch {completedBatchId.slice(0, 8)} has been reviewed and confirmed, so its
          records and text are now queryable. Taking you to the Ask page to put a question to them…
        </CompletionBanner>
      )}

      {!isLoading && groups.length > 0 && <ReviewQueueGuidance compact />}

      <InProgressList />

      {isLoading && <div className="skeleton" style={{ height: 160 }} />}

      {!isLoading && groups.length === 0 && !completedBatchId && (
        <EmptyState title="Nothing is waiting for review">
          <ReviewQueueGuidance />
        </EmptyState>
      )}

      {groups.map(([batchId, docs]) => (
        <BatchGroup key={batchId} batchId={batchId === "ungrouped" ? null : batchId} documents={docs} />
      ))}
    </div>
  );
}
