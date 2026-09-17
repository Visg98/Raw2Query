import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { retryJob } from "../../../api/jobs";
import { useToast } from "../../../components/common/Toast";
import { StatusBadge } from "../../../components/common/StatusBadge";
import styles from "../../upload/components/JobProgressRow.module.css";

/** One document's live row in the extraction queue: status, progress bar,
 * and whatever action makes sense for that status (retry if failed, jump
 * into review if it just finished). */
export function QueueRow({ filename, job }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const retryMutation = useMutation({
    mutationFn: () => retryJob(job.id),
    onSuccess: () => {
      showToast("Retrying…", { variant: "info" });
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      // The row's live status/progress comes from the batch poll
      // (useBatchPolling's `["batch", batchId]` query in QueueBatchGroup),
      // not from the documents list above - and that poll had already
      // stopped once nothing in the batch was pending/extracting anymore
      // (a `failed` job doesn't count as "in flight"). Without this, the
      // button flips to "Retrying…" and then just sits there: the job is
      // actually back to `pending` server-side, but the stale cached batch
      // data still says `failed` forever.
      if (job.batch_id) queryClient.invalidateQueries({ queryKey: ["batch", job.batch_id] });
    },
    onError: (err) => showToast(err.message || "Could not retry.", { variant: "error" }),
  });

  const status = job?.status;

  return (
    <div className={styles.row}>
      <span className={styles.filename}>{filename}</span>
      <span className={styles.step}>{job?.progress?.step || "—"}</span>
      <div className={styles.progressTrack}>
        <div className={styles.progressFill} style={{ width: `${job?.progress?.pct ?? 0}%` }} />
      </div>
      <StatusBadge status={status} />
      {status === "awaiting_review" && <Link to={`/review/${job.id}`}>Review →</Link>}
      {status === "failed" && (
        <>
          <span className={styles.error}>{job.error_message}</span>
          <button type="button" className="btn btn-sm" disabled={retryMutation.isPending} onClick={() => retryMutation.mutate()}>
            {retryMutation.isPending ? "Retrying…" : "Retry"}
          </button>
        </>
      )}
    </div>
  );
}
