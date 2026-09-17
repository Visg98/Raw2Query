import { createLazyLottie } from "../../../components/common/lazyLottie";
import styles from "./ExtractionActivity.module.css";

/** The "Live chatbot" composition in src/assets — 952x784. */
const LiveChatbotLottie = createLazyLottie(() => import("../../../assets/Live chatbot.json"));

/**
 * Shown at the top of the extraction queue for as long as anything in it is
 * still pending or extracting, and gone the moment the queue is only
 * failures (or empty).
 *
 * Extraction is slow and mostly invisible — per-document rows tick through
 * steps, but between steps nothing on the page moves, which looks the same
 * as a stalled tab. A running animation is the one signal that says work is
 * still happening without the user having to read a progress bar.
 */
export function ExtractionActivity({ activeCount }) {
  return (
    <div className={styles.banner} role="status" aria-live="polite">
      <div className={styles.art} aria-hidden="true">
        <LiveChatbotLottie loop autoplay style={{ width: "100%", height: "100%" }} />
      </div>
      <div className={styles.copy}>
        <strong className={styles.headline}>
          Extracting {activeCount} document{activeCount === 1 ? "" : "s"}…
        </strong>
        <p className="muted" style={{ margin: 0 }}>
          Each document is being read, matched to a schema and turned into
          structured records. They move to the review queue as they finish —
          you don't have to wait on this page.
        </p>
      </div>
    </div>
  );
}
