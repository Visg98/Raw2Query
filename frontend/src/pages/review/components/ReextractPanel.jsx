import { useState } from "react";
import { Modal } from "../../../components/common/Modal";
import styles from "./ReextractPanel.module.css";

/**
 * The third exit from the review screen, alongside confirm and reject: tell
 * the model what it got wrong and get another extraction.
 *
 * Worth having because the alternative to a bad extraction was hand-correcting
 * a whole table cell by cell, or rejecting the document and re-uploading it.
 * A misread column is one sentence to describe and expensive to fix by hand.
 *
 * Confirms before firing, because re-extracting replaces the table outright —
 * including edits the reviewer already saved. That is deliberate (the point is
 * a fresh read of the document, and interleaving it with edits made against
 * the previous read would produce a table matching neither), but it has to be
 * a choice rather than a surprise.
 *
 * @param {object[]} [history] prior {feedback, at} entries from job.result
 * @param {boolean} running true while the re-extract job is in flight
 * @param {{step?: string, pct?: number}} [progress]
 * @param {boolean} disabled
 * @param {(feedback: string) => void} onSubmit
 */
export function ReextractPanel({ history = [], running, progress, disabled, onSubmit }) {
  const [feedback, setFeedback] = useState("");
  const [confirming, setConfirming] = useState(false);

  function submit() {
    setConfirming(false);
    onSubmit(feedback.trim());
    setFeedback("");
  }

  if (running) {
    return (
      <div className={styles.running} role="status" aria-live="polite">
        <strong>Re-extracting…</strong>
        <span className="muted">
          {progress?.step ? `${progress.step.replace(/_/g, " ")} · ` : ""}
          {progress?.pct != null ? `${progress.pct}%` : "queued"}
        </span>
      </div>
    );
  }

  return (
    <>
      <label className="field-label" htmlFor="reextract-feedback">
        What did the extraction get wrong?
      </label>
      <textarea
        id="reextract-feedback"
        className={`text-input ${styles.textarea}`}
        rows={3}
        placeholder="e.g. The amounts are in the right-hand column, not the middle one. Each line item is one row even when the description wraps."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        disabled={disabled}
      />
      <div className={styles.actions}>
        <button
          type="button"
          className="btn"
          disabled={disabled || !feedback.trim()}
          onClick={() => setConfirming(true)}
        >
          Re-extract with this feedback
        </button>
      </div>

      {history.length > 0 && (
        <div className={styles.history}>
          <span className="field-label">Already tried</span>
          <ul className={styles.historyList}>
            {history.map((entry, index) => (
              <li key={index} className="muted">
                {entry?.feedback}
              </li>
            ))}
          </ul>
        </div>
      )}

      {confirming && (
        <Modal
          title="Re-extract this document?"
          subtitle="The extracted table will be replaced by a fresh reading of the document."
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button type="button" className="btn btn-primary" onClick={submit}>
                Re-extract
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            Any edits you have made to the table — including a saved draft — will be lost. Your
            topic and schema choices are kept, and so are the document&rsquo;s chunks.
          </p>
        </Modal>
      )}
    </>
  );
}
