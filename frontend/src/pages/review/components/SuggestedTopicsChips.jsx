import { TopicChipInput } from "../../../components/TopicChipInput";

/**
 * Suggested topics arrive as pre-filled but REMOVABLE chips (decision #10)
 * — never silently auto-applied. This is the same TopicChipInput used on
 * upload/query, just seeded from the review payload plus any freshly
 * created new-topic names from this same draft.
 */
export function SuggestedTopicsChips({ topicIds, onChange, knownTopics }) {
  return (
    <div>
      <TopicChipInput selectedIds={topicIds} onChange={onChange} knownTopics={knownTopics} />
      <p className="muted" style={{ marginTop: "var(--space-1)" }}>
        Pre-filled chips were suggested automatically — remove any that don't apply.
      </p>
    </div>
  );
}
