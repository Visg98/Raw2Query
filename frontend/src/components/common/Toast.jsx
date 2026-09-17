import { createContext, useCallback, useContext, useState } from "react";
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

  return (
    <ToastContext.Provider value={{ showToast, dismiss }}>
      {children}
      <div className={styles.stack}>
        {toasts.map((t) => (
          <div key={t.id} className={`${styles.toast} ${styles[t.variant] || ""}`} onClick={() => dismiss(t.id)}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
