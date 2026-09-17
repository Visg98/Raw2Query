import styles from "./ConfidenceFlag.module.css";

/**
 * Flags any extracted field whose confidence falls below the confirmed
 * threshold (0.75) — used inline in the review editor and to compute a
 * job's "clean vs. needs review" status on the queue.
 */
export function ConfidenceFlag({ value, threshold = 0.75 }) {
  if (value === undefined || value === null) return null;
  const pct = Math.round(value * 100);
  const low = value < threshold;
  if (!low) return null;
  return (
    <span className={styles.flag} title={`${pct}% confidence — below the ${Math.round(threshold * 100)}% review threshold`}>
      ⚠ {pct}%
    </span>
  );
}

export function isLowConfidence(value, threshold = 0.75) {
  return typeof value === "number" && value < threshold;
}

export function hasAnyLowConfidence(fieldConfidence, threshold = 0.75) {
  if (!fieldConfidence) return false;
  return Object.values(fieldConfidence).some((v) => isLowConfidence(v, threshold));
}

/**
 * Over a whole `data_confidence` list — one score map per extracted object.
 *
 * Reading only the first map would call a 40-line invoice clean on the
 * strength of its header, which is the one case the queue's flag most needs
 * to catch. Mirrors `confirm.is_job_clean` server-side.
 */
export function hasAnyLowConfidenceInAny(groups, threshold = 0.75) {
  if (!Array.isArray(groups)) return false;
  return groups.some((group) => hasAnyLowConfidence(group, threshold));
}
