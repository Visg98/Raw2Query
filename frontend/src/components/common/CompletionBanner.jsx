import { createLazyLottie } from "./lazyLottie";
import styles from "./CompletionBanner.module.css";

/** The "Confetti" composition in src/assets — a 320x320 square. */
const ConfettiLottie = createLazyLottie(() => import("../../assets/Confetti.json"));

/**
 * The hand-off card a queue shows in place of the batch it just finished,
 * for the few seconds before it moves the user on.
 *
 * It replaces the batch's own card rather than appearing above it, so the
 * thing the user was watching turns into the thing that says it's done —
 * a batch group silently vanishing from the list is the failure mode this
 * exists to fix.
 *
 * Deliberately not dismissible and not a toast: it is a step in a
 * navigation, and the copy has to be readable for its whole life. The
 * caller owns the timer (see `useBatchCompletion`), because the caller is
 * what does the navigating.
 *
 * `role="status"` with `aria-live="polite"`: the queue emptying is exactly
 * the kind of change a screen reader user gets no other signal for, and the
 * navigation that follows makes sense only if the announcement preceded it.
 *
 * @param {object} props
 * @param {string} props.title
 * @param {React.ReactNode} props.children - where the user is about to go.
 */
export function CompletionBanner({ title, children }) {
  return (
    <div className={styles.banner} role="status" aria-live="polite">
      <div className={styles.art} aria-hidden="true">
        <ConfettiLottie
          autoplay
          // Looped, despite a burst being a one-off gesture: the composition
          // runs ~2s and ends on a frame the confetti has already fallen out
          // of, so against a 3s banner the last second would be an empty box
          // that reads as a failed animation.
          loop
          style={{ width: "100%", height: "100%" }}
        />
      </div>
      <div className={styles.copy}>
        <strong className={styles.headline}>{title}</strong>
        <p className={styles.detail}>{children}</p>
      </div>
    </div>
  );
}
