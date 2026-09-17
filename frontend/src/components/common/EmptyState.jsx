import { createLazyLottie } from "./lazyLottie";
import styles from "./EmptyState.module.css";

/** The "empty ghost" composition in src/assets — a 600x600 square. */
const GhostLottie = createLazyLottie(() => import("../../assets/empty ghost.json"));

/**
 * The nothing-here state for a queue: a drifting ghost, a headline, and the
 * places to go instead.
 *
 * Centred in the remaining height rather than pinned under the page header,
 * because on an empty queue it *is* the page — a dashed box at the top of a
 * screen of whitespace reads as something that failed to load.
 *
 * The links are the point of the component. An empty queue is not an error
 * and usually not even a surprise: it means the work moved on somewhere
 * else, and the only unhelpful version of this screen is one that says
 * "nothing here" without saying where to look.
 *
 * @param {object} props
 * @param {string} props.title
 * @param {React.ReactNode} props.children - the explanation, links and all.
 */
export function EmptyState({ title, children }) {
  return (
    <div className={styles.wrap}>
      {/* Reserve the box so the copy doesn't jump when the animation chunk
          lands, and keep the drawing out of the accessibility tree — the
          heading and body already say everything it says. */}
      <div className={styles.art} aria-hidden="true">
        <GhostLottie loop autoplay style={{ width: "100%", height: "100%" }} />
      </div>
      <h2 className={styles.title}>{title}</h2>
      <div className={styles.body}>{children}</div>
    </div>
  );
}
