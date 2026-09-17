import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

/** How long the confetti card stays up before the page moves on. */
const CELEBRATION_MS = 3000;

function read(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    // Private mode / blocked storage. The hook degrades to "only notices a
    // batch finishing while you were looking at it", which is the common
    // case anyway — see the note on persistence below.
    return null;
  }
}

/**
 * Batches that left a queue because the user threw them away, rather than
 * because their work finished. One shared key for both queues: a delete in
 * either one is a delete everywhere, and both must stay quiet about it.
 */
const DISCARDED_KEY = "raw2query.discardedBatches";

function write(key, value) {
  try {
    if (value === null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, value);
  } catch {
    // As above — nothing to surface, the queue still works.
  }
}

function readDiscarded() {
  const raw = read(DISCARDED_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/**
 * Marks a batch as deliberately discarded, so neither queue celebrates it
 * disappearing.
 *
 * "Present, then absent" is the only signal the queues have, and a deleted
 * batch satisfies it exactly as a finished one does — which is how deleting
 * a bad upload used to end in confetti and a redirect. The delete is the one
 * caller that knows the difference, so it is the one that has to say so.
 *
 * Kept in sessionStorage next to the watched-batch marker for the same
 * reason: the queue page can be remounted between the delete and the
 * refetched list that drops the rows.
 */
export function markBatchDiscarded(batchId) {
  if (!batchId) return;
  const discarded = readDiscarded();
  if (discarded.includes(batchId)) return;
  // Bounded: a long session deleting many batches should not grow this
  // without limit, and only the most recent ones can still be in a queue's
  // marker slot.
  write(DISCARDED_KEY, JSON.stringify([batchId, ...discarded].slice(0, 20)));
}

/**
 * Watches a queue for its newest batch draining, then hands the caller a
 * batch id to celebrate and navigates on after three seconds.
 *
 * The queue's own data is the signal: both queues are filtered lists (the
 * extraction queue holds pending/extracting/failed, the review queue holds
 * awaiting_review), so a batch whose work is done stops appearing in them
 * altogether. "Present, then absent" is therefore the whole test — and it
 * is the right test rather than a convenient one, because it is false for
 * the case that must not celebrate: a failed extraction keeps its row, so
 * the batch stays in the list and nothing fires.
 *
 * ## Why sessionStorage and not a ref
 *
 * A ref only remembers batches this component instance has seen, and the
 * review queue is remounted by the very action it needs to react to:
 * confirming the last document navigates from /review/:jobId back to
 * /review, so the page mounts fresh into an already-empty queue and a ref
 * would have nothing to compare against. The watched batch id therefore has
 * to outlive the mount. sessionStorage (not local) because it is per-tab and
 * per-session: a batch finished last week is not news on a fresh visit.
 *
 * @param {object} params
 * @param {string} params.storageKey - unique per queue; the two queues watch
 *   the same batches independently and must not clear each other's marker.
 * @param {string[]} params.batchIds - the batch ids currently in this queue,
 *   newest first, and real ids only (no "ungrouped" placeholder — a
 *   batchless document predates batching and has no batch to finish). Only
 *   the newest is watched: it's the one the user just acted on, and
 *   celebrating an older batch that happened to drain first would navigate
 *   them away mid-task.
 * @param {boolean} params.ready - false while the query is still loading. A
 *   loading queue looks identical to a drained one, and acting on it would
 *   fire the celebration on every reload.
 * @param {string} params.navigateTo - where to send the user afterwards.
 * @returns {string | null} the batch id to show a completion banner for.
 */
export function useBatchCompletion({ storageKey, batchIds, ready, navigateTo }) {
  const navigate = useNavigate();
  const [completedBatchId, setCompletedBatchId] = useState(null);

  const newestBatchId = batchIds.length > 0 ? batchIds[0] : null;
  const present = batchIds.join(",");

  useEffect(() => {
    if (!ready) return;

    const watched = read(storageKey);

    // Keyed on the watched batch being gone, not on the queue being empty:
    // an older batch sitting in the queue with a failed row (which is
    // exactly why it's still there) would otherwise mask the newest batch
    // draining, and the upload the user is actually waiting on would finish
    // in silence.
    if (watched && !batchIds.includes(watched)) {
      write(storageKey, null);

      if (!readDiscarded().includes(watched)) {
        setCompletedBatchId(watched);
        return;
      }
      // Otherwise it is gone because the user deleted it, not because it
      // finished: stay silent (the delete reported itself with a toast) and
      // fall through, so whatever is left in the queue is picked up as the
      // new watched batch in this same pass rather than waiting for the
      // list to change again.
    }

    // Track the newest batch in the queue. A newer batch arriving replaces
    // the marker rather than celebrating it — a second upload is not a
    // completion of the first.
    if (newestBatchId && read(storageKey) !== newestBatchId) write(storageKey, newestBatchId);
  }, [ready, newestBatchId, present, storageKey, batchIds]);

  useEffect(() => {
    if (!completedBatchId) return;
    const timer = window.setTimeout(() => navigate(navigateTo), CELEBRATION_MS);
    // Cleared on unmount, so navigating away by hand during the three
    // seconds doesn't yank the user back out of wherever they went.
    // `navigateTo` is in the deps because it has to be, but it is a literal
    // at both call sites, so this never restarts the timer mid-countdown.
    return () => window.clearTimeout(timer);
  }, [completedBatchId, navigate, navigateTo]);

  return completedBatchId;
}
