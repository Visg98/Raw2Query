import { useEffect, useRef, useState } from "react";
import { getJob } from "../api/jobs";
import { jobEventsUrl } from "../api/jobs";

const TERMINAL_STATUSES = new Set(["awaiting_review", "confirmed", "rejected", "failed"]);

/**
 * Tracks one job's live status via SSE, seeded by a GET /jobs/{id} fetch so
 * a page refresh mid-extraction still shows correct state (the SSE stream
 * itself carries no history, only deltas from the same DB row). Falls back
 * to polling if the EventSource connection drops without a clean server
 * close; either source updates state, whichever produces a terminal status
 * first wins and both are torn down.
 *
 * The backend closes the SSE stream once status reaches "awaiting_review"
 * too (not only confirmed/rejected/failed) — further changes only happen
 * via explicit user action from there, so there's nothing more to stream.
 */
export function useJobEvents(jobId, { enabled = true } = {}) {
  const [state, setState] = useState({ status: null, progress: null, errorMessage: null, loading: true });
  const pollRef = useRef(null);
  const sourceRef = useRef(null);

  useEffect(() => {
    if (!jobId || !enabled) return undefined;

    let cancelled = false;

    function teardown() {
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }

    function applyJob(job) {
      if (cancelled) return;
      setState({ status: job.status, progress: job.progress, errorMessage: job.error_message, loading: false });
      if (TERMINAL_STATUSES.has(job.status)) teardown();
    }

    // Seed from the persisted row first, so reloading mid-extraction works.
    getJob(jobId).then(applyJob).catch(() => {
      if (!cancelled) setState((s) => ({ ...s, loading: false }));
    });

    const source = new EventSource(jobEventsUrl(jobId));
    sourceRef.current = source;

    source.onmessage = (event) => {
      if (cancelled) return;
      const payload = JSON.parse(event.data);
      setState({ status: payload.status, progress: payload.progress, errorMessage: payload.error_message, loading: false });
      if (TERMINAL_STATUSES.has(payload.status)) teardown();
    };

    source.addEventListener("error", (event) => {
      // Named "error" event from the backend ("job not found") vs. a plain
      // network-level EventSource error (event.data is undefined for the latter).
      if (event.data) {
        teardown();
        setState((s) => ({ ...s, loading: false, errorMessage: "job not found" }));
      }
    });

    // Fallback poll in case the connection drops without a clean close.
    pollRef.current = setInterval(() => {
      getJob(jobId).then(applyJob).catch(() => {});
    }, 2000);

    return () => {
      cancelled = true;
      teardown();
    };
  }, [jobId, enabled]);

  return { ...state, isTerminal: TERMINAL_STATUSES.has(state.status) };
}
