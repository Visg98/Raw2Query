import { useState } from "react";
import { Modal } from "../../../components/common/Modal";
import { DocumentViewer } from "../../../components/DocumentViewer";
import { BackIcon } from "../../../components/common/Icon";
import styles from "./SourcesModal.module.css";

/**
 * Sources used to be inlined under every answer, which buried the answer
 * itself under eight snippet boxes. They're behind a button now: one line
 * under the answer, opening a modal of stacked source cards.
 *
 * "View file" opens the document *in* the modal rather than linking to the
 * raw file endpoint — that link was a plain `<a href>` to the original
 * bytes, so anything the browser has no viewer for (.docx, .xlsx, .csv, …)
 * downloaded instead of opening. `DocumentViewer` renders every supported
 * type in place.
 */
export function SourcesModal({ sources, onClose }) {
  // Null = the list; otherwise the source whose document is being viewed.
  const [viewing, setViewing] = useState(null);

  if (viewing) {
    return (
      <Modal
        title={viewing.filename || "Source document"}
        subtitle={viewing.page_number ? `Cited from page ${viewing.page_number}` : null}
        size="wide"
        onClose={onClose}
        footer={
          <button type="button" className="btn btn-sm" onClick={() => setViewing(null)}>
            <BackIcon /> Back to sources
          </button>
        }
      >
        <DocumentViewer documentId={viewing.document_id} filename={viewing.filename} height="100%" />
      </Modal>
    );
  }

  return (
    <Modal
      title={`${sources.length} source${sources.length === 1 ? "" : "s"}`}
      subtitle="The document passages this answer was composed from."
      onClose={onClose}
    >
      <div className={styles.list}>
        {sources.map((source, index) => (
          <article className={styles.card} key={source.chunk_id || `${source.document_id}-${index}`}>
            <header className={styles.cardHeader}>
              <span className={styles.index}>{index + 1}</span>
              <div className={styles.cardTitle}>
                <strong className={styles.filename}>{source.filename || "Untitled document"}</strong>
                {source.page_number != null && <span className="muted"> · page {source.page_number}</span>}
              </div>
            </header>
            {source.snippet && <p className={styles.snippet}>{source.snippet}</p>}
            <button type="button" className="btn btn-sm" onClick={() => setViewing(source)}>
              View file
            </button>
          </article>
        ))}
      </div>
    </Modal>
  );
}
