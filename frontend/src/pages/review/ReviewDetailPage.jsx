import { useEffect, useReducer, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { confirmJob, getJobReview, patchJobReview, reextractJob, rejectJob } from "../../api/jobs";
import { listSchemas } from "../../api/schemas";
import { searchTopics } from "../../api/topics";
import { useToast } from "../../components/common/Toast";
import { useJobEvents } from "../../hooks/useJobEvents";
import { Accordion } from "../../components/common/Accordion";
import { SchemaFieldEditor } from "../../components/SchemaFieldEditor";
import { StatusBadge } from "../../components/common/StatusBadge";
import { DocumentPreviewPane } from "./components/DocumentPreviewPane";
import { ExtractedDataTable } from "./components/ExtractedDataTable";
import { ChunkCards } from "./components/ChunkCards";
import { ReextractPanel } from "./components/ReextractPanel";
import { DedupBanner } from "./components/DedupBanner";
import { SuggestedTopicsChips } from "./components/SuggestedTopicsChips";
import { SchemaMatchSwitcher } from "./components/SchemaMatchSwitcher";

const initialDraft = {
  extractedData: [],
  proposedSchemaFields: null,
  matchedSchemaId: null,
  topicIds: [],
  newTopicNames: [],
  dedupDecision: null,
  saveSchemaAs: "",
  // Which extraction run the draft reflects, counted as the number of
  // re-extracts recorded on the job. -1 means "nothing seeded yet"; a
  // completed re-extract bumps the server's count past it and re-seeds.
  seededRun: -1,
};

function reducer(state, action) {
  switch (action.type) {
    case "seed":
      return { ...state, ...action.payload, seededRun: action.run };
    case "set_extracted_data":
      return { ...state, extractedData: action.value };
    case "set_proposed_fields":
      return { ...state, proposedSchemaFields: action.value };
    case "set_matched_schema":
      return { ...state, matchedSchemaId: action.value, ...(action.value ? { proposedSchemaFields: null, saveSchemaAs: "" } : {}) };
    case "set_topic_ids":
      return { ...state, topicIds: action.value };
    case "set_save_schema_as":
      return { ...state, saveSchemaAs: action.value };
    case "set_dedup_decision":
      return { ...state, dedupDecision: action.value };
    default:
      return state;
  }
}

export function ReviewDetailPage() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [draft, dispatch] = useReducer(reducer, initialDraft);

  const { data: review, isLoading } = useQuery({ queryKey: ["jobReview", jobId], queryFn: () => getJobReview(jobId) });
  const { data: allTopics = [] } = useQuery({ queryKey: ["topics", ""], queryFn: () => searchTopics("") });
  // Same query key as SchemaMatchSwitcher, so this shares that cache entry
  // rather than adding a fetch. Needed for the table's columns: a field the
  // model found nothing for has no key in `extracted_data` to read a column
  // name off, so the column list has to come from the schema.
  const { data: allSchemas = [] } = useQuery({ queryKey: ["schemas"], queryFn: listSchemas });

  // Seed the local draft from the server payload - once per extraction run,
  // not once per mount. Keyed on the re-extract count rather than a boolean
  // so a completed re-extract re-seeds the table, while an ordinary refetch
  // (every PATCH writes the response back into the cache) leaves the
  // reviewer's in-progress edits alone.
  //
  // Field names here follow the *read* shape (suggested_topic_ids /
  // confirmed_new_topic_names) — they only translate back to JobReviewPatch's
  // topic_ids/new_topic_names at PATCH time (see api/jobs.js#patchJobReview).
  const reextractRun = (review?.result?.reextract_history || []).length;
  useEffect(() => {
    if (review && draft.seededRun !== reextractRun) {
      const result = review.result || {};
      dispatch({
        type: "seed",
        run: reextractRun,
        payload: {
          // Guarded on type, not shape: `job.result` is an untyped blob any
          // client can PATCH, and a non-array here would take the table's
          // column derivation straight to a TypeError and blank the page.
          extractedData: Array.isArray(result.extracted_data) ? result.extracted_data : [],
          proposedSchemaFields: result.proposed_schema_fields || null,
          matchedSchemaId: result.matched_schema_id || null,
          topicIds: result.suggested_topic_ids || [],
          newTopicNames: result.confirmed_new_topic_names || [],
        },
      });
    }
  }, [review, draft.seededRun, reextractRun]);

  const patchMutation = useMutation({
    mutationFn: (patch) => patchJobReview(jobId, patch),
    onSuccess: (updated) => queryClient.setQueryData(["jobReview", jobId], updated),
  });

  const confirmMutation = useMutation({
    mutationFn: async () => {
      await patchMutation.mutateAsync({
        extractedData: draft.extractedData,
        proposedSchemaFields: draft.proposedSchemaFields,
        matchedSchemaId: draft.matchedSchemaId,
        topicIds: draft.topicIds,
        newTopicNames: draft.newTopicNames,
      });
      return confirmJob(jobId, { dedupDecision: draft.dedupDecision || undefined, saveSchemaAs: draft.saveSchemaAs || undefined });
    },
    onSuccess: () => {
      showToast("Confirmed — now part of the live dataset.", { variant: "success" });
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      navigate("/review");
    },
    onError: (err) => showToast(err.message || "Could not confirm.", { variant: "error" }),
  });

  const rejectMutation = useMutation({
    mutationFn: () => rejectJob(jobId),
    onSuccess: () => {
      showToast("Rejected.", { variant: "info" });
      navigate("/review");
    },
  });

  // A re-extract puts the job back through the worker, so the page has to
  // watch it the way the processing queue does. The SSE stream closes once
  // the status is terminal, so the hook is only enabled for the duration of
  // a run - flipping it back on re-runs the effect for the next one.
  const [reextracting, setReextracting] = useState(false);
  const [chunksOpen, setChunksOpen] = useState(false);
  const { status: liveStatus, progress: liveProgress } = useJobEvents(jobId, { enabled: reextracting });

  const reextractMutation = useMutation({
    mutationFn: (feedback) => reextractJob(jobId, feedback),
    onSuccess: () => setReextracting(true),
    onError: (err) => showToast(err.message || "Could not start the re-extraction.", { variant: "error" }),
  });

  // Refetch once the worker is done, whichever way it went. The seeding
  // effect above notices the bumped re-extract count and reloads the table
  // from the new result.
  //
  // No guard against reading a stale status: the endpoint commits
  // `status = "pending"` before it responds, and this only arms once that
  // response has landed - so an `awaiting_review` seen here is always this
  // run finishing, even when the worker beats the SSE connection.
  useEffect(() => {
    if (!reextracting || (liveStatus !== "awaiting_review" && liveStatus !== "failed")) return;
    setReextracting(false);
    queryClient.invalidateQueries({ queryKey: ["jobReview", jobId] });
    if (liveStatus === "failed") {
      showToast("The re-extraction failed. The previous table is unchanged.", { variant: "error" });
    } else {
      showToast("Re-extracted with your feedback.", { variant: "success" });
    }
  }, [reextracting, liveStatus, jobId, queryClient, showToast]);

  if (isLoading) return <div className="page skeleton" style={{ height: 400 }} />;
  if (!review) return <div className="page empty-state">Job not found.</div>;

  const result = review.result || {};
  const dedupMatch = result.dedup_match;
  // A dedup match no longer gates Confirm - duplicates are acceptable, so
  // leaving the choice untouched just keeps both records (see confirm.py).
  const confirmDisabled =
    confirmMutation.isPending || reextracting || review.status !== "awaiting_review";
  const isAdHoc = !draft.matchedSchemaId && Boolean(draft.proposedSchemaFields);
  const chunks = result.chunks || [];

  // The table's columns, in schema declaration order. Taken from the matched
  // schema's current version, or from the ad hoc proposal when there is no
  // match - i.e. from the same field list the backend extracted against, so
  // the columns are the ones Confirm will actually store.
  const activeFields = isAdHoc
    ? draft.proposedSchemaFields || []
    : allSchemas.find((s) => s.id === draft.matchedSchemaId)?.current_version?.fields || [];
  const declaredColumns = activeFields.map((f) => ({ name: f.name, scope: f.scope || "document" }));
  // Any key the payload actually carries but the current field list doesn't
  // declare is appended rather than dropped. That covers two cases: an ad hoc
  // document, where the columns can only come from the data, and a schema
  // edited between extraction and review - where hiding a column would
  // silently confirm values the reviewer never saw. Their scope is unknown,
  // so they stay cell-local.
  const seenColumns = [...new Set((draft.extractedData || []).flatMap((o) => Object.keys(o || {})))];
  const tableColumns = [
    ...declaredColumns,
    ...seenColumns
      .filter((name) => !declaredColumns.some((c) => c.name === name))
      .map((name) => ({ name, scope: undefined })),
  ];

  return (
    <div className="page">
      <div className="page-header">
        <h1>{review.document.filename}</h1>
        <StatusBadge status={review.status} />
      </div>

      {/* `minmax(0, 1fr)`, not `1fr`: a grid track's implicit `min-width:
          auto` lets wide content force the column wider than its share, and
          a spreadsheet preview's table did exactly that — pushing the
          extracted-data column off the right of the screen. */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: "var(--space-5)" }}>
        {/* No `mimeType` prop: the stored upload content type is unreliable
            (often empty or octet-stream), so the renderer is resolved
            server-side from the filename instead — see app/preview.py. */}
        <DocumentPreviewPane documentId={review.document.id} filename={review.document.filename} />

        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
          <DedupBanner dedupMatch={dedupMatch} decision={draft.dedupDecision} onChange={(v) => dispatch({ type: "set_dedup_decision", value: v })} />

          <div className="card" style={{ padding: "var(--space-4)" }}>
            <h3 style={{ marginTop: 0 }}>Extracted data</h3>
            <ExtractedDataTable
              columns={tableColumns}
              rows={draft.extractedData}
              confidence={result.data_confidence}
              onChange={(v) => dispatch({ type: "set_extracted_data", value: v })}
            />
          </div>

          <div className="card" style={{ padding: "var(--space-4)" }}>
            <h3 style={{ marginTop: 0 }}>Re-extract</h3>
            <ReextractPanel
              history={Array.isArray(result.reextract_history) ? result.reextract_history : []}
              running={reextracting}
              progress={liveProgress}
              disabled={reextractMutation.isPending || review.status !== "awaiting_review"}
              onSubmit={(feedback) => reextractMutation.mutate(feedback)}
            />
          </div>

          {chunks.length > 0 && (
            <Accordion
              title="Document chunks"
              subtitle="Stored for retrieval when you confirm — this is where anything the schema didn't capture survives."
              badge={<span className="muted">{chunks.length}</span>}
              open={chunksOpen}
              onToggle={() => setChunksOpen((open) => !open)}
            >
              <ChunkCards chunks={chunks} />
            </Accordion>
          )}

          <div className="card" style={{ padding: "var(--space-4)" }}>
            <SchemaMatchSwitcher matchedSchemaId={draft.matchedSchemaId} onChange={(v) => dispatch({ type: "set_matched_schema", value: v })} />
          </div>

          {isAdHoc && (
            <div className="card" style={{ padding: "var(--space-4)" }}>
              <h3 style={{ marginTop: 0 }}>Proposed schema (inferred)</h3>
              <SchemaFieldEditor
                mode="proposed"
                fields={draft.proposedSchemaFields || []}
                onChange={(v) => dispatch({ type: "set_proposed_fields", value: v })}
              />
              <label className="field-label" style={{ marginTop: "var(--space-3)", display: "block" }}>
                Save as reusable schema (optional)
              </label>
              <input
                className="text-input"
                placeholder="e.g. Vendor Invoices"
                value={draft.saveSchemaAs}
                onChange={(e) => dispatch({ type: "set_save_schema_as", value: e.target.value })}
              />
            </div>
          )}

          <div className="card" style={{ padding: "var(--space-4)" }}>
            <h3 style={{ marginTop: 0 }}>Topics</h3>
            <SuggestedTopicsChips
              topicIds={draft.topicIds}
              onChange={(v) => dispatch({ type: "set_topic_ids", value: v })}
              knownTopics={allTopics}
            />
          </div>

          <div style={{ display: "flex", gap: "var(--space-3)", justifyContent: "flex-end" }}>
            <button type="button" className="btn" onClick={() => navigate("/review")}>
              Review later
            </button>
            <button type="button" className="btn btn-danger" disabled={rejectMutation.isPending} onClick={() => rejectMutation.mutate()}>
              Reject
            </button>
            <button
              type="button"
              className="btn"
              disabled={patchMutation.isPending || reextracting}
              onClick={() =>
                patchMutation.mutate(
                  {
                    extractedData: draft.extractedData,
                    proposedSchemaFields: draft.proposedSchemaFields,
                    matchedSchemaId: draft.matchedSchemaId,
                    topicIds: draft.topicIds,
                    newTopicNames: draft.newTopicNames,
                  },
                  { onSuccess: () => showToast("Draft saved.", { variant: "success" }) },
                )
              }
            >
              Save draft
            </button>
            <button type="button" className="btn btn-primary" disabled={confirmDisabled} onClick={() => confirmMutation.mutate()}>
              Confirm
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
