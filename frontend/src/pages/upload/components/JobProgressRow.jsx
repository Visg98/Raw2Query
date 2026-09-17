import { useEffect } from "react";
import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { StatusBadge } from "../../../components/common/StatusBadge";
import { useJobEvents } from "../../../hooks/useJobEvents";
import { retryJob } from "../../../api/jobs";
import styles from "./JobProgressRow.module.css";

function ProgressBody({ filename, jobId, status, progress, errorMessage }) {
  const retryMutation = useMutation({ mutationFn: () => retryJob(jobId) });

  return (
    <div className={styles.row}>
      <span className={styles.filename}>{filename}</span>
      <span className={styles.step}>{progress?.step || "—"}</span>
      <div className={styles.progressTrack}>
        <div className={styles.progressFill} style={{ width: `${progress?.pct ?? 0}%` }} />
      </div>
      <StatusBadge status={status} />
      {status === "awaiting_review" && jobId && <Link to={`/review/${jobId}`}>Preview & edit</Link>}
      {status === "failed" && (
        <>
          <span className={styles.error}>{errorMessage}</span>
          <button type="button" className="btn btn-sm" disabled={retryMutation.isPending} onClick={() => retryMutation.mutate()}>
            Retry
          </button>
        </>
      )}
    </div>
  );
}

/** Live per-job row, backed by SSE — used for small batches (N ≤ 4). */
export function JobProgressRowLive({ filename, jobId, onStatusChange }) {
  const { status, progress, errorMessage } = useJobEvents(jobId, { enabled: Boolean(jobId) });

  useEffect(() => {
    if (onStatusChange) onStatusChange(jobId, status);
  }, [jobId, status, onStatusChange]);

  return <ProgressBody filename={filename} jobId={jobId} status={status} progress={progress} errorMessage={errorMessage} />;
}

/** Row driven by a parent's already-fetched job data — used for large batches (poll GET /batches/{id} once, not N EventSources). */
export function JobProgressRowStatic({ filename, jobId, status, progress, errorMessage }) {
  return <ProgressBody filename={filename} jobId={jobId} status={status} progress={progress} errorMessage={errorMessage} />;
}
