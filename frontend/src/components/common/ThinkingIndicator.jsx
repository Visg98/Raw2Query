import { createLazyLottie } from "./lazyLottie";
import styles from "./ThinkingIndicator.module.css";

/** The "AI loading model" composition in src/assets — a 468x468 square. */
const AiLoadingLottie = createLazyLottie(() => import("../../assets/Ai loading model.json"));

/**
 * The "working on it" state for the Ask page, replacing a bare "Thinking…"
 * line. A query can take a while — topic classification, routing, then
 * either SQL generation + execution or hybrid retrieval + answer
 * composition — and static text gives no sign the page is still alive.
 *
 * Deliberately large and centred in the transcript column rather than an
 * inline row: while it's up it's the only thing on screen worth looking
 * at, so it reads as the app working rather than as a stray glyph beside
 * the last answer.
 */
export function ThinkingIndicator({ label = "Thinking…", size = 180 }) {
  return (
    <div className={styles.wrapper} role="status" aria-live="polite">
      {/* Reserve the box so the label doesn't jump when the animation
          chunk arrives. */}
      <div className={styles.slot} style={{ width: size, height: size }}>
        <AiLoadingLottie
          loop
          autoplay
          style={{ width: size, height: size }}
          // Kept out of the accessibility tree: the wrapper's role=status
          // and the visible label already announce the state, and a screen
          // reader has nothing useful to say about the drawing itself.
          aria-hidden="true"
        />
      </div>
      <span className={styles.label}>{label}</span>
    </div>
  );
}
