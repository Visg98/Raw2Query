import { useQuery } from "@tanstack/react-query";
import { listSchemas } from "../../../api/schemas";

/** Lets the reviewer override which schema this document's data belongs to
 * (decision #9/#11 — the match is always overridable per document). */
export function SchemaMatchSwitcher({ matchedSchemaId, onChange }) {
  const { data: schemas = [] } = useQuery({ queryKey: ["schemas"], queryFn: listSchemas });

  return (
    <div>
      <label className="field-label">Matched schema</label>
      <select className="text-input" value={matchedSchemaId || ""} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">— Ad hoc / no schema —</option>
        {schemas.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name} (v{s.current_version?.version ?? 1})
          </option>
        ))}
      </select>
    </div>
  );
}
