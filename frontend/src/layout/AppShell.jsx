import { Outlet } from "react-router-dom";
import { SideNav } from "./SideNav";
import styles from "./AppShell.module.css";

/**
 * Side nav on the left, page content on the right. The nav replaced the old
 * top header outright — a single navigation surface means a page never has
 * to work out which chrome owns a link.
 */
export function AppShell() {
  return (
    <div className={styles.shell}>
      <SideNav />
      <main className={styles.content}>
        <Outlet />
      </main>
    </div>
  );
}
