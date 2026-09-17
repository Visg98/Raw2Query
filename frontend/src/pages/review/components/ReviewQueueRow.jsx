import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getJobReview } from "../../../api/jobs";
import { StatusBadge } from "../../../components/common/StatusBadge";
import { hasAnyLowConfidenceInAny } from "../../../components/common/ConfidenceFlag";
import { DeleteDocumentButton } from "../../../components/DeleteDocumentButton/DeleteDocumentButton";

export function ReviewQueueRow({ jobId, documentId, filename }) {
  const { data: review, isLoading } = useQuery({ queryKey: ["jobReview", jobId], queryFn: () => getJobReview(jobId) });

  const flagged = review
    ? hasAnyLowConfidenceInAny(review.result?.data_confidence) || Boolean(review.result?.dedup_match)
    : false;

  return (
    <tr>
      <td>{filename}</td>
      <td>
        <StatusBadge status={review?.status} />
      </td>
      <td>
        {isLoading ? (
          <span className="muted">checking…</span>
        ) : flagged ? (
          <span style={{ color: "var(--color-warning)", fontWeight: 600 }}>Needs review</span>
        ) : (
          <span style={{ color: "var(--color-success)", fontWeight: 600 }}>Clean</span>
        )}
      </td>
      <td>
        <Link to={`/review/${jobId}`}>Open →</Link>
      </td>
      {/* Per-document escape hatch. "Delete batch" above spares confirmed
          documents, so without this a single unwanted document could not be
          removed once confirmed. */}
      <td style={{ textAlign: "right" }}>
        {documentId ? <DeleteDocumentButton documentId={documentId} filename={filename} /> : null}
      </td>
    </tr>
  );
}
