import { useState } from "react";
import styles from "./ChunkCards.module.css";

/**
 * The chunks this document will be stored as for retrieval, one card each.
 *
 * Every document is chunked and embedded regardless of what the schema
 * captured (decision #15), so these are where the information the schema
 * didn't anticipate survives. Showing them on review makes that deliberate
 * redundancy visible: the reviewer can see the passage a field was read out
 * of, and see what will still be answerable even where extraction found
 * nothing.
 *
 * Read-only. Chunks are derived from the file, so correcting one here would
 * only desynchronize it from the document — a bad extraction is fixed by
 * re-extracting, not by editing its chunks.
 *
 * @param {{content: string, metadata?: {page_number?: number, element_types?: string[]}}[]} chunks
 */
export function ChunkCards({ chunks }) {
  const [expanded, setExpanded] = useState(() => new Set());

  function toggle(index) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  if (!chunks?.length) return null;

  return (
    <div className={styles.list}>
      {chunks.map((chunk, index) => {
        const pageNumber = chunk?.metadata?.page_number;
        const elementTypes = chunk?.metadata?.element_types || [];
        const isExpanded = expanded.has(index);
        return (
          <article className={styles.card} key={index}>
            <header className={styles.cardHeader}>
              <span className={styles.index}>{index + 1}</span>
              <div className={styles.cardMeta}>
                {pageNumber != null && <span>page {pageNumber}</span>}
                {elementTypes.length > 0 && (
                  <span className="muted">
                    {pageNumber != null ? " · " : ""}
                    {elementTypes.join(", ")}
                  </span>
                )}
              </div>
            </header>
            <p className={isExpanded ? styles.contentFull : styles.content}>{chunk?.content}</p>
            <button type="button" className="btn btn-sm" onClick={() => toggle(index)}>
              {isExpanded ? "Show less" : "Show more"}
            </button>
          </article>
        );
      })}
    </div>
  );
}
