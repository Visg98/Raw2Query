import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listDocuments } from "../../../api/documents";

/**
 * The forward pointer out of the extraction queue, and the mirror image of
 * the review queue's `InProgressList`.
 *
 * Extraction finishing is not the same as the document being saved, and
 * nothing on this page said so: a row would tick to `awaiting_review` and
 * then disappear from the queue, which reads as "done" when in fact the
 * document is staged, unqueryable, and waiting for a human. This explains
 * the hand-off and links to where those documents went.
 *
 * Always rendered, not just when something is waiting — the explanation of
 * what this queue feeds into is the point, and it is as useful on an empty
 * queue as on a busy one.
 */
export function AwaitingReviewLink() {
  const { data: documents = [] } = useQuery({
    queryKey: ["documents", { status: "awaiting_review" }],
    queryFn: () => listDocuments({ status: "awaiting_review" }),
    // Documents land here as extractions finish, so this has to keep up
    // with the queue above rather than only refresh on navigation.
    refetchInterval: 5000,
  });

  const count = documents.length;

  return (
    <div 
      className="card" style={{
        display: "block",
        padding: "var(--space-4)",
        marginBottom: "var(--space-4)",
        color: "var(--color-text)",
      }}>
      <strong>
        {count > 0
          ? `${count} document${count === 1 ? "" : "s"} finished extracting and need${count === 1 ? "s" : ""} your review `
          : "Where documents go once they finish "}
          <Link style={{ color: "var(--color-primary)" }} to="/review">{count > 0 ? "Go to the review queue →" : "Open the review queue →"}</Link>
      </strong>
    </div>
  );
}
