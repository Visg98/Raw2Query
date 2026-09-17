/**
 * Small inline SVG icons.
 *
 * Deliberately not font glyphs or emoji: "＋", "◳", "✎" and "🗑" all render
 * as tofu boxes in the app's font stack on Linux, which is where this runs.
 * An inline SVG looks the same everywhere and scales with the text.
 *
 * Each is `aria-hidden` — every call site pairs it with a real label or an
 * `aria-label`, so announcing the shape as well would only be noise.
 */
function Svg({ children, size = 14 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      style={{ flex: "0 0 auto" }}
    >
      {children}
    </svg>
  );
}

export function PlusIcon(props) {
  return (
    <Svg {...props}>
      <path d="M8 3.5v9M3.5 8h9" />
    </Svg>
  );
}

export function PencilIcon(props) {
  return (
    <Svg {...props}>
      <path d="M11.3 2.7a1.4 1.4 0 0 1 2 2L5.6 12.4l-2.8.8.8-2.8z" />
    </Svg>
  );
}

export function TrashIcon(props) {
  return (
    <Svg {...props}>
      <path d="M2.8 4.5h10.4M6.2 4.5V3.2h3.6v1.3M4.3 4.5l.6 8.3h6.2l.6-8.3M6.6 7v3.6M9.4 7v3.6" />
    </Svg>
  );
}

/** Stacked pages — the "sources" affordance under an answer. */
export function SourcesIcon(props) {
  return (
    <Svg {...props}>
      <path d="M5.5 2.5h5l2 2v7h-7z" />
      <path d="M10.5 2.5v2h2" />
      <path d="M3.5 5v8.5h7" />
    </Svg>
  );
}

export function BackIcon(props) {
  return (
    <Svg {...props}>
      <path d="M12.5 8h-9M6.5 4.5 3 8l3.5 3.5" />
    </Svg>
  );
}

/* ── Side nav icons ──────────────────────────────────────────────────────
   One per destination, drawn on the same 16-unit grid and at the same
   stroke weight as the icons above so a nav rail of them reads as a set.
   Each is a literal of what the page does — a tray with an up arrow for
   Upload, a spinner arc for the extraction queue — rather than an
   abstract glyph, because collapsed the icon is the only label left. */

/** Tray with an arrow going in — Upload. */
export function UploadIcon(props) {
  return (
    <Svg {...props}>
      <path d="M8 10.5V2.5M5 5.5 8 2.5l3 3" />
      <path d="M2.5 9.5v3a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-3" />
    </Svg>
  );
}

/** Open-ended circular arc, the universal "work in progress" — Extraction queue. */
export function QueueIcon(props) {
  return (
    <Svg {...props}>
      <path d="M8 2.2a5.8 5.8 0 1 1-5.5 4" />
      <path d="M2.2 2.4v3.9h3.9" />
      <circle cx="8" cy="8" r="1.4" />
    </Svg>
  );
}

/** Checklist with a tick — Review queue. */
export function ReviewIcon(props) {
  return (
    <Svg {...props}>
      <path d="M2.5 4h4M2.5 8h4M2.5 12h4" />
      <path d="M8.5 7.6l1.9 1.9 3.1-3.9" />
    </Svg>
  );
}

/** Table with a pinned header row — Schemas (shape, not contents). */
export function SchemaIcon(props) {
  return (
    <Svg {...props}>
      <rect x="2.3" y="2.8" width="11.4" height="10.4" rx="1.2" />
      <path d="M2.3 6.2h11.4M6.4 6.2v7M10.4 2.8v10.4" />
    </Svg>
  );
}

/** Tag on a string — Topics. */
export function TopicsIcon(props) {
  return (
    <Svg {...props}>
      <path d="M8.4 2.4H13a.6.6 0 0 1 .6.6v4.6a1 1 0 0 1-.3.7l-5.3 5.3a1 1 0 0 1-1.4 0L2.8 10a1 1 0 0 1 0-1.4l5.3-5.3a1 1 0 0 1 .3-.9z" />
      <circle cx="10.9" cy="5.1" r="0.9" />
    </Svg>
  );
}

/** Speech bubble with a magnifier — Ask (a question answered from the data). */
export function AskIcon(props) {
  return (
    <Svg {...props}>
      <path d="M13.5 8.6a4.9 4.9 0 0 1-5 4.9H5.6L2.5 15v-3.2A4.9 4.9 0 0 1 7.6 2.5h.9a4.9 4.9 0 0 1 5 4.9z" />
      <circle cx="7.4" cy="7.6" r="1.9" />
      <path d="M8.9 9.1 10.4 10.6" />
    </Svg>
  );
}

/**
 * Panel with a caret — the side nav's collapse control. The caret's
 * direction is set by the caller (`open`), so the button shows which way
 * the rail is about to move rather than what state it's in.
 */
export function SidebarToggleIcon({ open = true, ...props }) {
  return (
    <Svg {...props}>
      <rect x="2.2" y="2.8" width="11.6" height="10.4" rx="1.4" />
      <path d="M6.4 2.8v10.4" />
      {open ? <path d="M11.4 6.4 9.6 8l1.8 1.6" /> : <path d="M9.6 6.4 11.4 8l-1.8 1.6" />}
    </Svg>
  );
}
