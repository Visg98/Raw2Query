import { useQuery } from "@tanstack/react-query";
import { getBatch } from "../api/batches";

const NON_TERMINAL = new Set(["pending", "extracting"]);

export function isBatchInFlight(batch) {
  if (!batch) return true;
  return batch.jobs.some((job) => NON_TERMINAL.has(job.status));
}

/**
 * Polls GET /batches/{id} on an interval (one request covers every job in
 * the batch) instead of opening one EventSource per document — used for
 * batches above the small-N threshold where useJobEvents-per-row would
 * risk the browser's per-origin connection cap.
 */
export function useBatchPolling(batchId, { enabled = true } = {}) {
  return useQuery({
    queryKey: ["batch", batchId],
    queryFn: () => getBatch(batchId),
    enabled: Boolean(batchId) && enabled,
    refetchInterval: (query) => (isBatchInFlight(query.state.data) ? 1500 : false),
  });
}
