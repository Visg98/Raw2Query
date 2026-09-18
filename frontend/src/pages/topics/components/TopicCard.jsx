import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listDocuments } from "../../../api/documents";
import { deleteTopic } from "../../../api/topics";
import { StatusBadge } from "../../../components/common/StatusBadge";
import { Modal } from "../../../components/common/Modal";
import { TrashIcon } from "../../../components/common/Icon";
import { useToast } from "../../../components/common/Toast";
import { DeleteDocumentButton } from "../../../components/DeleteDocumentButton/DeleteDocumentButton";
import styles from "./TopicCard.module.css";

/** The fallback topic every untagged document lands on (decision #16). The
 * backend refuses to delete it; hiding the button is how the UI says so
 * before the user finds out from an error. */
const UNCATEGORIZED = "Uncategorized";

/**
 * Reuses the existing GET /documents?topic_id= filter (no new backend
 * endpoint needed) to show what's actually under a topic: a document
 * count, a status mix, and the most recent filenames.
 */
export function TopicCard({ topic }) {
  const [expanded, setExpanded] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: documents, isLoading } = useQuery({
    queryKey: ["documents", { topicId: topic.id }],
    queryFn: () => listDocuments({ topicId: topic.id }),
    // Also needed by the delete dialog, which says how many documents the
    // topic is on — so it can't wait for the user to expand the card.
    enabled: expanded || confirming,
  });

  const isUncategorized = topic.name === UNCATEGORIZED;

  const deleteMutation = useMutation({
    mutationFn: () => deleteTopic(topic.id),
    onSuccess: (result) => {
      showToast(
        `Deleted “${topic.name}”` +
          (result.unlinked_document_count
            ? ` · removed from ${result.unlinked_document_count} document${result.unlinked_document_count === 1 ? "" : "s"}`
            : "") +
          (result.uncategorized_document_count
            ? ` · ${result.uncategorized_document_count} now Uncategorized`
            : ""),
        { variant: "success" },
      );
      setConfirming(false);
      queryClient.invalidateQueries({ queryKey: ["topics"] });
      // Documents carry their topic list, and one of them just changed.
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (err) => showToast(err.message || "Could not delete this topic.", { variant: "error" }),
  });

  const statusCounts = (documents || []).reduce((acc, doc) => {
    const key = doc.latest_job_status || "unknown";
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="card" style={{ padding: "var(--space-4)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h3 style={{ margin: 0 }}>{topic.name}</h3>
          {topic.description && <p className="muted" style={{ margin: "4px 0 0" }}>{topic.description}</p>}
        </div>
        <div style={{ display: "flex", gap: "var(--space-2)", flex: "0 0 auto" }}>
          <button type="button" className="btn btn-sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "Hide" : "View documents"}
          </button>
          {!isUncategorized && (
            <button
              type="button"
              className="btn btn-sm btn-danger"
              onClick={() => setConfirming(true)}
              aria-label={`Delete topic ${topic.name}`}
              title="Delete this topic"
            >
              <TrashIcon size={13} />
            </button>
          )}
        </div>
      </div>

      {confirming && (
        <Modal
          title={`Delete “${topic.name}”?`}
          subtitle="Your documents and their data are kept"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={deleteMutation.isPending}
                onClick={() => deleteMutation.mutate()}
              >
                {deleteMutation.isPending ? "Deleting…" : "Delete topic"}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            A topic is a label, not a folder — deleting it deletes nothing that was filed under it.{" "}
            {isLoading ? (
              <>Documents currently tagged with it</>
            ) : (
              <>
                The {documents?.length ?? 0} document{(documents?.length ?? 0) === 1 ? "" : "s"} tagged
                with it
              </>
            )}{" "}
            stay exactly as they are, along with their extracted rows and their searchable text. They
            simply stop carrying this label.
          </p>
          <p>
            Any document left with no topics at all becomes <strong>Uncategorized</strong>, so it stays
            findable and scopeable on the Ask page rather than falling out of every filter.
          </p>
          <p className="muted" style={{ marginBottom: 0 }}>
            The label is also removed from extracted tables, from chunk search scoping, and from any
            upload still waiting to be reviewed. Re-creating a topic with the same name later will not
            re-tag anything.
          </p>
        </Modal>
      )}

      {expanded && (
        <div className={styles.expanded}>
          {isLoading && <div className="skeleton" style={{ height: 60 }} />}
          {!isLoading && !documents?.length && <p className="muted">No documents under this topic yet.</p>}
          {!isLoading && documents?.length > 0 && (
            <>
              <p className="muted">
                {documents.length} document{documents.length === 1 ? "" : "s"} ·{" "}
                {Object.entries(statusCounts)
                  .map(([status, count]) => `${count} ${status.replace("_", " ")}`)
                  .join(" · ")}
              </p>
              <ul className={styles.fileList}>
                {documents.slice(0, 6).map((doc) => (
                  <li key={doc.id}>
                    <span className={styles.filename}>{doc.filename}</span>
                    <StatusBadge status={doc.latest_job_status} />
                    {/* Removing the topic (above) keeps the documents; this
                        removes one document outright, records included. */}
                    <DeleteDocumentButton documentId={doc.id} filename={doc.filename} iconOnly />
                  </li>
                ))}
              </ul>
              {documents.length > 6 && (
                <Link to={`/schemas`} className="muted">
                  +{documents.length - 6} more
                </Link>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
