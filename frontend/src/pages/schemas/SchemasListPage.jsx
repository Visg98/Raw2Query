import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listSchemas } from "../../api/schemas";
import { SchemaCard } from "./components/SchemaCard";

export function SchemasListPage() {
  const { data: schemas, isLoading } = useQuery({ queryKey: ["schemas"], queryFn: listSchemas });

  return (
    <div className="page">
      <div className="page-header">
        <h1>Schemas</h1>
        <Link to="/schemas/new" className="btn btn-primary">
          + New schema
        </Link>
      </div>

      {isLoading && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "var(--space-4)" }}>
          {[...Array(3)].map((_, i) => (
            <div key={i} className="skeleton" style={{ height: 120 }} />
          ))}
        </div>
      )}

      {!isLoading && !schemas?.length && (
        <div className="empty-state">
          No schemas yet. <Link to="/schemas/new">Create one</Link>, or define one while uploading documents.
        </div>
      )}

      {!isLoading && schemas?.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "var(--space-4)" }}>
          {schemas.map((schema) => (
            <SchemaCard key={schema.id} schema={schema} />
          ))}
        </div>
      )}
    </div>
  );
}
