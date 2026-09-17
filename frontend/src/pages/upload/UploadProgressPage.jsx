import { useCallback, useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getBatch } from "../../api/batches";
import { isBatchInFlight, useBatchPolling } from "../../hooks/useBatchPolling";
import { JobProgressRowLive, JobProgressRowStatic } from "./components/JobProgressRow";

const LIVE_SSE_THRESHOLD = 4;
const TERMINAL = new Set(["awaiting_review", "confirmed", "rejected", "failed"]);

export function UploadProgressPage() {
  const { batchId } = useParams();
  const location = useLocation();
  // The immediate POST /documents response (batch_id, documents[] with
  // job_id) is passed via router state to render rows without waiting on a
  // round trip. When it isn't there (a reopened/bookmarked progress URL),
  // GET /batches/{id} below covers the same ground - every uploaded file
  // now has its own document and job, so nothing is missing from it.
  const uploadResult = location.state?.uploadResult;

  const [liveStatuses, setLiveStatuses] = useState({});
  const onStatusChange = useCallback((jobId, status) => {
    setLiveStatuses((prev) => (prev[jobId] === status ? prev : { ...prev, [jobId]: status }));
  }, []);

  const documents = uploadResult?.documents ?? [];
  const useLive = documents.length > 0 && documents.length <= LIVE_SSE_THRESHOLD;

  const fallbackBatchQuery = useQuery({
    queryKey: ["batch", batchId, "fallback"],
    queryFn: () => getBatch(batchId),
    enabled: documents.length === 0,
    // Without this, a page load that lands on the fallback path (the
    // upload-result router state wasn't available - e.g. a bookmarked or
    // reopened progress URL) fetched the batch exactly once and then sat
    // frozen on whatever status the jobs happened to have at that moment
    // forever, even once they actually finished - looking like the
    // extraction was stuck or had vanished, with no indication anything
    // was still happening (and, once done, no link forward to review
    // either, since this same freeze also fed `allTerminal` below).
    refetchInterval: (query) => (isBatchInFlight(query.state.data) ? 1500 : false),
  });

  const polledBatch = useBatchPolling(batchId, { enabled: !useLive && documents.length > 0 });

  const jobsByDocumentId = useMemo(() => {
    const source = polledBatch.data || fallbackBatchQuery.data;
    const map = new Map();
    (source?.jobs || []).forEach((job) => map.set(job.document_id, job));
    return map;
  }, [polledBatch.data, fallbackBatchQuery.data]);

  const fallbackDocs = documents.length === 0 ? fallbackBatchQuery.data?.documents || [] : [];

  const rows = documents.length ? documents : fallbackDocs.map((d) => ({ document_id: d.id, filename: d.filename, job_id: jobsByDocumentId.get(d.id)?.id }));

  const allTerminal =
    rows.length > 0 &&
    rows.every((row) => {
      const status = useLive ? liveStatuses[row.job_id] : jobsByDocumentId.get(row.document_id)?.status;
      return TERMINAL.has(status);
    });

  return (
    <div className="page">
      <div className="page-header">
        <h1>Extracting…</h1>
        <Link to="/review">Go to review queue →</Link>
      </div>

      {rows.length === 0 && <div className="skeleton" style={{ height: 200 }} />}

      {rows.map((row) =>
        useLive ? (
          <JobProgressRowLive key={row.document_id} filename={row.filename} jobId={row.job_id} onStatusChange={onStatusChange} />
        ) : (
          <JobProgressRowStatic
            key={row.document_id}
            filename={row.filename}
            jobId={row.job_id}
            status={jobsByDocumentId.get(row.document_id)?.status}
            progress={jobsByDocumentId.get(row.document_id)?.progress}
            errorMessage={jobsByDocumentId.get(row.document_id)?.error_message}
          />
        ),
      )}

      {allTerminal && (
        <div className="card" style={{ padding: "var(--space-4)", marginTop: "var(--space-4)" }}>
          <strong>All documents processed.</strong>{" "}
          <Link to="/review">Review and confirm now</Link>, or come back later — nothing is saved until you confirm.
        </div>
      )}
    </div>
  );
}
