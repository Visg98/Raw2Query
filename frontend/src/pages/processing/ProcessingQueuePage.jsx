import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "../../api/documents";
import { CompletionBanner } from "../../components/common/CompletionBanner";
import { EmptyState } from "../../components/common/EmptyState";
import { ExtractionQueueGuidance } from "../../components/common/QueueGuidance";
import { useBatchCompletion } from "../../hooks/useBatchCompletion";
import { QueueBatchGroup } from "./components/QueueBatchGroup";
import { ExtractionActivity } from "./components/ExtractionActivity";
import { AwaitingReviewLink } from "./components/AwaitingReviewLink";

const QUEUE_STATUSES = ["pending", "extracting", "failed"];

/** The two statuses that mean work is still in flight, as opposed to a
 * `failed` row sitting in the queue waiting for someone to retry it. */
const IN_PROGRESS_STATUSES = new Set(["pending", "extracting"]);

/**
 * A standing, always-reachable view of what's currently being extracted (or
 * needs a retry) - separate from the Review queue (which only ever lists
 * `awaiting_review` documents) and from the old per-batch
 * `/upload/:batchId/progress` screen (which only existed for the batch you
 * just uploaded, and lost its detail on a true state-loss - see
 * InProgressList's note on the review queue). This page finds every
 * in-flight/failed document from scratch on every load, batch and all, so
 * there's nothing to lose track of.
 */
export function ProcessingQueuePage() {
  const { data: documents = [], isLoading } = useQuery({
    queryKey: ["documents", { status: QUEUE_STATUSES }],
    queryFn: () => listDocuments({ status: QUEUE_STATUSES }),
    refetchInterval: (query) => (query.state.data?.length ? 2000 : 5000),
  });

  const groups = useMemo(() => {
    const byBatch = new Map();
    documents.forEach((doc) => {
      const key = doc.batch_id || "ungrouped";
      if (!byBatch.has(key)) byBatch.set(key, []);
      byBatch.get(key).push(doc);
    });
    return Array.from(byBatch.entries());
  }, [documents]);

  const activeCount = documents.filter((doc) => IN_PROGRESS_STATUSES.has(doc.latest_job_status)).length;

  // Documents arrive newest-first (GET /documents orders by uploaded_at
  // desc), so the group order is upload order and the first real batch is
  // the latest one.
  const batchIds = useMemo(() => groups.map(([key]) => key).filter((key) => key !== "ungrouped"), [groups]);

  const completedBatchId = useBatchCompletion({
    storageKey: "raw2query.extraction.watchedBatch",
    batchIds,
    ready: !isLoading,
    navigateTo: "/review",
  });

  return (
    <div className="page">
      <div className="page-header">
        <h1>Extraction queue</h1>
        <p className="muted">Documents currently extracting or waiting their turn, plus anything that needs a retry — updates on its own.</p>
      </div>

      {/* In place of the batch's own card, which has just left `groups` —
          the user was watching that spot, so that is where the answer
          belongs. Above the guidance and the activity banner because for
          its three seconds it is the only thing on the page that matters. */}
      {completedBatchId && (
        <CompletionBanner title="Extraction complete — review the results">
          Every document in batch {completedBatchId.slice(0, 8)} finished extracting. Taking you to the
          review queue to confirm what was found…
        </CompletionBanner>
      )}

      {/* Shown whenever there is a queue to explain, empty or not: the
          empty state carries the same copy in full. */}
      {!isLoading && groups.length > 0 && <ExtractionQueueGuidance compact />}

      {activeCount > 0 && <ExtractionActivity activeCount={activeCount} />}

      {groups.length > 0 && <AwaitingReviewLink />}

      {isLoading && <div className="skeleton" style={{ height: 160 }} />}

      {/* Suppressed while the completion banner is up: the queue is empty at
          that moment by definition, and "nothing is extracting" directly
          under "extraction complete" reads as the page contradicting
          itself. */}
      {!isLoading && groups.length === 0 && !completedBatchId && (
        <EmptyState title="Nothing is extracting right now">
          <ExtractionQueueGuidance />
        </EmptyState>
      )}

      {groups.map(([batchId, docs]) => (
        <QueueBatchGroup key={batchId} batchId={batchId === "ungrouped" ? null : batchId} documents={docs} />
      ))}
    </div>
  );
}
