import styles from "./DataTable.module.css";

/**
 * Generic, presentational-only table. Pagination state lives in the parent
 * page — this component just renders whatever page of rows it's given.
 *
 * @param {{key: string, label: string, render?: (row: object) => any}[]} columns
 * @param {object[]} rows
 * @param {{page: number, pageSize: number, total?: number, onPageChange: (page:number)=>void}} [pagination]
 */
export function DataTable({ columns, rows, loading, emptyState, pagination }) {
  if (loading) {
    return (
      <div className={styles.wrap}>
        {[...Array(4)].map((_, i) => (
          <div key={i} className={`skeleton ${styles.skeletonRow}`} />
        ))}
      </div>
    );
  }

  const isEmpty = !rows || rows.length === 0;

  // An empty page past the first one still renders the pager. Returning early
  // here (as this used to, for any empty result) hid the only control that
  // could get the user back, so landing on a blank page - which was easy,
  // since "Next" had nothing to disable it - was a dead end that looked like
  // the table itself had broken.
  if (isEmpty && !(pagination && pagination.page > 0)) {
    return <div className="empty-state">{emptyState || "No rows to show."}</div>;
  }

  return (
    <div className={styles.wrap}>
      {isEmpty ? (
        <div className="empty-state">{emptyState || "No rows to show."}</div>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={col.key}>{col.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={row.id ?? i}>
                {columns.map((col) => (
                  <td key={col.key}>{col.render ? col.render(row) : String(row[col.key] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pagination && (
        <div className={styles.pager}>
          <button
            type="button"
            className="btn btn-sm"
            disabled={pagination.page <= 0}
            onClick={() => pagination.onPageChange(pagination.page - 1)}
          >
            Previous
          </button>
          <span className="muted">
            Page {pagination.page + 1}
            {pagination.total != null ? ` of ${Math.max(1, Math.ceil(pagination.total / pagination.pageSize))}` : ""}
            {pagination.total != null ? ` · ${pagination.total} record${pagination.total === 1 ? "" : "s"}` : ""}
          </span>
          <button
            type="button"
            className="btn btn-sm"
            // With no `total` there is nothing to compare against, so fall
            // back to the page itself: a short page is the last page. Without
            // this, a caller that doesn't supply a total left Next enabled
            // forever and paged off the end of the data.
            disabled={
              pagination.total != null
                ? (pagination.page + 1) * pagination.pageSize >= pagination.total
                : isEmpty || rows.length < pagination.pageSize
            }
            onClick={() => pagination.onPageChange(pagination.page + 1)}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
