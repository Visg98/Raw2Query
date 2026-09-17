import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Accordion } from "../../components/common/Accordion";
import { FileDropzone } from "../../components/FileDropzone";
import { TopicChipInput } from "../../components/TopicChipInput";
import { useToast } from "../../components/common/Toast";
import { uploadDocuments } from "../../api/documents";
import { createSchema } from "../../api/schemas";
import { StagedFileList } from "./components/StagedFileList";
import { SchemaPicker } from "./components/SchemaPicker";

const SECTIONS = { documents: "documents", schema: "schema", topics: "topics" };

export function UploadPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [openSections, setOpenSections] = useState(() => new Set([SECTIONS.documents]));

  const [files, setFiles] = useState([]);

  const [schemaMode, setSchemaMode] = useState("none");
  const [schemaId, setSchemaId] = useState(null);
  const [newSchema, setNewSchema] = useState({ name: "", fields: [], identityFields: [] });

  const [topicIds, setTopicIds] = useState([]);

  const createSchemaMutation = useMutation({ mutationFn: createSchema });
  const uploadMutation = useMutation({ mutationFn: ({ files: f, hints }) => uploadDocuments(f, hints) });

  function isOpen(section) {
    return openSections.has(section);
  }

  // Manual click: radio-style, same as before - opening one section closes
  // the others (or closes it if it's the only one already open).
  function toggle(section) {
    setOpenSections((current) => (current.size === 1 && current.has(section) ? new Set() : new Set([section])));
  }

  // Auto-open on validation error: additive, never closes another section.
  // A schema error surfacing while the reviewer already has e.g. Topics
  // open used to force-close it via the old single-value `openSection`,
  // hiding a topic they'd already picked even though it was still selected
  // (the subtitle still said so) - just no longer visible to look at.
  function ensureOpen(section) {
    setOpenSections((current) => (current.has(section) ? current : new Set([...current, section])));
  }

  const schemaSummary = useMemo(() => {
    if (schemaMode === "existing" && schemaId) return "1 schema selected";
    if (schemaMode === "new" && newSchema.name) return `New schema “${newSchema.name}”`;
    return "Auto-detect per document";
  }, [schemaMode, schemaId, newSchema.name]);

  async function handleExtract() {
    if (!files.length) {
      showToast("Add at least one document first.", { variant: "error" });
      ensureOpen(SECTIONS.documents);
      return;
    }

    let hintSchemaId = schemaMode === "existing" ? schemaId : null;

    if (schemaMode === "new") {
      if (!newSchema.name.trim() || !newSchema.fields.length) {
        showToast("Give the new schema a name and at least one field.", { variant: "error" });
        ensureOpen(SECTIONS.schema);
        return;
      }
      try {
        const created = await createSchemaMutation.mutateAsync({
          name: newSchema.name.trim(),
          fields: newSchema.fields,
          identityFields: newSchema.identityFields,
        });
        hintSchemaId = created.id;
      } catch (err) {
        showToast(err.message || "Could not create schema.", { variant: "error" });
        ensureOpen(SECTIONS.schema);
        return;
      }
    }

    try {
      const result = await uploadMutation.mutateAsync({ files, hints: { schemaId: hintSchemaId, topicIds } });
      // Straight to the standing extraction queue rather than this batch's
      // own progress URL: it shows the same live rows plus anything already
      // in flight from an earlier upload, and it survives a reload.
      showToast(
        `${result.documents?.length ?? files.length} document${(result.documents?.length ?? files.length) === 1 ? "" : "s"} queued for extraction.`,
        { variant: "success" },
      );
      navigate("/queue");
    } catch (err) {
      showToast(err.message || "Upload failed.", { variant: "error" });
    }
  }

  const isSubmitting = createSchemaMutation.isPending || uploadMutation.isPending;

  return (
    <div className="page">
      <div className="page-header">
        <h1>Upload documents</h1>
        <p className="muted">Hints you set here apply to every file as a default — each document still gets its own review.</p>
      </div>

      <Accordion
        title="1 · Documents"
        subtitle={files.length ? `${files.length} file(s) staged` : "Drag & drop or browse"}
        open={isOpen(SECTIONS.documents)}
        onToggle={() => toggle(SECTIONS.documents)}
      >
        <FileDropzone onFilesSelected={(picked) => setFiles((prev) => [...prev, ...picked])} />
        <StagedFileList files={files} onRemove={(index) => setFiles((prev) => prev.filter((_, i) => i !== index))} />
      </Accordion>

      <Accordion
        title="2 · Schema"
        subtitle={schemaSummary}
        open={isOpen(SECTIONS.schema)}
        onToggle={() => toggle(SECTIONS.schema)}
      >
        <SchemaPicker
          mode={schemaMode}
          onModeChange={setSchemaMode}
          schemaId={schemaId}
          onSchemaIdChange={setSchemaId}
          newSchema={newSchema}
          onNewSchemaChange={setNewSchema}
        />
      </Accordion>

      <Accordion
        title="3 · Topics"
        subtitle={topicIds.length ? `${topicIds.length} topic(s) selected` : "Optional — pick or create topics"}
        open={isOpen(SECTIONS.topics)}
        onToggle={() => toggle(SECTIONS.topics)}
      >
        <TopicChipInput selectedIds={topicIds} onChange={setTopicIds} />
      </Accordion>

      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-4)" }}>
        <button type="button" className="btn btn-primary" disabled={isSubmitting} onClick={handleExtract}>
          {isSubmitting ? "Starting extraction…" : `Extract ${files.length || ""} document${files.length === 1 ? "" : "s"}`}
        </button>
      </div>
    </div>
  );
}
