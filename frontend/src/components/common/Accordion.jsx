import styles from "./Accordion.module.css";

export function Accordion({ title, subtitle, open, onToggle, badge, children }) {
  return (
    <div className={`card ${styles.section}`}>
      <button type="button" className={styles.header} onClick={onToggle} aria-expanded={open}>
        <div>
          <div className={styles.title}>
            {title}
            {badge}
          </div>
          {subtitle && <div className={`muted ${styles.subtitle}`}>{subtitle}</div>}
        </div>
        <span className={styles.chevron}>{open ? "−" : "+"}</span>
      </button>
      {open && <div className={styles.body}>{children}</div>}
    </div>
  );
}
