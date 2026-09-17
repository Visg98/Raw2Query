import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { Logo } from "../components/common/Logo";
import {
  AskIcon,
  QueueIcon,
  ReviewIcon,
  SchemaIcon,
  SidebarToggleIcon,
  TopicsIcon,
  UploadIcon,
} from "../components/common/Icon";
import styles from "./SideNav.module.css";

const STORAGE_KEY = "raw2query.sidenav.collapsed";

/**
 * Every destination in the app, in the order a document moves through it:
 * upload → extract → review → the tables and topics it lands in → ask.
 * There is no other navigation surface, so anything reachable belongs here.
 */
const LINKS = [
  { to: "/upload", label: "Upload", Icon: UploadIcon },
  { to: "/queue", label: "Extraction queue", Icon: QueueIcon },
  { to: "/review", label: "Review queue", Icon: ReviewIcon },
  { to: "/schemas", label: "Schemas", Icon: SchemaIcon },
  { to: "/topics", label: "Topics", Icon: TopicsIcon },
  { to: "/query", label: "Ask", Icon: AskIcon },
];

/** Remembered across reloads; a narrow window starts collapsed. */
function initialCollapsed() {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored !== null) return stored === "true";
  } catch {
    // Private mode / blocked storage: fall through to the width heuristic.
  }
  return window.innerWidth < 1000;
}

export function SideNav() {
  const [collapsed, setCollapsed] = useState(initialCollapsed);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(collapsed));
    } catch {
      // Not worth surfacing — the nav still works, it just won't be
      // remembered next time.
    }
  }, [collapsed]);

  return (
    <aside className={`${styles.sidebar} ${collapsed ? styles.collapsed : ""}`} data-collapsed={collapsed}>
      <div className={styles.brandRow}>
        <NavLink to="/upload" className={styles.brand} aria-label="Raw2Query — home">
          <Logo collapsed={collapsed} />
        </NavLink>
        {!collapsed && (
          <button
            type="button"
            className={styles.toggle}
            onClick={() => setCollapsed(true)}
            aria-label="Collapse navigation"
            aria-expanded="true"
            title="Collapse navigation"
          >
            <SidebarToggleIcon open size={16} />
          </button>
        )}
      </div>

      {collapsed && (
        <button
          type="button"
          className={`${styles.toggle} ${styles.toggleStacked}`}
          onClick={() => setCollapsed(false)}
          aria-label="Expand navigation"
          aria-expanded="false"
          title="Expand navigation"
        >
          <SidebarToggleIcon open={false} size={16} />
        </button>
      )}

      <nav className={styles.nav} aria-label="Main">
        {LINKS.map(({ to, label, Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) => `${styles.link} ${isActive ? styles.active : ""}`}
            /* Collapsed, the icon is the only label on screen: the native
               tooltip and the accessible name have to carry the text. */
            title={collapsed ? label : undefined}
            aria-label={collapsed ? label : undefined}
          >
            <span className={styles.icon}>
              <Icon size={18} />
            </span>
            <span className={styles.label}>{label}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
