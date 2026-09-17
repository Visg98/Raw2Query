import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listDocuments } from "../../../api/documents";

const QUEUE_STATUSES = ["pending", "extracting", "failed"];

/**
 * Documents whose latest job is still `pending`/`extracting`/`failed` never
 * show up in the review queue below (it only lists `awaiting_review`).
 * The full breakdown (per-batch, live progress, retry) lives on its own
 * page at /queue; this is just a standing pointer to it so landing on the
 * review queue while something's still in flight doesn't look like it
 * vanished.
 */
export function InProgressList() {
  const { data: documents = [] } = useQuery({
    queryKey: ["documents", { status: QUEUE_STATUSES }],
    queryFn: () => listDocuments({ status: QUEUE_STATUSES }),
    refetchInterval: (query) => (query.state.data?.length ? 2000 : 5000),
  });

  if (!documents.length) return null;

  const failedCount = documents.filter((d) => d.latest_job_status === "failed").length;

  return (
    <Link
      to="/queue"
      className="card"
      style={{ display: "block", padding: "var(--space-4)", marginBottom: "var(--space-4)", color: "var(--color-text)" }}
    >
      <strong>{documents.length} document{documents.length === 1 ? "" : "s"} still in the extraction queue</strong>
      {failedCount > 0 && <span className="muted"> · {failedCount} need a retry</span>}
      <p className="muted" style={{ margin: "4px 0 0" }}>
        These will move into the queue below once extraction finishes — <span style={{ color: "var(--color-primary)" }}>view live progress →</span>
      </p>
    </Link>
  );
}
