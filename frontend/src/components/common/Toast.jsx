import { createContext, useCallback, useContext, useState } from "react";
import { createPortal } from "react-dom";
import styles from "./Toast.module.css";

const ToastContext = createContext(null);

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  const dismiss = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const showToast = useCallback(
    (message, { variant = "info", durationMs = 5000 } = {}) => {
      const id = crypto.randomUUID();
      setToasts((prev) => [...prev, { id, message, variant }]);
      if (durationMs) setTimeout(() => dismiss(id), durationMs);
      return id;
    },
    [dismiss],
  );

  // Portalled to <body> rather than rendered in place. Every toast - error,
  // success, info - has to land in the same corner, and `position: fixed`
  // does not guarantee that on its own: a `transform`, `filter`, `backdrop-
  // filter` or `contain` anywhere above the stack makes that ancestor the
  // containing block, and the toast then anchors to *its* corner instead of
  // the viewport's. That is what put some messages in the bottom right while
  // others appeared top right. <body> can never be that ancestor, so the
  // stack's top/right is always the viewport's top right.
  return (
    <ToastContext.Provider value={{ showToast, dismiss }}>
      {children}
      {createPortal(
        <div className={styles.stack} role="region" aria-label="Notifications">
          {toasts.map((t) => (
            <div
              key={t.id}
              className={`${styles.toast} ${styles[t.variant] || ""}`}
              // Errors interrupt; a success or info note is announced without
              // cutting off whatever the screen reader is already reading.
              role={t.variant === "error" ? "alert" : "status"}
              aria-live={t.variant === "error" ? "assertive" : "polite"}
              onClick={() => dismiss(t.id)}
            >
              {t.message}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
