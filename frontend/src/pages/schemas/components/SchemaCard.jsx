import { Link } from "react-router-dom";
import styles from "./SchemaCard.module.css";

export function SchemaCard({ schema }) {
  const version = schema.current_version;
  return (
    <Link to={`/schemas/${schema.id}`} className={`card ${styles.card}`}>
      <h3 className={styles.name}>{schema.name}</h3>
      <p className="muted">
        v{version?.version ?? 1} · {version?.fields?.length ?? 0} field{version?.fields?.length === 1 ? "" : "s"}
      </p>
      <div className={styles.fieldPreview}>
        {(version?.fields || []).slice(0, 4).map((f) => (
          <span key={f.name} className={styles.fieldTag}>
            {f.name}
          </span>
        ))}
        {(version?.fields?.length || 0) > 4 && <span className="muted">+{version.fields.length - 4} more</span>}
      </div>
    </Link>
  );
}
