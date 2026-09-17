import { useEffect, useRef, useState } from "react";
import { PencilIcon, PlusIcon, TrashIcon } from "../../../components/common/Icon";
import styles from "./ChatSessionList.module.css";

/**
 * The conversation sidebar.
 *
 * `draftActive` is the state that makes "New chat" behave the way it does
 * everywhere else: a brand-new chat shows as a selected row at the top of
 * the list while being nothing more than local state. It only becomes a
 * real session — with a real id, title and row — once the user asks
 * something in it (see app/routers/query.py).
 */
export function ChatSessionList({
  sessions,
  activeChatId,
  draftActive,
  isLoading,
  onNewChat,
  onSelect,
  onRename,
  onDelete,
}) {
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef(null);

  useEffect(() => {
    if (renamingId) renameInputRef.current?.select();
  }, [renamingId]);

  function startRename(session) {
    setRenamingId(session.id);
    setRenameValue(session.title);
  }

  function commitRename(event) {
    event.preventDefault();
    const title = renameValue.trim();
    if (title) onRename(renamingId, title);
    setRenamingId(null);
  }

  return (
    <aside className={styles.sidebar}>
      <button type="button" className={`btn ${styles.newChat}`} onClick={onNewChat} disabled={draftActive}>
        <PlusIcon /> New chat
      </button>

      <div className={styles.list}>
        {draftActive && (
          <div className={`${styles.row} ${styles.active} ${styles.draft}`}>
            <span className={styles.title}>New chat</span>
            <span className={styles.draftHint}>Unsaved</span>
          </div>
        )}

        {isLoading && <div className={`skeleton ${styles.rowSkeleton}`} />}

        {!isLoading && sessions.length === 0 && !draftActive && (
          <p className={`muted ${styles.empty}`}>No conversations yet.</p>
        )}

        {sessions.map((session) =>
          renamingId === session.id ? (
            <form key={session.id} className={styles.row} onSubmit={commitRename}>
              <input
                ref={renameInputRef}
                className={styles.renameInput}
                value={renameValue}
                onChange={(event) => setRenameValue(event.target.value)}
                onBlur={commitRename}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setRenamingId(null);
                }}
              />
            </form>
          ) : (
            <div
              key={session.id}
              className={`${styles.row} ${session.id === activeChatId && !draftActive ? styles.active : ""}`}
            >
              <button type="button" className={styles.rowButton} onClick={() => onSelect(session.id)}>
                <span className={styles.title}>{session.title}</span>
                {session.last_message_preview && (
                  <span className={styles.preview}>{session.last_message_preview}</span>
                )}
              </button>
              <div className={styles.rowActions}>
                <button
                  type="button"
                  className={styles.iconButton}
                  title="Rename"
                  aria-label={`Rename "${session.title}"`}
                  onClick={() => startRename(session)}
                >
                  <PencilIcon size={12} />
                </button>
                <button
                  type="button"
                  className={styles.iconButton}
                  title="Delete"
                  aria-label={`Delete "${session.title}"`}
                  onClick={() => onDelete(session)}
                >
                  <TrashIcon size={12} />
                </button>
              </div>
            </div>
          ),
        )}
      </div>
    </aside>
  );
}
