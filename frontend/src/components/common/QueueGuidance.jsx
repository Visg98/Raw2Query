import { Link } from "react-router-dom";
import styles from "./QueueGuidance.module.css";

/**
 * What the extraction queue is and where the work goes — as a standing note
 * at the top of a busy queue, and as the body of the empty state.
 *
 * One component for both so the two can't drift: the explanation a user
 * needs when the queue is empty ("where did everything go?") is the same one
 * they need when it's full ("what is this waiting for?"), and having written
 * it twice is how those diverge.
 *
 * @param {object} props
 * @param {boolean} [props.compact] - the top-of-page variant: one line, no
 *   heading, sized to be read past rather than read.
 */
export function ExtractionQueueGuidance({ compact = false }) {
  if (compact) {
    return (
      <p className={styles.compact}>
        Extraction only stages a document — nothing here is saved or queryable yet. Finished documents
        move to the <Link to="/review">review queue</Link>, where confirming them is what writes the
        data. Already-confirmed data lives under <Link to="/topics">topics</Link> and{" "}
        <Link to="/schemas">schemas</Link>, and you can question it on the{" "}
        <Link to="/query">Ask page</Link>.
      </p>
    );
  }

  return (
    <>
      <p>
        If you were expecting something here, it has probably already finished: check the{" "}
        <Link to="/review">review queue</Link> for documents in review, waiting for you to confirm what
        was extracted. Anything you've already confirmed is live data — browse it by{" "}
        <Link to="/topics">topic</Link> or <Link to="/schemas">schema</Link>, or just ask a question
        about it on the <Link to="/query">Ask page</Link>.
      </p>
      <p>
        Otherwise, <Link to="/upload">upload some documents</Link> and they'll show up here as they
        extract.
      </p>
    </>
  );
}

/**
 * The same idea for the review queue, which sits between extraction and live
 * data and so has two directions to point in: back to uploads if there is
 * genuinely nothing in the system, forward to topics and Ask if the user has
 * simply already confirmed everything.
 */
export function ReviewQueueGuidance({ compact = false }) {
  if (compact) {
    return (
      <p className={styles.compact}>
        Nothing here is saved yet — confirming a document (or a whole clean batch) is what makes it
        queryable. Still extracting? See the <Link to="/queue">extraction queue</Link>. Already
        confirmed? It's under <Link to="/topics">topics</Link> and <Link to="/schemas">schemas</Link>,
        and answerable on the <Link to="/query">Ask page</Link>.
      </p>
    );
  }

  return (
    <>
      <p>No documents are waiting for review — you're all caught up.</p>
      <p>
        If you haven't uploaded anything yet, start there:{" "}
        <Link to="/upload">upload your messy documents</Link> and they'll be extracted and queued here
        for you to check. If something is still being read, it's in the{" "}
        <Link to="/queue">extraction queue</Link>.
      </p>
      <p>
        And if you've already confirmed your documents, they're live: browse them by{" "}
        <Link to="/topics">topic</Link>, look at the extracted tables under{" "}
        <Link to="/schemas">schemas</Link>, or ask questions about them on the{" "}
        <Link to="/query">Ask page</Link>.
      </p>
    </>
  );
}
