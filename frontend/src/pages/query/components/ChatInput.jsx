import { useState } from "react";

export function ChatInput({ onSubmit, disabled }) {
  const [value, setValue] = useState("");

  function handleSubmit(e) {
    e.preventDefault();
    if (!value.trim()) return;
    onSubmit(value.trim());
    setValue("");
  }

  return (
    <form onSubmit={handleSubmit} style={{ display: "flex", gap: "var(--space-2)" }}>
      <textarea
        className="text-input"
        rows={2}
        placeholder="Ask a question about your documents…"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) handleSubmit(e);
        }}
      />
      <button type="submit" className="btn btn-primary" disabled={disabled || !value.trim()}>
        Ask
      </button>
    </form>
  );
}
