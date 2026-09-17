import styles from "./StagedFileList.module.css";

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function StagedFileList({ files, onRemove }) {
  if (!files.length) return null;
  return (
    <ul className={styles.list}>
      {files.map((file, index) => (
        <li key={`${file.name}-${index}`} className={styles.item}>
          <span className={styles.name}>{file.name}</span>
          <span className="muted">{formatBytes(file.size)}</span>
          <button type="button" className="btn btn-sm btn-danger" onClick={() => onRemove(index)}>
            Remove
          </button>
        </li>
      ))}
    </ul>
  );
}
