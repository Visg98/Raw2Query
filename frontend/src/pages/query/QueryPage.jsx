import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteChat, listChats, renameChat } from "../../api/chats";
import { runQuery } from "../../api/query";
import { useChatSession } from "../../hooks/useChatSession";
import { useToast } from "../../components/common/Toast";
import { ThinkingIndicator } from "../../components/common/ThinkingIndicator";
import { TopicChipInput } from "../../components/TopicChipInput";
import { ChatMessage } from "./components/ChatMessage";
import { ChatInput } from "./components/ChatInput";
import { ChatSessionList } from "./components/ChatSessionList";
import styles from "./QueryPage.module.css";

const EXAMPLE_QUESTIONS = [
  "What was the total spend on invoices last quarter?",
  "Summarize the key terms of the vendor contracts.",
  "Which documents mention a termination clause?",
];

export function QueryPage() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [topicIds, setTopicIds] = useState([]);
  const transcriptRef = useRef(null);

  const {
    activeChatId,
    isDraft,
    messages,
    isLoadingMessages,
    startNewChat,
    selectChat,
    adoptChatId,
    appendMessage,
  } = useChatSession();

  const { data: sessions = [], isLoading: sessionsLoading } = useQuery({
    queryKey: ["chats"],
    queryFn: listChats,
  });

  const queryMutation = useMutation({
    mutationFn: (question) => runQuery({ question, topicIds, chatId: activeChatId }),
  });

  const renameMutation = useMutation({
    mutationFn: ({ chatId, title }) => renameChat(chatId, title),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["chats"] }),
    onError: (err) => showToast(err.message || "Could not rename the chat.", { variant: "error" }),
  });

  const deleteMutation = useMutation({
    mutationFn: (chatId) => deleteChat(chatId),
    onSuccess: (_data, chatId) => {
      queryClient.invalidateQueries({ queryKey: ["chats"] });
      queryClient.removeQueries({ queryKey: ["chat", chatId] });
      if (chatId === activeChatId) startNewChat();
      showToast("Chat deleted.", { variant: "info" });
    },
    onError: (err) => showToast(err.message || "Could not delete the chat.", { variant: "error" }),
  });

  // Keep the newest turn in view, the way every chat UI does.
  useEffect(() => {
    const node = transcriptRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, queryMutation.isPending]);

  function handleAsk(question) {
    appendMessage({ role: "user", content: question });
    queryMutation.mutate(question, {
      onSuccess: (response) => {
        appendMessage({
          role: "assistant",
          content: response.answer,
          meta: {
            routing_used: response.routing_used,
            sql: response.sql,
            rows: response.rows,
            sources: response.sources,
            topic_ids_used: response.topic_ids_used,
          },
        });
        // A draft chat becomes a real session on its first answered
        // question — the backend created the row and told us its id.
        if (response.chat_id && response.chat_id !== activeChatId) adoptChatId(response.chat_id);
        queryClient.invalidateQueries({ queryKey: ["chats"] });
        // Drop this conversation's cached copy too: it's now a turn behind,
        // and it's what the sidebar would replay if the user navigated away
        // and came back.
        if (response.chat_id) {
          queryClient.invalidateQueries({ queryKey: ["chat", response.chat_id] });
        }
      },
      onError: (err) => {
        // Nothing was persisted for a failed question, so this turn stays
        // local: it's a note about what happened, not part of the record.
        appendMessage({
          role: "assistant",
          content: "",
          meta: { error: err.message || "Something went wrong." },
        });
      },
    });
  }

  function handleDelete(session) {
    if (!window.confirm(`Delete "${session.title}"? This can't be undone.`)) return;
    deleteMutation.mutate(session.id);
  }

  const showExamples = messages.length === 0 && !isLoadingMessages && !queryMutation.isPending;

  return (
    <div className={`page ${styles.page}`}>
      <ChatSessionList
        sessions={sessions}
        activeChatId={activeChatId}
        draftActive={isDraft}
        isLoading={sessionsLoading}
        onNewChat={startNewChat}
        onSelect={selectChat}
        onRename={(chatId, title) => renameMutation.mutate({ chatId, title })}
        onDelete={handleDelete}
      />

      <section className={styles.main}>
        <div className="page-header" style={{ marginBottom: "var(--space-4)" }}>
          <h1>{isDraft ? "Ask" : sessions.find((s) => s.id === activeChatId)?.title || "Ask"}</h1>
        </div>

        <div className={styles.topicRow}>
          <label className="field-label">Narrow by topic (optional)</label>
          <TopicChipInput selectedIds={topicIds} onChange={setTopicIds} />
        </div>

        <div className={`card ${styles.transcript}`} ref={transcriptRef}>
          {isLoadingMessages && <div className="skeleton" style={{ height: 120 }} />}

          {showExamples && (
            <div>
              <p className="muted">Try asking:</p>
              <ul className={styles.examples}>
                {EXAMPLE_QUESTIONS.map((q) => (
                  <li key={q}>
                    <button type="button" className="btn btn-sm" onClick={() => handleAsk(q)}>
                      {q}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {messages.map((message) => (
            <ChatMessage key={message.id} message={message} />
          ))}

          {queryMutation.isPending && <ThinkingIndicator />}
        </div>

        <ChatInput onSubmit={handleAsk} disabled={queryMutation.isPending} />
      </section>
    </div>
  );
}
