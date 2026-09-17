import styles from "./StatusBadge.module.css";

const LABELS = {
  pending: "Pending",
  extracting: "Extracting",
  awaiting_review: "Awaiting review",
  confirmed: "Confirmed",
  rejected: "Rejected",
  failed: "Failed",
};

export function StatusBadge({ status }) {
  if (!status) return <span className={`${styles.badge} ${styles.unknown}`}>—</span>;
  return <span className={`${styles.badge} ${styles[status] || styles.unknown}`}>{LABELS[status] || status}</span>;
}
