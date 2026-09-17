import { useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import styles from "./Modal.module.css";

/**
 * Overlay dialog. Rendered into a portal on `document.body` so it isn't
 * clipped by an ancestor's `overflow: hidden` — the source list opens from
 * inside a chat bubble and the document viewer from inside the review
 * page's scroll pane.
 *
 * `size="wide"` is for the document viewer, which needs the room.
 */
export function Modal({ title, subtitle, onClose, children, footer, size = "default" }) {
  const overlayRef = useRef(null);

  const handleKeyDown = useCallback(
    (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    },
    [onClose],
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    // Stop the page behind the overlay from scrolling with the wheel.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [handleKeyDown]);

  return createPortal(
    <div
      className={styles.overlay}
      ref={overlayRef}
      // Close on a backdrop click only — `onClick` on the overlay would also
      // fire for a click that started inside the dialog and ended on the
      // backdrop (a text selection drag), closing the dialog mid-selection.
      onMouseDown={(event) => {
        if (event.target === overlayRef.current) onClose();
      }}
    >
      <div className={`${styles.dialog} ${size === "wide" ? styles.wide : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header className={styles.header}>
          <div className={styles.titleBlock}>
            <h3 className={styles.title}>{title}</h3>
            {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
          </div>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className={styles.body}>{children}</div>
        {footer && <footer className={styles.footer}>{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
