import { useState } from "react";
import { SqlPreview } from "../../../components/common/SqlPreview";
import { DataTable } from "../../../components/common/DataTable";
import { SourcesIcon } from "../../../components/common/Icon";
import { SourcesModal } from "./SourcesModal";
import styles from "./ChatMessage.module.css";

/**
 * One turn of a conversation. Roles are the server's (`user` /
 * `assistant`), so a message replayed from `GET /chats/{id}` renders through
 * exactly this component with no translation layer.
 *
 * An assistant turn's supporting detail arrives under `meta` — routing, sql,
 * rows, sources, topics, or `error` — persisted as one blob by the query
 * endpoint.
 */
export function ChatMessage({ message }) {
  const [showSources, setShowSources] = useState(false);

  if (message.role === "user") {
    return <div className={`${styles.message} ${styles.question}`}>{message.content}</div>;
  }

  const meta = message.meta || {};
  const { routing_used: routingUsed, sql, rows, sources, topic_ids_used: topicIdsUsed, error } = meta;

  if (error) {
    return <div className={`${styles.message} ${styles.answer}`}>Couldn't get an answer: {error}</div>;
  }

  return (
    <div className={`${styles.message} ${styles.answer}`}>
      <div className={styles.meta}>
        <span className={styles.routingBadge}>{routingUsed === "sql" ? "SQL" : "RAG"}</span>
        {topicIdsUsed?.length > 0 && <span className="muted">Searched within {topicIdsUsed.length} topic(s)</span>}
      </div>

      <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{message.content}</p>

      {routingUsed === "sql" && sql && (
        <div className={styles.section}>
          <SqlPreview sql={sql} />
        </div>
      )}

      {rows?.length > 0 && (
        <div className={styles.section}>
          <DataTable columns={Object.keys(rows[0]).map((key) => ({ key, label: key }))} rows={rows} />
        </div>
      )}

      {sources?.length > 0 && (
        <div className={styles.section}>
          {/* One button instead of an inline stack of snippet boxes — eight
              sources under every answer pushed the answer itself out of
              view. */}
          <button type="button" className={`btn btn-sm ${styles.sourcesButton}`} onClick={() => setShowSources(true)}>
            <SourcesIcon size={13} />
            {sources.length} source{sources.length === 1 ? "" : "s"}
          </button>
        </div>
      )}

      {showSources && <SourcesModal sources={sources} onClose={() => setShowSources(false)} />}
    </div>
  );
}
