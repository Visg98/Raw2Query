import { useCallback, useRef, useState } from "react";
import styles from "./FileDropzone.module.css";

export function FileDropzone({ onFilesSelected, multiple = true }) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef(null);

  const handleFiles = useCallback(
    (fileList) => {
      const files = Array.from(fileList || []);
      if (files.length) onFilesSelected(files);
    },
    [onFilesSelected],
  );

  return (
    <div
      className={`${styles.zone} ${dragging ? styles.dragging : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
      role="button"
      tabIndex={0}
    >
      <input
        ref={inputRef}
        type="file"
        multiple={multiple}
        hidden
        onChange={(e) => {
          handleFiles(e.target.files);
          e.target.value = "";
        }}
      />
      <p>
        <strong>Drop files here</strong> or click to browse
      </p>
      <p className="muted">PDFs, Word docs, spreadsheets, scanned images — anything `unstructured` can parse.</p>
    </div>
  );
}
