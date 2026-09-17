import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteSchemaRecord, getSchemaRecords, listSchemaVersions, listSchemas } from "../../api/schemas";
import { DataTable } from "../../components/common/DataTable";
import { TrashIcon } from "../../components/common/Icon";
import { useToast } from "../../components/common/Toast";
import { RecordFilters } from "./components/RecordFilters";
import { DeleteSchemaButton } from "./components/DeleteSchemaButton";

const PAGE_SIZE = 10;

export function SchemaRecordsPage() {
  const { schemaId } = useParams();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [page, setPage] = useState(0);
  const [filters, setFilters] = useState({});
  const [pendingDeleteId, setPendingDeleteId] = useState(null);

  const { data: schemas = [] } = useQuery({ queryKey: ["schemas"], queryFn: listSchemas });
  const schema = schemas.find((s) => s.id === schemaId);

  const { data: versions } = useQuery({ queryKey: ["schemaVersions", schemaId], queryFn: () => listSchemaVersions(schemaId) });

  // Filters only make sense for fields the current version still declares.
  const fields = schema?.current_version?.fields || [];

  // Columns, though, come from every version: the view exposes the union
  // across the lineage, so a field dropped by a later version still has a
  // column in the rows. Without this it would render as an unlabelled column,
  // and since columns are now opaque keys that would surface a raw
  // `f_xxxxxxxx` as a header. Newest declaration wins for the label.
  const columns = useMemo(() => {
    const labelByColumn = new Map();
    for (const version of versions || []) {
      for (const field of version.fields || []) {
        if (field.column) labelByColumn.set(field.column, field.name);
      }
    }
    // The current version last, so its names win, and its field order leads.
    const ordered = [];
    for (const field of fields) {
      if (field.column) {
        labelByColumn.set(field.column, field.name);
        ordered.push(field.column);
      }
    }
    for (const column of labelByColumn.keys()) {
      if (!ordered.includes(column)) ordered.push(column);
    }
    return ordered.map((column) => ({ key: column, label: labelByColumn.get(column) }));
  }, [versions, fields]);

  const deleteRecordMutation = useMutation({
    mutationFn: (recordId) => deleteSchemaRecord(schemaId, recordId),
    onSuccess: () => {
      showToast("Row deleted.", { variant: "success" });
      setPendingDeleteId(null);
      // Both the rows and the count come from the same query, and the count
      // drives the pager — so this has to refetch rather than filter the
      // row out locally, or "12 records" would outlive the twelfth row.
      queryClient.invalidateQueries({ queryKey: ["records", schemaId] });
    },
    onError: (err) => {
      showToast(err.message || "Could not delete this row.", { variant: "error" });
      setPendingDeleteId(null);
    },
  });

  // Appended to the schema's own columns. Two clicks rather than a confirm
  // dialog: a single row is a small, cheap loss next to a schema or a batch,
  // and a modal per row would make clearing a handful of bad rows a chore.
  // The inline "Sure?" is still a deliberate second action.
  const columnsWithActions = useMemo(
    () => [
      ...columns,
      {
        key: "__delete",
        label: "",
        // The cell keeps the width of its *armed* state at all times. The
        // table sizes itself to its content inside a horizontally scrolling
        // wrap, so a cell that grows on click pushed the confirm's "Cancel"
        // past the right edge — the user had to scroll sideways to back out
        // of a delete they had just armed.
        render: (row) => (
          <span
            style={{
              display: "inline-flex",
              justifyContent: "flex-end",
              gap: "var(--space-1)",
              minWidth: 132,
              whiteSpace: "nowrap",
            }}
          >
            {pendingDeleteId === row.id ? (
              <>
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  disabled={deleteRecordMutation.isPending}
                  onClick={() => deleteRecordMutation.mutate(row.id)}
                >
                  {deleteRecordMutation.isPending ? "Deleting…" : "Delete"}
                </button>
                <button type="button" className="btn btn-sm" onClick={() => setPendingDeleteId(null)}>
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn btn-sm btn-danger"
                onClick={() => setPendingDeleteId(row.id)}
                aria-label="Delete this row"
                title="Delete this row"
              >
                <TrashIcon size={13} />
              </button>
            )}
          </span>
        ),
      },
    ],
    [columns, pendingDeleteId, deleteRecordMutation],
  );

  const activeFilters = useMemo(() => Object.fromEntries(Object.entries(filters).filter(([, v]) => v)), [filters]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["records", schemaId, page, activeFilters],
    queryFn: () => getSchemaRecords(schemaId, { limit: PAGE_SIZE, offset: page * PAGE_SIZE, filters: activeFilters }),
    enabled: Boolean(schemaId),
    // Keeps the current page on screen while the next one loads instead of
    // flashing the skeleton, which on a 10-row page is most of the view.
    placeholderData: (previous) => previous,
  });
  const rows = data?.rows;
  const total = data?.total;

  return (
    <div className="page">
      <div className="page-header">
        <h1>{schema?.name || "…"}</h1>
        <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
          <span className="muted">v{schema?.current_version?.version ?? 1}</span>
          {versions?.some((v) => v.is_breaking_from_prev) && (
            <span style={{ color: "var(--color-warning)" }}>⚠ breaking change in history</span>
          )}
          <Link to={`/schemas/${schemaId}/edit`} className="btn btn-sm">
            Edit fields
          </Link>
          {schema && (
            <DeleteSchemaButton
              schemaId={schemaId}
              schemaName={schema.name}
              documentCount={schema.document_count}
            />
          )}
        </div>
      </div>

      {fields.length > 0 && <RecordFilters fields={fields} filters={filters} onChange={(f) => { setFilters(f); setPage(0); }} onClear={() => setFilters({})} />}

      {/* Without this, a failing query left `rows` undefined and the table
          fell through to its "No records yet." empty state - indistinguishable
          from a schema that genuinely has no data, which is how a broken
          column cast looked for a while. */}
      {error && (
        <div
          className="card"
          style={{ padding: "var(--space-4)", marginBottom: "var(--space-4)", background: "var(--color-danger-bg)", borderColor: "var(--color-danger)" }}
        >
          <strong>Couldn’t load these records.</strong>
          <p className="muted" style={{ margin: "4px 0 0" }}>{error.message}</p>
        </div>
      )}

      <div className="card" style={{ padding: "var(--space-4)", marginBottom: "var(--space-4)" }}>
        <DataTable
          columns={columnsWithActions}
          rows={rows}
          loading={isLoading}
          emptyState={Object.keys(activeFilters).length ? "No records match your filters." : "No records yet."}
          pagination={{ page, pageSize: PAGE_SIZE, total, onPageChange: setPage }}
        />
      </div>
    </div>
  );
}
